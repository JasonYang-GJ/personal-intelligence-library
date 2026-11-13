"""Management integration checks in disposable libraries, never the real archive.

Capture transport is stubbed with an empty, complete six-source batch. The real
builders, weekly/daily transactions, event loader, and intake auditor are used.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "scripts"))

import test_weekly_review as fixture
import background_intake as intake
import build
import intake_audit_checks as audit
import intake_state
from intake_alias import SOURCES
from management_state import load_management
import weekly_review as weekly


PROJECT_ID = "00000000-0000-4000-8000-000000000005"


class ManagementIntegration(unittest.TestCase):
    # Reuse only setup/helpers; do not inherit or rerun the weekly test suite.
    save_drafts = fixture.WeeklyReviewTransactions.save_drafts

    def setUp(self):
        fixture.WeeklyReviewTransactions.setUp(self)
        shutil.copyfile(TESTS.parent / "scripts/management_state.py", self.root / "scripts/management_state.py")
        intake.write(self.root / "data/management/project.json", {"schema_version": 1, "project_id": PROJECT_ID})
        self.events = self.root / "data/management/events"
        self.events.mkdir()
        self.event_counter = 0
        self.input_counter = 0
        build.build(self.root)
        self.original_before = (self.root / self.record["original_file"]).read_bytes()
        self.ledger_before = (self.root / "data/intake-state.json").read_bytes()
        self.batch = {
            "captures": [], "run_dir": "evidence/fixture-management-capture", "run_sha256": "a" * 64,
            "status": "complete", "complete_categories": list(SOURCES), "failed_categories": {},
            "scan_start": "2026-10-19T00:00:00+08:00", "scan_end": "2026-10-25T10:01:00+08:00",
            "run": {"finished_at": "2026-10-25T10:02:00+08:00"},
        }
        intake.write(self.root / self.batch["run_dir"] / "run.json", self.batch["run"])
        self.batch["run_sha256"] = intake.sha((self.root / self.batch["run_dir"] / "run.json").read_bytes())

    def append_event(self, record_id, action, state, restore_to=None):
        self.event_counter += 1
        event = {
            "schema_version": 1, "project_id": PROJECT_ID, "id": str(uuid.uuid4()),
            "record_id": record_id, "action": action, "state": state,
            "restore_to": restore_to or state, "order": 100 + self.event_counter,
            "created_at": "2026-10-25T02:03:00.000Z",
        }
        path = self.events / (event["id"] + ".json")
        with path.open("xb") as handle:
            handle.write(intake.encode(event))
        return path

    def event_bytes(self):
        return {path.name: path.read_bytes() for path in self.events.glob("*.json")}

    def embedded(self):
        html = (self.root / build.OUTPUT).read_text(encoding="utf-8")
        blocks = re.findall(r'<script\s+type="application/json"\s+id="library-data">(.*?)</script>', html, re.S)
        self.assertEqual(len(blocks), 1)
        return json.loads(blocks[0])

    def prepare_current_weekly_input(self):
        self.input_counter += 1
        output = f".intake/weekly-review/management/input-{self.input_counter}.json"
        result = weekly.prepare_input(self.root, output=output, **self.input_args)
        self.packet["input_sha256"] = result["input_sha256"]
        self.save_drafts()

    def daily(self, run_id):
        with patch("background_capture.legacy_proofs", return_value={}), \
             patch("background_capture.load_capture", return_value=deepcopy(self.batch)):
            return intake.prepare_transaction(self.root, run_id, self.batch, {})

    def audit_daily(self, run_id):
        with patch.object(audit, "legacy_proofs", return_value={}), \
             patch.object(audit, "load_capture", return_value=deepcopy(self.batch)):
            return audit.validate_intake(self.root, f"evidence/daily/{run_id}/result.json")

    def test_real_events_survive_weekly_then_daily_html_and_originals_stay_exact(self):
        self.append_event(self.record["id"], "archive", "archived")
        build.build(self.root)
        self.prepare_current_weekly_input()
        result = weekly.commit(self.root, packet=self.packet_name, markdown=self.markdown_name)
        self.assertEqual(result["status"], "committed")
        after_weekly = self.embedded()
        self.assertEqual(after_weekly["management"]["states"][self.record["id"]]["state"], "archived")
        self.assertEqual(after_weekly["management"]["fingerprint"], load_management(self.root)["fingerprint"])
        self.assertEqual((self.root / "data/intake-state.json").read_bytes(), self.ledger_before)

        self.append_event(result["review_id"], "trash", "trashed", "active")
        preserved_events = self.event_bytes()
        daily_result = self.daily("bg-management-after-weekly")
        self.assertEqual(daily_result["status"], "complete")
        after_daily = self.embedded()
        self.assertEqual(after_daily["management"]["states"][self.record["id"]]["state"], "archived")
        self.assertEqual(after_daily["management"]["states"][result["review_id"]]["state"], "trashed")
        self.assertEqual(after_daily["management"]["event_count"], 2)
        self.assertEqual(after_daily["management"]["fingerprint"], load_management(self.root)["fingerprint"])
        self.assertEqual(after_daily["weekly_reviews"][0]["original"].encode(), self.markdown)
        self.assertEqual(self.event_bytes(), preserved_events)
        self.assertEqual((self.root / self.record["original_file"]).read_bytes(), self.original_before)
        self.assertEqual((self.root / result["original_file"]).read_bytes(), self.markdown)
        self.assertEqual(len(after_daily["records"]), 1)
        self.assertEqual(len(after_daily["weekly_reviews"]), 1)

    def test_event_appended_after_daily_preparation_is_not_a_recovery_target(self):
        self.append_event(self.record["id"], "archive", "archived")
        build.build(self.root)
        run_id = "bg-management-interrupted"
        with patch.object(intake, "apply_transaction", side_effect=OSError("stop after journal preparation")):
            with self.assertRaisesRegex(OSError, "journal preparation"):
                self.daily(run_id)
        journal_path = self.root / f".intake/background/runs/{run_id}/journal.json"
        prepared = intake.read_json(journal_path)
        self.assertEqual(prepared["status"], "prepared")
        self.assertFalse(any(name.startswith("data/management/events/") for name in prepared["baseline"]))
        self.assertFalse(any(item["path"].startswith("data/management/") for item in prepared["operations"]))

        self.append_event(self.record["id"], "trash", "trashed", "archived")
        preserved_events = self.event_bytes()
        intake.recover_transactions(self.root, acknowledge_intake=False)
        self.assertEqual(intake.read_json(journal_path)["status"], "committed")
        self.assertEqual(self.event_bytes(), preserved_events)
        self.assertEqual(load_management(self.root)["states"][self.record["id"]]["state"], "trashed")
        # The prepared HTML remains its explicitly identified older snapshot;
        # live events are authoritative and are loaded on browser connection.
        display = self.embedded()["management"]
        self.assertEqual(display["states"][self.record["id"]]["state"], "archived")
        manifest = intake.read_json(self.root / "evidence/build-manifest.json")
        self.assertEqual(manifest["management_snapshot"], display)
        self.assertNotEqual(display["fingerprint"], load_management(self.root)["fingerprint"])
        self.assertEqual((self.root / self.record["original_file"]).read_bytes(), self.original_before)
        self.assertTrue(all(item["checked_through"] == self.batch["scan_end"]
                            for item in intake_state.read_state(self.root)["tasks"].values()))
        before_retry = intake.snapshot(self.root)
        intake.recover_transactions(self.root, acknowledge_intake=False)
        self.assertEqual(intake.snapshot(self.root), before_retry)
        self.assertEqual(self.event_bytes(), preserved_events)
        audit_result = self.audit_daily(run_id)
        self.assertEqual(audit_result["status"], "pass", audit_result)

    def test_later_event_does_not_make_readonly_homepage_audit_fail(self):
        run_id = "bg-management-audit"
        self.daily(run_id)
        before = self.audit_daily(run_id)
        self.assertEqual(before["status"], "pass", before)
        html_before = (self.root / build.OUTPUT).read_bytes()
        manifest_before = (self.root / "evidence/build-manifest.json").read_bytes()
        self.append_event(self.record["id"], "archive", "archived")
        preserved_events = self.event_bytes()
        self.assertEqual(self.embedded()["management"]["states"], {})
        self.assertEqual(load_management(self.root)["states"][self.record["id"]]["state"], "archived")

        after = self.audit_daily(run_id)
        self.assertEqual(after["status"], "pass", after)
        self.assertNotIn("homepage_not_updated", [item["code"] for item in after["issues"]])
        self.assertEqual((self.root / build.OUTPUT).read_bytes(), html_before)
        self.assertEqual((self.root / "evidence/build-manifest.json").read_bytes(), manifest_before)
        self.assertEqual(self.event_bytes(), preserved_events)

        # The management exception must not hide an actually modified homepage.
        with (self.root / build.OUTPUT).open("ab") as handle:
            handle.write(b"<!-- unexpected homepage modification -->")
        corrupted = self.audit_daily(run_id)
        self.assertEqual(corrupted["status"], "issues")
        self.assertIn("homepage_not_updated", [item["code"] for item in corrupted["issues"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
