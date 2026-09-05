import json
import os
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from title_manager import Store
from title_hook import handle, REMINDER


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.tid = '00000000-0000-0000-0000-000000000001'

    def payload(self, event, **extra):
        return dict(hook_event_name=event, session_id=self.tid, turn_id='turn-1', **extra)

    def test_prompt_context_uses_current_thread_and_turn(self):
        out = handle(self.payload('UserPromptSubmit', prompt='做一套课程'), self.store)
        context = out['hookSpecificOutput']['additionalContext']
        self.assertIn('--thread-id ' + self.tid, context)
        self.assertIn('--check-id turn-1', context)
        self.assertIn('分类根据内容决定，可新增', context)
        self.assertNotIn('做一套课程', json.dumps(self.store.load(self.tid), ensure_ascii=False))

    def test_stop_does_not_interrupt_reply_when_check_is_missing(self):
        handle(self.payload('UserPromptSubmit', prompt='test'), self.store)
        out = handle(self.payload('Stop', stop_hook_active=False), self.store)
        self.assertEqual(out, {})
        self.assertFalse(self.store.load(self.tid)['pending']['nudged'])
        self.assertEqual(handle(self.payload('Stop', stop_hook_active=False), self.store), {})

    def test_checked_or_failed_turn_does_not_block(self):
        for result in ('updated', 'kept', 'error'):
            with self.subTest(result=result):
                self.store.save(self.tid, {'pending': {'id': 'turn-1', 'checked': True, 'result': result}})
                self.assertEqual(handle(self.payload('Stop', stop_hook_active=False), self.store), {})

    def test_new_real_input_resets_pending(self):
        self.store.save(self.tid, {'pending': {'id': 'old', 'checked': True, 'nudged': True}})
        handle(self.payload('UserPromptSubmit', prompt='接着实现'), self.store)
        state = self.store.load(self.tid)
        self.assertFalse(state['pending']['checked'])
        self.assertFalse(state['pending']['nudged'])

    def test_locked_task_subagent_and_stale_stop_are_ignored(self):
        self.store.save(self.tid, {'locked': True})
        self.assertEqual(handle(self.payload('UserPromptSubmit'), self.store), {})
        self.assertEqual(handle(self.payload('UserPromptSubmit', agent_id='child'), self.store), {})
        self.store.save(self.tid, {'pending': {'id': 'new-turn'}})
        self.assertEqual(handle(self.payload('Stop', stop_hook_active=False), self.store), {})

    def test_initial_migration_is_requested_once_and_workers_never_recurse(self):
        out = handle(self.payload('UserPromptSubmit'), self.store)
        self.assertIn('history_backfill.py', out['hookSpecificOutput']['additionalContext'])
        folder = self.store.root / 'history-migration'
        folder.mkdir()
        (folder / 'state.json').write_text(json.dumps({'status': 'completed'}))
        out = handle(self.payload('UserPromptSubmit'), self.store)
        self.assertNotIn('history_backfill.py', out['hookSpecificOutput']['additionalContext'])
        with patch.dict(os.environ, {'CODEX_TITLES_CLEAN_BACKFILL_WORKER': '1'}):
            self.assertEqual(handle(self.payload('UserPromptSubmit'), self.store), {})
            self.assertEqual(handle(self.payload('Stop'), self.store), {})

    def test_malformed_input_fails_open(self):
        import os
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/title_hook.py')],
            input='not-json', text=True, capture_output=True,
            env={**os.environ, 'CODEX_TITLES_CLEAN_STATE_DIR': self.tmp.name})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {})


if __name__ == '__main__':
    unittest.main()
