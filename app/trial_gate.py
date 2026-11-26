# -*- coding: utf-8 -*-
"""试用只读闸门：到期 / 时间回拨冻结后拦截写请求，GET 查看与导出放行。

本文件按约定先不接进 app/app.py，接入点只有一处：
  1) app/app.py 顶部 import 区加：import trial_gate
  2) Handler.dispatch（app.py:986）在 self.guard(...) 之后、self.route(method) 之前加：
        gate = trial_gate.blocked(method, self.path)
        if gate:
            return self.json(gate, 403)
"""
import license as trial

WRITE_METHODS = ('POST', 'PATCH', 'PUT', 'DELETE')
WRITE_EXEMPT = ('/api/access',)   # 局域网访问码换取，不产生内容
# 只读态仍要能导出已有内容（2026-11-15）：素材包、剪映草稿、项目/备份导出都不拦。
EXEMPT_PATHS = ('/api/clip/pack', '/api/clip/edit-project')
EXEMPT_SUFFIXES = ('/export',)
EXEMPT_PREFIXES = ('/api/auth', '/api/redeem', '/api/member', '/api/share')  # 登录注册登出、兑换开通、在线开通不产生内容，只读态也要能用（2026-11-16 加在线开通）；分享是导出已有稿件（2026-11-25），云端登录态仍由 cloud_gateway 校验
# 删除自己的记录不产生内容，到期/只读态也应能管理自己的数据（2026-11-25）。
EXEMPT_DELETE_PREFIXES = ('/api/intake/history',)


def blocked(method, path):
    """写请求在只读态返回 403 响应体；放行返回 None。"""
    if method not in WRITE_METHODS:
        return None
    clean = path.split('?', 1)[0].rstrip('/') or '/'
    if clean in WRITE_EXEMPT or clean in EXEMPT_PATHS or clean.startswith(EXEMPT_PREFIXES):
        return None
    if method == 'DELETE' and clean.startswith(EXEMPT_DELETE_PREFIXES):
        return None
    if any(clean.endswith(suffix) for suffix in EXEMPT_SUFFIXES):
        return None
    try:
        st = trial.state()
    except Exception as exc:
        return {'error': '授权状态读取失败，暂时只能查看和导出。', 'code': 'trial_unavailable',
                'trial': {'mode': 'unknown', 'days_left': 0, 'read_only': True,
                          'message': str(exc)[:120]}}
    if not st.get('read_only'):
        return None
    return {'error': st.get('message'), 'code': 'trial_readonly', 'trial': st}
