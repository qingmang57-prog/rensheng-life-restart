# -*- coding: utf-8 -*-
"""定时回采调度（P0-6）：只提醒与手动重跑，不代发、不后台自动发请求。

规则（沿用方案）：
- 发布回填后 FIRST_DELAY_MINUTES（默认 15）分钟提醒采一次；
- 成功后每 DAILY_HOURS（默认 24）小时提醒一次；
- 失败按 RETRY_MINUTES（默认 60）自动重试一次，再次失败转 needs_action 并要求本人处理；
- 失败不消耗频控日额度（rate_limit 只统计 status=ok 的采集）。

本模块不启动后台线程、不自动执行采集：due 项只统计并以 API 暴露，由本人点“现在采”触发，
避免无人值守采集带来的登录态/封号风险，也不越过“定时只提醒、不许代发”的红线。
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import contracts

KIND = 'collect_schedule'

STATUS_SCHEDULED = 'scheduled'
STATUS_RETRY = 'retry'
STATUS_NEEDS_ACTION = 'needs_action'


def _minutes(name, default):
    try:
        return max(1, int(os.getenv(name) or default))
    except ValueError:
        return default


def first_delay_minutes():
    return _minutes('IP_COLLECT_FIRST_DELAY_MINUTES', 15)


def daily_hours():
    try:
        return max(1.0, float(os.getenv('IP_COLLECT_DAILY_HOURS') or 24))
    except ValueError:
        return 24.0


def retry_minutes():
    return _minutes('IP_COLLECT_RETRY_MINUTES', 60)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _parse(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _find(store, work_id):
    for item in store.list(KIND, 20000):
        if item.get('platform_work_id') == work_id:
            return item
    return None


def upsert_after_publish(store, work, now=None):
    """发布回填成功后登记首次采集提醒；同一作品只登记一次，不覆盖已有进度。"""
    work_id = work.get('id')
    if not work_id:
        return None
    existing = _find(store, work_id)
    if existing:
        return existing
    now = now or datetime.now(timezone.utc)
    record = {'id': 'collect_'+uuid.uuid4().hex[:16], 'platform_work_id': work_id,
              'account_id': work.get('platform_account_id') or '',
              'status': STATUS_SCHEDULED, 'attempt': 0,
              'due_at': _iso(now+timedelta(minutes=first_delay_minutes())),
              'daily_hours': daily_hours(), 'last_run_at': None, 'last_error': None,
              'last_skipped': None, 'created_at': contracts.now_ms(),
              'updated_at': contracts.now_ms()}
    return store.put(KIND, record)


def list_schedules(store):
    items = sorted(store.list(KIND, 20000), key=lambda x: x.get('due_at') or '')
    return {'items': items, 'total': len(items)}


def due_items(store, now=None):
    """到期待采/待重试的项；needs_action 不算到期（等本人处理）。"""
    now = now or datetime.now(timezone.utc)
    items = []
    for item in store.list(KIND, 20000):
        if item.get('status') not in (STATUS_SCHEDULED, STATUS_RETRY):
            continue
        due = _parse(item.get('due_at'))
        if due and due <= now:
            items.append(item)
    return sorted(items, key=lambda x: x.get('due_at') or '')


def run(store, schedule_id, collect=None, now=None):
    """执行一次采集提醒项：成功顺延到明日；失败 60 分钟后重试一次，再失败转 needs_action。

    只采集数据（reports/metric_snapshot/comment），绝不触发发布。
    """
    item = store.get(KIND, schedule_id)
    if item is None:
        raise KeyError('没有找到该采集提醒。')
    if item.get('status') == STATUS_NEEDS_ACTION:
        pass  # 允许本人手动重跑；成功后自动回到 scheduled
    now = now or datetime.now(timezone.utc)
    if collect is None:
        import feedback_service
        collect = feedback_service.collect_work_now
    attempt = int(item.get('attempt') or 0)
    try:
        outcome = collect(item['platform_work_id'])
    except Exception as exc:  # 采集异常：可读原因落库，不静默
        message = str(exc)[:300]
        if attempt+1 >= 2:
            patch = {'status': STATUS_NEEDS_ACTION, 'attempt': attempt+1,
                     'last_error': message, 'last_run_at': _iso(now),
                     'due_at': _iso(now+timedelta(minutes=retry_minutes())),
                     'updated_at': contracts.now_ms()}
        else:
            patch = {'status': STATUS_RETRY, 'attempt': attempt+1,
                     'last_error': message, 'last_run_at': _iso(now),
                     'due_at': _iso(now+timedelta(minutes=retry_minutes())),
                     'updated_at': contracts.now_ms()}
        saved = store.patch(KIND, item['id'], patch)
        return {'schedule': saved, 'ok': False, 'code': 'collect_failed',
                'message': message}
    skipped = bool(outcome.get('skipped'))
    patch = {'attempt': 0 if not skipped else attempt,
             'last_run_at': _iso(now), 'last_error': None,
             'last_skipped': outcome.get('skip_code'),
             'status': STATUS_SCHEDULED,
             'due_at': _iso(now+timedelta(hours=daily_hours())),
             'updated_at': contracts.now_ms()}
    saved = store.patch(KIND, item['id'], patch)
    return {'schedule': saved, 'ok': True, 'skipped': skipped,
            'code': outcome.get('skip_code'), 'message': outcome.get('message') or '',
            'snapshots_created': outcome.get('snapshots_created'),
            'comments_created': outcome.get('comments_created')}


def summary(store, now=None):
    """调度总览：总数、到期数、需处理数；只读，供页面提醒。"""
    items = store.list(KIND, 20000)
    due = due_items(store, now)
    counts = {}
    for item in items:
        counts[item.get('status')] = counts.get(item.get('status'), 0)+1
    return {'total': len(items), 'due_count': len(due), 'counts': counts,
            'first_delay_minutes': first_delay_minutes(), 'daily_hours': daily_hours(),
            'retry_minutes': retry_minutes(), 'auto_run': False,
            'note': '定时只提醒，不代发、不后台自动采集；到期项由本人点“现在采”执行。'}
