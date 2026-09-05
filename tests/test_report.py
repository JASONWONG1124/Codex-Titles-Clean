"""Fictional OOXML fixtures only; no Codex history or user report is read."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import title_report as report

NS = {'s': report.NS}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'private reports' / '虚构标题记录.xlsx'

    def sample(self, **overrides):
        row = {'thread_id': '000001-fictional-id', 'original_title': '原始标题：用户感受',
               'optimized_title': 'PPT丨创作丨用户感受', 'status': 'updated',
               'archived': False, 'note': '根据实际对话内容命名。'}
        row.update(overrides)
        return row

    def sheet(self):
        with zipfile.ZipFile(self.output) as archive:
            return ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))

    def cells(self):
        return {cell.get('r'): ''.join(cell.itertext()) for cell in self.sheet().findall('.//s:c', NS)}

    def test_first_three_columns_are_id_original_optimized(self):
        result = report.write_report(self.output, [self.sample()])
        self.assertEqual(result['row_count'], 1)
        cells = self.cells()
        self.assertEqual([cells['A1'], cells['B1'], cells['C1']], ['对话ID', '原标题', '优化后标题'])
        self.assertEqual(cells['A2'], '000001-fictional-id')
        self.assertEqual(cells['B2'], '原始标题：用户感受')
        self.assertEqual(cells['C2'], 'PPT丨创作丨用户感受')

    def test_all_cells_are_inline_text_including_formula_prefixes(self):
        danger = ['=HYPERLINK("https://example.invalid")', '+SUM(1,2)', '-12', '@import', '00123456789012345678']
        rows = [self.sample(thread_id=value, original_title=value, optimized_title=value,
                            note=value) for value in danger]
        report.write_report(self.output, rows)
        root = self.sheet()
        self.assertFalse(root.findall('.//s:f', NS))
        self.assertTrue(all(cell.get('t') == 'inlineStr' for cell in root.findall('.//s:c', NS)))
        cells = self.cells()
        for index, value in enumerate(danger, 2):
            self.assertEqual(cells[f'A{index}'], value)
            self.assertEqual(cells[f'C{index}'], value)

    def test_unicode_xml_characters_and_illegal_controls(self):
        text = '中文😀 <tag>&"\n下一行\t保留\x00非法\ud800代理'
        result = report.write_report(self.output, [self.sample(original_title=text)])
        self.assertEqual(result['replaced_invalid_characters'], 2)
        self.assertEqual(self.cells()['B2'], text.replace('\x00', '\uFFFD').replace('\ud800', '\uFFFD'))
        with zipfile.ZipFile(self.output) as archive:
            for name in archive.namelist():
                ET.fromstring(archive.read(name))

    def test_literal_ooxml_escape_token_is_not_treated_as_control(self):
        report.write_report(self.output, [self.sample(original_title='literal _x000A_ text')])
        self.assertEqual(self.cells()['B2'], 'literal _x005F_x000A_ text')

    def test_formats_freeze_filter_widths_wrap_and_alternating_rows(self):
        report.write_report(self.output, [self.sample(), self.sample(thread_id='second', note='长中文说明' * 20)])
        root = self.sheet()
        pane = root.find('.//s:pane', NS)
        self.assertEqual(pane.get('state'), 'frozen')
        self.assertEqual(pane.get('ySplit'), '1')
        self.assertEqual(root.find('s:autoFilter', NS).get('ref'), 'A1:F3')
        self.assertEqual(len(root.findall('s:cols/s:col', NS)), 6)
        rows = root.findall('s:sheetData/s:row', NS)
        self.assertNotEqual(rows[1][0].get('s'), rows[2][0].get('s'))
        self.assertGreater(float(rows[2].get('ht')), float(rows[1].get('ht')))
        with zipfile.ZipFile(self.output) as archive:
            styles = ET.fromstring(archive.read('xl/styles.xml'))
        for xf in styles.findall('s:cellXfs/s:xf', NS):
            self.assertEqual(xf.get('numFmtId'), '49')
        self.assertEqual(styles.find('s:cellXfs', NS)[2].find('s:alignment', NS).get('wrapText'), '1')

    def test_unknown_archive_status_is_not_reported_as_unarchived(self):
        report.write_report(self.output, [self.sample(archived=None), self.sample(archived=True), self.sample(archived=False)])
        self.assertEqual([self.cells()[f'E{i}'] for i in (2, 3, 4)], ['未知', '已归档', '未归档'])

    def test_all_inventory_states_are_retained_and_proposals_never_claimed_as_written(self):
        statuses = ['updated', 'kept', 'pending', 'skipped', 'error', 'conflict', 'locked', 'restored']
        inventory = [{'id': status, 'name': '快照原名' + status, 'archived': index % 2 == 0}
                     for index, status in enumerate(statuses)]
        originals = {status: {'name': '本次快照' + status} for status in statuses}
        state = {'entries': {status: {'status': status, 'title': '结果' + status, 'reason': '状态说明'}
                             for status in statuses}}
        state['entries']['restored']['restore_result'] = {'title': '本次快照restored'}
        proposed = {status: {'title': '候选' + status} for status in statuses}
        rows = report.build_report_rows(inventory, originals, state, proposed)
        self.assertEqual(len(rows), len(statuses))
        by_id = {row['thread_id']: row for row in rows}
        for status in ('error', 'conflict', 'pending', 'locked', 'skipped'):
            self.assertEqual(by_id[status]['optimized_title'], '')
            self.assertIn('候选标题未当作', by_id[status]['note'])
        self.assertEqual(by_id['updated']['original_title'], '本次快照updated')
        self.assertEqual(by_id['updated']['optimized_title'], '结果updated')
        self.assertEqual(by_id['restored']['optimized_title'], '本次快照restored')
        report.write_report(self.output, rows)
        self.assertEqual(len(self.sheet().findall('s:sheetData/s:row', NS)), 9)

    def test_null_original_uses_display_fallback_and_explanation(self):
        inventory = [{'id': 'null', 'name': None, 'preview': '库存回退标题', 'archived': True}]
        originals = {'null': {'name': None, 'display_title': '当时可见的首句'}}
        rows = report.build_report_rows(inventory, originals, {'entries': {'null': {'status': 'updated', 'title': '新标题'}}})
        self.assertEqual(rows[0]['original_title'], '当时可见的首句')
        self.assertIn('没有自定义标题', rows[0]['note'])
        originals['null'] = {'name': 'API返回的可见标题', 'source_name': None}
        rows = report.build_report_rows(inventory, originals, {})
        self.assertEqual(rows[0]['original_title'], 'API返回的可见标题')
        self.assertIn('没有自定义标题', rows[0]['note'])

    def test_snapshot_is_used_instead_of_earliest_manager_title(self):
        rows = report.build_report_rows([{'id': 'one', 'name': '当前库存名'}],
                                       {'one': {'name': '本次迁移前标题'}},
                                       {'original_title': '更早的插件标题', 'entries': {
                                           'one': {'status': 'updated', 'title': '已写入标题'}}})
        self.assertEqual(rows[0]['original_title'], '本次迁移前标题')

    def test_missing_inventory_rows_are_appended_from_originals_and_results(self):
        rows = report.build_report_rows([{'id': 'one', 'name': '一'}], {'two': {'name': '二'}},
                                       {'entries': {'three': {'status': 'error', 'old_title': '三'}}})
        self.assertEqual([row['thread_id'] for row in rows], ['one', 'two', 'three'])
        self.assertEqual(rows[2]['original_title'], '三')

    def test_writer_also_refuses_failed_result_titles(self):
        report.write_report(self.output, [self.sample(status='error', optimized_title='未确认候选'),
                                         self.sample(status='conflict', optimized_title='未确认候选')])
        self.assertEqual(self.cells()['C2'], '')
        self.assertEqual(self.cells()['C3'], '')

    def test_locked_and_skipped_readback_titles_are_not_optimization_results(self):
        report.write_report(self.output, [self.sample(status='locked', optimized_title='手动锁定的标题'),
                                         self.sample(status='skipped', optimized_title='读取到但未处理的标题')])
        self.assertEqual(self.cells()['C2'], '')
        self.assertEqual(self.cells()['C3'], '')

    def test_package_and_app_properties_use_default_namespaces_for_importers(self):
        report.write_report(self.output, [self.sample()])
        with zipfile.ZipFile(self.output) as archive:
            expected = {'[Content_Types].xml': report.CONTENT, '_rels/.rels': report.PKGREL,
                        'xl/_rels/workbook.xml.rels': report.PKGREL, 'docProps/app.xml': report.APP}
            for name, namespace in expected.items():
                data = archive.read(name)
                self.assertNotIn(b'<ns0:', data)
                self.assertIn(('xmlns="' + namespace + '"').encode(), data)
                self.assertTrue(ET.fromstring(data).tag.startswith('{' + namespace + '}'))

    def test_32767_character_limit_is_explicit_and_preserves_previous_file(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'previous workbook')
        with self.assertRaisesRegex(ValueError, '32767'):
            report.write_report(self.output, [self.sample(original_title='字' * 32768)])
        self.assertEqual(self.output.read_bytes(), b'previous workbook')
        self.assertEqual(list(self.output.parent.glob('.title-report-*')), [])
        report.write_report(self.output, [self.sample(original_title='字' * 32767)])
        self.assertEqual(len(self.cells()['B2']), 32767)

    def test_zip_write_failure_keeps_old_output_and_removes_temp(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b'original file remains')
        with patch.object(report.zipfile.ZipFile, 'writestr', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                report.write_report(self.output, [self.sample()])
        self.assertEqual(self.output.read_bytes(), b'original file remains')
        self.assertEqual(list(self.output.parent.glob('.title-report-*')), [])

    def test_private_file_permissions_and_metadata_timestamp(self):
        report.write_report(self.output, [self.sample()], {'title': '虚构数据',
                            'created_at': '2026-09-05T15:23:01+08:00', 'description': '仅测试'})
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        with zipfile.ZipFile(self.output) as archive:
            core = ET.fromstring(archive.read('docProps/core.xml'))
            self.assertEqual(core.find(f'{{{report.DCTERMS}}}created').text, '2026-09-05T07:23:01Z')
            self.assertEqual(core.find(f'{{{report.DC}}}title').text, '虚构数据')
            self.assertEqual(set(archive.namelist()), {'[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml',
                'xl/_rels/workbook.xml.rels', 'xl/styles.xml', 'xl/worksheets/sheet1.xml',
                'docProps/core.xml', 'docProps/app.xml'})

    def test_cli_accepts_wrapped_source_and_plain_array(self):
        source = self.root / 'fixture.json'
        for payload in ([self.sample()], {'rows': [self.sample()], 'metadata': {'title': '虚构CLI样例'}}):
            source.write_text(json.dumps(payload), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = report.main(['--source', str(source), '--output', str(self.output)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())['row_count'], 1)
            self.assertTrue(self.output.is_file())

    def test_empty_report_has_headers_without_fabricated_rows(self):
        result = report.write_report(self.output, [])
        self.assertEqual(result['row_count'], 0)
        self.assertEqual(self.sheet().find('s:dimension', NS).get('ref'), 'A1:F1')
        self.assertEqual(len(self.sheet().findall('s:sheetData/s:row', NS)), 1)


if __name__ == '__main__':
    unittest.main()
