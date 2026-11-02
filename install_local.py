"""Create local launchers without compilation or system-wide Python changes."""
import os
import plistlib
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from contextlib import ExitStack

ROOT = Path(__file__).resolve().parent


def install():
    os.umask(0o077)
    target = Path.home() / "Library" / "Application Support" / "Telegram Contacts" if sys.platform == "darwin" else ROOT
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    target.chmod(0o700)
    # Old releases use app.lock; new backends additionally hold session.lock.
    from tgsearch.control import session_lock
    with ExitStack() as locks:
        for name in ('app.lock', 'session.lock', 'search.lock'):
            locks.enter_context(session_lock(target / '.local', name))
        return install_locked(target)


def install_locked(target):
    if target != ROOT:
        for name in ("app.py", "search_telegram.py", "find_top_legal.py", "curate_top_legal.py", "requirements.txt", "README.md"):
            shutil.copy2(ROOT / name, target / name)
        for name in ("tgsearch", "tests"):
            shutil.copytree(ROOT / name, target / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        private = target / ".local"
        private.mkdir(mode=0o700, exist_ok=True)
        for name in ("config.json", "telegram.session"):
            source = ROOT / ".local" / name
            destination = private / name
            if source.exists() and not destination.exists():
                shutil.copy2(source, destination)
                destination.chmod(0o600)
    python = target / ".venv/bin/python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(target / ".venv")], check=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(target / "requirements.txt")], check=True)
    # Run migrations with the installed interpreter, after dependencies exist.
    # No credentials enter argv: read them locally and store them in Keychain.
    migration = "from tgsearch.storage import prepare_storage, load_config; from tgsearch.security import EncryptedSession; from tgsearch.history import ContactHistory; from pathlib import Path; prepare_storage(); load_config(); p=Path('.local/telegram.session'); s=EncryptedSession(p) if p.exists() else None; s.close() if s else None; ContactHistory(reports_dir=Path('.local/reports'))"
    subprocess.run([str(python), '-c', migration], cwd=target, check=True)
    if target != ROOT and (ROOT / '.local/config.json').exists():
        subprocess.run([str(python), '-c', migration], cwd=ROOT, check=True)
    if sys.platform == 'darwin':
        # Exclude private local files from ordinary Time Machine backups. An
        # encrypted backup can be made explicitly from the Data settings tab.
        protected = {target / '.local', target / 'Результаты', ROOT / '.local', ROOT / 'Результаты'}
        for directory in sorted(protected):
            if directory.exists():
                result = subprocess.run(['/usr/bin/tmutil', 'addexclusion', str(directory)], capture_output=True)
                if result.returncode:
                    print('Time Machine: исключение не подтверждено; проверьте настройки резервного копирования.')
    architecture = platform.machine()
    if sys.platform == 'darwin' and architecture in {'arm64', 'x86_64'}:
        (target / '.local/python_arch').write_text(architecture + '\n', encoding='ascii')
    command = ROOT / "Запустить.command"
    command.write_text('''#!/bin/zsh
tg_contacts_project="${0:A:h}"
tg_contacts_install="$HOME/Library/Application Support/Telegram Contacts"
if [[ "$OSTYPE" == darwin* && -x "$tg_contacts_install/.venv/bin/python" ]]; then
    cd "$tg_contacts_install" || exit 1
else
    cd "$tg_contacts_project" || exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    print "Сначала установите программу по инструкции в README.md."
    exit 1
fi
if [[ "$OSTYPE" == darwin* && -r .local/python_arch ]]; then
    read -r tg_contacts_arch < .local/python_arch
    if [[ "$tg_contacts_arch" == arm64 || "$tg_contacts_arch" == x86_64 ]]; then
        exec /usr/bin/arch -"$tg_contacts_arch" .venv/bin/python app.py
    fi
fi
exec .venv/bin/python app.py
''', encoding="utf-8")
    command.chmod(0o755)
    if sys.platform == "darwin":
        if architecture not in {'arm64', 'x86_64'}:
            raise ValueError('Неизвестная архитектура Python.')
        app = Path.home() / "Desktop" / "Telegram Контакты.app"
        contents = app / "Contents"
        executable = contents / "MacOS" / "TelegramContacts"
        executable.parent.mkdir(parents=True, exist_ok=True)
        (contents / "Resources").mkdir(exist_ok=True)
        plist = {"CFBundleName": "Telegram Контакты", "CFBundleDisplayName": "Telegram Контакты",
                 "CFBundleIdentifier": "local.tgsearch.contacts", "CFBundleVersion": "2.0",
                 "CFBundleShortVersionString": "2.0", "CFBundlePackageType": "APPL",
                 "CFBundleExecutable": "TelegramContacts", "NSHighResolutionCapable": True}
        (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
        executable.write_text("#!/bin/zsh\numask 077\ncd " + shlex.quote(str(target)) +
                              "\nmkdir -p .local\nexec /usr/bin/arch -" + architecture + " " + shlex.quote(str(python)) + " " + shlex.quote(str(target / "app.py")) +
                              " >> .local/app.log 2>&1\n", encoding="utf-8")
        executable.chmod(0o755)
        results = target / "Результаты"
        results.mkdir(mode=0o700, exist_ok=True)
        results.chmod(0o700)
        shortcut = Path.home() / "Desktop" / "Результаты Telegram"
        if not shortcut.exists() and not shortcut.is_symlink():
            shortcut.symlink_to(results, target_is_directory=True)
        print(app)
        print(shortcut)
    print(target)
    print(command)


if __name__ == "__main__":
    try:
        install()
    except Exception as exc:
        print(f'Установка остановлена ({type(exc).__name__}). Данные сохранены. Закройте программу и повторите установку.')
        raise SystemExit(1) from None
