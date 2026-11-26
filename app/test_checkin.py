# -*- coding: utf-8 -*-
"""60 天打卡与结算模块的单元测试（独立临时目录，不碰 .runtime 正式数据）。"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checkin
import db


def at(base, offset_days=0, hour=20, minute=0, second=0):
    day = base + timedelta(days=offset_days)
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=checkin.TZ)


class CheckinTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'data'
        self.store = db.Store(self.root)
        self.base = datetime(2026, 11, 3, tzinfo=checkin.TZ).date()
        self.video_dir = Path(self._tmp.name) / 'videos'
        self.video_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # -------------------------------------------------- helpers
    def video(self, name='v1.mp4'):
        path = self.video_dir / name
        path.write_bytes(b'fake-video')
        return str(path)

    def profile(self, start=None, settings=None, name='林一'):
        return checkin.create_profile(self.store, self.root, name,
                                      (start or self.base).isoformat(), settings)

    def checkin_on(self, profile_id, day, name='v.mp4'):
        return checkin.submit_checkin(self.store, self.root, profile_id,
                                      {'video_path': self.video(name)},
                                      now=datetime(day.year, day.month, day.day, 20, 0, 0, tzinfo=checkin.TZ))

    # -------------------------------------------------- 时区 / 23:59 边界
    def test_timezone_boundary_2359_still_editable(self):
        profile = self.profile(start=self.base - timedelta(days=1))
        pid = profile['id']
        # UTC 15:59:59 = 上海 23:59:59，仍在当天
        late = datetime(2026, 11, 3, 15, 59, 59, tzinfo=timezone.utc)
        state = checkin.submit_checkin(self.store, self.root, pid,
                                       {'video_path': self.video()}, now=late)
        self.assertEqual(state['days'][1]['state'], 'checked')
        self.assertEqual(state['days'][1]['date'], '2026-11-03')
        # UTC 16:00:00 = 上海次日 00:00:00，锁死，不能删
        with self.assertRaises(ValueError):
            checkin.void_checkin(self.store, self.root, pid, '2026-11-03',
                                 now=datetime(2026, 11, 3, 16, 0, 0, tzinfo=timezone.utc))

    def test_cannot_backfill_yesterday(self):
        profile = self.profile(start=self.base - timedelta(days=3))
        with self.assertRaises(ValueError):
            checkin.void_checkin(self.store, self.root, profile['id'], '2026-11-02', now=at(self.base))

    # -------------------------------------------------- 跨日补算漏卡
    def test_reconcile_missed_days_is_idempotent(self):
        start = self.base - timedelta(days=9)
        profile = self.profile(start=start)
        pid = profile['id']
        for offset in (0, 1, 2, 4):
            self.checkin_on(pid, start + timedelta(days=offset), 'd%d.mp4' % offset)
        now = at(self.base, 0, 12)
        created = checkin.reconcile(self.root, profile, now=now)
        self.assertEqual(len(created), 5)  # 偏移 3/5/6/7/8
        again = checkin.reconcile(self.root, profile, now=now)
        self.assertEqual(again, [])
        state = checkin.state(self.store, self.root, pid, now=now)
        self.assertEqual(state['stats']['missed'], 5)
        self.assertEqual(state['stats']['checked'], 4)
        self.assertEqual(sorted(state['stats']['missed_dates']),
                         sorted([(start + timedelta(days=i)).isoformat() for i in (3, 5, 6, 7, 8)]))

    def test_today_is_pending_not_missed_before_midnight(self):
        profile = self.profile(start=self.base - timedelta(days=2))
        state = checkin.state(self.store, self.root, profile['id'], now=at(self.base, 0, 10))
        self.assertEqual(state['stats']['missed'], 2)
        self.assertEqual([d['state'] for d in state['days'][:3]], ['missed', 'missed', 'today'])

    # -------------------------------------------------- 哈希链
    def test_hash_chain_detects_tampering(self):
        profile = self.profile(start=self.base - timedelta(days=2))
        pid = profile['id']
        self.checkin_on(pid, self.base - timedelta(days=2), 'a.mp4')
        self.checkin_on(pid, self.base - timedelta(days=1), 'b.mp4')
        rows = checkin.load_events(self.root, pid)
        result = checkin.verify_chain(rows)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['records'], len(rows))
        self.assertEqual(rows[0]['prev'], checkin.GENESIS)
        self.assertIn('sync', rows[0])
        self.assertEqual(rows[0]['sync']['state'], 'local')

        path = checkin.log_path(self.root, pid)
        lines = path.read_text(encoding='utf-8').splitlines()
        tampered = json.loads(lines[1])
        tampered['payload']['note'] = '偷偷补一句'
        lines[1] = json.dumps(tampered, ensure_ascii=False)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        broken = checkin.verify_chain(checkin.load_events(self.root, pid))
        self.assertFalse(broken['ok'])
        self.assertEqual(broken['broken_at'], 2)

    # -------------------------------------------------- 结算金额
    def test_settlement_amounts_matrix(self):
        settings = dict(checkin.DEFAULT_SETTINGS)
        self.assertEqual(checkin.settle(0, settings)['refund'], 599)
        self.assertEqual(checkin.settle(3, settings)['deduction'], 150)
        self.assertEqual(checkin.settle(3, settings)['refund'], 449)
        self.assertEqual(checkin.settle(6, settings)['refund'], 299)

    def test_over_six_misses_two_modes(self):
        start = self.base - timedelta(days=8)
        profile = self.profile(start=start)
        pid = profile['id']
        self.checkin_on(pid, start, 'only.mp4')
        # 模拟到第 8 天结束：偏移 0..7 已过，只打了 1 天 → 7 次漏卡
        now = at(self.base, 0, 9)
        state = checkin.state(self.store, self.root, pid, now=now)
        self.assertEqual(state['stats']['missed'], 7)
        self.assertEqual(state['stats']['deduction'], 599)
        self.assertEqual(state['stats']['refund'], 0)
        self.assertTrue(state['stats']['deposit_forfeited'])

        checkin.update_profile(self.store, self.root, pid,
                               {'settings': {'forfeit_mode': 'capped'}})
        capped = checkin.state(self.store, self.root, pid, now=now)
        self.assertEqual(capped['stats']['deduction'], 350)
        self.assertEqual(capped['stats']['refund'], 249)
        self.assertFalse(capped['stats']['deposit_forfeited'])

    def test_capped_mode_never_exceeds_deposit(self):
        settings = dict(checkin.DEFAULT_SETTINGS, forfeit_mode='capped')
        self.assertEqual(checkin.settle(11, settings)['deduction'], 550)
        self.assertEqual(checkin.settle(20, settings)['deduction'], 599)
        self.assertEqual(checkin.settle(20, settings)['refund'], 0)

    # -------------------------------------------------- 一天一条 / 改 / 删
    def test_one_per_day_revise_and_void(self):
        profile = self.profile(start=self.base - timedelta(days=1))
        pid = profile['id']
        first = self.checkin_on(pid, self.base, 'a.mp4')
        self.assertTrue(first['stats']['today_checked'])
        second = self.checkin_on(pid, self.base, 'b.mp4')
        self.assertTrue(second['stats']['today_checked'])
        rows = checkin.load_events(self.root, pid)
        self.assertEqual([r['type'] for r in rows], ['profile_created', 'checkin', 'revise'])
        self.assertEqual(rows[-1]['payload']['video']['filename'], 'b.mp4')

        deleted = checkin.void_checkin(self.store, self.root, pid, self.base.isoformat(),
                                       now=at(self.base, 0, 20))
        self.assertFalse(deleted['stats']['today_checked'])
        self.assertEqual([r['type'] for r in checkin.load_events(self.root, pid)][-1], 'void')
        with self.assertRaises(ValueError):
            checkin.void_checkin(self.store, self.root, pid, self.base.isoformat(),
                                 now=at(self.base, 0, 20))

    def test_artifact_selection(self):
        profile = self.profile(start=self.base)
        path = self.video('artifact.mp4')
        artifact = self.store.put('artifact', {'kind': 'packaged_video', 'path': path,
                                               'filename': 'artifact.mp4', 'project_id': 'p1'})
        state = checkin.submit_checkin(self.store, self.root, profile['id'],
                                       {'artifact_id': artifact['id']}, now=at(self.base, 0))
        video = state['days'][0]['video']
        self.assertEqual(video['kind'], 'artifact')
        self.assertEqual(video['path'], path)
        self.assertEqual(len(checkin.outputs(self.store)), 1)

    def test_missing_file_is_rejected(self):
        profile = self.profile(start=self.base)
        with self.assertRaises(ValueError):
            checkin.submit_checkin(self.store, self.root, profile['id'],
                                   {'video_path': str(self.video_dir / 'nope.mp4')}, now=at(self.base))

    # -------------------------------------------------- 多主理人隔离
    def test_multiple_profiles_are_isolated(self):
        first = self.profile(start=self.base - timedelta(days=1), name='甲')
        second = self.profile(start=self.base - timedelta(days=1), name='乙')
        self.checkin_on(first['id'], self.base)
        a = checkin.state(self.store, self.root, first['id'], now=at(self.base, 1))
        b = checkin.state(self.store, self.root, second['id'], now=at(self.base, 1))
        self.assertEqual(a['stats']['checked'], 1)
        self.assertEqual(b['stats']['checked'], 0)
        self.assertEqual(len(checkin.list_profiles(self.store)), 2)
        self.assertEqual(a['profiles'][0]['name'], '甲')

    # -------------------------------------------------- 结算单导出
    def test_settlement_export_csv_and_html(self):
        start = self.base - timedelta(days=59)
        profile = self.profile(start=start)
        pid = profile['id']
        for offset in range(55):  # 漏 5 天（末尾 5 天不打卡）
            self.checkin_on(pid, start + timedelta(days=offset), 'day%02d.mp4' % offset)
        now = at(self.base, 1, 10)
        checkin.reconcile(self.root, profile, now=now)
        payload, csv_file, html_file = checkin.export_files(self.store, self.root, pid, now=now)
        self.assertEqual(payload['stats']['missed'], 5)
        self.assertEqual(payload['stats']['deduction'], 250)
        self.assertEqual(payload['stats']['refund'], 349)
        self.assertTrue(csv_file.is_file() and html_file.is_file())
        text = csv_file.read_text(encoding='utf-8-sig')
        self.assertIn('应退金额,349', text)
        self.assertIn('扣款合计,250', text)
        self.assertIn('哈希链校验,通过', text)
        page = html_file.read_text(encoding='utf-8')
        self.assertIn('349 元', page)
        self.assertIn('账本校验', page)
        self.assertIn('待双方书面确认', page)

    def test_days_cover_sixty_and_three_states(self):
        profile = self.profile(start=self.base - timedelta(days=20))
        state = checkin.state(self.store, self.root, profile['id'], now=at(self.base, 0, 9))
        self.assertEqual(len(state['days']), 60)
        self.assertEqual(state['days'][0]['index'], 1)
        self.assertEqual(state['days'][-1]['index'], 60)
        self.assertEqual(state['days'][20]['state'], 'today')
        self.assertEqual(state['days'][21]['state'], 'future')
        self.assertEqual(state['stats']['missed'], 20)
        self.assertEqual(state['stats']['remaining_misses'], 0)

    def test_profile_start_change_blocked_after_checkin(self):
        profile = self.profile(start=self.base - timedelta(days=1))
        self.checkin_on(profile['id'], self.base)
        with self.assertRaises(ValueError):
            checkin.update_profile(self.store, self.root, profile['id'],
                                   {'start_date': (self.base + timedelta(days=1)).isoformat()})

    def test_reconcile_all_reports_per_profile(self):
        a = self.profile(start=self.base - timedelta(days=3), name='甲')
        b = self.profile(start=self.base - timedelta(days=3), name='乙')
        self.checkin_on(a['id'], self.base)
        rows = checkin.reconcile_all(self.store, self.root, now=at(self.base, 0, 12))
        summary = {x['profile_id']: x['created'] for x in rows}
        self.assertEqual(summary[a['id']], 3)   # 前三天已过且未打卡
        self.assertEqual(summary[b['id']], 3)


if __name__ == '__main__':
    unittest.main()
