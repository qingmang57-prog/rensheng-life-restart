"""违禁词审核的 HTTP 接入层（独立文件，主程序只需 2 行）。

把本文件放到 app/ 后，在 app.py 的 GET/POST 分发处加：
    import compliance_route
    if compliance_route.route(self, method, bits, query): return

路由：
    POST /api/compliance/check   {"text": "...", "llm": false}
    POST /api/compliance/check   {"parts": [{"text": "..."}, ...]}
    POST /api/compliance/feedback {"term": "...", "level": "block"}  误报反馈，存数据目录
    GET  /api/compliance/lexicon 词库版本与来源清单
"""
from __future__ import annotations

import compliance


def route(handler, method, bits, query=None):
    if method == 'POST' and bits == ['api', 'compliance', 'check']:
        data = handler.body()
        if not isinstance(data, dict):
            raise ValueError('请求体必须是 JSON 对象。')
        text = data.get('text')
        if text is None:
            text = compliance.draft_text(data.get('parts'))
        text = str(text or '')
        if len(text) > compliance.MAX_TEXT:
            raise ValueError('待检文本超过 %d 字，请分段检查。' % compliance.MAX_TEXT)
        try:
            handler.json(compliance.check(text, use_llm=bool(data.get('llm'))))
        except compliance.LexiconError as exc:
            handler.json({'ok': False, 'error': 'LEXICON_UNAVAILABLE', 'message': str(exc)}, status=503)
        return True
    if method == 'POST' and bits == ['api', 'compliance', 'feedback']:
        handler.json(compliance.record_feedback(handler.body()))
        return True
    if method == 'GET' and bits == ['api', 'compliance', 'lexicon']:
        try:
            handler.json(compliance.lexicon_meta())
        except compliance.LexiconError as exc:
            handler.json({'ok': False, 'error': 'LEXICON_UNAVAILABLE', 'message': str(exc)}, status=503)
        return True
    return False
