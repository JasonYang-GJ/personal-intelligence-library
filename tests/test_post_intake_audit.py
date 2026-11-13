from pathlib import Path
from datetime import datetime
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import post_intake_audit as module


class AuditScheduling(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {"schema_version": 1, "id": module.REQUEST_ID,
                       "target": "2026-10-19T09:00:00+08:00", "not_before": "2026-10-19T09:05:00+08:00",
                       "problem_after": "2026-10-19T09:45:00+08:00"}
        module.write(self.root / module.REQUEST_FILE, self.config)
        module.write(self.root / "data/catchup-config.json", {})
        self.activity = patch.object(module, "native_activity", return_value={"available": True, "busy": False})
        self.activity.start(); self.addCleanup(self.activity.stop)

    def at(self, time):
        return patch.object(module, "now", return_value=datetime.fromisoformat(time))

    def result(self, complete=True):
        value={"run_id":"bg-real-001", "scan_start":"2026-10-17T09:00:00+08:00",
               "scan_end":"2026-10-19T09:01:00+08:00", "status":"complete" if complete else "partial"}
        module.write(self.root / "evidence/background-latest.json", value)
        module.write(self.root / "evidence/daily/bg-real-001/result.json", value)
        return "evidence/daily/bg-real-001/result.json"

    def test_before_due_has_no_lock_state_reader_or_notification(self):
        before = sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        with self.at("2026-10-18T22:00:00+08:00"), patch.object(module,"capture_again",side_effect=AssertionError), patch.object(module,"notify_problem",side_effect=AssertionError):
            self.assertEqual(module.run_once(self.root)["reason"],"not_due")
        self.assertEqual(before,sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()))

    def test_dry_run_after_due_does_not_start_work(self):
        with self.at("2026-10-19T09:10:00+08:00"), patch.object(module,"capture_again",side_effect=AssertionError):
            self.assertEqual(module.run_once(self.root,dry_run=True)["reason"],"dry_run_no_read_or_notification")
        self.assertFalse((self.root/module.STATE_FILE).exists())

    def test_old_result_waits_then_reports_missing_collection(self):
        self.result()
        current = module.read_json(self.root/"evidence/background-latest.json")
        current["scan_end"]="2026-10-18T09:00:00+08:00"
        module.write(self.root/"evidence/background-latest.json",current)
        with self.assertRaises(module.AuditWait):
            module.choose_result(self.root,self.config,datetime.fromisoformat("2026-10-19T09:10:00+08:00"))
        with self.assertRaisesRegex(ValueError,"尚无覆盖"):
            module.choose_result(self.root,self.config,datetime.fromisoformat("2026-10-19T09:46:00+08:00"))

    def test_running_collector_is_never_preempted(self):
        with self.at("2026-10-19T09:10:00+08:00"), patch.object(module,"schedule_lock",side_effect=module.RunnerBusy), patch.object(module,"notify_problem",side_effect=AssertionError):
            self.assertEqual(module.run_once(self.root)["status"],"waiting")
        self.assertFalse((self.root/module.RESULT_FILE).exists())

    def test_partial_result_is_a_problem_not_success(self):
        self.result(False)
        with self.assertRaisesRegex(ValueError,"未完成六类"):
            module.choose_result(self.root,self.config,datetime.fromisoformat("2026-10-19T09:10:00+08:00"))

    def test_collector_busy_after_deadline_notifies_without_preempting(self):
        module.write(self.root/module.STATE_FILE,{"request_id":module.REQUEST_ID,"status":"pending",
                     "first_due_check_at":"2026-10-19T09:05:00+08:00","boot_epoch":module.boot_epoch()})
        with self.at("2026-10-19T09:46:00+08:00"), patch.object(module,"schedule_lock",side_effect=module.RunnerBusy), patch.object(module,"capture_again",side_effect=AssertionError), patch.object(module,"notify_problem",return_value={"submitted":True}) as notify, patch.object(module,"disable_own_task",return_value=True):
            self.assertEqual(module.run_once(self.root)["status"],"issues")
        self.assertEqual(notify.call_count,1)
        self.assertEqual(module.read_json(self.root/module.RESULT_FILE)["issues"][0]["code"],"audit_blocked")

    def test_late_boot_allows_intake_to_finish_before_problem_deadline(self):
        with self.at("2026-10-19T11:00:00+08:00"), patch.object(module,"schedule_lock",side_effect=module.RunnerBusy), patch.object(module,"notify_problem",side_effect=AssertionError):
            self.assertEqual(module.run_once(self.root)["status"],"waiting")
        state=module.read_json(self.root/module.STATE_FILE)
        self.assertEqual(state["effective_problem_after"],"2026-10-19T11:40:00+08:00")

    def test_reboot_resets_only_waiting_grace_not_completed_audit(self):
        module.write(self.root/module.STATE_FILE,{"request_id":module.REQUEST_ID,"status":"pending",
                     "first_due_check_at":"2026-10-19T09:05:00+08:00","boot_epoch":module.boot_epoch()-3600})
        with self.at("2026-10-19T11:00:00+08:00"), patch.object(module,"schedule_lock",side_effect=module.RunnerBusy), patch.object(module,"notify_problem",side_effect=AssertionError):
            self.assertEqual(module.run_once(self.root)["status"],"waiting")
        self.assertEqual(module.read_json(self.root/module.STATE_FILE)["effective_problem_after"],"2026-10-19T11:40:00+08:00")

    def test_native_task_hung_after_deadline_is_reported(self):
        with patch.object(module,"native_activity",return_value={"available":True,"busy":True}):
            with self.assertRaisesRegex(ValueError,"仍在运行"):
                module.choose_result(self.root,self.config,datetime.fromisoformat("2026-10-19T09:46:00+08:00"))

    def test_full_pass_independent_reread_and_no_notification(self):
        relative=self.result()
        with self.at("2026-10-19T09:10:00+08:00"), patch("intake_audit_checks.validate_intake",return_value={"status":"pass","issues":[]}) as validate, patch.object(module,"capture_again",return_value="evidence/extension-probe/recheck") as capture, patch.object(module,"notify_problem",side_effect=AssertionError), patch.object(module,"disable_own_task",return_value=True):
            result=module.run_once(self.root)
        self.assertEqual(result["status"],"pass")
        self.assertEqual(validate.call_count,2)
        self.assertEqual(capture.call_count,1)
        self.assertEqual(validate.call_args.kwargs["recheck_run_dir"],"evidence/extension-probe/recheck")
        with patch.object(module,"notify_problem",side_effect=AssertionError), patch.object(module,"disable_own_task",side_effect=AssertionError):
            module.finish_notification(self.root,result)

    def test_check_failure_saves_before_notification_and_never_rereads(self):
        self.result()
        def notify(root):
            saved=module.read_json(root/module.RESULT_FILE)
            self.assertEqual(saved["status"],"issues")
            return {"submitted":True,"history_match":True}
        with self.at("2026-10-19T09:10:00+08:00"), patch("intake_audit_checks.validate_intake",return_value={"status":"issues","issues":[{"code":"hash","message":"damaged"}]}), patch.object(module,"capture_again",side_effect=AssertionError), patch.object(module,"notify_problem",side_effect=notify) as notify_call, patch.object(module,"disable_own_task",return_value=True):
            self.assertEqual(module.run_once(self.root)["status"],"issues")
        self.assertEqual(notify_call.call_count,1)

    def test_failed_notification_waits_fifteen_minutes_and_retries_same_result(self):
        state={"request_id":module.REQUEST_ID,"status":"issues"}
        with self.at("2026-10-19T09:10:00+08:00"), patch.object(module,"notify_problem",return_value={"submitted":False}) as notify, patch.object(module,"disable_own_task",side_effect=AssertionError):
            module.finish_notification(self.root,state)
            module.finish_notification(self.root,state)
            self.assertEqual(notify.call_count,1)
        with self.at("2026-10-19T09:26:00+08:00"), patch.object(module,"notify_problem",return_value={"submitted":True}) as notify, patch.object(module,"disable_own_task",return_value=True):
            module.finish_notification(self.root,state)
            self.assertEqual(notify.call_count,1)

    def test_completed_pinned_result_survives_interrupted_audit(self):
        relative=self.result()
        module.write(self.root/module.STATE_FILE,{"request_id":module.REQUEST_ID,"status":"checking","source_result":relative})
        self.assertEqual(module.choose_result(self.root,self.config,datetime.fromisoformat("2026-10-19T10:00:00+08:00")),relative)

    def plan(self):
        value={"extension_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","enabled":True,
               "request_id":"bg-existing","mode":"probe","scan_start":"2026-10-17T09:00:00+08:00","scan_end":None,
               "sources":[{"category_id":c,"task_name":task,"conversation_id":cid} for c,(task,cid) in module.SOURCES.items()]}
        module.write(self.root/".intake/extension-probe/config.json",value)
        return value

    def test_unacknowledged_existing_reader_plan_is_never_overwritten(self):
        before=self.plan()
        with self.at("2026-10-19T09:10:00+08:00"), self.assertRaises(module.AuditWait):
            module.capture_again(self.root,{"run_id":"bg-source"})
        self.assertEqual(module.read_json(self.root/".intake/extension-probe/config.json"),before)

    def test_same_window_plan_is_not_resumable_as_production_intake(self):
        from background_intake import request_capture
        self.plan()
        old="extension-probe-"+module.sha(b"bg-existing")[:24]
        module.write(self.root/f"evidence/extension-probe/{old}/run.json",{"status":"complete"})
        result={"run_id":"bg-source","scan_start":"2026-10-17T09:00:00+08:00","scan_end":"2026-10-19T09:01:00+08:00"}
        real_write=module.write
        def accept(path,value):
            real_write(path,value)
            run="extension-probe-"+module.sha(value['request_id'].encode())[:24]
            real_write(self.root/f"evidence/extension-probe/{run}/run.json",{"status":"complete"})
        with patch("extension_native_host.locked",return_value=nullcontext()), patch.object(module,"write",side_effect=accept):
            run_dir=module.capture_again(self.root,result)
        plan=module.read_json(self.root/".intake/extension-probe/config.json")
        self.assertTrue(plan["request_id"].startswith("audit-recheck-"))
        self.assertEqual(plan["scan_start"],result["scan_start"])
        self.assertEqual(plan["scan_end"],result["scan_end"])
        real_write(self.root/run_dir/"run.json",{"status":"pending"})
        with self.assertRaisesRegex(ValueError,"Another browser verification"):
            request_capture(self.root,"bg-production-next")


if __name__=="__main__":
    unittest.main()
