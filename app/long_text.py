# -*- coding: utf-8 -*-
"""长文本送模型前的统一切段、上限检查与合并（2026-11-16）。

网关单条 content 上限 20000 字，按 24000 准备的正文会被整条拒绝。
所有把长正文送 providers.llm_json 的地方都走这里：
- 15000 字/段，按中文句末标点和换行切，段间重叠 200 字；
- 每段序列化后检查是否超过 19000，超了先缩旁路列表（cues），再不行就缩小段长重切，正文不截；
- 短文本仍走一次调用，行为与改造前一致；
- 合并只做归纳与去重，不新增原结果里没有的判断。
"""
import json

SEGMENT_CHARS = 15000
SEGMENT_MIN = 1200          # t205：重切下限。再小就失去上下文，宁可报「装不下」也不切成碎片
SEGMENT_OVERLAP = 200
PAYLOAD_LIMIT = 19000
SENTENCE_END = '。！？!?\n'


def split_text(text, limit=SEGMENT_CHARS, overlap=SEGMENT_OVERLAP):
    """按句子边界把长文本切段，段间保留 overlap 字重叠；短文本原样返回一段。"""
    text = text or ''
    if len(text) <= limit:
        return [text]
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind(ch) for ch in SENTENCE_END)
            if cut >= limit // 2:
                end = start + cut + 1
        chunks.append(text[start:end])
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def payload_size(payload):
    return len(json.dumps(payload, ensure_ascii=False))


def fit_cues(payload, limit=PAYLOAD_LIMIT):
    """只缩 cues 旁路列表：字幕/正文一个字都不截。"""
    while payload.get('cues') and payload_size(payload) > limit:
        cues = payload['cues']
        payload['cues'] = cues[:max(0, int(len(cues) * 0.6))] if len(cues) > 1 else []
    return payload


def _plan(text, build, limit, shrink):
    text = text or ''
    # 快路径：整段正文一次装得下就直返（绝大多数回合走这里，行为与改造前一致）。
    head = shrink(build(text)) if shrink else build(text)
    if len(text) <= limit and payload_size(head) <= PAYLOAD_LIMIT:
        return [text], [head]
    # t205：短正文 + 大旁路（档案/素材）也会超上限，不能因为 len(text) <= limit 就跳过预算循环。
    # 从最大段长往下试，每轮缩旁路（shrink）再按当前额度重切，正文一个字都不截。
    size = limit
    for _ in range(16):
        segments = split_text(text, limit=size)
        payloads = [shrink(build(seg)) if shrink else build(seg) for seg in segments]
        if all(payload_size(p) <= PAYLOAD_LIMIT for p in payloads):
            return segments, payloads
        if size <= SEGMENT_MIN:
            break
        size = max(SEGMENT_MIN, size * 3 // 4)
    raise ValueError('长文本分段后仍超过单次请求上限，请缩短内容后重试。')


def run(text, build, call, tag, shrink=None, progress=None, merge=None):
    """切段→逐段调用→（可选）合并；短文本仍只调用一次。

    build(segment) -> payload
    call(payload, tag) -> 模型结果；tag 形如 base:single / base:seg1/3
    progress(index, total, merging) -> 界面阶段文案（可选）
    merge(results) -> 合并后的结果（可选；不传则返回逐段结果列表）
    """
    segments, payloads = _plan(text, build, SEGMENT_CHARS, shrink)
    if len(segments) == 1:
        try:
            return call(payloads[0], tag + ':single')
        except Exception as exc:
            # t205：单次调用贴着预算上限时，模型偶尔回一段不合约定结构的文本（实测 6460 字偶发）。
            # 同一次失败重试一次就够了；再失败才把原因交给上层，不静默降级成别的东西。
            retry_error = exc
        try:
            return call(payloads[0], tag + ':single-retry')
        except Exception as exc:
            raise ValueError('%s（同一次内容重试一次仍失败：%s）'
                             % (str(retry_error)[:200], str(exc)[:160])) from None
    results = []
    for index, payload in enumerate(payloads, 1):
        if progress:
            progress(index, len(segments), False)
        try:
            results.append(call(payload, '%s:seg%d/%d' % (tag, index, len(segments))))
        except Exception as exc:
            raise ValueError('第 %d/%d 段处理失败：%s' % (index, len(segments), str(exc)[:160])) from None
    if merge is None:
        return results
    if progress:
        progress(len(segments), len(segments), True)
    return merge(results)


def merge_json(shape, results, tag):
    """中立合并：只归纳与去重；shape 由调用方描述输出结构。"""
    import providers
    system = ('你是同一个文本分段结果的合并器。输入是同一份文本按顺序切出的多段结果。'
              '只做归纳与去重：不新增原结果里没有的判断、不补充例子、不改变立场、不评价用户。'
              + shape + '只返回 JSON。')
    return providers.llm_json(system, {'segments': results}, tag=tag + ':merge')
