"""Weekly review safety checks in disposable libraries, never the real archive."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import background_intake as intake
import build
from intake_alias import SOURCES
import intake_state
import weekly_review as weekly

PROJECT = Path(__file__).resolve().parents[1]
SLOT = "2026-10-25T10:00:00+08:00"
CURRENT = datetime.fromisoformat("2026-10-25T10:05:00+08:00")


class WeeklyReviewTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="weekly-review-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        names = ["PROJECT.md", "AGENTS.md", "使用说明.txt", "scripts/build.py",
                 "ui/template.html", "ui/styles.css", "ui/app.js"]
        if (PROJECT / "ui/management.js").is_file():
            names.append("ui/management.js")
        for name in names:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / name, target)
        original = b"# Original report\n\nKnown problem, no verified demand.\n"
        self.record = {"id": "intel-20261018-001", "category_id": "user-pains", "title": "待验证的文件任务",
                       "source_task": "真实用户痛点雷达", "collected_at": "2026-10-18", "report_date": None,
                       "summary": "本条只是线索。", "audience": "文件工具使用者", "relevance": "整理者判断：需要验证。",
                       "recommendation": "继续观察", "recommendation_reason": "没有付费证据。", "next_action": "询问实际耗时。",
                       "status": "待验证", "uncertainties": ["付费意愿未知"], "tags": ["文件"], "sources": [],
                       "related_ids": [], "original_file": "data/originals/intel-20261018-001.md",
                       "original_sha256": hashlib.sha256(original).hexdigest()}
        target = self.root / self.record["original_file"]
        target.parent.mkdir(parents=True)
        target.write_bytes(original)
        self.catalog = {"schema_version": 1, "library_name": "测试情报库", "updated_at": "2026-10-18",
                        "categories": [{"id": key, "label": label, "short_label": label,
                                        "description": "测试类别", "focus": "真实证据"} for key, label in build.CATEGORIES.items()],
                        "records": [deepcopy(self.record)], "last_intake": {"status": "complete", "summary": "保持原值"}}
        intake.write(self.root / "data/catalog.json", self.catalog)
        ledger = {"schema_version": 1, "normalization_version": 1, "occurrences": {},
                  "tasks": {category: {"checked_through": "2026-10-25T09:00:00+08:00", "run_id": "daily-before"}
                            for category in SOURCES}}
        intake.write(self.root / "data/intake-state.json", ledger)
        build.build(self.root)
        self.clock = patch.object(intake, "now", return_value=CURRENT)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.packet_name = ".intake/weekly-review/test/packet.json"
        self.markdown_name = ".intake/weekly-review/test/review.md"
        self.input_name = ".intake/weekly-review/test/input.json"
        self.input_args = {"scheduled_for": SLOT, "coverage_start": "2026-10-19T00:00:00+08:00",
                           "coverage_end": "2026-10-25T09:00:00+08:00"}
        self.before = intake.snapshot(self.root)
        weekly.prepare_input(self.root, output=self.input_name, **self.input_args)
        context = intake.read_json(self.root / self.input_name)
        self.packet = {"schema_version": 1, **self.input_args, "input_sha256": context["input_sha256"],
                       "title": "本周只验证一个文件任务", "summary": "尚无实际验证反馈，先问清真实耗时。",
                       "candidate_record_ids": [self.record["id"]], "selected_record_id": self.record["id"],
                       "next_action": "用约 30 分钟整理一次真实操作耗时；没有反馈就保持未知。", "unknowns": ["用户尚未提供验证结果"]}
        self.markdown = "# 本周复盘\r\n\r\n完整原文 & <script>不执行</script>\r\n".encode("utf-8")
        self.save_drafts()

    def save_drafts(self):
        intake.write(self.root / self.packet_name, self.packet)
        (self.root / self.markdown_name).write_bytes(self.markdown)

    def commit(self):
        return weekly.commit(self.root, packet=self.packet_name, markdown=self.markdown_name)

    def catalog_now(self):
        return intake.read_json(self.root / "data/catalog.json")

    def journals(self):
        return list((self.root / ".intake/background/runs").glob("*/journal.json"))

    def assert_original_state(self):
        for name, digest in self.before.items():
            self.assertEqual(intake.sha((self.root / name).read_bytes()), digest, name)

    def test_input_contains_full_evidence_and_does_not_change_archive(self):
        context = intake.read_json(self.root / self.input_name)
        self.assertEqual(context["records"][0]["original"].encode(), (self.root / self.record["original_file"]).read_bytes())
        self.assertIn("fingerprint", context["management"])
        self.assertEqual(context["weekly_reviews"], [])
        self.assertEqual(context["input_sha256"], self.packet["input_sha256"])
        self.assert_original_state()

    def test_commit_preserves_daily_state_and_embeds_exact_review(self):
        result = self.commit()
        self.assertEqual(result["status"], "committed")
        catalog = self.catalog_now()
        self.assertEqual(catalog["records"], self.catalog["records"])
        self.assertEqual(catalog["categories"], self.catalog["categories"])
        self.assertEqual(catalog["last_intake"], self.catalog["last_intake"])
        self.assertEqual(intake.sha((self.root / "data/intake-state.json").read_bytes()), self.before["data/intake-state.json"])
        self.assertEqual((self.root / result["original_file"]).read_bytes(), self.markdown)
        review = catalog["weekly_reviews"][0]
        self.assertEqual(review["id"], "review-20261025")
        self.assertEqual(review["generated_at"], CURRENT.isoformat())
        self.assertEqual(review["source_task_id"], build.WEEKLY_REVIEW_TASK_ID)
        self.assertEqual(review["source_thread_id"], build.WEEKLY_REVIEW_THREAD_ID)
        manifest = intake.read_json(self.root / "evidence/build-manifest.json")
        self.assertEqual((manifest["category_count"], manifest["record_count"], manifest["weekly_review_count"]), (6, 1, 1))
        self.assertEqual(manifest["sha256"][review["original_file"]], review["original_sha256"])
        html = (self.root / build.OUTPUT).read_text(encoding="utf-8")
        embedded = re.search(r'<script[^>]+id="library-data"[^>]*>(.*?)</script>', html, re.S).group(1)
        self.assertNotIn("<script>", embedded)
        self.assertEqual(json.loads(embedded)["weekly_reviews"][0]["original"].encode(), self.markdown)
        journal = intake.read_json(self.journals()[0])
        self.assertNotIn("data/intake-state.json", [item["path"] for item in journal["operations"]])
        self.assertEqual(journal["status"], "committed")

    def test_same_packet_and_exact_bytes_are_idempotent_even_with_json_format_changes(self):
        self.commit()
        before = intake.snapshot(self.root)
        (self.root / self.packet_name).write_text(json.dumps(self.packet, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        self.assertEqual(self.commit()["status"], "already_committed")
        self.assertEqual(intake.snapshot(self.root), before)
        self.assertEqual(len(self.journals()), 1)

    def test_same_schedule_with_changed_packet_or_original_refuses_overwrite(self):
        self.commit()
        before = intake.snapshot(self.root)
        for change in ("packet", "original"):
            with self.subTest(change=change):
                if change == "packet":
                    self.packet["summary"] += "新的结论。"
                else:
                    self.packet["summary"] = "尚无实际验证反馈，先问清真实耗时。"
                    self.markdown += b"\n"
                self.save_drafts()
                with self.assertRaisesRegex(ValueError, "已有不同内容"):
                    self.commit()
                self.assertEqual(intake.snapshot(self.root), before)

    def test_content_edit_since_input_rejects_stale_conclusion(self):
        catalog = self.catalog_now()
        catalog["records"][0]["summary"] = "用户后来修改的事实。"
        intake.write(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "输入依据已变化"):
            self.commit()
        self.assertNotIn("weekly_reviews", self.catalog_now())

    def test_new_daily_status_without_content_changes_does_not_invalidate_input(self):
        catalog = self.catalog_now()
        catalog["last_intake"] = {"status": "complete", "summary": "又完成一次检查"}
        intake.write(self.root / "data/catalog.json", catalog)
        build.build(self.root)
        self.assertEqual(self.commit()["status"], "committed")
        self.assertEqual(self.catalog_now()["last_intake"], catalog["last_intake"])

    def test_candidate_schema_rejects_unknown_duplicate_too_many_or_invalid_selection(self):
        scenarios = [({"candidate_record_ids": ["intel-20261018-999"]}, "候选"),
                     ({"candidate_record_ids": [self.record["id"]] * 2}, "重复"),
                     ({"candidate_record_ids": [self.record["id"]] * 4}, "最多三条"),
                     ({"selected_record_id": "intel-20261018-999"}, "选中"),
                     ({"id": "review-chosen-by-model"}, "未允许字段")]
        original = deepcopy(self.packet)
        for values, message in scenarios:
            with self.subTest(values=values):
                self.packet = {**original, **values}
                self.save_drafts()
                with self.assertRaisesRegex(ValueError, message):
                    self.commit()
                self.assert_original_state()

    def test_zero_candidates_with_no_selected_action_is_valid(self):
        self.packet.update(candidate_record_ids=[], selected_record_id=None, next_action="本周没有足够证据，继续观察。")
        self.save_drafts()
        self.assertEqual(self.commit()["status"], "committed")

    def test_future_coverage_and_early_formal_review_are_rejected(self):
        self.packet["coverage_end"] = "2026-10-26T09:00:00+08:00"
        self.save_drafts()
        with self.assertRaisesRegex(ValueError, "当前北京时间"):
            self.commit()
        self.packet["coverage_end"] = self.input_args["coverage_end"]
        self.save_drafts()
        with patch.object(intake, "now", return_value=datetime.fromisoformat("2026-10-25T09:30:00+08:00")):
            with self.assertRaisesRegex(ValueError, "计划时间之前"):
                self.commit()
        self.assert_original_state()

    def test_unapproved_schedule_and_naive_timestamps_are_rejected(self):
        self.packet["scheduled_for"] = "2026-10-24T10:00:00+08:00"
        self.save_drafts()
        with self.assertRaisesRegex(ValueError, "两次|只允许"):
            self.commit()
        self.packet["scheduled_for"] = SLOT
        self.packet["coverage_start"] = "2026-10-19T00:00:00"
        self.save_drafts()
        with self.assertRaisesRegex(ValueError, "时区"):
            self.commit()

    def test_outside_or_traversing_draft_paths_are_rejected(self):
        for name in ("packet.json", "data/packet.json", ".intake/weekly-review/../packet.json", str(self.root / self.packet_name)):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    weekly.commit(self.root, packet=name, markdown=self.markdown_name)
        self.assert_original_state()

    def test_corrupt_original_cannot_be_hidden_by_review(self):
        original = self.root / self.record["original_file"]
        original.write_bytes(original.read_bytes() + b"untracked")
        with self.assertRaisesRegex(ValueError, "原文校验不通过"):
            self.commit()
        self.assertNotIn("weekly_reviews", self.catalog_now())

    def test_recovery_finishes_after_catalog_without_duplicate_or_ledger_changes(self):
        original_write = build.atomic_write
        fail_at = self.root / build.OUTPUT
        failed = False
        def stop(path, content):
            nonlocal failed
            if Path(path) == fail_at and not failed:
                failed = True
                raise OSError("simulated power loss after catalog")
            return original_write(path, content)
        with patch.object(build, "atomic_write", side_effect=stop):
            with self.assertRaisesRegex(OSError, "power loss"):
                self.commit()
        self.assertEqual(len(self.catalog_now()["weekly_reviews"]), 1)
        # The daily recovery entry point discovers the review transaction too.
        intake.recover_transactions(self.root)
        self.assertEqual(self.commit()["status"], "already_committed")
        self.assertEqual(len(build.load_library(self.root)["weekly_reviews"]), 1)
        self.assertEqual(intake.sha((self.root / "data/intake-state.json").read_bytes()), self.before["data/intake-state.json"])
        self.assertEqual(intake.read_json(self.journals()[0])["status"], "committed")

    def assert_interrupted_commit_recovers(self, target_name):
        original_write = build.atomic_write
        def stop(path, content):
            target = Path(path)
            is_result = target.is_relative_to(self.root / "evidence/weekly-review") and target.name == "result.json"
            if (target_name == "result" and is_result) or target == self.root / target_name:
                raise OSError("simulated commit interruption")
            return original_write(path, content)
        with patch.object(build, "atomic_write", side_effect=stop):
            with self.assertRaisesRegex(OSError, "commit interruption"):
                self.commit()
        self.assertEqual(self.commit()["status"], "already_committed")
        journal = intake.read_json(self.journals()[0])
        self.assertEqual(journal["status"], "committed")
        self.assertTrue((self.root / journal["result_file"]).is_file())
        self.assertEqual(len(build.load_library(self.root)["weekly_reviews"]), 1)
        self.assertEqual(intake.sha((self.root / "data/intake-state.json").read_bytes()), self.before["data/intake-state.json"])

    def test_recovery_after_original_before_catalog(self):
        self.assert_interrupted_commit_recovers("data/catalog.json")

    def test_recovery_after_homepage_before_manifest(self):
        self.assert_interrupted_commit_recovers("evidence/build-manifest.json")

    def test_duplicate_check_recovers_missing_result_first(self):
        self.assert_interrupted_commit_recovers("result")

    def test_recovery_refuses_a_user_edit_after_interruption(self):
        original_write = build.atomic_write
        def stop(path, content):
            if Path(path) == self.root / build.OUTPUT:
                raise OSError("interrupt")
            return original_write(path, content)
        with patch.object(build, "atomic_write", side_effect=stop):
            with self.assertRaises(OSError):
                self.commit()
        catalog = self.catalog_now()
        catalog["user_note"] = "用户修改必须保留"
        intake.write(self.root / "data/catalog.json", catalog)
        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            self.commit()
        self.assertEqual(self.catalog_now()["user_note"], "用户修改必须保留")

    def test_draft_drift_during_build_prevents_any_formal_write(self):
        original_build = build.build
        def change(root, **kwargs):
            result = original_build(root, **kwargs)
            (self.root / self.markdown_name).write_bytes(self.markdown + b"changed")
            return result
        with patch.object(build, "build", side_effect=change):
            with self.assertRaisesRegex(ValueError, "草稿在准备期间变化"):
                self.commit()
        self.assert_original_state()

    def test_review_archive_hash_path_and_reference_validation(self):
        self.commit()
        saved = self.catalog_now()
        for change, message in (({"original_file": "../outside.md"}, "data/reviews"),
                                ({"original_sha256": "0" * 64}, "原文校验"),
                                ({"selected_record_id": "intel-20261018-999"}, "选中")):
            with self.subTest(change=change):
                catalog = deepcopy(saved)
                catalog["weekly_reviews"][0].update(change)
                intake.write(self.root / "data/catalog.json", catalog)
                with self.assertRaisesRegex(ValueError, message):
                    build.load_library(self.root)

    def test_existing_reviews_are_included_in_future_input_and_fingerprint(self):
        self.commit()
        args = {"scheduled_for": "2026-11-01T10:00:00+08:00", "coverage_start": "2026-10-25T10:00:00+08:00",
                "coverage_end": "2026-11-01T09:00:00+08:00"}
        with patch.object(intake, "now", return_value=datetime.fromisoformat("2026-11-01T10:05:00+08:00")):
            weekly.prepare_input(self.root, output=".intake/weekly-review/second/input.json", **args)
            first = intake.read_json(self.root / ".intake/weekly-review/second/input.json")
            self.assertEqual(first["weekly_reviews"][0]["original"].encode(), self.markdown)
            catalog = self.catalog_now()
            catalog["weekly_reviews"][0]["summary"] = "复盘内容后来得到纠正。"
            amended = {key: catalog["weekly_reviews"][0][key] for key in weekly.PACKET_FIELDS if key != "schema_version"}
            amended["schema_version"] = 1
            catalog["weekly_reviews"][0]["packet_sha256"] = weekly.digest(weekly.canonical(amended))
            intake.write(self.root / "data/catalog.json", catalog)
            second = weekly.evidence_context(self.root, **args)
            self.assertNotEqual(first["input_sha256"], second["input_sha256"])

    def test_next_week_can_keep_identical_full_text_as_a_separate_review(self):
        self.commit()
        args = {"scheduled_for": "2026-11-01T10:00:00+08:00", "coverage_start": "2026-10-25T10:00:00+08:00",
                "coverage_end": "2026-11-01T09:00:00+08:00"}
        with patch.object(intake, "now", return_value=datetime.fromisoformat("2026-11-01T10:05:00+08:00")):
            weekly.prepare_input(self.root, output=".intake/weekly-review/second/input.json", **args)
            context = intake.read_json(self.root / ".intake/weekly-review/second/input.json")
            self.packet.update(**args, input_sha256=context["input_sha256"])
            self.save_drafts()
            result = self.commit()
        self.assertEqual(result["review_id"], "review-20261101")
        reviews = build.load_library(self.root)["weekly_reviews"]
        self.assertEqual(len(reviews), 2)
        self.assertEqual(reviews[0]["original"], reviews[1]["original"])
        self.assertEqual(intake.sha((self.root / "data/intake-state.json").read_bytes()), self.before["data/intake-state.json"])

    def test_weekly_recovery_can_skip_intake_notification_acknowledgement(self):
        self.commit()
        with patch.object(intake_state, "pending_notifications", side_effect=AssertionError("must not acknowledge daily notices")):
            self.assertEqual(self.commit()["status"], "already_committed")

    def test_following_daily_intake_stages_and_preserves_full_weekly_review(self):
        self.commit()
        saved_review = deepcopy(self.catalog_now()["weekly_reviews"])
        self.assertIn(saved_review[0]["original_file"], intake.snapshot(self.root))
        batch = {"captures": [], "run_dir": "evidence/fixture", "run_sha256": "a" * 64,
                 "complete_categories": list(SOURCES), "failed_categories": {},
                 "scan_start": "2026-10-19T00:00:00+08:00", "scan_end": "2026-10-25T10:01:00+08:00"}
        with patch("background_capture.legacy_proofs", return_value={}), \
             patch("background_capture.load_capture", return_value=batch):
            result = intake.prepare_transaction(self.root, "bg-after-weekly", batch, {})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.catalog_now()["weekly_reviews"], saved_review)
        self.assertEqual(build.load_library(self.root)["weekly_reviews"][0]["original"].encode(), self.markdown)

    def test_trashed_candidates_and_management_fingerprint_drift_are_rejected(self):
        import management_state
        context = intake.read_json(self.root / self.input_name)
        changed = deepcopy(context["management"])
        changed["states"][self.record["id"]] = {"state": "trashed"}
        changed["fingerprint"] = "f" * 64
        with patch.object(management_state, "load_management", return_value=changed):
            with self.assertRaisesRegex(ValueError, "回收站"):
                self.commit()
            new_input = weekly.evidence_context(self.root, **self.input_args)
            self.assertEqual(new_input["records"], [])
        changed["states"][self.record["id"]] = {"state": "archived"}
        with patch.object(management_state, "load_management", return_value=changed):
            with self.assertRaisesRegex(ValueError, "输入依据已变化"):
                self.commit()
        self.assert_original_state()

    def test_management_drift_at_final_observation_prevents_commit(self):
        original_context = weekly.evidence_context
        calls = 0
        def change(*args, **kwargs):
            nonlocal calls
            result = original_context(*args, **kwargs)
            calls += 1
            if calls == 2:
                result["input_sha256"] = "f" * 64
            return result
        with patch.object(weekly, "evidence_context", side_effect=change):
            with self.assertRaisesRegex(ValueError, "依据在准备期间变化"):
                self.commit()
        self.assert_original_state()

    def test_existing_schedule_lock_blocks_weekly_without_formal_writes(self):
        with weekly.schedule_lock(self.root):
            with self.assertRaises(weekly.RunnerBusy):
                self.commit()
        self.assert_original_state()


if __name__ == "__main__":
    unittest.main(verbosity=2)
