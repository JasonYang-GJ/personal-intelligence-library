"""Pure migration plans; tests never write production data or mutate inputs."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import intake_alias as aliases
from intake_state import digest, normalize


class IntakeAliasTests(unittest.TestCase):
    def setUp(self):
        self.source = {"category_id": "ai-models", "source_task": aliases.SOURCES["ai-models"][0],
                       "conversation_id": aliases.SOURCES["ai-models"][1]}
        body = "# 报告\r\n金额 12 元\r\n"
        self.capture = {**self.source, "turn_key": "turn-one", "generated_at": "2026-10-15T16:03:10.256Z",
                        "original": body, "capture_file": "evidence/report.md"}
        self.entry = {"category_id": "ai-models", "generated_at": "2026-10-16T00:03:00+08:00",
                      "normalized_sha256": digest(normalize(body).encode()), "raw_sha256": digest(body.encode()),
                      "capture_file": "data/originals/intel.md", "record_id": "intel-one",
                      "outcome": "imported", "run_id": "old-run"}
        self.state = {"schema_version": 1, "normalization_version": 1,
                      "tasks": {"ai-models": {"checked_through": "2026-10-18T10:58:47+08:00"}},
                      "occurrences": {"old-one": self.entry}}
        self.proof = {**self.source, "generated_at": self.entry["generated_at"], "precision": "minute",
                      "ui_timestamp": "星期五 0:03", "evidence_file": ".intake/old.json", "evidence_sha256": "a" * 64}
        self.proofs = {"old-one": self.proof}

    def plan(self, captures=None):
        return aliases.plan_aliases(self.state, captures if captures is not None else [self.capture], self.proofs)

    def assert_blocked(self, result, code):
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["aliases"], {})
        self.assertEqual(result["known"], [])
        self.assertIn(code, [e["code"] for e in result["errors"]])

    def test_unique_minute_alias_retains_exact_time_original_identity_and_inputs(self):
        before = deepcopy((self.state, self.capture, self.proofs))
        result = self.plan()
        self.assertEqual(result["status"], "ready")
        alias = next(iter(result["aliases"].values()))
        self.assertEqual(alias["generated_at"], "2026-10-15T16:03:10.256Z")
        self.assertEqual(alias["generated_at_beijing_exact"], "2026-10-16T00:03:10.256000+08:00")
        self.assertEqual(alias["turn_key"], "turn-one")
        self.assertEqual(result["known"][0]["occurrence_key"], "old-one")
        self.assertEqual(result["known"][0]["target_entry"], self.entry)
        self.assertEqual((self.state, self.capture, self.proofs), before)
        alias["precision_evidence"]["precision"] = "edited-output"
        result["known"][0]["target_entry"]["outcome"] = "edited-output"
        self.assertEqual((self.state, self.capture, self.proofs), before)

    def test_same_minute_two_real_turns_do_not_merge(self):
        second = {**self.capture, "turn_key": "turn-two", "generated_at": "2026-10-15T16:03:40.000Z"}
        self.assert_blocked(self.plan([self.capture, second]), "ambiguous_source_turns")

    def test_multiple_legacy_candidates_block_even_if_one_has_no_precision_proof(self):
        self.state["occurrences"]["old-two"] = {**self.entry, "generated_at": "2026-10-16T00:03:11+08:00"}
        self.assert_blocked(self.plan(), "ambiguous_legacy_targets")

    def test_different_body_number_is_unmatched(self):
        self.capture["original"] = self.capture["original"].replace("12", "13")
        result = self.plan()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["aliases"], {})
        self.assertEqual(len(result["unmatched"]), 1)

    def test_existing_millisecond_precision_cannot_be_claimed_as_minute(self):
        self.entry["generated_at"] = "2026-10-16T00:03:00.000+08:00"
        self.proof["generated_at"] = self.entry["generated_at"]
        self.assert_blocked(self.plan(), "legacy_time_not_proven_minute_precision")

    def test_explicit_seconds_precision_cannot_be_claimed_as_minute(self):
        self.entry["generated_at_precision"] = "second"
        self.assert_blocked(self.plan(), "legacy_time_not_proven_minute_precision")

    def test_zero_seconds_without_proof_do_not_imply_minute_precision(self):
        self.proofs = {}
        result = self.plan()
        self.assertEqual(result["aliases"], {})
        self.assertEqual(len(result["unmatched"]), 1)

    def test_wrong_source_or_category_is_rejected(self):
        for changes in ({"conversation_id": aliases.SOURCES["ai-products"][1]}, {"category_id": "ai-products"},
                        {"source_task": "不同任务"}):
            with self.subTest(changes=changes):
                self.assert_blocked(self.plan([{**self.capture, **changes}]), "source_not_allowlisted_for_category")

    def test_other_valid_source_and_category_is_not_aliased(self):
        self.capture.update(category_id="ai-products", source_task=aliases.SOURCES["ai-products"][0],
                            conversation_id=aliases.SOURCES["ai-products"][1])
        result = self.plan()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["aliases"], {})
        self.assertEqual(len(result["unmatched"]), 1)

    def test_duplicate_reminder_acknowledgement_is_inherited_and_not_reopened(self):
        self.entry.update(outcome="duplicate", reported_in_run_id="reported-before",
                          notification_status="recorded_in_result", notification_result={"file": "evidence/old.json", "sha256": "a" * 64})
        result = self.plan()
        self.assertFalse(result["known"][0]["notification_pending"])
        self.assertEqual(result["known"][0]["target_entry"], self.entry)
        self.state["source_aliases"] = result["aliases"]
        again = self.plan()
        self.assertEqual(again["aliases"], {})
        self.assertFalse(again["known"][0]["notification_pending"])

    def test_pending_reminder_is_preserved_then_reads_later_ack(self):
        self.entry.update(outcome="duplicate", notification_status="pending")
        result = self.plan()
        self.assertTrue(result["known"][0]["notification_pending"])
        self.state["source_aliases"] = result["aliases"]
        self.entry["reported_in_run_id"] = "later-ack"
        self.assertFalse(self.plan()["known"][0]["notification_pending"])

    def test_identical_repeat_input_is_idempotent(self):
        result = self.plan([self.capture, deepcopy(self.capture)])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["aliases"]), 1)
        self.assertEqual(len(result["known"]), 1)

    def test_repeat_identity_conflicting_text_or_timestamp_blocks_whole_batch(self):
        for changes in ({"original": "Changed 12"}, {"generated_at": "2026-10-15T16:03:11.256Z"}):
            with self.subTest(changes=changes):
                self.assert_blocked(self.plan([self.capture, {**self.capture, **changes}]), "conflicting_repeated_source_turn")

    def test_new_turn_in_previously_aliased_minute_is_ambiguous(self):
        self.state["source_aliases"] = self.plan()["aliases"]
        self.capture["turn_key"] = "later-distinct-turn"
        self.assert_blocked(self.plan(), "ambiguous_source_turns")

    def test_aliased_turn_change_never_rebinds(self):
        self.state["source_aliases"] = self.plan()["aliases"]
        self.capture["original"] = "Different report"
        self.assert_blocked(self.plan(), "source_turn_changed_after_alias")

    def test_same_content_next_day_is_a_new_occurrence(self):
        self.capture.update(turn_key="next-day", generated_at="2026-10-16T16:03:10.256Z")
        result = self.plan()
        self.assertEqual(result["aliases"], {})
        self.assertEqual(len(result["unmatched"]), 1)

    def test_ui_seconds_and_mismatched_minute_are_not_positive_proof(self):
        for text in ("昨天 0:03:00", "昨天 0:04", "未知"):
            with self.subTest(text=text):
                self.proof["ui_timestamp"] = text
                self.assert_blocked(self.plan(), "minute_only_ui_time_required")

    def test_hash_mismatch_is_rejected(self):
        self.capture["normalized_sha256"] = "0" * 64
        self.assert_blocked(self.plan(), "normalized_sha256_mismatch")

    def test_existing_normalizer_used_without_looser_content_matching(self):
        self.entry["normalized_sha256"] = digest(normalize("金额 12 元\nhttps://example.com/p?a=1&utm_source=old").encode())
        self.capture["original"] = "# 金额 12 元\r\nhttps://example.com/p?a=1&utm_source=new\r\n"
        self.assertEqual(len(self.plan()["aliases"]), 1)
        self.capture["original"] = self.capture["original"].replace("a=1", "a=2")
        self.assertEqual(len(self.plan()["unmatched"]), 1)

    def test_equivalent_timestamp_notation_keeps_first_observation(self):
        same = {**self.capture, "generated_at": "2026-10-16T00:03:10.256000+08:00"}
        result = self.plan([self.capture, same])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(next(iter(result["aliases"].values()))["generated_at"], self.capture["generated_at"])

    def test_two_eligible_legacy_entries_do_not_pick_first(self):
        self.state["occurrences"]["old-two"] = deepcopy(self.entry)
        self.proofs["old-two"] = deepcopy(self.proof)
        self.assert_blocked(self.plan(), "ambiguous_legacy_targets")

    def test_corrupt_proof_suppresses_every_planned_alias(self):
        self.proofs["missing-entry"] = None
        result = self.plan()
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["errors"])
        self.assertEqual(result["aliases"], {})

    def test_stored_alias_wrong_target_does_not_resolve(self):
        self.state["source_aliases"] = self.plan()["aliases"]
        next(iter(self.state["source_aliases"].values()))["target_occurrence_key"] = "missing"
        result = self.plan()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["known"], [])
        self.assertEqual(result["aliases"], {})

    def test_path_and_precision_evidence_are_required(self):
        for field, value, code in (("evidence_file", "../elsewhere.json", "evidence_path_must_be_project_relative"),
                                   ("evidence_sha256", "not-a-hash", "invalid_sha256"),
                                   ("precision", "unknown", "legacy_time_not_proven_minute_precision")):
            with self.subTest(field=field):
                old = self.proof[field]
                self.proof[field] = value
                self.assert_blocked(self.plan(), code)
                self.proof[field] = old


if __name__ == "__main__":
    unittest.main()
