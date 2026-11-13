from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import catchup_watch as watch
from catchup_policy import BEIJING, CATEGORIES


class CatchupHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 18, 12, tzinfo=BEIJING)
        self.idle = {'available': True, 'busy': False, 'event': 'task_complete',
                     'at': '2026-10-17T01:00:00+08:00', 'turn_id': 'old'}
        watch.write_json(self.root / 'data/catchup-config.json', {'schema_version': 1, 'enabled': True})
        self.ledger = {'schema_version': 1, 'tasks': {c: {'checked_through': '2026-10-17T09:00:00+08:00'} for c in CATEGORIES}}
        watch.write_json(self.root / 'data/intake-state.json', self.ledger)
        self.before = (self.root / 'data/intake-state.json').read_bytes()

    def run_host(self, activity=None, helper=None, now=None, dry=False):
        with patch.object(watch, 'now_local', return_value=now or self.now), \
             patch.object(watch, 'native_activity', return_value=activity or self.idle), \
             patch.object(watch, 'helper', side_effect=helper or AssertionError('UI should not be called')):
            return watch.run_once(self.root, dry_run=dry)

    def test_completed_target_never_touches_ui(self):
        for item in self.ledger['tasks'].values():
            item['checked_through'] = '2026-10-18T09:01:00+08:00'
        watch.write_json(self.root / 'data/intake-state.json', self.ledger)
        self.assertEqual(self.run_host()['action'], 'covered')
        self.assertEqual(self.run_host()['trigger_count'], 0)

    def test_app_not_ready_only_probes_and_preserves_checkpoints(self):
        calls = []
        def helper(root, config, probe):
            calls.append(probe)
            return {'ok': False, 'triggered': False, 'status': 'waiting', 'reason': 'app_closed'}
        result = self.run_host(helper=helper)
        self.assertEqual(result['reason'], 'app_not_ready')
        self.assertEqual(calls, [True])
        self.assertEqual((self.root / 'data/intake-state.json').read_bytes(), self.before)

    def test_native_running_never_queues_another_run(self):
        result = self.run_host(activity={**self.idle, 'busy': True, 'event': 'task_started'})
        self.assertEqual(result['reason'], 'native_task_busy')

    def test_reserve_before_trigger_confirm_then_complete_once(self):
        busy = {**self.idle, 'busy': True, 'event': 'task_started', 'at': self.now.isoformat(), 'turn_id': 'new'}
        calls = []
        def helper(root, config, probe):
            calls.append(probe)
            if not probe:
                self.assertEqual(watch.load_runtime(root)['phase'], 'starting')
            return {'ok': True, 'triggered': not probe, 'status': 'ready' if probe else 'triggered'}
        with patch.object(watch, 'now_local', return_value=self.now), \
             patch.object(watch, 'native_activity', side_effect=[self.idle, self.idle, busy]), \
             patch.object(watch, 'helper', side_effect=helper):
            result = watch.run_once(self.root)
        self.assertEqual(result['phase'], 'running')
        self.assertEqual(calls, [True, False])
        self.assertEqual((self.root / 'data/intake-state.json').read_bytes(), self.before)
        for item in self.ledger['tasks'].values():
            item['checked_through'] = self.now.isoformat()
        watch.write_json(self.root / 'data/intake-state.json', self.ledger)
        done = {**busy, 'busy': False, 'event': 'task_complete', 'at': (self.now + timedelta(minutes=5)).isoformat()}
        self.assertEqual(self.run_host(activity=done, now=self.now + timedelta(minutes=6))['action'], 'covered')
        self.assertEqual(self.run_host(activity=done, now=self.now + timedelta(minutes=7))['trigger_count'], 1)

    def test_uncertain_ui_timeout_never_requeues_without_native_evidence(self):
        def helper(root, config, probe):
            if probe:
                return {'ok': True, 'triggered': False, 'status': 'ready'}
            raise subprocess.TimeoutExpired('fixture', 45)
        result = self.run_host(helper=helper)
        self.assertEqual(result['phase'], 'starting')
        result = self.run_host(now=self.now + timedelta(seconds=30))
        self.assertEqual(result['reason'], 'launch_awaiting_confirmation')
        result = self.run_host(now=self.now + timedelta(minutes=17))
        self.assertEqual(result['reason'], 'launch_awaiting_confirmation')
        self.assertEqual(result['trigger_count'], 1)
        self.assertEqual((self.root / 'data/intake-state.json').read_bytes(), self.before)

    def test_daily_completion_during_probe_is_rechecked_before_launch(self):
        calls = []
        def helper(root, config, probe):
            calls.append(probe)
            for item in self.ledger['tasks'].values():
                item['checked_through'] = self.now.isoformat()
            watch.write_json(root / 'data/intake-state.json', self.ledger)
            return {'ok': True, 'triggered': False, 'status': 'ready'}
        result = self.run_host(helper=helper)
        self.assertEqual(result['action'], 'covered')
        self.assertEqual(calls, [True])
        self.assertEqual(result['trigger_count'], 0)

    def test_ended_run_without_full_coverage_is_a_retry_not_success(self):
        watch.write_json(self.root / 'data/catchup-runtime.json',
                         {'schema_version': 1, 'phase': 'running', 'requested_at': self.now.isoformat(), 'trigger_count': 1})
        done = {**self.idle, 'at': (self.now + timedelta(minutes=5)).isoformat()}
        result = self.run_host(activity=done, now=self.now + timedelta(minutes=6))
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(result['reason'], 'failure_cooldown')
        self.assertEqual((self.root / 'data/intake-state.json').read_bytes(), self.before)

    def test_confirmed_no_dispatch_failure_waits_fifteen_minutes(self):
        calls = []
        def helper(root, config, probe):
            calls.append(probe)
            return {'ok': probe, 'triggered': False, 'status': 'ready' if probe else 'waiting',
                    'reason': 'ready' if probe else 'foreground_window_changed'}
        result = self.run_host(helper=helper)
        self.assertEqual(result['phase'], 'failed')
        result = self.run_host(now=self.now + timedelta(minutes=14))
        self.assertEqual(result['reason'], 'failure_cooldown')
        self.assertEqual(calls, [True, False])

    def test_host_lock_prevents_second_writer_and_releases_on_exit(self):
        with watch.host_lock(self.root):
            with self.assertRaises(OSError):
                with watch.host_lock(self.root):
                    self.fail('Concurrent writer admitted')
        with watch.host_lock(self.root):
            pass

    def test_lifecycle_ignores_partial_tail_and_never_reads_message_contents_as_status(self):
        p = self.root / 'own-rollout.jsonl'
        p.write_text(json.dumps({'type': 'event_msg', 'timestamp': self.now.isoformat(), 'payload': {'type': 'task_started', 'turn_id': 'real'}}) + '\n' +
                     json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'content': 'task_complete'}}) + '\n{"type":', encoding='utf-8')
        self.assertTrue(watch.last_activity(p)['busy'])

    def test_verification_request_is_ignored_after_confirmed_coverage(self):
        request = {'schema_version': 1, 'id': 'one-off', 'purpose': 'user_authorized_integration_verification',
                   'target': self.now.isoformat(), 'expires_at': (self.now + timedelta(hours=1)).isoformat()}
        watch.write_json(self.root / 'data/catchup-verification-request.json', request)
        runtime = {'schema_version': 1, 'phase': 'idle', 'verification_completed_id': 'one-off'}
        self.assertIsNone(watch.verification_request(self.root, runtime, self.now))


if __name__ == '__main__':
    unittest.main()
