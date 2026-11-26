# -*- coding: utf-8 -*-
"""主理人公测打卡 + 履约保证金台账的回归用例。

跑法：tools\\python\\python.exe app\\test_mentor_checkin.py
全部使用临时数据目录，不碰 .runtime 与 8791 的数据。
"""
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import Store  # noqa: E402
from mentor_checkin import MentorLedger, MISS_FORFEIT_COUNT, yuan  # noqa: E402

START = date(2026, 1, 1)
PERIOD_END = START + timedelta(days=59)          # 2026-03-01
AFTER_PERIOD = PERIOD_END + timedelta(days=1)    # 2026-03-02，周期已结束


class MentorCheckinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='mentor_test_'))
        self.store = Store(self.tmp)
        self.ledger = MentorLedger(self.store)
        self.seat = self.ledger.create_seat(1, '测试主理人', START.isoformat())['seat']
        self.seat_id = self.seat['id']

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fill(self, count, skip=(), prefix='w', start_offset=0):
        for offset in range(start_offset, start_offset + count):
            if offset in skip:
                continue
            day = START + timedelta(days=offset)
            self.ledger.record_checkin(self.seat_id, '%s%d' % (prefix, offset),
                                       day.isoformat(), 'douyin')

    def test_01_full_60_refunds_all(self):
        self._fill(60)
        progress = self.ledger.compute_deposit(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertEqual(progress['checked'], 60)
        self.assertEqual(progress['missed'], 0)
        self.assertEqual(progress['deduction_cents'], 0)
        self.assertEqual(progress['refundable_cents'], 59900)
        self.assertEqual(progress['refundable_yuan'], '599.00')
        self.assertTrue(progress['period_done'])

    def test_02_one_miss_refunds_549(self):
        self._fill(59)
        progress = self.ledger.compute_deposit(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertEqual(progress['checked'], 59)
        self.assertEqual(progress['missed'], 1)
        self.assertEqual(progress['deduction_cents'], 5000)
        self.assertEqual(progress['refundable_cents'], 54900)
        self.assertEqual(progress['refundable_yuan'], '549.00')
        self.assertEqual(progress['missed_dates'], [PERIOD_END.isoformat()])

    def test_03_seven_misses_refund_zero(self):
        self._fill(53)
        progress = self.ledger.compute_deposit(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertEqual(progress['checked'], 53)
        self.assertEqual(progress['missed'], 7)
        self.assertTrue(progress['forfeited'])
        self.assertEqual(progress['penalty_cents'], 35000)       # 名义扣减 7 x 50
        self.assertEqual(progress['deduction_cents'], 59900)     # 不予退还，全额扣
        self.assertEqual(progress['refundable_cents'], 0)
        self.assertEqual(progress['refundable_yuan'], '0.00')

    def test_04_waiver_removes_deduction(self):
        skip = 49
        self._fill(60, skip={skip})
        day = (START + timedelta(days=skip)).isoformat()
        result = self.ledger.grant_waiver(self.seat_id, day, '平台故障，作品被限流未发出')
        self.assertEqual(result['waiver']['waived'], 1)
        self.assertEqual(result['waiver']['waive_reason'], '平台故障，作品被限流未发出')
        progress = self.ledger.compute_deposit(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertEqual(progress['checked'], 59)
        self.assertEqual(progress['waived'], 1)
        self.assertEqual(progress['missed'], 0)
        self.assertEqual(progress['refundable_cents'], 59900)

    def test_05_same_day_counts_once(self):
        day = START.isoformat()
        first = self.ledger.record_checkin(self.seat_id, 'w_a', day, 'douyin')
        second = self.ledger.record_checkin(self.seat_id, 'w_b', day, 'douyin')
        self.assertFalse(first['dedup'])
        self.assertTrue(second['dedup'])
        self.assertEqual(len(self.ledger._records(self.seat_id)), 1)
        progress = self.ledger.compute_deposit(
            self.seat_id, as_of=(START + timedelta(days=1)).isoformat())
        self.assertEqual(progress['checked'], 1)

    def test_06_eleventh_seat_rejected(self):
        for no in range(2, 11):
            self.ledger.create_seat(no, '主理人%d' % no, START.isoformat())
        self.assertEqual(self.ledger._active_count(), 10)
        with self.assertRaises(ValueError):
            self.ledger.create_seat(11, '第十一人', START.isoformat())
        with self.assertRaises(ValueError):
            self.ledger.create_seat(1, '重复一号', START.isoformat())

    def test_07_license_follows_forfeit(self):
        self._fill(53)
        revoked = self.ledger.seat_license(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertFalse(revoked['licensed'])
        self.assertIn('停用', revoked['reason'])
        self._fill(7, prefix='late', start_offset=53)  # 补齐第 54-60 天，变成 60 条
        kept = self.ledger.seat_license(self.seat_id, as_of=AFTER_PERIOD.isoformat())
        self.assertTrue(kept['licensed'])

    def test_08_waiver_needs_reason_and_no_future(self):
        with self.assertRaises(ValueError):
            self.ledger.grant_waiver(self.seat_id, START.isoformat(), '   ')
        with self.assertRaises(ValueError):
            self.ledger.record_checkin(self.seat_id, 'w_future',
                                       (date(2026, 11, 4)).isoformat(), 'douyin')

    def test_09_money_is_integer_cents(self):
        progress = self.ledger.compute_deposit(self.seat_id, as_of=START.isoformat())
        for key in ('deposit_cents', 'penalty_cents', 'deduction_cents', 'refundable_cents'):
            self.assertIsInstance(progress[key], int, key)
        self.assertEqual(yuan(59900), '599.00')
        self.assertEqual(progress['deposit_yuan'], '599.00')

    def test_10_same_work_not_counted_twice(self):
        first = self.ledger.record_checkin(self.seat_id, 'same_work',
                                           START.isoformat(), 'douyin')
        self.assertFalse(first['dedup'])
        second = self.ledger.record_checkin(
            self.seat_id, 'same_work',
            (START + timedelta(days=1)).isoformat(), 'douyin')
        self.assertTrue(second['dedup'])
        self.assertEqual(len(self.ledger._records(self.seat_id)), 1)

    def test_11_progress_before_period_ends(self):
        self._fill(3)
        progress = self.ledger.compute_deposit(
            self.seat_id, as_of=(START + timedelta(days=3)).isoformat())
        self.assertEqual(progress['checked'], 3)
        self.assertEqual(progress['missed'], 0)     # 第 4 天还没结束，不算漏
        self.assertEqual(progress['days_settled'], 3)
        self.assertFalse(progress['period_done'])
        self.assertEqual(progress['refundable_cents'], 59900)


if __name__ == '__main__':
    unittest.main(verbosity=2)
