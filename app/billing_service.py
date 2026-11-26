"""计费服务：只追加账本 + hold/settle/release + entitlement + 会员状态 + 价目热读。

边界（本轮）：
  - 不做支付：不接微信/支付宝、不生成订单、不写假支付成功；只留 PaymentProvider 空壳。
  - 不改 app.py / db.py / cover_packaging.py / avatar_service.py / web/*；接线点见 billing_README.md。
  - 单用户本地版 user_id 默认 local-owner，但所有表与查询都按多用户设计。

账本口径（沿用 20261027 方案 B）：
  - delta 有符号：正 = 入账，负 = 出账；账本只追加，余额由流水重放得出。
  - hold: delta = -quoted；settle: delta = +quoted - actual（多退少补）；
    release: delta = +quoted（全额退回，余额净变化 0）。
  - credit_account 只是缓存，余额以 credit_ledger 重放为准；对不上就按账本修复。
  - 金额全部整数：积分用毫分（1 积分 = 1000 毫分），人民币用分；禁止浮点。

积分有效期（2026-11-03 定稿）：
  - 购买积分：expires_at = NULL，永久有效、永不清零。
  - 赠送积分：expires_at = 名义到期日；expiring_at = expires_at + 宽限期（默认 30 天）。
    宽限期内仍可花；宽限期过仍未用，由 sweep_expired() 追加 kind='void' 流水作废。
  - 扣减顺序：先扣最快到期的赠送积分（expiring_at 升序），再扣永久购买积分。
  - 余额分桶返回：granted_milli / purchased_milli。
"""
import functools
import inspect
import json
import os
import sqlite3
import threading
import time
import uuid
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

import billing_db

LOCAL_USER_ID = 'local-owner'

# 请求级用户上下文（2026-11-14 G0）：独立登录上线后，账本必须按账号隔离。
# app.route 每个请求先 set_request_user()，没有会话（如后台任务）时回落到 local-owner。
_REQUEST_USER = threading.local()


def set_request_user(user_id):
    """由 app.route 在分发前调用；传空表示没有登录会话。"""
    _REQUEST_USER.user_id = str(user_id or '')


def request_user_id():
    return getattr(_REQUEST_USER, 'user_id', '') or LOCAL_USER_ID


def set_entitlements(entitlements):
    """云端下发的账号权益（exports_per_month / max_height / allow_cloud_asr / ...）。

    有云端权益时以云端为准（见 _features）；没下发就返回 {}，继续沿用
    billing_pricing.json 的 free_limits，不在客户端另写一套额度。
    """
    _REQUEST_USER.entitlements = dict(entitlements or {})


def request_entitlements():
    return dict(getattr(_REQUEST_USER, 'entitlements', {}) or {})


def allow_cloud_asr():
    """云端 ASR 开关：云端下发什么就用什么，缺字段按不可用处理（2026-11-16 收紧）。"""
    ent = request_entitlements()
    if 'allow_cloud_asr' not in ent:
        return False
    return bool(ent.get('allow_cloud_asr'))


def capture_context():
    """后台线程启动前快照登录账号与权益（线程池/新线程不继承 threading.local）。"""
    return {'user_id': str(getattr(_REQUEST_USER, 'user_id', '') or ''),
            'entitlements': request_entitlements()}


def apply_context(context):
    """在后台线程恢复快照；空 user_id 仍按“无会话”处理，由 request_user_id() 回落 local-owner。"""
    context = dict(context or {})
    set_request_user(context.get('user_id'))
    set_entitlements(context.get('entitlements') or {})


def _with_request_user(method):
    """把 user_id=None 解析成当前请求账号；显式传值（含位置参数）不受影响。"""
    try:
        _params = list(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        _params = []
    _slot = _params.index('user_id') if 'user_id' in _params else -1

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if kwargs.get('user_id') is None and _slot >= 0 and len(args) < _slot:
            kwargs['user_id'] = request_user_id()
        return method(self, *args, **kwargs)
    return wrapper


CREDIT_MILLI = 1000
DAY = 86400
MONTH_SECONDS = 30 * DAY
_FOREVER = 10 ** 18
DEFAULT_PRICING = Path(__file__).resolve().parent / 'src' / 'billing_pricing.json'

_ACTION_FOR_KIND = {'template': 'cover_template_unlock', 'avatar': 'avatar_unlock'}
_RESERVED_SUFFIXES = (':settle', ':release')


class BillingError(Exception):
    """所有计费失败的基类；code 可直接给前端/接口用。"""
    code = 'billing_error'

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self):
        return {'error': self.code, 'message': self.message, **self.details}


class ValidationError(BillingError):
    code = 'validation_error'


class UnknownAction(BillingError):
    code = 'unknown_action'


class InsufficientCredits(BillingError):
    code = 'insufficient_credits'


class IdempotencyConflict(BillingError):
    code = 'idempotency_conflict'


class InvalidState(BillingError):
    code = 'invalid_state'


class HoldNotFound(BillingError):
    code = 'hold_not_found'


class PaymentNotConfigured(BillingError):
    code = 'payment_not_configured'


class PaymentProvider:
    """支付通道接口签名（空壳）。

    本轮不接微信/支付宝，也不生成假订单。实现类必须覆盖：
      create_checkout(...) -> 由支付通道返回的收银台参数
      verify_callback(payload) -> 校验签名并返回已确认的支付结果
    """
    name = 'unconfigured'

    def create_checkout(self, *, user_id, sku, amount_cents, idempotency_key):
        raise PaymentNotConfigured('未配置支付通道：本轮只做账本，不接微信/支付宝，不生成订单。')

    def verify_callback(self, payload):
        raise PaymentNotConfigured('未配置支付通道：无法校验回调，也不会写入假支付成功。')


# ---------------------------------------------------------------- 工具函数

def _now(clock=None):
    return int(clock() if clock else time.time())


def _as_int(value, name, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError('%s 必须是整数。' % name)
    if value < minimum:
        raise ValidationError('%s 不能小于 %d。' % (name, minimum))
    return value


def _qty_milli(qty):
    """用量转千分之一单位（分钟/件）。用 Decimal，不落浮点。"""
    try:
        value = Decimal(str(qty))
    except Exception as exc:  # noqa: BLE001
        raise ValidationError('用量不合法：%r' % (qty,)) from exc
    if value <= 0:
        raise ValidationError('用量必须大于 0。')
    return int((value * CREDIT_MILLI).to_integral_value(rounding=ROUND_CEILING))


def _ceil_div(a, b):
    return -(-int(a) // int(b))


def _credits(milli):
    """整数毫分 -> 积分展示串（'3' / '3.5'），不产生 float。"""
    return format(Decimal(int(milli)) / CREDIT_MILLI, 'f')


# ---------------------------------------------------------------- 价目热读

_PRICING_CACHE = {'path': None, 'stamp': None, 'data': None}
_PRICING_LOCK = threading.Lock()


def _read_pricing(path):
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        raise ValidationError('价目文件不可读：%s' % path) from exc
    stamp = (st.st_mtime_ns, st.st_size)
    with _PRICING_LOCK:
        if (_PRICING_CACHE['path'] == str(path)
                and _PRICING_CACHE['stamp'] == stamp
                and _PRICING_CACHE['data']):
            return _PRICING_CACHE['data']
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        raise ValidationError('价目文件不是合法 JSON：%s' % path) from exc
    _validate_pricing(data)
    with _PRICING_LOCK:
        _PRICING_CACHE.update(path=str(path), stamp=stamp, data=data)
    return data


def _validate_pricing(data):
    if not isinstance(data, dict):
        raise ValidationError('价目文件顶层必须是对象。')
    plans = data.get('plans')
    actions = data.get('actions')
    if not isinstance(plans, dict) or not isinstance(actions, dict) or not actions:
        raise ValidationError('价目文件缺 plans / actions。')
    if 'free' not in plans:
        raise ValidationError('价目文件缺 free 档位。')
    if not [name for name in plans if name != 'free']:
        raise ValidationError('价目文件至少需要一个付费档位。')
    for name, plan in plans.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(plan, dict):
            raise ValidationError('档位名与档位配置不合法：%r' % (name,))
        price = plan.get('price_cents')
        if not isinstance(price, int) or isinstance(price, bool) or price < 0:
            raise ValidationError('档位 %s.price_cents 必须是非负整数。' % name)
        for key in ('months', 'days', 'period_days'):
            if key in plan and (not isinstance(plan[key], int) or plan[key] < 0):
                raise ValidationError('档位 %s.%s 必须是非负整数。' % (name, key))
        if name != 'free' and 'days' not in plan and 'period_days' not in plan:
            raise ValidationError('档位 %s 必须配 days 或 period_days。' % name)
        list_price = plan.get('list_price_cents')
        if list_price is not None and (not isinstance(list_price, int)
                                       or isinstance(list_price, bool)
                                       or list_price < price):
            raise ValidationError('档位 %s.list_price_cents 必须是 >= price_cents 的整数。' % name)
        if 'enabled' in plan and not isinstance(plan['enabled'], bool):
            raise ValidationError('档位 %s.enabled 必须是布尔。' % name)
        if 'sort' in plan and (not isinstance(plan['sort'], int) or isinstance(plan['sort'], bool)):
            raise ValidationError('档位 %s.sort 必须是整数。' % name)
        benefits = plan.get('benefits')
        if benefits is not None and (not isinstance(benefits, list)
                                     or any(not isinstance(item, str) for item in benefits)):
            raise ValidationError('档位 %s.benefits 必须是字符串数组。' % name)
        bp = plan.get('discount_bp', 10000)
        if not isinstance(bp, int) or not 0 <= bp <= 10000:
            raise ValidationError('档位 %s.discount_bp 必须在 0..10000。' % name)
    for name, price in actions.items():
        for key in ('unit_milli', 'min_milli'):
            if not isinstance(price.get(key), int):
                raise ValidationError('动作 %s.%s 必须是整数（毫分）。' % (name, key))
        if price['unit_milli'] < 0 or price['min_milli'] < 0:
            raise ValidationError('动作 %s 的单价/最低消费不能为负。' % name)
        if not isinstance(price.get('billable', True), bool):
            raise ValidationError('动作 %s.billable 必须是布尔。' % name)
        if price.get('settle_rule', 'on_complete') not in ('on_complete', 'on_success', 'free'):
            raise ValidationError('动作 %s.settle_rule 不合法。' % name)
        items = price.get('item_prices_milli') or {}
        if not isinstance(items, dict) or any(not isinstance(v, int) or v < 0 for v in items.values()):
            raise ValidationError('动作 %s.item_prices_milli 必须是非负整数映射。' % name)


# ---------------------------------------------------------------- 服务

class BillingService:
    def __init__(self, db_path=None, pricing_path=None, clock=None):
        self.db_path = Path(db_path) if db_path else billing_db.default_db_path()
        # 白牌/测试可外置价目表：IP_BILLING_PRICING 优先于包内 src/billing_pricing.json。
        env_pricing = str(os.getenv('IP_BILLING_PRICING') or '').strip()
        if pricing_path:
            self.pricing_path = Path(pricing_path)
        elif env_pricing:
            self.pricing_path = Path(env_pricing)
        else:
            self.pricing_path = DEFAULT_PRICING
        self._clock = clock
        self.migrate()

    # ------------------------------------------------------------ 基础
    def _now(self):
        return _now(self._clock)

    def migrate(self):
        return billing_db.ensure(self.db_path)

    def _connect(self):
        return billing_db.connect(self.db_path)

    def pricing(self):
        """每次调用都按文件 mtime/size 判断是否重读：改 JSON 即热更，不重启进程。"""
        return _read_pricing(self.pricing_path)

    def price_book(self):
        return [dict(value, action=name) for name, value in self.pricing()['actions'].items()]

    @staticmethod
    def _cny(cents):
        """整数分 -> '49.00'，不落浮点。"""
        cents = int(cents or 0)
        sign = '-' if cents < 0 else ''
        yuan, fen = divmod(abs(cents), 100)
        return '%s%d.%02d' % (sign, yuan, fen)

    def catalog(self):
        """档位展示目录（G1）：档位可增删、上下架、排序、权益说明全部来自价目表。"""
        data = self.pricing()
        plans = data.get('plans') or {}
        free_limits = data.get('free_limits') or {}
        items = []
        for code, cfg in plans.items():
            price = int(cfg.get('price_cents') or 0)
            raw_list = cfg.get('list_price_cents')
            list_price = price if raw_list is None else int(raw_list)
            raw_days = cfg.get('period_days')
            if raw_days is None:
                raw_days = cfg.get('days') or 0
            items.append({
                'code': code,
                'label': str(cfg.get('label') or code),
                'price_cents': price,
                'price_cny': self._cny(price),
                'list_price_cents': list_price,
                'list_price_cny': self._cny(list_price),
                'save_cents': max(0, list_price - price),
                'save_cny': self._cny(max(0, list_price - price)),
                'period': str(cfg.get('period') or ('none' if code == 'free' else 'custom')),
                'period_days': int(raw_days),
                'enabled': bool(cfg.get('enabled', True)),
                'sort': int(cfg.get('sort') if cfg.get('sort') is not None else 100),
                'badge': str(cfg.get('badge') or ''),
                'benefits': [str(item) for item in (cfg.get('benefits') or [])],
                'benefits_note': str(cfg.get('benefits_note') or ''),
                'includes': cfg.get('includes') or {},
                'pricing_status': str(cfg.get('pricing_status') or ''),
                'features': self._features(code),
            })
        items.sort(key=lambda item: (item['sort'], item['code']))
        return {
            'currency': data.get('currency') or 'CNY',
            'items': items,
            'pricing_note': str(data.get('pricing_note') or ''),
            'free_limits': free_limits,
            'free_limits_note': str(data.get('free_limits_note') or ''),
            'watermark_enabled': bool(free_limits.get('watermark')),
            'watermark_note': str(data.get('watermark_note') or ''),
        }

    def _grace(self):
        validity = self.pricing().get('credit_validity') or {}
        return int(validity.get('granted_grace_days', 30))

    def _price(self, action):
        if not isinstance(action, str) or not action.strip():
            raise ValidationError('action 必填。')
        price = self.pricing()['actions'].get(action)
        if price is None:
            raise UnknownAction('未在价目表定价的动作：%r' % (action,), action=action)
        return dict(price)

    def _quote(self, action, *, qty=1, item_id=None, discount_bp=10000):
        price = self._price(action)
        qty_milli = _qty_milli(qty)
        unit_milli = int(price['unit_milli'])
        items = price.get('item_prices_milli') or {}
        if item_id and item_id in items:
            unit_milli = int(items[item_id])
        if not price.get('billable', True) or price.get('settle_rule') == 'free':
            amount = 0
        else:
            amount = max(int(price['min_milli']),
                         _ceil_div(unit_milli * qty_milli, CREDIT_MILLI))
            if discount_bp != 10000:
                amount = _ceil_div(amount * int(discount_bp), 10000)
        return {'action': action, 'qty_milli': qty_milli, 'unit_milli': unit_milli,
                'amount_milli': amount, 'amount_credits': _credits(amount),
                'discount_bp': int(discount_bp),
                'settle_rule': price.get('settle_rule', 'on_complete')}

    def quote(self, action, *, qty=1, item_id=None, user_id=None):
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                row = self._membership_row(con, user_id, now)
                row = self._auto_resume(con, row, now)
                plan = self._effective_plan(row, now)
                out = self._quote(action, qty=qty, item_id=item_id,
                                  discount_bp=self._discount_bp(plan))
                out['plan'] = plan
                return out

    # ------------------------------------------------------------ 余额 / 流水
    def balance(self, user_id=None):
        """返回可花余额（已扣冻结、已剔除过期赠送）。赠送/购买分桶返回。

        每次读余额顺带扫一次超时冻结（G8），任务丢失后不会永久冻结积分。
        """
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                # 先放冻结再作废：超时释放退回的积分如果所在批次已过宽限，
                # 同一次 sweep 里一起作废，不会留下"退回了但花不出去"的悬挂余额。
                released = self._close_expired_holds_in_tx(con, user_id, now)
                self._sweep_in_tx(con, user_id, now)
                if released:
                    self._sync_account(con, user_id, now)
                return self._snapshot(con, user_id, now)

    def ledger(self, *, limit=50, offset=0, user_id=None):
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with self._connect() as con:
            total = con.execute('SELECT COUNT(*) FROM credit_ledger WHERE user_id=?',
                                (user_id,)).fetchone()[0]
            rows = [dict(r) for r in con.execute(
                'SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                (user_id, limit, offset))]
        for row in rows:
            row['credits'] = _credits(row['delta_milli'])
            row['qty'] = _credits(row['qty_milli'] or 0)
        return {'total': int(total), 'limit': limit, 'offset': offset, 'items': rows}

    def expiring_soon(self, *, days=7, user_id=None):
        """赠送积分到期提醒源：expiring_at（宽限结束=真正作废时间）已持久化在流水上。"""
        days = max(0, int(days))
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                self._sweep_in_tx(con, user_id, now)
                replay = self._replay(con, user_id, now)
        horizon = now + days * DAY
        items = []
        for lot in replay['lots']:
            if lot['kind'] != 'grant' or lot['remaining'] <= 0:
                continue
            if lot['expiring_at'] is None or int(lot['expiring_at']) > horizon:
                continue
            items.append({
                'lot_id': lot['id'], 'remaining_milli': lot['remaining'],
                'remaining_credits': _credits(lot['remaining']),
                'expires_at': lot['expires_at'], 'expiring_at': lot['expiring_at'],
                'days_to_void': max(0, (int(lot['expiring_at']) - now) // DAY),
                'in_grace': bool(lot['expires_at'] is not None
                                 and int(lot['expires_at']) <= now),
            })
        items.sort(key=lambda item: int(item['expiring_at']))
        return items

    def sweep_expired(self, *, user_id=None):
        """宽限期结束的赠送积分作废：只追加 kind='void' 流水，余额净减少。"""
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                voided = self._sweep_in_tx(con, user_id, now)
                self._sync_account(con, user_id, now)
        return {'voided': voided, 'voided_milli': sum(item['amount_milli'] for item in voided),
                'voided_credits': _credits(sum(item['amount_milli'] for item in voided))}

    # ------------------------------------------------------------ 入账
    def grant(self, amount_milli, *, idempotency_key, kind='grant', expires_at=None,
              action=None, note=None, user_id=None):
        """正向入账。

        kind='purchase'：购买积分，永久有效，禁止传 expires_at。
        kind='grant'：赠送积分（注册/活动/会员月赠），必须传名义到期日 expires_at；
                      宽限期 = expires_at + granted_grace_days，实际作废时间写入 expiring_at。
        kind='adjust'：人工调整，永久有效。
        """
        amount = _as_int(amount_milli, 'amount_milli', minimum=1)
        if kind not in ('grant', 'purchase', 'adjust'):
            raise ValidationError("入账 kind 只能是 grant/purchase/adjust。")
        self._validate_key(idempotency_key)
        now = self._now()
        if kind == 'grant':
            if expires_at is None:
                raise ValidationError('赠送额度必须带名义到期日 expires_at。')
            expires_at = _as_int(expires_at, 'expires_at', minimum=1)
            if expires_at <= now:
                raise ValidationError('赠送额度的到期日必须晚于当前时间。')
            expiring_at = expires_at + self._grace() * DAY
        else:
            if expires_at is not None:
                raise ValidationError('%s 额度永久有效，不能带 expires_at。' % kind)
            expiring_at = None
        with self._connect() as con:
            with billing_db.transaction(con):
                existing = self._by_key(con, user_id, idempotency_key)
                if existing is not None:
                    if existing['kind'] != kind:
                        raise IdempotencyConflict('幂等键已被其它流水占用：%r' % idempotency_key,
                                                  idempotency_key=idempotency_key)
                    snap = self._snapshot(con, user_id, now)
                    return self._ledger_view(existing, snap, duplicate=True)
                entry = self._append(con, user_id=user_id, delta_milli=amount, kind=kind,
                                     action=action, expires_at=expires_at,
                                     expiring_at=expiring_at, note=note,
                                     idempotency_key=idempotency_key, now=now)
                snap = self._sync_account(con, user_id, now)
                view = self._ledger_view(entry, snap, duplicate=False)
                view.update(expires_at=expires_at, expiring_at=expiring_at, grant_kind=kind)
                return view

    # ------------------------------------------------------------ 冻 / 结 / 释
    def hold(self, *, action, idempotency_key, qty=1, job_id=None, amount_milli=None,
             note=None, user_id=None):
        """冻结：quote -> 余额校验 -> 追加 -quoted 流水。同幂等键重复提交返回同一笔。

        余额校验按「赠送+购买」合计；真正扣到哪一类由重放时的分配顺序决定：
        先扣最快到期的赠送积分，再扣永久购买积分。
        """
        target = str(user_id or request_user_id())
        if target.startswith('cloud-') and action == 'brain_ask':
            # 2026-11-18 云端积分真源（设计稿 §3.2-3）：cloud-* 的第二大脑问答
            # 只走云端钱包（billing_route → cloud_gateway.credit_hold/settle/release）。
            # 本地账本落到这里说明接线回退了——防御性拒绝：既不静默免费
            # （旧 cloud_free 分支的缺陷），也不扣本地、不赊账。
            # 本地账本继续服务 local-owner 问答与封面/形象等本地商品。
            raise ValidationError(
                '云端账号的知识库积分由云端钱包管理，本地账本不记 brain_ask；'
                '请走云端 /api/brain/credit/hold。')
        self._validate_key(idempotency_key)
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                existing = self._by_key(con, user_id, idempotency_key)
                if existing is not None:
                    if existing['kind'] != 'hold':
                        raise IdempotencyConflict('幂等键已被其它流水占用：%r' % idempotency_key,
                                                  idempotency_key=idempotency_key)
                    if job_id is not None and existing['job_id'] != job_id:
                        raise IdempotencyConflict('幂等键对应另一个 job_id：%r' % idempotency_key,
                                                  idempotency_key=idempotency_key,
                                                  job_id=existing['job_id'])
                    snap = self._snapshot(con, user_id, now)
                    return self._hold_view(existing, snap, duplicate=True)

                self._close_expired_holds_in_tx(con, user_id, now)
                self._sweep_in_tx(con, user_id, now)
                row = self._auto_resume(con, self._membership_row(con, user_id, now), now)
                bp = self._discount_bp(self._effective_plan(row, now))
                quote = self._quote(action, qty=qty, discount_bp=bp)
                quoted = quote['amount_milli'] if amount_milli is None else _as_int(
                    amount_milli, 'amount_milli')
                job_id = job_id or ('job_' + uuid.uuid4().hex[:16])
                if self._find_hold(con, user_id, job_id) is not None:
                    raise InvalidState('job_id 已有冻结流水，请换 job_id。', job_id=job_id)

                snap = self._snapshot(con, user_id, now)
                if quoted > snap['balance_milli']:
                    raise InsufficientCredits(
                        '余额不足：需要 %s 额度，可用 %s 额度。'
                        % (_credits(quoted), _credits(snap['balance_milli'])),
                        available_milli=snap['balance_milli'], required_milli=quoted,
                        shortfall_milli=quoted - snap['balance_milli'])

                entry = self._append(con, user_id=user_id, delta_milli=-quoted, kind='hold',
                                     action=action, qty_milli=quote['qty_milli'],
                                     unit_milli=quote['unit_milli'], discount_bp=bp,
                                     job_id=job_id, idempotency_key=idempotency_key,
                                     note=note, now=now)
                snap = self._sync_account(con, user_id, now)
                return self._hold_view(entry, snap, duplicate=False, quote=quote)

    def settle(self, job_id, *, actual_milli=None, actual_qty=None, note=None,
               user_id=None):
        """成功结算：delta = +quoted - actual（多退少补）。重复调用返回同一笔。

        user_id 留空时按 job_id 反查账本归属，后台线程结算也能落到正确账号。
        """
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                user_id = user_id or self._user_for_job(con, job_id) or request_user_id()
                hold_row = self._find_hold(con, user_id, job_id)
                if hold_row is None:
                    raise HoldNotFound('找不到该 job 的冻结流水：%r' % (job_id,), job_id=job_id)
                closing = self._find_closing(con, user_id, job_id)
                if closing is not None:
                    if closing['kind'] == 'release':
                        raise InvalidState('该冻结已释放，不能结算。', job_id=job_id)
                    return self._close_view(con, hold_row, closing, user_id, now, duplicate=True)

                quoted = -int(hold_row['delta_milli'])
                if actual_milli is not None:
                    actual = _as_int(actual_milli, 'actual_milli')
                elif actual_qty is not None:
                    actual = self._quote(hold_row['action'], qty=actual_qty,
                                         discount_bp=hold_row['discount_bp'])['amount_milli']
                else:
                    actual = quoted
                delta = quoted - actual
                if delta < 0:
                    snap = self._snapshot(con, user_id, now)
                    if snap['balance_milli'] < -delta:
                        raise InsufficientCredits(
                            '结算差额超出余额：还差 %s 额度。'
                            % _credits(-delta - snap['balance_milli']),
                            available_milli=snap['balance_milli'], required_milli=-delta)
                entry = self._append(
                    con, user_id=user_id, delta_milli=delta, kind='settle',
                    action=hold_row['action'], qty_milli=hold_row['qty_milli'],
                    unit_milli=hold_row['unit_milli'], discount_bp=hold_row['discount_bp'],
                    job_id=job_id, idempotency_key=job_id + ':settle', note=note, now=now)
                self._sync_account(con, user_id, now)
                return self._close_view(con, hold_row, entry, user_id, now, duplicate=False)

    def release(self, job_id, *, reason='', user_id=None):
        """失败/取消释放：全额退回。重复调用返回同一笔；已结算则拒绝。"""
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                user_id = user_id or self._user_for_job(con, job_id) or request_user_id()
                hold_row = self._find_hold(con, user_id, job_id)
                if hold_row is None:
                    raise HoldNotFound('找不到该 job 的冻结流水：%r' % (job_id,), job_id=job_id)
                closing = self._find_closing(con, user_id, job_id)
                if closing is not None:
                    if closing['kind'] == 'settle':
                        raise InvalidState('该冻结已结算，不能释放。', job_id=job_id)
                    return self._close_view(con, hold_row, closing, user_id, now, duplicate=True)
                quoted = -int(hold_row['delta_milli'])
                entry = self._append(
                    con, user_id=user_id, delta_milli=quoted, kind='release',
                    action=hold_row['action'], qty_milli=hold_row['qty_milli'],
                    unit_milli=hold_row['unit_milli'], discount_bp=hold_row['discount_bp'],
                    job_id=job_id, idempotency_key=job_id + ':release',
                    note=reason or 'release', now=now)
                self._sync_account(con, user_id, now)
                return self._close_view(con, hold_row, entry, user_id, now, duplicate=False)

    # ------------------------------------------------------------ entitlement
    def grant_entitlement(self, *, kind, ref_id=None, source_sha=None, grant_source='purchase',
                          ledger_id=None, expires_at=None, user_id=None):
        """授予授权。会员赠送必须 ledger_id=None —— 不伪造扣费流水。"""
        kind, ref_id, source_sha = self._normalize_entitlement(kind, ref_id, source_sha)
        if grant_source not in billing_db.GRANT_SOURCES:
            raise ValidationError('未知授权来源：%r' % (grant_source,))
        if grant_source == 'purchase':
            if ledger_id is None:
                raise ValidationError('购买授权必须带真实 ledger_id。')
            _as_int(ledger_id, 'ledger_id', minimum=1)
        elif ledger_id is not None:
            raise ValidationError('%s 授权不能带 ledger_id（不伪造扣费流水）。' % grant_source)
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                existing = self._entitlement_row(con, user_id, kind, ref_id, source_sha)
                if existing is not None:
                    if existing['status'] != 'active':
                        raise InvalidState('授权已撤销，需人工恢复。', entitlement_id=existing['id'])
                    return {'entitlement': dict(existing), 'already': True}
                ent_id = 'ent_' + uuid.uuid4().hex[:16]
                con.execute(
                    'INSERT INTO entitlement'
                    '(id, user_id, kind, ref_id, source_sha, grant_source, granted_at,'
                    ' expires_at, ledger_id, status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (ent_id, user_id, kind, ref_id, source_sha, grant_source, now,
                     expires_at, ledger_id, 'active'))
                row = dict(con.execute('SELECT * FROM entitlement WHERE id=?',
                                       (ent_id,)).fetchone())
                return {'entitlement': row, 'already': False}

    def purchase_entitlement(self, *, kind, ref_id=None, source_sha=None, idempotency_key,
                             note=None, user_id=None):
        """用积分买断授权：已授权直接返回；否则 hold -> settle -> grant（同一事务）。"""
        kind, ref_id, source_sha = self._normalize_entitlement(kind, ref_id, source_sha)
        self._validate_key(idempotency_key)
        action = _ACTION_FOR_KIND[kind]
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                existing = self._entitlement_row(con, user_id, kind, ref_id, source_sha)
                if existing is not None:
                    if existing['status'] != 'active':
                        raise InvalidState('授权已撤销，需人工恢复。', entitlement_id=existing['id'])
                    return {'entitlement': dict(existing), 'already': True,
                            'charged_milli': 0, 'charged_credits': _credits(0),
                            'balance_milli': self._snapshot(con, user_id, now)['balance_milli']}

                self._close_expired_holds_in_tx(con, user_id, now)
                self._sweep_in_tx(con, user_id, now)
                row = self._auto_resume(con, self._membership_row(con, user_id, now), now)
                bp = self._discount_bp(self._effective_plan(row, now))
                quote = self._quote(action, qty=1, item_id=ref_id, discount_bp=bp)
                quoted = quote['amount_milli']
                snap = self._snapshot(con, user_id, now)
                if quoted > snap['balance_milli']:
                    raise InsufficientCredits(
                        '余额不足：需要 %s 额度，可用 %s 额度。'
                        % (_credits(quoted), _credits(snap['balance_milli'])),
                        available_milli=snap['balance_milli'], required_milli=quoted,
                        shortfall_milli=quoted - snap['balance_milli'])

                job_id = 'job_buy_' + uuid.uuid4().hex[:16]
                hold_entry = self._append(
                    con, user_id=user_id, delta_milli=-quoted, kind='hold', action=action,
                    qty_milli=quote['qty_milli'], unit_milli=quote['unit_milli'],
                    discount_bp=bp, job_id=job_id, idempotency_key=idempotency_key,
                    note=note, now=now)
                settle_entry = self._append(
                    con, user_id=user_id, delta_milli=0, kind='settle', action=action,
                    qty_milli=quote['qty_milli'], unit_milli=quote['unit_milli'],
                    discount_bp=bp, job_id=job_id,
                    idempotency_key=job_id + ':settle', note='entitlement purchase', now=now)
                ent = self._grant_entitlement_in_tx(
                    con, user_id=user_id, kind=kind, ref_id=ref_id, source_sha=source_sha,
                    grant_source='purchase', ledger_id=settle_entry['id'], now=now)
                snap = self._sync_account(con, user_id, now)
                return {'entitlement': ent, 'already': False,
                        'charged_milli': quoted, 'charged_credits': _credits(quoted),
                        'ledger_id': settle_entry['id'], 'hold_id': hold_entry['id'],
                        'job_id': job_id, 'balance_milli': snap['balance_milli'],
                        'action': action}

    def has_entitlement(self, *, kind, ref_id=None, source_sha=None, user_id=None,
                        now=None):
        kind, ref_id, source_sha = self._normalize_entitlement(kind, ref_id, source_sha)
        ts = self._now() if now is None else int(now)
        with self._connect() as con:
            row = self._entitlement_row(con, user_id, kind, ref_id, source_sha)
        if row is None or row['status'] != 'active':
            return False
        return row['expires_at'] is None or int(row['expires_at']) > ts

    def entitlements(self, *, kind=None, user_id=None):
        if kind is not None and kind not in billing_db.ENTITLEMENT_KINDS:
            raise ValidationError('未知授权类型：%r' % (kind,))
        with self._connect() as con:
            if kind is None:
                rows = con.execute('SELECT * FROM entitlement WHERE user_id=?'
                                   ' ORDER BY granted_at DESC, id DESC', (user_id,)).fetchall()
            else:
                rows = con.execute('SELECT * FROM entitlement WHERE user_id=? AND kind=?'
                                   ' ORDER BY granted_at DESC, id DESC',
                                   (user_id, kind)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 会员
    def membership(self, user_id=None):
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                row = self._auto_resume(con, self._membership_row(con, user_id, now), now)
                return self._membership_view(row, now)

    def set_membership(self, plan, *, expire_at=None, months=None, days=None,
                       idempotency_key=None, note=None, user_id=None):
        """激活/续费/切换档位（本轮不接支付，只由运营、卡密或测试调用）。

        续费从最远到期点叠加：当前有效期内再买 30 天 = 原到期日 + 30 天
        （1 天 + 30 天 = 31 天）。会员不是积分，不写 credit_ledger；
        每次变更追加一条 funnel_event('membership_change')，带幂等键防重复延期。
        """
        plan = str(plan or '').strip()
        cfg = (self.pricing().get('plans') or {}).get(plan)
        if not cfg:
            raise ValidationError('未知档位：%r' % (plan,))
        if plan != 'free' and not cfg.get('enabled', True):
            raise ValidationError('该档位已下架，暂不可开通或续费。')
        if idempotency_key is not None:
            self._validate_key(idempotency_key)
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                current = self._membership_row(con, user_id, now)
                if idempotency_key:
                    hit = con.execute(
                        "SELECT 1 FROM funnel_event WHERE user_id=? AND event=?"
                        " AND project_id=?",
                        (user_id, 'membership_change', idempotency_key)).fetchone()
                    if hit is not None:
                        view = self._membership_view(current, now)
                        view.update(duplicate=True, days_added=0)
                        return view
                alive = (current['plan'] != 'free' and current['status'] == 'active'
                         and current['expire_at'] is not None
                         and int(current['expire_at']) > now)
                if plan == 'free':
                    con.execute('UPDATE membership SET plan=?, status=?, started_at=NULL,'
                                ' expire_at=NULL, paused_at=NULL, paused_until=NULL,'
                                ' pause_used=0, updated_at=? WHERE user_id=?',
                                ('free', 'active', now, user_id))
                    added, expiry = 0, None
                else:
                    if expire_at is not None:
                        expiry = int(expire_at)
                        added = max(0, (expiry - now) // DAY)
                    else:
                        if days is not None:
                            span = _as_int(days, 'days', minimum=1)
                        elif months:
                            span = int(months) * 30
                        else:
                            span = self._plan_days(cfg)
                        base = int(current['expire_at']) if alive else now
                        expiry = base + span * DAY
                        added = span
                    con.execute('UPDATE membership SET plan=?, status=?, started_at=?,'
                                ' expire_at=?, paused_at=NULL, paused_until=NULL,'
                                ' pause_used=0, updated_at=? WHERE user_id=?',
                                (plan, 'active', current['started_at'] if alive else now,
                                 expiry, now, user_id))
                props = json.dumps({
                    'plan': plan, 'days_added': added, 'before_plan': current['plan'],
                    'before_expire_at': current['expire_at'], 'after_expire_at': expiry,
                    'note': str(note or '')[:200]}, ensure_ascii=False)
                con.execute(
                    'INSERT OR IGNORE INTO funnel_event'
                    '(id, user_id, event, project_id, props_json, created_at)'
                    ' VALUES (?,?,?,?,?,?)',
                    ('evt_' + uuid.uuid4().hex[:16], user_id, 'membership_change',
                     idempotency_key or ('mem_' + uuid.uuid4().hex[:16]), props, now))
                view = self._membership_view(self._membership_row(con, user_id, now), now)
                view.update(duplicate=False, days_added=added,
                            before_expire_at=current['expire_at'])
                return view

    def pause_membership(self, days, *, user_id=None):
        """按档位配置暂停（年卡默认 1 次、≤60 天）；暂停期间不享受付费档权益。"""
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                row = self._auto_resume(con, self._membership_row(con, user_id, now), now)
                cfg = (self.pricing().get('plans') or {}).get(row['plan']) or {}
                allowed = int(cfg.get('pause_allowed', 0) or 0)
                limit = int(cfg.get('pause_max_days', 60) or 60)
                days = _as_int(days, 'days', minimum=1)
                if allowed <= 0:
                    raise InvalidState('该档位不支持暂停。')
                if days > limit:
                    raise ValidationError('单次暂停不能超过 %d 天。' % limit)
                if row['status'] != 'active':
                    raise InvalidState('当前会员状态不能暂停：%r' % (row['status'],))
                if int(row['pause_used'] or 0) >= allowed:
                    raise InvalidState('该档位暂停次数已用完。')
                if row['expire_at'] is None or int(row['expire_at']) <= now:
                    raise InvalidState('会员已到期，不能暂停。')
                con.execute('UPDATE membership SET status=?, paused_at=?, paused_until=?,'
                            ' pause_used=1, updated_at=? WHERE user_id=?',
                            ('paused', now, now + days * DAY, now, user_id))
                return self._membership_view(self._membership_row(con, user_id, now), now)

    def resume_membership(self, *, user_id=None):
        now = self._now()
        with self._connect() as con:
            with billing_db.transaction(con):
                row = self._membership_row(con, user_id, now)
                if row['status'] != 'paused':
                    raise InvalidState('当前不是暂停状态。')
                paused_at = int(row['paused_at'] or now)
                until = int(row['paused_until'] or now)
                elapsed = max(0, min(now, until) - paused_at)
                expiry = int(row['expire_at'] or now) + elapsed
                con.execute('UPDATE membership SET status=?, expire_at=?, paused_at=NULL,'
                            ' paused_until=NULL, updated_at=? WHERE user_id=?',
                            ('active', expiry, now, user_id))
                return self._membership_view(self._membership_row(con, user_id, now), now)

    def refund_quote(self, *, user_id=None, now=None):
        """按未使用整月退：refund = 实付价 × 未使用整月 / 总月数（只计算，不做支付）。"""
        ts = self._now() if now is None else int(now)
        with self._connect() as con:
            row = self._auto_resume(con, self._membership_row(con, user_id, ts), ts)
        plan = row['plan']
        cfg = self.pricing()['plans'].get(plan) or {}
        total_months = int(cfg.get('months') or 0)
        price_cents = int(cfg.get('price_cents') or 0)
        if plan == 'free' or total_months <= 0 or row['expire_at'] is None:
            return {'plan': plan, 'total_months': 0, 'used_months': 0, 'unused_months': 0,
                    'price_cents': 0, 'refund_cents': 0, 'refund_cny': '0.00',
                    'note': '免费档无可退金额。'}
        remaining = max(0, int(row['expire_at']) - ts)
        unused = min(total_months, remaining // MONTH_SECONDS)
        used = total_months - unused
        refund = price_cents * unused // total_months
        return {'plan': plan, 'total_months': total_months, 'used_months': used,
                'unused_months': unused, 'price_cents': price_cents,
                'refund_cents': refund, 'refund_cny': '%d.%02d' % divmod(refund, 100),
                'note': '按未使用整月比例；已消耗额度与已下载模板不退，支付手续费按通道规则另行扣除。'}

    # ------------------------------------------------------------ 埋点
    def record_event(self, event, *, project_id=None, props=None, user_id=None):
        if not isinstance(event, str) or not event.strip() or len(event) > 80:
            raise ValidationError('event 必填且不超过 80 字符。')
        if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
            raise ValidationError('project_id 必须是非空字符串或 None。')
        if props is not None and not isinstance(props, dict):
            raise ValidationError('props 必须是对象。')
        now = self._now()
        payload = json.dumps(props or {}, ensure_ascii=False)
        with self._connect() as con:
            with billing_db.transaction(con):
                event_id = 'evt_' + uuid.uuid4().hex[:16]
                cur = con.execute('INSERT OR IGNORE INTO funnel_event'
                                  '(id, user_id, event, project_id, props_json, created_at)'
                                  ' VALUES (?,?,?,?,?,?)',
                                  (event_id, user_id, event, project_id, payload, now))
                created = cur.rowcount == 1
                if created:
                    # 主键是 TEXT id，不是 rowid；必须按 event_id 回读。
                    row = dict(con.execute('SELECT * FROM funnel_event WHERE id=?',
                                           (event_id,)).fetchone())
                else:
                    if project_id is None:
                        found = con.execute('SELECT * FROM funnel_event WHERE user_id=?'
                                            ' AND event=? AND project_id IS NULL',
                                            (user_id, event)).fetchone()
                    else:
                        found = con.execute('SELECT * FROM funnel_event WHERE user_id=?'
                                            ' AND event=? AND project_id=?',
                                            (user_id, event, project_id)).fetchone()
                    row = dict(found)
        return {'created': created, 'already': not created, 'event': row}

    def funnel_events(self, *, limit=100, user_id=None):
        with self._connect() as con:
            rows = con.execute('SELECT * FROM funnel_event WHERE user_id=?'
                               ' ORDER BY created_at DESC, id DESC LIMIT ?',
                               (user_id, max(1, min(int(limit), 500)))).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 支付空壳
    def payment_provider(self):
        return PaymentProvider()

    # ------------------------------------------------------------ 内部：账本重放
    def _order_lots(self, lots, at):
        """扣减顺序：先扣最快到期的赠送积分，再扣永久购买积分（同到期按流水先后）。"""
        grants = [lot for lot in lots.values()
                  if lot['kind'] == 'grant' and lot['remaining'] > 0 and (
                      lot['expiring_at'] is None or int(lot['expiring_at']) > at)]
        grants.sort(key=lambda lot: (int(lot['expiring_at'])
                                     if lot['expiring_at'] is not None else _FOREVER, lot['id']))
        permanent = [lot for lot in lots.values()
                     if lot['kind'] == 'purchase' and lot['remaining'] > 0]
        permanent.sort(key=lambda lot: lot['id'])
        return grants + permanent

    def _allocate(self, lots, amount, at):
        alloc, leftover = [], amount
        if leftover <= 0:
            return alloc, 0
        for lot in self._order_lots(lots, at):
            if leftover <= 0:
                break
            take = min(leftover, lot['remaining'])
            if take <= 0:
                continue
            lot['remaining'] -= take
            leftover -= take
            alloc.append([lot['id'], take])
        return alloc, leftover

    def _return_alloc(self, lots, alloc, amount):
        leftover = amount
        for lot_id, taken in reversed(alloc):
            if leftover <= 0:
                break
            give = min(leftover, taken)
            lots[lot_id]['remaining'] += give
            leftover -= give
        return leftover

    def _replay(self, con, user_id, at):
        """从流水重放：每笔正入账是 lot，每笔 hold 按到期顺序占 lot，settle/release 回填。

        返回可花余额（分赠送/购买）、冻结（分赠送/购买）、宽限已过待作废额、下一笔到期。
        O(n) 重放，单用户本地量级足够；上云后再换物化 lot 表。
        """
        rows = [dict(r) for r in con.execute(
            'SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id', (user_id,))]
        lots, jobs = {}, {}
        imbalance = 0
        for row in rows:
            kind, delta = row['kind'], int(row['delta_milli'])
            when = int(row['created_at'])
            if kind in ('grant', 'purchase', 'adjust') and delta > 0:
                if kind == 'grant':
                    expiring = row['expiring_at']
                    if expiring is None and row['expires_at'] is not None:
                        expiring = int(row['expires_at']) + self._grace() * DAY
                    lot_kind = 'grant'
                else:
                    expiring = None
                    lot_kind = 'purchase'
                lots[int(row['id'])] = {'id': int(row['id']), 'kind': lot_kind,
                                        'remaining': delta,
                                        'expires_at': row['expires_at'],
                                        'expiring_at': expiring}
            elif kind == 'void':
                amount = -delta
                lot_id = row['lot_id']
                if lot_id is not None and int(lot_id) in lots:
                    lot = lots[int(lot_id)]
                    take = min(amount, lot['remaining'])
                    lot['remaining'] -= take
                    amount -= take
                else:
                    for lot in self._order_lots(lots, when):
                        if amount <= 0:
                            break
                        take = min(amount, lot['remaining'])
                        lot['remaining'] -= take
                        amount -= take
                imbalance += amount
            elif kind == 'hold':
                alloc, leftover = self._allocate(lots, -delta, when)
                jobs[row['job_id']] = {'alloc': alloc, 'closed': False,
                                       'quoted': -delta, 'created_at': when}
                imbalance += leftover
            elif kind == 'settle':
                job = jobs.get(row['job_id'])
                if job is None:
                    if delta < 0:
                        _, leftover = self._allocate(lots, -delta, when)
                        imbalance += leftover
                    else:
                        imbalance += delta
                    continue
                if delta > 0:
                    imbalance += self._return_alloc(lots, job['alloc'], delta)
                elif delta < 0:
                    extra, leftover = self._allocate(lots, -delta, when)
                    job['alloc'].extend(extra)
                    imbalance += leftover
                job['closed'] = True
            elif kind == 'release':
                job = jobs.get(row['job_id'])
                if job is not None:
                    imbalance += self._return_alloc(lots, job['alloc'], max(0, delta))
                    job['closed'] = True

        granted = purchased = frozen_granted = frozen_purchased = expired_pending = 0
        for lot in lots.values():
            remaining = lot['remaining']
            if remaining <= 0:
                continue
            if (lot['kind'] == 'grant' and lot['expiring_at'] is not None
                    and int(lot['expiring_at']) <= at):
                expired_pending += remaining
            elif lot['kind'] == 'grant':
                granted += remaining
            else:
                purchased += remaining
        for job in jobs.values():
            if job['closed']:
                continue
            for lot_id, amount in job['alloc']:
                lot = lots.get(lot_id)
                if lot is None:
                    continue
                if lot['kind'] == 'grant':
                    frozen_granted += amount
                else:
                    frozen_purchased += amount

        grant_lots = [lot for lot in lots.values()
                      if lot['kind'] == 'grant' and lot['remaining'] > 0
                      and lot['expiring_at'] is not None]
        grant_lots.sort(key=lambda lot: int(lot['expiring_at']))
        next_expiring = None
        for lot in grant_lots:
            next_expiring = {'lot_id': lot['id'], 'remaining_milli': lot['remaining'],
                             'remaining_credits': _credits(lot['remaining']),
                             'expires_at': lot['expires_at'],
                             'expiring_at': lot['expiring_at'],
                             'days_to_void': max(0, (int(lot['expiring_at']) - at) // DAY)}
            break

        return {'lots': list(lots.values()), 'granted_milli': granted,
                'purchased_milli': purchased,
                'frozen_granted_milli': frozen_granted,
                'frozen_purchased_milli': frozen_purchased,
                'expired_pending_milli': expired_pending, 'next_expiring': next_expiring,
                'ledger_total_milli': sum(int(row['delta_milli']) for row in rows),
                'imbalance_milli': imbalance}

    def _sweep_in_tx(self, con, user_id, at, replay=None):
        replay = replay or self._replay(con, user_id, at)
        voided = []
        for lot in replay['lots']:
            if lot['kind'] != 'grant' or lot['remaining'] <= 0:
                continue
            if lot['expiring_at'] is None or int(lot['expiring_at']) > at:
                continue
            amount = lot['remaining']
            key = 'void:%d:%d:%d' % (lot['id'], amount, at)
            try:
                self._append(con, user_id=user_id, delta_milli=-amount, kind='void',
                             action='grant_expired', lot_id=lot['id'],
                             expires_at=lot['expires_at'],
                             expiring_at=lot['expiring_at'],
                             idempotency_key=key,
                             note='赠送积分宽限期结束作废', now=at)
            except IdempotencyConflict:
                continue
            voided.append({'lot_id': lot['id'], 'amount_milli': amount,
                           'amount_credits': _credits(amount),
                           'expiring_at': lot['expiring_at']})
        if voided:
            self._sync_account(con, user_id, at)
        return voided

    def _validate_key(self, key):
        if not isinstance(key, str) or not key.strip():
            raise ValidationError('idempotency_key 必填（扣费入口必须带幂等键）。')
        if len(key) > 200:
            raise ValidationError('idempotency_key 过长（上限 200）。')
        if key.endswith(_RESERVED_SUFFIXES):
            raise ValidationError('idempotency_key 不能以保留后缀结尾：%r' % (_RESERVED_SUFFIXES,))

    def _append(self, con, *, user_id, delta_milli, kind, idempotency_key, action=None,
                qty_milli=None, unit_milli=None, discount_bp=10000, job_id=None, lot_id=None,
                expires_at=None, expiring_at=None, note=None, now=None):
        ts = self._now() if now is None else int(now)
        try:
            cur = con.execute(
                'INSERT INTO credit_ledger'
                '(user_id, delta_milli, kind, action, qty_milli, unit_milli, discount_bp,'
                ' job_id, lot_id, idempotency_key, expires_at, expiring_at, note, created_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (user_id, int(delta_milli), kind, action, qty_milli, unit_milli,
                 int(discount_bp), job_id, lot_id, idempotency_key, expires_at,
                 expiring_at, note, ts))
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflict('幂等键重复：%r' % (idempotency_key,),
                                      idempotency_key=idempotency_key) from exc
        return dict(con.execute('SELECT * FROM credit_ledger WHERE id=?',
                                (cur.lastrowid,)).fetchone())

    def _by_key(self, con, user_id, key):
        row = con.execute('SELECT * FROM credit_ledger WHERE user_id=? AND idempotency_key=?',
                          (user_id, key)).fetchone()
        return dict(row) if row is not None else None

    def _find_hold(self, con, user_id, job_id):
        row = con.execute("SELECT * FROM credit_ledger WHERE user_id=? AND job_id=?"
                          " AND kind='hold' ORDER BY id LIMIT 1", (user_id, job_id)).fetchone()
        return dict(row) if row is not None else None

    def _find_closing(self, con, user_id, job_id):
        row = con.execute("SELECT * FROM credit_ledger WHERE user_id=? AND job_id=?"
                          " AND kind IN ('settle','release') ORDER BY id LIMIT 1",
                          (user_id, job_id)).fetchone()
        return dict(row) if row is not None else None

    def _user_for_job(self, con, job_id):
        """按 job_id 反查这笔冻结属于哪个账号（后台线程结算用）。"""
        if not job_id:
            return None
        row = con.execute("SELECT user_id FROM credit_ledger WHERE job_id=?"
                          " AND kind='hold' ORDER BY id LIMIT 1", (job_id,)).fetchone()
        return row['user_id'] if row is not None else None

    def _ensure_account(self, con, user_id, now):
        con.execute('INSERT OR IGNORE INTO credit_account'
                    '(user_id, balance_milli, frozen_milli, granted_milli,'
                    ' purchased_milli, version, updated_at) VALUES (?,0,0,0,0,0,?)',
                    (user_id, now))

    def _sync_account(self, con, user_id, now, replay=None):
        self._ensure_account(con, user_id, now)
        replay = replay or self._replay(con, user_id, now)
        available = replay['granted_milli'] + replay['purchased_milli']
        frozen = replay['frozen_granted_milli'] + replay['frozen_purchased_milli']
        con.execute('UPDATE credit_account SET balance_milli=?, frozen_milli=?,'
                    ' granted_milli=?, purchased_milli=?, version=version+1,'
                    ' updated_at=? WHERE user_id=?',
                    (available, frozen, replay['granted_milli'], replay['purchased_milli'],
                     now, user_id))
        return {'balance_milli': available, 'frozen_milli': frozen,
                'granted_milli': replay['granted_milli'],
                'purchased_milli': replay['purchased_milli'],
                'frozen_granted_milli': replay['frozen_granted_milli'],
                'frozen_purchased_milli': replay['frozen_purchased_milli']}

    def _snapshot(self, con, user_id, now):
        """余额以流水重放为准；缓存对不上就按账本修复（账本赢）。"""
        self._ensure_account(con, user_id, now)
        replay = self._replay(con, user_id, now)
        available = replay['granted_milli'] + replay['purchased_milli']
        frozen = replay['frozen_granted_milli'] + replay['frozen_purchased_milli']
        cached = con.execute('SELECT * FROM credit_account WHERE user_id=?',
                             (user_id,)).fetchone()
        repaired = (cached['balance_milli'] != available
                    or cached['frozen_milli'] != frozen
                    or cached['granted_milli'] != replay['granted_milli']
                    or cached['purchased_milli'] != replay['purchased_milli'])
        if repaired:
            self._sync_account(con, user_id, now, replay)
        return {'user_id': user_id, 'balance_milli': available,
                'balance_credits': _credits(available),
                'granted_milli': replay['granted_milli'],
                'granted_credits': _credits(replay['granted_milli']),
                'purchased_milli': replay['purchased_milli'],
                'purchased_credits': _credits(replay['purchased_milli']),
                'frozen_milli': frozen, 'frozen_credits': _credits(frozen),
                'frozen_granted_milli': replay['frozen_granted_milli'],
                'frozen_purchased_milli': replay['frozen_purchased_milli'],
                'expired_pending_milli': replay['expired_pending_milli'],
                'ledger_total_milli': replay['ledger_total_milli'],
                'next_expiring': replay['next_expiring'],
                'cache_repaired': repaired, 'imbalance_milli': replay['imbalance_milli']}

    def _ledger_view(self, entry, snap, *, duplicate):
        return {'ledger_id': entry['id'], 'kind': entry['kind'], 'action': entry['action'],
                'delta_milli': int(entry['delta_milli']),
                'delta_credits': _credits(entry['delta_milli']),
                'duplicate': bool(duplicate),
                'balance_milli': snap['balance_milli'],
                'balance_credits': _credits(snap['balance_milli']),
                'granted_milli': snap['granted_milli'],
                'purchased_milli': snap['purchased_milli']}

    def _hold_view(self, entry, snap, *, duplicate, quote=None):
        quoted = -int(entry['delta_milli'])
        return {'hold_id': entry['id'], 'job_id': entry['job_id'], 'action': entry['action'],
                'quoted_milli': quoted, 'quoted_credits': _credits(quoted),
                'qty_milli': entry['qty_milli'], 'unit_milli': entry['unit_milli'],
                'duplicate': bool(duplicate),
                'balance_milli': snap['balance_milli'],
                'balance_credits': _credits(snap['balance_milli']),
                'granted_milli': snap['granted_milli'],
                'purchased_milli': snap['purchased_milli'],
                'frozen_milli': snap['frozen_milli'],
                'frozen_credits': _credits(snap['frozen_milli']),
                'frozen_granted_milli': snap['frozen_granted_milli'],
                'frozen_purchased_milli': snap['frozen_purchased_milli'],
                'quote': quote}

    def _close_view(self, con, hold_row, entry, user_id, now, *, duplicate):
        quoted = -int(hold_row['delta_milli'])
        delta = int(entry['delta_milli'])
        if entry['kind'] == 'settle':
            charged = quoted - delta
            refund = max(0, delta)
        else:
            charged = 0
            refund = quoted
        snap = self._snapshot(con, user_id, now)
        return {'job_id': hold_row['job_id'], 'action': hold_row['action'],
                'ledger_kind': entry['kind'], 'ledger_id': entry['id'],
                'quoted_milli': quoted, 'quoted_credits': _credits(quoted),
                'charged_milli': charged, 'charged_credits': _credits(charged),
                'refund_milli': refund, 'refund_credits': _credits(refund),
                'duplicate': bool(duplicate),
                'balance_milli': snap['balance_milli'],
                'balance_credits': _credits(snap['balance_milli']),
                'granted_milli': snap['granted_milli'],
                'purchased_milli': snap['purchased_milli'],
                'frozen_milli': snap['frozen_milli']}

    # ------------------------------------------------------------ 内部：授权
    def _normalize_entitlement(self, kind, ref_id, source_sha):
        if kind not in billing_db.ENTITLEMENT_KINDS:
            raise ValidationError('授权类型只能是 template / avatar。')
        if kind == 'template':
            if not isinstance(ref_id, str) or not ref_id.strip():
                raise ValidationError('模板授权必须带 ref_id。')
            if source_sha:
                raise ValidationError('模板授权不能带 source_sha。')
            return kind, ref_id.strip(), None
        if not isinstance(source_sha, str) or len(source_sha.strip()) < 16:
            raise ValidationError('形象授权必须带源图 SHA（至少 16 位）。')
        if ref_id:
            raise ValidationError('形象授权不能带 ref_id。')
        return kind, None, source_sha.strip()

    def _entitlement_row(self, con, user_id, kind, ref_id, source_sha):
        if kind == 'template':
            row = con.execute('SELECT * FROM entitlement WHERE user_id=? AND kind=?'
                              ' AND ref_id=?', (user_id, kind, ref_id)).fetchone()
        else:
            row = con.execute('SELECT * FROM entitlement WHERE user_id=? AND kind=?'
                              ' AND source_sha=?', (user_id, kind, source_sha)).fetchone()
        return dict(row) if row is not None else None

    def _grant_entitlement_in_tx(self, con, *, user_id, kind, ref_id, source_sha,
                                 grant_source, ledger_id, now):
        ent_id = 'ent_' + uuid.uuid4().hex[:16]
        con.execute('INSERT INTO entitlement'
                    '(id, user_id, kind, ref_id, source_sha, grant_source, granted_at,'
                    ' expires_at, ledger_id, status) VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (ent_id, user_id, kind, ref_id, source_sha, grant_source, now,
                     None, ledger_id, 'active'))
        return dict(con.execute('SELECT * FROM entitlement WHERE id=?',
                                (ent_id,)).fetchone())

    # ------------------------------------------------------------ 内部：会员
    def _membership_row(self, con, user_id, now):
        row = con.execute('SELECT * FROM membership WHERE user_id=?', (user_id,)).fetchone()
        if row is None:
            con.execute('INSERT INTO membership'
                        '(user_id, plan, status, started_at, expire_at, paused_at,'
                        ' paused_until, pause_used, updated_at)'
                        ' VALUES (?,?,?,?,?,?,?,?,?)',
                        (user_id, 'free', 'active', None, None, None, None, 0, now))
            row = con.execute('SELECT * FROM membership WHERE user_id=?', (user_id,)).fetchone()
        return dict(row)

    def _auto_resume(self, con, row, now):
        """暂停期到点自动恢复：到期日按实际暂停时长顺延。"""
        if row['status'] != 'paused':
            return row
        until = row['paused_until']
        if until is None or now < int(until):
            return row
        paused_at = int(row['paused_at'] or until)
        elapsed = max(0, int(until) - paused_at)
        expiry = int(row['expire_at'] or until) + elapsed
        con.execute('UPDATE membership SET status=?, expire_at=?, paused_at=NULL,'
                    ' paused_until=NULL, updated_at=? WHERE user_id=?',
                    ('active', expiry, now, row['user_id']))
        row = dict(row)
        row.update(status='active', expire_at=expiry, paused_at=None,
                   paused_until=None, updated_at=now)
        return row

    def _effective_plan(self, row, now):
        if row['status'] != 'active':
            return 'free'
        if row['plan'] != 'free' and row['expire_at'] is not None \
                and int(row['expire_at']) > now:
            return row['plan']
        return 'free'

    @staticmethod
    def _plan_days(cfg):
        """档位默认天数：优先 period_days，兼容老的 days；都没有按 30 天。"""
        for key in ('period_days', 'days'):
            value = cfg.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return 30

    def _discount_bp(self, plan):
        return int(self.pricing()['plans'].get(plan, {}).get('discount_bp', 10000))

    def _membership_view(self, row, now):
        plan = self._effective_plan(row, now)
        expiry = row['expire_at']
        days_left = max(0, (int(expiry) - now) // DAY) if expiry is not None else 0
        plan_cfg = (self.pricing().get('plans') or {}).get(row['plan']) or {}
        return {'user_id': row['user_id'], 'plan': plan, 'stored_plan': row['plan'],
                'active': plan != 'free', 'status': row['status'],
                'started_at': row['started_at'], 'expire_at': expiry,
                'days_left': days_left if row['plan'] != 'free' else 0,
                'paused': row['status'] == 'paused',
                'paused_at': row['paused_at'], 'paused_until': row['paused_until'],
                'pause_used': int(row['pause_used'] or 0),
                'pause_allowed': int(plan_cfg.get('pause_allowed', 0) or 0),
                'pause_max_days': int(plan_cfg.get('pause_max_days', 60) or 60),
                'features': self._features(plan)}

    def _features(self, plan):
        """权益下发（G4）：免费档读 free_limits，付费档不限。

        2026-11-14 云端账号：登录后云端下发 entitlements 就以云端为准
        （逐人可改、下次打开即生效）；云端没下发的键才沿用本地配置兜底。
        """
        free = self.pricing().get('free_limits') or {}
        if plan and plan != 'free':
            base = {'exports_per_month': None, 'exports_per_day': None,
                    'max_height': None, 'watermark': False, 'unlimited': True,
                    'asr_unlimited': True, 'all_style_packs': True,
                    'vip_service': True}
        else:
            base = {'exports_per_month': free.get('exports_per_month'),
                    'exports_per_day': free.get('exports_per_day'),
                    'max_height': free.get('max_height'),
                    'watermark': bool(free.get('watermark')),
                    'unlimited': False,
                    'asr_unlimited': False, 'all_style_packs': False,
                    'vip_service': False}
        ent = request_entitlements()
        if not ent:
            return base
        exports = ent['exports_per_month'] if 'exports_per_month' in ent \
            else base['exports_per_month']
        height = ent['max_height'] if 'max_height' in ent else base['max_height']
        return {'exports_per_month': exports,
                'exports_per_day': base['exports_per_day'],
                'max_height': height,
                'watermark': base['watermark'],
                'unlimited': exports is None and height is None,
                'allow_cloud_asr': ent.get('allow_cloud_asr', True),
                'whitelist': bool(ent.get('whitelist')),
                'asr_unlimited': bool(ent.get('asr_unlimited', base['asr_unlimited'])),
                'all_style_packs': bool(ent.get('all_style_packs', base['all_style_packs'])),
                'vip_service': bool(ent.get('vip_service', base['vip_service'])),
                'expires_at': ent.get('expires_at'),
                'source': 'cloud'}

    # ------------------------------------------------------------ 冻结超时（G8）
    def _hold_ttl(self):
        """冻结存活上限：默认 6 小时，可在 billing_pricing.json 的 hold_ttl_seconds 改。"""
        try:
            value = int(self.pricing().get('hold_ttl_seconds') or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else 6 * 3600

    def open_holds(self, *, user_id=None, now=None):
        """当前还挂着的冻结（没有 settle / release 收尾的 hold）。"""
        now = self._now() if now is None else int(now)
        ttl = self._hold_ttl()
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM credit_ledger l WHERE l.user_id=? AND l.kind='hold'"
                " AND NOT EXISTS (SELECT 1 FROM credit_ledger c WHERE c.user_id=l.user_id"
                "   AND c.job_id=l.job_id AND c.kind IN ('settle','release'))"
                " ORDER BY l.id", (user_id,)).fetchall()
        items = []
        for row in rows:
            expire_at = int(row['created_at']) + ttl
            items.append({'job_id': row['job_id'], 'action': row['action'],
                          'frozen_credits': _credits(-int(row['delta_milli'])),
                          'created_at': int(row['created_at']),
                          'expires_at': expire_at, 'overdue': now >= expire_at})
        return {'hold_ttl_seconds': ttl, 'items': items}

    def _close_expired_holds_in_tx(self, con, user_id, now, limit=200):
        ttl = self._hold_ttl()
        deadline = now - ttl
        rows = con.execute(
            "SELECT * FROM credit_ledger l WHERE l.user_id=? AND l.kind='hold'"
            " AND l.created_at<=? AND NOT EXISTS (SELECT 1 FROM credit_ledger c"
            "   WHERE c.user_id=l.user_id AND c.job_id=l.job_id"
            "   AND c.kind IN ('settle','release'))"
            " ORDER BY l.id LIMIT ?", (user_id, deadline, int(limit))).fetchall()
        released = []
        for row in rows:
            entry = self._append(
                con, user_id=user_id, delta_milli=-int(row['delta_milli']), kind='release',
                action=row['action'], qty_milli=row['qty_milli'],
                unit_milli=row['unit_milli'], discount_bp=row['discount_bp'],
                job_id=row['job_id'],
                idempotency_key=str(row['job_id']) + ':release',
                note='冻结超时自动释放（%d 秒）' % ttl, now=now)
            view = self._close_view(con, dict(row), entry, user_id, now, duplicate=False)
            view['reason'] = 'hold_timeout'
            released.append(view)
        return released

    def close_expired_holds(self, *, user_id=None, now=None, limit=200):
        """把超过 TTL 还没结算的冻结全额退回，并落一条 kind='release' 流水。

        任务线程崩溃、进程重启、前端断线都可能留下永不结算的 hold；不扫就会
        永久冻结用户积分。幂等键沿用 <job_id>:release，重复扫描不会重复退款。
        """
        now = self._now() if now is None else int(now)
        with self._connect() as con:
            with billing_db.transaction(con):
                released = self._close_expired_holds_in_tx(con, user_id, now, limit)
                if released:
                    self._sync_account(con, user_id, now)
        return {'hold_ttl_seconds': self._hold_ttl(), 'released': len(released),
                'items': released}


# 公开方法统一解析请求级用户：user_id 未显式传入时取 app.route 设置的当前账号。
# settle / release 不在名单里，它们要按 job_id 反查归属（后台线程结算）。
for _name in ('quote', 'balance', 'ledger', 'expiring_soon', 'sweep_expired', 'grant',
              'hold', 'grant_entitlement', 'purchase_entitlement', 'has_entitlement',
              'entitlements', 'membership', 'set_membership', 'pause_membership',
              'resume_membership', 'refund_quote', 'record_event', 'funnel_events',
              'open_holds', 'close_expired_holds'):
    setattr(BillingService, _name, _with_request_user(getattr(BillingService, _name)))
