# -*- coding: utf-8 -*-
"""P0-5 频控测试：预检只观察、enforce 才拦；重复标记 duplicate_skipped；日限给剩余额度。"""
import json
import os
import threading
import unittest
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT/('rate-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import contracts
import rate_limit
from db import Store


def stamp(delta_seconds=0):
    moment = datetime.now(timezone.utc)+timedelta(seconds=delta_seconds)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(str(TEST_PATH/('db-'+uuid.uuid4().hex)))
        self.rule = rate_limit.RULES['collect']
        self.clean_mode = os.environ.pop('IP_RATE_LIMIT_MODE', None)
        if self.clean_mode is not None:
            self.addCleanup(os.environ.__setitem__, 'IP_RATE_LIMIT_MODE', self.clean_mode)

    def use_rule(self, **changes):
        rate_limit.RULES['collect'] = {**self.rule, **changes}
        self.addCleanup(rate_limit.RULES.__setitem__, 'collect', self.rule)

    def event(self, status='ok', work_id='work_a', started_at=None, account_id='acc'):
        return self.store.put(rate_limit.KIND, {
            'id': 'rate_'+uuid.uuid4().hex[:16], 'action': 'collect', 'account_id': account_id,
            'work_id': work_id, 'status': status, 'started_at': started_at or stamp(),
            'finished_at': stamp() if status != 'running' else None})

    def test_first_run_allowed_with_full_quota(self):
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_a')
        self.assertTrue(gate['allowed'])
        self.assertFalse(gate['would_block'])
        self.assertEqual(gate['remaining_today'], 60)
        self.assertEqual(gate['mode'], 'observe')

    def test_duplicate_marked_in_observe_mode_but_not_blocked(self):
        event = self.event()
        rate_limit.finish(self.store, event, 'ok')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_a')
        self.assertTrue(gate['duplicate'])
        self.assertEqual(gate['code'], 'duplicate_skipped')
        self.assertIn('24 小时', gate['reason'])
        self.assertTrue(gate['would_block'])
        self.assertFalse(gate['blocked'])
        self.assertTrue(gate['allowed'])  # observe：只标记不拦
        self.assertEqual(gate['remaining_today'], 59)

    def test_enforce_mode_blocks_duplicate(self):
        os.environ['IP_RATE_LIMIT_MODE'] = 'enforce'
        event = self.event()
        rate_limit.finish(self.store, event, 'ok')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_a')
        self.assertTrue(gate['blocked'])
        self.assertFalse(gate['allowed'])

    def test_failed_collect_does_not_consume_quota_or_block_retry(self):
        event = self.event(status='running')
        rate_limit.finish(self.store, event, 'failed', message='平台要求登录')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_a')
        self.assertEqual(gate['remaining_today'], 60)
        self.assertIsNone(gate['code'])
        self.assertTrue(gate['allowed'])

    def test_daily_limit_reports_zero_remaining(self):
        self.use_rule(daily_limit=2, min_interval_seconds=0, dedupe_hours=0)
        self.event(started_at=stamp(-3600))
        event = self.event(started_at=stamp(-1800))
        rate_limit.finish(self.store, event, 'ok')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_b')
        self.assertEqual(gate['remaining_today'], 0)
        self.assertEqual(gate['code'], 'daily_limit_exceeded')
        self.assertIn('剩余 0 次', gate['reason'])
        os.environ['IP_RATE_LIMIT_MODE'] = 'enforce'
        self.assertTrue(rate_limit.precheck(self.store, 'collect', 'acc', 'work_b')['blocked'])

    def test_cooldown_after_recent_success(self):
        self.use_rule(dedupe_hours=0)
        event = self.event(started_at=stamp(-5))
        rate_limit.finish(self.store, event, 'ok')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_z')
        self.assertEqual(gate['code'], 'cooling')
        self.assertIn('冷却中', gate['reason'])

    def test_busy_when_same_account_running(self):
        self.event(status='running')
        gate = rate_limit.precheck(self.store, 'collect', 'acc', 'work_y')
        self.assertEqual(gate['code'], 'busy')
        self.assertIn('已有 1 个', gate['reason'])

    def test_publish_prepare_has_own_quota(self):
        gate = rate_limit.precheck(self.store, 'publish_prepare', 'acc')
        self.assertEqual(gate['remaining_today'], 10)
        with self.assertRaises(ValueError):
            rate_limit.precheck(self.store, 'unknown_action', 'acc')


class RateLimitAPITests(unittest.TestCase):
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

    def test_route_returns_quota_contract(self):
        with urllib.request.urlopen(self.base+'/api/feedback/rate-limit?action=collect',
                                    timeout=20) as res:
            self.assertEqual(res.status, 200)
            result = json.loads(res.read())
        for key in ('allowed', 'would_block', 'blocked', 'mode', 'code', 'reason',
                    'remaining_today', 'next_allowed_at', 'duplicate'):
            self.assertIn(key, result)
        self.assertEqual(result['mode'], 'observe')
        self.assertEqual(result['remaining_today'], 60)


if __name__ == '__main__':
    unittest.main()
