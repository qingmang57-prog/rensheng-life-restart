# -*- coding: utf-8 -*-
"""微信小程序 B 档 · 只读桥接（开发期）。

边界（2026-11-06）：
  - 只暴露 /mp/login /mp/me /mp/tasks；不写上传、任务提交、卡密兑换、支付。
  - /mp/login 没配 AppID/Secret 时**明确失败**，绝不返回模拟 token 或假 openid。
    有凭据也尚未实现 code2session —— 不凭记忆杜撰微信接口地址。
  - 数据来自本机单用户账本与本机 job 表，响应一律带 scope='local_dev_only'。
    D1/D3 要求多客户 + 服务端计量，生产版必须按 openid 做租户隔离，不能直用本模块。
  - 现有 job 记录没有「进度百分比」字段，progress 返回 None，不编造 0~100。
"""
import os

import billing_route

SCOPE = 'local_dev_only'

_STATUS = {
    'queued': 'pending',
    'running': 'running',
    'succeeded': 'success',
    'failed': 'failed',
    'needs_action': 'failed',
    'released': 'released'
}


def handle(method, path, body=None, store=None):
    """返回 (status, payload)；不属于本模块的路径返回 None，交回原路由。"""
    if path == '/mp/login' and method == 'POST':
        return _login(body or {})
    if path == '/mp/me' and method == 'GET':
        return _me()
    if path == '/mp/tasks' and method == 'GET':
        return _tasks(store)
    return None


def _login(body):
    code = str((body or {}).get('code') or '').strip()
    appid = os.environ.get('IP_MP_APPID', '').strip()
    secret = os.environ.get('IP_MP_SECRET', '').strip()
    need = []
    if not code:
        need.append('wx.login 的 code')
    if not appid:
        need.append('IP_MP_APPID')
    if not secret:
        need.append('IP_MP_SECRET')
    if need:
        return 503, {
            'ok': False,
            'code': 'mp_login_not_configured',
            'message': '微信登录未启用，缺少：' + '、'.join(need),
            'need': need,
            'scope': SCOPE
        }
    # 凭据齐了也不假装成功：code2session 尚未实现，等拿到真实 AppID 后按微信官方文档接入。
    return 501, {
        'ok': False,
        'code': 'mp_login_not_implemented',
        'message': '已读到 AppID/Secret，但 code2session 尚未实现，先不返回登录态。',
        'scope': SCOPE
    }


def _me():
    try:
        brief = billing_route.brief()
    except Exception as exc:  # noqa: BLE001
        return 503, {
            'ok': False,
            'code': 'billing_unavailable',
            'message': '账本读取失败：' + str(exc)[:150],
            'scope': SCOPE
        }
    balance = brief.get('balance_credits')
    credits_display = ('%s 额度' % balance) if balance not in (None, '') else '—'
    label = brief.get('plan_label') or brief.get('plan') or '免费'
    if brief.get('membership_active'):
        left = brief.get('membership_days_left')
        membership_text = label + ((' · 剩 %s 天' % left) if left not in (None, '') else '')
    else:
        membership_text = label
    return 200, {
        'ok': True,
        'credits_display': credits_display,
        'membership_text': membership_text,
        'balance_credits': balance,
        'plan': brief.get('plan'),
        'membership_active': bool(brief.get('membership_active')),
        'scope': SCOPE,
        'source': 'billing_route.brief'
    }


def _tasks(store):
    if store is None:
        return 503, {
            'ok': False, 'code': 'store_unavailable',
            'message': '任务库未注入，无法读取任务。', 'scope': SCOPE
        }
    try:
        rows = store.list('job', 20)
    except Exception as exc:  # noqa: BLE001
        return 503, {
            'ok': False, 'code': 'store_unavailable',
            'message': '任务库读取失败：' + str(exc)[:150], 'scope': SCOPE
        }
    out = []
    for row in rows or []:
        item = dict(row)
        out.append({
            'task_id': item.get('id') or '',
            'title': item.get('stage') or item.get('kind') or '未命名任务',
            'status': _STATUS.get(item.get('status'), 'pending'),
            'progress': None,
            'updated_at': item.get('updated_at') or item.get('created_at') or ''
        })
    return 200, {
        'ok': True,
        'list': out,
        'count': len(out),
        'progress_supported': False,
        'scope': SCOPE,
        'source': 'store.list(job)'
    }
