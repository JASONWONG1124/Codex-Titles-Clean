"""Archived title writes and crash recovery with no real Codex operations."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from title_manager import Manager, Store

TID = 'archived-fixture'
OLD = '讨论历史标题'
NEW = 'Codex丨开发丨历史标题整理'


class ProcessGone(BaseException):
    """The original worker cannot execute any more RPCs, even its finalizer."""


class ArchivedRpc:
    def __init__(self):
        self.thread = {'id': TID, 'name': OLD, 'source': 'cli',
                       'path': '/isolated/codex/archived_sessions/history.jsonl'}
        self.calls = []
        self.fail_name = False
        self.fail_archive = False
        self.ignore_archive = False
        self.archive_response_lost = False
        self.unarchive_response_lost = False
        self.crash_after_unarchive = False
        self.dead = False
        self.before_unarchive = None

    @property
    def archived(self):
        return 'archived_sessions' in Path(self.thread['path']).parts

    def call(self, method, params):
        if self.dead:
            raise ProcessGone('worker no longer exists')
        self.calls.append((method, copy.deepcopy(params)))
        if params['threadId'] != TID:
            raise AssertionError('unexpected thread')
        if method == 'thread/read':
            return {'thread': copy.deepcopy(self.thread)}
        if method == 'thread/unarchive':
            if self.before_unarchive:
                self.before_unarchive()
            self.thread['path'] = '/isolated/codex/sessions/history.jsonl'
            if self.crash_after_unarchive:
                self.dead = True
                raise ProcessGone('process died immediately after unarchive')
            if self.unarchive_response_lost:
                raise TimeoutError('unarchive response lost after success')
            return {'thread': copy.deepcopy(self.thread)}
        if method == 'thread/name/set':
            if self.archived:
                raise RuntimeError('no rollout found for archived thread')
            if self.fail_name:
                raise RuntimeError('title write rejected')
            self.thread['name'] = params['name']
            return {}
        if method == 'thread/archive':
            if self.fail_archive:
                raise RuntimeError('archive temporarily failed')
            if not self.ignore_archive:
                self.thread['path'] = '/isolated/codex/archived_sessions/history.jsonl'
            if self.archive_response_lost:
                raise TimeoutError('archive response lost after success')
            return {}
        raise AssertionError(method)

    def mutations(self):
        return [method for method, _ in self.calls if method != 'thread/read']


class ArchivedTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.rpc = ArchivedRpc()
        self.manager = Manager(self.store, self.rpc)

    def check(self, manager=None):
        return (manager or self.manager).check(TID, NEW, '整理历史标题', 'major', '按内容分类命名')

    def test_success_unarchives_sets_name_and_rearchives(self):
        result = self.check()
        self.assertEqual(result['status'], 'updated')
        self.assertEqual(self.rpc.thread['name'], NEW)
        self.assertTrue(self.rpc.archived)
        self.assertEqual(self.rpc.mutations(), ['thread/unarchive', 'thread/name/set', 'thread/archive'])
        state = self.store.load(TID)
        self.assertNotIn('archive_restore_required', state)
        self.assertNotIn('inflight', state)
        self.assertEqual(state['original_title'], OLD)
        self.assertEqual(state['history'][-1]['status'], 'applied')

    def test_restore_marker_is_durable_before_unarchive(self):
        def inspect_marker():
            persisted = Store(self.temp.name).load(TID)
            self.assertTrue(persisted['archive_restore_required'])
            self.assertEqual(persisted['inflight']['old_title'], OLD)
            self.assertEqual(persisted['inflight']['new_title'], NEW)
        self.rpc.before_unarchive = inspect_marker
        self.check()

    def test_title_write_failure_still_rearchives_and_can_retry(self):
        self.rpc.fail_name = True
        with self.assertRaisesRegex(RuntimeError, 'title write rejected'):
            self.check()
        self.assertTrue(self.rpc.archived)
        self.assertEqual(self.rpc.thread['name'], OLD)
        self.assertNotIn('archive_restore_required', self.store.load(TID))
        self.assertNotIn('history', self.store.load(TID))
        self.rpc.fail_name = False
        self.assertEqual(self.check()['status'], 'updated')
        self.assertTrue(self.rpc.archived)

    def test_archive_failure_keeps_marker_and_retry_restores_before_receipt(self):
        self.rpc.fail_archive = True
        with self.assertRaisesRegex(RuntimeError, 'archive temporarily failed'):
            self.check()
        self.assertFalse(self.rpc.archived)
        self.assertEqual(self.rpc.thread['name'], NEW)
        persisted = Store(self.temp.name).load(TID)
        self.assertTrue(persisted['archive_restore_required'])
        self.assertEqual(persisted['inflight']['new_title'], NEW)
        self.assertNotIn('history', persisted)
        self.rpc.fail_archive = False
        self.rpc.calls.clear()
        result = self.check(Manager(Store(self.temp.name), self.rpc))
        self.assertEqual(result['status'], 'kept')
        self.assertTrue(self.rpc.archived)
        self.assertEqual(self.rpc.mutations(), ['thread/archive'])
        recovered = self.store.load(TID)
        self.assertNotIn('archive_restore_required', recovered)
        self.assertNotIn('inflight', recovered)
        self.assertEqual(recovered['history'][-1]['status'], 'recovered')

    def test_process_gone_after_unarchive_recovers_persisted_marker(self):
        self.rpc.crash_after_unarchive = True
        with self.assertRaises(ProcessGone):
            self.check()
        self.assertFalse(self.rpc.archived)
        self.assertEqual(self.rpc.thread['name'], OLD)
        self.assertTrue(Store(self.temp.name).load(TID)['archive_restore_required'])
        self.rpc.dead = False
        self.rpc.crash_after_unarchive = False
        self.rpc.calls.clear()
        self.assertEqual(self.check(Manager(Store(self.temp.name), self.rpc))['status'], 'updated')
        self.assertEqual(self.rpc.mutations(), ['thread/archive', 'thread/unarchive',
                                              'thread/name/set', 'thread/archive'])
        self.assertTrue(self.rpc.archived)
        self.assertNotIn('archive_restore_required', self.store.load(TID))

    def test_restore_original_title_preserves_archive(self):
        self.check()
        self.rpc.calls.clear()
        result = self.manager.restore(TID, expected_title=NEW, original_override=OLD)
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.rpc.thread['name'], OLD)
        self.assertTrue(self.rpc.archived)
        self.assertEqual(self.rpc.mutations(), ['thread/unarchive', 'thread/name/set', 'thread/archive'])
        self.assertTrue(self.store.load(TID)['locked'])

    def test_archive_noop_is_not_marked_as_success(self):
        self.rpc.ignore_archive = True
        with self.assertRaisesRegex(RuntimeError, '尚未确认恢复归档'):
            self.check()
        self.assertTrue(self.store.load(TID)['archive_restore_required'])
        self.assertNotIn('history', self.store.load(TID))
        self.rpc.ignore_archive = False
        self.check()
        self.assertTrue(self.rpc.archived)

    def test_lost_archive_response_recovers_without_duplicate_archive(self):
        self.rpc.archive_response_lost = True
        with self.assertRaises(TimeoutError):
            self.check()
        self.assertTrue(self.rpc.archived)
        self.assertTrue(self.store.load(TID)['archive_restore_required'])
        self.rpc.archive_response_lost = False
        self.rpc.calls.clear()
        self.assertEqual(self.check()['status'], 'kept')
        self.assertEqual(self.rpc.mutations(), [])
        self.assertNotIn('archive_restore_required', self.store.load(TID))

    def test_lost_unarchive_response_still_restores_archive(self):
        self.rpc.unarchive_response_lost = True
        with self.assertRaises(TimeoutError):
            self.check()
        self.assertTrue(self.rpc.archived)
        self.assertEqual(self.rpc.thread['name'], OLD)
        self.assertEqual(self.rpc.mutations(), ['thread/unarchive', 'thread/archive'])


if __name__ == '__main__':
    unittest.main()
