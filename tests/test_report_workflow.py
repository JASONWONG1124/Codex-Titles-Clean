"""End-to-end report workflow using temporary state and fictional RPC only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import history_backfill
import install_plugin
import title_manager
import title_report
from test_backfill import FakeRPC


class ReportWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / 'private fictional state'
        self.env = patch.dict(os.environ, {'CODEX_TITLES_CLEAN_STATE_DIR': str(self.state_dir)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.rpc = FakeRPC(3)
        self.store = title_manager.Store()
        self.migration = history_backfill.Migration(self.store)
        self.model_calls = []

    def plan(self):
        return [{'thread_id': tid, 'expected_title': row['name'],
                 'title': f'PPT丨创作丨虚构主题{tid}', 'focus': '制作测试PPT', 'reason': '虚构用户任务要求创建PPT'}
                for tid, row in self.rpc.threads.items()]

    def model(self, migration, batch, runner):
        self.model_calls.append([row['thread_id'] for row in batch])
        return [{'thread_id': row['thread_id'], 'title': f"PPT丨创作丨虚构主题{row['thread_id']}",
                 'focus': '制作测试PPT', 'reason': '虚构用户任务要求创建PPT',
                 'expected_title': row['expected_title']} for row in batch]

    def history_cli(self, args, forbid_rpc=False, model_error=None):
        rpc_patch = patch.object(history_backfill, 'AppServer',
                                 side_effect=AssertionError('report/completed run must not open RPC')) if forbid_rpc else \
                    patch.object(history_backfill, 'AppServer', return_value=contextlib.nullcontext(self.rpc))
        effect = AssertionError('report/completed run must not invoke a model') if forbid_rpc else (model_error or self.model)
        with rpc_patch, patch.object(history_backfill.Migration, 'model_plan', autospec=True, side_effect=effect), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = history_backfill.main(args)
        decoder = json.JSONDecoder()
        output, offset, result = stdout.getvalue(), 0, None
        while offset < len(output):
            while offset < len(output) and output[offset].isspace():
                offset += 1
            if offset >= len(output):
                break
            result, offset = decoder.raw_decode(output, offset)
        return code, result

    def sheet_cells(self, path):
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'s': title_report.NS}
        return root, {cell.get('r'): ''.join(cell.itertext()) for cell in root.findall('.//s:c', ns)}

    def prepare_completed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.migration.export(self.rpc, progress=False)
            self.migration.apply(self.rpc, self.plan(), progress=False)
        self.assertEqual(self.migration.read('state')['status'], 'completed')

    def test_first_automatic_run_finishes_with_complete_excel(self):
        code, result = self.history_cli(['run', '--automatic'])
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['report']['status'], 'written')
        self.assertEqual(result['report']['row_count'], 3)
        path = Path(result['report']['path'])
        self.assertTrue(path.is_relative_to(self.state_dir))
        root, cells = self.sheet_cells(path)
        self.assertEqual(cells['A1'], '对话ID')
        self.assertEqual(cells['B2'], '旧标题0')
        self.assertEqual(cells['C2'], 'PPT丨创作丨虚构主题t0')
        self.assertEqual(len(root.findall(f'{{{title_report.NS}}}sheetData/{{{title_report.NS}}}row')), 4)
        self.assertEqual(len(self.rpc.writes), 3)
        self.assertEqual(len(self.model_calls), 1)

    def test_partial_model_error_still_reports_every_row_without_claiming_candidates(self):
        code, result = self.history_cli(['run', '--automatic'], model_error=RuntimeError('fake model quota failure'))
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['counts'], {'error': 3})
        self.assertEqual(result['report']['status'], 'written')
        self.assertEqual(result['report']['row_count'], 3)
        _, cells = self.sheet_cells(result['report']['path'])
        self.assertEqual([cells[f'A{i}'] for i in (2, 3, 4)], ['t0', 't1', 't2'])
        self.assertTrue(all(cells[f'C{i}'] == '' for i in (2, 3, 4)))
        self.assertEqual(self.rpc.writes, [])

    def test_completed_run_generates_missing_report_without_rpc_model_or_title_write(self):
        self.prepare_completed()
        self.assertIsNone(self.migration.read('state').get('report'))
        calls, writes = len(self.rpc.calls), len(self.rpc.writes)
        code, result = self.history_cli(['run', '--automatic'], forbid_rpc=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['report']['row_count'], 3)
        self.assertTrue(Path(result['report']['path']).is_file())
        self.assertEqual((len(self.rpc.calls), len(self.rpc.writes)), (calls, writes))

    def test_report_command_is_offline_and_accepts_output_path(self):
        self.prepare_completed()
        target = self.root / 'custom report' / 'result.xlsx'
        calls = len(self.rpc.calls)
        code, result = self.history_cli(['report', '--output', str(target)], forbid_rpc=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['report']['path'], str(target))
        self.assertTrue(target.is_file())
        self.assertEqual(len(self.rpc.calls), calls)

    def test_report_write_failure_returns_two_keeps_migration_and_offline_retry_succeeds(self):
        with patch.object(history_backfill, 'write_report', side_effect=OSError('fake disk full')):
            code, result = self.history_cli(['run', '--automatic'])
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['report']['status'], 'error')
        self.assertEqual(self.migration.read('state')['status'], 'completed')
        writes, calls = len(self.rpc.writes), len(self.rpc.calls)
        retry_code, retry_result = self.history_cli(['report'], forbid_rpc=True)
        self.assertEqual(retry_code, 0)
        self.assertEqual(retry_result['report']['status'], 'written')
        self.assertEqual(self.migration.read('state')['status'], 'completed')
        self.assertEqual((len(self.rpc.writes), len(self.rpc.calls)), (writes, calls))

    def test_report_error_state_save_failure_does_not_hide_completed_title_results(self):
        original_write = history_backfill.Migration.write
        def failing_error_save(migration, name, value):
            if name == 'state' and value.get('report', {}).get('status') == 'error':
                raise OSError('cannot save report-error metadata')
            return original_write(migration, name, value)
        with patch.object(history_backfill, 'write_report', side_effect=OSError('workbook output failure')), \
                patch.object(history_backfill.Migration, 'write', autospec=True, side_effect=failing_error_save):
            code, result = self.history_cli(['run', '--automatic'])
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['counts'], {'updated': 3})
        self.assertEqual(result['report']['status'], 'error')
        self.assertIn('workbook output failure', result['report']['message'])
        self.assertEqual(self.migration.read('state')['status'], 'completed')
        self.assertEqual(len(self.rpc.writes), 3)

    def batch_cli(self, entries, apply=False):
        source = self.root / 'fictional-plan.json'
        source.write_text(json.dumps(entries), encoding='utf-8')
        argv = ['title_manager.py', 'batch', '--plan', str(source)] + (['--apply'] if apply else [])
        with patch.object(sys, 'argv', argv), \
                patch.object(title_manager, 'AppServer', return_value=contextlib.nullcontext(self.rpc)), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = title_manager.main()
        return code, json.loads(stdout.getvalue())

    def test_batch_apply_reports_success_error_conflict_and_retryable_source(self):
        entries = self.plan()
        self.rpc.fail_write_once.add('t1')
        self.rpc.threads['t2']['name'] = '虚构手动改名'
        code, result = self.batch_cli(entries, apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['counts'], {'updated': 1, 'error': 1, 'conflict': 1})
        generated = result['report']
        self.assertEqual(generated['status'], 'written')
        self.assertEqual(generated['row_count'], 3)
        source = json.loads(Path(generated['source_path']).read_text())
        self.assertEqual(len(source['rows']), 3)
        self.assertEqual([row['thread_id'] for row in source['rows']], ['t0', 't1', 't2'])
        _, cells = self.sheet_cells(generated['path'])
        self.assertEqual(cells['C2'], entries[0]['title'])
        self.assertEqual(cells['C3'], '')
        self.assertEqual(cells['C4'], '')
        self.assertEqual(cells['B4'], '旧标题2')

    def test_batch_preview_does_not_generate_report_or_modify_title(self):
        code, result = self.batch_cli(self.plan(), apply=False)
        self.assertEqual(code, 0)
        self.assertTrue(all(row['status'] == 'preview' for row in result))
        self.assertEqual(self.rpc.writes, [])
        self.assertFalse((self.state_dir / 'reports').exists())

    def test_batch_report_failure_retains_source_for_standalone_reexport(self):
        with patch.object(title_report, 'write_report', side_effect=OSError('fake writer failure')):
            code, result = self.batch_cli(self.plan(), apply=True)
        self.assertEqual(code, 2)
        self.assertEqual(result['counts'], {'updated': 3})
        self.assertEqual(result['report']['status'], 'error')
        source = Path(result['report']['source_path'])
        self.assertTrue(source.is_file())
        calls, writes = len(self.rpc.calls), len(self.rpc.writes)
        with contextlib.redirect_stdout(io.StringIO()):
            retry_code = title_report.main(['--source', str(source), '--output', str(source.with_suffix('.xlsx'))])
        self.assertEqual(retry_code, 0)
        self.assertEqual((len(self.rpc.calls), len(self.rpc.writes)), (calls, writes))

    def test_batch_report_directory_failure_keeps_title_results_and_existing_file(self):
        obstruction = self.state_dir / 'reports'
        obstruction.write_text('fictional existing user file')
        code, result = self.batch_cli(self.plan(), apply=True)
        self.assertEqual(code, 2)
        self.assertEqual(result['counts'], {'updated': 3})
        self.assertEqual(result['report']['status'], 'error')
        self.assertNotIn('source_path', result['report'])
        self.assertEqual(obstruction.read_text(), 'fictional existing user file')
        self.assertEqual(len(result['results']), 3)
        self.assertEqual(len(self.rpc.writes), 3)

    def test_installer_rejects_missing_report_module_before_installation(self):
        package = self.root / 'incomplete-fictional-plugin'
        files = ['.codex-plugin/plugin.json', 'hooks/hooks.json', 'scripts/title_hook.py',
                 'scripts/title_manager.py', 'scripts/app_server.py', 'scripts/history_backfill.py',
                 'skills/codex-titles-clean/SKILL.md', 'scripts/vendor/create_basic_plugin.py',
                 'scripts/vendor/identifier_validation.py', 'scripts/vendor/read_marketplace_name.py']
        for name in files:
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'title_report.py'):
            install_plugin.validate_package(package)


if __name__ == '__main__':
    unittest.main()
