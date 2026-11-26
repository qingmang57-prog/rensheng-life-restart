# -*- coding: utf-8 -*-
"""导出失败退款 / 缓存自愈 回归防线（2026-11-16）。

覆盖两件事：
1) 四件套导出、成片下载在"已计次"之后失败，次数必须退回（用量不涨）。
2) export_cache 里的副本损坏时，下一次导出要删掉重建并成功，而不是永久失败。

跑法：python -m unittest test_export_refund -v
"""
import json
import os
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent / '.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT / uuid.uuid4().hex
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
os.environ['IP_DISABLE_LOCAL_ASR'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)

import app  # noqa: E402
from db import Store  # noqa: E402
import aigc_mark  # noqa: E402

PARTS = [
    {'id': 'hook_01', 'role': 'hook', 'label': '开头', 'text': '今天聊一个关于时间管理的小方法。'},
    {'id': 'content_01', 'role': 'content', 'label': '主体',
     'text': '先把要做的事情写下来，再按重要程度排序。'},
]


def make_mp4(path):
    body = b'isom' + (0).to_bytes(4, 'big') + b'isommp42'
    data = (8 + len(body)).to_bytes(4, 'big') + b'ftyp' + body
    data += (8).to_bytes(4, 'big') + b'moov'
    data += (72).to_bytes(4, 'big') + b'mdat' + b'\x00' * 64
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ExportRefundTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = 'http://127.0.0.1:%s' % cls.server.server_port
        store = app.STORE
        now = app.db_now() if hasattr(app, 'db_now') else None
        for pid, with_video in (('tr_delivery', False), ('tr_download', False),
                                ('tr_cache', True)):
            vid, aid = 'ver_' + pid, 'art_' + pid
            video = TEST_PATH / 'artifacts' / (aid + '.mp4')
            if with_video:
                make_mp4(video)
            store.put('script_version', {
                'project_id': pid, 'parts': PARTS, 'draft_revision': 2,
                'confirmed_at': now or 0, 'source_id': 'src_tr', 'source_revision': 1,
                'source_snapshot': '', 'compliance': {'ok': True}, 'compliance_waivers': []},
                vid)
            store.put('artifact', {
                'project_id': pid, 'script_version_id': vid, 'asset_id': None,
                'kind': 'packaged_video', 'path': str(video), 'filename': '成片.mp4',
                'info': {'width': 640, 'height': 360, 'duration_ms': 1000}}, aid)
            store.put('project', {
                'title': '退款测试' + pid, 'source_id': 'src_tr', 'draft': PARTS,
                'draft_revision': 2, 'is_test': True, 'draft_changed': False,
                'confirmed_version_id': vid, 'stage': 'ready_to_export', 'mode': 'script',
                'knowledge_ids': [], 'tags': [], 'director': [], 'title_origin': 'manual',
                'publication_pack': {'script_version_id': vid, 'selected_title': '测试标题',
                                     'caption': '测试正文。', 'tags': ['测试']},
                'reviewed_artifact_id': aid}, pid)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def usage(self):
        path = TEST_PATH / 'billing.sqlite3'
        if not path.is_file():
            return 0, 0
        con = sqlite3.connect(str(path))
        row = con.execute("SELECT COALESCE(SUM(used),0) FROM quota_usage"
                          " WHERE user_id='local-owner' AND feature='export'").fetchone()
        events = con.execute("SELECT COUNT(*) FROM quota_event"
                             " WHERE user_id='local-owner'").fetchone()[0]
        con.close()
        return row[0], events

    def test_1_failed_delivery_refunds_quota(self):
        before = self.usage()
        status, raw = self.get('/api/projects/tr_delivery/export?include_video=1')
        after = self.usage()
        self.assertEqual(status, 400, raw[:300])
        self.assertIn('成片文件不存在', json.loads(raw.decode('utf-8')).get('error', ''))
        self.assertEqual(after, before, '四件套导出失败后次数没有退回：%s -> %s' % (before, after))

    def test_2_failed_download_refunds_quota(self):
        before = self.usage()
        status, raw = self.get('/api/artifacts/art_tr_download?download=1')
        after = self.usage()
        self.assertEqual(status, 400, raw[:300])
        self.assertIn('成片文件不存在', json.loads(raw.decode('utf-8')).get('error', ''))
        self.assertEqual(after, before, '成片下载失败后次数没有退回：%s -> %s' % (before, after))

    def test_3_corrupt_cache_rebuilds(self):
        before = self.usage()
        status, raw = self.get('/api/projects/tr_cache/export?include_video=1')
        self.assertEqual(status, 200, raw[:300])
        cache = sorted((TEST_PATH / 'export_cache').glob('*.mp4'))
        self.assertTrue(cache, '第一次导出没有生成缓存副本')
        cache[0].write_bytes((4).to_bytes(4, 'big') + b'ftyp' + b'\x00' * 32)  # 损坏副本
        status, raw = self.get('/api/projects/tr_cache/export?include_video=1')
        after = self.usage()
        self.assertEqual(status, 200, '缓存损坏后没有自愈重建：%s' % raw[:300])
        self.assertEqual(after[0] - before[0], 1,
                         '同一作品重复导出按幂等只计一次：%s -> %s' % (before, after))
        info = aigc_mark.read(cache[0])
        self.assertTrue(info['present'], '重建后的缓存副本缺少 AIGC 标识')
        video = None
        with zipfile.ZipFile(BytesIO(raw)) as z:
            names = [n for n in z.namelist() if n.lower().endswith('.mp4')]
            video = z.read(names[0]) if names else None
        self.assertTrue(video, '交付包内没有成片')
        marked = TEST_PATH / 'downloaded.mp4'
        marked.write_bytes(video)
        self.assertTrue(aigc_mark.read(marked)['present'], '交付包内成片缺少 AIGC 标识')


if __name__ == '__main__':
    unittest.main(verbosity=2)
