# -*- coding: utf-8 -*-
"""会员在线开通的本机转发（2026-11-16）。

浏览器 → 本机 8791 → 云端 api.jidixh.com。本模块只做转发：
- 带本机会话里的云端 token，云端据此认账号；
- 只转「下单 / 查单 / 取二维码」三个动作，金额与权益全由云端决定；
- 不缓存订单、不改金额、不在本机落任何支付数据。
"""
import re

import auth_service

_ORDER_NO = re.compile(r'^[A-Za-z0-9]{6,40}$')

_MESSAGES = {
    'UNAUTHENTICATED': '登录状态已失效，请重新登录后再购买。',
    'PAY_DISABLED': '在线开通暂未开放，请用兑换码开通。',
    'PLAN_OFF': '月卡已下架，目前只提供年度会员。',
    'PLAN_NOT_FOUND': '这个套餐不存在，请刷新页面后重试。',
    'PREPAY_FAILED': '生成付款二维码失败，请稍后重试。',
    'TOO_MANY_ORDERS': '还有待支付的订单，请先完成或等它超时后再试。',
    'ORDER_NOT_FOUND': '订单不存在，请重新下单。',
    'ORDER_NOT_PAYABLE': '订单已结束，请重新下单。',
    'QR_NOT_READY': '二维码还在生成，请稍后重试。',
    'QR_UNAVAILABLE': '二维码组件异常，请联系管理员。',
    'CLOUD_UNREACHABLE': '连不上服务器，请检查网络后再试。',
    'AGREEMENT_REQUIRED': '请先勾选同意《服务协议》与《隐私政策》，再下单。',
}


def _fail(handler, message, code='', status=400):
    handler.json({'ok': False, 'error': message, 'code': code}, status)
    return True


def route(handler, method, bits, query, store):
    """处理 /api/member/*；不是这些路径返回 False 交给后面的路由。"""
    if len(bits) < 3 or bits[:2] != ['api', 'member']:
        return False
    token = auth_service._request_token(handler)
    row = auth_service._session_row(store, token) if token else None
    cloud_token = str((row or {}).get('cloud_token') or '')
    if not cloud_token:
        return _fail(handler, _MESSAGES['UNAUTHENTICATED'], 'UNAUTHENTICATED', 401)
    try:
        if method == 'POST' and bits[2:] == ['checkout']:
            data = handler.body(limit=8 * 1024) or {}
            # 2026-11-16：勾选必须由服务端强制，不能只靠前端按钮置灰。
            # 前端把勾选结果放在 agreed 里传上来；不是布尔 true 一律本地拒单，
            # 不转发云端、不生成订单。这样直接打接口（脚本、重放、改前端）也下不了单。
            if data.get('agreed') is not True:
                return _fail(handler, _MESSAGES['AGREEMENT_REQUIRED'], 'AGREEMENT_REQUIRED', 400)
            plan = str(data.get('plan') or 'year')[:32]
            # 2026-11-16：只允许年卡（499 元/365 天）；月卡、year_test 测试单一律本地拒单，
            # 不把请求转发给云端，不改金额与权益计算。
            if plan != 'year':
                return _fail(handler, _MESSAGES['PLAN_NOT_FOUND'], 'PLAN_NOT_FOUND', 400)
            payload, _ = auth_service._cloud(
                'POST', '/api/member/checkout',
                {'plan': plan,
                 'idem_key': str(data.get('idem_key') or '')[:64]},
                cloud_token=cloud_token, timeout=20)
            return auth_service._reply(handler, payload)
        if method == 'GET' and len(bits) >= 4 and bits[2] == 'order':
            order_no = str(bits[3] or '')
            if not _ORDER_NO.match(order_no):
                return _fail(handler, '订单号不对。', 'ORDER_NOT_FOUND', 400)
            if len(bits) == 5 and bits[4] == 'qr':
                payload, _ = auth_service._cloud(
                    'GET', '/api/member/order/%s/qr' % order_no,
                    cloud_token=cloud_token, timeout=20)
                return auth_service._reply(handler, payload)
            if len(bits) == 4:
                payload, _ = auth_service._cloud(
                    'GET', '/api/member/order/%s' % order_no,
                    cloud_token=cloud_token, timeout=20)
                return auth_service._reply(handler, payload)
    except auth_service.CloudAuthError:
        return _fail(handler, _MESSAGES['UNAUTHENTICATED'], 'UNAUTHENTICATED', 401)
    except auth_service.CloudUnreachable:
        return _fail(handler, _MESSAGES['CLOUD_UNREACHABLE'], 'CLOUD_UNREACHABLE', 502)
    except auth_service.CloudError as exc:
        code = str(getattr(exc, 'code', '') or '')
        message = _MESSAGES.get(code) or str(exc) or '下单失败，请稍后重试。'
        status = 502 if int(getattr(exc, 'status', 400) or 400) >= 500 else 400
        return _fail(handler, message, code or 'checkout_failed', status)
    return False
