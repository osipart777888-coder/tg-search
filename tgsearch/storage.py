import csv
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / ".local"
RESULTS = ROOT / "Результаты"
CONFIG = PRIVATE / "config.json"


def prepare_storage():
    os.umask(0o077)
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    PRIVATE.chmod(0o700)
    RESULTS.mkdir(exist_ok=True)


def load_config():
    prepare_storage()
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def atomic_text(path, text, encoding="utf-8"):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.chmod(0o600)
    tmp.replace(path)


def save_config(config):
    prepare_storage()
    atomic_text(CONFIG, json.dumps(config, ensure_ascii=False, indent=2))


def export_txt(path, contacts):
    names = sorted({c.username.lower() for c in contacts})
    atomic_text(path, "".join(f"@{name}\n" for name in names), "utf-8-sig")


def export_csv(path, contacts):
    def safe(value):
        text = str(value)
        return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text

    with Path(path).open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Ник", "Роль", "Группа / канал", "Источник", "Подтверждение"])
        for contact in contacts:
            for evidence in contact.evidence:
                writer.writerow([safe(contact.username), safe(evidence.role_label),
                                 safe(evidence.title), safe(evidence.source), safe(evidence.excerpt)])
