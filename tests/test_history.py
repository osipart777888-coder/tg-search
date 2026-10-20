import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import Application
from tgsearch.engine import Contact, Evidence
from tgsearch.history import ContactHistory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "contacts.sqlite3"
        self.history = ContactHistory(self.path)
        self.contact = Contact("firstuser", 100, evidence=[Evidence("advertising", "Юристы", "https://t.me/lawyers", "По рекламе @firstuser")])

    def test_reopened_history_remembers_stable_id(self):
        self.assertTrue(self.history.remember(self.contact, run_id="run1"))
        reopened = ContactHistory(self.path)
        self.assertFalse(reopened.remember(self.contact, run_id="run2"))
        self.assertEqual(reopened.total(), 1)

    def test_same_run_keeps_new_status_for_repeated_sources(self):
        self.assertTrue(self.history.remember(self.contact, run_id="run1"))
        self.contact.evidence.append(Evidence("admin_contact", "Адвокаты", "https://t.me/advocates", "Админ @firstuser"))
        self.assertTrue(self.history.remember(self.contact, run_id="run1"))
        self.assertTrue(ContactHistory(self.path).remember(self.contact, run_id="run1"))
        self.assertFalse(self.history.remember(self.contact, run_id="run2"))
        self.assertEqual(len(self.history.get(100)["evidence"]), 2)

    def test_renamed_account_is_known_and_export_uses_new_handle(self):
        self.history.remember(self.contact)
        self.contact.username = "renameduser"
        self.assertFalse(self.history.remember(self.contact))
        record = self.history.get(100)
        self.assertEqual(record["username"], "renameduser")
        self.assertEqual(record["aliases"], ["firstuser", "renameduser"])
        export = self.root / "all.txt"
        self.history.export_txt(export)
        self.assertEqual(export.read_text(encoding="utf-8-sig"), "@renameduser\n")

    def test_handle_reassigned_to_different_id_is_new(self):
        self.history.remember(self.contact)
        self.assertTrue(self.history.remember(Contact("firstuser", 200)))
        self.assertEqual(self.history.total(), 2)
        self.assertIsNone(self.history.get(100)["username"])
        export = self.root / "all.txt"
        self.assertEqual(self.history.export_txt(export), 1)
        self.assertEqual(export.read_text(encoding="utf-8-sig"), "@firstuser\n")

    def test_old_import_cannot_revert_live_rename(self):
        self.history.remember(self.contact, seen_at="2026-10-19T10:00:00+03:00")
        self.contact.username = "renameduser"
        self.history.remember(self.contact, seen_at="2026-10-19T11:00:00+03:00")
        self.contact.username = "firstuser"
        self.history.remember(self.contact, seen_at="2026-10-19T09:00:00+03:00")
        self.assertEqual(self.history.get(100)["username"], "renameduser")
        self.assertEqual(self.history.get(100)["first_seen"], "2026-10-19T09:00:00+03:00")

    def test_import_legacy_reports_once_without_changing_old_files(self):
        reports = self.root / "reports"
        reports.mkdir()
        report = reports / "legacy.json"
        raw = json.dumps({"contacts": [asdict(self.contact)]}, ensure_ascii=False).encode()
        report.write_bytes(raw)
        old_txt = reports / "old.txt"
        old_txt.write_text("@firstuser\n")
        # Channel listings do not have a 'contacts' array and are ignored.
        (reports / "channels.json").write_text(json.dumps([{"id": 999, "username": "lawyers_channel"}]))
        migrated = ContactHistory(self.path, reports)
        self.assertEqual(migrated.total(), 1)
        self.assertFalse(migrated.remember(self.contact, run_id="new-run"))
        self.assertEqual(ContactHistory(self.path, reports).total(), 1)
        self.assertEqual(report.read_bytes(), raw)
        self.assertEqual(old_txt.read_text(), "@firstuser\n")

    def test_malformed_report_does_not_erase_existing_history(self):
        self.history.remember(self.contact)
        reports = self.root / "reports"
        reports.mkdir()
        path = reports / "broken.json"
        path.write_text('{"contacts": [')
        migrated = ContactHistory(self.path, reports)
        self.assertEqual(migrated.total(), 1)
        self.assertEqual(migrated.migration_errors, ["broken.json"])
        path.write_text(json.dumps({"contacts": [asdict(Contact("seconduser", 200))]}))
        self.assertEqual(ContactHistory(self.path, reports).total(), 2)

    def test_invalid_contacts_are_not_recorded(self):
        for contact in (Contact("bad user", 100), Contact("validname", -100), Contact("validname", True)):
            with self.assertRaises(ValueError):
                self.history.remember(contact)
        self.assertEqual(self.history.total(), 0)

    def test_evidence_is_deduplicated_and_database_is_private(self):
        self.history.remember(self.contact)
        self.history.remember(self.contact)
        self.assertEqual(len(self.history.get(100)["evidence"]), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_gui_export_new_only_and_full_history_are_separate(self):
        from types import MethodType
        fresh = Contact("freshuser", 200, evidence=self.contact.evidence)
        known = Contact("firstuser", 100, evidence=self.contact.evidence, is_new=False)
        self.history.remember_many([known, fresh])
        view = NS(contacts={100: known, 200: fresh}, history=self.history,
                  export_role=NS(get=lambda: 'Все типы'),
                  export_confidence=NS(get=lambda: 'Любая достоверность'))
        new = Application.selected_exports(view)
        full = Application.selected_exports(view, full=True)
        self.assertEqual([c.username for c in new], ['freshuser'])
        self.assertEqual(sorted(c.username for c in full), ['firstuser', 'freshuser'])
        calls = []
        view.request_export = lambda *a, **kw: calls.append((a, kw))
        Application.save_as(view)
        Application.save_history(view)
        self.assertEqual(calls, [(('txt',), {}), (('txt',), {'full': True})])
