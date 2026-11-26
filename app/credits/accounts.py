"""Users, membership tiers and the credit-account cache row.

Design source: 设计稿 §二 会员档位 / schema-credits.sql users+membership+credit_account.
Membership only decides: 能不能用 / 便宜多少 / 额度多少. It never replaces credits.
"""
import hashlib
import hmac
import json
import secrets
import time
import uuid

from . import db_migrate
from .errors import AccountError, AccountNotFound, AccountDisabled, ValidationError

PLANS = ('free', 'member', 'svip')

# 设计稿 §二：折扣 1.00 / 0.80 / 0.60（月费与月赠待用户定价，不在本模块硬编码）。
PLAN_DEFAULTS = {
    'free': {'discount_bp': 10000, 'quota': {'platforms': 1, 'accounts': 1, 'projects': 3}},
    'member': {'discount_bp': 8000, 'quota': {'platforms': 5, 'accounts': 10, 'projects': None}},
    'svip': {'discount_bp': 6000, 'quota': {'platforms': None, 'accounts': None, 'projects': None}},
}

USER_STATUSES = ('active', 'disabled')
PBKDF2_ITERATIONS = 120_000


def hash_password(password, *, iterations=PBKDF2_ITERATIONS):
    """PBKDF2-SHA256, salted. Never store or log the plaintext."""
    if not isinstance(password, str) or not password:
        raise ValidationError('密码不能为空。')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, int(iterations))
    return 'pbkdf2_sha256$%d$%s$%s' % (int(iterations), salt.hex(), digest.hex())


def verify_password(password, encoded):
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, iterations, salt_hex, digest_hex = encoded.split('$')
        if scheme != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                     bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _now(now=None):
    return int(now if now is not None else time.time())


def _row(row):
    return dict(row) if row is not None else None


def _user_dict(row):
    data = _row(row)
    if data is None:
        return None
    data.pop('password_hash', None)  # never leak the hash to callers
    return data


def create_user(con, *, password_hash, phone=None, email=None, user_id=None,
                status='active', now=None):
    """Create a user + empty credit account + free membership in one transaction."""
    if not isinstance(password_hash, str) or not password_hash:
        raise ValidationError('password_hash 必填。')
    if status not in USER_STATUSES:
        raise ValidationError('未知用户状态：%r' % (status,))
    user_id = user_id or ('usr_' + uuid.uuid4().hex[:16])
    ts = _now(now)
    with db_migrate.transaction(con):
        try:
            con.execute('INSERT INTO users(id, phone, email, password_hash, status, created_at)'
                        ' VALUES (?,?,?,?,?,?)',
                        (user_id, phone, email, password_hash, status, ts))
        except db_migrate.sqlite3.IntegrityError as exc:
            raise AccountError('账号已存在（手机号/邮箱/user_id 重复）。') from exc
        ensure_account(con, user_id, now=ts)
        con.execute('INSERT OR IGNORE INTO membership'
                    '(user_id, plan, expire_at, grace_days, discount_bp, quota_json, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?)',
                    (user_id, 'free', None, 3, 10000,
                     json.dumps(PLAN_DEFAULTS['free']['quota'], ensure_ascii=False), ts))
    return get_user(con, user_id)


def get_user(con, user_id):
    return _user_dict(con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())


def require_active_user(con, user_id):
    user = get_user(con, user_id)
    if user is None:
        raise AccountNotFound('用户不存在：%r' % (user_id,))
    if user['status'] != 'active':
        raise AccountDisabled('用户已被禁用：%r' % (user_id,))
    return user


def set_user_status(con, user_id, status, *, now=None):
    if status not in USER_STATUSES:
        raise ValidationError('未知用户状态：%r' % (status,))
    with db_migrate.transaction(con):
        cur = con.execute('UPDATE users SET status=? WHERE id=?', (status, user_id))
    if cur.rowcount != 1:
        raise AccountNotFound('用户不存在：%r' % (user_id,))
    return get_user(con, user_id)


def ensure_account(con, user_id, *, now=None):
    """credit_account is a cache; created at zero and always recomputed from the ledger."""
    ts = _now(now)
    con.execute('INSERT OR IGNORE INTO credit_account'
                '(user_id, balance_milli, frozen_milli, version, updated_at)'
                ' VALUES (?,0,0,0,?)', (user_id, ts))
    return _row(con.execute('SELECT * FROM credit_account WHERE user_id=?', (user_id,)).fetchone())


def get_account(con, user_id):
    return _row(con.execute('SELECT * FROM credit_account WHERE user_id=?', (user_id,)).fetchone())


def default_membership(user_id, now=None):
    ts = _now(now)
    return {'user_id': user_id, 'plan': 'free', 'expire_at': None, 'grace_days': 3,
            'discount_bp': 10000, 'quota': dict(PLAN_DEFAULTS['free']['quota']),
            'updated_at': ts}


def get_membership(con, user_id, *, now=None):
    row = _row(con.execute('SELECT * FROM membership WHERE user_id=?', (user_id,)).fetchone())
    if row is None:
        return default_membership(user_id, now)
    try:
        row['quota'] = json.loads(row.pop('quota_json') or '{}')
    except ValueError:
        row['quota'] = {}
    return row


def set_membership(con, user_id, plan, *, expire_at=None, grace_days=None,
                   discount_bp=None, quota=None, now=None):
    """Grant/upgrade a plan. `expire_at=None` means "no paid membership" per schema."""
    if plan not in PLANS:
        raise ValidationError('未知会员档：%r' % (plan,))
    defaults = PLAN_DEFAULTS[plan]
    bp = defaults['discount_bp'] if discount_bp is None else int(discount_bp)
    if not 0 <= bp <= 10000:
        raise ValidationError('discount_bp 必须在 0..10000（10000=原价）。')
    grace = 3 if grace_days is None else int(grace_days)
    if grace < 0:
        raise ValidationError('grace_days 不能为负。')
    quota_obj = defaults['quota'] if quota is None else quota
    if not isinstance(quota_obj, dict):
        raise ValidationError('quota 必须是 dict。')
    if expire_at is not None:
        expire_at = int(expire_at)
    ts = _now(now)
    with db_migrate.transaction(con):
        con.execute('INSERT OR REPLACE INTO membership'
                    '(user_id, plan, expire_at, grace_days, discount_bp, quota_json, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?)',
                    (user_id, plan, expire_at, grace, bp,
                     json.dumps(quota_obj, ensure_ascii=False), ts))
    return get_membership(con, user_id, now=ts)


def effective_membership(con, user_id, *, now=None):
    """Resolve the plan actually in force now.

    Rules (设计稿 §二): 会员到期 ≠ 不能按量用 → fall back to free discount, not a block.
    `grace_days` gives a paid plan a short grace window after expire_at.
    """
    ts = _now(now)
    raw = get_membership(con, user_id, now=ts)
    plan = raw.get('plan') or 'free'
    result = {
        'plan': 'free',
        'source_plan': plan,
        'expire_at': raw.get('expire_at'),
        'grace_days': raw.get('grace_days', 3),
        'quota': raw.get('quota', {}),
        'expired': False,
    }
    if plan in ('member', 'svip') and raw.get('expire_at') is not None:
        limit = int(raw['expire_at']) + int(raw.get('grace_days', 3)) * 86400
        if ts <= limit:
            result['plan'] = plan
        else:
            result['expired'] = True
    # free tier always has a discount of 10000 regardless of stored column.
    if result['plan'] == 'free':
        result['discount_bp'] = 10000
    else:
        result['discount_bp'] = int(raw.get('discount_bp') or PLAN_DEFAULTS[result['plan']]['discount_bp'])
    return result
