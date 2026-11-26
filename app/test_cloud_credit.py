# -*- coding: utf-8 -*-
"""t2 云端积分真源·客户端接线回归测试（可重复执行，stdlib-only）。

运行：
  "G:/极地信号科技/中控系统/_pkg_20261118/tools/python/python.exe" \
      "G:/极地信号科技/中控系统/_pkg_20261118/app/test_cloud_credit.py"

覆盖（对应任务验收）：
  1) 云端扣费：cloud-* 的 brain_ask hold/settle/release 全走 /api/brain/credit/*，
     准备制冻结（balance 只在 settle 减少），llm_chat 自动带 jobRef；
  2) 后台加减生效：admin grant/debit 后强制刷新/TTL 过期/下一次 hold 立即反映；
  3) 离线兜底：云端不可达/未登录 → 模型路径如实拦截，不扣本地、不赊账，
     余额回 stale 缓存；local-owner 本地链路不受影响；
  4) 本地商品不变：云端账号解锁封面/形象仍扣本地账本，云端钱包零流水；
  5) 防御性拒绝：cloud-* + brain_ask 落到本地 hold 直接抛 ValidationError；
  6) 关单纪律：settle 失败重试 3 次、穷尽写待对账、启动幂等重放销账。

云端侧用进程内假钱包服务严格按《积分真源设计_20261118.md》§2 契约打桩
（真实 ip-api 由实现B施工，本测试不触网、不碰生产）；数据目录全部临时，
跑完即删，重复执行互不污染。
"""
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

TOKEN = 'test-cloud-token-43'
ACCOUNT_ID = 43
CLOUD_USER = 'cloud-43'


# ---------------------------------------------------------------- 假云端钱包
class FakeWalletState:
    """按设计稿 §2 语义实现的内存钱包：单余额 + 准备制冻结 + jobRef 幂等。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.balance = 0
            self.price = 1                 # brain_ask_points：服务端报价
            self.holds = {}                # jobRef -> {points,status,action,qty}
            self.entries = []              # (kind, jobRef, delta_available)
            self.llm_calls = []            # /api/llm/chat 收到的 jobRef
            self.fail_settle = 0           # 注入：接下来 N 次 settle 返回 500
            self.fail_release = 0

    def held(self):
        return sum(h['points'] for h in self.holds.values() if h['status'] == 'held')

    def snapshot(self):
        return {'balance': self.balance, 'held': self.held(),
                'available': self.balance - self.held(),
                'brain_ask_points': self.price, 'updated_at': int(time.time())}


STATE = FakeWalletState()


class FakeCloudHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默，避免测试输出噪音
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if self.headers.get('Authorization') == 'Bearer ' + TOKEN:
            return True
        self._json(401, {'error': '请先登录。', 'code': 'UNAUTHORIZED'})
        return False

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8')) if length else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        if self.path == '/api/brain/credit/balance':
            if not self._authed():
                return
            with STATE.lock:
                self._json(200, STATE.snapshot())
            return
        self._json(404, {'error': 'not found', 'code': 'NOT_FOUND'})

    def do_POST(self):
        path = self.path
        body = self._body()
        if path.startswith('/api/brain/credit/') or path == '/api/llm/chat':
            if not self._authed():
                return
        with STATE.lock:
            if path == '/api/brain/credit/hold':
                self._hold(body)
            elif path == '/api/brain/credit/settle':
                self._settle(body)
            elif path == '/api/brain/credit/release':
                self._release(body)
            elif path == '/api/llm/chat':
                STATE.llm_calls.append({'jobRef': str(body.get('jobRef') or '')})
                self._json(200, {'content': 'fake answer', 'usage': {},
                                 'model': 'fake', 'cost_cny': 0})
            elif path == '/admin/wallet/grant':
                points = int(body.get('points') or 0)
                STATE.balance += points
                STATE.entries.append(('admin_grant', None, points))
                self._json(200, {'ok': True, **STATE.snapshot()})
            elif path == '/admin/wallet/debit':
                points = int(body.get('points') or 0)
                avail = STATE.balance - STATE.held()
                if points > avail:  # 设计稿 §2.7：debit 只扣 available，不侵吞 held
                    self._json(400, {'error': '可用余额不足。',
                                     'code': 'INSUFFICIENT_AVAILABLE'})
                    return
                STATE.balance -= points
                STATE.entries.append(('admin_debit', None, -points))
                self._json(200, {'ok': True, **STATE.snapshot()})
            else:
                self._json(404, {'error': 'not found', 'code': 'NOT_FOUND'})

    def _hold(self, body):
        job_ref = str(body.get('jobRef') or '')
        action = str(body.get('action') or '')
        qty = int(body.get('qty') or 1)
        if not job_ref or action != 'brain_ask' or qty < 1:
            self._json(400, {'error': '参数不合法。', 'code': 'INVALID_INPUT'})
            return
        points = STATE.price * qty
        existing = STATE.holds.get(job_ref)
        if existing is not None:
            if (existing['action'], existing['qty']) == (action, qty):
                self._json(200, {'ok': True, 'jobRef': job_ref,
                                 'heldPoints': existing['points'],
                                 'duplicate': True, **STATE.snapshot()})
            else:
                self._json(409, {'error': '同一冻结号不能换参数。',
                                 'code': 'WALLET_CONFLICT'})
            return
        avail = STATE.balance - STATE.held()
        if avail < points:
            self._json(402, {'error': '积分不足。', 'code': 'INSUFFICIENT_POINTS',
                             'required': points, 'available': avail,
                             'balance': STATE.balance})
            return
        STATE.holds[job_ref] = {'points': points, 'status': 'held',
                                'action': action, 'qty': qty}
        STATE.entries.append(('hold', job_ref, -points))
        self._json(200, {'ok': True, 'jobRef': job_ref, 'heldPoints': points,
                         'duplicate': False, **STATE.snapshot()})

    def _settle(self, body):
        if STATE.fail_settle > 0:
            STATE.fail_settle -= 1
            self._json(500, {'error': '模拟结算故障。', 'code': 'INTERNAL'})
            return
        job_ref = str(body.get('jobRef') or '')
        hold = STATE.holds.get(job_ref)
        if hold is None:
            self._json(404, {'error': '无此冻结。', 'code': 'HOLD_NOT_FOUND'})
            return
        if hold['status'] == 'released':
            self._json(409, {'error': '该冻结已释放。', 'code': 'HOLD_RELEASED'})
            return
        charged = hold['points']
        duplicate = hold['status'] == 'settled'
        if not duplicate:
            STATE.balance = max(0, STATE.balance - charged)
            hold['status'] = 'settled'
            STATE.entries.append(('settle', job_ref, 0))
        self._json(200, {'ok': True, 'jobRef': job_ref, 'chargedPoints': charged,
                         'refundedPoints': 0, 'duplicate': duplicate,
                         **STATE.snapshot()})

    def _release(self, body):
        if STATE.fail_release > 0:
            STATE.fail_release -= 1
            self._json(500, {'error': '模拟释放故障。', 'code': 'INTERNAL'})
            return
        job_ref = str(body.get('jobRef') or '')
        hold = STATE.holds.get(job_ref)
        if hold is None:
            self._json(404, {'error': '无此冻结。', 'code': 'HOLD_NOT_FOUND'})
            return
        if hold['status'] == 'settled':
            self._json(409, {'error': '该冻结已结算。', 'code': 'HOLD_SETTLED'})
            return
        duplicate = hold['status'] == 'released'
        if not duplicate:
            hold['status'] = 'released'
            STATE.entries.append(('release', job_ref, hold['points']))
        self._json(200, {'ok': True, 'jobRef': job_ref,
                         'releasedPoints': hold['points'],
                         'duplicate': duplicate, **STATE.snapshot()})


def _free_dead_port():
    """取一个确定没有监听的端口，模拟云端不可达（连接立即被拒）。"""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------- 测试基座
import billing_db          # noqa: E402
import billing_route       # noqa: E402
import billing_service     # noqa: E402
import cloud_gateway       # noqa: E402


class CloudCreditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FakeCloudHandler)
        cls.base_url = 'http://127.0.0.1:%d' % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cloud_credit_test_')
        self._env_backup = {k: os.environ.get(k)
                            for k in ('IP_DATA_DIR', 'IP_API_BASE', 'IP_KEY_SOURCE')}
        os.environ['IP_DATA_DIR'] = self.tmp
        os.environ['IP_API_BASE'] = self.base_url
        os.environ['IP_KEY_SOURCE'] = 'proxy'
        STATE.reset()
        billing_route._SERVICE['svc'] = None       # 惰性单例指向本用例的临时库
        cloud_gateway.wallet_cache_clear()
        cloud_gateway.clear_ask_job()
        self._delays_backup = cloud_gateway.WALLET_RETRY_DELAYS
        cloud_gateway.WALLET_RETRY_DELAYS = (0.0, 0.0, 0.0)  # 重试不真等 1/2/4s
        billing_service.set_request_user(CLOUD_USER)
        self._make_session()

    def tearDown(self):
        billing_service.set_request_user('')
        cloud_gateway.WALLET_RETRY_DELAYS = self._delays_backup
        cloud_gateway.wallet_cache_clear()
        cloud_gateway.clear_ask_job()
        billing_route._SERVICE['svc'] = None
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 工具
    def _make_session(self):
        """临时 workbench.sqlite3 里放一条有效云端会话（明文令牌，unseal 兼容）。"""
        path = Path(self.tmp) / 'workbench.sqlite3'
        con = sqlite3.connect(str(path))
        try:
            con.execute('''CREATE TABLE IF NOT EXISTS auth_cloud_sessions (
                token TEXT PRIMARY KEY, cloud_token TEXT NOT NULL,
                cloud_id INTEGER NOT NULL, username TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                last_seen_at REAL NOT NULL, last_verified_at REAL NOT NULL DEFAULT 0,
                user_agent TEXT NOT NULL DEFAULT '',
                entitlements TEXT NOT NULL DEFAULT '',
                vip_level INTEGER NOT NULL DEFAULT 0, vip_expires_at REAL,
                trial_started_at REAL, trial_ends_at REAL,
                membership_state TEXT NOT NULL DEFAULT '')''')
            now = time.time()
            con.execute('INSERT OR REPLACE INTO auth_cloud_sessions'
                        ' (token, cloud_token, cloud_id, username, created_at,'
                        '  expires_at, last_seen_at) VALUES (?,?,?,?,?,?,?)',
                        ('local-test-token', TOKEN, ACCOUNT_ID, 'tester',
                         now, now + 86400, now))
            con.commit()
        finally:
            con.close()

    def _admin(self, action, points):
        req = urllib.request.Request(
            self.base_url + '/admin/wallet/' + action,
            json.dumps({'points': points}).encode('utf-8'),
            {'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.loads(res.read().decode('utf-8'))

    def _local_rows(self, user_id, action=None):
        path = Path(self.tmp) / 'billing.sqlite3'
        if not path.is_file():
            return 0
        con = sqlite3.connect(str(path))
        try:
            if action:
                return con.execute('SELECT COUNT(*) FROM credit_ledger'
                                   ' WHERE user_id=? AND action=?',
                                   (user_id, action)).fetchone()[0]
            return con.execute('SELECT COUNT(*) FROM credit_ledger WHERE user_id=?',
                               (user_id,)).fetchone()[0]
        finally:
            con.close()

    def _local_balance(self, user_id):
        return billing_route.service().balance(user_id=user_id)['balance_milli']

    def _grant_local(self, user_id, credits):
        billing_route.service().grant(
            credits * billing_service.CREDIT_MILLI,
            idempotency_key='test-purchase:' + user_id,
            kind='purchase', action='redeem', note='测试购入', user_id=user_id)

    def _dead_cloud(self):
        os.environ['IP_API_BASE'] = 'http://127.0.0.1:%d' % _free_dead_port()


# ---------------------------------------------------------------- 用例
class TestCloudCharge(CloudCreditTest):
    def test_hold_settle_cloud_only_and_jobref(self):
        """云端扣费：hold 冻结不动账面，settle 才减；本地账本零 brain_ask 流水。"""
        self._admin('grant', 5)
        local_before = self._local_balance(CLOUD_USER)
        job, err = billing_route.start_brain_ask()
        self.assertIsNone(err)
        self.assertTrue(job.startswith(billing_route.CLOUD_JOB_PREFIX))
        self.assertEqual(STATE.balance, 5)          # 准备制：冻结不改 balance
        self.assertEqual(STATE.held(), 1)
        self.assertEqual(cloud_gateway.current_ask_job(), job)
        # providers 不改也能带 jobRef：llm_chat 自动取本线程登记的冻结号
        cloud_gateway.llm_chat([{'role': 'user', 'content': '你好'}])
        self.assertEqual(STATE.llm_calls[-1]['jobRef'], job)
        billing_route.finish_brain_ask(job, ok=True)
        self.assertEqual(STATE.balance, 4)
        self.assertEqual(STATE.held(), 0)
        self.assertEqual(STATE.holds[job]['status'], 'settled')
        self.assertEqual(cloud_gateway.current_ask_job(), '')
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)
        self.assertEqual(self._local_balance(CLOUD_USER), local_before)
        kinds = [k for k, _, _ in STATE.entries]
        self.assertEqual(kinds, ['admin_grant', 'hold', 'settle'])

    def test_release_on_failure(self):
        self._admin('grant', 3)
        job, err = billing_route.start_brain_ask()
        self.assertIsNone(err)
        billing_route.finish_brain_ask(job, ok=False)
        self.assertEqual(STATE.balance, 3)          # 失败全退，账面不动
        self.assertEqual(STATE.held(), 0)
        self.assertEqual(STATE.holds[job]['status'], 'released')
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)

    def test_hold_idempotent_replay(self):
        self._admin('grant', 5)
        first = cloud_gateway.credit_hold('brainaskc_replay_test')
        second = cloud_gateway.credit_hold('brainaskc_replay_test')
        self.assertFalse(first['duplicate'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(STATE.held(), 1)           # 只冻结一次


class TestAdminImmediate(CloudCreditTest):
    def test_grant_debit_visible_on_next_read(self):
        """后台加减即时生效：强制刷新/TTL 过期/下一次 hold 都读真源。"""
        self._admin('grant', 5)
        job, _ = billing_route.start_brain_ask()
        billing_route.finish_brain_ask(job, ok=True)   # balance 4
        self._admin('grant', 50)
        snap = cloud_gateway.credit_balance(force=True)
        self.assertEqual(snap['available'], 54)
        self.assertFalse(snap['stale'])
        summary = billing_route.summary(force=True)
        self.assertEqual(summary['cloud_wallet']['available'], 54)
        # TTL 过期后普通读取也回真源（把缓存时间拨回过去模拟 15s 到期）
        self._admin('debit', 54)
        with cloud_gateway._WALLET_LOCK:
            for cached in cloud_gateway._WALLET_CACHE.values():
                cached['fetched_at'] = 0
        self.assertEqual(cloud_gateway.credit_balance()['available'], 0)
        # available=0：下一次提问 hold 即拒，0 模型调用，话术含实时余额
        job2, err = billing_route.start_brain_ask()
        self.assertIsNone(job2)
        self.assertEqual(err['code'], 'insufficient_credits')
        self.assertIn('0 积分', err['message'])
        self.assertEqual(STATE.llm_calls, [])
        self.assertEqual(STATE.held(), 0)
        # 后台加分后无需等缓存：hold 直读服务端，立即放行
        self._admin('grant', 1)
        job3, err3 = billing_route.start_brain_ask()
        self.assertIsNone(err3)
        billing_route.finish_brain_ask(job3, ok=True)
        self.assertEqual(STATE.balance, 0)

    def test_insufficient_points_details(self):
        job, err = billing_route.start_brain_ask()   # 余额 0
        self.assertIsNone(job)
        self.assertEqual(err['code'], 'insufficient_credits')
        self.assertEqual(err['required_points'], 1)
        self.assertEqual(err['available_points'], 0)
        self.assertIn('联系管理员', err['message'])
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)


class TestOfflineFallback(CloudCreditTest):
    def test_unreachable_blocks_model_not_local(self):
        """云端不可达：模型路径如实拦（不扣本地不赊账），余额 stale，本地链路照常。"""
        self._admin('grant', 5)
        job, _ = billing_route.start_brain_ask()
        billing_route.finish_brain_ask(job, ok=True)   # 留下 balance=4 的内存缓存
        local_before = self._local_balance(CLOUD_USER)
        self._dead_cloud()
        job2, err = billing_route.start_brain_ask()
        self.assertIsNone(job2)
        self.assertEqual(err['code'], 'cloud_unreachable')
        self.assertIn('不扣积分', err['message'])
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)
        self.assertEqual(self._local_balance(CLOUD_USER), local_before)
        snap = cloud_gateway.credit_balance()
        self.assertTrue(snap['stale'])
        self.assertEqual(snap['available'], 4)         # 最近一次内存值
        self.assertTrue(billing_route.summary()['cloud_wallet']['stale'])
        # 本地兜底：local-owner 问答全链路在断云时照常（本地 quota + 本地账本）
        billing_service.set_request_user('')
        self._grant_local(billing_service.LOCAL_USER_ID, 10)
        local_job, local_err = billing_route.start_brain_ask()
        self.assertIsNone(local_err)
        self.assertTrue(local_job.startswith('brainask_'))
        billing_route.finish_brain_ask(local_job, ok=True)
        self.assertEqual(self._local_balance(billing_service.LOCAL_USER_ID),
                         9 * billing_service.CREDIT_MILLI)
        self.assertNotIn('cloud_wallet', billing_route.summary())

    def test_not_logged_in_cloud_user(self):
        """会话失效但请求上下文仍是 cloud-*：提示重新登录，不扣本地。"""
        con = sqlite3.connect(str(Path(self.tmp) / 'workbench.sqlite3'))
        try:
            con.execute('UPDATE auth_cloud_sessions SET expires_at=?', (time.time() - 1,))
            con.commit()
        finally:
            con.close()
        job, err = billing_route.start_brain_ask()
        self.assertIsNone(job)
        self.assertEqual(err['code'], 'cloud_unauthorized')
        self.assertIn('重新登录', err['message'])
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)
        snap = cloud_gateway.credit_balance()
        self.assertIsNone(snap['balance'])
        self.assertEqual(snap['error'], 'NOT_LOGGED_IN')


class TestLocalBoundary(CloudCreditTest):
    def test_cover_and_avatar_still_local(self):
        """云端账号的封面/形象解锁仍扣本地账本，云端钱包零流水。"""
        self._grant_local(CLOUD_USER, 200)
        self._admin('grant', 10)
        entries_before = len(STATE.entries)
        local_before = self._local_balance(CLOUD_USER)
        result = billing_route.charge_cover_template('tpl-test')
        self.assertTrue(result['ok'], result)
        self.assertEqual(int(result['charged_credits']), 49)  # 本地 API 回字符串额度
        self.assertEqual(self._local_balance(CLOUD_USER),
                         local_before - 49 * billing_service.CREDIT_MILLI)
        again = billing_route.charge_cover_template('tpl-test')
        self.assertTrue(again['ok'])
        self.assertTrue(again['already'])
        avatar = billing_route.service().purchase_entitlement(
            kind='avatar', source_sha='ab12' * 4,
            idempotency_key='avatar:%s:sha-test' % CLOUD_USER, note='测试形象')
        self.assertEqual(int(avatar['charged_credits']), 30)
        self.assertEqual(STATE.balance, 10)            # 云端钱包分毫未动
        self.assertEqual(len(STATE.entries), entries_before)
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)

    def test_local_owner_full_chain(self):
        """local-owner 问答仍走本地 quota + 本地账本，不触云端。"""
        billing_service.set_request_user('')
        self._grant_local(billing_service.LOCAL_USER_ID, 10)
        job, err = billing_route.start_brain_ask()
        self.assertIsNone(err)
        self.assertTrue(job.startswith('brainask_'))
        self.assertEqual(self._local_balance(billing_service.LOCAL_USER_ID),
                         9 * billing_service.CREDIT_MILLI)   # hold 先扣
        billing_route.finish_brain_ask(job, ok=True)
        self.assertEqual(self._local_balance(billing_service.LOCAL_USER_ID),
                         9 * billing_service.CREDIT_MILLI)   # settle 实扣 1
        self.assertEqual(STATE.entries, [])
        self.assertEqual(STATE.llm_calls, [])
        self.assertNotIn('cloud_wallet', billing_route.summary())

    def test_defensive_reject_local_hold(self):
        """cloud-* + brain_ask 落到本地 hold：防御性拒绝，不静默免费。"""
        svc = billing_route.service()
        with self.assertRaises(billing_service.ValidationError):
            svc.hold(action='brain_ask', idempotency_key='defensive-test',
                     job_id='job_defensive', user_id=CLOUD_USER)
        self.assertEqual(self._local_rows(CLOUD_USER, 'brain_ask'), 0)


class TestCloseDiscipline(CloudCreditTest):
    def test_settle_retry_then_pending_replay(self):
        """关单重试 3 次；穷尽写待对账；恢复后幂等重放销账，不重复扣。"""
        self._admin('grant', 5)
        job1, _ = billing_route.start_brain_ask()
        STATE.fail_settle = 2                      # 前两次 500，第三次成功
        billing_route.finish_brain_ask(job1, ok=True)
        self.assertEqual(STATE.balance, 4)
        self.assertFalse(cloud_gateway._pending_path().exists())
        job2, _ = billing_route.start_brain_ask()
        STATE.fail_settle = 99                     # 重试穷尽
        self.assertIsNone(billing_route.finish_brain_ask(job2, ok=True))
        pending = cloud_gateway._pending_path()
        self.assertTrue(pending.is_file())
        self.assertIn(job2, pending.read_text(encoding='utf-8'))
        self.assertEqual(STATE.balance, 4)         # 未结算，账面不动
        self.assertEqual(STATE.held(), 1)
        STATE.fail_settle = 0
        report = cloud_gateway.wallet_replay_pending()
        self.assertEqual(report['replayed'], 1)
        self.assertEqual(report['left'], 0)
        self.assertEqual(STATE.balance, 3)         # 只扣一次
        self.assertEqual(STATE.holds[job2]['status'], 'settled')
        self.assertFalse(pending.exists())
        again = cloud_gateway.wallet_replay_pending()
        self.assertEqual(again, {'replayed': 0, 'left': 0})

    def test_closed_codes_not_retried(self):
        """服务端已关单（HOLD_NOT_FOUND 等）视为完成，不进待对账。"""
        self.assertIsNone(cloud_gateway.wallet_close('brainaskc_ghost', ok=True))
        self.assertFalse(cloud_gateway._pending_path().exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)