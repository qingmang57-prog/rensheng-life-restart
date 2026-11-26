# -*- coding: utf-8 -*-
"""念稿版、气口字幕、封面标题套路检查（不做 TTS，用户自己念）。

- 念稿版：标短停｜、换气‖、重音▲、抬头收句■，并写清每处是哪条规则判出来的；
- 气口字幕：单条 ≤14 字，按语义单元断，不按标点机械切；时间轴按 4.2 字/秒估算，给出微调位置；
- 标题检查：命中套路词就给出换法，不替用户改。
"""
from __future__ import annotations

import re

from db import now

MAX_CHARS = 14
MIN_CHARS = 4
CHARS_PER_SEC = 4.2
MIN_DURATION_MS = 1200
GAP_MS = 120
PAUSE = '｜'
BREATH = '‖'
STRESS = '▲'
LOOK = '■'
STRESS_WORDS = ('不是', '而是', '反而', '结果', '后来', '但是', '所以', '其实', '只')
SENT_RE = re.compile(r'[^。！？!?；;\n]+[。！？!?；;]?')
NUMBER_RE = re.compile(r'[0-9]+[万千百十个]?|[一二三四五六七八九十百千万]+万')
TRAILING = '。，；：、 '
CLICHES = (
    ('3个方法', '换成“我试过的 3 步”，后面直接给步骤'),
    ('三个方法', '换成“我试过的 3 步”，后面直接给步骤'),
    ('你必须知道', '换成具体后果：“不知道这条，我那天多亏了三千”'),
    ('看完就懂', '换成具体场景：“看完你就知道我那半年的账错在哪”'),
    ('99%的人不知道', '换成你自己的观察：“我见过的老板，十个有九个算错这笔账”'),
    ('99%的人', '换成你自己见过的具体人数或场景'),
    ('干货', '删掉“干货”二字，直接说里面有什么'),
    ('保姆级', '换成“照着做就行”，后面给可照抄的清单'),
    ('秒懂', '删掉，用具体例子替代'),
    ('建议收藏', '删掉，改成“下次遇到时翻出来对着算”'),
)


def _clean_marks(text):
    return str(text or '').replace(PAUSE, '').replace(BREATH, '').replace(STRESS, '').replace(LOOK, '')


def _sentences(text):
    return [m.group(0).strip() for m in SENT_RE.finditer(_clean_marks(text)) if m.group(0).strip()]


def _char_len(text):
    return len(re.sub(r'\s', '', text))


def _stress_word(sentence):
    numbers = NUMBER_RE.findall(sentence)
    if numbers:
        return numbers[0], '数字是这条的钩子，念的时候要让人听清'
    for word in STRESS_WORDS:
        if word in sentence:
            return word, '转折/对比处用力，观众才知道重点变了'
    return '', ''


def build_read(version):
    """返回 (markdown, marks)；marks 记录每处记号是哪条规则判出来的。"""
    parts = version.get('parts') or []
    mark_lines = []
    body = ['# 念稿版｜' + str(version.get('title') or ''),
            '',
            '> 记号：' + PAUSE + ' 短停（约 0.3 秒，看一眼下一句）　' + BREATH + ' 换气（约 0.8 秒，抬头吸气）　'
            + STRESS + ' 重音（这句最该用力的词）　' + LOOK + ' 收句（抬头看镜头，停半秒）',
            '> 不做 TTS，这一版是给你自己念的；括号里的秒数是建议，不是硬性节拍。',
            '']
    for part_index, part in enumerate(parts):
        body.append('## ' + str(part.get('label') or part.get('role') or ''))
        body.append('')
        sentences = _sentences(part.get('text'))
        rendered = []
        for index, sentence in enumerate(sentences):
            is_last = index == len(sentences) - 1
            word, why = _stress_word(sentence)
            marked = sentence
            if word and word in marked:
                marked = marked.replace(word, STRESS + word, 1)
                mark_lines.append({'part': part.get('label'), 'sentence': index + 1,
                                   'mark': STRESS + word, 'reason': why})
            if _char_len(sentence) >= 26 and '，' in sentence and index < len(sentences) - 1:
                marked = marked.replace('，', '，' + PAUSE, 1)
                mark_lines.append({'part': part.get('label'), 'sentence': index + 1,
                                   'mark': PAUSE, 'reason': '长句一口气念不完，在第一个逗号处短停换气'})
            if is_last:
                marked = marked.rstrip() + LOOK
                mark_lines.append({'part': part.get('label'), 'sentence': index + 1,
                                   'mark': LOOK, 'reason': '这段最后一句抬头收，给观众半秒反应'})
            rendered.append(marked)
        body.append('\n\n'.join(rendered))
        body.append('')
        if part_index < len(parts) - 1:
            body.append(BREATH)
            body.append('')
            mark_lines.append({'part': part.get('label'), 'sentence': len(sentences),
                               'mark': BREATH, 'reason': '段落之间换气，别把上一段的劲带到下一段'})
    body.append('## 判读说明（哪几处是判的，尺度是什么）')
    body.append('')
    for item in mark_lines:
        body.append('- %s 第 %d 句：%s —— %s' % (item['part'], item['sentence'], item['mark'], item['reason']))
    body.append('')
    body.append('- 尺度：短停只标“长句≥26 字且还有下一句”的第一个逗号；换气只放段与段之间；'
                '重音每句最多一个，只挑数字或转折词；收句每段只标最后一句。')
    return '\n'.join(body), mark_lines


def _clauses(sentence):
    out, buf = [], ''
    for char in sentence:
        buf += char
        if char in '，；：':
            out.append(buf)
            buf = ''
    if buf:
        out.append(buf)
    return out


QUOTES = '\'"“”‘’「」『』'


def _break_candidates(clause):
    """列出可断点：引号外/内边界优先，其次标点后，最后虚词后；引号内部不给断点。"""
    out, depth = [], 0
    for index, char in enumerate(clause):
        if char in QUOTES:
            depth = 0 if depth else 1
            out.append(('quote', index if depth else index + 1))
            continue
        if depth:
            continue
        if char in '，；：—－':
            out.append(('punct', index + 1))
        elif char in '的了在把让给和与而但就才':
            out.append(('word', index + 1))
    return out


def _hard_split(clause):
    """只在语义边界断：不断在引号里、不只留两三个字，宁可一行长一点。"""
    if _char_len(clause) <= MAX_CHARS + 4:
        return [clause]
    half = _char_len(clause) / 2.0
    best = None
    for kind, index in _break_candidates(clause):
        left = clause[:index].rstrip(TRAILING)
        right = clause[index:].strip(TRAILING)
        if _char_len(left) < MIN_CHARS or _char_len(right) < MIN_CHARS + 1:
            continue
        if right[0] in '的了' and kind != 'quote':
            continue
        score = (0 if kind == 'quote' else 1, abs(_char_len(left) - half))
        if best is None or score < best[0]:
            best = (score, left, right)
    if not best:
        return _fallback_split(clause)
    parts = [best[1], best[2]]
    tail = parts.pop()
    return parts + _hard_split(tail)


FUNC_TAIL = '对把让给和与就才都也还又再为用拿从向做去来是在从'
SORT_TAIL = '的了于'
FUNC_START = '的了是在就才都也还又再为用拿从向对把让给和与'


def _fallback_split(clause):
    """整句没有自然停顿时的兜底：在 4–16 字之间找最像词界的位置，尽量不切在词中间。"""
    size = _char_len(clause)
    limit = min(size - MIN_CHARS - 1, MAX_CHARS + 2)
    half = size / 2.0
    best = None
    for index in range(MIN_CHARS, limit + 1):
        if index <= 0 or index >= size:
            continue
        if clause[index - 1] not in FUNC_TAIL + SORT_TAIL:
            continue
        left = clause[:index].rstrip(TRAILING)
        right = clause[index:].strip(TRAILING)
        if _char_len(left) < MIN_CHARS or _char_len(right) < MIN_CHARS:
            continue
        score = abs(_char_len(left) - half) + (8 if right[0] in FUNC_START else 0)
        if best is None or score < best[0]:
            best = (score, left, right)
    if not best:
        pick = min(MAX_CHARS, size - MIN_CHARS - 1)
        if pick <= 0:
            return [clause]
        return [clause[:pick].rstrip(TRAILING), clause[pick:].strip(TRAILING)]
    parts = [best[1], best[2]]
    tail = parts.pop()
    return parts + _hard_split(tail)


def build_captions(version):
    """按语义单元断字幕：单条 ≤14 字，长短错落；不按标点机械切。"""
    lines = []
    for part_index, part in enumerate(version.get('parts') or []):
        def add(text, part_index=part_index):
            if text:
                lines.append({'text': text, 'part': part_index})

        for sentence in _sentences(part.get('text')):
            sentence = sentence.rstrip(TRAILING)
            if not sentence:
                continue
            if _char_len(sentence) <= MAX_CHARS:
                add(sentence)
                continue
            current = ''
            for clause in _clauses(sentence):
                clause = clause.rstrip(TRAILING)
                if not clause:
                    continue
                if _char_len(current) + _char_len(clause) <= MAX_CHARS:
                    current += clause
                elif _char_len(clause) > MAX_CHARS:
                    if current:
                        add(current)
                        current = ''
                    for piece in _hard_split(clause):
                        add(piece)
                else:
                    if current:
                        add(current)
                    current = clause
            if current:
                add(current)
    fixed = []
    for line in lines:
        if _char_len(line['text']) < MIN_CHARS and fixed and fixed[-1]['part'] == line['part']:
            fixed[-1]['text'] += '，' + line['text']
        else:
            fixed.append(dict(line))
    cues, clock = [], 0
    for item in fixed:
        line = item['text']
        duration = max(MIN_DURATION_MS, int(_char_len(line) / CHARS_PER_SEC * 1000))
        cues.append({'text': line, 'start_ms': clock, 'end_ms': clock + duration})
        clock += duration + GAP_MS
    return cues


def srt(cues):
    def stamp(ms):
        ms = int(round(ms))
        return '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
    return '\n\n'.join('%d\n%s --> %s\n%s' % (index + 1, stamp(cue['start_ms']), stamp(cue['end_ms']), cue['text'])
                       for index, cue in enumerate(cues))


def alignment_note(cues):
    hints = []
    if cues:
        hints.append('第 1 条（%s）：开场第一条，按实际开口时间挪 0.2–0.5 秒' % cues[0]['text'][:8])
    for index, cue in enumerate(cues):
        text = cue['text']
        if index and (NUMBER_RE.search(text) or text[:2] in ('但是', '后来', '所以', '结果', '反而')):
            hints.append('第 %d 条（%s）：数字/转折处，可多留 0.2 秒再切' % (index + 1, text[:8]))
    if len(cues) > 1:
        hints.append('第 %d 条（%s）：结尾收句，别抢拍，停满半秒' % (len(cues), cues[-1]['text'][:8]))
    return {
        'estimated': True,
        'rule': '按 %.1f 字/秒估算：起点=上一条结束+0.12 秒；每条时长=max(1.2 秒, 字数/%.1f)。'
                '这是估算时间轴，录完真声后按实际口播对齐，字幕行序不变。' % (CHARS_PER_SEC, CHARS_PER_SEC),
        'manual_adjust': hints[:8],
        'line_rule': '单条 ≤约 %d 字，按语义单元断，不按标点机械切；短句和长句交替，避免整片都一样长。' % MAX_CHARS,
    }


def check_copy(pack):
    """封面标题/标题候选的套路词检查；命中给换法，不自动改。"""
    fields = []

    def inspect(label, text):
        hits = []
        text = str(text or '')
        for word, suggestion in CLICHES:
            if word in text:
                hits.append({'word': word, 'suggestion': suggestion})
        return {'field': label, 'text': text, 'hits': hits} if text else None

    main = inspect('封面标题', pack.get('selected_title'))
    if main:
        fields.append(main)
    for text in (pack.get('titles') or [])[:6]:
        if str(text or '') == str(pack.get('selected_title') or ''):
            continue
        item = inspect('标题候选', text)
        if item and any(hit for hit in item['hits']):
            fields.append(item)
    total = sum(len(item['hits']) for item in fields)
    return {'ok': total == 0, 'checked_at': now(), 'hit_count': total, 'fields': fields,
            'note': '命中只提示换法，不自动改标题；模板去套路建议见 rules_zh.json 的 copy_tips。'}
