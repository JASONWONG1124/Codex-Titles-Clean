"""Exercise the exact installed shell command, including missing cache files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from title_hook import handle
from title_manager import Store


class HookSafetyTests(unittest.TestCase):
    def run_hook(self, script=None):
        with tempfile.TemporaryDirectory(prefix='title hook cache ') as temporary:
            package = Path(temporary) / 'removed version'
            if script is not None:
                target = package / 'scripts/title_hook.py'
                target.parent.mkdir(parents=True)
                target.write_text(script)
            command = json.loads((ROOT / 'hooks/hooks.json').read_text())['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
            command = command.replace('${PLUGIN_ROOT}', str(package))
            result = subprocess.run(['/bin/sh', '-c', command], input='{}',
                                    text=True, capture_output=True, timeout=6)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            return json.loads(result.stdout)

    def test_removed_version_cannot_block_new_message(self):
        self.assertEqual(self.run_hook(), {})

    def test_child_exit_two_cannot_block_new_message(self):
        self.assertEqual(self.run_hook('import sys; sys.exit(2)'), {})

    def test_import_error_cannot_block_new_message(self):
        self.assertEqual(self.run_hook('import missing_title_dependency'), {})

    def test_invalid_output_is_discarded(self):
        self.assertEqual(self.run_hook('print("broken output")'), {})

    def test_hanging_child_is_bounded_and_cannot_block_message(self):
        self.assertEqual(self.run_hook('import time; time.sleep(30)'), {})

    def test_control_decisions_are_not_forwarded(self):
        self.assertEqual(self.run_hook('print(\'{"decision":"block","reason":"title unavailable"}\')'), {})

    def test_prompt_context_is_preserved(self):
        response = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit',
                                         'additionalContext': 'fictional title guidance'}}
        self.assertEqual(self.run_hook('print(' + repr(json.dumps(response)) + ')'), response)

    def test_legacy_stop_never_blocks_even_when_check_was_missed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            payload = {'session_id': 'fixture', 'turn_id': 'test-turn'}
            handle({**payload, 'hook_event_name': 'UserPromptSubmit'}, store)
            self.assertEqual(handle({**payload, 'hook_event_name': 'Stop'}, store), {})


if __name__ == '__main__':
    unittest.main()
