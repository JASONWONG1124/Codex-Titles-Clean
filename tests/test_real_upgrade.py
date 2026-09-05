"""Opt-in real CLI regression; never uses the user's history or model account.

Set CODEX_TITLES_CLEAN_INTEGRATION_CODEX to a Codex CLI executable to run.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from install_plugin import install_cached_plugin, load_helper


@unittest.skipUnless(os.environ.get('CODEX_TITLES_CLEAN_INTEGRATION_CODEX'),
                     'explicit Codex CLI path required for isolated integration test')
class RealUpgradeTests(unittest.TestCase):
    def test_upgrade_preserves_a_live_hook_and_missing_runtime_fails_open(self):
        cli = os.environ['CODEX_TITLES_CLEAN_INTEGRATION_CODEX']
        with tempfile.TemporaryDirectory(prefix='codex-title-upgrade-') as temporary:
            isolated_home = Path(temporary) / 'home with spaces'
            (isolated_home / '.codex').mkdir(parents=True)
            package = isolated_home / 'plugins/codex-titles-clean'
            ignore = shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '.DS_Store')
            shutil.copytree(ROOT, package, ignore=ignore)
            manifest = package / '.codex-plugin/plugin.json'
            data = json.loads(manifest.read_text())
            current_version = data['version']
            data['version'] = '0.0.0-upgrade-fixture'
            manifest.write_text(json.dumps(data))
            load_helper(package).update_marketplace_json(
                isolated_home / '.agents/plugins/marketplace.json', None,
                'codex-titles-clean', 'AVAILABLE', 'ON_INSTALL', 'Productivity', False)
            env = {**os.environ, 'HOME': str(isolated_home),
                   'CODEX_HOME': str(isolated_home / '.codex'),
                   'CODEX_TITLES_CLEAN_STATE_DIR': str(isolated_home / 'title-state')}
            def run(args, **kwargs):
                return subprocess.run(args, cwd=isolated_home, capture_output=True,
                                      text=True, **kwargs)
            selector = 'codex-titles-clean@personal'
            run([cli, 'plugin', 'add', selector], env=env, check=True, timeout=60)
            cache = isolated_home / '.codex/plugins/cache/personal/codex-titles-clean'
            previous = cache / data['version']
            old_entrypoint = previous / 'scripts/title_hook.py'
            self.assertTrue(old_entrypoint.exists())
            saved = isolated_home / 'saved-runtime'
            shutil.copytree(previous, saved)
            shutil.copytree(ROOT, package, dirs_exist_ok=True, ignore=ignore)
            # First demonstrate what an unprotected CLI upgrade does.
            run([cli, 'plugin', 'add', selector], env=env, check=True, timeout=60)
            broken = run([sys.executable, str(old_entrypoint)], input='{}', env=env, timeout=5)
            self.assertEqual(broken.returncode, 2)
            shutil.copytree(saved, previous)
            install_cached_plugin(cli, selector, package, isolated_home, env, run)
            restored = run([sys.executable, str(old_entrypoint)], input='{}', env=env, timeout=5)
            self.assertEqual(restored.returncode, 0)
            self.assertEqual(json.loads(restored.stdout), {})
            installed = cache / current_version
            command = json.loads((installed / 'hooks/hooks.json').read_text())['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
            missing = run(['/bin/sh', '-c', command.replace('${PLUGIN_ROOT}', str(isolated_home / 'missing'))],
                          input='{}', env=env, timeout=5)
            self.assertEqual(missing.returncode, 0)
            self.assertEqual(json.loads(missing.stdout), {})
            payload = json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'fixture', 'turn_id': 'turn'})
            normal = run(['/bin/sh', '-c', command.replace('${PLUGIN_ROOT}', str(installed))],
                         input=payload, env=env, timeout=5)
            self.assertEqual(normal.returncode, 0)
            self.assertIn('--thread-id fixture', json.loads(normal.stdout)['hookSpecificOutput']['additionalContext'])


if __name__ == '__main__':
    unittest.main()
