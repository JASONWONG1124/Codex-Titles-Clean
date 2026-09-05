#!/usr/bin/env python3
"""Portable macOS installation; public Codex CLI, no database/config patching.

The unmodified official plugin-creator helpers in vendor/ were copied on
2026-09-05 from Codex's bundled skills/.system/plugin-creator/scripts/:
create_basic_plugin.py SHA256 272cb14e02ad7c76ac40777443c49cfecfa333ee28ab7f6214210b72c8fd02ae
identifier_validation.py SHA256 a6d51ce4a9a7e8f85626ff5808a467a67574e7f8cdf1167ffb467c5f67e57223
read_marketplace_name.py SHA256 ba24e6d91eed6f778bde022a967be335c6253983b5ecd1c5e30c8483385887fd

Existing entries are never rewritten. The official helper appends a new entry
only after validating the entire marketplace and rejecting name/source conflicts.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

NAME = 'codex-titles-clean'
SOURCE = {'source': 'local', 'path': './plugins/codex-titles-clean'}


def load_helper(package: Path):
    path = package / 'scripts/vendor/create_basic_plugin.py'
    spec = importlib.util.spec_from_file_location('codex_titles_clean_scaffold', path)
    if not spec or not spec.loader:
        raise ValueError('插件缺少官方 marketplace helper，请重新取得完整文件夹。')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def codex_binary(home: Path):
    candidates = [os.environ.get('CODEX_TITLES_CLEAN_CODEX'),
                  os.environ.get('SIDEBAR_TITLES_CODEX'),
                  os.environ.get('CODEX_CLI_PATH'),
                  '/Applications/Codex.app/Contents/Resources/codex',
                  '/Applications/ChatGPT.app/Contents/Resources/codex',
                  str(home / 'Applications/Codex.app/Contents/Resources/codex'),
                  str(home / 'Applications/ChatGPT.app/Contents/Resources/codex'),
                  shutil.which('codex')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise ValueError('找不到 Codex。请先安装并登录 Codex 桌面版，或把 codex CLI 加入 PATH。')


def validate_package(package: Path):
    if not package.is_dir():
        raise ValueError('请解压完整插件文件夹后安装。')
    required = ['.codex-plugin/plugin.json', 'hooks/hooks.json',
                'scripts/title_hook.py', 'scripts/title_manager.py', 'scripts/app_server.py',
                'scripts/title_report.py',
                'scripts/history_backfill.py', 'skills/codex-titles-clean/SKILL.md',
                'scripts/vendor/create_basic_plugin.py',
                'scripts/vendor/identifier_validation.py',
                'scripts/vendor/read_marketplace_name.py']
    for relative in required:
        if not (package / relative).is_file():
            raise ValueError(f'插件不完整，缺少 {relative}。请重新取得完整文件夹。')
    for path in package.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'安装包包含符号链接，已停止以避免复制外部文件：{path}')
    manifest = json.loads((package / '.codex-plugin/plugin.json').read_text())
    if manifest.get('name') != NAME:
        raise ValueError('安装包 manifest.name 与 codex-titles-clean 不一致。')


def preflight_marketplace(package: Path, path: Path, runner=subprocess.run):
    helper = load_helper(package)
    if path.exists():
        # Use the official validator before constructing any plugin selector.
        check = runner([sys.executable, str(package / 'scripts/vendor/read_marketplace_name.py'),
                        '--marketplace-path', str(path)], check=True, capture_output=True,
                       text=True, timeout=15)
        actual_name = check.stdout.strip()
    else:
        actual_name = 'personal'
    payload = helper.load_validated_marketplace(path, None, NAME, True)
    if payload['name'] != actual_name:
        raise ValueError('marketplace 在检查期间发生变化，请重新运行安装。')
    matches = [entry for entry in payload['plugins']
               if isinstance(entry, dict) and entry.get('name') == NAME]
    if len(matches) > 1 or (matches and matches[0].get('source') != SOURCE):
        raise ValueError('个人 marketplace 已存在同名、不同来源的 codex-titles-clean；已停止，未覆盖。')
    return helper, actual_name, bool(matches)


def backup_name(path: Path, suffix='backup'):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return path.with_name(f'{path.name}.{suffix}-{stamp}-{uuid.uuid4().hex[:8]}')


def copy_package(package: Path, target: Path):
    if target.is_symlink():
        raise ValueError(f'安装目标是符号链接，已停止：{target}')
    if package.resolve() == target.resolve():
        return None
    old = None
    if target.exists():
        manifest = target / '.codex-plugin/plugin.json'
        if not manifest.is_file() or json.loads(manifest.read_text()).get('name') != NAME:
            raise ValueError(f'安装目标包含其他文件，已停止，未覆盖：{target}')
        old = backup_name(target)
        target.rename(old)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package, target,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store', '.git'))
    except BaseException:
        if target.exists():
            target.rename(backup_name(target, 'failed'))
        if old:
            old.rename(target)
        raise
    return old


def describe_hooks(package: Path):
    definitions = json.loads((package / 'hooks/hooks.json').read_text())
    events = definitions.get('hooks')
    if not isinstance(events, dict) or not events:
        raise ValueError('插件 Hook 定义缺失或无效。')
    print('插件声明的 Hook（请在 Codex 设置 → Hooks 中仅审阅本插件）：')
    for event, groups in events.items():
        for group in groups:
            for hook in group.get('hooks', []):
                print(f"  {event}: {hook.get('command', hook.get('type', 'unknown'))}")
    digest = hashlib.sha256((package / 'hooks/hooks.json').read_bytes()).hexdigest()
    print(f'  hooks.json 文件校验 SHA256: {digest}')
    print('上面是文件校验值，不是 Codex 的 Hook 信任键。安装器不会修改全局信任设置。')


def commands(codex: str, selector: str, target: Path):
    print('\n管理位置：Codex → 插件（Plugins）→ Codex-Titles-Clean。')
    print('查看安装状态：' + shlex.join([codex, 'plugin', 'list', '--json']))
    print('继续历史整理：' + shlex.join([sys.executable, str(target / 'scripts/history_backfill.py'),
                                     'run', '--automatic']))
    print('查看历史整理状态：' + shlex.join([sys.executable, str(target / 'scripts/history_backfill.py'),
                                         'status']))
    print('重新导出 Excel（不重复改名）：' + shlex.join([sys.executable, str(target / 'scripts/history_backfill.py'),
                                                   'report']))
    print('卸载：' + shlex.join([codex, 'plugin', 'remove', selector]))
    print('卸载后停止自动检查；已改标题、原标题恢复记录及源文件夹会保留。')


def install(package: Path, home: Path, runner=subprocess.run):
    if sys.version_info < (3, 9):
        raise ValueError('需要 Python 3.9 或更新版本。')
    package, home = package.resolve(), home.expanduser().resolve()
    validate_package(package)
    codex = codex_binary(home)
    target = home / 'plugins' / NAME
    marketplace = home / '.agents/plugins/marketplace.json'
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    with (marketplace.parent / '.codex-titles-clean-install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('另一个安装正在运行，请等待其结束。')
        helper, market_name, present = preflight_marketplace(package, marketplace, runner)
        describe_hooks(package)
        print('\n本次安装将整理全部本机 Codex 历史（包括归档），保留原名供恢复。')
        print('首次整理需要使用当前 Codex 账户的模型额度；任务较多时会持续一段时间。', flush=True)
        old = copy_package(package, target)
        if old:
            print(f'旧版本完整保留在：{old}')
        if not present:
            if marketplace.exists():
                saved = backup_name(marketplace)
                shutil.copy2(marketplace, saved)
                print(f'marketplace 备份：{saved}')
            helper.update_marketplace_json(marketplace, None, NAME,
                                           'AVAILABLE', 'ON_INSTALL', 'Productivity', False)
        selector = f'{NAME}@{market_name}'
        env = dict(os.environ, CODEX_TITLES_CLEAN_CODEX=codex, SIDEBAR_TITLES_CODEX=codex)
        try:
            runner([codex, 'plugin', 'add', selector], check=True, env=env, timeout=180)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f'插件安装未确认成功：{error}', file=sys.stderr)
            commands(codex, selector, target)
            return 1
        print('\n插件安装命令已成功，正在执行首次历史整理，结束后自动生成 Excel 对照表。', flush=True)
        try:
            result = runner([sys.executable, str(target / 'scripts/history_backfill.py'),
                             'run', '--automatic'], env=env, check=False)
            code = result.returncode
        except (OSError, KeyboardInterrupt) as error:
            print(f'历史整理中断：{error}', file=sys.stderr)
            code = 1
        if code == 0:
            print('\n历史整理与 Excel 导出命令已成功返回；处理数量、跳过项及完成状态以上方报告为准。')
            print('Excel 文件位置见上方 report.path；可用 Excel、Numbers 或兼容的软件打开。')
        else:
            print('\n插件已安装，但历史整理或 Excel 导出未全部完成。请查看上方状态，用下方继续或重新导出命令恢复。')
        print('以后自动检查：在 Codex 设置 → Hooks 中查看并信任本插件声明的 Hook。')
        print('若已经信任，无需重复操作。新建任务可载入最新插件；已有历史由上述整理单独处理。')
        commands(codex, selector, target)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description='安装 Codex-Titles-Clean 插件，并首次整理全部本机 Codex 历史。')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        return install(args.source, Path.home())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'安装已停止：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
