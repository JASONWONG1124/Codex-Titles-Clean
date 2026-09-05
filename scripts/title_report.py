#!/usr/bin/env python3
"""Portable title-change reports using Python's standard library only.

build_report_rows(inventory, originals, state, proposed=None) returns dictionaries
with thread_id, original_title, optimized_title, status, archived, and note.
The first three spreadsheet columns always follow that order. Only confirmed
result titles are shown: failed/pending/conflicting proposals remain blank.

write_report(path, rows, metadata=None) writes one private, atomic .xlsx file.
Metadata supports title, description, creator, and created_at (ISO 8601).
This module never reads Codex state or invokes a model by itself.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import math
import os
import re
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

HEADERS = ('对话ID', '原标题', '优化后标题', '处理结果', '归档状态', '说明')
KEYS = ('thread_id', 'original_title', 'optimized_title', 'status', 'archived', 'note')
WIDTHS = (38, 44, 46, 20, 12, 64)
STATUSES = {'updated': '已更新', 'kept': '保留原名', 'skipped': '已跳过',
            'locked': '已锁定', 'conflict': '有冲突，未覆盖', 'error': '处理失败',
            'pending': '待处理', 'restored': '已恢复', 'stale': '检查已过期', 'preview': '仅预览'}
CONFIRMED = {'updated', 'kept', 'restored'}
SHEET_NAME = '标题整理记录'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CONTENT = 'http://schemas.openxmlformats.org/package/2006/content-types'
CORE = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'
DC = 'http://purl.org/dc/elements/1.1/'
DCTERMS = 'http://purl.org/dc/terms/'
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
APP = 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties'
VT = 'http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes'

ET.register_namespace('', NS)
for prefix, namespace in [('r', REL), ('cp', CORE), ('dc', DC), ('dcterms', DCTERMS),
                           ('xsi', XSI), ('vt', VT)]:
    ET.register_namespace(prefix, namespace)


def _text(value):
    return '' if value is None else str(value)


def _entries(state):
    entries = (state or {}).get('entries', {})
    if isinstance(entries, list):
        return {entry['thread_id']: entry for entry in entries}
    if not isinstance(entries, dict):
        raise ValueError('state.entries 必须是以对话ID为键的对象或结果数组。')
    return entries


def build_report_rows(inventory, originals, state, proposed=None):
    """Join a complete snapshot to actual results; never substitute proposals."""
    originals, entries = originals or {}, _entries(state)
    if isinstance(proposed, list):
        proposed = {row['thread_id']: row for row in proposed}
    elif isinstance(proposed, dict) and isinstance(proposed.get('entries'), list):
        proposed = {row['thread_id']: row for row in proposed['entries']}
    proposed = proposed or {}
    records, order = {}, []
    for row in inventory or []:
        tid = row.get('id', row.get('thread_id'))
        if not isinstance(tid, str) or not tid:
            raise ValueError('库存中的每条记录必须包含文本对话ID。')
        if tid in records:
            raise ValueError(f'库存出现重复对话ID：{tid}')
        records[tid] = row
        order.append(tid)
    for tid in list(originals) + list(entries):
        if tid not in records:
            records[tid] = {}
            order.append(tid)
    rows = []
    for tid in order:
        record, result = records[tid], entries.get(tid, {})
        original = originals.get(tid, {})
        if not isinstance(original, dict):
            original = {'name': original}
        raw_title = original.get('name', record.get('name', result.get('old_title')))
        was_untitled = raw_title is None or ('source_name' in original and original['source_name'] is None)
        original_title = raw_title
        notes = []
        if was_untitled:
            original_title = raw_title or original.get('display_title') or _text(record.get('preview')).split('\n')[0]
            original_title = original_title or '（当时无自定义标题）'
            notes.append('原来没有自定义标题；原标题列显示当时的可见标题。')
        status = result.get('status') or 'pending'
        actual = result.get('title')
        if status == 'restored':
            actual = result.get('restore_result', {}).get('title', actual)
            notes.append('已恢复原标题；本列显示已确认的恢复结果。')
        if status not in CONFIRMED:
            actual = None
            notes.append({'error': '未确认新标题，优化后标题留空。',
                          'conflict': '标题发生变化，本次未覆盖；优化后标题留空。',
                          'pending': '尚未处理，优化后标题留空。',
                          'locked': '已锁定，本次未修改；优化后标题留空。',
                          'skipped': '已跳过，本次未修改；优化后标题留空。',
                          'stale': '检查已过期，没有确认写入。',
                          'preview': '仅生成建议，尚未写入。'}.get(status, '没有已确认的标题结果。'))
        elif not isinstance(actual, str) or not actual:
            actual = None
            notes.append('本次结果没有返回已核实标题，优化后标题留空。')
        if tid in proposed and actual is None:
            notes.append('候选标题未当作已写入结果列出。')
        for key in ('reason', 'message', 'read_error'):
            if result.get(key):
                notes.append(_text(result[key]))
        archived = record.get('archived', original.get('archived'))
        rows.append({'thread_id': tid, 'original_title': _text(original_title),
                     'optimized_title': _text(actual), 'status': status,
                     'archived': archived if isinstance(archived, bool) else None,
                     'note': '\n'.join(dict.fromkeys(notes))})
    return rows


def _safe_xml(text):
    """Replace XML-illegal characters visibly; preserve tabs and line breaks."""
    valid = lambda c: c in '\t\n\r' or (0x20 <= ord(c) <= 0xD7FF) or (0xE000 <= ord(c) <= 0xFFFD) or (0x10000 <= ord(c) <= 0x10FFFF)
    return ''.join(c if valid(c) else '\uFFFD' for c in _text(text))


def _cell_text(value, row, column, replacements):
    raw = _text(value)
    clean = _safe_xml(raw)
    replacements[0] += sum(a != b for a, b in zip(raw, clean))
    # Excel stores UTF-16: use this stricter limit for non-BMP characters too.
    if len(clean.encode('utf-16-le')) // 2 > 32767:
        raise ValueError(f'第 {row} 行「{column}」超过 Excel 单元格 32767 字符上限；未截断，未覆盖文件。')
    return clean


def _xml(element):
    # Some workbook importers require default namespaces for package parts.
    # Clone only these fixed structures instead of changing ElementTree's global
    # namespace registry, so parallel report creation cannot affect serialization.
    namespace = element.tag[1:].split('}', 1)[0] if element.tag.startswith('{') else ''
    if namespace in (CONTENT, PKGREL, APP):
        element = copy.deepcopy(element)
        prefix = '{' + namespace + '}'
        for node in element.iter():
            if isinstance(node.tag, str) and node.tag.startswith(prefix):
                node.tag = node.tag[len(prefix):]
        element.set('xmlns', namespace)
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def _el(parent, tag, namespace=NS, **attributes):
    return ET.SubElement(parent, f'{{{namespace}}}{tag}', {key: _text(value) for key, value in attributes.items()})


def _styles():
    root = ET.Element(f'{{{NS}}}styleSheet')
    fonts = _el(root, 'fonts', count=2)
    for header in (False, True):
        font = _el(fonts, 'font')
        _el(font, 'sz', val=11)
        _el(font, 'name', val='Arial')
        _el(font, 'family', val=2)
        if header:
            _el(font, 'b')
            _el(font, 'color', rgb='FFFFFFFF')
        else:
            _el(font, 'color', rgb='FF243746')
    fills = _el(root, 'fills', count=4)
    for kind, color in [('none', None), ('gray125', None), ('solid', 'FF234E63'), ('solid', 'FFF2F6F9')]:
        fill = _el(fills, 'fill')
        pattern = _el(fill, 'patternFill', patternType=kind)
        if color:
            _el(pattern, 'fgColor', rgb=color)
            _el(pattern, 'bgColor', indexed=64)
    borders = _el(root, 'borders', count=1)
    border = _el(borders, 'border')
    for side in ('left', 'right', 'top', 'bottom', 'diagonal'):
        _el(border, side)
    base = _el(root, 'cellStyleXfs', count=1)
    _el(base, 'xf', numFmtId=0, fontId=0, fillId=0, borderId=0)
    formats = _el(root, 'cellXfs', count=4)
    for font, fill, wrap in [(0, 0, False), (1, 2, True), (0, 0, True), (0, 3, True)]:
        xf = _el(formats, 'xf', numFmtId=49, fontId=font, fillId=fill, borderId=0,
                 xfId=0, applyNumberFormat=1, applyAlignment=1, applyFill=1)
        _el(xf, 'alignment', vertical='center' if font else 'top', wrapText=1 if wrap else 0)
    styles = _el(root, 'cellStyles', count=1)
    _el(styles, 'cellStyle', name='Normal', xfId=0, builtinId=0)
    _el(root, 'dxfs', count=0)
    return _xml(root)


def _height(values):
    lines = 1
    for text, width in zip(values, WIDTHS):
        needed = sum(max(1, math.ceil(sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1
                                         for c in part) / (width - 3))) for part in text.split('\n'))
        lines = max(lines, needed)
    return min(409, max(30, 16 * lines + 10))


def _worksheet(values):
    root = ET.Element(f'{{{NS}}}worksheet')
    _el(root, 'dimension', ref=f'A1:F{len(values) + 1}')
    views = _el(root, 'sheetViews')
    view = _el(views, 'sheetView', workbookViewId=0, showGridLines=0)
    _el(view, 'pane', ySplit=1, topLeftCell='A2', activePane='bottomLeft', state='frozen')
    _el(view, 'selection', pane='bottomLeft', activeCell='A2', sqref='A2')
    _el(root, 'sheetFormatPr', defaultRowHeight=30)
    cols = _el(root, 'cols')
    for number, width in enumerate(WIDTHS, 1):
        _el(cols, 'col', min=number, max=number, width=width, customWidth=1)
    data = _el(root, 'sheetData')
    for row_number, row_values in enumerate([HEADERS] + values, 1):
        row = _el(data, 'row', r=row_number, ht=32 if row_number == 1 else _height(row_values), customHeight=1)
        style = 1 if row_number == 1 else 2 + (row_number % 2)
        for col, value in enumerate(row_values):
            cell = _el(row, 'c', r=f'{chr(65 + col)}{row_number}', s=style, t='inlineStr')
            inline = _el(cell, 'is')
            text = _el(inline, 't')
            text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
            # OOXML uses _xHHHH_ escape tokens; protect matching literal text.
            text.text = re.sub(r'_x[0-9A-Fa-f]{4}_', lambda match: '_x005F_' + match.group(0)[1:], value)
    _el(root, 'autoFilter', ref=f'A1:F{len(values) + 1}')
    _el(root, 'pageMargins', left='0.25', right='0.25', top='0.4', bottom='0.4', header='0.2', footer='0.2')
    _el(root, 'pageSetup', orientation='landscape', paperSize=9, fitToWidth=1, fitToHeight=0)
    return _xml(root)


def _relationships(entries):
    root = ET.Element(f'{{{PKGREL}}}Relationships')
    for ident, kind, target in entries:
        _el(root, 'Relationship', namespace=PKGREL, Id=ident, Type=kind, Target=target)
    return _xml(root)


def _timestamp(value):
    if value:
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('metadata.created_at 必须为 ISO 8601 日期时间。') from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    else:
        parsed = datetime.datetime.now(datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _package(values, metadata):
    types = ET.Element(f'{{{CONTENT}}}Types')
    for extension, content in [('rels', 'application/vnd.openxmlformats-package.relationships+xml'),
                               ('xml', 'application/xml')]:
        _el(types, 'Default', namespace=CONTENT, Extension=extension, ContentType=content)
    for part, content in [('/xl/workbook.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml'),
                           ('/xl/worksheets/sheet1.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml'),
                           ('/xl/styles.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml'),
                           ('/docProps/core.xml', 'application/vnd.openxmlformats-package.core-properties+xml'),
                           ('/docProps/app.xml', 'application/vnd.openxmlformats-officedocument.extended-properties+xml')]:
        _el(types, 'Override', namespace=CONTENT, PartName=part, ContentType=content)
    workbook = ET.Element(f'{{{NS}}}workbook')
    views = _el(workbook, 'bookViews')
    _el(views, 'workbookView', activeTab=0)
    sheet = _el(_el(workbook, 'sheets'), 'sheet', name=SHEET_NAME, sheetId=1)
    sheet.set(f'{{{REL}}}id', 'rId1')
    core = ET.Element(f'{{{CORE}}}coreProperties')
    for key, default, tag in [('title', '对话标题整理记录', 'title'),
                               ('creator', 'Codex-Titles-Clean', 'creator'), ('description', '', 'description')]:
        _el(core, tag, namespace=DC).text = _safe_xml(metadata.get(key, default))
    created = _el(core, 'created', namespace=DCTERMS)
    created.set(f'{{{XSI}}}type', 'dcterms:W3CDTF')
    created.text = _timestamp(metadata.get('created_at'))
    app = ET.Element(f'{{{APP}}}Properties')
    _el(app, 'Application', namespace=APP).text = 'Codex-Titles-Clean'
    _el(app, 'AppVersion', namespace=APP).text = '1.0'
    titles = _el(app, 'TitlesOfParts', namespace=APP)
    vector = _el(titles, 'vector', namespace=VT, size=1, baseType='lpstr')
    _el(vector, 'lpstr', namespace=VT).text = SHEET_NAME
    return {'[Content_Types].xml': _xml(types),
            '_rels/.rels': _relationships([
                ('rId1', REL + '/officeDocument', 'xl/workbook.xml'),
                ('rId2', 'http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties', 'docProps/core.xml'),
                ('rId3', REL + '/extended-properties', 'docProps/app.xml')]),
            'xl/workbook.xml': _xml(workbook),
            'xl/_rels/workbook.xml.rels': _relationships([
                ('rId1', REL + '/worksheet', 'worksheets/sheet1.xml'), ('rId2', REL + '/styles', 'styles.xml')]),
            'xl/worksheets/sheet1.xml': _worksheet(values), 'xl/styles.xml': _styles(),
            'docProps/core.xml': _xml(core), 'docProps/app.xml': _xml(app)}


def write_report(path, rows, metadata=None):
    """Write atomic OOXML with inline strings; never infer a new title from a plan."""
    target = Path(path).expanduser().absolute()
    if target.suffix.lower() != '.xlsx':
        raise ValueError('报告输出路径必须以 .xlsx 结尾。')
    rows = list(rows)
    if len(rows) > 1048575:
        raise ValueError('记录数超过 Excel 单张工作表上限（含表头最多1048576行）。')
    values, replacements = [], [0]
    for number, row in enumerate(rows, 2):
        status = row.get('status') or 'pending'
        archived = row.get('archived')
        value = [row.get('thread_id'), row.get('original_title'),
                 row.get('optimized_title') if status in CONFIRMED else '',
                 STATUSES.get(status, _text(status)),
                 '已归档' if archived is True else '未归档' if archived is False else '未知', row.get('note')]
        values.append([_cell_text(cell, number, header, replacements) for cell, header in zip(value, HEADERS)])
    parts = _package(values, metadata or {})  # validation happens before touching the output
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.title-report-', suffix='.xlsx.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w+b') as stream:
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for name, payload in parts.items():
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'path': str(target), 'row_count': len(rows),
            'replaced_invalid_characters': replacements[0]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='把标题整理记录JSON导出为Excel；不读取Codex或调用模型。')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    source = json.loads(args.source.read_text(encoding='utf-8'))
    if isinstance(source, list):
        rows, metadata = source, {}
    elif isinstance(source, dict) and isinstance(source.get('rows'), list):
        rows, metadata = source['rows'], source.get('metadata', {})
    else:
        raise ValueError('来源JSON须为记录数组或包含 rows 数组的对象。')
    result = write_report(args.output, rows, metadata)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
