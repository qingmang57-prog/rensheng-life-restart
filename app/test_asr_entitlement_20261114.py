"""云端 ASR 账号权益（allow_cloud_asr）调度回归锁（2026-11-14）。

锁住三件事：
  1. allow_cloud_asr=false + 本机有云端 Key → providers.asr 不走云端、不发请求；
  2. allow_cloud_asr=true / 云端未下发 → 正常走云端；
  3. 后台线程（线程池/新线程）不继承 threading.local，必须靠 capture/apply 恢复权益，
     否则作业线程会把“账号未开通”误判成“可以走云端”。

全程用本机 mock ASR（IP_ASR_URL），不发真实云端请求、不花额度、不打印凭据。
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import billing_service
import providers


class _MockAsr(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        type(self).hits += 1
        body = json.dumps({'text': '今天', 'words': [
            {'text': '今天', 'start_ms': 0, 'end_ms': 500}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 不把请求打到测试输出
        pass


class AsrEntitlementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), _MockAsr)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d/asr' % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audio = Path(self.temp.name) / 'a.mp3'
        self.audio.write_bytes(b'ID3-fake-audio')
        _MockAsr.hits = 0
        self.env = mock.patch.dict(os.environ, {
            'IP_ASR_MODE': 'auto', 'IP_ASR_URL': self.url, 'IP_ASR_KEY': '',
            'IP_VOLC_ASR_KEY': '', 'IP_DISABLE_LOCAL_ASR': '1'})
        self.env.start()
        # 固定本地不可用，避免测试机是否装了模型影响走向，也不触发 preflight 写盘。
        self.preflight = mock.patch('local_asr.preflight', return_value={
            'ok': False, 'problems': ['找不到本地 ASR 解释器（测试模拟）'], 'warnings': [],
            'free_mb': 8000, 'need_mb': 3000, 'memory_low': False})
        self.preflight.start()
        # 测试机可能存着真实火山 Key；强制只走 mock 外部适配器，绝不触达真实云端。
        self.volc = mock.patch('providers.volc_asr', return_value=False)
        self.volc.start()
        billing_service.set_request_user('cloud-test-account')
        billing_service.set_entitlements({})

    def tearDown(self):
        billing_service.set_request_user('')
        billing_service.set_entitlements({})
        self.volc.stop()
        self.preflight.stop()
        self.env.stop()
        self.temp.cleanup()

    # ---------------------------------------------------------------- 调度
    def test_entitlement_absent_allows_cloud(self):
        """云端未下发 allow_cloud_asr → 按放行处理。"""
        billing_service.set_entitlements({})
        result = providers.asr(self.audio)
        self.assertEqual(result['asr_route'], 'cloud_external')
        self.assertEqual(_MockAsr.hits, 1)

    def test_false_blocks_cloud_even_with_key(self):
        """false + 本机有 Key → 不走云端，且说明是账号未开通。"""
        billing_service.set_entitlements({'allow_cloud_asr': False})
        with self.assertRaises(providers.ProviderMissing) as ctx:
            providers.asr(self.audio)
        msg = str(ctx.exception)
        self.assertIn('未开通云端语音识别', msg)
        self.assertIn('不是本机 Key 配置问题', msg)
        self.assertNotIn('云端转写还没配置', msg)
        self.assertEqual(_MockAsr.hits, 0, '账号未开通时不允许发出云端请求')

    def test_true_allows_cloud(self):
        billing_service.set_entitlements({'allow_cloud_asr': True})
        result = providers.asr(self.audio)
        self.assertEqual(result['asr_route'], 'cloud_external')
        self.assertEqual(_MockAsr.hits, 1)

    def test_cloud_mode_blocked(self):
        """IP_ASR_MODE=cloud 同样受权益控制。"""
        billing_service.set_entitlements({'allow_cloud_asr': False})
        with mock.patch.dict(os.environ, {'IP_ASR_MODE': 'cloud'}):
            with self.assertRaises(providers.ProviderMissing):
                providers.asr(self.audio)
        self.assertEqual(_MockAsr.hits, 0)

    # ---------------------------------------------------------------- 状态
    def test_available_and_status_reflect_switch(self):
        billing_service.set_entitlements({'allow_cloud_asr': False})
        caps = providers.available()
        self.assertFalse(caps['asr_ready_cloud'], 'false 时不能报云端就绪')
        self.assertFalse(caps['asr_cloud_allowed'])
        self.assertFalse(caps['asr'])

        billing_service.set_entitlements({'allow_cloud_asr': True})
        caps = providers.available()
        self.assertTrue(caps['asr_ready_cloud'])
        self.assertTrue(caps['asr_cloud_allowed'])
        self.assertTrue(caps['asr'])

    # ---------------------------------------------------------------- 线程快照
    def test_job_thread_restores_snapshot(self):
        """模拟 new_job：请求线程快照，runner 线程恢复；作业线程同样不发云端请求。"""
        billing_service.set_request_user('cloud-abc')
        billing_service.set_entitlements({'allow_cloud_asr': False})
        context = billing_service.capture_context()
        seen = {}

        def worker():
            billing_service.apply_context(context)
            seen['user'] = billing_service.request_user_id()
            try:
                providers.asr(self.audio)
            except providers.ProviderMissing as exc:
                seen['error'] = str(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(seen.get('user'), 'cloud-abc')
        self.assertIn('未开通云端语音识别', seen.get('error', ''))
        self.assertEqual(_MockAsr.hits, 0)

    def test_thread_without_snapshot_loses_entitlement(self):
        """反向锁定：不恢复快照的线程读不到权益（这就是本次要修的问题）。"""
        billing_service.set_entitlements({'allow_cloud_asr': False})
        seen = {}

        def worker():
            seen['user'] = billing_service.request_user_id()
            seen['allowed'] = providers.cloud_allowed()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(seen.get('user'), billing_service.LOCAL_USER_ID)
        self.assertTrue(seen.get('allowed'), '无快照线程确实会误判为放行')

    # ---------------------------------------------------------------- 文案
    def test_route_label_matches_real_branch(self):
        billing_service.set_entitlements({'allow_cloud_asr': True})
        self.assertIn('会上传', providers.asr_route_label(), 'auto 下本地不可用会走云端，不能说不实际上传')
        billing_service.set_entitlements({'allow_cloud_asr': False})
        self.assertNotIn('会上传', providers.asr_route_label())
        self.assertEqual(providers.asr_route_label(), '语音识别不可用')

    def test_route_label_local_ready_cloud_blocked(self):
        """本地可用但账号未开通云端：阶段文案不能承诺“失败会切云端”。"""
        with mock.patch('local_asr.available', return_value=True):
            billing_service.set_entitlements({'allow_cloud_asr': False})
            self.assertNotIn('会上传', providers.asr_route_label())
            self.assertIn('不上传云端', providers.asr_route_label())
            billing_service.set_entitlements({'allow_cloud_asr': True})
            self.assertIn('会上传', providers.asr_route_label())

    def test_unavailable_reason_keeps_memory_detail(self):
        billing_service.set_entitlements({'allow_cloud_asr': False})
        local = {'ok': False, 'problems': ['内存不足：可用 1200 MB，本地转写至少需要 3000 MB'],
                 'free_mb': 1200, 'need_mb': 3000, 'memory_low': True}
        with mock.patch('local_asr.preflight', return_value=local):
            reason = providers.asr_unavailable_reason()
        self.assertIn('内存不足：可用 1200 MB', reason)
        self.assertIn('3000 MB', reason)
        self.assertIn('未开通云端语音识别', reason)


if __name__ == '__main__':
    unittest.main(verbosity=2)
