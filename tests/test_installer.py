import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace as NS
import install_local


class InstallerTests(unittest.TestCase):
    def test_macos_upgrade_preserves_user_data_and_creates_desktop_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / 'Desktop').mkdir()
            source = home / 'source'; source.mkdir()
            for name in ('app.py', 'search_telegram.py', 'find_top_legal.py', 'curate_top_legal.py', 'requirements.txt', 'README.md'):
                (source / name).write_text('# application code')
            for name in ('tgsearch', 'tests'):
                (source / name).mkdir()
                (source / name / '__init__.py').write_text('')
            target = home / 'Library/Application Support/Telegram Contacts'
            private = target / '.local'; private.mkdir(parents=True)
            old = {'telegram.session': b'encrypted-session', 'config.json': b'{"api_id":123}', 'contacts.sqlite3': b'database', 'search_settings.json': b'{}'}
            for name, raw in old.items():
                (private / name).write_bytes(raw)
            (private / 'reports').mkdir(); (private / 'reports/previous.json').write_text('{}')
            (target / 'Результаты').mkdir(); (target / 'Результаты/old.txt').write_text('@olduser')
            with patch.object(install_local, 'ROOT', source), patch('install_local.Path.home', return_value=home), patch('install_local.sys.platform', 'darwin'), patch('install_local.subprocess.run', return_value=NS(returncode=0)) as run:
                install_local.install()
            for name, raw in old.items():
                self.assertEqual((private / name).read_bytes(), raw)
            self.assertEqual((target / 'Результаты/old.txt').read_text(), '@olduser')
            self.assertTrue((private / 'reports/previous.json').exists())
            wrapper = home / 'Desktop/Telegram Контакты.app/Contents/MacOS/TelegramContacts'
            self.assertTrue(wrapper.exists())
            self.assertIn(str(target), wrapper.read_text())
            self.assertIn('/usr/bin/arch -' + install_local.platform.machine(), wrapper.read_text())
            self.assertTrue((home / 'Desktop/Результаты Telegram').is_symlink())
            self.assertTrue(any('Keychain' in str(c) or 'EncryptedSession' in str(c) for c in run.call_args_list))
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
