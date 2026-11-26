# -*- coding: utf-8 -*-
"""转写后处理（2026-11-17 / t60）：移植切片工坊 asr_postprocess 的 LLM 纠错与五道护栏。

来源：切片工坊源码树 backend/app/services/asr_postprocess.py（2026-11-17 只读核对）；
只做三处适配：
  1. LLM 调用改走本包 providers.post/secret_store（切片工坊用 llm_service._post_json）；
  2. 去掉切片工坊的 llm_verdict_log / cache_order 开关（本包没有这两套设施）；
  3. 新增 apply_to_transcript()：纠错结果**只按等长逐字替换**回写到单字 words，
     再按 jieba（随包 _vendor）把单字合并成词，时间戳取首/尾字，保证剪点不动。

五道护栏（与切片工坊逐条一致）：
  ① 行数/行号必须一一对应；② 长度漂移 ≤35%；③ 编辑比 ≤40%；
  ④ 数字集合必须一致；⑤ 热词不得被纠掉、语气词不得减少。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import unicodedata

_PROMPT = (
    "你是直播字幕校对员，只做一件事：纠正语音识别（ASR）产生的**明显错字**。\n"
    "【只纠错，不改写】\n"
    "1. 只修改「明显的同音字错误」（如「回为天」应为「会为难」）和「明显的识别乱码」；\n"
    "2. 严禁润色、改写、概括、翻译、删减或补充任何内容；\n"
    "3. 必须原样保留说话人的口误、语气词（嗯/啊/吧/呀/呃/哦）和重复的口语"
    "（如「我我」「对对对」）——这是账号风格，属刻意保留，不得删改；\n"
    "4. 严禁改动人名、数字、价格、时间、地点等事实性内容的含义；\n"
    "5. 无法确定的地方保持原样——宁可不改，不可改错。\n"
    "输入是带行号的字幕行（形如 [3] 文本）。只输出 JSON 数组，每项形如 "
    "{\"i\":3,\"text\":\"修正后的文本\"}，i 为原行号，text 为修正后的文本；"
    "行数、行号必须与输入完全一致。输出形如 {\"lines\":[{\"i\":行号,\"text\":\"文本\"}]} 的 JSON 对象，不要输出任何解释文字。"
)

_LEN_DRIFT_MAX = 0.35
_EDIT_RATIO_MAX = 0.4
_SAFE_MAX_LEN = 400
_KEEP_PARTICLES = "嗯呀吧啊呢吗嘛哦呃唉诶哈嘿噢嗷"
_CHUNK = max(50, int(os.getenv('ASR_CORRECT_CHUNK', '150') or 150))
_TIMEOUT = 120.0
_SENT_FINAL = "。！？!?"
_PUNCT = "，。！？、；：,.!?;:…—·\"'（）()《》〈〉【】「」『』 \t\r\n"


def _content_chars(text):
    return [ch for ch in str(text or '') if ch not in _PUNCT]


def _has_content(text):
    for ch in str(text or ''):
        if unicodedata.category(ch)[0] in ('L', 'N'):
            return True
    return False


def _content_len(text):
    return sum(1 for ch in str(text or '') if unicodedata.category(ch)[0] in ('L', 'N'))


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _levenshtein(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def is_safe_correction(old, new, hotwords=()):
    """五道护栏（切片工坊 _is_safe_correction 原样移植）。"""
    old, new = str(old or ''), str(new or '')
    if not new:
        return False
    if len(old) > _SAFE_MAX_LEN or len(new) > _SAFE_MAX_LEN:
        return False
    if set(re.findall(r'\d+', old)) != set(re.findall(r'\d+', new)):
        return False
    for w in hotwords or ():
        if w and w in old and w not in new:
            return False
    if abs(len(new) - len(old)) / max(len(old), 1) > _LEN_DRIFT_MAX:
        return False
    if _levenshtein(old, new) / max(len(old), 1) > _EDIT_RATIO_MAX:
        return False
    for ch in _KEEP_PARTICLES:
        if new.count(ch) < old.count(ch):
            return False
    return True


def _llm_call(system, payload):
    """走本包 providers.llm_json（代理/直连两种模式都由它路由；不直接拼 HTTP）。"""
    import providers
    return providers.llm_json(system, payload)


def _correct_chunk(texts, base):
    n = len(texts)
    if not n:
        return []
    body = '\n'.join('[%d] %s' % (base + i, t) for i, t in enumerate(texts))
    user = ('以下是 %d 行直播字幕（语音识别生成，可能含错别字），请逐行纠错。\n'
            '输出 JSON 数组，**长度必须 = %d**，每项形如 {"i":行号,"text":"修正后的文本"}；'
            '行号 i 必须与输入一一对应（%d~%d，不能缺、不能重、不能越界）。\n字幕：\n%s'
            % (n, n, base, base + n - 1, body))
    payload = {'count': n, 'base': base, 'lines_text': body,
               'format': '{"lines":[{"i":行号,"text":"修正后的文本"}]}'}
    raw = _llm_call(_PROMPT, payload)
    data = raw
    if isinstance(data, dict):
        data = data.get('lines') or data.get('subtitles') or data.get('result') or []
    if not isinstance(data, list) or len(data) != n:
        return None
    by_i = {}
    for d in data:
        if not isinstance(d, dict):
            return None
        try:
            i = int(d.get('i'))
        except (TypeError, ValueError):
            return None
        if i < base or i >= base + n or i in by_i:
            return None
        by_i[i] = '' if d.get('text') is None else str(d.get('text')).strip()
    if len(by_i) != n:
        return None
    return [by_i[base + i] for i in range(n)]


def correct_lines(subtitles, hotwords=(), task_id=None):
    """LLM 纠错（分段并发、按 base 顺序应用、五道护栏）。返回 (new_subs, stats)。"""
    stats = {'lines': len(subtitles or []), 'fixed': 0, 'blocked': 0, 'chunks_failed': 0}
    if not subtitles:
        return subtitles, stats
    orig = [str(s.get('text') or '') for s in subtitles]
    jobs = [(base, orig[base:base + _CHUNK]) for base in range(0, len(orig), _CHUNK)]
    results = {}
    if jobs:
        workers = max(1, min(int(os.getenv('ASR_CORRECT_PARALLEL', '2') or 2), len(jobs)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {base: ex.submit(_correct_chunk, chunk, base) for base, chunk in jobs}
            for base, fut in futs.items():
                try:
                    results[base] = fut.result()
                except Exception as exc:
                    results[base] = None
                    stats.setdefault('errors', []).append('%s: %s' % (type(exc).__name__, str(exc)[:200]))
    fixed_all = list(orig)
    for base, chunk in jobs:
        fixed = results.get(base)
        if not fixed:
            stats['chunks_failed'] += 1
            continue
        for j, t in enumerate(fixed):
            old = chunk[j]
            new = str(t or '').strip()
            if not new or new == old.strip():
                continue
            if not is_safe_correction(old, new, hotwords):
                stats['blocked'] += 1
                continue
            fixed_all[base + j] = new
            stats['fixed'] += 1
    out = []
    for s, t in zip(subtitles, fixed_all):
        row = dict(s)
        row['text'] = t
        out.append(row)
    return out, stats


def postprocess(subtitles, hotwords=(), task_id=None):
    """切片工坊 postprocess 的等价实现：丢空行 → 短行/残片合并 → LLM 纠错。"""
    if not subtitles:
        return subtitles, {}
    filtered = [dict(s) for s in subtitles if isinstance(s, dict) and _has_content(s.get('text'))]
    merged = _merge_short_lines(filtered)
    corrected, stats = correct_lines(merged, hotwords=hotwords, task_id=task_id)
    stats['merged'] = len(filtered) - len(merged)
    stats['dropped_empty'] = len(subtitles) - len(filtered)
    return corrected, stats


# ---------------------------------------------------------------- 行合并（同切片工坊）
_SHORT_SEC = 0.6
_SHORT_CHARS = 3
_MERGE_MAX_SEC = 8.0
_MERGE_MAX_CHARS = 60


def _is_short(s):
    dur = _to_float(s.get('end_ms')) - _to_float(s.get('start_ms'))
    if dur < _SHORT_SEC * 1000:
        return True
    return _content_len(s.get('text')) < _SHORT_CHARS


def _can_merge(target, src):
    gap = _to_float(src.get('start_ms')) - _to_float(target.get('end_ms'))
    if gap >= 600:
        return False
    if target.get('speaker') != src.get('speaker'):
        return False
    start = min(_to_float(target.get('start_ms')), _to_float(src.get('start_ms')))
    end = max(_to_float(target.get('end_ms')), _to_float(src.get('end_ms')))
    if (end - start) > _MERGE_MAX_SEC * 1000:
        return False
    if len(str(target.get('text') or '')) + len(str(src.get('text') or '')) > _MERGE_MAX_CHARS:
        return False
    return True


def _absorb(target, src, prepend=False):
    target['start_ms'] = round(min(_to_float(target.get('start_ms')), _to_float(src.get('start_ms'))))
    target['end_ms'] = round(max(_to_float(target.get('end_ms')), _to_float(src.get('end_ms'))))
    target['text'] = (str(src.get('text') or '') + str(target.get('text') or '')) if prepend else \
        (str(target.get('text') or '') + str(src.get('text') or ''))


def _merge_short_lines(subtitles):
    lines = [dict(s) for s in subtitles]
    out = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if _is_short(cur):
            if out and _can_merge(out[-1], cur):
                _absorb(out[-1], cur)
                i += 1
                continue
            if i + 1 < len(lines) and _can_merge(lines[i + 1], cur):
                _absorb(lines[i + 1], cur, prepend=True)
                i += 1
                continue
        out.append(cur)
        i += 1
    return out


# ---------------------------------------------------------------- 回写 words（等长才改）
def apply_corrections_to_words(cues_before, cues_after, words):
    """把按行纠错的结果**只按等长逐字替换**回写到单字 words；长度不等则整行跳过。"""
    word_chars = [str(w.get('text') or '') for w in words]
    stream = ''.join(word_chars)
    pos = 0
    replaced = 0
    for before, after in zip(cues_before, cues_after):
        raw = _content_chars(before.get('text'))
        new_chars = _content_chars(after.get('text'))
        span = len(raw)
        if span == 0:
            continue
        if len(new_chars) != span:
            # 长度漂移（合并/增删标点导致）：不写回，保剪点
            pos += span
            continue
        if stream[pos:pos + span] != ''.join(raw):
            # 行内文本与 words 不一致（极少见）：放弃该行
            pos += span
            continue
        for k, ch in enumerate(new_chars):
            if ch != raw[k]:
                idx = pos + k
                # 找到该字符所在的 word（words 每项可能多字）
                acc = 0
                for w in words:
                    t = str(w.get('text') or '')
                    if acc <= idx < acc + len(t):
                        local = idx - acc
                        w['text'] = t[:local] + ch + t[local + 1:]
                        break
                    acc += len(t)
                replaced += 1
        pos += span
    return replaced


# ---------------------------------------------------------------- 单字合并成词
def group_words(words):
    """按 jieba（随包 _vendor）把单字 words 合并成词，时间戳取首字 start / 末字 end。"""
    if not words:
        return words
    try:
        import subtitle_segment
        cut = subtitle_segment._jieba_cut()
    except Exception:
        cut = None
    texts = [str(w.get('text') or '') for w in words]
    stream = ''.join(texts)
    if not stream:
        return words
    spans = []
    if cut is not None:
        idx = 0
        # 拉丁/数字整词、CJK 按 jieba 词边界；逐字符求词边界
        try:
            spans = subtitle_segment._cjk_words(stream)
        except Exception:
            spans = []
    if not spans:
        spans = [(i, i + 1) for i in range(len(stream))]
    out = []
    cursor = 0
    for (a, b) in spans:
        a, b = max(a, cursor), min(b, len(stream))
        if b <= a:
            continue
        # 跨 words 边界合并；一个 word 可能被切成多段（罕见），用重叠区间分配
        start = None
        end = None
        acc = 0
        for w in words:
            t = str(w.get('text') or '')
            w_start, w_end = acc, acc + len(t)
            overlap = min(b, w_end) - max(a, w_start)
            if overlap > 0:
                if start is None:
                    start = int(w.get('start_ms'))
                end = int(w.get('end_ms'))
            acc = w_end
        if start is None:
            continue
        out.append({'id': 'word_%d' % (len(out) + 1), 'text': stream[a:b],
                    'start_ms': start, 'end_ms': max(end or start, start),
                    'speaker': words[0].get('speaker') if len(words) == 1 else _speaker_at(words, a)})
        cursor = b
    return out or words


def _speaker_at(words, offset):
    acc = 0
    for w in words:
        t = str(w.get('text') or '')
        if acc <= offset < acc + len(t):
            return w.get('speaker')
        acc += len(t)
    return None


def apply_to_transcript(data, hotwords=(), task_id=None):
    """转写结果后处理总入口：纠错 → 等长回写 words → 词级分组。"""
    if not isinstance(data, dict):
        return data, {}
    cues = [c for c in (data.get('cues') or []) if isinstance(c, dict)]
    if not cues:
        return data, {}
    fixed_cues, stats = postprocess(cues, hotwords=hotwords, task_id=task_id)
    words = [dict(w) for w in (data.get('words') or []) if isinstance(w, dict)]
    if words:
        replaced = apply_corrections_to_words(cues, fixed_cues, words)
        stats['word_chars_replaced'] = replaced
        grouped = group_words(words)
        stats['words_in'] = len(words)
        stats['words_out'] = len(grouped)
        data['words'] = grouped
    data['cues'] = fixed_cues
    data['text'] = '\n'.join(str(c.get('text') or '') for c in fixed_cues)
    data['postprocess'] = stats
    return data, stats
