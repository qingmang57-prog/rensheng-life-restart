# -*- coding: utf-8 -*-
"""口播稿硬约束检查：只报警，不自动改稿。纯标准库，0 外部依赖。

规则在 rules_zh.json（来源/许可/是否已改造/匹配方式/严重度都在配置里）。
三条自我保护：
1) 宁可漏报不误伤：句式类规则都带“需上下文/人工判”标记。
2) 命中必须能解释为什么算问题，不给抽象评分。
3) 可整体关闭（enabled=false），也可对本人基准稿整句白名单（whitelist）。
"""
import json
import re
import time
from pathlib import Path

import style_profile

RULES_FILE = Path(__file__).resolve().parent / 'rules_zh.json'
_cache = {'mtime': None, 'data': {}}


def config():
    try:
        stamp = RULES_FILE.stat().st_mtime
    except OSError:
        return _cache['data']
    if _cache['mtime'] != stamp:
        try:
            _cache.update({'mtime': stamp,
                           'data': json.loads(RULES_FILE.read_text(encoding='utf-8'))})
        except (OSError, ValueError):
            pass
    return _cache['data']


def _normalize(sentence):
    return re.sub(r'[^\w\u4e00-\u9fff]+', '', sentence or '')


def _hit(rule, sentence, index, offset, manual=False, severity=None):
    return {'rule_id': rule.get('id'), 'rule': rule.get('name'),
            'severity': severity or rule.get('severity', 'info'), 'sentence': sentence[:200],
            'sentence_index': index, 'offset': offset,
            'why': rule.get('why', ''), 'suggestion': rule.get('suggestion', ''),
            'source': rule.get('source', ''), 'license': rule.get('license', ''),
            'adapted': rule.get('adapted', False),
            'manual_judgement': manual or rule.get('needs_context', False),
            'auto_rewrite': bool(rule.get('auto_rewrite', False))}


def _match(rule, sentence):
    mode = rule.get('match') or 'phrase'
    if mode == 'phrase':
        return [(w, None) for w in rule.get('words') or [] if w and w in sentence]
    if mode == 'regex':
        return [(sentence, None)] if re.search(rule.get('pattern') or '', sentence) else []
    if mode == 'long':
        chars = style_profile.char_len(sentence)
        hard = int(rule.get('hard_chars') or 45)
        comma = sentence.count('，') + sentence.count(',')
        if chars >= hard or comma >= 4:
            return [(sentence, 'error')]
        if chars >= int(rule.get('min_chars') or 30):
            return [(sentence, rule.get('severity') or 'warning')]
        return []
    return []


def _density_hits(rule, clean):
    """全文词频密度：同一水词出现 >= min_count 次才报 info，不做合格判定。"""
    hits = []
    for word in rule.get('words') or []:
        n = clean.count(word) if word else 0
        if n >= int(rule.get('min_count') or 3):
            hits.append(_hit(rule, '全文出现 %d 次「%s」' % (n, word), -1, -1))
    return hits


def check(text):
    """返回命中清单：位置（句序/字符偏移）+ 原句 + 为什么 + 建议怎么改。"""
    started = time.perf_counter()
    data = config()
    raw = str(text or '')
    result = {'enabled': data.get('enabled', True), 'rules_version': data.get('version', ''),
              'hits': [], 'count': 0, 'auto_rewrite': False,
              'note': '只做提示，不改稿；误报请按“人工判”那几条自行忽略。'}
    if not result['enabled']:
        return result
    clean = style_profile.clean_text(raw)
    sentences = style_profile.split_sentences(clean)
    whitelist = {_normalize(x) for x in (data.get('whitelist') or []) if x}
    for rule in data.get('rules') or []:
        if (rule.get('match') or '') == 'density':
            result['hits'].extend(_density_hits(rule, clean))
    offset = 0
    for index, sentence in enumerate(sentences):
        if _normalize(sentence) in whitelist:
            offset += len(sentence)
            continue
        for rule in data.get('rules') or []:
            if (rule.get('match') or '') == 'density':
                continue
            for token, severity in _match(rule, sentence):
                result['hits'].append(_hit(rule, sentence, index, offset, severity=severity))
        offset += len(sentence)
    result['count'] = len(result['hits'])
    result['chars'] = style_profile.char_len(raw)
    result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
    return result
