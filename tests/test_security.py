import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession
from tgsearch import storage
from tgsearch.engine import Contact, Evidence
from tgsearch.history import ContactHistory
from tgsearch.maintenance import clear_history, obsolete_reports, remove_reports
from tgsearch.security import EncryptedSession, MAGIC, seal, unseal


class FakeKeychain:
    def __init__(self):
        self.values = {}
    def get(self, account):
        return self.values.get(account)
    def set(self, account, value):
        self.values[account] = value


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.chain = FakeKeychain()

    def test_migrates_complete_sqlite_session_without_plaintext_copy(self):
        path = self.root / 'telegram.session'
        old = SQLiteSession(str(path))
        old.set_dc(2, '149.154.167.51', 443)
        old.auth_key = AuthKey(data=b'k' * 256)
        old._conn.execute('INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)', (1234, 5678, 'testuser', None, 'Public name', 123))
        old.save()
        old.close()
        session = EncryptedSession(path, self.chain)
        self.assertEqual(session.auth_key.key, b'k' * 256)
        self.assertEqual(session.get_input_entity(1234).access_hash, 5678)
        session.close()
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(MAGIC))
        self.assertNotIn(b'k' * 256, raw)
        self.assertEqual([p.name for p in self.root.iterdir()], ['telegram.session'])
        restored = EncryptedSession(path, self.chain)
        self.assertEqual(restored.dc_id, 2)
        self.assertEqual(restored.auth_key.key, b'k' * 256)
        restored.close()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_legacy_session_migration_accepts_relative_installer_path(self):
        path = self.root / 'telegram.session'
        old = SQLiteSession(str(path)); old.save(); old.close()
        original = Path.cwd()
        try:
            os.chdir(self.root)
            session = EncryptedSession(Path('telegram.session'), self.chain)
            session.close()
        finally:
            os.chdir(original)
        self.assertTrue(path.read_bytes().startswith(MAGIC))

    def test_encrypted_session_detects_tampering_preserving_original_file(self):
        path = self.root / 'telegram.session'
        session = EncryptedSession(path, self.chain)
        session.close()
        broken = bytearray(path.read_bytes())
        broken[-1] ^= 1
        path.write_bytes(broken)
        with self.assertRaises(ValueError):
            EncryptedSession(path, self.chain)
        self.assertEqual(path.read_bytes(), broken)

    def test_keychain_failure_leaves_existing_session_intact(self):
        path = self.root / 'telegram.session'
        old = SQLiteSession(str(path)); old.save(); old.close()
        raw = path.read_bytes()
        with patch.object(self.chain, 'get', side_effect=ValueError('Keychain locked')):
            with self.assertRaises(ValueError):
                EncryptedSession(path, self.chain)
        self.assertEqual(path.read_bytes(), raw)

    def test_config_hash_migration_and_round_trip_never_write_hash_back_to_json(self):
        config = self.root / 'config.json'
        secret = 'b' * 32
        config.write_text(json.dumps({'api_id': 123, 'api_hash': secret, 'phone': '+70000000000'}))
        with patch.object(storage, 'PRIVATE', self.root), patch.object(storage, 'RESULTS', self.root / 'results'), patch.object(storage, 'CONFIG', config), patch('tgsearch.security.Keychain', return_value=self.chain):
            loaded = storage.load_config()
            self.assertEqual(loaded['api_hash'], secret)
            self.assertNotIn(secret, config.read_text())
            self.assertEqual(storage.load_config()['api_hash'], secret)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_config_migration_failure_preserves_plaintext_until_keychain_verified(self):
        config = self.root / 'config.json'
        raw = json.dumps({'api_id': 123, 'api_hash': 'c'*32})
        config.write_text(raw)
        with patch.object(storage, 'PRIVATE', self.root), patch.object(storage, 'RESULTS', self.root / 'results'), patch.object(storage, 'CONFIG', config), patch('tgsearch.security.Keychain', return_value=self.chain), patch.object(self.chain, 'get', return_value=None):
            with self.assertRaises(ValueError):
                storage.load_config()
        self.assertEqual(config.read_text(), raw)

    def test_secret_redaction_and_incomplete_export(self):
        secret = 'e' * 32
        storage._SECRETS.add('test-password-secret')
        self.addCleanup(storage._SECRETS.discard, 'test-password-secret')
        good = Contact('gooduser', 1, evidence=[Evidence('owner_contact', 'Title', 'https://t.me/test', secret + ' test-password-secret')])
        incomplete = Contact('unknownuser', 2, verification='incomplete', evidence=good.evidence)
        txt, csv = self.root / 'out.txt', self.root / 'out.csv'
        storage.export_txt(txt, [good, incomplete])
        storage.export_csv(csv, [good, incomplete])
        self.assertNotIn('unknownuser', txt.read_text())
        self.assertNotIn('unknownuser', csv.read_text())
        self.assertNotIn(secret, csv.read_text())
        self.assertNotIn('test-password-secret', csv.read_text())
        self.assertEqual(csv.stat().st_mode & 0o777, 0o600)

    def test_old_schema_migration_retains_contacts_aliases_and_evidence(self):
        path = self.root / 'history.sqlite3'
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE contacts (user_id INTEGER PRIMARY KEY, username TEXT, bot INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
            CREATE TABLE evidence (user_id INTEGER NOT NULL,role TEXT NOT NULL,title TEXT NOT NULL,source TEXT NOT NULL,excerpt TEXT NOT NULL,PRIMARY KEY(user_id,role,source,excerpt));
            INSERT INTO contacts VALUES (55,'olduser',0,'2026-01-01','2026-01-02');
            INSERT INTO evidence VALUES (55,'owner_contact','Title','https://t.me/test','Владелец');
        """)
        db.close()
        history = ContactHistory(path)
        record = history.get(55)
        self.assertEqual(record['username'], 'olduser')
        self.assertEqual(record['first_seen'], '2026-01-01')
        self.assertEqual(len(record['evidence']), 1)
        self.assertEqual(record['checked_at'], '')  # Migration is not a live check.
        self.assertEqual(ContactHistory(path).total(), 1)
        self.assertFalse(history.remember(Contact('renameduser', 55), run_id='new'))
        self.assertEqual(history.get(55)['aliases'], ['renameduser'])

    def test_batch_and_checkpoint_transaction_roll_back_together(self):
        history = ContactHistory(self.root / 'history.sqlite3')
        with self.assertRaises(ValueError):
            history.remember_many([Contact('validuser', 1), Contact('bad name', 2)], run_id='run', snapshot={'complete': False})
        self.assertEqual(history.total(), 0)
        self.assertIsNone(history.unfinished())

    def test_retention_preserves_legacy_unfinished_latest_and_exports(self):
        reports = self.root / 'reports'; reports.mkdir()
        for name, complete in [('old', True), ('unfinished', False), ('latest', True), ('legacy', None)]:
            path = reports / (name + '.json')
            path.write_text(json.dumps({'complete': complete}))
            os.utime(path, (0, 0))
        (self.root / 'last_search.json').write_text(json.dumps({'report': str(reports / 'latest.json')}))
        paths = obsolete_reports(self.root, 30)
        self.assertEqual([p.stem for p in paths], ['old'])
        with self.assertRaises(ValueError):
            remove_reports(paths, self.root)
        remove_reports(paths, self.root, confirmed=True)
        self.assertTrue((reports / 'unfinished.json').exists())
        self.assertTrue((reports / 'legacy.json').exists())

    def test_clear_requires_confirmation_and_preserves_login_settings_cooldown_and_txt(self):
        (self.root / 'reports').mkdir()
        for name in ('contacts.sqlite3', 'telegram.session', 'config.json', 'search_settings.json', 'cooldown.json', 'old.txt'):
            (self.root / name).write_text('preserve')
        (self.root / 'reports/test.json').write_text('{}')
        with self.assertRaises(ValueError):
            clear_history(self.root)
        self.assertTrue((self.root / 'contacts.sqlite3').exists())
        clear_history(self.root, confirmed=True)
        self.assertFalse((self.root / 'contacts.sqlite3').exists())
        for name in ('telegram.session', 'config.json', 'search_settings.json', 'cooldown.json', 'old.txt'):
            self.assertEqual((self.root / name).read_text(), 'preserve')
