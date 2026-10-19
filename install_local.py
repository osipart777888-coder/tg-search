"""Create local launchers without compilation or system-wide Python changes."""
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def install():
    os.umask(0o077)
    target = Path.home() / "Library" / "Application Support" / "Telegram Contacts" if sys.platform == "darwin" else ROOT
    target.mkdir(parents=True, exist_ok=True)
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
exec .venv/bin/python app.py
''', encoding="utf-8")
    command.chmod(0o755)
    if sys.platform == "darwin":
        app = Path.home() / "Desktop" / "Telegram Контакты.app"
        contents = app / "Contents"
        executable = contents / "MacOS" / "TelegramContacts"
        executable.parent.mkdir(parents=True, exist_ok=True)
        (contents / "Resources").mkdir(exist_ok=True)
        plist = {"CFBundleName": "Telegram Контакты", "CFBundleDisplayName": "Telegram Контакты",
                 "CFBundleIdentifier": "local.tgsearch.contacts", "CFBundleVersion": "1.0",
                 "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL",
                 "CFBundleExecutable": "TelegramContacts", "NSHighResolutionCapable": True}
        (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
        executable.write_text("#!/bin/zsh\numask 077\ncd " + shlex.quote(str(target)) +
                              "\nmkdir -p .local\nexec " + shlex.quote(str(python)) + " " + shlex.quote(str(target / "app.py")) +
                              " >> .local/app.log 2>&1\n", encoding="utf-8")
        executable.chmod(0o755)
        results = target / "Результаты"
        results.mkdir(exist_ok=True)
        shortcut = Path.home() / "Desktop" / "Результаты Telegram"
        if not shortcut.exists() and not shortcut.is_symlink():
            shortcut.symlink_to(results, target_is_directory=True)
        print(app)
        print(shortcut)
    print(target)
    print(command)


if __name__ == "__main__":
    install()
