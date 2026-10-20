"""macOS Keychain and encrypted, in-memory Telethon SQLite sessions.

No secrets are passed to a shell, command line, JSON configuration or diagnostics.
"""
import ctypes as C
import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon.sessions import SQLiteSession

SERVICE = 'local.tgsearch.contacts'
MAGIC = b'TGSEARCH-AESGCM-1\n'


class Keychain:
    def __init__(self):
        if sys.platform != 'darwin':
            raise ValueError('Защищённое хранение требует macOS Keychain.')
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec = C.CDLL('/System/Library/Frameworks/Security.framework/Security')
        p = C.c_void_p
        for name, restype, args in [
            ('CFStringCreateWithCString', p, [p, C.c_char_p, C.c_uint32]),
            ('CFDataCreate', p, [p, p, C.c_long]),
            ('CFDictionaryCreateMutable', p, [p, C.c_long, p, p]),
            ('CFDictionarySetValue', None, [p, p, p]),
            ('CFDataGetLength', C.c_long, [p]), ('CFDataGetBytePtr', p, [p]),
            ('CFRelease', None, [p])]:
            fn = getattr(self.cf, name)
            fn.restype, fn.argtypes = restype, args
        for name, args in [('SecItemCopyMatching', [p, C.POINTER(p)]),
                           ('SecItemAdd', [p, C.POINTER(p)]), ('SecItemUpdate', [p, p]),
                           ('SecItemDelete', [p])]:
            fn = getattr(self.sec, name)
            fn.restype, fn.argtypes = C.c_int32, args

    def symbol(self, name):
        return C.c_void_p.in_dll(self.sec, name).value

    def dictionary(self, values):
        keys = C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryKeyCallBacks'))
        vals = C.addressof(C.c_byte.in_dll(self.cf, 'kCFTypeDictionaryValueCallBacks'))
        result = self.cf.CFDictionaryCreateMutable(None, 0, keys, vals)
        for key, value in values.items():
            self.cf.CFDictionarySetValue(result, self.symbol(key), value)
        return result

    def query(self, account):
        service = self.cf.CFStringCreateWithCString(None, SERVICE.encode(), 0x08000100)
        name = self.cf.CFStringCreateWithCString(None, account.encode(), 0x08000100)
        try:
            return self.dictionary({'kSecClass': self.symbol('kSecClassGenericPassword'),
                                    'kSecAttrService': service, 'kSecAttrAccount': name})
        finally:
            self.cf.CFRelease(service)
            self.cf.CFRelease(name)

    @staticmethod
    def check(status):
        if status:
            raise ValueError(f'Keychain недоступен (статус {status}). Разблокируйте Связку ключей; данные сохранены.')

    def get(self, account):
        query = self.query(account)
        result = C.c_void_p()
        self.cf.CFDictionarySetValue(query, self.symbol('kSecReturnData'),
                                    C.c_void_p.in_dll(self.cf, 'kCFBooleanTrue').value)
        try:
            status = self.sec.SecItemCopyMatching(query, C.byref(result))
            if status == -25300:
                return None
            self.check(status)
            return C.string_at(self.cf.CFDataGetBytePtr(result), self.cf.CFDataGetLength(result))
        finally:
            if result.value:
                self.cf.CFRelease(result)
            self.cf.CFRelease(query)

    def set(self, account, value):
        query = self.query(account)
        data = self.cf.CFDataCreate(None, C.cast(C.c_char_p(value), C.c_void_p), len(value))
        attrs = self.dictionary({'kSecValueData': data})
        try:
            status = self.sec.SecItemUpdate(query, attrs)
            if status == -25300:
                self.cf.CFDictionarySetValue(query, self.symbol('kSecValueData'), data)
                self.cf.CFDictionarySetValue(query, self.symbol('kSecAttrAccessible'),
                                            self.symbol('kSecAttrAccessibleWhenUnlockedThisDeviceOnly'))
                status = self.sec.SecItemAdd(query, None)
            self.check(status)
        finally:
            self.cf.CFRelease(query)
            self.cf.CFRelease(attrs)
            self.cf.CFRelease(data)


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def encryption_key(keychain=None):
    keychain = keychain or Keychain()
    key = keychain.get('session-encryption-v1')
    if key is None:
        key = AESGCM.generate_key(bit_length=256)
        keychain.set('session-encryption-v1', key)
        if keychain.get('session-encryption-v1') != key:
            raise ValueError('Не удалось проверить ключ шифрования в Keychain.')
    return key


def seal(data, key):
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def unseal(data, key):
    if not data.startswith(MAGIC):
        raise ValueError('Неизвестный формат защищённого файла.')
    body = data[len(MAGIC):]
    try:
        return AESGCM(key).decrypt(body[:12], body[12:], MAGIC)
    except Exception:
        raise ValueError('Не удалось расшифровать файл. Нужен исходный ключ из Keychain; файл сохранён.') from None


class EncryptedSession(SQLiteSession):
    """Keep the complete Telethon SQLite database in RAM, encrypt every durable save.

    Migration atomically replaces the old session with verified ciphertext at the
    same path. Auth key, entity access hashes and update state all survive.
    """
    def __init__(self, path, keychain=None):
        self.protected_path = Path(path).resolve()
        self.key = encryption_key(keychain)
        self.deleted = False
        self.initializing = True
        self.digest = None
        super().__init__(None)
        self._conn.commit()
        try:
            if self.protected_path.exists():
                raw = self.protected_path.read_bytes()
                if raw.startswith(MAGIC):
                    raw = unseal(raw, self.key)
                elif raw.startswith(b'SQLite format 3\0'):
                    # backup() also includes any committed rollback journal state.
                    from contextlib import closing
                    with closing(sqlite3.connect(f'{self.protected_path.as_uri()}?mode=ro', uri=True)) as old:
                        old.backup(self._conn)
                        raw = self._conn.serialize()
                else:
                    raise ValueError('Формат сессии не распознан. Исходный файл сохранён.')
                self._conn.deserialize(raw)
                if self._conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Проверка сессии не пройдена. Исходный файл сохранён.')
                from telethon.sessions.sqlite import CURRENT_VERSION
                version = self._conn.execute('SELECT version FROM version').fetchone()[0]
                if version > CURRENT_VERSION:
                    raise ValueError('Сессия создана более новой версией Telethon. Исходный файл сохранён.')
                if version < CURRENT_VERSION:
                    self._upgrade_database(old=version)
                    self._conn.execute('UPDATE version SET version=?', (CURRENT_VERSION,))
                    self._conn.commit()
                # Reload Telethon's in-memory properties from the restored database.
                row = self._conn.execute('SELECT * FROM sessions').fetchone()
                if row:
                    from telethon.crypto import AuthKey
                    self._dc_id, self._server_address, self._port, auth_key, self._takeout_id, tmp_key = row
                    self._auth_key = AuthKey(data=auth_key)
                    self._tmp_auth_key = AuthKey(data=tmp_key)
        except Exception:
            self._conn.close()
            self._conn = None
            raise
        self.initializing = False
        self.save()

    def save(self):
        if self.initializing or self.deleted or self._conn is None or not hasattr(self, 'key'):
            return
        super().save()
        raw = self._conn.serialize()
        digest = hashlib.sha256(raw).digest()
        if digest != self.digest:
            protected = seal(raw, self.key)
            if unseal(protected, self.key) != raw:
                raise ValueError('Проверка шифрования сессии не пройдена.')
            atomic_bytes(self.protected_path, protected)
            self.digest = digest

    def close(self):
        self.save()
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def delete(self):
        # Only Telethon logout invokes this, following the user's logout action.
        self.deleted = True
        self.protected_path.unlink(missing_ok=True)
        return True


def account_lock(session):
    from .control import session_lock
    root = Path.home() / 'Library' / 'Caches' / 'Telegram Contacts'
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    root.chmod(0o700)
    name = 'account-' + session_fingerprint(session) + '.lock'
    return session_lock(root, name)


def session_fingerprint(session):
    auth_key = session.auth_key.key if session.auth_key else b'not-yet-authorized'
    return hashlib.sha256(auth_key).hexdigest()
