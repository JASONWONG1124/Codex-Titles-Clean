"""Rename compatibility with fictional home directories and no real Codex RPC."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import app_server
import history_backfill
import install_plugin
import title_hook
from title_manager import Store


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'fictional home'
        self.home.mkdir()
        self.scope = patch.dict(os.environ, {'HOME': str(self.home)}, clear=True)
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.primary = self.home / '.codex/codex-titles-clean'
        self.legacy = self.home / '.codex/sidebar-titles'

    def executable(self, name):
        path = self.home / name
        path.write_text('# fixture; never executed\n')
        path.chmod(0o700)
        return path

    def test_fresh_install_uses_new_default_state_directory(self):
        self.assertEqual(Store().root, self.primary)
        self.assertTrue(self.primary.is_dir())
        self.assertFalse(self.legacy.exists())

    def test_existing_legacy_directory_is_reused_without_copying_or_reset(self):
        old = Store(self.legacy)
        old.save('fictional-task', {'original_title': '改名前的真实原名快照', 'last_title': 'PPT丨创作丨虚构主题'})
        before = old.path('fictional-task').read_bytes()
        current = Store()
        self.assertEqual(current.root, self.legacy)
        self.assertEqual(current.load('fictional-task')['original_title'], '改名前的真实原名快照')
        self.assertEqual(old.path('fictional-task').read_bytes(), before)
        self.assertFalse(self.primary.exists())

    def test_existing_new_directory_takes_precedence_without_touching_legacy(self):
        Store(self.legacy).save('fixture', {'original_title': '旧目录备份'})
        Store(self.primary).save('fixture', {'original_title': '新目录备份'})
        self.assertEqual(Store().root, self.primary)
        self.assertEqual(Store().load('fixture')['original_title'], '新目录备份')
        self.assertEqual(Store(self.legacy).load('fixture')['original_title'], '旧目录备份')

    def test_new_state_environment_variable_takes_precedence_over_legacy(self):
        modern, legacy = self.home / 'modern override', self.home / 'legacy override'
        os.environ.update(CODEX_TITLES_CLEAN_STATE_DIR=str(modern), SIDEBAR_TITLES_STATE_DIR=str(legacy))
        self.assertEqual(Store().root, modern)
        self.assertFalse(legacy.exists())

    def test_legacy_state_environment_variable_remains_supported(self):
        old_override = self.home / 'old explicit state'
        os.environ['SIDEBAR_TITLES_STATE_DIR'] = str(old_override)
        self.assertEqual(Store().root, old_override)
        self.assertFalse(self.primary.exists())

    def test_explicit_root_wins_over_both_environment_variables(self):
        os.environ.update(CODEX_TITLES_CLEAN_STATE_DIR=str(self.home / 'new-env'),
                          SIDEBAR_TITLES_STATE_DIR=str(self.home / 'old-env'))
        explicit = self.home / 'explicit root'
        self.assertEqual(Store(explicit).root, explicit)
        self.assertFalse((self.home / 'new-env').exists())
        self.assertFalse((self.home / 'old-env').exists())

    def test_state_paths_expand_tilde(self):
        os.environ['CODEX_TITLES_CLEAN_STATE_DIR'] = '~/nested/state'
        self.assertEqual(Store().root, self.home / 'nested/state')

    def test_custom_codex_home_uses_same_legacy_fallback(self):
        custom = self.home / 'custom codex'
        old = custom / 'sidebar-titles'
        Store(old).save('fixture', {'original_title': '自定义CODEX_HOME旧记录'})
        os.environ['CODEX_HOME'] = str(custom)
        self.assertEqual(Store().root, old)
        self.assertFalse((custom / 'codex-titles-clean').exists())

    def test_legacy_completed_migration_does_not_call_rpc_or_model(self):
        old = Store(self.legacy)
        migration = history_backfill.Migration(old)
        migration.write('state', {'schema_version': 1, 'snapshot_ready': True,
                                 'status': 'completed', 'entries': {}})
        before = (migration.files.root / 'state.json').read_bytes()
        class NoRPC:
            def call(self, *_):
                raise AssertionError('completed legacy migration must not call RPC')
        current = history_backfill.Migration()
        with patch.object(current, 'model_plan', side_effect=AssertionError('must not invoke a model')):
            result = current.run(NoRPC())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(current.store.root, self.legacy)
        self.assertEqual((migration.files.root / 'state.json').read_bytes(), before)
        self.assertFalse(self.primary.exists())

    def test_legacy_completed_migration_prevents_new_hook_onboarding(self):
        store = Store(self.legacy)
        migration = history_backfill.Migration(store)
        migration.write('state', {'status': 'completed'})
        result = title_hook.handle({'hook_event_name': 'UserPromptSubmit',
                                   'session_id': 'fixture', 'turn_id': 'turn-1', 'prompt': '继续讨论'}, Store())
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Codex-Titles-Clean 已启用', context)
        self.assertNotIn('history_backfill.py', context)

    def test_legacy_reminder_continuation_does_not_create_a_second_turn(self):
        store = Store()
        title_hook.handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'fixture',
                           'turn_id': 'original-turn', 'prompt': '测试主题'}, store)
        title_hook.handle({'hook_event_name': 'Stop', 'session_id': 'fixture',
                           'turn_id': 'original-turn'}, store)
        title_hook.handle({'hook_event_name': 'UserPromptSubmit', 'session_id': 'fixture',
                           'turn_id': 'continuation-turn',
                           'prompt': title_hook.LEGACY_REMINDER + '\n旧版本最后一次提醒'}, store)
        pending = store.load('fixture')['pending']
        self.assertEqual(pending['id'], 'original-turn')
        self.assertTrue(pending['nudged'])

    def test_new_binary_override_wins_in_runtime_and_installer(self):
        modern, legacy = self.executable('modern-codex'), self.executable('legacy-codex')
        os.environ.update(CODEX_TITLES_CLEAN_CODEX=str(modern), SIDEBAR_TITLES_CODEX=str(legacy))
        self.assertEqual(Path(app_server.codex_binary()), modern)
        self.assertEqual(Path(install_plugin.codex_binary(self.home)), modern.resolve())

    def test_legacy_binary_override_works_in_runtime_and_installer(self):
        legacy = self.executable('legacy-codex')
        os.environ['SIDEBAR_TITLES_CODEX'] = str(legacy)
        self.assertEqual(Path(app_server.codex_binary()), legacy)
        self.assertEqual(Path(install_plugin.codex_binary(self.home)), legacy.resolve())

    def test_either_worker_environment_variable_disables_hooks(self):
        for flag in ('CODEX_TITLES_CLEAN_BACKFILL_WORKER', 'SIDEBAR_TITLES_BACKFILL_WORKER'):
            with self.subTest(flag=flag), patch.dict(os.environ, {flag: '1'}):
                for event in ('UserPromptSubmit', 'Stop'):
                    self.assertEqual(title_hook.handle({'hook_event_name': event, 'session_id': 'worker'}, Store()), {})

    def test_model_worker_sets_new_and_legacy_recursion_guards(self):
        migration = history_backfill.Migration(Store())
        migration.write('originals', {'fixture': {'name': '原始标题'}})
        batch = [{'thread_id': 'fixture', 'expected_title': '原始标题', 'user_excerpts': ['制作PPT']}]
        captured = {}
        def runner(args, **kwargs):
            captured.update(kwargs['env'])
            output = Path(args[args.index('--output-last-message') + 1])
            output.write_text(json.dumps({'entries': [{'thread_id': 'fixture',
                'title': 'PPT丨创作丨虚构主题', 'focus': '制作PPT', 'reason': '虚构内容依据'}]}))
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        with patch.object(history_backfill, 'codex_binary', return_value='/fictional/codex'):
            migration.model_plan(batch, runner)
        self.assertEqual(captured['CODEX_TITLES_CLEAN_BACKFILL_WORKER'], '1')
        self.assertEqual(captured['SIDEBAR_TITLES_BACKFILL_WORKER'], '1')


if __name__ == '__main__':
    unittest.main()
