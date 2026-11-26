# -*- coding: utf-8 -*-
"""P0-1 统一平台会话层测试：ok / missing / expired / need_rescan 四态与不泄露凭据。"""
import json
import os
import threading
import unittest
import urllib.request
import uuid
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT/('session-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import platform_session

COOKIE_FILE = TEST_PATH/'douyin_cookie.json'
SESSION_FILE = TEST_PATH/'publish'/'douyin-session.json'
SECRET = 'SECRET_TOKEN_SHOULD_NOT_LEAK_9f3a'


def write_cookie(cookies):
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding='utf-8')


def write_session(cookies):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps({'cookies': cookies, 'origins': []}, ensure_ascii=False),
                            encoding='utf-8')


def cookie(name='sessionid', value=SECRET, expires=4102444800):
    return {'name': name, 'value': value, 'domain': '.douyin.com', 'path': '/',
            'expires': expires}


class PlatformSessionUnitTests(unittest.TestCase):
    def setUp(self):
        for path in (COOKIE_FILE, SESSION_FILE):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()

    def state(self):
        return platform_session.state('douyin', '本人抖音号',
                                      cookie_file=COOKIE_FILE, session_file=SESSION_FILE)

    def test_missing_when_no_files(self):
        result = self.state()
        self.assertEqual(result['status'], 'missing')
        self.assertIn('没有登录态', platform_session.describe(result))
        self.assertEqual(result['account_name'], '本人抖音号')

    def test_ok_when_both_sessions_valid(self):
        write_cookie([cookie(expires=4102444800)])
        write_session([cookie(expires=4102444800)])
        result = self.state()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['reason'], '')
        self.assertTrue(result['last_checked_at'])
        self.assertTrue(result['expires_at'].startswith('2100'))

    def test_expired_when_expiry_in_past(self):
        write_cookie([cookie(expires=1600000000)])
        write_session([cookie(expires=1600000000)])
        result = self.state()
        self.assertEqual(result['status'], 'expired')
        self.assertIn('过期', platform_session.describe(result))

    def test_need_rescan_when_corrupt(self):
        write_cookie([cookie()])
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text('{不是 JSON', encoding='utf-8')
        result = self.state()
        self.assertEqual(result['status'], 'need_rescan')
        self.assertIn('重新扫码', platform_session.describe(result))

    def test_need_rescan_when_sessionid_missing(self):
        write_cookie([cookie()])
        write_session([cookie(name='ttwid', value='x')])
        result = self.state()
        self.assertEqual(result['status'], 'need_rescan')
        self.assertIn('sessionid', result['reason'])

    def test_state_never_contains_cookie_value(self):
        write_cookie([cookie()])
        write_session([cookie()])
        dump = json.dumps(self.state(), ensure_ascii=False)+platform_session.describe(self.state())
        self.assertNotIn(SECRET, dump)

    def test_unsupported_platform_says_pending(self):
        result = platform_session.state('xiaohongshu', '小红书号')
        self.assertEqual(result['status'], 'missing')
        self.assertIn('待验收', result['reason'])


class PlatformSessionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base+path, timeout=20) as res:
            self.assertEqual(res.status, 200)
            return json.loads(res.read())

    def test_route_contract(self):
        for path in (COOKIE_FILE, SESSION_FILE):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                path.unlink()
        COOKIE_FILE.write_text(json.dumps([cookie()]), encoding='utf-8')
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps({'cookies': [cookie()]}, ensure_ascii=False),
                                encoding='utf-8')
        result = self.get('/api/platform/session?platform=douyin')
        for key in ('status', 'account_name', 'last_checked_at', 'expires_at', 'reason'):
            self.assertIn(key, result)
        self.assertEqual(result['status'], 'ok')
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))
        other = self.get('/api/platform/session?platform=kuaishou')
        self.assertEqual(other['status'], 'missing')


if __name__ == '__main__':
    unittest.main()
