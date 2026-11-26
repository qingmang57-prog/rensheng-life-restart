# -*- coding: utf-8 -*-
"""密钥目录在「盘根下一层」布局下的回归测试（2026-11-16，修 IndexError: 1 后留下）。

背景：包按《首次运行说明》解到 `D:\\人生重启系统` 这种只有一层父目录的位置时，
旧实现写死 `BASE.parents[1]`；`D:\\X` 的 `parents` 只有 1 项，取 [1] 直接抛
`IndexError: 1`，导致 `GET /api/asr/status` 返回 500。
设置页因此读不到转写状态（本地转写与出片主链不受影响）。

覆盖：
  - 浅层布局（包根 = D:\\_asr_repro）不抛 IndexError，read() 返回「未配置」
  - 包根就是盘符根（D:\\）也不抛
  - 深层布局（真实源码树）行为不变
  - `/api/asr/status` 的组成函数在浅层布局下不抛（providers.available / asr_volc.setup_help）
  - 可选：真实 HTTP 断言——设置 IP_SHALLOW_APP_DIR 指向浅层解包目录时，
    起服务打 /api/asr/status 必须 200

运行（在 app 目录）：
  ..\\tools\\python\\python.exe -m unittest test_secret_store_shallow -v
  set IP_SHALLOW_APP_DIR=D:\\_asr_repro\\app   # 可选，跑真实 HTTP 那条
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import secret_store

SHALLOW = Path('D:/_asr_repro')   # 盘根下一层：parents 只有 1 项


class ShallowLayoutTest(unittest.TestCase):
    """不依赖 HTTP 的快速回归：直接验证基准路径与读取行为。"""

    def setUp(self):
        self._base = secret_store.BASE

    def tearDown(self):
        secret_store.BASE = self._base

    def test_shallow_layout_no_index_error(self):
        secret_store.BASE = SHALLOW
        self.assertEqual(len(SHALLOW.parents), 1, 'D:\\_asr_repro 的 parents 应只有盘符根一项')
        path = secret_store.secrets_dir()          # 旧实现在这一行抛 IndexError: 1
        self.assertIsInstance(path, Path)
        self.assertEqual(secret_store.read(('IP_NOT_EXIST_KEY',), 'none.key'), ('', ''))

    def test_drive_root_package_no_index_error(self):
        secret_store.BASE = Path('D:/')            # 极端布局：包根就是盘符根
        self.assertIsInstance(secret_store.secrets_dir(), Path)

    def test_deep_layout_unchanged(self):
        path = secret_store.secrets_dir()          # 真实源码树：深层目录
        self.assertIsInstance(path, Path)
        self.assertGreaterEqual(len(path.parts), 2)

    def test_asr_status_pieces_shallow(self):
        """/api/asr/status 里唯一碰密钥目录的就是这两个函数。"""
        secret_store.BASE = SHALLOW
        import asr_volc
        import providers
        caps = providers.available()
        self.assertIsInstance(caps, dict)
        self.assertIn('llm_key_set', caps)
        self.assertIsInstance(asr_volc.setup_help(), dict)


@unittest.skipUnless(os.getenv('IP_SHALLOW_APP_DIR'), '需要设置 IP_SHALLOW_APP_DIR 指向浅层解包目录')
class ShallowHttpTest(unittest.TestCase):
    """真跑：在浅层解包目录起服务，断言接口 200（不是只测函数）。"""

    def test_asr_status_200(self):
        appdir = Path(os.environ['IP_SHALLOW_APP_DIR']).resolve()
        self.assertTrue((appdir / 'app.py').is_file(), '%s 下没有 app.py' % appdir)
        self.assertEqual(len(appdir.parent.parents), 1, '这里不是盘根下一层，测不出浅层布局')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        data = Path(tempfile.mkdtemp(prefix='shallow-asr-'))
        env = dict(os.environ, IP_DATA_DIR=str(data), IP_KEY_SOURCE='proxy')
        log = open(data / 'run.log', 'w', encoding='utf-8')
        proc = subprocess.Popen([sys.executable, str(appdir / 'app.py'), '--port', str(port)],
                                cwd=str(appdir.parent), env=env, stdout=log,
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        try:
            base = 'http://127.0.0.1:%d' % port
            for _ in range(120):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(base + '/', timeout=10) as r:
                        if r.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    continue
            else:
                self.fail('服务 120 秒内未起来')
            with urllib.request.urlopen(base + '/api/asr/status', timeout=30) as r:
                self.assertEqual(r.status, 200)
                body = json.loads(r.read().decode('utf-8'))
            self.assertIn('available', body)
            self.assertIn('local', body)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            shutil.rmtree(data, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
