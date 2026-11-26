# -*- coding: utf-8 -*-
"""P0-6 定时回采测试：只提醒不代发；成功顺延 24h；失败 60min 重试一次后转 needs_action。"""
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
TEST_PATH = TEST_ROOT/('schedule-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import collect_schedule
import contracts
from db import Store

WORK = {'id': 'platform_work_demo', 'platform_account_id': 'account_demo'}


class CollectScheduleTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(str(TEST_PATH/('db-'+uuid.uuid4().hex)))
        self.now = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)

    def schedule(self):
        return collect_schedule.upsert_after_publish(self.store, WORK, now=self.now)

    def test_upsert_is_idempotent_and_due_in_15_minutes(self):
        first = self.schedule()
        second = collect_schedule.upsert_after_publish(self.store, WORK, now=self.now)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['status'], 'scheduled')
        self.assertEqual(first['due_at'], '2026-11-16T06:15:00Z')
        self.assertEqual(len(self.store.list(collect_schedule.KIND, 100)), 1)
        self.assertEqual(collect_schedule.due_items(self.store, self.now), [])
        due = collect_schedule.due_items(self.store, self.now+timedelta(minutes=16))
        self.assertEqual([x['id'] for x in due], [first['id']])

    def test_success_reschedules_daily_and_records_run(self):
        item = self.schedule()
        result = collect_schedule.run(
            self.store, item['id'], now=self.now+timedelta(minutes=20),
            collect=lambda work_id: {'snapshots_created': 1, 'comments_created': 0})
        self.assertTrue(result['ok'])
        saved = result['schedule']
        self.assertEqual(saved['status'], 'scheduled')
        self.assertEqual(saved['attempt'], 0)
        self.assertEqual(saved['last_run_at'], '2026-11-16T06:20:00Z')
        self.assertEqual(saved['due_at'], '2026-11-17T06:20:00Z')
        self.assertIsNone(saved['last_error'])

    def test_first_failure_retries_in_60_minutes(self):
        item = self.schedule()

        def boom(work_id):
            raise RuntimeError('浏览器未就绪：找不到可用的 Chromium')

        result = collect_schedule.run(self.store, item['id'],
                                      now=self.now+timedelta(minutes=20), collect=boom)
        self.assertFalse(result['ok'])
        self.assertEqual(result['schedule']['status'], 'retry')
        self.assertEqual(result['schedule']['attempt'], 1)
        self.assertEqual(result['schedule']['due_at'], '2026-11-16T07:20:00Z')
        self.assertIn('浏览器未就绪', result['message'])

    def test_second_failure_needs_action_but_manual_rerun_works(self):
        item = self.schedule()
        collect_schedule.run(self.store, item['id'], now=self.now+timedelta(minutes=20),
                             collect=lambda work_id: (_ for _ in ()).throw(RuntimeError('第一次失败')))
        second = collect_schedule.run(self.store, item['id'], now=self.now+timedelta(minutes=80),
                                      collect=lambda work_id: (_ for _ in ()).throw(RuntimeError('第二次失败')))
        self.assertEqual(second['schedule']['status'], 'needs_action')
        self.assertEqual(second['schedule']['attempt'], 2)
        self.assertEqual(collect_schedule.due_items(self.store, self.now+timedelta(days=1)), [])
        rerun = collect_schedule.run(self.store, item['id'], now=self.now+timedelta(days=2),
                                     collect=lambda work_id: {'snapshots_created': 1})
        self.assertTrue(rerun['ok'])
        self.assertEqual(rerun['schedule']['status'], 'scheduled')
        self.assertEqual(rerun['schedule']['attempt'], 0)

    def test_duplicate_skip_keeps_schedule_and_does_not_consume_attempt(self):
        item = self.schedule()
        result = collect_schedule.run(
            self.store, item['id'], now=self.now+timedelta(minutes=20),
            collect=lambda work_id: {'skipped': True, 'skip_code': 'duplicate_skipped',
                                     'message': '24 小时内已采集'})
        self.assertTrue(result['ok'])
        self.assertTrue(result['skipped'])
        self.assertEqual(result['schedule']['attempt'], 0)
        self.assertEqual(result['schedule']['status'], 'scheduled')
        self.assertEqual(result['schedule']['last_skipped'], 'duplicate_skipped')

    def test_summary_says_reminder_only(self):
        self.schedule()
        summary = collect_schedule.summary(self.store, self.now+timedelta(minutes=16))
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['due_count'], 1)
        self.assertFalse(summary['auto_run'])
        self.assertIn('不代发', summary['note'])


class CollectScheduleAPITests(unittest.TestCase):
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

    def test_list_route_contract(self):
        with urllib.request.urlopen(self.base+'/api/collect/schedules', timeout=20) as res:
            self.assertEqual(res.status, 200)
            data = json.loads(res.read())
        for key in ('items', 'total', 'due_count', 'counts', 'auto_run', 'note'):
            self.assertIn(key, data)
        self.assertFalse(data['auto_run'])


if __name__ == '__main__':
    unittest.main()
