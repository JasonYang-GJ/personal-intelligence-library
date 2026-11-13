"""Contract and failure tests; never starts the real CLI or edits the library."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import background_summary as module


CATEGORY = {"id": "ai-models", "label": "AI模型新发布", "focus": "发布内容、适用任务、使用条件"}
ORIGINAL = "# AI 模型雷达｜2026 年 10 月 18 日\r\n仅限指定地区，报价未核验。\r\n[官方](https://example.com/model?utm_source=chatgpt.com)\r\n重复 https://example.com/model\r\n"


def good_card():
    return {"title": "模型能力更新", "summary": "原报告介绍模型能力变化，试用资格和效果仍需确认。",
            "audience": "个人开发者", "relevance": "整理者判断：可用自己的样例比较效果。",
            "recommendation": "继续观察", "recommendation_reason": "地区限制存在，尚未实测。",
            "next_action": "先核对所在地区是否可试用。", "report_date": "2026-10-18",
            "report_date_evidence": "# AI 模型雷达｜2026 年 10 月 18 日",
            "uncertainties": ["原文报价、来源与可用条件未核验。"], "tags": ["模型试用"]}


def good_events():
    return [{"type": "thread.started", "thread_id": "test"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}}]


def event_bytes(events):
    return ("\n".join(json.dumps(v, ensure_ascii=False) for v in events) + "\n").encode()


class CardTests(unittest.TestCase):
    def test_adapts_build_fields_without_model_identity(self):
        result = module.validate_card(good_card(), ORIGINAL, CATEGORY)
        self.assertEqual(result["status"], "待验证")
        self.assertEqual(result["category_id"], "ai-models")
        self.assertEqual(result["source_task"], CATEGORY["label"])
        self.assertIsInstance(result["audience"], str)
        self.assertNotIn("report_date_evidence", result)
        self.assertNotIn("related_ids", result)
        self.assertNotIn("original_file", result)
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["verification"], "未核验")

    def test_unknown_fields_and_model_sources_rejected(self):
        for field, value in (("sources", []), ("id", "intel-20261018-999"), ("status", "准备做")):
            with self.subTest(field=field):
                card = good_card()
                card[field] = value
                with self.assertRaises(module.SummaryError):
                    module.validate_card(card, ORIGINAL, CATEGORY)

    def test_invalid_enum_type_and_length_rejected(self):
        for field, value in (("recommendation", "立即开发"), ("summary", "x" * 221),
                             ("audience", ["开发者"]), ("tags", []), ("tags", ["a", "a"]),
                             ("relevance", "未注明判断"), ("title", " ")):
            with self.subTest(field=field, value=value):
                card = good_card()
                card[field] = value
                with self.assertRaises(module.SummaryError):
                    module.validate_card(card, ORIGINAL, CATEGORY)

    def test_core_budget(self):
        card = good_card()
        card.update(summary="x" * 180, next_action="x" * 180, recommendation_reason="x" * 150)
        with self.assertRaises(module.SummaryError):
            module.validate_card(card, ORIGINAL, CATEGORY)

    def test_new_url_in_summary_rejected(self):
        card = good_card()
        card["next_action"] = "访问 https://invented.example/"
        with self.assertRaises(module.SummaryError):
            module.validate_card(card, ORIGINAL, CATEGORY)

    def test_report_date_requires_exact_report_evidence(self):
        card = good_card()
        for replacement in ({"report_date": "2026-10-17"}, {"report_date_evidence": "2026 年 10 月 18 日"},
                            {"report_date": None}, {"report_date_evidence": None}):
            with self.subTest(replacement=replacement), self.assertRaises(module.SummaryError):
                module.validate_card({**card, **replacement}, ORIGINAL, CATEGORY)

    def test_source_post_and_product_release_are_not_report_dates(self):
        original = "信号一：2026 年 10 月 18 日某帖子\n模型发布于2026-10-18。\n来源发布日期：2026-10-18\n发布日期：2026-10-18\n"
        self.assertEqual(module.report_date_candidates(original), [])
        card = good_card()
        with self.assertRaises(module.SummaryError):
            module.validate_card(card, original, CATEGORY)
        card.update(report_date=None, report_date_evidence=None)
        self.assertIsNone(module.validate_card(card, original, CATEGORY)["report_date"])

    def test_date_range_invalid_date_and_late_product_heading_rejected(self):
        self.assertEqual(module.report_date_candidates("# 报告｜2026-10-17至2026-10-18"), [])
        self.assertEqual(module.report_date_candidates("报告日期：2026-02-30"), [])
        self.assertEqual(module.report_date_candidates("\n" * 10 + "# 模型报告｜2026-10-18"), [])

    def test_early_report_check_time_is_explicit_date(self):
        line = "**检查时间：2026 年 10 月 18 日 12:35（北京时间）**"
        self.assertEqual(module.report_date_candidates("# 全球 AI 模型雷达\n\n" + line),
                         [{"date": "2026-10-18", "evidence": line}])
        for original in ("\n" * 8 + line, "某产品" + line, line + " https://example.com",
                         "检查时间：2026 年 10 月 18 日，某产品发布时间"):
            self.assertEqual(module.report_date_candidates(original), [])

    def test_deterministic_source_labels_tracking_and_parentheses(self):
        original = "[原网址](https://example.com/a_(b)?utm_source=x)\nhttps://example.com/a_(b)\nhttps://u:pw@example.com/private\nhttps://other.example/a。"
        sources = module.extract_sources(original)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["url"], "https://example.com/a_(b)?utm_source=x")
        self.assertEqual(sources[0]["label"], "原网址")
        self.assertEqual(sources[1]["url"], "https://other.example/a")

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(module.SummaryError):
            module._parse_json('{"title":"a","title":"b"}')


class EventTests(unittest.TestCase):
    def test_agent_only_accepted(self):
        self.assertEqual(module.inspect_events(event_bytes(good_events()))["item_types"], ["agent_message"])

    def test_all_tool_and_unknown_items_rejected(self):
        for item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search",
                          "computer_use", "collab_tool_call", "code_mode", "future_tool"):
            events = good_events()
            events.insert(2, {"type": "item.started", "item": {"type": item_type}})
            with self.subTest(item_type=item_type), self.assertRaises(module.SummaryError):
                module.inspect_events(event_bytes(events))

    def test_failure_malformed_and_missing_completion_rejected(self):
        for raw in (event_bytes(good_events()[:-1]), b"not json\n", event_bytes([{"type": "turn.failed"}]),
                    event_bytes([{"type": "error"}]), event_bytes(good_events() + [{"type": "turn.started"}])):
            with self.subTest(raw=raw), self.assertRaises((module.SummaryError, ValueError)):
                module.inspect_events(raw)

    def test_exact_disabled_capability_warnings_allowed_only_before_turn(self):
        warning = {"type": "item.completed", "item": {"type": "error", "message": module.DISABLED_CODE_MODE_WARNING}}
        events = good_events()
        events.insert(1, warning)
        self.assertEqual(module.inspect_events(event_bytes(events))["startup_warnings"], [module.DISABLED_CODE_MODE_WARNING])
        events = good_events()
        events.insert(2, warning)
        with self.assertRaises(module.SummaryError):
            module.inspect_events(event_bytes(events))

    def test_unstable_warning_requires_exact_feature_and_completed_item(self):
        text = r"Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in C:\Users\Example\.codex\config.toml."
        events = good_events()
        events.insert(1, {"type": "item.completed", "item": {"type": "error", "message": text}})
        self.assertEqual(module.inspect_events(event_bytes(events))["startup_warnings"], [text])
        for event_type, item_type, message in (("item.started", "error", text),
                                               ("item.completed", "mcp_tool_call", text),
                                               ("item.completed", "error", text.replace("skip_host_skill_discovery", "unsafe_feature"))):
            events = good_events()
            events.insert(1, {"type": event_type, "item": {"type": item_type, "message": message}})
            with self.subTest(event_type=event_type, item_type=item_type), self.assertRaises(module.SummaryError):
                module.inspect_events(event_bytes(events))
        warning = {"type": "item.completed", "item": {"type": "error", "message": module.DISABLED_CODE_MODE_WARNING + " A different error."}}
        events = good_events()
        events.insert(1, warning)
        with self.assertRaises(module.SummaryError):
            module.inspect_events(event_bytes(events))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "evidence" / "summary-test"
        self.cli = self.root / "fake-codex.exe"
        self.cli.write_bytes(b"test, never executed")
        self.cli_patch = patch.object(module, "CLI", self.cli)
        self.cli_patch.start()
        self.addCleanup(self.cli_patch.stop)
        self.spawn_calls = []

    def fake_popen(self, command, **kwargs):
        self.spawn_calls.append((command, kwargs))
        self.assertTrue((self.out / "input.json").is_file())
        self.assertTrue((self.out / "schema.json").is_file())
        self.assertTrue((self.out / "attempt.json").is_file())
        outer = self

        class Process:
            pid = 12345
            returncode = None

            def communicate(self, content=None, timeout=None):
                outer.assertEqual(content, (outer.out / "input.json").read_bytes())
                packet = json.loads(content)
                outer.assertEqual(packet["original_report"], ORIGINAL)
                kwargs["stdout"].write(event_bytes(good_events()))
                (outer.out / "card.json").write_text(json.dumps(good_card(), ensure_ascii=False), encoding="utf-8")
                self.returncode = 0
                return None, None

            def poll(self):
                return self.returncode

        return Process()

    def test_success_preserves_bytes_and_uses_focus_json_stdin(self):
        raw = ORIGINAL.encode()
        with patch.object(module.subprocess, "Popen", side_effect=self.fake_popen):
            result = module.summarize(raw, CATEGORY, self.out, self.root)
        self.assertEqual(result["title"], good_card()["title"])
        self.assertEqual((self.out / "original.md").read_bytes(), raw)
        packet = json.loads((self.out / "input.json").read_text(encoding="utf-8"))
        self.assertEqual(packet["category"]["focus"], CATEGORY["focus"])
        self.assertTrue(json.loads((self.out / "result.json").read_text())["passed"])
        self.assertEqual(len(self.spawn_calls), 1)
        command, kwargs = self.spawn_calls[0]
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("read-only", command)
        self.assertIn('web_search="disabled"', command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("project_doc_max_bytes=0", command)
        self.assertIn('forced_login_method="chatgpt"', command)
        for feature in ("shell_tool", "apps", "plugins", "browser_use", "computer_use", "multi_agent", "hooks", "memories", "code_mode_host"):
            self.assertIn(["--disable", feature], [command[i:i+2] for i in range(len(command)-1)])
        self.assertEqual(kwargs["cwd"], self.out.resolve())

    def test_existing_output_not_overwritten_or_retried(self):
        self.out.mkdir(parents=True)
        (self.out / "card.json").write_text("first failed answer")
        with patch.object(module.subprocess, "Popen") as popen, self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        popen.assert_not_called()
        self.assertEqual((self.out / "card.json").read_text(), "first failed answer")

    def test_reuse_revalidates_first_attempt_without_call_or_write(self):
        with patch.object(module.subprocess, "Popen", side_effect=self.fake_popen):
            expected = module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.out.iterdir()}
        with patch.object(module.subprocess, "Popen") as popen:
            actual = module.load_validated(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        popen.assert_not_called()
        self.assertEqual(expected, actual)
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.out.iterdir()})

    def test_reuse_rejects_changed_input_raw_card_events_and_validated_card(self):
        with patch.object(module.subprocess, "Popen", side_effect=self.fake_popen):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        for filename in ("input.json", "schema.json", "events.jsonl", "card.json", "validated-card.json"):
            path = self.out / filename
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            try:
                # Semantic equality is sufficient for derived validated-card;
                # change its value instead of inconsequential whitespace.
                if filename == "validated-card.json":
                    data = json.loads(original)
                    data["status"] = "准备做"
                    path.write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(filename=filename), self.assertRaises(module.SummaryError):
                    module.load_validated(ORIGINAL.encode(), CATEGORY, self.out, self.root)
            finally:
                path.write_bytes(original)
        with self.assertRaises(module.SummaryError):
            module.load_validated(ORIGINAL.encode(), {**CATEGORY, "focus": "changed"}, self.out, self.root)

    def test_explicit_warning_revalidation_preserves_failure_and_model_answer(self):
        # Reproduce the original validator's false-negative without a new model.
        with patch.object(module.subprocess, "Popen", side_effect=self.fake_popen), patch.object(module, "inspect_events", side_effect=module.SummaryError("Forbidden or unknown tool item")), self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        events = good_events()
        events.insert(1, {"type": "item.completed", "item": {"type": "error", "message": module.DISABLED_CODE_MODE_WARNING}})
        events[-2]["item"]["text"] = json.dumps(good_card())
        (self.out / "events.jsonl").write_bytes(event_bytes(events))
        preserved = {p.name: p.read_bytes() for p in self.out.iterdir()}
        with patch.object(module.subprocess, "Popen") as popen:
            card = module.revalidate_attempt(ORIGINAL.encode(), CATEGORY, self.out, self.root)
            self.assertEqual(module.load_validated(ORIGINAL.encode(), CATEGORY, self.out, self.root), card)
        popen.assert_not_called()
        self.assertFalse(json.loads((self.out / "result.json").read_text())["passed"])
        self.assertTrue(json.loads((self.out / "revalidation.json").read_text())["passed"])
        for name, raw in preserved.items():
            self.assertEqual((self.out / name).read_bytes(), raw)
        with self.assertRaises(module.SummaryError):
            module.revalidate_attempt(ORIGINAL.encode(), CATEGORY, self.out, self.root)

    def test_production_path_refused(self):
        with patch.object(module.subprocess, "Popen") as popen, self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.root / "data" / "summary", self.root)
        popen.assert_not_called()

    def test_failure_exit_preserves_events_and_no_validated_card(self):
        def failed(command, **kwargs):
            process = self.fake_popen(command, **kwargs)
            normal = process.communicate
            def communicate(*args, **kw):
                normal(*args, **kw)
                process.returncode = 7
            process.communicate = communicate
            return process
        with patch.object(module.subprocess, "Popen", side_effect=failed), self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        self.assertTrue((self.out / "events.jsonl").read_bytes())
        self.assertTrue((self.out / "card.json").exists())
        self.assertFalse((self.out / "validated-card.json").exists())
        self.assertFalse(json.loads((self.out / "result.json").read_text())["passed"])
        self.assertEqual(len(self.spawn_calls), 1)

    def test_original_tamper_fails_without_hiding_model_answer(self):
        def tamper(command, **kwargs):
            process = self.fake_popen(command, **kwargs)
            normal = process.communicate
            def communicate(*args, **kw):
                normal(*args, **kw)
                (self.out / "original.md").write_bytes(b"changed")
            process.communicate = communicate
            return process
        with patch.object(module.subprocess, "Popen", side_effect=tamper), self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        self.assertTrue((self.out / "card.json").exists())
        self.assertFalse((self.out / "validated-card.json").exists())

    def test_forbidden_tool_event_rejects_valid_card(self):
        def tool_use(command, **kwargs):
            process = self.fake_popen(command, **kwargs)
            normal = process.communicate
            def communicate(*args, **kw):
                normal(*args, **kw)
                kwargs["stdout"].write(event_bytes([{"type": "item.completed", "item": {"type": "web_search"}}]))
            process.communicate = communicate
            return process
        with patch.object(module.subprocess, "Popen", side_effect=tool_use), self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        self.assertFalse((self.out / "validated-card.json").exists())

    def test_environment_drops_api_keys_and_tokens_keeps_login_home(self):
        with patch.dict(module.os.environ, {"CODEX_API_KEY": "secret", "OPENAI_API_KEY": "secret", "CODEX_ACCESS_TOKEN": "secret", "AZURE_OPENAI_ENDPOINT": "secret", "CODEX_HOME": "existing-auth-home", "USERPROFILE": "user-home"}, clear=True):
            env = module.sanitized_environment()
        self.assertEqual(env["CODEX_HOME"], "existing-auth-home")
        self.assertEqual(env["USERPROFILE"], "user-home")
        self.assertFalse(any("KEY" in key or "TOKEN" in key or key.startswith("AZURE_OPENAI") for key in env))

    def test_timeout_targets_only_owned_child(self):
        created = []
        def timeout(command, **kwargs):
            process = self.fake_popen(command, **kwargs)
            count = 0
            def communicate(*args, **kw):
                nonlocal count
                count += 1
                if count == 1:
                    kwargs["stdout"].write(b'{"type":"turn.started"}\n')
                    raise subprocess.TimeoutExpired(command, module.TIMEOUT_SECONDS)
                process.returncode = -1
            process.communicate = communicate
            created.append(process)
            return process
        with patch.object(module.subprocess, "Popen", side_effect=timeout), patch.object(module, "_terminate_owned_tree") as kill, self.assertRaises(module.SummaryError):
            module.summarize(ORIGINAL.encode(), CATEGORY, self.out, self.root)
        kill.assert_called_once_with(created[0])
        self.assertEqual(len(self.spawn_calls), 1)
        self.assertTrue((self.out / "events.jsonl").read_bytes())
        self.assertTrue(json.loads((self.out / "result.json").read_text())["timed_out"])


if __name__ == "__main__":
    unittest.main()
