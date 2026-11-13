"""Evidence corruption tests use temporary projects, never production files."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import background_capture as capture
import extension_native_host as native
from intake_state import digest, normalize


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="intel-capture-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.request = "test-background-reader"
        self.run_id = "extension-probe-" + digest(self.request.encode())[:24]
        self.folder = self.root / "evidence/extension-probe" / self.run_id
        self.folder.mkdir(parents=True)
        self.identity = {"category_id": "ai-models", "conversation_id": capture.SOURCES["ai-models"][1], "turn_key": "test-turn"}
        self.identity_hash = digest(capture._json_bytes(self.identity))
        self.body = "# 完整报告\r\n金额 12 元\r\n".encode()
        self.body_path = self.folder / "reports" / (self.identity_hash + ".md")
        self.meta_path = self.body_path.with_suffix(".json")
        self.body_path.parent.mkdir()
        self.body_path.write_bytes(self.body)
        self.meta = {"schema_version": 1, "identity": self.identity, "identity_sha256": self.identity_hash,
                     "is_report": True, "title": capture.SOURCES["ai-models"][0], "path": "/c/" + self.identity["conversation_id"],
                     "report_time": "2026-10-16T00:03:10.256+08:00", "report_date_beijing": "2026-10-16",
                     "captured_at": "2026-10-18T13:01:00+08:00", "original_sha256": digest(self.body), "original_bytes": len(self.body),
                     "guard": {"installed_early": True, "copy_intercepted": 1, "real_clipboard_calls": 0,
                               "legacy_copy_calls": 0, "clipboard_read_calls": 0}, "table_count": 0, "links": [],
                     "history_dates": ["2026-10-15T00:00:00+08:00"]}
        self.run = {"schema_version": 1, "mode": "probe", "run_id": self.run_id, "request_id": self.request,
                    "config_sha256": "a" * 64, "status": "complete", "scan_start": "2026-10-16T00:00:00+08:00",
                    "scan_end": "2026-10-18T13:00:00+08:00", "created_at": "2026-10-18T13:00:00+08:00",
                    "finished_at": "2026-10-18T13:02:00+08:00", "updated_at": "2026-10-18T13:02:00+08:00",
                    "sources": [{"category_id": c, "task_name": n, "conversation_id": v} for c, (n, v) in capture.SOURCES.items()],
                    "categories": {c: {"complete": True, "reason": "boundary_before_start", "boundary_time": "2026-10-15T00:00:00+08:00",
                                       "dates": ["2026-10-15T00:00:00+08:00"], "page_count": 0} for c in capture.SOURCES},
                    "reports": {self.identity_hash: {"body_file": "reports/" + self.body_path.name,
                                 "metadata_file": "reports/" + self.meta_path.name, "category_id": "ai-models",
                                 "original_sha256": digest(self.body), "report_time": self.meta["report_time"]}}, "skipped": {}}

    def save(self):
        self.meta_path.write_bytes(capture._json_bytes(self.meta))
        (self.folder / "run.json").write_bytes(capture._json_bytes(self.run))

    def load(self):
        self.save()
        return capture.load_capture(self.folder, self.root)

    def rejected(self, reason):
        with self.assertRaisesRegex(capture.CaptureError, reason):
            self.load()

    def test_complete_capture_keeps_exact_raw_body_time_and_sha(self):
        result = self.load()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(result["complete_categories"]), 6)
        self.assertEqual(result["failed_categories"], {})
        item = result["captures"][0]
        self.assertEqual(item["raw_bytes"], self.body)
        self.assertEqual(item["original"].encode(), self.body)
        self.assertEqual(item["generated_at"], self.meta["report_time"])
        self.assertEqual(item["metadata_sha256"], digest(self.meta_path.read_bytes()))
        self.assertEqual(self.root / item["metadata_file"], self.meta_path)

    def test_corrupt_body_is_rejected(self):
        self.body_path.write_bytes(self.body.replace(b"12", b"13"))
        self.rejected("original_sha256_mismatch")

    def test_metadata_length_mismatch_is_rejected(self):
        self.meta["original_bytes"] += 1
        self.rejected("original_length_mismatch")

    def test_wrong_metadata_identity_is_rejected(self):
        self.meta["identity"] = {**self.identity, "turn_key": "some-other-turn"}
        self.rejected("report_identity_hash_mismatch")

    def test_source_conversation_cannot_be_replaced(self):
        self.run["sources"][0]["conversation_id"] = "another-conversation"
        self.rejected("fixed_source_mismatch")

    def test_duplicate_source_cannot_hide_missing_sixth_source(self):
        self.run["sources"][-1] = deepcopy(self.run["sources"][0])
        self.rejected("fixed_source_mismatch")

    def test_request_id_must_match_run_hash(self):
        self.run["request_id"] = "different-request"
        self.rejected("request_run_id_mismatch")

    def test_report_path_cannot_traverse_to_another_file(self):
        self.run["reports"][self.identity_hash]["body_file"] = "../report.md"
        self.rejected("report_path_mismatch")

    def test_run_path_rejects_dotdot_even_if_it_resolves_inside_project(self):
        self.save()
        with self.assertRaisesRegex(capture.CaptureError, "path_traversal"):
            capture.load_capture(self.folder / ".." / self.folder.name, self.root)

    def test_linked_body_is_rejected(self):
        other = self.root / "other.md"
        other.write_bytes(self.body)
        self.body_path.unlink()
        try:
            self.body_path.symlink_to(other)
        except OSError:
            self.skipTest("Windows account does not permit creating test symlinks")
        self.rejected("linked_evidence_path")

    def test_naive_time_and_invalid_calendar_date_rejected(self):
        for value in ("2026-10-18T13:00:00", "2026-02-30T13:00:00+08:00", "tomorrow"):
            with self.subTest(value=value):
                self.run["scan_end"] = value
                self.rejected("invalid_timestamp")

    def test_report_time_outside_scan_rejected(self):
        self.meta["report_time"] = "2026-10-19T00:03:00+08:00"
        self.run["reports"][self.identity_hash]["report_time"] = self.meta["report_time"]
        self.rejected("report_outside_scan_or_time_mismatch")

    def test_report_generation_day_not_inferred_from_capture_day(self):
        self.meta["report_date_beijing"] = "2026-10-18"
        self.rejected("report_generation_date_mismatch")

    def test_copy_guard_requires_real_interception_without_clipboard_use(self):
        for field, value in (("copy_intercepted", 0), ("real_clipboard_calls", 1), ("clipboard_read_calls", 1), ("legacy_copy_calls", 1)):
            with self.subTest(field=field):
                before = self.meta["guard"][field]
                self.meta["guard"][field] = value
                self.rejected("copy_guard_failed")
                self.meta["guard"][field] = before

    def test_partial_run_only_returns_completed_categories_as_importable(self):
        self.run["status"] = "partial"
        self.run["categories"]["ai-models"] = {"complete": False, "reason": "history_load_timeout", "error": "timed out", "page_count": 1}
        result = self.load()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["complete_categories"]), 5)
        self.assertEqual(result["captures"], [])
        self.assertEqual(len(result["incomplete_captures"]), 1)
        self.assertEqual(result["failed_categories"]["ai-models"]["reason"], "history_load_timeout")

    def test_native_host_failure_without_page_count_preserves_other_completed_sources(self):
        # This is the actual worker catch shape accepted by the native host.
        config = {"extension_id": "a" * 32, "enabled": True, "mode": "probe",
                  "request_id": self.request, "scan_start": self.run["scan_start"],
                  "scan_end": self.run["scan_end"], "sources": self.run["sources"]}
        config_path = self.root / ".intake/extension-probe/config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(capture._json_bytes(config))
        host = native.Host(self.root, ["chrome-extension://" + "a" * 32 + "/"])
        self.run["status"] = "partial"
        self.run["categories"].pop("ai-products")
        coverage = {"complete": False, "reason": "read_failed", "error": "undated_report"}
        response = host.finish_category({"category_id": "ai-products", "coverage": coverage}, self.run)
        self.assertFalse(response["complete"])
        result = self.load()
        self.assertEqual(len(result["complete_categories"]), 5)
        self.assertNotIn("ai-products", result["complete_categories"])
        self.assertEqual(result["failed_categories"]["ai-products"], coverage)
        self.assertNotIn("page_count", result["failed_categories"]["ai-products"])
        self.assertEqual(len(result["captures"]), 1)
        self.assertEqual(result["captures"][0]["raw_bytes"], self.body)

    def test_missing_page_count_never_promotes_failed_source_reports(self):
        self.run["status"] = "partial"
        self.run["categories"]["ai-models"] = {"complete": False, "reason": "read_failed", "error": "undated_report"}
        result = self.load()
        self.assertEqual(result["captures"], [])
        self.assertEqual(len(result["incomplete_captures"]), 1)
        self.assertEqual(result["status"], "partial")

    def test_completed_source_still_requires_explicit_page_count(self):
        self.run["categories"]["ai-models"].pop("page_count")
        self.rejected("invalid_page_count")

    def test_explicit_invalid_page_count_rejected_for_complete_and_failed_sources(self):
        for complete in (False, True):
            self.run["status"] = "complete" if complete else "partial"
            self.run["categories"]["ai-models"]["complete"] = complete
            for value in (None, False, -1, 10001, 1.5, "0", []):
                with self.subTest(complete=complete, value=value):
                    self.run["categories"]["ai-models"]["page_count"] = value
                    self.rejected("invalid_page_count")

    def test_complete_flag_cannot_override_failed_category(self):
        self.run["categories"]["ai-models"]["complete"] = False
        self.rejected("run_status_coverage_mismatch")

    def test_history_boundary_needs_an_actual_observed_older_date(self):
        self.run["categories"]["ai-models"]["dates"] = []
        self.rejected("unproven_history_boundary")

    def test_unfinished_run_cannot_be_imported(self):
        self.run["status"] = "running"
        self.rejected("run_not_finished")

    def test_unindexed_report_cannot_be_silently_omitted(self):
        (self.body_path.parent / "extra.md").write_text("not indexed", encoding="utf-8")
        self.rejected("unindexed_report_files")

    def test_empty_index_with_saved_reports_is_not_zero_report_success(self):
        self.run["reports"] = {}
        self.rejected("unindexed_report_files")

    def test_optional_pinned_metadata_hash_is_checked(self):
        self.run["reports"][self.identity_hash]["metadata_sha256"] = "0" * 64
        self.rejected("metadata_sha256_mismatch")

    def test_duplicate_json_keys_are_rejected(self):
        self.save()
        (self.folder / "run.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(capture.CaptureError, "duplicate_json_key"):
            capture.load_capture(self.folder, self.root)


class LegacyProofTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="intel-proof-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "data/originals").mkdir(parents=True)
        (self.root / ".intake/old").mkdir(parents=True)
        self.body = "# 报告\n金额 12 元\n".encode()
        self.capture_file = ".intake/old/report.md"
        (self.root / self.capture_file).write_bytes(self.body)
        (self.root / "data/originals/intel.md").write_bytes(self.body)
        self.meta = {"category_id": "ai-models", "task_name": capture.SOURCES["ai-models"][0], "file": "report.md",
                     "sha256": digest(self.body), "ui_timestamp": "今天 0:03", "capture_date": "2026-10-16", "header": "报告"}
        self.record = {"id": "intel-one", "category_id": "ai-models", "source_task": capture.SOURCES["ai-models"][0],
                       "original_file": "data/originals/intel.md", "original_sha256": digest(self.body)}
        self.entry = {"category_id": "ai-models", "generated_at": "2026-10-16T00:03:00+08:00", "record_id": "intel-one",
                      "capture_file": self.capture_file, "raw_sha256": digest(self.body),
                      "normalized_sha256": digest(normalize(self.body.decode()).encode())}
        self.state = {"occurrences": {"old-one": self.entry}}

    def proof(self):
        (self.root / "data/catalog.json").write_bytes(capture._json_bytes({"records": [self.record]}))
        (self.root / (self.capture_file + ".json")).write_bytes(capture._json_bytes(self.meta))
        return capture.legacy_proofs(self.root, self.state)

    def test_actual_ui_sidecar_body_and_source_create_proof(self):
        proof = self.proof()["old-one"]
        self.assertEqual(proof["precision"], "minute")
        self.assertEqual(proof["evidence_sha256"], digest((self.root / proof["evidence_file"]).read_bytes()))
        self.assertEqual(proof["generated_at"], self.entry["generated_at"])

    def test_zero_seconds_without_ui_evidence_is_not_a_proof(self):
        self.meta.pop("ui_timestamp")
        self.assertEqual(self.proof(), {})

    def test_proof_rejects_changed_raw_body_instead_of_rehashing_it(self):
        (self.root / self.capture_file).write_bytes(b"different body")
        with self.assertRaisesRegex(capture.CaptureError, "legacy_capture_sha256_mismatch"):
            self.proof()

    def test_proof_rejects_wrong_source(self):
        self.meta["task_name"] = capture.SOURCES["ai-products"][0]
        with self.assertRaisesRegex(capture.CaptureError, "legacy_metadata_source_mismatch"):
            self.proof()

    def test_relative_ui_day_is_resolved_using_capture_day(self):
        self.meta["ui_timestamp"] = "昨天 0:03"
        with self.assertRaisesRegex(capture.CaptureError, "legacy_ui_date_mismatch"):
            self.proof()

    def test_precision_with_milliseconds_is_not_aliased(self):
        self.entry["generated_at"] = "2026-10-16T00:03:00.000+08:00"
        self.assertEqual(self.proof(), {})

    def test_legacy_capture_date_format_and_invalid_day_are_rejected(self):
        for value in ("20261016", "2026-02-30", "2026-10-16T00:00:00"):
            with self.subTest(value=value):
                self.meta["capture_date"] = value
                with self.assertRaisesRegex(capture.CaptureError, "invalid_legacy_capture_date"):
                    self.proof()

    def test_metadata_sha_must_bind_to_legacy_capture(self):
        self.meta["sha256"] = "0" * 64
        with self.assertRaisesRegex(capture.CaptureError, "legacy_metadata_sha256_mismatch"):
            self.proof()

    def test_inventory_requires_body_header_and_minute_ui_match(self):
        self.record["source_capture"] = {"timezone": "Asia/Shanghai", "generated_at": self.entry["generated_at"],
                                         "captured_at": "2026-10-18T00:23:23+08:00", "ui_timestamp": "星期五 0:03",
                                         "evidence_file": ".intake/old/inventory-ai-models.json"}
        # Match real first import: catalog original has no sidecar; a separate
        # original copy sidecar ties inventory header to the same body digest.
        self.entry["capture_file"] = "data/originals/intel.md"
        self.meta.pop("ui_timestamp")
        inventory = self.root / ".intake/old/inventory-ai-models.json"
        inventory.write_bytes(capture._json_bytes([{"header": "报告", "ui_timestamp": "星期五 0:03"}]))
        self.assertEqual(len(self.proof()), 1)
        inventory.write_bytes(capture._json_bytes([{"header": "不同报告", "ui_timestamp": "星期五 0:03"}]))
        with self.assertRaisesRegex(capture.CaptureError, "legacy_inventory_body_binding_ambiguous"):
            self.proof()


if __name__ == "__main__":
    unittest.main()
