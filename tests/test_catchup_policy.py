"""Wall-clock and interrupted-run boundaries; no files, clocks or tasks changed."""
from copy import deepcopy
from datetime import datetime
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("catchup_policy", Path(__file__).resolve().parents[1] / "scripts/catchup_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def clock(value):
    return datetime.fromisoformat(value)


def checkpoints(value):
    return {category: value for category in policy.CATEGORIES}


class CatchupPolicyTests(unittest.TestCase):
    def decide(self, now="2026-10-18T10:00:00+08:00", points=None, runtime=None, **kwargs):
        options = {"app_ready": True, "native_busy": False, **kwargs}
        return policy.decide(clock(now), points if points is not None else checkpoints("2026-10-17T09:00:00+08:00"),
                             runtime if runtime is not None else {"phase": "idle"}, **options)

    def test_before_nine_targets_yesterday_and_catches_missed_day(self):
        result = self.decide(now="2026-10-18T08:59:59+08:00", points=checkpoints("2026-10-16T10:00:00+08:00"))
        self.assertEqual(result["action"], "trigger")
        self.assertEqual(result["due_at"], "2026-10-17T09:00:00+08:00")
        self.assertEqual(len(result["missing_categories"]), 6)
        self.assertEqual(self.decide(now="2026-10-18T08:59:59+08:00")["action"], "covered")

    def test_midnight_after_several_days_off_uses_latest_due(self):
        result = self.decide(now="2026-10-22T00:00:00+08:00")
        self.assertEqual(result["action"], "trigger")
        self.assertEqual(result["due_at"], "2026-10-21T09:00:00+08:00")

    def test_utc_clock_is_converted_to_beijing(self):
        result = self.decide(now="2026-10-18T01:06:00+00:00")
        self.assertEqual(result["action"], "trigger")
        self.assertEqual(result["due_at"], "2026-10-18T09:00:00+08:00")

    def test_daily_task_gets_five_full_minutes(self):
        for value in ("2026-10-18T09:00:00+08:00", "2026-10-18T09:04:59.999999+08:00"):
            with self.subTest(now=value):
                result = self.decide(now=value)
                self.assertEqual((result["action"], result["reason"]), ("wait", "native_grace_period"))
                self.assertEqual(result["retry_at"], "2026-10-18T09:05:00+08:00")
        self.assertEqual(self.decide(now="2026-10-18T09:05:00+08:00")["action"], "trigger")

    def test_partial_failure_reports_only_missing_categories(self):
        points = {c: {"checked_through": "2026-10-18T09:20:00+08:00"} for c in policy.CATEGORIES}
        points["cross-border"]["checked_through"] = "2026-10-16T00:00:00+08:00"
        points["user-pains"]["checked_through"] = "2026-10-18T08:59:59+08:00"
        result = self.decide(points=points)
        self.assertEqual(result["action"], "trigger")
        self.assertEqual(result["missing_categories"], ["user-pains", "cross-border"])

    def test_actual_coverage_stops_even_when_app_is_closed(self):
        result = self.decide(points=checkpoints("2026-10-18T09:00:00+08:00"), app_ready=False, native_busy=True,
                             runtime={"phase": "running"})
        self.assertEqual(result["action"], "covered")
        self.assertEqual(result["missing_categories"], [])

    def test_ready_and_native_busy_gates(self):
        self.assertEqual(self.decide(app_ready=False)["reason"], "app_not_ready")
        self.assertEqual(self.decide(native_busy=True)["reason"], "native_task_busy")

    def test_uncertain_or_confirmed_launch_never_retriggers_by_age(self):
        for phase in ("starting", "running"):
            with self.subTest(phase=phase):
                result = self.decide(now="2026-10-25T10:00:00+08:00", runtime={"phase": phase, "started_at": "2026-10-18T10:00:00+08:00"})
                self.assertEqual(result["action"], "wait")

    def test_failure_cooldown_is_fifteen_minutes_not_trigger_time(self):
        runtime = {"phase": "failed", "started_at": "2026-10-18T08:00:00+08:00", "failed_at": "2026-10-18T09:50:00+08:00"}
        result = self.decide(now="2026-10-18T10:04:59+08:00", runtime=runtime)
        self.assertEqual((result["action"], result["reason"]), ("wait", "failure_cooldown"))
        self.assertEqual(result["retry_at"], "2026-10-18T10:05:00+08:00")
        self.assertEqual(self.decide(now="2026-10-18T10:05:00+08:00", runtime=runtime)["action"], "trigger")
        runtime["phase"] = "idle"
        self.assertEqual(self.decide(runtime=runtime)["reason"], "failure_cooldown")

    def test_verification_uses_same_gates_and_stops_on_actual_coverage(self):
        target = "2026-10-18T09:02:00+08:00"
        self.assertEqual(self.decide(now=target, verification_target=target)["action"], "trigger")
        self.assertEqual(self.decide(now=target, verification_target=target, native_busy=True)["action"], "wait")
        self.assertEqual(self.decide(now=target, verification_target=target, app_ready=False)["action"], "wait")
        self.assertEqual(self.decide(now=target, verification_target=target, runtime={"phase": "starting"})["action"], "wait")
        runtime = {"phase": "failed", "failed_at": "2026-10-18T09:01:00+08:00"}
        self.assertEqual(self.decide(now=target, verification_target=target, runtime=runtime)["reason"], "failure_cooldown")
        points = checkpoints(target)
        self.assertEqual(self.decide(now=target, points=points, verification_target=target)["action"], "covered")

    def test_partial_verification_is_not_prematurely_complete(self):
        target = "2026-10-18T08:30:00+08:00"
        points = checkpoints(target)
        points["ai-models"] = "2026-10-18T08:29:59+08:00"
        result = self.decide(now="2026-10-18T08:35:00+08:00", points=points, verification_target=target)
        self.assertEqual((result["action"], result["missing_categories"]), ("trigger", ["ai-models"]))

    def test_invalid_or_future_times_never_trigger_or_claim_coverage(self):
        for target in ("未知", "2026-10-18T09:00:00", "2026-10-19T09:00:00+08:00"):
            with self.subTest(target=target):
                self.assertEqual(self.decide(verification_target=target)["action"], "wait")
        for value in (None, "未知", "2026-10-19T09:00:00+08:00"):
            points = checkpoints("2026-10-18T09:00:00+08:00")
            points["ai-products"] = value
            self.assertEqual(self.decide(points=points)["reason"], "invalid_checkpoints")
        self.assertEqual(self.decide(runtime={"phase": "failed"})["reason"], "invalid_failure_time")
        self.assertEqual(self.decide(runtime={"phase": "unknown"})["reason"], "invalid_runtime")
        with self.assertRaises(ValueError):
            policy.decide(datetime(2026, 10, 18, 10), {}, {}, app_ready=True, native_busy=False)

    def test_decision_is_pure_and_does_not_make_a_trigger_successful(self):
        points = checkpoints("2026-10-17T09:00:00+08:00")
        runtime = {"phase": "idle", "host_details": {"attempts": 3}}
        before = deepcopy((points, runtime))
        first = self.decide(points=points, runtime=runtime)
        self.assertEqual(first["action"], "trigger")
        self.assertEqual(self.decide(points=points, runtime=runtime), first)
        self.assertEqual((points, runtime), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
