# -*- coding: utf-8 -*-
"""生成后自动洗稿：只做安全改法，改坏了就放弃，原稿不动。

自动改只做四类白名单动作（不做整句重写、不删观点、不改立场和数字、不碰口头禅、不搓排比）：
- opener-cliche：句首起跑铺垫（“你有没有想过，”这类）连逗号一起删；
- split-long-sentence：超长句在转折连词前的逗号处断一刀（每句最多一刀）；
- nominal-verb-object：进行+白名单动词 → 去掉“进行”；
- slogan-ending：段落最后一句是“愿你/共勉”式金句，整句删。

只提示不自动改：directive-audience（给观众立规矩）、coined-jargon（生造词）。

硬保护（任一触发即整体放弃、保留原稿并写原因）：
字数变化超 ±15%；原本有 AI 腔命中但洗后没下降；语法残缺（连续标点/空段/半句）；数字被改掉。
检测器（style_lint）只当守门员，不参与“该改哪一句”的决策。
"""
from __future__ import annotations

import re

import style_lint
import style_profile

MAX_DELTA = 0.15
LONG_SENTENCE = 40
MIN_HALF = 9
SPLIT_CONJ = ('但是', '但', '所以', '因为', '结果', '后来', '其实', '而且',
              '于是', '可是', '不过', '然后', '反而', '反倒')
OPENER_WORDS = ('你有没有想过', '很多人不知道', '不知道你有没有发现', '这就有意思了')
NOMINAL_VERBS = ('讨论', '分析', '检查', '测试', '调整', '复盘', '整理', '沟通',
                 '对比', '说明', '核对', '确认', '评估')
SLOGAN_RE = re.compile(r'愿你|愿你我|共勉|与君共勉')
_SENT_RE = re.compile(r'[^。！？!?；;\n]+[。！？!?；;]?')
_BAD_PUNCT_RE = re.compile(r'[，。！？；、,.!?;]{2,}')
_ALLOWED_REPEAT = {'……', '...', '！！', '？？', '——'}
_DIGIT_RE = re.compile(r'\d+(?:\.\d+)?')
_SPLIT_RE = re.compile('，(?=(?:' + '|'.join(SPLIT_CONJ) + '))')
_DANGLING = ('因为', '所以', '但是', '而且', '如果', '虽然', '然后', '于是', '不过',
             '可是', '并且', '或者', '以及', '而', '但', '和', '与', '或', '的',
             '了', '是', '在', '把', '被', '让', '给', '从', '到', '着')


def enabled():
    data = style_lint.config()
    block = data.get('auto_rewrite') or {}
    return bool(block.get('enabled', True))


def _split_sentences(block):
    return [m.group(0) for m in _SENT_RE.finditer(block or '')]


def _strip_opener(sentence):
    for word in OPENER_WORDS:
        if sentence.startswith(word):
            rest = sentence[len(word):]
            if rest[:1] in ('，', '、'):
                rest = rest[1:].lstrip('，、')
                if style_profile.char_len(rest) >= 4:
                    return rest, word
    return None, ''


def _wash_sentence(sentence, is_last):
    """返回 (新句, 改动清单)。改动清单里每条都是原句→改后。"""
    changes = []
    current = sentence
    stripped, word = _strip_opener(current)
    if stripped is not None:
        changes.append({'rule_id': 'opener-cliche', 'rule': '起跑式铺垫',
                        'before': current, 'after': stripped})
        current = stripped
    if is_last and SLOGAN_RE.search(current) and len(_split_sentences(current)) == 1:
        changes.append({'rule_id': 'slogan-ending', 'rule': '金句升华收尾',
                        'before': current, 'after': ''})
        return '', changes
    fixed = re.sub('进行(' + '|'.join(NOMINAL_VERBS) + ')', r'\1', current)
    if fixed != current:
        changes.append({'rule_id': 'nominal-verb-object', 'rule': '名词化空动词',
                        'before': current, 'after': fixed})
        current = fixed
    if style_profile.char_len(current) >= LONG_SENTENCE:
        for match in _SPLIT_RE.finditer(current):
            head, tail = current[:match.start()], current[match.start() + 1:]
            if style_profile.char_len(head) >= MIN_HALF and style_profile.char_len(tail) >= MIN_HALF:
                new = head + '。' + tail
                changes.append({'rule_id': 'split-long-sentence', 'rule': '拆长句',
                                'before': current, 'after': new})
                current = new
                break
    return current, changes


def _wash_part(part):
    raw = str(part.get('text') or '')
    blocks = raw.split('\n')
    changes = []
    out_blocks = []
    offset = 0
    for block in blocks:
        block_offset = offset
        offset += len(block) + 1
        sentences = _split_sentences(block)
        if not sentences:
            out_blocks.append(block)
            continue
        cursor = 0
        pieces = []
        for index, sentence in enumerate(sentences):
            pos = block.find(sentence, cursor)
            if pos < 0:
                pos = cursor
            cursor = pos + len(sentence)
            new_sentence, edits = _wash_sentence(sentence, index == len(sentences) - 1)
            for edit in edits:
                changes.append({**edit, 'role': part.get('role'), 'label': part.get('label'),
                                'position': block_offset + pos})
            pieces.append(new_sentence)
        out_blocks.append(''.join(pieces))
    return '\n'.join(out_blocks), changes


def _syntax_problem(before, after):
    if not after.strip():
        return '整篇被删空'
    before_blocks = before.split('\n')
    after_blocks = after.split('\n')
    if len(after_blocks) < len(before_blocks):
        return '段落被删掉'
    for old_block, new_block in zip(before_blocks, after_blocks):
        if old_block.strip() and not new_block.strip():
            return '出现空段'
        if new_block.rstrip().endswith('，') or new_block.rstrip().endswith(','):
            return '句子没说完就断了'
    for match in _BAD_PUNCT_RE.finditer(after):
        if match.group(0) in _ALLOWED_REPEAT:
            continue
        return '出现连续标点“%s”' % match.group(0)
    for block in after_blocks:
        for sentence in _split_sentences(block):
            core = sentence.rstrip('。！？!?；;')
            if not core:
                continue
            if style_profile.char_len(core) < 2:
                return '出现半句：“%s”' % sentence
            if core.endswith(_DANGLING):
                return '出现半句：“%s”' % sentence
    return ''


def _join(parts):
    return '\n\n'.join(str(x.get('text') or '') for x in parts)


def summary(report):
    """给页面用的小摘要；明细另走详情接口。"""
    return {'id': report.get('id') or '', 'outcome': report.get('outcome', ''),
            'change_count': len(report.get('changes') or []),
            'chars_before': report.get('chars_before', 0),
            'chars_after': report.get('chars_after', 0),
            'delta_pct': report.get('delta_pct', 0),
            'hits_before': report.get('hits_before', 0),
            'hits_after': report.get('hits_after', 0),
            'reason': report.get('reason', ''),
            'restored_at': report.get('restored_at', ''),
            'created_at': report.get('created_at', '')}


def wash_parts(parts):
    """输入三段结构，返回洗后结构 + 完整报告；判定放弃时 parts_after 就是原稿。"""
    before_parts = [{'role': x.get('role'), 'label': x.get('label'),
                     'text': str(x.get('text') or '')} for x in parts]
    result_parts = []
    changes = []
    for part in parts:
        new_text, edits = _wash_part(part)
        result_parts.append({**{k: part.get(k) for k in ('role', 'label')}, 'text': new_text})
        changes.extend(edits)
    before_text = _join(before_parts)
    after_text = _join(result_parts)
    before_chars = style_profile.char_len(before_text)
    after_chars = style_profile.char_len(after_text)
    delta = (after_chars - before_chars) / before_chars if before_chars else 0.0
    hits_before = style_lint.check(before_text).get('count', 0) if enabled() else 0
    hits_after = style_lint.check(after_text).get('count', 0) if enabled() else 0
    report = {'parts_before': before_parts,
              'chars_before': before_chars, 'chars_after': after_chars,
              'delta_pct': round(delta * 100, 1),
              'hits_before': hits_before, 'hits_after': hits_after,
              'changes': changes}
    if not enabled():
        report.update({'outcome': 'noop', 'parts_after': before_parts,
                       'reason': 'rules_zh.json 里自动洗稿已关闭（auto_rewrite.enabled=false）。'})
        return report
    if not changes:
        report.update({'outcome': 'noop', 'parts_after': before_parts,
                       'reason': '没有命中可安全修改的地方，原稿不动。'})
        return report
    problem = ''
    if abs(delta) > MAX_DELTA:
        problem = '字数变化 %.1f%%，超过 ±%d%% 的保护线' % (delta * 100, round(MAX_DELTA * 100))
    elif hits_before > 0 and hits_after >= hits_before:
        problem = 'AI 腔命中数没有下降（%d → %d）' % (hits_before, hits_after)
    elif _syntax_problem(before_text, after_text):
        problem = _syntax_problem(before_text, after_text)
    elif set(_DIGIT_RE.findall(before_text)) != set(_DIGIT_RE.findall(after_text)):
        problem = '数字被改掉了（保护立场与数据）'
    if problem:
        report.update({'outcome': 'abandoned', 'parts_after': before_parts,
                       'reason': problem + '，这次没敢改，原稿保持不变。'})
        return report
    report.update({'outcome': 'applied', 'parts_after': result_parts,
                   'reason': '只做了删铺垫、拆长句、去空动词、删金句；观点、数字、口头禅没动。'})
    return report
