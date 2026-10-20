import csv
import io
import json
import os
import re
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / '.local'
RESULTS = ROOT / 'Результаты'
CONFIG = PRIVATE / 'config.json'
_SECRETS = set()


def redact(value):
    text = str(value)
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, '[скрыто]')
    return re.sub(r'(?i)\b[a-f0-9]{32}\b', '[скрыто]', text)


def prepare_storage():
    os.umask(0o077)
    for root in (PRIVATE, RESULTS):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        for path in root.rglob('*'):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)


def load_config():
    from .security import Keychain
    prepare_storage()
    try:
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if config.get('api_hash'):
        save_config(config)  # Verified Keychain migration before plaintext replacement.
    elif config.get('keychain_account'):
        value = Keychain().get(config['keychain_account'])
        if value:
            config['api_hash'] = value.decode()
    if config.get('api_hash'):
        _SECRETS.add(config['api_hash'])
    return config


def atomic_text(path, text, encoding='utf-8'):
    from .security import atomic_bytes
    atomic_bytes(Path(path), text.encode(encoding))


def save_config(config):
    from .security import Keychain
    prepare_storage()
    disk = dict(config)
    secret = disk.pop('api_hash', None)
    if secret:
        _SECRETS.add(secret)
        account = disk.setdefault('keychain_account', 'api-hash-' + uuid.uuid4().hex)
        chain = Keychain()
        chain.set(account, secret.encode())
        if chain.get(account) != secret.encode():
            raise ValueError('Не удалось проверить API Hash в Keychain. Старые настройки сохранены.')
        config['keychain_account'] = account
    atomic_text(CONFIG, json.dumps(disk, ensure_ascii=False, indent=2))


def export_txt(path, contacts):
    names = sorted({c.username.lower() for c in contacts if c.username and getattr(c, 'verification', 'verified') == 'verified'})
    atomic_text(path, ''.join(f'@{name}\n' for name in names), 'utf-8-sig')


def export_csv(path, contacts):
    def safe(value):
        text = redact(value)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    out = io.StringIO(newline='')
    writer = csv.writer(out, delimiter=';')
    writer.writerow(['Ник', 'Роль', 'Группа / канал', 'Источник', 'Подтверждение',
                     'Telegram ID', 'Метод', 'Достоверность', 'Последняя проверка', 'Сообщество', 'Статус'])
    for contact in contacts:
        if getattr(contact, 'verification', 'verified') != 'verified':
            continue
        for evidence in contact.evidence:
            writer.writerow([safe(contact.username), safe(evidence.role_label), safe(evidence.title),
                             safe(evidence.source), safe(evidence.excerpt), contact.user_id,
                             safe(evidence.method_label), safe(evidence.confidence_label),
                             safe(getattr(contact, 'checked_at', '') or evidence.checked_at),
                             safe(evidence.community), 'Новый' if contact.is_new else 'Ранее найден'])
    atomic_text(path, out.getvalue(), 'utf-8-sig')
