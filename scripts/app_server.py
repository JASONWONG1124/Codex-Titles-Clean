"""Small JSON-RPC client for the locally installed Codex app-server.

Uses public protocol methods; never edits Codex's database or rollout files.
No model invocation, credentials, or persistent daemon are required.
"""
import json
import os
import selectors
import shutil
import subprocess
import time
from pathlib import Path


def codex_binary():
    candidates = [os.environ.get('CODEX_TITLES_CLEAN_CODEX'),
                  os.environ.get('SIDEBAR_TITLES_CODEX'),
                  os.environ.get('CODEX_CLI_PATH'),
                  '/Applications/ChatGPT.app/Contents/Resources/codex',
                  '/Applications/Codex.app/Contents/Resources/codex',
                  shutil.which('codex')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('找不到 Codex CLI，请在 Codex 桌面环境中运行。')


class AppServer:
    def __init__(self, timeout=15):
        self.timeout = timeout
        self.proc = None
        self.buffer = b''
        self.counter = 0
        self.broken = False

    def __enter__(self):
        self.buffer = b''
        self.broken = False
        self.proc = subprocess.Popen([codex_binary(), 'app-server', '--stdio'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            self.call('initialize', {'clientInfo': {'name': 'codex_titles_clean', 'version': '0.1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _send(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + '\n').encode())
        self.proc.stdin.flush()

    def call(self, method, params):
        if self.broken:
            # Do not replay an unconfirmed operation. A later call gets a fresh transport.
            self.__exit__(None, None, None)
            self.__enter__()
        self.counter += 1
        request_id = self.counter
        self._send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                obj = json.loads(line)
                if obj.get('id') != request_id:
                    continue
                if 'error' in obj:
                    raise RuntimeError(f"{method}: {obj['error'].get('message', '请求失败')}")
                return obj.get('result', {})
            if not self.selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Codex app-server 已关闭。')
            self.buffer += chunk
            if len(self.buffer) > 32 * 1024 * 1024:
                self.buffer = b''
                self.broken = True
                raise RuntimeError('Codex 返回内容过大。')
        raise TimeoutError(f'{method} 超时；未确认成功，请先检查当前标题再重试。')

    def __exit__(self, *_):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            for stream in (self.proc.stdin, self.proc.stdout):
                stream.close()
            self.selector.close()
