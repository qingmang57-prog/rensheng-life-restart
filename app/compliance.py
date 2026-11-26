"""口播稿违禁词审核（独立模块，只新增文件）。

设计约束：
- 纯词库 + 规则即可出结果，不依赖主服务、不依赖网络。
- LLM 只做可选复核：未配置、断网、超时、返回格式错误时保留规则结果，绝不抛错。
- 每个词条必须引用 src/compliance_lexicon.json 里的可公开来源；加载时校验，缺来源直接报错。
- 误杀控制：日常口语、顺序表达、客观数量、引用、否定、非广告语境降级或排除；
  被排除的命中写入 excluded，便于人工复核，不静默丢弃。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import prompt_store

MAX_TEXT = 120000
MAX_LLM_CHARS = 20000
LEVELS = ('block', 'warn', 'hint')
_LEVEL_RANK = {'block': 3, 'warn': 2, 'hint': 1}
LEXICON_PATH = Path(__file__).resolve().parent / 'src' / 'compliance_lexicon.json'


def _cloud_lexicon():
    """2026-11-17（t42）：52 条合规词库是云端核心资产，本地只剩空壳。"""
    try:
        import asset_store
        value = asset_store.get('compliance.lexicon')
    except Exception:
        value = None
    return value if isinstance(value, dict) else None


def _feedback_path():
    """误报反馈只落数据目录（默认 app/.runtime），不进包、不上传。"""
    base = os.getenv('IP_DATA_DIR') or str(Path(__file__).resolve().parent / '.runtime')
    return Path(base) / 'compliance-feedback.jsonl'


def record_feedback(payload):
    """买家点「认为误拦」：记录词条与级别供回收复核；不改本次判定结果。"""
    data = payload if isinstance(payload, dict) else {}
    term = str(data.get('term') or '').strip()
    if not term:
        raise ValueError('请填写要反馈的词。')
    if len(term) > 40:
        raise ValueError('反馈词请控制在 40 字以内。')
    entry = {'time': time.strftime('%Y-%m-%d %H:%M:%S'),
             'term': term,
             'level': str(data.get('level') or '')[:10],
             'note': str(data.get('note') or '')[:200]}
    path = _feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
    return {'ok': True, 'term': term}

_END_CHARS = '。！？!?；;\n'
_CLIP_CHARS = _END_CHARS + '，,、'
_OPEN_QUOTES = '“"「『\''
_CLOSE_QUOTES = '”"」』\''


class LexiconError(RuntimeError):
    """词库缺失或词条没有来源时抛出，避免静默放行。"""


def _as_regex(value):
    return re.compile(value, re.IGNORECASE) if value else None


def _entry_pattern(entry):
    parts = []
    if entry.get('pattern'):
        parts.append('(?:' + entry['pattern'] + ')')
    if entry.get('terms'):
        terms = sorted((str(t) for t in entry['terms']), key=len, reverse=True)
        parts.append('(?:' + '|'.join(re.escape(t) for t in terms) + ')')
    if not parts:
        raise LexiconError('词条 %s 既没有 terms 也没有 pattern。' % entry.get('id'))
    return '|'.join(parts)


def load_lexicon(path=LEXICON_PATH):
    raw = _cloud_lexicon()
    if not isinstance(raw, dict) or not raw.get('entries'):
        path = Path(path)
        if not path.is_file():
            raise LexiconError('合规词库是云端核心资产，请先登录后使用；未登录或离线超过 72 小时时不可用。')
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise LexiconError('词库无法读取或不是有效 JSON：%s' % exc) from None
    if not (raw.get('entries') or []):
        raise LexiconError('合规词库是云端核心资产，请先登录后使用；未登录或离线超过 72 小时时不可用。')
    sources = raw.get('sources') or {}
    entries = raw.get('entries') or []
    if not isinstance(sources, dict) or not isinstance(entries, list) or not entries:
        raise LexiconError('词库结构无效：缺少 sources 或 entries。')
    errors = []
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append('存在非对象词条。')
            continue
        eid = entry.get('id') or '(无 id)'
        level = entry.get('level')
        if level not in LEVELS:
            errors.append('%s 的 level 无效：%r' % (eid, level))
        basis = entry.get('basis')
        if basis not in sources:
            errors.append('%s 的来源 basis 不存在：%r' % (eid, basis))
        else:
            source = sources[basis] or {}
            if not source.get('url') or not source.get('retrieved_at'):
                errors.append('%s 的来源缺少 url 或 retrieved_at。' % eid)
        suggestions = entry.get('suggestions')
        if not isinstance(suggestions, list) or not 1 <= len(suggestions) <= 2:
            errors.append('%s 需要 1-2 条替换建议。' % eid)
        elif any(not str(x).strip() for x in suggestions):
            errors.append('%s 的替换建议不能为空。' % eid)
        try:
            re.compile(_entry_pattern(entry), re.IGNORECASE)
        except re.error as exc:
            errors.append('%s 的正则无效：%s' % (eid, exc))
    if errors:
        raise LexiconError('词库校验失败：\n- ' + '\n- '.join(errors))
    return raw


# 2026-11-17（t42 词库封壳）：模块级不再直接 load——未登录/无缓存时那会直接抛异常、连带整个应用起不来。
# 改成惰性：第一次真正用到词库时才加载；拿不到就抛带引导的 LexiconError，由调用方降级。
_LEXICON = None
_LEXICON_ERROR = ''


def _lexicon():
    global _LEXICON, _LEXICON_ERROR
    if _LEXICON is not None:
        return _LEXICON
    try:
        _LEXICON = load_lexicon()
        _LEXICON_ERROR = ''
        return _LEXICON
    except LexiconError as exc:
        _LEXICON_ERROR = str(exc)
        raise


def _prepare(raw):
    prepared = []
    for entry in raw['entries']:
        prepared.append({
            'entry': entry,
            'rx': re.compile(_entry_pattern(entry), re.IGNORECASE),
            'ex_after': _as_regex(entry.get('exclude_after')),
            'ex_before': _as_regex(entry.get('exclude_before')),
            'dg_after': _as_regex(entry.get('downgrade_after')),
            'dg_before': _as_regex(entry.get('downgrade_before')),
        })
    return prepared


_RULES = None


def _rules():
    global _RULES
    if _RULES is None:
        raw = _lexicon()
        if raw is not None:
            _RULES = _prepare(raw)
    return _RULES or []


def lexicon_meta():
    lex = _lexicon()
    sources = lex['sources']
    return {
        'version': lex.get('version', ''),
        'updated_at': lex.get('updated_at', ''),
        'entry_count': len(lex['entries']),
        'source_count': len(sources),
        'levels': list(LEVELS),
        'exclusion_policy': lex.get('exclusion_policy', {}),
        'sources': [
            {
                'id': key,
                'title': value.get('title', ''),
                'issuer': value.get('issuer', ''),
                'url': value.get('url', ''),
                'published_at': value.get('published_at', ''),
                'retrieved_at': value.get('retrieved_at', ''),
                'note': value.get('note', ''),
            }
            for key, value in sources.items()
        ],
    }


def draft_text(parts):
    """把 project['draft'] 的三段/五段拼成待检文本。"""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, (list, tuple)):
        return ''
    return '\n\n'.join(
        str(item.get('text') or '') for item in parts if isinstance(item, dict)
    )


def pack_text(pack):
    """把发布资料（标题/文案/话题）拼成待检文本。"""
    if not isinstance(pack, dict):
        return ''
    tags = pack.get('tags') or []
    return '\n'.join([
        str(pack.get('selected_title') or ''),
        str(pack.get('caption') or ''),
        ' '.join('#' + str(t).lstrip('#') for t in tags if str(t).strip()),
    ])


_SENT_END = '。！？!?；;\n'


def _sentence(text, start, end, max_len=600):
    """返回包含该命中的**完整句子**（按句号/叹号/问号/分号/换行切），不截成 90 字片段。
    超长句按 max_len 截断并标记，保证前端可显示且不撑爆版面。"""
    text = str(text or '')
    if not text:
        return {'sentence': '', 'sentence_truncated': False}
    left = 0
    for i in range(start - 1, -1, -1):
        if text[i] in _SENT_END:
            left = i + 1
            break
    right = len(text)
    for i in range(end, len(text)):
        if text[i] in _SENT_END:
            right = i + 1 if text[i] != '\n' else i
            break
    sentence = text[left:right].strip()
    truncated = False
    if len(sentence) > max_len:
        rel = max(0, start - left)
        a = max(0, rel - max_len // 2)
        sentence = ('…' if a > 0 else '') + sentence[a:a + max_len] + '…'
        truncated = True
    return {'sentence': sentence, 'sentence_truncated': truncated}


def _clip(text, start, end, max_len=90):
    left = 0
    for i in range(start - 1, -1, -1):
        if text[i] in _CLIP_CHARS:
            left = i + 1
            break
    right = len(text)
    for i in range(end, len(text)):
        if text[i] in _CLIP_CHARS:
            right = i
            break
    snippet = text[left:right].strip()
    if len(snippet) > max_len:
        rel_start = start - left
        rel_end = end - left
        a = max(0, rel_start - 28)
        b = min(len(snippet), rel_end + 28)
        snippet = ('…' if a > 0 else '') + snippet[a:b] + ('…' if b < len(snippet) else '')
    return snippet


def _is_quoted(text, start, end):
    i = start - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    j = end
    while j < len(text) and text[j].isspace():
        j += 1
    return i >= 0 and j < len(text) and text[i] in _OPEN_QUOTES and text[j] in _CLOSE_QUOTES


def _basis(entry):
    source = _lexicon()['sources'][entry['basis']]
    return {
        'id': entry['basis'],
        'title': source.get('title', ''),
        'issuer': source.get('issuer', ''),
        'url': source.get('url', ''),
        'published_at': source.get('published_at', ''),
        'retrieved_at': source.get('retrieved_at', ''),
        'quote': source.get('quote', ''),
        'detail': entry.get('basis_detail', ''),
    }


def _dedupe(hits):
    hits.sort(key=lambda h: (h['start'], -(h['end'] - h['start']), -_LEVEL_RANK[h['level']]))
    kept = []
    for hit in hits:
        if not kept or hit['start'] >= kept[-1]['end']:
            kept.append(hit)
            continue
        prev = kept[-1]
        same_span = hit['start'] == prev['start'] and hit['end'] == prev['end']
        if same_span or _LEVEL_RANK[hit['level']] > _LEVEL_RANK[prev['level']]:
            kept[-1] = hit
    return kept


def _recount(report):
    counts = {level: 0 for level in LEVELS}
    for hit in report['hits']:
        counts[hit['level']] += 1
    report['counts'] = counts
    report['ok'] = counts['block'] == 0
    report['block_count'] = counts['block']
    report['warn_count'] = counts['warn']
    report['hint_count'] = counts['hint']


def _rules_report(text):
    text = '' if text is None else str(text)
    if len(text) > MAX_TEXT:
        raise ValueError('待检文本超过 %d 字，请分段检查。' % MAX_TEXT)
    hits = []
    excluded = []
    for rule in _rules():
        entry = rule['entry']
        for match in rule['rx'].finditer(text):
            start, end = match.start(), match.end()
            term = match.group(0)
            after = text[end:end + 24]
            before = text[max(0, start - 12):start]
            if rule['ex_after'] and rule['ex_after'].match(after):
                excluded.append({
                    'entry_id': entry['id'], 'term': term, 'start': start,
                    'reason': entry.get('exclude_reason') or '按排除规则跳过。',
                    'snippet': _clip(text, start, end),
                })
                continue
            if rule['ex_before'] and rule['ex_before'].search(before):
                excluded.append({
                    'entry_id': entry['id'], 'term': term, 'start': start,
                    'reason': entry.get('exclude_reason') or '按前置排除规则跳过。',
                    'snippet': _clip(text, start, end),
                })
                continue
            level = entry['level']
            notes = [entry.get('reason', '')]
            if rule['dg_after'] and rule['dg_after'].match(after):
                level = 'hint'
                notes.append(entry.get('downgrade_note') or '按语境降级为提示。')
            if rule['dg_before'] and rule['dg_before'].search(before):
                level = 'hint'
                notes.append(entry.get('downgrade_note') or '前面有否定/限定词，按语境降级为提示。')
            if _is_quoted(text, start, end):
                level = 'hint'
                notes.append('引号内引用，平台机审仍可能命中；建议改成描述而不是直接念词。')
            hits.append({
                'id': '%s:%d' % (entry['id'], start),
                'entry_id': entry['id'],
                'term': term,
                'start': start,
                'end': end,
                'level': level,
                'category': entry.get('category', ''),
                'reason': ' '.join(x for x in notes if x),
                'basis': _basis(entry),
                'suggestions': list(entry['suggestions']),
                'snippet': _clip(text, start, end),
                **_sentence(text, start, end),
                'context': text[max(0, start - 20):min(len(text), end + 20)],
            })
    report = {
        'ok': True, 'mode': 'rules', 'text_length': len(text),
        'lexicon_version': (_lexicon().get('version', '')),
        'checked_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'hits': _dedupe(hits), 'excluded': excluded,
        'llm': {'used': False, 'status': 'not_requested'},
    }
    _recount(report)
    return report



def _llm_mark_false(hit):
    if hit and hit['level'] != 'hint':
        hit['level'] = 'hint'
        hit['llm_note'] = '模型复核：日常表达/引用/非广告语境，已降级，请人工确认。'


def _llm_add_missed(report, text, extra, index):
    if not isinstance(extra, dict):
        return
    quote = str(extra.get('quote') or '').strip()
    if not quote or quote not in text:
        return
    level = extra.get('level') if extra.get('level') in ('warn', 'hint') else 'warn'
    suggestions = [str(x).strip() for x in (extra.get('suggestions') or []) if str(x).strip()][:2]
    if not suggestions:
        suggestions = ['改为具体事实＋适用条件，并说明不能保证的部分。']
    start = text.find(quote)
    report['hits'].append({
        'id': 'llm:%d:%d' % (index, start),
        'entry_id': 'llm_semantic',
        'term': quote[:24],
        'start': start,
        'end': start + len(quote),
        'level': level,
        'category': str(extra.get('category') or '模型补充')[:40],
        'reason': str(extra.get('reason') or '模型语义复核发现，建议人工确认。')[:300],
        'basis': {
            'id': 'llm_review', 'title': 'LLM 语义复核（非词库）',
            'issuer': '', 'url': '', 'published_at': '', 'retrieved_at': '',
            'quote': '', 'detail': '模型补充项没有法条来源，只按语义风险提示，不能当作处罚依据。',
        },
        'suggestions': suggestions,
        'snippet': _clip(text, start, start + len(quote)),
        'context': text[max(0, start - 20):min(len(text), start + len(quote) + 20)],
    })


def _apply_llm(reports, texts):
    try:
        import providers
        if not providers.available().get('llm'):
            for report in reports:
                report['llm'] = {'used': False, 'status': 'unconfigured'}
            return
        total = sum(len(t) for t in texts)
        if total > MAX_LLM_CHARS:
            for report in reports:
                report['llm'] = {'used': False, 'status': 'skipped_size',
                                 'note': '文本合计超过 %d 字，跳过模型复核，规则结果仍可用。' % MAX_LLM_CHARS}
            return
        payload = {
            'items': [
                {
                    'index': index,
                    'text': text,
                    'rule_hits': [
                        {'id': '%d|%s' % (index, h['id']), 'term': h['term'],
                         'level': h['level'], 'snippet': h['snippet']}
                        for h in report['hits']
                    ],
                }
                for index, (text, report) in enumerate(zip(texts, reports))
            ]
        }
        result = providers.llm_json(prompt_store.get('compliance.review'), payload)
        global_lookup = {}
        raw_lookup = {}
        for index, report in enumerate(reports):
            for hit in report['hits']:
                global_lookup['%d|%s' % (index, hit['id'])] = hit
                raw_lookup.setdefault(hit['id'], hit)
        if isinstance(result, dict) and isinstance(result.get('items'), list):
            items = result['items']
        elif isinstance(result, list):
            items = result
        elif len(reports) == 1 and isinstance(result, dict):
            items = [dict(result, index=0)]
        else:
            items = None
        if items is not None:
            by_index = {}
            for position, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                index = item.get('index')
                by_index[index if isinstance(index, int) else position] = item
            for index, (text, report) in enumerate(zip(texts, reports)):
                item = by_index.get(index) or {}
                hit_lookup = {h['id']: h for h in report['hits']}
                false_ids = item.get('false_positive_ids') or []
                if isinstance(false_ids, list):
                    for hit_id in false_ids:
                        hit = hit_lookup.get(str(hit_id)) or global_lookup.get(str(hit_id))
                        _llm_mark_false(hit)
                for extra in (item.get('missed') or [])[:20]:
                    _llm_add_missed(report, text, extra, index)
        else:
            if not isinstance(result, dict):
                raise ValueError('模型返回格式无法识别。')
            # 批量请求时模型可能只返回一个总的 false_positive_ids / missed，
            # id 已带批次前缀，按前缀回填；补充项按原文定位到第一条包含它的文本。
            for hit_id in result.get('false_positive_ids') or []:
                _llm_mark_false(global_lookup.get(str(hit_id)) or raw_lookup.get(str(hit_id)))
            for extra in (result.get('missed') or [])[:20]:
                if not isinstance(extra, dict):
                    continue
                quote = str(extra.get('quote') or '').strip()
                owner = next((i for i, text in enumerate(texts) if quote and quote in text), None)
                if owner is not None:
                    _llm_add_missed(reports[owner], texts[owner], extra, owner)
        for report in reports:
            report['hits'] = _dedupe(report['hits'])
            report['mode'] = 'rules+llm'
            report['llm'] = {
                'used': True, 'status': 'ok',
                'model': providers.available().get('llm_model', ''),
            }
            _recount(report)
    except Exception as exc:
        for report in reports:
            report['llm'] = {'used': False, 'status': 'failed', 'error': str(exc)[:300]}
            report['mode'] = 'rules'


def check_many(texts, use_llm=False):
    texts = ['' if text is None else str(text) for text in texts]
    reports = [_rules_report(text) for text in texts]
    if use_llm:
        _apply_llm(reports, texts)
    return reports


def check(text, use_llm=False):
    return check_many([text], use_llm=use_llm)[0]


def apply_waivers(report, waivers):
    """2026-11-10（试用反馈 1C）：本人逐项确认误判后，仅本次放行并留记录。

    必删（block）默认硬拦；只有被逐条点名、带理由的命中才标 waived。
    记录会随定稿存进 script_version，导出时复用，不改变规则库本身。
    """
    by_id = {}
    for item in (waivers or []):
        if isinstance(item, dict) and item.get('id'):
            by_id[str(item['id'])] = str(item.get('reason') or '').strip()[:200] or '本人逐项确认（未填理由）'
    applied = []
    for hit in report.get('hits', []):
        if hit['level'] == 'block' and str(hit['id']) in by_id:
            hit['waived'] = True
            applied.append({'id': hit['id'], 'term': hit['term'], 'snippet': hit['snippet'],
                            'reason': by_id[str(hit['id'])],
                            'waived_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    blocks = [h for h in report.get('hits', []) if h['level'] == 'block' and not h.get('waived')]
    report['waivers'] = applied
    report['waived_count'] = len(applied)
    report['block_count'] = len(blocks)
    report['ok'] = not blocks
    return report


def block_message(report, limit=3):
    blocks = [h for h in report.get('hits', []) if h['level'] == 'block' and not h.get('waived')]
    if not blocks:
        return ''
    shown = '；'.join('%s→%s' % (h['term'], h['suggestions'][0]) for h in blocks[:limit])
    more = '，另有%d处' % (len(blocks) - limit) if len(blocks) > limit else ''
    return '还有%d处必删表达：%s%s。' % (len(blocks), shown, more)
