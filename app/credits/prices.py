"""price_book reads + quote math. No price is hardcoded here (设计稿 §七 价目热更).

All amounts are integer 毫分 (1 积分 = 1000 毫分). qty may be fractional (minutes), so the
quote uses Decimal and rounds up — never float multiplication on money.
"""
from decimal import Decimal, ROUND_CEILING

from . import db_migrate
from .errors import UnknownAction, ValidationError

UNITS = ('minute', 'piece', 'platform')
SETTLE_RULES = ('on_success', 'on_complete', 'free')
DISCOUNT_FULL = 10000


def _price_dict(row):
    if row is None:
        return None
    data = dict(row)
    data['billable'] = bool(data['billable'])
    return data


def get_price(con, action):
    if not isinstance(action, str) or not action.strip():
        raise ValidationError('action 必填。')
    return _price_dict(con.execute('SELECT * FROM price_book WHERE action=?', (action,)).fetchone())


def require_price(con, action):
    price = get_price(con, action)
    if price is None:
        raise UnknownAction('未在 price_book 定价的动作：%r' % (action,), action=action)
    return price


def list_prices(con):
    return [_price_dict(r) for r in con.execute('SELECT * FROM price_book ORDER BY action')]


def set_price(con, action, *, unit, unit_milli, min_milli=0, billable=True,
              settle_rule='on_success', now=None):
    """Hot price update (admin/ops path). Re-running migrate() keeps this value."""
    if not isinstance(action, str) or not action.strip():
        raise ValidationError('action 必填。')
    if unit not in UNITS:
        raise ValidationError('unit 必须是 %s 之一。' % (UNITS,))
    if settle_rule not in SETTLE_RULES:
        raise ValidationError('settle_rule 必须是 %s 之一。' % (SETTLE_RULES,))
    unit_milli = int(unit_milli)
    min_milli = int(min_milli)
    if unit_milli < 0 or min_milli < 0:
        raise ValidationError('单价/最低消费不能为负。')
    ts = int(now if now is not None else db_migrate.now_ts())
    with db_migrate.transaction(con):
        con.execute('INSERT OR REPLACE INTO price_book'
                    '(action, unit, unit_milli, min_milli, billable, settle_rule, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?)',
                    (action, unit, unit_milli, min_milli, int(bool(billable)), settle_rule, ts))
    return get_price(con, action)


def is_free(price):
    return (not price['billable']) or price['settle_rule'] == 'free' or price['unit_milli'] == 0


def quote_milli(price, qty=1, discount_bp=DISCOUNT_FULL):
    """ceil(unit * qty * discount), clamped to min_milli. Returns integer 毫分."""
    if not isinstance(price, dict):
        raise ValidationError('price 必须是 price_book 行。')
    bp = int(discount_bp)
    if not 0 <= bp <= DISCOUNT_FULL:
        raise ValidationError('discount_bp 必须在 0..10000（10000=原价）。')
    if is_free(price):
        return 0
    try:
        qty_dec = Decimal(str(qty))
    except Exception as exc:  # noqa: BLE001 - Decimal raises several types
        raise ValidationError('qty 非法：%r' % (qty,)) from exc
    if qty_dec < 0:
        raise ValidationError('qty 不能为负。')
    raw = Decimal(int(price['unit_milli'])) * qty_dec * Decimal(bp) / Decimal(DISCOUNT_FULL)
    amount = int(raw.to_integral_value(rounding=ROUND_CEILING))
    return max(int(price.get('min_milli') or 0), amount)


def quote(con, action, qty=1, discount_bp=DISCOUNT_FULL):
    return quote_milli(require_price(con, action), qty=qty, discount_bp=discount_bp)
