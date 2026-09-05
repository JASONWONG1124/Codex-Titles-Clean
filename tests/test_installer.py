"""Installer integration tests: temporary HOME and fake Codex only."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('install_plugin', ROOT / 'scripts/install_plugin.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home with spaces'
        self.home.mkdir()
        self.package = self.root / 'shared $(touch NEVER) plugin'
        self.package.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'HOME': str(self.home)}).start()
        patch.object(installer, 'codex_binary', return_value='/fake Codex/codex').start()
        for path, value in {
            '.codex-plugin/plugin.json': {'name': 'codex-titles-clean', 'version': '0.2.0'},
            'hooks/hooks.json': {'hooks': {'Stop': [{'hooks': [
                {'type': 'command', 'command': 'python3 "${PLUGIN_ROOT}/scripts/title_hook.py"'}]}]}},
        }.items():
            dest = self.package / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(value))
        for relative in ['scripts/title_hook.py', 'scripts/title_manager.py', 'scripts/app_server.py',
                         'scripts/history_backfill.py', 'scripts/title_report.py', 'skills/codex-titles-clean/SKILL.md']:
            dest = self.package / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text('# fixture')
        shutil.copytree(ROOT / 'scripts/vendor', self.package / 'scripts/vendor',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.calls = []
        self.backfill_code = 0
        self.install_code = 0
        self.output = io.StringIO()

    @property
    def marketplace(self):
        return self.home / '.agents/plugins/marketplace.json'

    @property
    def target(self):
        return self.home / 'plugins/codex-titles-clean'

    def runner(self, args, **kwargs):
        self.assertIsInstance(args, list)
        self.assertFalse(kwargs.get('shell'))
        self.calls.append((args, kwargs))
        if 'read_marketplace_name.py' in args[1]:
            return subprocess.run(args, **kwargs)
        if args[1:3] == ['plugin', 'add']:
            if self.install_code:
                raise subprocess.CalledProcessError(self.install_code, args)
            return subprocess.CompletedProcess(args, 0)
        self.assertEqual(args[2:], ['run', '--automatic'])
        return subprocess.CompletedProcess(args, self.backfill_code)

    def run_install(self):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output):
            return installer.install(self.package, self.home, self.runner)

    def seed_marketplace(self, name='my-market', source=None):
        data = {'name': name, 'interface': {'displayName': 'Keep my name'},
                'extra': {'custom': True}, 'plugins': [
                    {'name': 'another-plugin', 'source': {'source': 'git', 'url': 'keep'}}]}
        if source is not None:
            data['plugins'].append({'name': 'codex-titles-clean', 'source': source,
                                    'policy': {'installation': 'AVAILABLE'}, 'custom': 17})
        self.marketplace.parent.mkdir(parents=True)
        self.marketplace.write_text(json.dumps(data, indent=3))
        return data

    def test_fresh_install_copies_plugin_and_runs_entire_history(self):
        self.assertEqual(self.run_install(), 0)
        self.assertTrue((self.target / 'scripts/history_backfill.py').is_file())
        data = json.loads(self.marketplace.read_text())
        self.assertEqual(data['name'], 'personal')
        self.assertEqual(data['plugins'][0]['source'], installer.SOURCE)
        self.assertEqual(self.calls[0][0], ['/fake Codex/codex', 'plugin', 'add',
                                          'codex-titles-clean@personal'])
        self.assertEqual(self.calls[-1][0][-2:], ['run', '--automatic'])
        self.assertEqual(self.calls[-1][1]['env']['CODEX_TITLES_CLEAN_CODEX'], '/fake Codex/codex')
        self.assertEqual(self.calls[-1][1]['env']['SIDEBAR_TITLES_CODEX'], '/fake Codex/codex')
        self.assertFalse((self.home / '.codex/config.toml').exists())
        self.assertIn('首次整理需要使用', self.output.getvalue())
        self.assertIn('不修改全局', self.output.getvalue().replace('不会修改全局', '不修改全局'))

    def test_existing_entry_is_preserved_byte_for_byte(self):
        self.seed_marketplace(source=installer.SOURCE)
        before = self.marketplace.read_bytes()
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(self.marketplace.read_bytes(), before)
        self.assertIn('codex-titles-clean@my-market', self.calls[1][0])

    def test_append_uses_helper_preserving_other_plugins_and_metadata(self):
        before = self.seed_marketplace()
        self.assertEqual(self.run_install(), 0)
        after = json.loads(self.marketplace.read_text())
        self.assertEqual(after['plugins'][0], before['plugins'][0])
        self.assertEqual(after['interface'], before['interface'])
        self.assertEqual(after['extra'], before['extra'])
        backups = list(self.marketplace.parent.glob('marketplace.json.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), before)

    def test_conflicting_source_stops_before_copy_or_cli(self):
        self.seed_marketplace(source={'source': 'local', 'path': './someone-else/codex-titles-clean'})
        before = self.marketplace.read_bytes()
        with self.assertRaisesRegex(ValueError, '不同来源'):
            self.run_install()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.marketplace.read_bytes(), before)
        self.assertEqual(len(self.calls), 1)  # official read-only name validator

    def test_invalid_marketplace_identifier_is_rejected(self):
        self.seed_marketplace(name='bad;touch /tmp/NEVER')
        before = self.marketplace.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install()
        self.assertFalse(self.target.exists())
        self.assertEqual(self.marketplace.read_bytes(), before)

    def test_existing_source_is_backed_up_before_replacement(self):
        shutil.copytree(self.package, self.target)
        (self.target / 'local-edit.txt').write_text('original user data')
        self.assertEqual(self.run_install(), 0)
        backups = list(self.target.parent.glob('codex-titles-clean.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'local-edit.txt').read_text(), 'original user data')
        self.assertFalse((self.target / 'local-edit.txt').exists())

    def test_unrelated_target_is_not_overwritten(self):
        self.target.mkdir(parents=True)
        (self.target / 'mine').write_text('keep')
        with self.assertRaisesRegex(ValueError, '其他文件'):
            self.run_install()
        self.assertEqual((self.target / 'mine').read_text(), 'keep')
        self.assertFalse(self.marketplace.exists())

    def test_backfill_failure_is_reported_as_incomplete_with_retry_command(self):
        self.backfill_code = 3
        self.assertEqual(self.run_install(), 3)
        self.assertIn('历史整理或 Excel 导出未全部完成', self.output.getvalue())
        self.assertIn('继续历史整理：', self.output.getvalue())
        self.assertIn('plugin remove', self.output.getvalue())
        self.assertNotIn('历史整理与 Excel 导出命令已成功返回', self.output.getvalue())

    def test_failed_codex_install_does_not_run_backfill(self):
        self.install_code = 1
        self.assertEqual(self.run_install(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('插件安装未确认成功', self.output.getvalue())

    def test_incomplete_package_has_no_side_effects(self):
        (self.package / 'scripts/history_backfill.py').unlink()
        with self.assertRaisesRegex(ValueError, '不完整'):
            self.run_install()
        self.assertFalse(self.marketplace.parent.exists())
        self.assertFalse(self.target.exists())

    def test_package_symlinks_are_rejected(self):
        (self.package / 'private').symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.run_install()
        self.assertFalse(self.target.exists())

    def test_failed_copy_restores_old_source_without_deletion(self):
        shutil.copytree(self.package, self.target)
        original = (self.target / '.codex-plugin/plugin.json').read_bytes()
        with patch.object(installer.shutil, 'copytree', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.run_install()
        self.assertEqual((self.target / '.codex-plugin/plugin.json').read_bytes(), original)
        self.assertFalse(self.marketplace.exists())


if __name__ == '__main__':
    unittest.main()
