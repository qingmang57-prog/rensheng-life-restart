"""积分账本 / 授权 / 会员状态单元测试（真跑 SQLite，不 mock）。

覆盖：
  - 幂等：同键两次 -> 一条流水、只扣一次
  - hold -> settle 与 hold -> release 两条路径
  - 余额不足拒绝，余额不为负
  - 模板重复解锁、形象同 SHA 重复上传不重复扣
  - 会员赠送授权不产生扣费流水
  - 并发同幂等键 -> 只有一条 hold
  - 购买积分 24 个月后仍在；赠送积分有 30 天宽限，宽限后作废
  - 扣减顺序：先扣赠送，购买余额不动
  - 会员暂停 / 按未使用整月退计算
  - 价目 JSON 热更

运行（在 app 目录）：
  ..\\tools\\python\\python.exe -m unittest test_billing_ledger -v
"""
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

import billing_db
import billing_payee
import billing_quota
import billing_service
import redeem_service

DAY = 86400
CREDIT = 1000
T0 = 1760000000


class BillingTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.clock = {'now': T0}
        self.svc = billing_service.BillingService(
            db_path=self.root / 'billing.sqlite3',
            clock=lambda: self.clock['now'])

    def grant_purchase(self, credits, key='seed-purchase'):
        return self.svc.grant(credits * CREDIT, kind='purchase', idempotency_key=key)

    def grant_gift(self, credits, *, key, expires_at):
        return self.svc.grant(credits * CREDIT, kind='grant', expires_at=expires_at,
                              idempotency_key=key)

    def count_ledger(self, kind=None):
        with billing_db.connect(self.svc.db_path) as con:
            if kind:
                row = con.execute('SELECT COUNT(*) FROM credit_ledger'
                                  ' WHERE kind=?', (kind,)).fetchone()
            else:
                row = con.execute('SELECT COUNT(*) FROM credit_ledger').fetchone()
        return int(row[0])


class LedgerTest(BillingTestBase):
    def test_hold_idempotent_one_row_one_charge(self):
        self.grant_purchase(100)
        first = self.svc.hold(action='asr_minute', idempotency_key='asr-1', qty=3,
                              job_id='job-asr-1')
        second = self.svc.hold(action='asr_minute', idempotency_key='asr-1', qty=3,
                               job_id='job-asr-1')
        self.assertEqual(first['hold_id'], second['hold_id'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.count_ledger('hold'), 1)
        snap = self.svc.balance()
        self.assertEqual(snap['balance_milli'], 97000)
        self.assertEqual(snap['frozen_milli'], 3000)

    def test_hold_settle_refunds_difference(self):
        self.grant_purchase(100)
        hold = self.svc.hold(action='asr_minute', idempotency_key='asr-2', qty=5,
                             job_id='job-asr-2')
        self.assertEqual(hold['quoted_milli'], 5000)
        settled = self.svc.settle('job-asr-2', actual_qty=4)
        self.assertEqual(settled['charged_milli'], 4000)
        self.assertEqual(settled['refund_milli'], 1000)
        snap = self.svc.balance()
        self.assertEqual(snap['balance_milli'], 96000)
        self.assertEqual(snap['frozen_milli'], 0)
        again = self.svc.settle('job-asr-2', actual_qty=4)
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.count_ledger('settle'), 1)

    def test_hold_release_then_settle_rejected(self):
        self.grant_purchase(100)
        self.svc.hold(action='comment_angle', idempotency_key='angle-1',
                      job_id='job-angle-1')
        released = self.svc.release('job-angle-1', reason='任务失败')
        self.assertEqual(released['refund_milli'], 8000)
        snap = self.svc.balance()
        self.assertEqual(snap['balance_milli'], 100000)
        self.assertEqual(snap['frozen_milli'], 0)
        with self.assertRaises(billing_service.InvalidState):
            self.svc.settle('job-angle-1', actual_milli=8000)

    def test_insufficient_never_negative(self):
        self.grant_purchase(1)
        with self.assertRaises(billing_service.InsufficientCredits) as ctx:
            self.svc.hold(action='avatar_unlock', idempotency_key='avatar-poor')
        self.assertEqual(ctx.exception.code, 'insufficient_credits')
        self.assertEqual(ctx.exception.details['required_milli'], 30000)
        self.assertEqual(ctx.exception.details['available_milli'], 1000)
        snap = self.svc.balance()
        self.assertEqual(snap['balance_milli'], 1000)
        self.assertEqual(self.count_ledger(), 1)

    def test_ledger_pagination(self):
        for index in range(1, 4):
            self.grant_purchase(10, key='page-%d' % index)
        page = self.svc.ledger(limit=2, offset=0)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len(page['items']), 2)
        tail = self.svc.ledger(limit=2, offset=2)
        self.assertEqual(len(tail['items']), 1)

    def test_concurrent_same_key_one_hold(self):
        self.grant_purchase(100)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(self.svc.hold(
                    action='avatar_unlock', idempotency_key='same-key',
                    job_id='job-shared'))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['hold_id'], results[1]['hold_id'])
        self.assertEqual(sum(1 for item in results if item['duplicate']), 1)
        self.assertEqual(self.count_ledger('hold'), 1)
        snap = self.svc.balance()
        self.assertEqual(snap['balance_milli'], 70000)
        self.assertEqual(snap['frozen_milli'], 30000)


class CreditValidityTest(BillingTestBase):
    def test_purchased_survives_24_months(self):
        self.grant_purchase(100)
        self.clock['now'] = T0 + 24 * 30 * DAY
        snap = self.svc.balance()
        self.assertEqual(snap['purchased_milli'], 100000)
        self.assertEqual(snap['granted_milli'], 0)
        self.assertEqual(snap['expired_pending_milli'], 0)
        self.assertEqual(self.svc.sweep_expired()['voided'], [])
        hold = self.svc.hold(action='topic_generate', idempotency_key='after-2y')
        self.assertEqual(hold['quoted_milli'], 5000)
        # 购买积分永久有效：不允许带到期日
        with self.assertRaises(billing_service.ValidationError):
            self.svc.grant(1000, kind='purchase', idempotency_key='bad',
                           expires_at=T0 + DAY)

    def test_gift_grace_then_void(self):
        self.grant_gift(10, key='gift-10', expires_at=T0 + 30 * DAY)
        snap = self.svc.balance()
        self.assertEqual(snap['granted_milli'], 10000)
        self.assertEqual(snap['purchased_milli'], 0)
        self.assertIsNotNone(snap['next_expiring'])
        self.assertEqual(snap['next_expiring']['expiring_at'], T0 + 60 * DAY)

        # 到期后进入 30 天宽限，宽限内仍可花
        self.clock['now'] = T0 + 45 * DAY
        self.assertEqual(self.svc.balance()['granted_milli'], 10000)
        self.svc.hold(action='topic_generate', idempotency_key='grace-spend')
        self.assertEqual(self.svc.balance()['granted_milli'], 5000)

        # 宽限期过，余额查询触发作废：追加 void 流水，余额归零
        self.clock['now'] = T0 + 61 * DAY
        snap = self.svc.balance()
        self.assertEqual(snap['granted_milli'], 0)
        self.assertEqual(snap['expired_pending_milli'], 0)
        self.assertEqual(self.count_ledger('void'), 1)
        with self.assertRaises(billing_service.InsufficientCredits):
            self.svc.hold(action='asr_minute', idempotency_key='after-grace', qty=3)

    def test_expiring_soon_reports_reminder(self):
        self.grant_gift(10, key='gift-remind', expires_at=T0 + 15 * DAY)
        self.clock['now'] = T0 + 10 * DAY
        items = self.svc.expiring_soon(days=40)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['expiring_at'], T0 + 45 * DAY)
        self.assertTrue(items[0]['in_grace'] is False)
        self.assertEqual(items[0]['remaining_milli'], 10000)

    def test_gift_spent_first_purchase_untouched(self):
        self.grant_purchase(100, key='buy-100')
        self.grant_gift(10, key='gift-10', expires_at=T0 + 30 * DAY)
        snap = self.svc.balance()
        self.assertEqual(snap['granted_milli'], 10000)
        self.assertEqual(snap['purchased_milli'], 100000)

        hold = self.svc.hold(action='asr_minute', idempotency_key='order-1', qty=12,
                             job_id='job-order-1')
        self.assertEqual(hold['quoted_milli'], 12000)
        snap = self.svc.balance()
        self.assertEqual(snap['granted_milli'], 0)
        self.assertEqual(snap['purchased_milli'], 98000)
        self.assertEqual(snap['frozen_granted_milli'], 10000)
        self.assertEqual(snap['frozen_purchased_milli'], 2000)

        self.svc.settle('job-order-1')
        snap = self.svc.balance()
        self.assertEqual(snap['granted_milli'], 0)
        self.assertEqual(snap['purchased_milli'], 98000)
        self.assertEqual(snap['frozen_milli'], 0)


class EntitlementTest(BillingTestBase):
    def test_template_duplicate_not_regranted(self):
        self.grant_purchase(100)
        first = self.svc.purchase_entitlement(kind='template', ref_id='04-magazine',
                                              idempotency_key='buy-tpl')
        self.assertFalse(first['already'])
        self.assertEqual(first['charged_milli'], 49000)
        self.assertEqual(self.svc.balance()['balance_milli'], 51000)
        second = self.svc.purchase_entitlement(kind='template', ref_id='04-magazine',
                                               idempotency_key='buy-tpl')
        self.assertTrue(second['already'])
        self.assertEqual(second['charged_milli'], 0)
        self.assertEqual(self.count_ledger(), 3)  # grant + hold + settle
        self.assertEqual(len(self.svc.entitlements(kind='template')), 1)
        self.assertTrue(self.svc.has_entitlement(kind='template', ref_id='04-magazine'))

    def test_avatar_same_sha_not_double_charged(self):
        self.grant_purchase(100)
        sha = 'a' * 64
        first = self.svc.purchase_entitlement(kind='avatar', source_sha=sha,
                                              idempotency_key='buy-avatar-1')
        self.assertEqual(first['charged_milli'], 30000)
        self.assertEqual(first['action'], 'avatar_unlock')
        after_first = self.count_ledger()
        second = self.svc.purchase_entitlement(kind='avatar', source_sha=sha,
                                               idempotency_key='buy-avatar-2')
        self.assertTrue(second['already'])
        self.assertEqual(self.count_ledger(), after_first)
        self.assertEqual(self.svc.balance()['balance_milli'], 70000)
        self.assertTrue(self.svc.has_entitlement(kind='avatar', source_sha=sha))

    def test_membership_grant_has_no_ledger_row(self):
        result = self.svc.grant_entitlement(kind='template', ref_id='05-lefttext',
                                            grant_source='membership')
        self.assertFalse(result['already'])
        self.assertEqual(self.count_ledger(), 0)
        self.assertEqual(self.svc.balance()['balance_milli'], 0)
        with self.assertRaises(billing_service.ValidationError):
            self.svc.grant_entitlement(kind='template', ref_id='06-money',
                                       grant_source='membership', ledger_id=1)
        with self.assertRaises(billing_service.ValidationError):
            self.svc.grant_entitlement(kind='template', ref_id='07-banner',
                                       grant_source='purchase')
        self.assertEqual(self.count_ledger(), 0)


class MembershipTest(BillingTestBase):
    def test_year_pause_once_and_expiry_extends(self):
        self.svc.set_membership('year')
        view = self.svc.membership()
        self.assertEqual(view['plan'], 'year')
        self.assertEqual(view['expire_at'], T0 + 365 * DAY)
        paused = self.svc.pause_membership(60)
        self.assertTrue(paused['paused'])
        self.assertEqual(paused['plan'], 'free')
        self.clock['now'] = T0 + 30 * DAY
        resumed = self.svc.resume_membership()  # 提前恢复，只顺延实际暂停天数
        self.assertEqual(resumed['status'], 'active')
        self.assertEqual(resumed['expire_at'], T0 + 395 * DAY)
        self.assertEqual(resumed['pause_used'], 1)
        with self.assertRaises(billing_service.InvalidState):
            self.svc.pause_membership(10)

    def test_year_pause_auto_resume_after_60_days(self):
        self.svc.set_membership('year')
        self.svc.pause_membership(60)
        self.clock['now'] = T0 + 60 * DAY
        view = self.svc.membership()
        self.assertEqual(view['status'], 'active')
        self.assertEqual(view['expire_at'], T0 + 425 * DAY)
        self.assertEqual(view['pause_used'], 1)

    def test_pause_more_than_60_days_rejected(self):
        self.svc.set_membership('year')
        with self.assertRaises(billing_service.ValidationError):
            self.svc.pause_membership(61)

    def test_refund_unused_whole_months(self):
        self.svc.set_membership('year', expire_at=T0 + 180 * DAY)
        quote = self.svc.refund_quote()
        self.assertEqual(quote['total_months'], 12)
        self.assertEqual(quote['unused_months'], 6)
        self.assertEqual(quote['refund_cents'], 14950)
        self.assertEqual(quote['refund_cny'], '149.50')
        self.clock['now'] = T0 + 181 * DAY
        self.assertEqual(self.svc.refund_quote()['refund_cents'], 0)


class PricingHotReloadTest(BillingTestBase):
    def test_price_file_hot_reload(self):
        source = json.loads(billing_service.DEFAULT_PRICING.read_text(encoding='utf-8'))
        path = self.root / 'pricing.json'
        path.write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
        svc = billing_service.BillingService(db_path=self.root / 'hot.sqlite3',
                                             pricing_path=path,
                                             clock=lambda: self.clock['now'])
        self.assertEqual(svc.quote('comment_angle')['amount_milli'], 8000)
        source['actions']['comment_angle']['unit_milli'] = 9000
        source['note'] = 'hot-reload-probe'
        path.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(svc.quote('comment_angle')['amount_milli'], 9000)


class RenewalStackTest(BillingTestBase):
    """G2：续费从最远到期点叠加，1 天 + 30 天 = 31 天。"""

    def _events(self):
        with billing_db.connect(self.svc.db_path) as con:
            return con.execute("SELECT COUNT(*) FROM funnel_event WHERE event='membership_change'"
                               ).fetchone()[0]

    def test_renew_stacks_from_furthest_expiry(self):
        self.svc.set_membership('month')
        self.svc.set_membership('month')
        self.assertEqual(self.svc.membership()['expire_at'], T0 + 60 * DAY)

    def test_one_day_plus_30_days(self):
        self.svc.set_membership('month', days=1)
        self.svc.set_membership('month')
        self.assertEqual(self.svc.membership()['expire_at'], T0 + 31 * DAY)

    def test_expired_restarts_from_now(self):
        self.svc.set_membership('month')
        self.clock['now'] = T0 + 40 * DAY
        self.svc.set_membership('month')
        self.assertEqual(self.svc.membership()['expire_at'], T0 + 70 * DAY)

    def test_idempotent_key_extends_once(self):
        self.svc.set_membership('month', idempotency_key='mem-once')
        second = self.svc.set_membership('month', idempotency_key='mem-once')
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.svc.membership()['expire_at'], T0 + 30 * DAY)
        self.assertEqual(self._events(), 1)


class RedeemPlanTest(BillingTestBase):
    """G6：套餐卡密兑会员时长，复用 G2 叠加。"""

    def _new_code(self, **kwargs):
        return redeem_service.create_codes(batch='b-plan', db_path=self.svc.db_path,
                                           **kwargs)[0]['code']

    def test_plan_code_stacks_membership(self):
        first_code = self._new_code(plan='month', plan_days=30)
        first = redeem_service.redeem(first_code, user_id='u1', db_path=self.svc.db_path)
        self.assertTrue(first['ok'], first)
        self.assertEqual(first['plan'], 'month')
        expire_1 = self.svc.membership(user_id='u1')['expire_at']
        self.assertGreaterEqual(expire_1, int(time.time()) + 30 * DAY - 5)
        second_code = self._new_code(plan='month', plan_days=30)
        second = redeem_service.redeem(second_code, user_id='u1', db_path=self.svc.db_path)
        self.assertTrue(second['ok'], second)
        expire_2 = self.svc.membership(user_id='u1')['expire_at']
        self.assertEqual(expire_2 - expire_1, 30 * DAY)

    def test_same_plan_code_only_once(self):
        code = self._new_code(plan='month', plan_days=30)
        self.assertTrue(redeem_service.redeem(code, user_id='u1',
                                              db_path=self.svc.db_path)['ok'])
        again = redeem_service.redeem(code, user_id='u1', db_path=self.svc.db_path)
        self.assertFalse(again['ok'])
        self.assertEqual(again['code'], 'already_used')

    def test_credit_code_unchanged(self):
        code = self._new_code(credits=10)
        result = redeem_service.redeem(code, user_id='u1', db_path=self.svc.db_path)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.svc.balance(user_id='u1')['purchased_milli'], 10 * CREDIT)


class DynamicPlanTest(BillingTestBase):
    """G1：档位可增删、可上下架、可排序，价格全部来自价目表。"""

    def _svc_with(self, mutate):
        source = json.loads(billing_service.DEFAULT_PRICING.read_text(encoding='utf-8'))
        mutate(source)
        path = self.root / ('pricing-%d.json' % len(list(self.root.glob('pricing-*.json'))))
        path.write_text(json.dumps(source, ensure_ascii=False), encoding='utf-8')
        return billing_service.BillingService(db_path=self.root / 'billing.sqlite3',
                                              pricing_path=path,
                                              clock=lambda: self.clock['now'])

    def test_catalog_sorted_and_flags(self):
        catalog = self.svc.catalog()
        self.assertEqual([item['code'] for item in catalog['items']],
                         ['free', 'month', 'year'])
        self.assertFalse(catalog['watermark_enabled'])
        self.assertIn('拍板', catalog['watermark_note'])
        self.assertTrue(catalog['pricing_note'])
        self.assertTrue(all(item['pricing_status'] for item in catalog['items']))

    def test_add_and_disable_plan_without_code_change(self):
        def mutate(source):
            source['plans']['quarter'] = {
                'label': '季卡', 'price_cents': 12900, 'list_price_cents': 14700,
                'period': 'quarter', 'period_days': 90, 'days': 90, 'months': 3,
                'enabled': True, 'sort': 15, 'badge': '', 'benefits': ['90 天'],
                'pricing_status': 'pending_user_confirmation'}
            source['plans']['year']['enabled'] = False
        svc = self._svc_with(mutate)
        codes = [item['code'] for item in svc.catalog()['items']]
        self.assertEqual(codes, ['free', 'month', 'quarter', 'year'])
        view = svc.set_membership('quarter')
        self.assertEqual(view['plan'], 'quarter')
        self.assertEqual(view['expire_at'], T0 + 90 * DAY)
        with self.assertRaises(billing_service.ValidationError):
            svc.set_membership('year')
        year = [item for item in svc.catalog()['items'] if item['code'] == 'year'][0]
        self.assertFalse(year['enabled'])

    def test_free_limits_follow_config(self):
        def mutate(source):
            source['free_limits']['exports_per_month'] = 5
            source['free_limits']['max_height'] = 1080
        svc = self._svc_with(mutate)
        features = svc.membership()['features']
        self.assertEqual(features['exports_per_month'], 5)
        self.assertEqual(features['max_height'], 1080)

    def test_discount_fields_do_not_use_float(self):
        def mutate(source):
            source['plans']['month']['list_price_cents'] = 5900
        svc = self._svc_with(mutate)
        month = [item for item in svc.catalog()['items'] if item['code'] == 'month'][0]
        self.assertEqual(month['price_cny'], '49.00')
        self.assertEqual(month['list_price_cny'], '59.00')
        self.assertEqual(month['save_cents'], 1000)


class ExportNoCreditTest(BillingTestBase):
    """T4：导出只受条数限制，不扣积分、不写账本。"""

    def test_export_quota_does_not_touch_ledger(self):
        self.grant_purchase(50)
        before_rows = self.count_ledger()
        before_balance = self.svc.balance()['balance_milli']
        member = self.svc.membership()
        with billing_db.connect(self.svc.db_path) as con:
            with billing_db.transaction(con):
                ok, state = billing_quota.consume(
                    con, billing_service.LOCAL_USER_ID, 'export', self.svc.pricing(),
                    member['plan'], 'export:project-1', action='clip_pack')
        self.assertTrue(ok, state)
        self.assertEqual(self.count_ledger(), before_rows)
        self.assertEqual(self.svc.balance()['balance_milli'], before_balance)


class PayeeConfigTest(unittest.TestCase):
    """N1：二维码与说明只落数据目录；未配置可识别；清空后恢复未配置。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_data = os.environ.get('IP_DATA_DIR')
        os.environ['IP_DATA_DIR'] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_data is None:
            os.environ.pop('IP_DATA_DIR', None)
        else:
            os.environ['IP_DATA_DIR'] = self._old_data

    def test_unconfigured_upload_replace_clear(self):
        self.assertFalse(billing_payee.get()['configured'])
        info = billing_payee.save_info(title='月卡 49 元', contact='微信 kf-001',
                                       note='付款后发截图')
        self.assertEqual(info['title'], '月卡 49 元')
        raw = b'\x89PNG\r\n\x1a\n' + b'0' * 16
        saved = billing_payee.save_image(
            'data:image/png;base64,' + base64.b64encode(raw).decode())
        self.assertTrue(saved['configured'])
        image = Path(self._tmp.name) / 'payee' / 'qrcode.png'
        self.assertTrue(image.is_file())
        self.assertTrue(saved['image_url'].startswith('/api/billing/payee/image?v='))
        cleared = billing_payee.clear_image()
        self.assertFalse(cleared['configured'])
        self.assertFalse(image.exists())

    def test_rejects_non_image(self):
        with self.assertRaises(ValueError):
            billing_payee.save_image(
                'data:image/png;base64,' + base64.b64encode(b'not-an-image').decode())


class MigrationV5Test(unittest.TestCase):
    """v4 老库升 v5：档位枚举解除、redeem_code 加套餐字段，数据不丢。"""

    OLD = """
    CREATE TABLE membership (
        user_id TEXT PRIMARY KEY, plan TEXT NOT NULL DEFAULT 'free',
        status TEXT NOT NULL DEFAULT 'active', started_at INTEGER, expire_at INTEGER,
        paused_at INTEGER, paused_until INTEGER, pause_used INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL,
        CHECK (plan IN ('free','month','year')),
        CHECK (status IN ('active','paused','expired','cancelled')));
    CREATE TABLE redeem_code (
        id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT NOT NULL UNIQUE,
        code_tail TEXT NOT NULL, batch TEXT NOT NULL, credits_milli INTEGER NOT NULL,
        expires_at INTEGER, created_at INTEGER NOT NULL, created_by TEXT, used_at INTEGER,
        used_by TEXT, ledger_id INTEGER, note TEXT, CHECK (credits_milli > 0));
    INSERT INTO membership (user_id, plan, status, started_at, expire_at, updated_at)
        VALUES ('u-old', 'month', 'active', 1, 2, 3);
    INSERT INTO redeem_code (code_hash, code_tail, batch, credits_milli, created_at)
        VALUES ('hash-old', 'ABCD', 'old-batch', 5000, 4);
    """

    def test_old_db_upgrades_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'billing.sqlite3'
            con = sqlite3.connect(str(path))
            con.executescript(self.OLD)
            con.execute('PRAGMA user_version=4')
            con.commit()
            con.close()
            billing_db.ensure(path)
            with billing_db.connect(path) as con:
                cols = [row[1] for row in con.execute('PRAGMA table_info(redeem_code)')]
                self.assertIn('plan', cols)
                self.assertIn('plan_days', cols)
                old = con.execute("SELECT * FROM redeem_code WHERE code_hash='hash-old'"
                                  ).fetchone()
                self.assertEqual(old['credits_milli'], 5000)
                self.assertIsNone(old['plan'])
                member = con.execute("SELECT * FROM membership WHERE user_id='u-old'"
                                     ).fetchone()
                self.assertEqual(member['plan'], 'month')
                con.execute("INSERT INTO membership (user_id, plan, status, updated_at)"
                            " VALUES ('u-new', 'quarter', 'active', 5)")
                con.execute('INSERT INTO redeem_code (code_hash, code_tail, batch,'
                            ' credits_milli, plan, plan_days, created_at)'
                            " VALUES ('hash-new', 'EFGH', 'b2', 0, 'quarter', 90, 6)")
                con.commit()


if __name__ == '__main__':
    unittest.main(verbosity=2)
