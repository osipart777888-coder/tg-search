"""Persistent public contact history, keyed by Telegram user ID."""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .storage import PRIVATE, atomic_text


def timestamp():
    return datetime.now(ZoneInfo("Europe/Moscow")).isoformat()


class ContactHistory:
    def __init__(self, path=None, reports_dir=None):
        self.path = Path(path) if path is not None else PRIVATE / "contacts.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS contacts (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    bot INTEGER NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS contacts_username ON contacts(username);
                CREATE TABLE IF NOT EXISTS aliases (
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    PRIMARY KEY (user_id, username)
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    excerpt TEXT NOT NULL,
                    PRIMARY KEY (user_id, role, source, excerpt)
                );
                CREATE TABLE IF NOT EXISTS run_contacts (
                    run_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    is_new INTEGER NOT NULL,
                    PRIMARY KEY (run_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS imported_reports (
                    path TEXT PRIMARY KEY,
                    digest TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)
        self.migration_errors = []
        if reports_dir is not None:
            self.import_reports(Path(reports_dir))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember(self, contact, run_id=None, seen_at=None):
        data = asdict(contact) if is_dataclass(contact) else contact
        user_id = data["user_id"]
        username = str(data["username"]).lower().removeprefix("@")
        if type(user_id) is not int or user_id <= 0 or not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", username):
            raise ValueError("Некорректный контакт для истории.")
        when = seen_at or timestamp()
        evidence = data.get("evidence", [])
        with self.connection() as db:
            # Serialize classification and insertion: another search cannot mark the
            # same ID as new between this SELECT and INSERT.
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM contacts WHERE user_id = ?", (user_id,)).fetchone()
            is_new = old is None
            latest = old is None or when >= old["last_seen"]
            if old is not None:
                db.execute("UPDATE contacts SET first_seen=MIN(first_seen, ?) WHERE user_id=?", (when, user_id))
            if latest:
                # A Telegram handle can be reassigned to a different account. Keep
                # the older account by ID but stop exporting its stale handle.
                holder = db.execute("SELECT last_seen FROM contacts WHERE username = ? AND user_id <> ? ORDER BY last_seen DESC LIMIT 1",
                                    (username, user_id)).fetchone()
                current_name = username
                if holder is not None and holder["last_seen"] > when:
                    current_name = None
                else:
                    db.execute("UPDATE contacts SET username = NULL WHERE username = ? AND user_id <> ?", (username, user_id))
                db.execute("""INSERT INTO contacts VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                    bot=excluded.bot, last_seen=excluded.last_seen""",
                           (user_id, current_name, bool(data.get("bot", False)), when, when))
            db.execute("""INSERT INTO aliases VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, username) DO UPDATE SET
                first_seen=MIN(first_seen, excluded.first_seen), last_seen=MAX(last_seen, excluded.last_seen)""",
                       (user_id, username, when, when))
            for item in evidence:
                if is_dataclass(item):
                    item = asdict(item)
                db.execute("""INSERT INTO evidence VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, role, source, excerpt) DO UPDATE SET title=excluded.title""",
                           (user_id, item["role"], item["title"], item["source"], item["excerpt"]))
            if run_id is not None:
                db.execute("INSERT OR IGNORE INTO run_contacts VALUES (?, ?, ?)", (run_id, user_id, is_new))
                is_new = bool(db.execute("SELECT is_new FROM run_contacts WHERE run_id=? AND user_id=?", (run_id, user_id)).fetchone()[0])
        return is_new

    def total(self):
        with self.connection() as db:
            return db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    def get(self, user_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM contacts WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["aliases"] = [r[0] for r in db.execute("SELECT username FROM aliases WHERE user_id=? ORDER BY username", (user_id,))]
            result["evidence"] = [dict(r) for r in db.execute("SELECT role, title, source, excerpt FROM evidence WHERE user_id=?", (user_id,))]
            return result

    def export_txt(self, path):
        with self.connection() as db:
            names = [row[0] for row in db.execute("SELECT DISTINCT username FROM contacts WHERE username IS NOT NULL ORDER BY username")]
        atomic_text(Path(path), "".join(f"@{name}\n" for name in names), "utf-8-sig")
        return len(names)

    def import_reports(self, directory):
        if not directory.is_dir():
            return
        paths = sorted(directory.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        for path in paths:
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                key = str(path.resolve())
                with self.connection() as db:
                    imported = db.execute("SELECT digest FROM imported_reports WHERE path=?", (key,)).fetchone()
                if imported is not None and imported[0] == digest:
                    continue
                report = json.loads(raw)
                if not isinstance(report, dict) or not isinstance(report.get("contacts"), list):
                    continue
                # Ignore malformed files as a whole; retry after they are repaired.
                for contact in report["contacts"]:
                    if (not isinstance(contact, dict) or type(contact.get("user_id")) is not int
                            or contact["user_id"] <= 0 or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", str(contact.get("username", "")))
                            or not isinstance(contact.get("evidence", []), list)):
                        raise ValueError("Некорректный отчёт")
                    for item in contact.get("evidence", []):
                        if not isinstance(item, dict) or any(not isinstance(item.get(field), str) for field in ("role", "title", "source", "excerpt")):
                            raise ValueError("Некорректный источник")
                when = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo("Europe/Moscow")).isoformat()
                for contact in report["contacts"]:
                    self.remember(contact, seen_at=when)
                with self.connection() as db:
                    db.execute("INSERT OR REPLACE INTO imported_reports VALUES (?, ?)", (key, digest))
            except (OSError, ValueError, TypeError, KeyError):
                self.migration_errors.append(path.name)
