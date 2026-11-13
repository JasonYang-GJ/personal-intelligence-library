import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from management_state import load_management

PROJECT_ID = '00000000-0000-4000-8000-000000000005'


class ManagementEvents(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = self.root / 'data/management/events'
        self.events.mkdir(parents=True)
        (self.events.parent / 'project.json').write_text(json.dumps({'schema_version': 1, 'project_id': PROJECT_ID}), encoding='utf-8')
        self.original = self.root / 'data/originals/example.md'
        self.original.parent.mkdir()
        self.original.write_bytes('真实原文保持不变'.encode())
        self.before = self.original.read_bytes()

    def event(self, suffix, action, state, order, restore_to=None):
        event = {'schema_version': 1, 'project_id': PROJECT_ID,
                 'id': f'56f99d1e-a4f4-446d-8319-{suffix:012d}', 'record_id': 'intel-20261018-001',
                 'action': action, 'state': state, 'restore_to': restore_to or state, 'order': order,
                 'created_at': '2026-10-18T15:00:00.000Z'}
        path = self.events / (event['id'] + '.json')
        path.write_bytes((json.dumps(event, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        return path

    def test_archive_trash_and_restore_leave_the_original_and_all_events(self):
        self.event(1, 'archive', 'archived', 10)
        self.event(2, 'trash', 'trashed', 11, 'archived')
        state = load_management(self.root)
        self.assertEqual(state['states']['intel-20261018-001']['restore_to'], 'archived')
        self.event(3, 'restore', 'archived', 12)
        final = load_management(self.root)
        self.assertEqual(final['states']['intel-20261018-001']['state'], 'archived')
        self.assertEqual(final['event_count'], 3)
        self.assertEqual(self.original.read_bytes(), self.before)

    def test_two_concurrent_files_are_kept_and_ties_are_deterministic(self):
        self.event(2, 'trash', 'trashed', 10, 'active')
        self.event(1, 'archive', 'archived', 10)
        state = load_management(self.root)
        self.assertEqual(state['event_count'], 2)
        self.assertEqual(state['states']['intel-20261018-001']['state'], 'trashed')
        again = load_management(self.root)
        self.assertEqual(state, again)

    def test_unclosed_empty_file_does_not_apply_an_action(self):
        file = self.event(1, 'archive', 'archived', 10)
        raw = file.read_bytes()
        file.write_bytes(b'')
        incomplete = load_management(self.root)
        self.assertEqual(incomplete['states'], {})
        self.assertEqual(incomplete['empty_files'], [file.name])
        file.write_bytes(raw)
        self.assertEqual(load_management(self.root)['states']['intel-20261018-001']['state'], 'archived')

    def test_nonempty_corruption_does_not_silently_show_everything_active(self):
        file = self.event(1, 'archive', 'archived', 10)
        file.write_bytes(b'{"schema_version":')
        with self.assertRaises(ValueError):
            load_management(self.root)

    def test_foreign_identity_duplicate_keys_and_filename_changes_are_rejected(self):
        file = self.event(1, 'archive', 'archived', 10)
        original = file.read_text(encoding='utf-8')
        for bad in [original.replace(PROJECT_ID, 'other-project'), original.replace('"schema_version": 1,', '"schema_version": 1, "schema_version": 1,')]:
            file.write_text(bad, encoding='utf-8')
            with self.assertRaises(ValueError):
                load_management(self.root)
        file.write_text(original, encoding='utf-8')
        file.rename(file.with_name('wrong.json'))
        with self.assertRaises(ValueError):
            load_management(self.root)

    def test_fingerprint_uses_disk_bytes_in_sorted_filename_order(self):
        paths = [self.event(2, 'trash', 'trashed', 11, 'active'), self.event(1, 'archive', 'archived', 10)]
        expected = '\n'.join(p.name + ':' + hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths))
        self.assertEqual(load_management(self.root)['fingerprint'], hashlib.sha256(expected.encode()).hexdigest())

    def test_absent_store_is_compatible_with_old_projects(self):
        with tempfile.TemporaryDirectory() as old:
            state = load_management(Path(old))
            self.assertEqual(state['states'], {})
            self.assertIsNone(state['project_id'])


if __name__ == '__main__':
    unittest.main()
