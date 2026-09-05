#!/bin/bash
# Finder double-click entry; all state changes are implemented in Python.
set -u
plugin_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3.9 或更新版本。请先从 https://www.python.org/downloads/macos/ 安装，再双击此文件。'
  read -r -p '按回车关闭窗口。' _reply
  exit 1
fi
python3 "$plugin_dir/scripts/install_plugin.py" "$@"
result=$?
if [ -t 0 ]; then
  read -r -p '按回车关闭窗口。' _reply
fi
exit "$result"
