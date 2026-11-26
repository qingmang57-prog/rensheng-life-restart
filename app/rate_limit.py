# -*- coding: utf-8 -*-
"""频控与去重（P0-5）：预检 + 硬闸门两层，默认只观察不拦截。

规则来自切片工坊的成熟做法（每账号+动作日限、最小间隔、单作品 24h 去重、单账号并发 1），
先只读展示一周再开硬拦：
- 环境变量 IP_RATE_LIMIT_MODE=enforce 才真正跳过/拒绝；默认 observe 只标记不拦。
- 只有成功的采集消耗日额度（失败不计），符合“失败不消耗额度”的既定口径。
- 所有计数来自 rate_limit_event 表的真实行，不维护内存计数器，重启后口径一致。
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import contracts

KIND = 'rate_limit_event'

RULES = {
    'collect': {'label': '采集数据', 'daily_limit': 60, 'min_interval_seconds': 30,
                'dedupe_hours': 24, 'max_concurrency': 1},
    'publish_prepare': {'label': '准备发布资料', 'daily_limit': 10, 'min_interval_seconds': 300,
                        'dedupe_hours': 0, 'max_concurrency': 1},
}

CODE_BUSY = 'busy'
CODE_DUPLICATE = 'duplicate_skipped'
CODE_COOLING = 'cooling'
CODE_DAILY = 'daily_limit_exceeded'


def mode():
    value = (os.getenv('IP_RATE_LIMIT_MODE') or 'observe').strip().lower()
    return 'enforce' if value == 'enforce' else 'observe'


def _parse(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None


def _day_start(now):
    local = now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _events(store, action, account_id):
    rows = [e for e in store.list(KIND, 20000)
            if e.get('action') == action and (not account_id or e.get('account_id') == account_id)]
    return rows


def precheck(store, action, account_id='', work_id='', now=None):
    """预检：固定返回 {allowed, would_block, blocked, mode, code, reason,
    remaining_today, next_allowed_at, duplicate}。"""
    rule = RULES.get(action)
    if rule is None:
        raise ValueError(f'未知频控动作：{action}（可用：{"/".join(RULES)}）')
    now = now or datetime.now(timezone.utc)
    rows = _events(store, action, account_id)
    day_start = _day_start(now)
    today_ok = [e for e in rows if e.get('status') == 'ok'
                and (_parse(e.get('started_at')) or now) >= day_start]
    remaining = max(0, rule['daily_limit']-len(today_ok))

    started = [(e, _parse(e.get('started_at'))) for e in rows]
    started = [(e, t) for e, t in started if t is not None]
    running = [e for e, _ in started if not e.get('finished_at')]
    successful = [(e, t) for e, t in started if e.get('status') == 'ok']
    last_ok = max((t for _, t in successful), default=None)

    code, reason, next_at = None, '', None
    if running and len(running) >= rule['max_concurrency']:
        code = CODE_BUSY
        reason = f'同一账号已有 {len(running)} 个{rule["label"]}任务在跑，先等它结束。'
    elif rule.get('dedupe_hours') and work_id:
        since = now-timedelta(hours=rule['dedupe_hours'])
        recent = [(e, t) for e, t in successful
                  if e.get('work_id') == work_id and t >= since]
        if recent:
            code = CODE_DUPLICATE
            last_at = max(t for _, t in recent)
            next_at = (last_at+timedelta(hours=rule['dedupe_hours'])).isoformat(timespec='seconds')
            reason = (f'该作品 {rule["dedupe_hours"]} 小时内已采集过（{last_at.isoformat(timespec="seconds")}），'
                      f'本次标记 duplicate_skipped，{next_at} 之后可再采。')
    if code is None and last_ok and rule.get('min_interval_seconds'):
        next_time = last_ok+timedelta(seconds=rule['min_interval_seconds'])
        if now < next_time:
            code = CODE_COOLING
            next_at = next_time.isoformat(timespec='seconds')
            reason = (f'{rule["label"]}冷却中：同一账号每 {rule["min_interval_seconds"]} 秒一次，'
                      f'{next_at} 后可再来。')
    if code is None and remaining <= 0:
        code = CODE_DAILY
        reason = (f'今日{rule["label"]}额度已用完（上限 {rule["daily_limit"]} 次），'
                  f'剩余 0 次；明日 00:00 重置。')
    enforce = mode() == 'enforce'
    blocked = code is not None and enforce
    result = {'action': action, 'mode': mode(), 'code': code, 'reason': reason,
              'remaining_today': remaining, 'next_allowed_at': next_at,
              'duplicate': code == CODE_DUPLICATE,
              'would_block': code is not None,
              'allowed': not blocked, 'blocked': blocked}
    return result


def guard(store, action, account_id='', work_id='', now=None):
    """闸门：observe 模式只标记（blocked=False），enforce 才真正拦。"""
    return precheck(store, action, account_id, work_id, now)


def begin(store, action, account_id='', work_id=''):
    """登记一次尝试开始（running），用于并发与冷却判定。"""
    record = {'id': 'rate_'+uuid.uuid4().hex[:16], 'action': action,
              'account_id': account_id or '', 'work_id': work_id or '',
              'status': 'running', 'code': None, 'message': '',
              'started_at': contracts.now_ms(), 'finished_at': None}
    return store.put(KIND, record)


def finish(store, event, status='ok', code=None, message=''):
    """收尾：status=ok 才计入日额度；failed/needs_action 不消耗额度。"""
    return store.patch(KIND, event['id'], {
        'status': status, 'code': code, 'message': str(message or '')[:300],
        'finished_at': contracts.now_ms()})
