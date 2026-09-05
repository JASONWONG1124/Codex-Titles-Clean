#!/usr/bin/env python3
"""Validated title changes, receipts, and undo for Codex-Titles-Clean."""
import argparse
import collections
import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path

from app_server import AppServer

ROOT = Path(__file__).resolve().parents[1]
UNSET = object()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_title(title):
    if not isinstance(title, str) or title != title.strip():
        raise ValueError('标题不能含首尾空白。')
    if any(unicodedata.category(c).startswith('C') or c in '\r\n\t|｜' for c in title):
        raise ValueError('标题含控制符或错误的分隔符，请使用 丨。')
    parts = title.split('丨')
    if len(parts) != 3 or any(not p.strip() or p != p.strip() for p in parts):
        raise ValueError('标题格式为 简短对象丨两字分类丨具体主题。')
    subject, category, summary = parts
    if len(subject) > 8:
        raise ValueError('第一段过长，请缩短到 8 个字符以内。')
    if not re.fullmatch(r'[\u3400-\u4dbf\u4e00-\u9fff]{2}', category):
        raise ValueError('中间分类必须恰好两个汉字；类别不限。')
    if len(summary) > 24:
        raise ValueError('第三段过长，请保留最有区分度的内容。')
    width = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in title)
    if width > 50:
        raise ValueError('标题总长度过长，请缩短到约 25 个汉字的显示宽度以内。')
    return title


def validate_decision(focus, reason):
    if not isinstance(focus, str) or not isinstance(reason, str) or not focus.strip() or len(focus) > 300 or not reason.strip() or len(reason) > 500:
        raise ValueError('需要简短的主要目标与判断理由。')


class StaleCheck(RuntimeError):
    pass


def batch_report(store, entries, results, inventory):
    source, target, source_saved = None, None, False
    try:
        from title_report import build_report_rows, write_report
        originals = {e['thread_id']: {'name': e['expected_title']} for e in entries}
        state = {'entries': {r['thread_id']: r for r in results}}
        rows = build_report_rows(inventory, originals, state,
                                 {e['thread_id']: e for e in entries})
        folder = store.root / 'reports'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        batch_id = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        source = folder / ('batch-' + batch_id + '.json')
        target = source.with_suffix('.xlsx')
        metadata = {'title': '批量标题整理对照', 'created_at': now()}
        # Keep a retryable result even if producing the workbook fails.
        store._atomic_write(source, {'rows': rows, 'metadata': metadata})
        source_saved = True
        return {**write_report(target, rows, metadata), 'status': 'written', 'source_path': str(source)}
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        result = {'status': 'error', 'message': str(exc)}
        if source_saved:
            result.update(source_path=str(source), retry=shlex.join([
                'python3', str(ROOT / 'scripts/title_report.py'), '--source', str(source), '--output', str(target)]))
        return result


class Store:
    def __init__(self, root=None):
        selected = root or os.environ.get('CODEX_TITLES_CLEAN_STATE_DIR') or os.environ.get('SIDEBAR_TITLES_STATE_DIR')
        if selected:
            self.root = Path(selected).expanduser()
        else:
            codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').expanduser()
            primary, legacy = codex_home / 'codex-titles-clean', codex_home / 'sidebar-titles'
            # Keep the original records in place. A rename must not lose undo
            # history or make an already completed migration run again.
            self.root = legacy if not primary.exists() and legacy.exists() else primary
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, thread_id, suffix='.json'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', thread_id or ''):
            raise ValueError('无效任务 ID。')
        return self.root / (thread_id + suffix)

    def load(self, thread_id):
        path = self.path(thread_id)
        state = json.loads(path.read_text()) if path.exists() else {}
        marker_path = self.path(thread_id, '.turn.json')
        if marker_path.exists():
            marker = json.loads(marker_path.read_text())
            if state.get('pending', {}).get('id') == marker['id']:
                marker.update(state['pending'])
            nudge = self.path(thread_id, '.nudge.json')
            if nudge.exists() and json.loads(nudge.read_text()).get('id') == marker['id']:
                marker['nudged'] = True
            state['pending'] = marker
        return state

    def save(self, thread_id, state):
        state['thread_id'] = thread_id
        state['schema_version'] = 1
        self._atomic_write(self.path(thread_id), state)

    def _atomic_write(self, target, state):
        fd, name = tempfile.mkstemp(prefix='.title-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextlib.contextmanager
    def locked(self, thread_id, suffix='.lock'):
        with self.path(thread_id, suffix).open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def record_prompt(self, thread_id, pending):
        # Separate from long RPC transactions so new input immediately supersedes them.
        self._atomic_write(self.path(thread_id, '.turn.json'), pending)

    def record_nudge(self, thread_id, nonce):
        self._atomic_write(self.path(thread_id, '.nudge.json'), {'id': nonce})


class Manager:
    def __init__(self, store, rpc):
        self.store, self.rpc = store, rpc

    def read(self, thread_id):
        return self.rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']

    def _receipt(self, thread_id, state, status):
        pending = state.get('pending')
        if pending:
            pending['checked'] = True
            pending['result'] = status
        state['last_check_at'] = now()
        self.store.save(thread_id, state)

    def _recover(self, thread_id, state, current):
        # A crash/timeout after a successful RPC must retain a usable undo entry.
        intent = state.get('inflight')
        if intent and current == intent['new_title']:
            intent['status'] = 'recovered'
            state.setdefault('history', []).append(intent)
            state['last_title'] = current
            if intent['kind'] == 'restore':
                state['locked'] = True
            state.pop('inflight', None)
        elif intent and current == intent['old_title']:
            state.pop('inflight', None)
        elif intent:
            state['locked'] = True
            state['lock_reason'] = 'pending_change_conflict'

    @staticmethod
    def _archived(thread):
        return 'archived_sessions' in Path(thread.get('path') or '').parts

    def _restore_archive(self, thread_id, state):
        if not state.get('archive_restore_required'):
            return
        if not self._archived(self.read(thread_id)):
            self.rpc.call('thread/archive', {'threadId': thread_id})
            if not self._archived(self.read(thread_id)):
                raise RuntimeError('标题处理后尚未确认恢复归档；已保存待恢复标记，请继续整理。')
        state.pop('archive_restore_required', None)
        self.store.save(thread_id, state)

    def _write(self, thread_id, state, current, title, reason, kind='rename', check_id=None, allow_untitled=False):
        if not current and not allow_untitled:
            raise ValueError('当前任务尚无可恢复的标题，等待默认标题生成后再检查。')
        # Re-read immediately before mutation; app-server has no atomic title CAS.
        target = self.read(thread_id)
        if target.get('name') != current:
            raise RuntimeError('标题在检查期间变化，已放弃此次修改。')
        if check_id and self.store.load(thread_id).get('pending', {}).get('id') != check_id:
            raise StaleCheck('新一轮已开始，旧检查未执行。')
        state.setdefault('original_title', current)
        record = {'id': str(uuid.uuid4()), 'at': now(), 'old_title': current,
                  'new_title': title, 'reason': reason, 'kind': kind}
        state['inflight'] = record
        archived = self._archived(target)
        if archived:
            state['archive_restore_required'] = True
        self.store.save(thread_id, state)
        try:
            if archived:
                self.rpc.call('thread/unarchive', {'threadId': thread_id})
            self.rpc.call('thread/name/set', {'threadId': thread_id, 'name': title})
            if self.read(thread_id).get('name') != title:
                raise RuntimeError('写入后标题不一致，保留恢复记录，未标记成功。')
        finally:
            self._restore_archive(thread_id, state)
        record['status'] = 'applied'
        state.setdefault('history', []).append(record)
        state.pop('inflight', None)
        state['last_title'] = title
        self.store.save(thread_id, state)

    def check(self, thread_id, title, focus, change, reason, check_id=None, expected_title=UNSET, allow_untitled=False):
        validate_title(title)
        if change not in ('initial', 'major', 'keep'):
            raise ValueError('change 必须为 initial、major 或 keep。')
        validate_decision(focus, reason)
        with self.store.locked(thread_id):
            state = self.store.load(thread_id)
            self._restore_archive(thread_id, state)
            pending = state.get('pending', {})
            if check_id and pending.get('id') != check_id:
                return {'status': 'stale', 'reason': '新一轮对话已开始，此次检查未执行。'}
            if check_id and pending.get('checked'):
                return {'status': 'kept', 'reason': '本轮已检查。'}
            thread = self.read(thread_id)
            current = thread.get('name')
            if expected_title is not UNSET and current != expected_title:
                return {'status': 'conflict', 'title': current}
            source = thread.get('source')
            if thread.get('parentThreadId') or thread.get('threadSource') in ('subagent', 'subAgent') or (isinstance(source, dict) and 'subAgent' in source):
                self._receipt(thread_id, state, 'skipped')
                return {'status': 'skipped', 'reason': '跳过子代理任务。'}
            self._recover(thread_id, state, current)
            if state.get('last_title') and current != state['last_title']:
                state['locked'] = True
                state['lock_reason'] = 'manual_title_change'
            if state.get('locked'):
                self._receipt(thread_id, state, 'locked')
                return {'status': 'locked', 'title': current, 'reason': state.get('lock_reason', '已锁定。')}
            if not current and not allow_untitled:
                self._receipt(thread_id, state, 'skipped')
                return {'status': 'skipped', 'reason': '等待默认标题生成，下一轮再检查。'}
            if not current and allow_untitled:
                state.setdefault('original_display_title', (thread.get('preview') or '新任务').splitlines()[0][:200])
            update = title != current and (change == 'major' or (change == 'initial' and not state.get('last_title')))
            if update:
                try:
                    self._write(thread_id, state, current, title, reason, check_id=check_id, allow_untitled=allow_untitled)
                except StaleCheck:
                    return {'status': 'stale', 'reason': '新一轮已开始，旧检查未执行。'}
                state['focus'] = focus
            else:
                state.setdefault('original_title', current)
                state.setdefault('last_title', current)
                state.setdefault('focus', focus)
            status = 'updated' if update else 'kept'
            self._receipt(thread_id, state, status)
            return {'status': status, 'old_title': current, 'title': state['last_title']}

    def restore(self, thread_id, expected_title=UNSET, original_override=UNSET, display_override=None):
        with self.store.locked(thread_id):
            state = self.store.load(thread_id)
            self._restore_archive(thread_id, state)
            current = self.read(thread_id).get('name')
            if expected_title is not UNSET and current != expected_title:
                return {'status': 'conflict', 'title': current}
            self._recover(thread_id, state, current)
            raw_original = state.get('original_title') if original_override is UNSET else original_override
            original = raw_original or (state.get('original_display_title') if original_override is UNSET else display_override)
            if not original:
                raise ValueError('没有可恢复的原名记录。')
            if original != current:
                self._write(thread_id, state, current, original, '用户恢复原名', 'restore')
            state['locked'] = True
            state['lock_reason'] = 'restored'
            self._receipt(thread_id, state, 'restored')
            return {'status': 'restored', 'title': original, 'locked': True,
                    'restore_mode': 'display_fallback' if not raw_original else 'exact'}

    def unlock(self, thread_id):
        with self.store.locked(thread_id):
            state = self.store.load(thread_id)
            state['last_title'] = self.read(thread_id).get('name')
            state['locked'] = False
            state.pop('lock_reason', None)
            state.pop('inflight', None)
            self.store.save(thread_id, state)
            return {'status': 'unlocked', 'title': state['last_title']}


@contextlib.contextmanager
def failure_receipt(store, args):
    try:
        yield
    except Exception as exc:
        if args.command == 'check' and args.check_id:
            with store.locked(args.thread_id):
                state = store.load(args.thread_id)
                if state.get('pending', {}).get('id') == args.check_id:
                    state['last_error'] = str(exc)[:500]
                    pending = state['pending']
                    pending.update(checked=True, result='error')
                    state['last_check_at'] = now()
                    store.save(args.thread_id, state)
        raise


def main():
    parser = argparse.ArgumentParser(description='Codex 三段标题管理')
    parser.add_argument('--thread-id', default=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID'))
    subs = parser.add_subparsers(dest='command', required=True)
    check = subs.add_parser('check')
    for key in ('title', 'focus', 'reason'):
        check.add_argument('--' + key, required=True)
    check.add_argument('--change', choices=['initial', 'major', 'keep'], required=True)
    check.add_argument('--check-id')
    for command in ('status', 'history', 'restore', 'lock', 'unlock'):
        subs.add_parser(command)
    listing = subs.add_parser('list')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--cursor')
    reading = subs.add_parser('read')
    reading.add_argument('--cursor')
    batch = subs.add_parser('batch')
    batch.add_argument('--plan', required=True)
    batch.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    store = Store()
    if args.command not in ('list', 'batch') and not args.thread_id:
        raise ValueError('缺少当前任务 ID，可通过 --thread-id 指定。')
    if args.command in ('history', 'lock'):
        with store.locked(args.thread_id):
            state = store.load(args.thread_id)
            if args.command == 'lock':
                state.update(locked=True, lock_reason='user')
                store.save(args.thread_id, state)
            result = state if args.command == 'lock' else state.get('history', [])
    else:
        with failure_receipt(store, args), AppServer() as rpc:
            manager = Manager(store, rpc)
            if args.command == 'check':
                result = manager.check(args.thread_id, args.title, args.focus, args.change, args.reason, args.check_id)
            elif args.command == 'restore':
                result = manager.restore(args.thread_id)
            elif args.command == 'unlock':
                result = manager.unlock(args.thread_id)
            elif args.command == 'status':
                state = store.load(args.thread_id)
                result = {'title': manager.read(args.thread_id).get('name'), 'state': state}
            elif args.command == 'list':
                params = {'limit': max(1, min(args.limit, 100)), 'sortKey': 'updated_at', 'sourceKinds': ['cli', 'vscode', 'appServer']}
                if args.cursor:
                    params['cursor'] = args.cursor
                response = rpc.call('thread/list', params)
                result = {'nextCursor': response.get('nextCursor'), 'threads': [
                    {k: t.get(k) for k in ('id', 'name', 'cwd', 'updatedAt')} for t in response.get('data', [])]}
            elif args.command == 'read':
                params = {'threadId': args.thread_id, 'itemsView': 'summary', 'limit': 5}
                if args.cursor:
                    params['cursor'] = args.cursor
                response = rpc.call('thread/turns/list', params)
                turns = []
                for turn in response.get('data', []):
                    items = [i for i in turn.get('items', []) if i.get('type') in ('userMessage', 'agentMessage')]
                    turns.append({'id': turn.get('id'), 'items': items})
                result = {'title': manager.read(args.thread_id).get('name'), 'turns': turns, 'nextCursor': response.get('nextCursor')}
            elif args.command == 'batch':
                entries = json.loads(Path(args.plan).read_text())
                if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
                    raise ValueError('批量计划须为包含 1–100 条的 JSON 数组。')
                seen = set()
                for entry in entries:
                    store.path(entry['thread_id'])
                    validate_title(entry['title'])
                    if entry['thread_id'] in seen:
                        raise ValueError('批量计划有重复任务 ID。')
                    seen.add(entry['thread_id'])
                    if not all(isinstance(entry.get(k), str) and entry[k].strip() for k in ('expected_title', 'focus', 'reason')):
                        raise ValueError('每项需包含 expected_title、focus、reason。')
                    validate_decision(entry['focus'], entry['reason'])
                result = []
                inventory = []
                for entry in entries:
                    tid = entry['thread_id']
                    inventory_row = {'id': tid, 'archived': None}
                    inventory.append(inventory_row)
                    try:
                        thread = manager.read(tid)
                        current = thread.get('name')
                        inventory_row['archived'] = manager._archived(thread)
                        if current != entry['expected_title']:
                            result.append({'thread_id': tid, 'status': 'conflict', 'title': current})
                        elif not args.apply:
                            result.append({'thread_id': tid, 'status': 'preview', 'old_title': current, 'title': entry['title']})
                        else:
                            result.append({'thread_id': tid, **manager.check(tid, entry['title'], entry['focus'], 'major', entry['reason'], expected_title=entry['expected_title'])})
                    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as exc:
                        result.append({'thread_id': tid, 'status': 'error', 'message': str(exc)})
                if args.apply:
                    report = batch_report(store, entries, result, inventory)
                    result = {'results': result, 'counts': dict(collections.Counter(r['status'] for r in result)),
                              'report': report}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and (result.get('report') or {}).get('status') == 'error':
        return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
