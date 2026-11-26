# -*- coding: utf-8 -*-
"""免费额度与每日/每月配额（2026-11-14 G4）。

边界：
  - 只做"用了几次、还能不能用"，不碰账本；积分扣费仍走 billing_service。
  - 限额真源（t165）：优先云端已验签的 `billing_entitlements.free_limits`；
    未接线/未开闸（默认）时回落 src/billing_pricing.json；
    `BILLING_ENTITLEMENTS_HARD=1` 且取不到云端 ⇒ 抛 `EntitlementsUnavailable`，**不静默降级**；
  - 显式 null ≠ 不限（契约 §3.3）：`exports_per_day / brain_ask_per_day / max_duration_seconds`
    属主理人**待裁**项，`status()` 以 `pendingDecisions` 点名，未裁前既不视为不限也不按 0 拦；
    付费档（month / year）一律不限，返回 None。
  - 计数表 quota_usage 只是缓存；幂等靠 quota_event 的
    UNIQUE(user_id, feature, idem_key)，重复提交不会多扣一次。
  - 周期键按本地时间：日 = 2026-11-14，月 = 2026-11，跨周期自动归零（新键）。
"""
import datetime
import time
import os as _os          # t165：读 BILLING_ENTITLEMENTS_HARD 开关

FEATURES = ('export', 'brain_ask')

# ---------------------------------------------------------------- t165：显式 null 的未裁决处理
# 契约《契约-billing_entitlements-t195.md》§3.3：这三项在真源里是显式 null 且真源自述
# 「以下数字为占位默认值，待用户确认」⇒ 属主理人待裁，客户端**不得自行解释**成"不限"或"0"。
_PENDING_FIELD = {('export', 'day'): 'exports_per_day',
                  ('brain_ask', 'day'): 'brain_ask_per_day'}
_warned_pending = set()


def _pending_for(feature, lim, entitlements):
    """列出该功能里"源值是显式 null、尚未裁决"的字段名（只点名，不改变放行结果）。"""
    out = []
    for (f, period), name in _PENDING_FIELD.items():
        if f != feature:
            continue
        if (lim.get(f) or {}).get(period) is None:
            out.append(name)
    if feature == 'export' and isinstance(entitlements, dict):
        fl = entitlements.get('free_limits')
        if isinstance(fl, dict) and fl.get('max_duration_seconds') is None:
            out.append('max_duration_seconds')
    return sorted(set(out))


def _warn_pending(feature, pend):
    """一次性 WARN（同组合只打一次，防刷屏）；不改返回值、不拦客户。"""
    key = (feature, tuple(pend))
    if key in _warned_pending:
        return
    _warned_pending.add(key)
    try:
        import logging
        logging.getLogger('billing.entitlements').warning(
            '[billing] %s 有 %d 项免费层阈值**待主理人裁决**（显式 null，未裁前不拦截也不视为不限）：%s'
            '——依据《契约-billing_entitlements-t195.md》§3.3',
            feature, len(pend), ','.join(pend))
    except Exception:
        pass


def _period_key(period, now=None):
    dt = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    if period == 'day':
        return dt.strftime('%Y-%m-%d')
    if period == 'month':
        return dt.strftime('%Y-%m')
    if period == 'total':
        return 'total'
    raise ValueError('未知配额周期：%r' % (period,))


class EntitlementsUnavailable(RuntimeError):
    """t165（契约《契约-billing_entitlements-t195.md》§3.2）：开闸后取不到已验签的云端
    权益表 ⇒ 明确拒绝，**不回落本地兜底**。"""


def _free_limits_source(pricing, entitlements):
    """选免费层阈值真源（t165，契约 §3.2/§5.3）：

     · 给了已验签的云端正文 ⇒ 用云端 `free_limits`，本地 `billing_pricing.json` **不再参与**；
     · 没给且 `BILLING_ENTITLEMENTS_HARD=1` ⇒ 抛 `EntitlementsUnavailable`（不静默降级）；
     · 没给且开关为 0（**现网默认**）⇒ 回落本地表，行为与改造前一字不变。
       🔴 硬顺序（契约 §5.3）：客户端删兜底必须与云端 deploy **同版**发布，
       否则出现"云端还没这份资产、客户端已没兜底"的空窗——所以开关默认必须是关。
    """
    if isinstance(entitlements, dict) and isinstance(entitlements.get('free_limits'), dict):
        return entitlements['free_limits'], 'cloud'
    if _os.environ.get('BILLING_ENTITLEMENTS_HARD', '0').strip() == '1':
        raise EntitlementsUnavailable(
            '免费层阈值应由云端权益表下发；本次未取到已验签的 billing_entitlements'
            '（原因码见 billing_entitlements.entitlements()），已按"不降级"拒绝，不回落本地。')
    return (pricing.get('free_limits') or {}), 'local'


def limits(pricing, plan, overrides=None, entitlements=None):
    """按档位给出各功能限额：{feature: {period: int|None}}。

    🔴 t165 更正（契约 §3.3）：这里的 `None` **不再等于"不限"**。
    真源里 `exports_per_day / brain_ask_per_day / max_duration_seconds` 是**显式 null**、
    属主理人**待裁**项（真源文件自己写着「以下数字为占位默认值，待用户确认」）。
    未裁前本件**既不按"不限"上报、也不按 0 拦截**：`status()` 用 `pendingDecisions` 点名，
    清单本身由 `billing_entitlements.pending_nulls()` 给出。

    overrides：云端**逐人**下发的额度（形如 {'export': {'month': 5}}）；
    entitlements：**全量阈值表**（t165 新增，已验签的云端正文），与逐人额度不是一回事。
    """
    if plan and plan != 'free':
        base = {name: {'month': None, 'day': None} for name in FEATURES}
    else:
        free, _origin = _free_limits_source(pricing, entitlements)

        def _num(value):
            if value is None or value == '':
                return None
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return None

        base = {
            'export': {'month': _num(free.get('exports_per_month')),
                       'day': _num(free.get('exports_per_day'))},
            'brain_ask': {'day': _num(free.get('brain_ask_per_day'))},
        }
    if not isinstance(overrides, dict):
        return base
    for feature, periods in overrides.items():
        if feature not in FEATURES or not isinstance(periods, dict):
            continue
        merged = dict(base.get(feature) or {})
        for period, value in periods.items():
            if period not in ('day', 'month'):
                continue
            if value is None or value == '':
                merged[period] = None
                continue
            try:
                merged[period] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        base[feature] = merged
    return base


def _row(con, user_id, feature, period_key):
    row = con.execute('SELECT * FROM quota_usage WHERE user_id=? AND feature=?'
                      ' AND period_key=?', (user_id, feature, period_key)).fetchone()
    return dict(row) if row is not None else None


def status(con, user_id, feature, pricing, plan, now=None, overrides=None,
           entitlements=None):
    """只读：当前各周期用量与剩余。over = 任一周期已用满。

    t165 新增 `pendingDecisions`：把"显式 null 且主理人未裁"的字段**点名上报**（契约 §3.3），
    免得 `limit is None` 的 `continue` 被下游误读成"这项已裁为不限"。
    """
    now = int(now if now is not None else time.time())
    lim = limits(pricing, plan, overrides, entitlements)
    pend = _pending_for(feature, lim, entitlements)
    out = {'feature': feature, 'plan': plan, 'periods': [], 'blocked': False,
           'message': '', 'binding': None, 'pendingDecisions': pend}
    if pend:
        _warn_pending(feature, pend)
    for period, limit in (lim.get(feature) or {}).items():
        if limit is None:
            continue
        key = _period_key(period, now)
        row = _row(con, user_id, feature, key)
        used = int(row['used']) if row else 0
        item = {'period': period, 'period_key': key, 'used': used, 'limit': limit,
                'remaining': max(0, limit - used), 'exceeded': used >= limit}
        out['periods'].append(item)
        if not out['binding'] or item['remaining'] < out['binding']['remaining']:
            out['binding'] = item
        if item['exceeded']:
            out['blocked'] = True
    if out['blocked']:
        b = out['binding']
        label = '本月' if b['period'] == 'month' else '今天'
        if int(b['limit']) <= 0:
            # 到期态（2026-11-16 拍板）：0 次导出，能看不能导出；按钮照常可点，点了给这句
            out['message'] = ('当前账号暂无导出额度（0 次）。开通会员后可恢复导出，'
                              '已有内容仍可查看。')
        else:
            out['message'] = ('当前账号%s最多 %d 次，已经用完（%d/%d）。'
                              '升级会员可不限次数，或下个周期再试。'
                              % (label, b['limit'], b['used'], b['limit']))
    return out


def consume(con, user_id, feature, pricing, plan, idem_key, *, action='', now=None,
            overrides=None):
    """占用一次额度；同一 idem_key 重复调用只记一次。返回 (ok, status)。

    必须在调用方的事务里执行，保证"计数 + 事件"一起提交。
    """
    now = int(now if now is not None else time.time())
    if feature not in FEATURES:
        raise ValueError('未知配额功能：%r' % (feature,))
    limit_map = limits(pricing, plan, overrides).get(feature) or {}
    if all(value is None for value in limit_map.values()):
        return True, {'feature': feature, 'plan': plan, 'periods': [], 'blocked': False,
                      'message': '', 'binding': None, 'unlimited': True}
    peek = status(con, user_id, feature, pricing, plan, now, overrides)
    # 先认幂等：同一笔重复提交（比如重试导出同一作品）不该因为额度刚好用满被拒。
    exists = con.execute('SELECT 1 FROM quota_event WHERE user_id=? AND feature=?'
                         ' AND idem_key=?', (user_id, feature, str(idem_key))).fetchone()
    if exists:
        peek['duplicate'] = True
        return True, peek
    if peek['blocked']:
        return False, peek
    for item in peek['periods']:
        con.execute('INSERT INTO quota_usage (user_id, feature, period_key, used,'
                    ' limit_value, updated_at) VALUES (?,?,?,1,?,?)'
                    ' ON CONFLICT(user_id, feature, period_key) DO UPDATE SET'
                    ' used=used+1, limit_value=excluded.limit_value,'
                    ' updated_at=excluded.updated_at',
                    (user_id, feature, item['period_key'], item['limit'], now))
    # 事件只记一行（ux_quota_event 唯一）；退款时按 created_at 把当时占过的周期一起退回。
    con.execute('INSERT INTO quota_event (user_id, feature, period_key, idem_key,'
                ' action, created_at) VALUES (?,?,?,?,?,?)',
                (user_id, feature, peek['binding']['period_key'] if peek['binding'] else '',
                 str(idem_key), action, now))
    after = status(con, user_id, feature, pricing, plan, now)
    after['duplicate'] = False
    return True, after


def refund(con, user_id, feature, idem_key, now=None):
    """打包失败时把刚占用的额度退回（删除事件 + 计数减一，不会减到负数）。"""
    now = int(now if now is not None else time.time())
    rows = con.execute('SELECT * FROM quota_event WHERE user_id=? AND feature=?'
                       ' AND idem_key=?', (user_id, feature, str(idem_key))).fetchall()
    if not rows:
        return False
    for row in rows:
        con.execute('DELETE FROM quota_event WHERE id=?', (row['id'],))
        # consume 会同时累加日与月两个周期，事件里只记了 binding 那一个。
        # 这里按事件时间推出当时占过的所有周期（日键 YYYY-MM-DD → 同时退 YYYY-MM），
        # 只退 binding 会让另一个周期的用量永远多 1，额度越用越少。
        keys = {row['period_key']}
        if len(row['period_key']) == 10:
            keys.add(row['period_key'][:7])
        for key in keys:
            con.execute('UPDATE quota_usage SET used=CASE WHEN used>0 THEN used-1 ELSE 0 END,'
                        ' updated_at=? WHERE user_id=? AND feature=? AND period_key=?',
                        (now, user_id, feature, key))
    return True


def open_usage(con, user_id):
    rows = con.execute('SELECT * FROM quota_usage WHERE user_id=? ORDER BY feature, period_key',
                       (user_id,)).fetchall()
    return [dict(row) for row in rows]


__all__ = ['FEATURES', 'limits', 'status', 'consume', 'refund', 'open_usage']
