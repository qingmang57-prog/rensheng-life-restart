"""BillingService: the only public entry point. Implements 设计稿 §四 end to end.

    submit  : 1 提交(必带 idempotency_key) -> 2 校验 -> 3 hold -> 4 签发 ticket -> 8 幂等
    consume : 5 执行器本地校验签名+TTL，服务端再核对一次并 issued->consumed
    report  : 6 回报 result（发布类必须 platform_work_id + work_url 同时存在）
    settle  : 7 成功 -> settle（按实际用量重算，多退少补）
    release : 7 失败 -> release（全额退分）
    expire  : 7 无响应 -> TTL 扫描自动 release
    reclaim : 对账，ledger 求和覆盖 balance 缓存

Storage is a local SQLite file for P0. A cloud implementation only has to keep the same
schema and method surface; the ledger/account/ticket split is deliberately independent of
the transport so `app.py` can later replace `BillingService`'s connection factory.
"""
import os
import secrets
import time
import uuid
from pathlib import Path

from . import accounts, db_migrate, ledger, prices, tickets
from .errors import (AccountNotFound, CreditsError, InsufficientCredits, InvalidResult,
                     InvalidState, TicketNotFound, ValidationError)

RESULT_STATUSES = ('success', 'failed', 'needs_action', 'cancelled')
RESERVED_SUFFIXES = (':settle', ':release', ':expired')
DEFAULT_TICKET_TTL = 1800  # seconds; long enough for a render/publish, short enough to reclaim
SECRET_ENV = 'IP_CREDITS_TICKET_SECRET'
SECRET_FILENAME = '.credits_ticket_secret'


def _int_amount(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError('%s 必须是整数毫分。' % name)
    if value < 0:
        raise ValidationError('%s 不能为负。' % name)
    return value


class BillingService:
    def __init__(self, db_path, *, secret=None, ticket_ttl=DEFAULT_TICKET_TTL,
                 clock=None, bootstrap=True):
        self.db_path = Path(db_path)
        self.ticket_ttl = int(ticket_ttl)
        if self.ticket_ttl <= 0:
            raise ValidationError('ticket_ttl 必须为正。')
        self._clock = clock or time.time
        self.secret = secret or self._resolve_secret()
        if bootstrap:
            self.migrate()

    # ---------------------------------------------------------------- plumbing
    def _now(self):
        return int(self._clock())

    def _resolve_secret(self):
        """No shared hardcoded HMAC secret: env -> local key file -> freshly generated."""
        env = os.getenv(SECRET_ENV)
        if env:
            return env
        key_file = self.db_path.parent / SECRET_FILENAME
        if key_file.is_file():
            value = key_file.read_text(encoding='utf-8').strip()
            if value:
                return value
        value = secrets.token_hex(32)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(value + '\n', encoding='utf-8')
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return value

    def migrate(self):
        return db_migrate.ensure(self.db_path, now=self._now())

    @staticmethod
    def _scoped_key(user_id, idempotency_key):
        return str(user_id) + '\x1f' + idempotency_key

    @staticmethod
    def _validate_key(key):
        if not isinstance(key, str) or not key.strip():
            raise ValidationError('idempotency_key 必填（发布/生成动作必须带幂等键）。')
        if len(key) > 200:
            raise ValidationError('idempotency_key 过长（上限 200）。')
        if '\x1f' in key:
            raise ValidationError('idempotency_key 含保留分隔符。')
        if key.endswith(RESERVED_SUFFIXES):
            raise ValidationError('idempotency_key 不能以保留后缀结尾：%r' % (RESERVED_SUFFIXES,))
        return key

    def _connect(self):
        return db_migrate.connect(self.db_path)

    def _snapshot(self, con, user_id, now):
        return ledger.snapshot(con, user_id, now=now)

    # ---------------------------------------------------------------- accounts
    def create_user(self, *, password=None, password_hash=None, phone=None, email=None,
                    user_id=None, trial_grant_milli=0, plan='free', expire_at=None):
        if password_hash is None:
            if password is None:
                raise ValidationError('password 或 password_hash 必填。')
            password_hash = accounts.hash_password(password)
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                user = accounts.create_user(con, password_hash=password_hash, phone=phone,
                                            email=email, user_id=user_id, now=now)
                if plan != 'free':
                    accounts.set_membership(con, user['id'], plan, expire_at=expire_at, now=now)
                if int(trial_grant_milli or 0) > 0:
                    ledger.grant(con, user['id'], int(trial_grant_milli), kind='grant',
                                 action='trial',
                                 idempotency_key=self._scoped_key(user['id'], 'trial'),
                                 note='trial', now=now)
        return user

    def get_user(self, user_id):
        with self._connect() as con:
            return accounts.get_user(con, user_id)

    def get_membership(self, user_id):
        with self._connect() as con:
            return accounts.effective_membership(con, user_id, now=self._now())

    def set_membership(self, user_id, plan, *, expire_at=None, grace_days=None,
                       discount_bp=None, quota=None):
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                accounts.require_active_user(con, user_id)
                return accounts.set_membership(con, user_id, plan, expire_at=expire_at,
                                               grace_days=grace_days, discount_bp=discount_bp,
                                               quota=quota, now=now)

    def grant(self, user_id, amount_milli, *, kind='grant', action=None,
              idempotency_key=None, note=None):
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                accounts.require_active_user(con, user_id)
                key = self._scoped_key(user_id, idempotency_key
                                       or ('grant-' + uuid.uuid4().hex[:16]))
                return ledger.grant(con, user_id, _int_amount(amount_milli, 'amount_milli'),
                                    kind=kind, action=action, idempotency_key=key,
                                    note=note, now=now)

    # ---------------------------------------------------------------- read APIs
    def balance(self, user_id):
        with self._connect() as con:
            with db_migrate.transaction(con):
                accounts.require_active_user(con, user_id)
                return self._snapshot(con, user_id, self._now())

    def list_ledger(self, user_id, *, limit=200):
        with self._connect() as con:
            return ledger.entries(con, user_id, limit=limit)

    def quote(self, user_id, action, qty=1):
        now = self._now()
        with self._connect() as con:
            accounts.require_active_user(con, user_id)
            price = prices.require_price(con, action)
            member = accounts.effective_membership(con, user_id, now=now)
            amount = prices.quote_milli(price, qty=qty, discount_bp=member['discount_bp'])
            return {'action': action, 'qty': qty, 'plan': member['plan'],
                    'discount_bp': member['discount_bp'],
                    'unit_milli': price['unit_milli'], 'quoted_milli': amount,
                    'settle_rule': price['settle_rule']}

    def get_ticket(self, job_id):
        with self._connect() as con:
            return tickets.load(con, job_id=job_id)

    def reconcile(self, user_id=None):
        with self._connect() as con:
            with db_migrate.transaction(con):
                return ledger.reconcile(con, user_id=user_id, now=self._now())

    # ---------------------------------------------------------------- step 1-4
    def submit(self, user_id, action, idempotency_key, *, qty=1, job_id=None,
               estimate_milli=None, note=None):
        """提交任务：校验 -> 冻结 hold -> 签发 ticket；同一幂等键返回同一 ticket。"""
        self._validate_key(idempotency_key)
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                accounts.require_active_user(con, user_id)
                price = prices.require_price(con, action)
                member = accounts.effective_membership(con, user_id, now=now)
                discount = member['discount_bp']

                key = self._scoped_key(user_id, idempotency_key)
                existing = ledger.get(con, key)
                if existing is not None:
                    ticket = tickets.require(con, job_id=existing['job_id'])
                    return self._ticket_summary(con, ticket, duplicate=True, now=now)

                if estimate_milli is not None:
                    quoted = _int_amount(estimate_milli, 'estimate_milli')
                else:
                    if price['billable'] and price['settle_rule'] != 'free':
                        try:
                            if float(qty) <= 0:
                                raise ValidationError('计费动作的预估用量必须大于 0。')
                        except (TypeError, ValueError) as exc:
                            raise ValidationError('qty 非法：%r' % (qty,)) from exc
                    quoted = prices.quote_milli(price, qty=qty, discount_bp=discount)
                if prices.is_free(price):
                    quoted = 0

                job_id = job_id or ('job_' + uuid.uuid4().hex[:16])
                if tickets.load(con, job_id=job_id) is not None:
                    raise InvalidState('job_id 已存在 ticket，请换 job_id。', job_id=job_id)
                ticket_id = 'tkt_' + uuid.uuid4().hex[:16]

                # step 3: freeze first (this is where balance is checked, atomically)
                ledger.hold(con, user_id=user_id, job_id=job_id, ticket_id=ticket_id,
                            action=action, quoted_milli=quoted, idempotency_key=key,
                            qty=qty, unit_milli=price['unit_milli'],
                            discount_bp=discount, note=note, now=now)
                # step 4: sign the ticket (same transaction: no ticket without a hold)
                ticket = tickets.issue(con, user_id=user_id, job_id=job_id, action=action,
                                       quoted_milli=quoted, secret=self.secret,
                                       ttl_seconds=self.ticket_ttl, now=now,
                                       ticket_id=ticket_id)
                return self._ticket_summary(con, ticket, duplicate=False, now=now)

    def _ticket_summary(self, con, ticket, *, duplicate, now):
        snap = self._snapshot(con, ticket['user_id'], now)
        return {
            'user_id': ticket['user_id'],
            'job_id': ticket['job_id'],
            'ticket_id': ticket['id'],
            'action': ticket['action'],
            'quoted_milli': ticket['quoted_milli'],
            'status': ticket['status'],
            'nonce': ticket['nonce'],
            'issued_at': ticket['issued_at'],
            'expire_at': ticket['expire_at'],
            'token': tickets.token_for(self.secret, ticket),
            'duplicate': bool(duplicate),
            'balance_milli': snap['balance_milli'],
            'frozen_milli': snap['frozen_milli'],
        }

    # ---------------------------------------------------------------- step 5
    def verify_ticket(self, token):
        """Client-side (offline) check: signature + TTL. No DB access, no state change."""
        return tickets.verify_token(token, self.secret, now=self._now())

    def consume(self, token, *, user_id=None):
        """Server-side gate before a worker starts: signature + TTL + DB status -> consumed."""
        with self._connect() as con:
            with db_migrate.transaction(con):
                return tickets.consume(con, token, self.secret, user_id=user_id,
                                       now=self._now())

    # ---------------------------------------------------------------- step 6-7
    def settle(self, job_id, *, actual_qty=None, actual_milli=None, result=None, note=None):
        """成功结算：按实际用量重算，多退少补。"""
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                ticket = tickets.require(con, job_id=job_id)
                price = prices.get_price(con, ticket['action'])
                rule = price['settle_rule'] if price else 'on_complete'

                if result is not None:
                    self._validate_result(result, rule)
                    if result.get('status') != 'success':
                        raise InvalidResult('settle 只能用于 success 结果；失败请走 release。',
                                            status=result.get('status'))

                closing = ledger.find_closing(con, ticket['id'])
                if closing is not None:
                    if closing['kind'] == 'release':
                        raise InvalidState('hold 已 release/expired，不能 settle。',
                                           ticket_id=ticket['id'], current=ticket['status'])
                    return self._close_summary(con, ticket, closing, duplicate=True, now=now)
                if ticket['status'] in ('released', 'expired'):
                    raise InvalidState('ticket 已是 %r，不能 settle。' % (ticket['status'],),
                                       ticket_id=ticket['id'], current=ticket['status'])

                hold_row = ledger.find_hold(con, ticket['id'])
                if hold_row is None:
                    raise InvalidState('找不到该 ticket 的 hold 流水。', ticket_id=ticket['id'])

                if actual_milli is not None:
                    actual = _int_amount(actual_milli, 'actual_milli')
                elif actual_qty is not None:
                    member = accounts.effective_membership(con, ticket['user_id'], now=now)
                    if price is None:
                        raise ValidationError('price_book 缺少动作定价：%r' % (ticket['action'],))
                    actual = prices.quote_milli(price, qty=actual_qty,
                                                discount_bp=member['discount_bp'])
                else:
                    actual = int(ticket['quoted_milli'])

                settle_row, duplicate = ledger.settle_charge(
                    con, hold_row=hold_row, actual_milli=actual,
                    idempotency_key=ticket['id'] + ':settle', note=note, now=now)
                if duplicate:
                    return self._close_summary(con, ticket, settle_row, duplicate=True, now=now)
                tickets.transition(con, ticket['id'], 'settled',
                                   allowed_from=('issued', 'consumed'), now=now)
                if result is not None:
                    self._store_result(con, ticket, result, now)
                ticket = tickets.require(con, job_id=job_id)
                return self._close_summary(con, ticket, settle_row, duplicate=False, now=now)

    def release(self, job_id, *, reason='', result=None, final_status='released'):
        """失败/无响应退分：全额 release，不留余额痕迹。"""
        if final_status not in ('released', 'expired'):
            raise ValidationError("final_status 只能是 'released' 或 'expired'。")
        now = self._now()
        with self._connect() as con:
            with db_migrate.transaction(con):
                ticket = tickets.require(con, job_id=job_id)
                price = prices.get_price(con, ticket['action'])
                rule = price['settle_rule'] if price else 'on_complete'
                if result is not None:
                    self._validate_result(result, rule)
                return self._release_in_tx(con, ticket, reason=reason, result=result,
                                           final_status=final_status, now=now)

    def _release_in_tx(self, con, ticket, *, reason, result, final_status, now):
        closing = ledger.find_closing(con, ticket['id'])
        if closing is not None:
            if closing['kind'] == 'settle':
                raise InvalidState('hold 已 settle，不能 release。',
                                   ticket_id=ticket['id'], current=ticket['status'])
            return self._close_summary(con, ticket, closing, duplicate=True, now=now)
        hold_row = ledger.find_hold(con, ticket['id'])
        if hold_row is None:
            raise InvalidState('找不到该 ticket 的 hold 流水。', ticket_id=ticket['id'])
        if ticket['status'] in ('settled', 'expired') and final_status != 'expired':
            raise InvalidState('ticket 已是 %r，不能 release。' % (ticket['status'],),
                               ticket_id=ticket['id'], current=ticket['status'])
        release_row, duplicate = ledger.release_charge(
            con, hold_row=hold_row, idempotency_key=ticket['id'] + ':release',
            note=reason or final_status, now=now)
        if not duplicate:
            tickets.transition(con, ticket['id'], final_status,
                               allowed_from=('issued', 'consumed'), now=now)
            if result is not None:
                self._store_result(con, ticket, result, now)
        ticket = tickets.require(con, job_id=ticket['job_id'])
        return self._close_summary(con, ticket, release_row, duplicate=duplicate, now=now)

    def report_result(self, job_id, result, *, actual_qty=None, actual_milli=None):
        """执行结束回报。success -> settle；failed/needs_action/cancelled -> release。"""
        if not isinstance(result, dict):
            raise InvalidResult('result 必须是对象。')
        status = result.get('status')
        if status not in RESULT_STATUSES:
            raise InvalidResult('result.status 必须是 %s 之一。' % (RESULT_STATUSES,),
                                status=status)
        if status == 'success':
            return self.settle(job_id, actual_qty=actual_qty, actual_milli=actual_milli,
                               result=result)
        return self.release(job_id, reason=str(result.get('message') or status),
                            result=result)

    def expire_stale(self, *, now=None):
        """TTL 对账：issued/consumed 且已过期的 ticket 自动全额 release。"""
        ts = int(now if now is not None else self._now())
        with self._connect() as con:
            with db_migrate.transaction(con):
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM job_ticket WHERE status IN ('issued','consumed')"
                    ' AND expire_at<=? ORDER BY expire_at, id', (ts,))]
                out = []
                for ticket in rows:
                    out.append(self._release_in_tx(con, ticket, reason='ticket_ttl_expired',
                                                   result=None, final_status='expired',
                                                   now=ts))
                return out

    # ---------------------------------------------------------------- helpers
    def _close_summary(self, con, ticket, entry, *, duplicate, now):
        snap = self._snapshot(con, ticket['user_id'], now)
        hold = ledger.find_hold(con, ticket['id'])
        quoted = -int(hold['delta_milli']) if hold else 0
        delta = int(entry['delta_milli'])
        if entry['kind'] == 'settle':
            charged = quoted - delta              # settle delta = quoted - actual
            refund = max(0, delta)                # positive delta = 多退
        else:
            charged = 0
            refund = quoted                       # full refund
        return {
            'user_id': ticket['user_id'],
            'job_id': ticket['job_id'],
            'ticket_id': ticket['id'],
            'action': ticket['action'],
            'status': ticket['status'],
            'quoted_milli': quoted,
            'charged_milli': charged,
            'refund_milli': refund,
            'ledger_kind': entry['kind'],
            'ledger_id': entry['id'],
            'duplicate': bool(duplicate),
            'balance_milli': snap['balance_milli'],
            'frozen_milli': snap['frozen_milli'],
        }

    @staticmethod
    def _validate_result(result, settle_rule):
        if not isinstance(result, dict):
            raise InvalidResult('result 必须是对象。')
        status = result.get('status')
        if status not in RESULT_STATUSES:
            raise InvalidResult('result.status 必须是 %s 之一。' % (RESULT_STATUSES,),
                                status=status)
        if status != 'success':
            return
        if settle_rule != 'on_success':
            return
        work_id = result.get('platform_work_id')
        work_url = result.get('work_url')
        if not (isinstance(work_id, str) and work_id.strip()):
            raise InvalidResult('发布类成功必须提供 platform_work_id。', missing='platform_work_id')
        if not (isinstance(work_url, str) and work_url.strip().lower().startswith(('http://', 'https://'))):
            raise InvalidResult('发布类成功必须提供可验证的 work_url（http/https）。', missing='work_url')

    def _store_result(self, con, ticket, result, now):
        con.execute('INSERT INTO publish_result'
                    '(id, ticket_id, user_id, platform, platform_work_id, work_url,'
                    ' status, message, reported_at) VALUES (?,?,?,?,?,?,?,?,?)',
                    ('res_' + uuid.uuid4().hex[:16], ticket['id'], ticket['user_id'],
                     str(result.get('platform') or ''), result.get('platform_work_id'),
                     result.get('work_url'), str(result['status']),
                     str(result.get('message') or '')[:1000], now))
