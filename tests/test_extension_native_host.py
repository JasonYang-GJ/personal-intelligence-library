"""Protocol and evidence safety checks; these do not prove browser extraction."""
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import extension_native_host as native


EXTENSION = "a" * 32
ORIGIN = f"chrome-extension://{EXTENSION}/"


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"extension_id": EXTENSION, "enabled": True, "mode": "probe",
                       "request_id": "unit-check-001", "scan_start": "2026-10-16T00:00:00+08:00",
                       "scan_end": "2026-10-18T18:00:00+08:00",
                       "sources": [{"category_id": key, "task_name": title,
                                    "conversation_id": "conversation-" + key}
                                   for key, title in native.SOURCES.items()]}
        self.write_config()
        (self.root / "data").mkdir()
        (self.root / "data/catalog.json").write_bytes(b"production sentinel")
        self.host = native.Host(self.root, [ORIGIN, "--parent-window=0"])
        self.plan = self.host.dispatch({"op": "get_plan"})
        self.run_id = self.plan["run_id"]

    def write_config(self):
        target = self.root / ".intake/extension-probe/config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.config), encoding="utf-8")

    def report(self, category="ai-models", **changes):
        value = {"title": native.SOURCES[category], "path": "/c/conversation-" + category,
                 "turn_key": "turn-1", "report_time": "2026-10-17T02:33:00Z",
                 "text": "# 完整报告\n\n|甲|乙|\n|--|--|\n|一|二|\n\n[来源](https://example.com/a)\n",
                 "rendered_text": "完整报告 甲 乙 一 二 来源", "message_id": "message-1",
                 "links": [{"href": "https://example.com/a", "text": "来源"}],
                 "table_count": 1, "guard": {"copy_intercepted": 1, "real_clipboard_calls": 0},
                 "history_dates": ["2026-10-17T02:33:00Z"], "is_report": True}
        value.update(changes)
        return {"op": "save_report", "run_id": self.run_id, "category_id": category, "report": value}

    def finish(self, category, complete=True, **changes):
        coverage = {"complete": complete, "reason": "boundary_before_start" if complete else "history_pending",
                    "boundary_time": "2026-10-15T23:00:00+08:00", "page_count": 2,
                    "dates": ["2026-10-15T23:00:00+08:00", "2026-10-17T10:33:00+08:00"]}
        coverage.update(changes)
        return self.host.dispatch({"op": "finish_category", "run_id": self.run_id,
                                   "category_id": category, "coverage": coverage})

    def assertError(self, code, action):
        with self.assertRaises(native.HostError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_origin_is_exact_and_required(self):
        for argv in ([], ["chrome-extension://" + "b" * 32 + "/"],
                     [ORIGIN.rstrip("/")], [ORIGIN + "evil"], [ORIGIN, ORIGIN]):
            self.assertError("origin_denied", lambda: native.Host(self.root, argv))

    def test_config_requires_six_exact_sources_and_enabled_probe(self):
        cases = [({"enabled": 1}, "config_invalid"), ({"mode": "production"}, "config_invalid"),
                 ({"sources": self.config["sources"][:-1]}, "config_invalid"),
                 ({"request_id": "../../outside"}, "config_invalid")]
        original = self.config.copy()
        for patch, code in cases:
            self.config = {**original, **patch}
            self.write_config()
            self.assertError(code, lambda: native.Host(self.root, [ORIGIN]))

    def test_disabled_plan_creates_no_run_and_can_record_startup_event(self):
        self.config.update(enabled=False, request_id="disabled-install")
        self.write_config()
        host = native.Host(self.root, [ORIGIN])
        self.assertEqual(host.dispatch({"op": "get_plan", "event": "startup"}),
                         {"ok": True, "status": "disabled"})
        self.assertFalse(host.state_path.exists())
        event = json.loads((self.root / "evidence/extension-probe/connection-events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "startup")
        self.assertFalse(event["enabled"])
        self.assertError("invalid_event", lambda: host.dispatch({"op": "get_plan", "event": "execute"}))
        self.assertError("disabled", lambda: host.dispatch({"op": "save_report"}))

    def test_same_request_plan_is_stable_across_processes(self):
        other = native.Host(self.root, [ORIGIN]).dispatch({"op": "get_plan"})
        self.assertEqual(other, self.plan)
        self.config["scan_end"] = "2026-10-18T19:00:00+08:00"
        self.write_config()
        self.assertError("config_changed", lambda: native.Host(self.root, [ORIGIN]).dispatch({"op": "get_plan"}))

    def test_visible_history_diagnostic_requires_explicit_exact_configuration(self):
        self.assertIsNone(self.plan["diagnostic"])
        self.config.update(request_id="visible-approved", diagnostic=dict(native.VISIBLE_HISTORY_DIAGNOSTIC))
        self.write_config()
        host = native.Host(self.root, [ORIGIN])
        plan = host.dispatch({"op": "get_plan"})
        self.assertEqual(plan["diagnostic"], native.VISIBLE_HISTORY_DIAGNOSTIC)
        self.assertEqual(plan["sources"], self.config["sources"])
        self.assertEqual(len(plan["sources"]), 6)
        result = host.dispatch({"op": "finish_run", "run_id": plan["run_id"], "status": "complete"})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_categories"], [])

    def test_visible_history_diagnostic_rejects_implicit_or_expanded_consent(self):
        approved = dict(native.VISIBLE_HISTORY_DIAGNOSTIC)
        invalid = [None, {}, {k: v for k, v in approved.items() if k != "approval_id"},
                   {**approved, "approval_id": "different-consent"},
                   {**approved, "kind": "visible_history_always"},
                   {**approved, "category_id": "ai-products"},
                   {**approved, "max_visible_seconds": 121},
                   {**approved, "max_visible_seconds": 0},
                   {**approved, "max_visible_seconds": -1},
                   {**approved, "max_visible_seconds": 120.0},
                   {**approved, "max_visible_seconds": "120"},
                   {**approved, "allow_clipboard": True}]
        for value in invalid:
            self.config["diagnostic"] = value
            self.write_config()
            self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def continuation_prior(self, prior_id=None, **changes):
        prior_id = prior_id or native.VISIBLE_HISTORY_CONTINUATION["resume_from_run_id"]
        prior = {"schema_version": 1, "run_id": prior_id, "mode": "probe", "status": "partial",
                 "reports": {}, "created_at": "2026-10-18T12:50:19+08:00",
                 "finished_at": "2026-10-18T12:50:40+08:00",
                 "categories": {"ai-models": {"complete": False, "error": "visible_condition_not_established",
                                                "reason": "read_failed"}}, **changes}
        path = self.root / "evidence/extension-probe" / prior_id / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(native.json_bytes(prior))
        return path

    def continuation_config(self, **changes):
        self.config.update(request_id="visible-continuation",
                           diagnostic={**native.VISIBLE_HISTORY_CONTINUATION, **changes})
        self.write_config()

    def test_visible_continuation_accepts_only_fixed_failed_short_run_without_mutating_it(self):
        path = self.continuation_prior()
        before = path.read_bytes()
        self.continuation_config()
        host = native.Host(self.root, [ORIGIN])
        plan = host.dispatch({"op": "get_plan"})
        self.assertEqual(plan["diagnostic"], native.VISIBLE_HISTORY_CONTINUATION)
        self.assertEqual(plan["diagnostic"]["max_visible_seconds"], 60)
        self.assertEqual(len(plan["sources"]), 6)
        self.assertEqual(path.read_bytes(), before)

    def test_visible_continuation_rejects_missing_prior_or_wrong_fixed_reference(self):
        self.continuation_config()
        self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))
        self.continuation_prior()
        for patch in ({"resume_from_run_id": "extension-probe-other"},
                      {"max_visible_seconds": 120}, {"max_visible_seconds": 61},
                      {"max_visible_seconds": 60.0}, {"category_id": "cross-border"}):
            self.continuation_config(**patch)
            self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def test_visible_continuation_rejects_wrong_state_report_capture_or_time_budget(self):
        self.continuation_config()
        cases = [{"status": "pending"}, {"status": "complete"}, {"mode": "production"},
                 {"reports": {"captured": {}}}, {"reports": []},
                 {"categories": {"ai-models": {"complete": False, "error": "other_failure", "reason": "read_failed"}}},
                 {"finished_at": "2026-10-18T12:50:45+08:00"},
                 {"finished_at": "2026-10-18T12:50:19+08:00"},
                 {"finished_at": "2026-10-18T12:50:18+08:00"},
                 {"finished_at": None}]
        for patch in cases:
            self.continuation_prior(**patch)
            self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def test_final_initialization_requires_both_fixed_failures_and_preserves_them(self):
        first = self.continuation_prior()
        final_id = native.VISIBLE_HISTORY_FINAL_INITIALIZATION["resume_from_run_id"]
        second = self.continuation_prior(prior_id=final_id)
        before = {path: path.read_bytes() for path in (first, second)}
        self.continuation_config(resume_from_run_id=final_id)
        host = native.Host(self.root, [ORIGIN])
        plan = host.dispatch({"op": "get_plan"})
        self.assertEqual(plan["diagnostic"], native.VISIBLE_HISTORY_FINAL_INITIALIZATION)
        self.assertEqual(len(plan["sources"]), 6)
        self.assertEqual(plan["status"], "pending")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_final_initialization_rejects_either_missing_prior(self):
        first_id = native.VISIBLE_HISTORY_CONTINUATION["resume_from_run_id"]
        final_id = native.VISIBLE_HISTORY_FINAL_INITIALIZATION["resume_from_run_id"]
        self.continuation_config(resume_from_run_id=final_id)
        first = self.continuation_prior(prior_id=first_id)
        self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))
        first.unlink()
        self.continuation_prior(prior_id=final_id)
        self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def test_final_initialization_validates_each_prior_independently_and_rejects_expansion(self):
        first_id = native.VISIBLE_HISTORY_CONTINUATION["resume_from_run_id"]
        final_id = native.VISIBLE_HISTORY_FINAL_INITIALIZATION["resume_from_run_id"]
        self.continuation_config(resume_from_run_id=final_id)
        invalid = [{"status": "complete"}, {"mode": "production"}, {"schema_version": 2},
                   {"reports": {"captured": {}}},
                   {"categories": {"ai-models": {"complete": False, "error": "history_timeout", "reason": "read_failed"}}},
                   {"finished_at": "2026-10-18T12:50:45+08:00"},
                   {"finished_at": "2026-10-18T12:50:19+08:00"}]
        for prior_id in (first_id, final_id):
            for patch in invalid:
                self.continuation_prior(prior_id=first_id)
                self.continuation_prior(prior_id=final_id)
                self.continuation_prior(prior_id=prior_id, **patch)
                self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))
        self.continuation_prior(prior_id=first_id)
        self.continuation_prior(prior_id=final_id)
        for patch in ({"max_visible_seconds": 120}, {"max_visible_seconds": 60.0},
                      {"max_visible_seconds": 61}, {"category_id": "ai-products"},
                      {"resume_from_run_id": "extension-probe-next"}, {"extra": True}):
            self.continuation_config(**{"resume_from_run_id": final_id, **patch})
            self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def startup_host(self):
        self.config.update(request_id="startup-gated-test", activation_event="startup")
        self.write_config()
        return native.Host(self.root, [ORIGIN])

    def test_startup_gate_accepts_only_exact_event_configuration_without_visible_diagnostic(self):
        for value in (None, True, "alarm", "installed", "manual", "STARTUP", ["startup"]):
            self.config["activation_event"] = value
            self.write_config()
            self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))
        self.config.update(activation_event="startup", diagnostic=dict(native.VISIBLE_HISTORY_DIAGNOSTIC))
        self.write_config()
        self.assertError("config_invalid", lambda: native.Host(self.root, [ORIGIN]))

    def test_startup_gate_waits_without_creating_run_for_all_other_get_plan_events(self):
        host = self.startup_host()
        expected = {"ok": True, "status": "awaiting_startup", "run_id": host.run_id}
        for event in (None, "installed", "alarm", "manual"):
            message = {"op": "get_plan"}
            if event is not None:
                message["event"] = event
            self.assertEqual(host.dispatch(message), expected)
            self.assertFalse(host.state_path.exists())
            self.assertFalse(host.run_dir.exists())
        self.assertEqual(host.dispatch({"op": "status"}), expected)
        self.assertTrue(host.dispatch({"op": "hello", "event": "startup"})["ok"])
        self.assertFalse(host.state_path.exists(), "hello startup must not consume the activation gate")

    def test_startup_gate_cannot_be_bypassed_by_save_finish_observe_or_internal_creation(self):
        host = self.startup_host()
        for op in ("save_report", "finish_category", "finish_run"):
            self.assertError("run_missing", lambda: host.dispatch({"op": op, "run_id": host.run_id}))
        self.assertError("run_missing", lambda: host.dispatch({"op": "observe", "run_id": host.run_id,
                                                               "event": "phase", "meta": {"phase": "startup"}}))
        self.assertError("startup_required", lambda: host.state(create=True))
        self.assertError("startup_required", lambda: host.state(create=True, activation_event="alarm"))
        self.assertFalse(host.run_dir.exists())

    def test_startup_activation_persists_and_allows_alarm_resume_without_restarting(self):
        host = self.startup_host()
        plan = host.dispatch({"op": "get_plan", "event": "startup"})
        state = host.state()
        self.assertEqual(state["activation"], {"required_event": "startup", "trigger_event": "startup",
                                               "observed_at": state["created_at"]})
        before = host.state_path.read_bytes()
        resumed = native.Host(self.root, [ORIGIN]).dispatch({"op": "get_plan", "event": "alarm"})
        self.assertEqual(resumed, plan)
        self.assertEqual(host.state_path.read_bytes(), before)
        coverage = {"complete": True, "reason": "boundary_before_start",
                    "boundary_time": "2026-10-15T00:00:00+08:00"}
        for category in native.SOURCES:
            host.dispatch({"op": "finish_category", "run_id": host.run_id,
                           "category_id": category, "coverage": coverage})
        self.assertEqual(host.dispatch({"op": "finish_run", "run_id": host.run_id})["status"], "complete")
        complete_bytes = host.state_path.read_bytes()
        for event in ("startup", "alarm", "installed", "manual"):
            self.assertEqual(native.Host(self.root, [ORIGIN]).dispatch({"op": "get_plan", "event": event})["status"], "complete")
            self.assertEqual(host.state_path.read_bytes(), complete_bytes)

    def test_startup_resume_and_completion_require_existing_activation_evidence(self):
        host = self.startup_host()
        host.dispatch({"op": "get_plan", "event": "startup"})
        valid = host.state()
        for activation in (None, {}, {"required_event": "startup", "trigger_event": "alarm", "observed_at": valid["created_at"]},
                           {"required_event": "startup", "trigger_event": "startup", "observed_at": "2000-01-01T00:00:00+08:00"}):
            state = {**valid, "status": "complete", "activation": activation}
            host.state_path.write_bytes(native.json_bytes(state))
            self.assertError("activation_invalid", lambda: host.dispatch({"op": "get_plan", "event": "alarm"}))
            self.assertError("activation_invalid", lambda: host.dispatch({"op": "finish_run", "run_id": host.run_id}))
            self.assertError("activation_invalid", lambda: host.dispatch({"op": "status"}))

    def test_full_text_is_exact_and_repeat_is_idempotent(self):
        request = self.report()
        first = self.host.dispatch(request)
        self.assertEqual(first["status"], "saved")
        state = self.host.state()
        saved = state["reports"][first["identity_sha256"]]
        body = self.host.run_dir / saved["body_file"]
        metadata = self.host.run_dir / saved["metadata_file"]
        before = (body.read_bytes(), metadata.read_bytes())
        self.assertEqual(body.read_bytes(), request["report"]["text"].encode("utf-8"))
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["report_date_beijing"], "2026-10-17")
        self.assertEqual(self.host.dispatch(request)["status"], "duplicate")
        self.assertEqual(before, (body.read_bytes(), metadata.read_bytes()))
        self.assertEqual(len(self.host.state()["reports"]), 1)
        self.assertEqual((self.root / "data/catalog.json").read_bytes(), b"production sentinel")
        self.assertFalse((self.root / "data/originals").exists())
        self.assertFalse((self.root / "data/intake-state.json").exists())

    def test_identity_conflict_preserves_existing_text(self):
        first = self.host.dispatch(self.report())
        body = next((self.host.run_dir / "reports").glob("*.md"))
        before = body.read_bytes()
        self.assertError("identity_conflict", lambda: self.host.dispatch(self.report(text="不同正文")))
        self.assertEqual(body.read_bytes(), before)
        self.assertEqual(self.host.state()["reports"][first["identity_sha256"]]["original_sha256"], native.digest(before))

    def test_partial_transaction_recovers_without_overwriting_body(self):
        result = self.host.dispatch(self.report())
        state = self.host.state()
        metadata = self.host.run_dir / state["reports"][result["identity_sha256"]]["metadata_file"]
        metadata.unlink()
        state["reports"] = {}
        self.host.save_state(state)
        self.assertEqual(self.host.dispatch(self.report())["status"], "duplicate")
        self.assertTrue(metadata.exists())

    def test_missing_or_tampered_saved_evidence_is_not_repaired(self):
        self.host.dispatch(self.report())
        body = next((self.host.run_dir / "reports").glob("*.md"))
        body.write_text("tampered", encoding="utf-8")
        self.assertError("identity_conflict", lambda: self.host.dispatch(self.report()))
        self.assertEqual(body.read_text(encoding="utf-8"), "tampered")

    def test_paths_sources_and_unknown_operations_are_rejected(self):
        for patch in ({"path": "../../data/catalog.json"}, {"path": "/c/conversation-ai-products"},
                      {"title": "外部任务"}):
            self.assertError("source_mismatch", lambda: self.host.dispatch(self.report(**patch)))
        self.assertError("unknown_category", lambda: self.host.dispatch({**self.report(), "category_id": "../../outside"}))
        self.assertError("run_mismatch", lambda: self.host.dispatch({**self.report(), "run_id": "../../outside"}))
        self.assertError("unknown_op", lambda: self.host.dispatch({"op": "execute", "command": "ignored"}))
        self.assertFalse((self.host.run_dir / "reports").exists())

    def test_client_turn_key_is_only_hashed_not_used_as_a_filename(self):
        result = self.host.dispatch(self.report(turn_key="../../data/catalog.json"))
        files = list((self.host.run_dir / "reports").iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(all(path.stem == result["identity_sha256"] for path in files))
        self.assertEqual((self.root / "data/catalog.json").read_bytes(), b"production sentinel")

    def test_guard_must_confirm_one_capture_and_zero_actual_calls(self):
        for guard in ({}, {"copy_intercepted": True, "real_clipboard_calls": 0},
                      {"copy_intercepted": 1, "real_clipboard_calls": 1},
                      {"copy_intercepted": 1, "real_clipboard_calls": 0, "legacy_copy_calls": 1}):
            self.assertError("clipboard_guard_failed", lambda: self.host.dispatch(self.report(guard=guard)))

    def test_report_requires_observed_time_and_safe_links(self):
        self.assertError("invalid_time", lambda: self.host.dispatch(self.report(report_time="2026-10-17")))
        self.assertError("outside_window", lambda: self.host.dispatch(self.report(report_time="2026-10-15T00:00:00Z")))
        for link in ("javascript:alert(1)", "file:///C:/secret", "https://name:secret@example.com"):
            self.assertError("invalid_report", lambda: self.host.dispatch(self.report(links=[link])))

    def test_pause_notice_is_not_a_report(self):
        self.assertError("invalid_report", lambda: self.host.dispatch(self.report(is_report=1)))
        skipped = self.host.dispatch(self.report(is_report=False, text="旧的暂停通知", report_time=None))
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(len(self.host.state()["reports"]), 0)
        self.assertFalse((self.host.run_dir / "reports").exists())

    def test_client_complete_claim_does_not_override_missing_categories(self):
        for category in list(native.SOURCES)[:-1]:
            self.finish(category)
        self.finish("cross-border", complete=False)
        result = self.host.dispatch({"op": "finish_run", "run_id": self.run_id, "status": "complete"})
        self.assertEqual(result["status"], "partial")
        plan = self.host.dispatch({"op": "get_plan"})
        self.assertEqual(plan["status"], "pending")
        self.assertEqual(len(plan["completed_categories"]), 5)
        self.finish("cross-border")
        self.assertEqual(self.host.dispatch({"op": "finish_run", "run_id": self.run_id})["status"], "complete")
        self.assertEqual(self.host.dispatch({"op": "get_plan"})["status"], "complete")
        self.assertError("run_complete", lambda: self.host.dispatch(self.report()))

    def test_complete_history_requires_strict_earlier_boundary_or_true_start(self):
        for patch in ({"boundary_time": "2026-10-16T00:00:00+08:00"},
                      {"reason": "conversation_start"}, {"error": "failed"},
                      {"reason": "browser_success"}):
            self.assertError("invalid_coverage", lambda: self.finish("ai-models", **patch))
        self.finish("ai-models", reason="conversation_start", reached_start=True)
        self.assertError("coverage_conflict", lambda: self.finish("ai-models", complete=False))

    def test_protocol_accepts_multiple_frames_without_extra_stdout(self):
        incoming, outgoing = BytesIO(), BytesIO()
        for op in ("hello", "get_plan", "status"):
            native.write_frame(incoming, {"op": op})
        incoming.seek(0)
        self.assertEqual(native.serve(incoming, outgoing, [ORIGIN], root=self.root), 0)
        outgoing.seek(0)
        replies = [native.read_frame(outgoing) for _ in range(3)]
        self.assertTrue(all(reply["ok"] for reply in replies))
        self.assertIsNone(native.read_frame(outgoing))

    def test_malformed_frames_return_safe_error_and_stop(self):
        frames = [(b"\x01", "truncated_frame"), (struct.pack("<I", native.MAX_FRAME + 1), "frame_size"),
                  (struct.pack("<I", 0), "frame_size"), (struct.pack("<I", 10) + b"{}", "truncated_frame"),
                  (struct.pack("<I", 1) + b"\xff", "invalid_json"),
                  (struct.pack("<I", 2) + b"[]", "invalid_request")]
        for frame, code in frames:
            outgoing = BytesIO()
            self.assertEqual(native.serve(BytesIO(frame), outgoing, [ORIGIN], root=self.root), 1)
            outgoing.seek(0)
            response = native.read_frame(outgoing)
            self.assertEqual(response["error"]["code"], code)
            self.assertIsNone(native.read_frame(outgoing))

    def test_json_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for raw in (b'{"op":"hello","op":"status"}', b'{"op":"hello","value":NaN}'):
            self.assertError("invalid_json", lambda: native.decode_json(raw))

    def test_process_lock_prevents_concurrent_write(self):
        with native.locked(self.root):
            self.assertError("busy", lambda: self.host.dispatch(self.report()))

    def observe(self, event="window_checked", meta=None, **changes):
        return self.host.dispatch({"op": "observe", "run_id": self.run_id, "event": event,
                                   "meta": meta if meta is not None else {}, **changes})

    def test_observations_append_small_metadata_without_mutating_completion(self):
        before = self.host.state_path.read_bytes()
        meta = {"id": 123, "state": "minimized", "focused": False, "tabCount": 1,
                "error": None, "window": {"tabs": [123, 124]},
                "url": "https://chatgpt.com/c/conversation-ai-models#personal-intel-probe"}
        self.assertEqual(self.observe(meta=meta), {"ok": True, "status": "observed", "event": "window_checked"})
        self.observe(event="failure", meta={"error": "probe_window_unexpected_state"})
        self.assertEqual(self.host.state_path.read_bytes(), before)
        lines = (self.host.run_dir / "observations.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["meta"], meta)
        self.assertEqual(first["event"], "window_checked")
        self.assertEqual(self.host.state()["status"], "pending")
        self.assertEqual(self.host.state()["categories"], {})

    def test_observation_requires_active_existing_matching_run(self):
        self.assertError("run_mismatch", lambda: self.observe(run_id="another-run"))
        self.config["request_id"] = "new-run"
        self.write_config()
        host = native.Host(self.root, [ORIGIN])
        self.assertError("run_missing", lambda: host.dispatch({"op": "observe", "run_id": host.run_id,
                                                               "event": "phase", "meta": {}}))
        self.config["enabled"] = False
        self.write_config()
        host = native.Host(self.root, [ORIGIN])
        self.assertError("disabled", lambda: host.dispatch({"op": "observe", "run_id": host.run_id,
                                                            "event": "phase", "meta": {}}))
        self.assertFalse(host.state_path.exists())

    def test_observation_rejects_sensitive_fields_extra_operations_and_urls(self):
        for key in ("text", "body", "innerHTML", "cookie", "authToken", "password", "authorization", "command", "script", "headers"):
            self.assertError("invalid_observation", lambda: self.observe(meta={key: "private"}))
        self.assertError("invalid_observation", lambda: self.observe(event="execute"))
        self.assertError("invalid_observation", lambda: self.observe(command="ignored"))
        for value in ("https://example.com", "/c/unknown", "C:\\private\\file", "\\\\server\\file",
                      "https://chatgpt.com/c/conversation-ai-models?token=secret",
                      "https://chatgpt.com/c/conversation-ai-models#unknown"):
            self.assertError("invalid_observation", lambda: self.observe(meta={"url": value}))
        self.assertFalse((self.host.run_dir / "observations.jsonl").exists())

    def test_observation_limits_depth_size_strings_and_numbers(self):
        invalid = [{"error": "x" * 301}, {"error": "line\nbreak"},
                   {"a": {"b": {"c": {"d": 1}}}}, {"a": [1] * 101},
                   {"count": float("inf")}, {"count": 10 ** 19},
                   {"items": ["x" * 300] * 60}]
        for meta in invalid:
            self.assertError("invalid_observation", lambda: self.observe(meta=meta))
        self.assertFalse((self.host.run_dir / "observations.jsonl").exists())

    def test_symlink_evidence_directory_is_rejected_when_supported(self):
        target = self.root / "external"
        target.mkdir()
        link = self.host.run_dir / "reports"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertError("unsafe_path", lambda: self.host.dispatch(self.report()))
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
