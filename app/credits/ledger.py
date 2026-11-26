"""credit_ledger: append-only event store. `credit_account` is only a cache.

Design source: 设计稿 §四 硬规则 4 "账本只追加：credit_account.balance 是缓存，真实余额由
credit_ledger 求和重算；出错可回滚可对账".

Conventions
-----------
* delta_milli is signed: positive = 入账 (grant/purchase/refund), negative = 出账 (hold/settle).
* A hold freezes `quoted` by appending delta = -quoted; the money is released when the job is
  closed:
      settle:  delta = +quoted - actual   (net hold+settle = -actual, 多退少补)
      release: delta = +quoted            (net 0, full refund)
  so `balance = SUM(delta_milli)` is always the available balance and needs no special casing.
* `frozen = -SUM(delta of hold rows with no settle/release row for the same ticket)`.
* idempotency_key is the hard dedup gate (schema UNIQUE). Hold rows use the caller key
  (user-scoped by the service); closing rows use `<ticket_id>:settle` / `<ticket_id>:release`.
* No UPDATE/DELETE is ever issued against credit_ledger (append-only). The only UPDATE in
  this module targets credit_account, the cache.
"""
from . import db_migrate
from .errors import IdempotencyConflict, InsufficientCredits, InvalidState, ValidationError

KINDS = ('purchase', 'grant', 'hold', 'settle', 'release', 'adjust')
CREDIT_KINDS = ('purchase', 'grant', 'adjust')   # positive/negative manual entries
CLOSING_KINDS = ('settle', 'release')


def _now(now=None):
    return int(now if now is not None else db_migrate.now_ts())


def _row(row):
    return dict(row) if row is not None else None


def _require_int(value, name, *, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError('%s 必须是整数（毫分）。' % name)
    if minimum is not None and value < minimum:
        raise ValidationError('%s 不能小于 %s。' % (name, minimum))
    return value


def get(con, idempotency_key):
    return _row(con.execute('SELECT * FROM credit_ledger WHERE idempotency_key=?',
                            (idempotency_key,)).fetchone())


def append(con, *, user_id, delta_milli, kind, idempotency_key, action=None, qty=None,
           unit_milli=None, discount_bp=10000, job_id=None, ticket_id=None, note=None,
           now=None):
    """Append exactly one immutable ledger entry. Duplicate key -> IdempotencyConflict."""
    if not isinstance(user_id, str) or not user_id:
        raise ValidationError('user_id 必填。')
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValidationError('idempotency_key 必填。')
    if kind not in KINDS:
        raise ValidationError('未知流水类型：%r' % (kind,))
    _require_int(delta_milli, 'delta_milli')
    bp = int(discount_bp)
    if not 0 <= bp <= 10000:
        raise ValidationError('discount_bp 必须在 0..10000。')
    if unit_milli is not None:
        _require_int(unit_milli, 'unit_milli', minimum=0)
    if qty is not None:
        try:
            if float(qty) < 0:
                raise ValidationError('qty 不能为负。')
        except (TypeError, ValueError) as exc:
            raise ValidationError('qty 非法：%r' % (qty,)) from exc
    try:
        cur = con.execute('INSERT INTO credit_ledger'
                          '(user_id, delta_milli, kind, action, qty, unit_milli, discount_bp,'
                          ' job_id, ticket_id, idempotency_key, note, created_at)'
                          ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          (user_id, delta_milli, kind, action, qty, unit_milli, bp,
                           job_id, ticket_id, idempotency_key, note, _now(now)))
    except db_migrate.sqlite3.IntegrityError as exc:
        raise IdempotencyConflict('幂等键重复：%r' % (idempotency_key,),
                                  idempotency_key=idempotency_key) from exc
    return _row(con.execute('SELECT * FROM credit_ledger WHERE id=?',
                            (cur.lastrowid,)).fetchone())


def balance(con, user_id):
    """Authoritative available balance: sum of the whole ledger."""
    row = con.execute('SELECT COALESCE(SUM(delta_milli),0) AS total FROM credit_ledger'
                      ' WHERE user_id=?', (user_id,)).fetchone()
    return int(row['total'])


def frozen(con, user_id):
    """Money locked by holds that have not been settled/released yet."""
    row = con.execute(
        'SELECT COALESCE(SUM(-h.delta_milli),0) AS total FROM credit_ledger h'
        " WHERE h.user_id=? AND h.kind='hold' AND NOT EXISTS ("
        '   SELECT 1 FROM credit_ledger c WHERE c.ticket_id=h.ticket_id'
        "   AND c.kind IN ('settle','release'))", (user_id,)).fetchone()
    return int(row['total'])


def find_hold(con, ticket_id):
    return _row(con.execute("SELECT * FROM credit_ledger WHERE ticket_id=? AND kind='hold'"
                            ' ORDER BY id LIMIT 1', (ticket_id,)).fetchone())


def find_closing(con, ticket_id, kind=None):
    if kind is None:
        return _row(con.execute("SELECT * FROM credit_ledger WHERE ticket_id=?"
                                " AND kind IN ('settle','release') ORDER BY id LIMIT 1",
                                (ticket_id,)).fetchone())
    return _row(con.execute('SELECT * FROM credit_ledger WHERE ticket_id=? AND kind=?'
                            ' ORDER BY id LIMIT 1', (ticket_id, kind)).fetchone())


def entries(con, user_id, *, limit=200):
    return [_row(r) for r in con.execute(
        'SELECT * FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT ?',
        (user_id, int(limit)))]


def sync_account(con, user_id, *, now=None):
    """Recompute the cache from the ledger and write it back (the only cache writer)."""
    from . import accounts  # local import: avoids a cycle at module import time
    accounts.ensure_account(con, user_id, now=now)
    available = balance(con, user_id)
    locked = frozen(con, user_id)
    ts = _now(now)
    con.execute('UPDATE credit_account SET balance_milli=?, frozen_milli=?,'
                ' version=version+1, updated_at=? WHERE user_id=?',
                (available, locked, ts, user_id))
    return _row(con.execute('SELECT * FROM credit_account WHERE user_id=?',
                            (user_id,)).fetchone())


def snapshot(con, user_id, *, now=None, repair=True):
    """Read the authoritative balance, and repair the cache when it drifted (ledger wins)."""
    from . import accounts
    accounts.ensure_account(con, user_id, now=now)
    cached = _row(con.execute('SELECT * FROM credit_account WHERE user_id=?',
                              (user_id,)).fetchone())
    available = balance(con, user_id)
    locked = frozen(con, user_id)
    repaired = (cached['balance_milli'] != available) or (cached['frozen_milli'] != locked)
    if repaired and repair:
        cached = sync_account(con, user_id, now=now)
    return {
        'user_id': user_id,
        'balance_milli': available,
        'frozen_milli': locked,
        'cached_balance_milli': cached['balance_milli'],
        'cached_frozen_milli': cached['frozen_milli'],
        'cache_repaired': bool(repaired and repair),
    }


def grant(con, user_id, amount_milli, *, kind='grant', action=None, idempotency_key,
          note=None, now=None):
    """Positive credit entry (purchase / monthly grant / trial / manual top-up)."""
    amount = _require_int(amount_milli, 'amount_milli', minimum=1)
    if kind not in CREDIT_KINDS:
        raise ValidationError('grant 的 kind 必须是 %s 之一。' % (CREDIT_KINDS,))
    with db_migrate.transaction(con):
        row = append(con, user_id=user_id, delta_milli=amount, kind=kind,
                     action=action, idempotency_key=idempotency_key, note=note, now=now)
        sync_account(con, user_id, now=now)
    return row


def hold(con, *, user_id, job_id, ticket_id, action, quoted_milli, idempotency_key,
         qty=None, unit_milli=None, discount_bp=10000, note=None, now=None):
    """Freeze `quoted_milli`. Returns (entry, duplicate). Duplicate returns the first entry."""
    quoted = _require_int(quoted_milli, 'quoted_milli', minimum=0)
    with db_migrate.transaction(con):
        existing = get(con, idempotency_key)
        if existing is not None:
            if existing['kind'] != 'hold' or existing['job_id'] != job_id:
                raise IdempotencyConflict('幂等键已被其它流水占用：%r' % (idempotency_key,),
                                          idempotency_key=idempotency_key)
            return existing, True
        available = balance(con, user_id)
        if quoted > available:
            raise InsufficientCredits(
                '余额不足：需要 %d 毫分，可用 %d 毫分。' % (quoted, available),
                available_milli=available, required_milli=quoted,
                shortfall_milli=quoted - available)
        row = append(con, user_id=user_id, delta_milli=-quoted, kind='hold',
                     action=action, qty=qty, unit_milli=unit_milli,
                     discount_bp=discount_bp, job_id=job_id, ticket_id=ticket_id,
                     idempotency_key=idempotency_key, note=note, now=now)
        sync_account(con, user_id, now=now)
        return row, False


def settle_charge(con, *, hold_row, actual_milli, idempotency_key, note=None, now=None):
    """Close a hold with the actual amount: delta = quoted - actual (多退少补)."""
    actual = _require_int(actual_milli, 'actual_milli', minimum=0)
    quoted = -int(hold_row['delta_milli'])
    with db_migrate.transaction(con):
        existing = get(con, idempotency_key)
        if existing is not None:
            return existing, True
        if find_closing(con, hold_row['ticket_id'], 'release') is not None:
            raise InvalidState('hold 已 release，不能 settle。',
                               ticket_id=hold_row['ticket_id'], current='released')
        delta = quoted - actual
        if delta < 0:
            available = balance(con, hold_row['user_id'])
            if available < -delta:
                raise InsufficientCredits(
                    '结算超出冻结额且余额不足：差额 %d 毫分，可用 %d 毫分。' % (-delta, available),
                    available_milli=available, required_milli=-delta)
        row = append(con, user_id=hold_row['user_id'], delta_milli=delta, kind='settle',
                     action=hold_row['action'], qty=hold_row['qty'],
                     unit_milli=hold_row['unit_milli'], discount_bp=hold_row['discount_bp'],
                     job_id=hold_row['job_id'], ticket_id=hold_row['ticket_id'],
                     idempotency_key=idempotency_key, note=note, now=now)
        sync_account(con, hold_row['user_id'], now=now)
        return row, False


def release_charge(con, *, hold_row, idempotency_key, note=None, now=None):
    """Close a hold with a full refund: delta = +quoted (net zero, no trace left on balance)."""
    quoted = -int(hold_row['delta_milli'])
    with db_migrate.transaction(con):
        existing = get(con, idempotency_key)
        if existing is not None:
            return existing, True
        if find_closing(con, hold_row['ticket_id'], 'settle') is not None:
            raise InvalidState('hold 已 settle，不能 release。',
                               ticket_id=hold_row['ticket_id'], current='settled')
        row = append(con, user_id=hold_row['user_id'], delta_milli=quoted, kind='release',
                     action=hold_row['action'], qty=hold_row['qty'],
                     unit_milli=hold_row['unit_milli'], discount_bp=hold_row['discount_bp'],
                     job_id=hold_row['job_id'], ticket_id=hold_row['ticket_id'],
                     idempotency_key=idempotency_key, note=note, now=now)
        sync_account(con, hold_row['user_id'], now=now)
        return row, False


def reconcile(con, *, user_id=None, now=None):
    """Audit every account: ledger sum is truth, cache is corrected. Returns a report list."""
    if user_id is not None:
        users = [user_id]
    else:
        users = [r['user_id'] for r in con.execute(
            'SELECT user_id FROM credit_account UNION SELECT user_id FROM credit_ledger'
            ' ORDER BY user_id')]
    report = []
    with db_migrate.transaction(con):
        for uid in users:
            before = _row(con.execute('SELECT * FROM credit_account WHERE user_id=?',
                                      (uid,)).fetchone()) or {'balance_milli': 0, 'frozen_milli': 0}
            available = balance(con, uid)
            locked = frozen(con, uid)
            corrected = (before['balance_milli'] != available
                         or before['frozen_milli'] != locked)
            if corrected:
                after = sync_account(con, uid, now=now)
            else:
                after = before
            report.append({'user_id': uid,
                           'ledger_balance_milli': available,
                           'ledger_frozen_milli': locked,
                           'cache_before': {'balance_milli': before['balance_milli'],
                                            'frozen_milli': before['frozen_milli']},
                           'cache_after': {'balance_milli': after['balance_milli'],
                                           'frozen_milli': after['frozen_milli']},
                           'corrected': corrected})
    return report
