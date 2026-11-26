# -*- coding: utf-8 -*-
"""卡密兑换：管理员生成、用户兑换、一次性使用、同设备防暴力、全程审计。

边界：
  - 只做兑换码，不接第三方支付；
  - 兑换到账走 billing_service.grant(kind='purchase')：购买积分，永久有效，与赠送分桶；
  - 账本只追加；兑换码表只更新 used_at/used_by/ledger_id，不改金额。
"""
import hashlib
import re
import secrets
import time
from decimal import Decimal

import billing_db
import billing_service
import billing_settings

LOCAL_USER_ID = billing_service.LOCAL_USER_ID
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # 32 个：去掉 I/O/0/1
CODE_LEN = 16
_DEVICE = {'id': None}


def _settings():
    return billing_settings.load().get('redeem') or {}


def _db_path(db_path=None):
    return billing_db.default_db_path() if db_path is None else db_path


def _now():
    return int(time.time())


def normalize(code):
    return re.sub(r'[^A-Z0-9]', '', str(code or '').upper())


def _code_hash(normalized):
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def _display(code):
    return '-'.join(code[i:i + 4] for i in range(0, len(code), 4))


def _tail(normalized):
    return normalized[-4:] if normalized else ''


def _fmt(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)) if ts else ''


def device_id():
    """设备标识：优先硬件指纹；取不到时退化为固定值（单机版仍能限制）。"""
    if _DEVICE['id']:
        return _DEVICE['id']
    raw = ''
    try:
        import license
        tokens = license.hardware_tokens()
        raw = repr(tokens.get('hard') or {}) + repr(tokens.get('soft') or {})
    except Exception:  # noqa: BLE001
        raw = ''
    if raw in ('', '{}{}'):
        raw = 'local-device'
    _DEVICE['id'] = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    return _DEVICE['id']


def _generate_one(length):
    return ''.join(secrets.choice(ALPHABET) for _ in range(length))


def _record(db_path, now, device, tail, ok, reason):
    with billing_db.connect(db_path) as con:
        with billing_db.transaction(con):
            con.execute('INSERT INTO redeem_attempt (at, device, code_tail, ok, reason) '
                        'VALUES (?,?,?,?,?)', (now, device, tail, 1 if ok else 0, reason))


def _locked(con, device, now, settings):
    max_failures = int(settings.get('max_failures') or 5)
    lock_seconds = int(settings.get('lock_seconds') or 900)
    rows = con.execute('SELECT ok, at FROM redeem_attempt WHERE device=? '
                       'ORDER BY id DESC LIMIT ?', (device, max_failures)).fetchall()
    if len(rows) < max_failures or any(row['ok'] for row in rows):
        return 0
    remaining = lock_seconds - (now - int(rows[0]['at']))
    return remaining if remaining > 0 else 0


def _fail(code, message):
    return {'ok': False, 'code': code, 'message': message}


def create_codes(*, credits=None, batch, plan=None, plan_days=None, days=None, count=1,
                 created_by='admin', note=None, db_path=None):
    """生成兑换码，返回明文（只此一次）；库里只存哈希。

    二选一：积分码（credits 正整数）或套餐码（plan + plan_days，兑会员时长）。
    """
    plan = str(plan or '').strip() or None
    path = _db_path(db_path)
    billing_db.ensure(path)
    if plan is None:
        if isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
            raise ValueError('额度必须是正整数积分，或指定套餐 plan + plan_days。')
        credits_milli = credits * billing_service.CREDIT_MILLI
        plan_days = None
    else:
        if credits is not None:
            raise ValueError('积分码和套餐码只能二选一，不能同时给 credits 和 plan。')
        cfg = (billing_service.BillingService(db_path=path).pricing()
               .get('plans') or {}).get(plan)
        if not cfg or plan == 'free':
            raise ValueError('价目表里没有这个付费档位：%s' % plan)
        if plan_days is None:
            plan_days = int(cfg.get('period_days') or cfg.get('days') or 30)
        if isinstance(plan_days, bool) or not isinstance(plan_days, int) or plan_days <= 0:
            raise ValueError('套餐天数必须是正整数。')
        credits_milli = 0
    count = int(count)
    if not 1 <= count <= 1000:
        raise ValueError('单次生成数量必须在 1..1000。')
    batch = str(batch or '').strip()
    if not batch:
        raise ValueError('批次不能为空。')
    settings = _settings()
    length = int(settings.get('code_bytes') or 10) * 8 // 5  # 10 bytes ≈ 16 个 base32 字符
    length = max(12, min(24, length))
    now = _now()
    expires_at = now + int(days) * 86400 if days else None
    out = []
    with billing_db.connect(path) as con:
        for _ in range(count):
            for _attempt in range(20):
                code = _generate_one(length)
                try:
                    with billing_db.transaction(con):
                        con.execute(
                            'INSERT INTO redeem_code (code_hash, code_tail, batch, credits_milli, '
                            'plan, plan_days, expires_at, created_at, created_by, note) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (_code_hash(code), _tail(code), batch, credits_milli,
                             plan, plan_days, expires_at, now, created_by, note))
                    break
                except Exception as exc:  # 哈希撞车几乎不可能，重试即可
                    if 'UNIQUE' not in str(exc).upper():
                        raise
            else:
                raise RuntimeError('连续生成失败，请重试。')
            out.append({'code': _display(code), 'credits': _credits(credits_milli),
                        'plan': plan, 'plan_days': plan_days,
                        'batch': batch, 'expires_at': expires_at,
                        'expires_text': _fmt(expires_at) if expires_at else '永久'})
    return out


def redeem(code, *, user_id=LOCAL_USER_ID, db_path=None):
    """兑换一次；任何失败都返回人话字典，不抛异常给 HTTP 层。"""
    path = _db_path(db_path)
    billing_db.ensure(path)
    normalized = normalize(code)
    tail = _tail(normalized)
    settings = _settings()
    now = _now()
    device = device_id()
    if len(normalized) != CODE_LEN or any(ch not in ALPHABET for ch in normalized):
        _record(path, now, device, tail, False, 'invalid_format')
        return _fail('invalid_code', '兑换码格式不对：应为 16 位字母数字，可以带横线。')
    with billing_db.connect(path) as con:
        remaining = _locked(con, device, now, settings)
    if remaining:
        return _fail('too_many_attempts',
                     '尝试次数过多，请 %d 分钟后再试；也可以联系客服。' % max(1, (remaining + 59) // 60))
    code_hash = _code_hash(normalized)
    with billing_db.connect(path) as con:
        row = con.execute('SELECT * FROM redeem_code WHERE code_hash=?', (code_hash,)).fetchone()
    if row is None:
        _record(path, now, device, tail, False, 'not_found')
        return _fail('invalid_code', '兑换码无效，请核对后重试。')
    row = dict(row)
    if row['used_at']:
        _record(path, now, device, tail, False, 'already_used')
        return _fail('already_used',
                     '这个兑换码已经在 %s 使用过，不能重复兑换。' % _fmt(row['used_at']))
    if row['expires_at'] and now > row['expires_at']:
        _record(path, now, device, tail, False, 'expired')
        return _fail('expired', '这个兑换码已于 %s 过期。' % _fmt(row['expires_at']))
    svc = billing_service.BillingService(db_path=path)
    plan = row.get('plan')
    plan_days = row.get('plan_days')
    if plan:
        try:
            svc.set_membership(
                plan, days=int(plan_days or 0),
                idempotency_key='redeem-membership:' + code_hash,
                note='兑换码 %s 尾号 %s' % (row['batch'], row['code_tail']),
                user_id=user_id)
        except billing_service.BillingError as exc:
            _record(path, now, device, tail, False, 'membership:' + exc.code)
            return _fail(exc.code, exc.message)
        ledger_id = None
    else:
        try:
            result = svc.grant(
                row['credits_milli'], idempotency_key='redeem:' + code_hash, kind='purchase',
                action='redeem', user_id=user_id,
                note='兑换码 %s 尾号 %s' % (row['batch'], row['code_tail']))
        except billing_service.BillingError as exc:
            _record(path, now, device, tail, False, 'grant:' + exc.code)
            return _fail(exc.code, exc.message)
        ledger_id = result.get('ledger_id')
    with billing_db.connect(path) as con:
        with billing_db.transaction(con):
            cur = con.execute(
                'UPDATE redeem_code SET used_at=?, used_by=?, ledger_id=? '
                'WHERE id=? AND used_at IS NULL', (now, user_id, ledger_id, row['id']))
            claimed = cur.rowcount == 1
    if not claimed:
        with billing_db.connect(path) as con:
            other = con.execute('SELECT used_at, ledger_id FROM redeem_code WHERE id=?',
                                (row['id'],)).fetchone()
        # 并发下同一码只发一次；若账本条目一致，视为同一次兑换成功返回。
        if not other or (other['ledger_id'] or None) != (ledger_id or None):
            _record(path, now, device, tail, False, 'race_lost')
            return _fail('already_used', '这个兑换码刚刚已被使用，不能重复兑换。')
    _record(path, now, device, tail, True, 'ok')
    snap = svc.balance(user_id=user_id)
    props = {'batch': row['batch'], 'code_tail': row['code_tail'],
             'credits': _credits(row['credits_milli'])}
    if plan:
        props.update(plan=plan, plan_days=int(plan_days or 0))
    try:
        svc.record_event('redeem', props=props, user_id=user_id)
    except billing_service.BillingError:
        pass
    if plan:
        member = svc.membership(user_id=user_id)
        expiry = member.get('expire_at')
        return {'ok': True, 'code': 'ok', 'ledger_id': None,
                'plan': plan, 'plan_days': int(plan_days or 0),
                'expire_at': expiry, 'balance': snap,
                'message': '兑换成功，会员已延长 %d 天，到期时间 %s。'
                           % (int(plan_days or 0), _fmt(expiry) if expiry else '—')}
    return {'ok': True, 'code': 'ok', 'ledger_id': ledger_id,
            'credits': _credits(row['credits_milli']),
            'batch': row['batch'], 'code_tail': row['code_tail'],
            'balance': snap,
            'message': '兑换成功，到账 %s 额度；当前可用 %s（赠送 %s / 购买 %s）。'
                       % (_credits(row['credits_milli']), snap['balance_credits'],
                          snap['granted_credits'], snap['purchased_credits'])}


def _credits(milli):
    return format(Decimal(int(milli)) / billing_service.CREDIT_MILLI, 'f')


def history(*, user_id=LOCAL_USER_ID, limit=20, db_path=None):
    path = _db_path(db_path)
    billing_db.ensure(path)
    limit = max(1, min(int(limit), 200))
    with billing_db.connect(path) as con:
        rows = [dict(row) for row in con.execute(
            'SELECT code_tail, batch, credits_milli, plan, plan_days, used_at, ledger_id, note '
            'FROM redeem_code WHERE used_by=? AND used_at IS NOT NULL '
            'ORDER BY used_at DESC LIMIT ?', (user_id, limit))]
    for row in rows:
        row['credits'] = _credits(row.pop('credits_milli'))
        row['used_text'] = _fmt(row['used_at'])
    return rows


def list_codes(*, batch=None, limit=200, db_path=None):
    path = _db_path(db_path)
    billing_db.ensure(path)
    limit = max(1, min(int(limit), 1000))
    sql = ('SELECT id, code_tail, batch, credits_milli, plan, plan_days, expires_at, '
           'created_at, created_by, used_at, used_by, ledger_id, note FROM redeem_code')
    args = []
    if batch:
        sql += ' WHERE batch=?'
        args.append(batch)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)
    with billing_db.connect(path) as con:
        rows = [dict(row) for row in con.execute(sql, args)]
    for row in rows:
        row['credits'] = _credits(row.pop('credits_milli'))
        row['created_text'] = _fmt(row['created_at'])
        row['expires_text'] = _fmt(row['expires_at']) if row['expires_at'] else '永久'
        row['used_text'] = _fmt(row['used_at']) if row['used_at'] else ''
    return rows


def audit(*, limit=100, db_path=None):
    path = _db_path(db_path)
    billing_db.ensure(path)
    limit = max(1, min(int(limit), 1000))
    with billing_db.connect(path) as con:
        rows = [dict(row) for row in con.execute(
            'SELECT at, device, code_tail, ok, reason FROM redeem_attempt '
            'ORDER BY id DESC LIMIT ?', (limit,))]
    for row in rows:
        row['at_text'] = _fmt(row['at'])
    return rows
