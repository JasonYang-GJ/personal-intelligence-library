"""Manual-only mode: unattended entry points cannot start a browser or intake."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import background_schedule as schedule
import manual_intake as manual
import catchup_watch as watch
import extension_native_host as native


class ManualOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.fromisoformat('2026-10-20T12:00:00+08:00')
        self.identity = str(uuid.uuid4())
        self.write('data/management/project.json', {'schema_version': 1, 'project_id': self.identity})
        self.write('.intake/background/config.json', {'schema_version': 1, 'enabled': True})
        self.write('data/catchup-config.json', {'schema_version': 1, 'enabled': True,
                   'backend': 'background', 'mode': 'manual_only'})
        self.checkpoints(self.now - timedelta(days=1))
        self.addCleanup(patch.stopall)
        patch.object(schedule, 'now_local', return_value=self.now).start()
        self.native = patch.object(schedule, 'native_activity', side_effect=AssertionError('No desktop/daily dependency')).start()
        self.runner = patch.object(schedule, 'invoke_intake', side_effect=self.complete).start()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def checkpoints(self, when):
        self.write('data/intake-state.json', {'schema_version': 1, 'tasks': {
            key: {'checked_through': when.isoformat()} for key in schedule.CATEGORIES}})

    def request(self):
        identity = str(uuid.uuid4())
        self.write(f'.intake/manual/requests/{identity}.json', {'schema_version': 1,
                   'project_id': self.identity, 'id': identity, 'action': 'intake',
                   'requested_at': self.now.isoformat()})
        return identity

    def complete(self, root, run_id):
        self.checkpoints(self.now)
        return {'status': 'complete', 'summary': '6/6'}

    def test_daily_and_catchup_do_not_read_native_or_run_intake(self):
        before = set(self.root.rglob('*'))
        for source in ('daily', 'catchup'):
            for dry in (False, True):
                result = schedule.run_once(self.root, source=source, dry_run=dry)
                self.assertEqual(result['reason'], 'manual_only')
        self.native.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(before, set(self.root.rglob('*')))

    def test_manual_works_with_daily_paused_and_desktop_absent(self):
        self.request()
        self.assertEqual(manual.process_pending(self.root)['action'], 'completed')
        self.native.assert_not_called()
        self.runner.assert_called_once()
        self.assertIsNone(manual.process_pending(self.root))

    def test_no_request_has_no_side_effect(self):
        before = set(self.root.rglob('*'))
        self.assertIsNone(manual.process_pending(self.root))
        self.assertEqual(before, set(self.root.rglob('*')))
        self.runner.assert_not_called()

    def test_failure_is_terminal_and_does_not_retry(self):
        request_id = self.request()
        self.runner.side_effect = RuntimeError('fixture capture failure')
        self.assertEqual(manual.process_pending(self.root)['manual_result']['status'], 'failed')
        self.assertIsNone(manual.process_pending(self.root))
        self.runner.assert_called_once()

    def test_cooldown_requires_another_click(self):
        self.write('.intake/background/runtime.json', {'schema_version': 1, 'phase': 'failed',
                   'run_count': 1, 'failed_at': self.now.isoformat(), 'last_failure': 'intake_partial'})
        self.request()
        result = manual.process_pending(self.root)
        self.assertEqual(result['manual_result']['status'], 'failed')
        self.assertIn('重新点击', result['manual_result']['message'])
        self.assertIsNone(manual.process_pending(self.root))
        self.runner.assert_not_called()

    def test_manual_watcher_never_falls_through_to_automatic(self):
        with patch.object(watch, 'ROOT', self.root), patch.object(sys, 'argv', ['catchup_watch.py']), \
             patch.object(schedule, 'main', side_effect=AssertionError('automatic fallthrough')):
            self.assertEqual(watch.main(), 0)

    def reader(self):
        request_id = self.request()
        run_id = 'bg-manual-lease-test'
        self.write('.intake/extension-probe/config.json', {'extension_id': 'a' * 32,
            'enabled': True, 'mode': 'probe', 'request_id': run_id,
            'scan_start': '2026-10-16T00:00:00+08:00', 'scan_end': None,
            'sources': [{'category_id': k, 'task_name': v, 'conversation_id': 'conversation-' + k}
                        for k, v in native.SOURCES.items()]})
        self.write('.intake/background/runtime.json', {'schema_version': 1, 'phase': 'running',
            'source': 'manual', 'run_count': 1, 'run_id': run_id, 'manual_request_id': request_id})
        self.write(f'.intake/manual/results/{request_id}.json', {'schema_version': 1,
            'project_id': self.identity, 'id': request_id, 'status': 'running', 'run_id': run_id,
            'message': 'reading', 'updated_at': self.now.isoformat()})
        return native.Host(self.root, ['chrome-extension://' + 'a' * 32 + '/'])

    def test_browser_restart_cannot_resume_orphaned_manual_plan(self):
        host = self.reader()
        with schedule.schedule_lock(self.root):
            pass  # Runner exited, but its old running state and enabled plan survived.
        self.assertEqual(host.dispatch({'op': 'get_plan', 'event': 'startup'})['status'], 'disabled')
        self.assertFalse(host.state_path.exists())

    def test_reader_only_runs_while_manual_coordinator_owns_lock(self):
        host = self.reader()
        with schedule.schedule_lock(self.root):
            self.assertEqual(host.dispatch({'op': 'get_plan'})['status'], 'pending')
        self.assertEqual(host.dispatch({'op': 'get_plan'})['status'], 'disabled')


if __name__ == '__main__':
    unittest.main()
