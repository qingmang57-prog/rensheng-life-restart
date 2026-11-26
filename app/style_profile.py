"""账号风格学习（独立模块）。

输入：records 表里已采集的真实作品（kind=bench_work，文案在 title 字段）。
输出：一张 style_card，可写回 profile 的 style_card 字段，供写稿时使用。

两条路径：
- 统计路径：分词/频次/句长/句式模板，不依赖 LLM，断网也能用。
- LLM 路径：只接受能在原文逐字回查的引用；任何编造引用都会被丢弃。
  模型不可用或调用失败时自动退回统计路径，不向调用方抛错。

硬门槛：可用文案作品少于 5 条时拒绝生成，返回中文说明。

本模块只新增文件，不改 app.py / app.js / creation_service.py。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from datetime import datetime, timezone

import prompt_store
import providers

MIN_WORKS = 5
KIND_BENCH_WORK = 'bench_work'
KIND_PLATFORM_WORK = 'platform_work'   # 本人已发布作品（t80 风格卡新语料源）
KIND_TRANSCRIPT = 'transcript'         # 真实口播转写（本人逐字稿）
KIND_ASSET = 'asset'
KIND_PROJECT = 'project'
KIND_BENCH_ACCOUNT = 'bench_account'
PROFILE_KIND = 'profile'
PROFILE_KEY = 'owner'
SCHEMA = 'style_card.v1'

# 句长分档：中文口播里 15 字以内算短句，30 字以上算长句。
SHORT_MAX = 15
LONG_MIN = 30
# 节奏判定：至少三成作品出现该特征，才认为账号“爱用”。
RHYTHM_THRESHOLD = 0.3

_TAG = re.compile(r'#[^\s#，。！？；、,.!?;:：]+')
_URL = re.compile(r'https?://[^\s]+')
_SENTENCE_RE = re.compile(r'[^。！？!?；;\n]+[。！？!?；;]?')
_CLAUSE_SEP = re.compile(r'[，,、：:。！？!?；;]+')
_CHAR_RE = re.compile(r'[\u4e00-\u9fffA-Za-z0-9]')
_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')

PRONOUNS = (
    ('我', re.compile(r'我(?!们)')),
    ('我们', re.compile(r'我们')),
    ('你', re.compile(r'你(?!们)')),
    ('你们', re.compile(r'你们')),
    ('咱', re.compile(r'咱(?!们)')),
    ('咱们', re.compile(r'咱们')),
    ('自己', re.compile(r'自己')),
    ('大家', re.compile(r'大家')),
)

CATCH_PHRASES = (
    '我告诉你', '我跟你说', '说实话', '说真的', '讲真', '有一说一', '说白了',
    '其实', '真的', '就是', '然后', '对吧', '是吧', '你知道吗', '你会发现',
    '你要知道', '记住', '一定要', '千万', '不要', '别', '咱们', '我们',
    '我觉得', '可能', '反正', '总之', '所以说', '换句话说',
)

# 2026-11-17 t80（人味研究§⑤）：嗯/啊/其实/真的/就是/然后 这类是**本人人味词**，
# 不再列进「避免水词」；真正该清的是模糊指代与套路填充。人味词单独成表，提示词里说明可保留。
HUMAN_FLAVOR_WORDS = {
    '嗯': '停顿/思考音，真人说话的节奏锚点，可保留',
    '啊': '语气词，真人感强，可保留（不要连续堆）',
    '诶': '招呼/转折语气，口语特征',
    '哎': '叹气/招呼，情绪真实',
    '其实': '自我修正的转折，本人常用就保留',
    '真的': '强调程度，本人口头强调可保留',
    '就是': '口语连接词，本人常用可保留（一段别超过两次）',
    '然后': '叙事连接词，讲过程时可保留',
    '反正': '收束态度词，本人性格的一部分',
    '可能': '留余地的判断，不一定算没底气',
    '我觉得': '本人观点前缀，可保留',
    '对吧': '求认同的口头禅，真人互动感',
    '是吧': '求认同的口头禅，真人互动感',
    '吧': '语气词，口语自然度来源',
    '呢': '语气词，口语自然度来源',
    '咱': '亲近称呼，本人习惯可保留',
    '咱们': '亲近称呼，本人习惯可保留',
}

WATER_WORDS = {
    '这个': '指代词，指代不清时观众会跟丢（指代清楚可保留）',
    '那个': '指代词，口语残留，成稿应明确对象',
    '就是说': '解释性填充，删掉不损失信息',
    '怎么说呢': '口头填充，拖慢节奏',
    '你知道吗': '口头互动套话，短视频开头慎用',
    '总之': '总结套话，信息密度低',
    '所以说': '解释性收束套话，可删',
    '换句话说': '重复解释套话，可删',
}

RHETORICAL_MARKERS = (
    '难道', '凭什么', '为什么', '是不是', '对不对', '有没有', '能不能',
    '要不要', '怎么会', '不是吗', '对吧', '是吧', '谁还', '谁不',
)

ENUM_PATTERNS = (
    r'第[一二三四五六七八九十]+',
    r'[一二三四五六七八九十]是',
    r'首先', r'其次', r'最后',
    r'第一步', r'第二步', r'第三步',
    r'\d+[.、]',
)
THREE_PART_GROUPS = (
    (r'第一', r'第二', r'第三'),
    (r'一是', r'二是', r'三是'),
    (r'首先', r'其次', r'最后'),
    (r'第一步', r'第二步', r'第三步'),
    (r'\d+[.、]',),
)

STOPWORDS = {
    '的', '了', '是', '在', '我', '你', '他', '她', '它', '我们', '你们', '他们',
    '这', '那', '这个', '那个', '这些', '那些', '一个', '一种', '一些', '什么',
    '怎么', '怎样', '如何', '为什么', '因为', '所以', '但是', '可是', '而且',
    '然后', '如果', '虽然', '还是', '就是', '只是', '不是', '没有', '可以',
    '可能', '应该', '需要', '觉得', '感觉', '知道', '看到', '听到', '自己',
    '大家', '时候', '东西', '事情', '问题', '方法', '方式', '内容', '真的',
    '其实', '起来', '出来', '下来', '上去', '过去', '回来', '进去', '出去',
    '一样', '这种', '那种', '这么', '那么', '多少', '非常', '特别', '比较',
    '已经', '还有', '或者', '不过', '然后呢', '就是说', '怎么说', '对不对',
    '是不是', '有没有', '能不能', '要不要', '为什么', '什么样', '怎么办',
    '每一', '一点', '一下', '一起', '一次', '一天', '一年', '一直', '一切',
    '一定', '一边', '一半', '所有', '整个', '各种', '每个', '每天', '每年',
}
BAD_START = set('的了是在就也都得很太更最又再才还却并而把被让给对从到和与或着过得地么怎什哪谁吗呢吧啊呀哦嗯哈嘛啦呗')
BAD_END = BAD_START | set('做说想要会能去来看听走用有当为一')

OPENER_TYPES = (
    ('提问式', re.compile(r'[？?]$|^(为什么|怎么|怎样|如何|是不是|要不要|能不能|有没有)|(吗|呢)$')),
    ('分点式', re.compile(r'^(第[一二三四五六七八九十]+|[一二三四五六七八九十]是|[0-9]+[.、])')),
    ('宣告式', re.compile(r'^(我告诉你|我跟你说|说实话|说真的|我想说|今天|给你|记住|千万别)')),
    ('场景式', re.compile(r'^(当|每当|如果|一个人|曾经|那年|那天|小时候|最近|昨天|今天)')),
    ('数字式', re.compile(r'^[\d一二三四五六七八九十百千万]+[岁年天个万块]')),
)

CLOSER_TYPES = (
    ('反问式', re.compile(r'[？?]$|吗|呢')),
    ('劝勉式', re.compile(r'所以|因此|记住|一定要|不要|别|希望|愿你|一起|加油|共勉')),
    ('结论式', re.compile(r'因为|这就是|才是|终究|最后|结果|说到底|归根结底')),
)


# ---------------------------------------------------------------------------
# 文本清洗与切分
# ---------------------------------------------------------------------------

def clean_text(value):
    """去掉话题标签、链接、零宽字符；换行视为句号，便于按句统计。"""
    text = str(value or '')
    text = text.replace('\ufeff', '').replace('\u200b', '')
    text = _URL.sub(' ', text)
    text = _TAG.sub(' ', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'[ \t\u3000]+', '', text)
    text = re.sub(r'\n+', '。', text)
    text = re.sub(r'。{2,}', '。', text)
    return text.strip('。 ')


def char_len(value):
    """中文字/数字/字母的有效字数，不含标点和空格。"""
    return len(_CHAR_RE.findall(str(value or '')))


def split_sentences(text):
    """按中文句末标点切句，保留标点；分号也算句界。"""
    out = []
    for match in _SENTENCE_RE.finditer(text or ''):
        sentence = match.group(0).strip()
        if sentence:
            out.append(sentence)
    return out


def split_clauses(sentence):
    return [x.strip() for x in _CLAUSE_SEP.split(sentence or '') if x.strip()]


def _clip(value, maximum=48):
    value = str(value or '').strip()
    if len(value) <= maximum:
        return value
    return value[:maximum].rstrip('，,、：:')


def _frame(value):
    """句式聚类键：数字和英文按槽位归一，避免把同类开头拆散。"""
    text = re.sub(r'\d+(?:\.\d+)?', '{数}', str(value or ''))
    text = re.sub(r'[A-Za-z]+', '{词}', text)
    text = re.sub(r'[^\u4e00-\u9fff{}]', '', text)
    return text[:10]


def _opening_quote(text):
    sentences = split_sentences(text)
    if not sentences:
        return ''
    first = sentences[0]
    clauses = split_clauses(first)
    if clauses and char_len(clauses[0]) >= 4:
        return _clip(clauses[0])
    return _clip(first)


def _closing_quote(text):
    sentences = split_sentences(text)
    if not sentences:
        return ''
    last = sentences[-1]
    clauses = split_clauses(last)
    if clauses and char_len(clauses[-1]) >= 4:
        return _clip(clauses[-1])
    return _clip(last)


def _type_of(value, patterns, default):
    for label, pattern in patterns:
        if pattern.search(value or ''):
            return label
    return default


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _sentence_stats(rows):
    lengths = []
    shortest = None
    longest = None
    for row in rows:
        for sentence in row['sentences']:
            size = char_len(sentence)
            if size < 1:
                continue
            lengths.append(size)
            if shortest is None or size < shortest[0]:
                shortest = (size, sentence, row['id'])
            if longest is None or size > longest[0]:
                longest = (size, sentence, row['id'])
    if not lengths:
        return {'avg_chars': 0, 'median_chars': 0, 'p90_chars': 0, 'min_chars': 0,
                'max_chars': 0, 'sentence_count': 0, 'avg_sentences_per_work': 0,
                'short_max': SHORT_MAX, 'long_min': LONG_MIN,
                'short_ratio': 0, 'long_ratio': 0, 'shortest': None, 'longest': None}
    return {
        'avg_chars': round(statistics.fmean(lengths), 1),
        'median_chars': round(statistics.median(lengths), 1),
        'p90_chars': round(sorted(lengths)[max(0, math.ceil(len(lengths) * 0.9) - 1)], 1),
        'min_chars': min(lengths),
        'max_chars': max(lengths),
        'sentence_count': len(lengths),
        'avg_sentences_per_work': round(len(lengths) / len(rows), 1) if rows else 0,
        'short_max': SHORT_MAX,
        'long_min': LONG_MIN,
        'short_ratio': round(sum(1 for x in lengths if x <= SHORT_MAX) / len(lengths), 3),
        'long_ratio': round(sum(1 for x in lengths if x >= LONG_MIN) / len(lengths), 3),
        'shortest': ({'chars': shortest[0], 'quote': shortest[1], 'work_id': shortest[2]}
                     if shortest else None),
        'longest': ({'chars': longest[0], 'quote': longest[1], 'work_id': longest[2]}
                    if longest else None),
    }


def _quote_groups(pairs, limit=5, kind='opener'):
    """把开头/结尾真实摘句按句式聚类，返回 Top N，每条都带作品 ID。"""
    groups = {}
    for quote, work_id in pairs:
        if not quote:
            continue
        key = _frame(quote) or quote[:6]
        groups.setdefault(key, []).append((quote, work_id))
    rows = []
    for items in groups.values():
        work_ids = list(dict.fromkeys(work_id for _, work_id in items))
        # 代表句取最接近“句式长度”的一条，避免极短或极长把组带偏。
        representative = min(items, key=lambda x: (abs(len(x[0]) - 14), len(x[0]), x[1]))
        rows.append({
            'pattern': representative[0],
            'quote': representative[0],
            'work_id': representative[1],
            'count': len(work_ids),
            'work_ids': work_ids,
            'examples': [{'quote': quote, 'work_id': work_id} for quote, work_id in items[:3]],
        })
    rows.sort(key=lambda r: (-r['count'], len(r['pattern']), r['work_id']))
    result = rows[:limit]
    patterns = OPENER_TYPES if kind == 'opener' else CLOSER_TYPES
    for row in result:
        row['type'] = _type_of(row['quote'], patterns, '陈述式')
    return result


def _address_stats(rows, total_chars):
    counts = {}
    work_counts = {}
    for name, pattern in PRONOUNS:
        counts[name] = 0
        work_counts[name] = 0
        for row in rows:
            hits = len(pattern.findall(row['text']))
            counts[name] += hits
            if hits:
                work_counts[name] += 1
    per_1000 = {name: round(value / total_chars * 1000, 1) if total_chars else 0
                for name, value in counts.items()}
    dominant = max(counts, key=lambda name: (counts[name], -[x[0] for x in PRONOUNS].index(name)))
    spoken = sum(counts[x] for x in ('我', '我们', '你', '你们', '咱', '咱们'))
    return {
        'counts': counts,
        'work_counts': work_counts,
        'per_1000_chars': per_1000,
        'dominant': dominant if counts.get(dominant) else '',
        'spoken_total': spoken,
    }


def _catchphrase_stats(rows):
    items = []
    for phrase in CATCH_PHRASES:
        work_ids = [row['id'] for row in rows if phrase in row['text']]
        count = sum(row['text'].count(phrase) for row in rows)
        if count < 2:
            continue
        example = None
        for row in rows:
            if phrase in row['text']:
                sentence = next((s for s in row['sentences'] if phrase in s), row['text'])
                example = {'quote': _clip(sentence, 60), 'work_id': row['id']}
                break
        items.append({
            'phrase': phrase,
            'count': count,
            'work_count': len(work_ids),
            'work_ids': work_ids,
            'example': example,
        })
    items.sort(key=lambda x: (-x['count'], -len(x['phrase']), x['phrase']))
    return items[:8]


def _keyword_stats(rows):
    """无分词库时的降级：2–4 字高频词，按作品覆盖数和词长加权。"""
    counts = {}
    coverage = {}
    for row in rows:
        seen = set()
        for run in _CJK_RUN.findall(row['text']):
            for size in (2, 3, 4):
                for index in range(0, len(run) - size + 1):
                    candidate = run[index:index + size]
                    if candidate in STOPWORDS:
                        continue
                    if candidate[0] in BAD_START or candidate[-1] in BAD_END:
                        continue
                    if len(set(candidate)) == 1:
                        continue
                    counts[candidate] = counts.get(candidate, 0) + 1
                    if candidate not in seen:
                        coverage[candidate] = coverage.get(candidate, 0) + 1
                        seen.add(candidate)
    candidates = []
    for word, count in counts.items():
        covered = coverage.get(word, 0)
        if count < 2 or covered < 2:
            continue
        # 碎片过滤：
        # 1) 更短的片段，如果被更完整的长词覆盖且频次不低，丢掉；
        # 2) 更长的片段，如果内部某个子词覆盖的作品更多，说明它只是拼接碎片，丢掉。
        longer = any(
            len(other) > len(word) and word in other
            and counts.get(other, 0) >= count and coverage.get(other, 0) >= covered
            for other in counts
        )
        inner = [word[start:end] for start in range(len(word))
                 for end in range(start + 2, len(word) + 1) if end - start < len(word)]
        max_inner = max((coverage.get(part, 0) for part in inner), default=0)
        if longer or covered < max_inner:
            continue
        score = count * (1 + 0.15 * (len(word) - 2)) * (1 + 0.25 * (covered - 1))
        candidates.append((score, word, count, covered))
    candidates.sort(key=lambda x: (-x[0], -x[2], -len(x[1]), x[1]))
    picked = []
    for _, word, count, covered in candidates:
        if any(word in other['word'] or other['word'] in word for other in picked):
            continue
        picked.append({
            'word': word,
            'count': count,
            'work_count': covered,
            'work_ids': [row['id'] for row in rows if word in row['text']],
        })
        if len(picked) >= 8:
            break
    return picked


def _avoid_words(rows):
    items = []
    for word, reason in WATER_WORDS.items():
        count = sum(row['text'].count(word) for row in rows)
        work_ids = [row['id'] for row in rows if word in row['text']]
        if count < 3 or len(work_ids) < 2:
            continue
        example = None
        for row in rows:
            if word in row['text']:
                sentence = next((s for s in row['sentences'] if word in s), row['text'])
                example = {'quote': _clip(sentence, 60), 'work_id': row['id']}
                break
        items.append({
            'word': word,
            'count': count,
            'work_count': len(work_ids),
            'work_ids': work_ids,
            'reason': reason,
            'example': example,
        })
    items.sort(key=lambda x: (-x['count'], x['word']))
    return items[:8]


def _emotion_stats(rows, total_chars):
    all_text = ''.join(row['text'] for row in rows)
    exclamation = all_text.count('！') + all_text.count('!')
    question = all_text.count('？') + all_text.count('?')
    rhetorical = sum(len(re.findall(marker, all_text)) for marker in RHETORICAL_MARKERS)
    numbers = len(re.findall(r'\d+(?:\.\d+)?', all_text))
    enumeration = sum(len(re.findall(pattern, all_text)) for pattern in ENUM_PATTERNS)
    density = (exclamation + question) / total_chars * 1000 if total_chars else 0
    level = '高' if density >= 8 else '中' if density >= 3 else '低'
    samples = []
    for row in rows:
        for sentence in row['sentences']:
            if ('！' in sentence or '!' in sentence) and len(samples) < 2:
                samples.append({'kind': '感叹', 'quote': _clip(sentence, 60), 'work_id': row['id']})
        for sentence in row['sentences']:
            if ('？' in sentence or '?' in sentence) and sum(1 for x in samples if x['kind'] == '提问') < 2:
                samples.append({'kind': '提问', 'quote': _clip(sentence, 60), 'work_id': row['id']})
    return {
        'exclamation_count': exclamation,
        'question_count': question,
        'rhetorical_count': rhetorical,
        'number_count': numbers,
        'enumeration_count': enumeration,
        'per_1000_chars': {
            'exclamation': round(exclamation / total_chars * 1000, 2) if total_chars else 0,
            'question': round(question / total_chars * 1000, 2) if total_chars else 0,
            'rhetorical': round(rhetorical / total_chars * 1000, 2) if total_chars else 0,
            'number': round(numbers / total_chars * 1000, 2) if total_chars else 0,
        },
        'expressiveness_level': level,
        'level_rule': '按每千字感叹号+问号数量分档：≥8 高，3–8 中，<3 低；只作相对描述。',
        'samples': samples[:4],
    }


def _has_three_part(text):
    for group in THREE_PART_GROUPS:
        if len(group) == 1:
            if len(re.findall(group[0], text)) >= 2:
                return True
        elif sum(1 for pattern in group if re.search(pattern, text)) >= 2:
            return True
    return False


def _question_opening(row):
    if not row['sentences']:
        return False
    first = row['sentences'][0]
    if '？' in first or '?' in first:
        return True
    return bool(re.search(r'(为什么|怎么|怎样|如何|是不是|要不要|能不能|有没有|吗|呢)[？?]?$', first))


def _rhythm_stats(rows, sentence_stats):
    three_ids = [row['id'] for row in rows if _has_three_part(row['text'])]
    question_ids = [row['id'] for row in rows if _question_opening(row)]
    line_counts = [len([x for x in row['raw'].splitlines() if x.strip()]) or 1 for row in rows]
    three_ratio = round(len(three_ids) / len(rows), 3) if rows else 0
    question_ratio = round(len(question_ids) / len(rows), 3) if rows else 0
    summary = []
    if question_ratio >= RHYTHM_THRESHOLD:
        summary.append('常用提问开场')
    if three_ratio >= RHYTHM_THRESHOLD:
        summary.append('常用分点/三步结构')
    if sentence_stats['short_ratio'] >= 0.5:
        summary.append('短句为主')
    elif sentence_stats['avg_chars'] >= 28:
        summary.append('长句为主')
    if not summary:
        summary.append('以陈述为主，未出现稳定的三段式或提问开场')
    return {
        'threshold': RHYTHM_THRESHOLD,
        'three_part_work_ids': three_ids,
        'three_part_ratio': three_ratio,
        'prefers_three_part': three_ratio >= RHYTHM_THRESHOLD,
        'question_opening_work_ids': question_ids,
        'question_opening_ratio': question_ratio,
        'prefers_question_opening': question_ratio >= RHYTHM_THRESHOLD,
        'avg_sentences_per_work': sentence_stats['avg_sentences_per_work'],
        'avg_lines_per_work': round(statistics.fmean(line_counts), 1) if line_counts else 0,
        'summary': summary,
    }


# ---------------------------------------------------------------------------
# 风格卡组装
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 2026-11-17 t80：风格卡语料从「对标账号文案」换成「本人真实口播转写」。
# 只读本机已有 records（transcript/platform_work/asset/project），不新造数据源。
# 过滤：转写必须来自本人素材（asset.purpose in shooting/direct/upload 或 project.mode=direct），
# 剔除 purpose=reference 的对标/参考视频；过短（<40 字）的残片不进语料。
# ---------------------------------------------------------------------------
OWNER_ASSET_PURPOSES = ('shooting', 'direct', 'upload', 'capture', 'creation')
MIN_OWNER_TEXT = 40
VOICE_SAMPLE_TOTAL = 1200


def _owner_transcript_rows(store):
    rows = []
    for rec in store.list(KIND_TRANSCRIPT, 5000):
        text = clean_text(rec.get('text') or '')
        if char_len(text) < MIN_OWNER_TEXT:
            continue
        asset = store.get(KIND_ASSET, rec.get('asset_id') or '') or {}
        project = store.get(KIND_PROJECT, rec.get('project_id') or '') or {}
        purpose = str(asset.get('purpose') or '')
        owner = purpose in OWNER_ASSET_PURPOSES or str(project.get('mode') or '') == 'direct'
        if not owner:
            # 对标/参考视频转写（platform_link）不进本人风格语料。
            continue
        rows.append({'id': 'transcript:' + str(rec.get('id') or ''),
                     'title': text, 'text': text,
                     'project_id': rec.get('project_id') or '',
                     'asset_id': rec.get('asset_id') or '',
                     'purpose': purpose,
                     'published_at': str(rec.get('updated_at') or rec.get('created_at') or '')})
    rows.sort(key=lambda x: (x['published_at'], x['id']), reverse=True)
    return rows


def collect_owner_corpus(store):
    """本人逐字语料（转写优先）；不足 MIN_WORKS 时返回空，由调用方决定是否降级。"""
    return _owner_transcript_rows(store)


def owner_corpus_stats(store):
    rows = collect_owner_corpus(store)
    return {'count': len(rows), 'chars': sum(char_len(r['text']) for r in rows),
            'min_required': MIN_WORKS}


def _sentence_pick(text, limit=3):
    """挑有口语特征的句子做 few-shot 样本（带了口癖/短句/反问的优先）。"""
    picked = []
    for sentence in split_sentences(text):
        clean = sentence.strip()
        n = char_len(clean)
        if n < 8 or n > 80:
            continue
        score = 0
        for word in ('其实', '真的', '就是', '然后', '反正', '是吧', '对吧', '咱', '我觉得', '可能'):
            if word in clean:
                score += 2
        if clean.endswith(('？', '?')):
            score += 1
        if n <= 20:
            score += 1
        picked.append((score, clean))
    picked.sort(key=lambda x: (-x[0], -char_len(x[1])))
    return [p[1] for p in picked[:limit]]


def voice_samples(store, limit=4, total_chars=VOICE_SAMPLE_TOTAL):
    """本人逐字样本（few-shot 锚定）：优先口癖/短句/反问句；整段预算 ≤ total_chars。"""
    rows = collect_owner_corpus(store)
    samples = []
    used = 0
    for row in rows:
        for sentence in _sentence_pick(row['text'], limit=2):
            if used + char_len(sentence) > total_chars:
                return samples
            samples.append({'text': sentence, 'source': row['id']})
            used += char_len(sentence)
            if len(samples) >= limit * 2:
                return samples
    return samples


def catchphrase_candidates(store, min_count=2, top=16):
    """n-gram 口癖候选（2–5 字）：从本人转写统计高频短语，排除停用词与常见套话。"""
    rows = collect_owner_corpus(store)
    counts = {}
    for row in rows:
        text = row['text']
        for n in (2, 3, 4, 5):
            for i in range(0, max(0, len(text) - n + 1)):
                gram = text[i:i + n]
                if not all('\u4e00' <= ch <= '\u9fff' for ch in gram):
                    continue
                if gram in STOPWORDS or any(w in gram for w in ('的', '了', '是', '我们', '你们')):
                    continue
                counts[gram] = counts.get(gram, 0) + 1
    items = [{'phrase': k, 'count': v} for k, v in counts.items() if v >= min_count]
    items.sort(key=lambda x: (-x['count'], -len(x['phrase'])))
    return items[:top]


def confirmed_catchphrases(store):
    profile = store.get(PROFILE_KIND, PROFILE_KEY) or {}
    return [str(x) for x in (profile.get('voice_catchphrases') or []) if str(x).strip()][:20]


def save_catchphrases(store, phrases):
    """本人勾选确认后的口癖（只保存本人选中的；未勾选不生效）。"""
    clean = []
    for phrase in phrases or []:
        text = str(phrase or '').strip()
        if text and text not in clean:
            clean.append(text[:12])
    old = store.get(PROFILE_KIND, PROFILE_KEY) or {}
    record = {key: value for key, value in old.items()
              if key not in ('id', 'revision', 'created_at', 'updated_at')}
    record['voice_catchphrases'] = clean[:20]
    record['voice_catchphrases_confirmed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return store.put(PROFILE_KIND, record, PROFILE_KEY, old.get('revision', 0))


def voice_sample_block(store, samples=None, limit_chars=VOICE_SAMPLE_TOTAL):
    """给写稿提示词的【本人逐字样本】段：样本 + 本人勾选口癖 + 说话禁区（负面约束）。"""
    samples = samples if samples is not None else voice_samples(store)
    catchphrases = confirmed_catchphrases(store)
    lines = ['【本人逐字样本】（下面的原话才是他的口吻；沿用语感，不要照抄整句）']
    used = 0
    for item in samples:
        text = str(item.get('text') or '').strip()
        if not text or used + char_len(text) > limit_chars:
            continue
        lines.append('· ' + text)
        used += char_len(text)
    if catchphrases:
        lines.append('【本人确认的口癖】' + '、'.join(catchphrases))
    lines.append('【本人说话禁区】禁「首先/其次/总之/换句话说」这类套话连接词；禁排比升华（这不是…而是…）；'
                 '禁号召腔（让我们一起/你一定要相信）；禁书面语长句。宁可短、宁可留一点停顿。')
    return '\n'.join(lines)


def _profile_rows(works):
    rows = []
    for work in works:
        raw = str(work.get('title') or '')
        text = clean_text(raw)
        if char_len(text) < 8:
            continue
        opening = _opening_quote(text)
        closing = _closing_quote(text)
        # 单句且无逗号时，开头和结尾是同一句；不把它重复算成“收束句式”。
        if closing == opening:
            closing = ''
        rows.append({
            'id': work.get('id') or '',
            'raw': raw,
            'text': text,
            'sentences': split_sentences(text),
            'opening': opening,
            'closing': closing,
        })
    return rows


def _account_info(store, works, account_id):
    ids = list(dict.fromkeys(w.get('bench_account_id') for w in works if w.get('bench_account_id')))
    if account_id and account_id not in ids:
        ids = [account_id] + ids
    name = ''
    if account_id:
        account = store.get(KIND_BENCH_ACCOUNT, account_id) or {}
        name = account.get('display_name') or ''
    elif len(ids) == 1:
        account = store.get(KIND_BENCH_ACCOUNT, ids[0]) or {}
        name = account.get('display_name') or ''
    return {
        'account_id': account_id or (ids[0] if len(ids) == 1 else ''),
        'account_ids': ids,
        'account_name': name,
    }


def _source_fingerprint(rows):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda x: x['id']):
        digest.update(row['id'].encode('utf-8'))
        digest.update(b'\n')
        digest.update(row['text'].encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()[:16]


def _build_statistics(store, works, account_id):
    rows = _profile_rows(works)
    total_chars = sum(char_len(row['text']) for row in rows)
    sentence = _sentence_stats(rows)
    openers = _quote_groups([(row['opening'], row['id']) for row in rows], 5, 'opener')
    closers = _quote_groups([(row['closing'], row['id']) for row in rows], 5, 'closer')
    address = _address_stats(rows, total_chars)
    emotion = _emotion_stats(rows, total_chars)
    rhythm = _rhythm_stats(rows, sentence)
    info = _account_info(store, works, account_id)
    published = [str(w.get('published_at') or w.get('collected_at') or '') for w in works]
    card = {
        'schema': SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'method': 'statistics',
        'source': {
            'account_id': info['account_id'],
            'account_ids': info['account_ids'],
            'account_name': info['account_name'],
            'work_count': len(works),
            'text_work_count': len(rows),
            'min_works': MIN_WORKS,
            'work_ids': [row['id'] for row in rows],
            'latest_published_at': max(published) if published else '',
            'text_chars': total_chars,
            'fingerprint': _source_fingerprint(rows),
        },
        'sentence': sentence,
        'openers': openers,
        'closers': closers,
        'address': address,
        'catchphrases': _catchphrase_stats(rows),
        'keywords': _keyword_stats(rows),
        'emotion': emotion,
        'rhythm': rhythm,
        'avoid_words': _avoid_words(rows),
        'llm': {'attempted': False, 'ok': False, 'valid': 0, 'rejected': 0, 'reason': 'not_attempted'},
        'llm_insights': None,
        'work_samples': [{'work_id': row['id'], 'quote': _clip(row['opening'] or row['text'], 80)}
                         for row in rows[:3]],
    }
    card['card_id'] = 'stylecard_' + hashlib.sha256(
        json.dumps({'source': card['source'], 'openers': openers, 'closers': closers},
                   ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:12]
    return card


def _prompt_block(card):
    if not isinstance(card, dict) or not card.get('source'):
        return ''
    source = card.get('source') or {}
    sentence = card.get('sentence') or {}
    address = card.get('address') or {}
    emotion = card.get('emotion') or {}
    rhythm = card.get('rhythm') or {}
    lines = ['【账号风格卡·基于 %s 条真实作品】' % source.get('text_work_count', 0)]
    lines.append('句长：平均 %s 字/句；短句(≤%s字)占 %s%%；长句(≥%s字)占 %s%%。' % (
        sentence.get('avg_chars', 0), SHORT_MAX,
        round((sentence.get('short_ratio') or 0) * 100), LONG_MIN,
        round((sentence.get('long_ratio') or 0) * 100)))
    openers = card.get('openers') or []
    if openers:
        lines.append('开头 Top%d：%s' % (min(3, len(openers)), '；'.join(
            '“%s”' % item.get('quote', '') for item in openers[:3])))
    closers = card.get('closers') or []
    if closers:
        lines.append('结尾 Top%d：%s' % (min(3, len(closers)), '；'.join(
            '“%s”' % item.get('quote', '') for item in closers[:3])))
    counts = address.get('counts') or {}
    if counts:
        lines.append('称呼：%s。' % ' / '.join(
            '%s %s 次' % (name, counts[name]) for name in ('我', '我们', '你', '你们', '咱', '咱们', '自己', '大家')
            if counts.get(name)))
    catch = card.get('catchphrases') or []
    if catch:
        lines.append('口头禅：%s。' % '、'.join('%s(%s次)' % (x.get('phrase'), x.get('count')) for x in catch[:5]))
    keywords = card.get('keywords') or []
    if keywords:
        lines.append('高频词：%s。' % '、'.join('%s(%s次)' % (x.get('word'), x.get('count')) for x in keywords[:6]))
    rates = emotion.get('per_1000_chars') or {}
    lines.append('情绪：感叹 %.1f/千字，提问 %.1f/千字，反问词 %s 次，数字 %s 处。' % (
        rates.get('exclamation', 0), rates.get('question', 0),
        emotion.get('rhetorical_count', 0), emotion.get('number_count', 0)))
    lines.append('节奏：%s；提问开场占 %s%%，三段式占 %s%%，平均每篇 %s 句。' % (
        '、'.join(rhythm.get('summary') or []),
        round((rhythm.get('question_opening_ratio') or 0) * 100),
        round((rhythm.get('three_part_ratio') or 0) * 100),
        rhythm.get('avg_sentences_per_work', 0)))
    avoid = card.get('avoid_words') or []
    if avoid:
        lines.append('避免水词：%s。' % '、'.join('%s(%s次)' % (x.get('word'), x.get('count')) for x in avoid[:6]))
    insights = (card.get('llm_insights') or {}).get('observations') or []
    if insights:
        lines.append('模型归纳（每条都有原文可回查）：%s' % '；'.join(
            '%s（例：“%s”）' % (x.get('claim', ''), x.get('quote', '')) for x in insights[:3]))
    lines.append('写稿要求：模仿上述句长、称呼和节奏；引用句只作风格证据，不得整句照抄；'
                 '内容必须来自本次选题和本人真实经历，不得虚构。')
    return '\n'.join(lines)


def to_prompt_block(card):
    """把风格卡压成可直接塞进写稿 prompt 的中文段落；无卡返回空串。"""
    return _prompt_block(card)


def profile_style_block(store):
    """从 profile 读 style_card 并转成 prompt 段落；老档案没有该字段时返回空串。"""
    card = (store.get(PROFILE_KIND, PROFILE_KEY) or {}).get('style_card')
    return _prompt_block(card) if isinstance(card, dict) else ''


# ---------------------------------------------------------------------------
# 个人档案进提示词：字段、顺序、单字段上限（字）与整块预算
# 目的：把行业、经历、痛点、看完做什么、变现方式、更新频率、片长、红线、禁忌
# 都带进选题/写稿/洗稿；老档案没有这些键时按空处理，不报错。
# 超长处理：单字段先硬截，整块到 PROFILE_TOTAL_CHARS 为止（按上面的顺序优先保前面的）。
# ---------------------------------------------------------------------------
PROFILE_CONTEXT_FIELDS = (
    ('name', '本人', 60),
    ('industry', '行业', 300),
    ('audience', '目标受众', 200),
    ('experience', '过往经历', 500),
    ('product', '产品/服务', 300),
    ('pain_points', '观众痛点', 300),
    ('after_view', '看完做什么', 200),
    ('monetization', '变现方式', 200),
    ('update_frequency', '更新频率', 60),
    ('video_length', '片长', 60),
    ('style', '口吻', 300),
    ('redlines', '红线', 200),
    ('taboos', '禁忌', 200),
    ('account', '账号', 80),
)
PROFILE_TOTAL_CHARS = 3000


def _squash(value, limit):
    """压成一行并截断：口播提示词不需要原文换行堆叠。

    超长时优先在句号/分号/问号处收口，避免把一句话从中间切断；
    找不到合适句末才硬截。
    """
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in '。；！？!?;')
    if cut >= int(limit * 0.6):
        return head[:cut + 1]
    return head


def profile_context(profile, total_chars=PROFILE_TOTAL_CHARS):
    """把档案压成可注入 prompt 的短 dict。

    逐字段限长，整块再按总预算截；空字段不返回，老档案缺键按空处理。
    """
    out = {}
    budget = max(0, int(total_chars))
    for key, _label, limit in PROFILE_CONTEXT_FIELDS:
        if budget <= 0:
            break
        value = _squash((profile or {}).get(key), min(limit, budget))
        if not value:
            continue
        out[key] = value
        budget -= len(value)
    return out


def profile_block(profile, total_chars=PROFILE_TOTAL_CHARS):
    """把 profile_context 拼成中文提示词段落；没有档案返回空串。"""
    compact = profile_context(profile, total_chars)
    lines = []
    for key, label, _limit in PROFILE_CONTEXT_FIELDS:
        if compact.get(key):
            lines.append('%s：%s' % (label, compact[key]))
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# LLM 归纳：只接受原文可回查的引用
# ---------------------------------------------------------------------------


def _norm_quote(value):
    return re.sub(r'\s+', '', str(value or ''))


def _resolve_work_id(quote, work_id, text_map):
    """验证引用；work_id 缺失或对不上时，仅当引用只出现在一条作品里才自动补。"""
    quote_norm = _norm_quote(quote)
    if len(quote_norm) < 2:
        return ''
    if work_id and quote_norm in _norm_quote(text_map.get(work_id, '')):
        return work_id
    hits = [key for key, text in text_map.items() if quote_norm in _norm_quote(text)]
    return hits[0] if len(hits) == 1 else ''


def _real_sentence(text, quote_or_phrase):
    for sentence in split_sentences(text):
        if quote_or_phrase in sentence:
            return _clip(sentence, 80)
    return ''


def _merge_llm(answer, works):
    """只保留能回查原文的条目；返回 (insights, meta)。"""
    text_map = {work['id']: clean_text(work.get('title')) for work in works}
    rejected = 0
    if not isinstance(answer, dict):
        return None, {'attempted': True, 'ok': False, 'valid': 0, 'rejected': 1,
                      'reason': '模型未返回 JSON 对象'}

    def keep(item, fields, required_quote=True):
        nonlocal rejected
        if not isinstance(item, dict):
            rejected += 1
            return None
        quote = str(item.get('quote') or '')
        work_id = str(item.get('work_id') or '')
        resolved = _resolve_work_id(quote, work_id, text_map)
        if required_quote and not resolved:
            rejected += 1
            return None
        row = {key: str(item.get(key) or '').strip() for key in fields}
        row['quote'] = quote
        row['work_id'] = resolved
        return row

    observations = []
    for item in (answer.get('observations') or [])[:6]:
        row = keep(item, ('trait', 'claim'))
        if row and row.get('claim'):
            observations.append(row)
        elif row is None:
            pass
    openers = []
    for item in (answer.get('openers') or [])[:5]:
        row = keep(item, ())
        if row:
            openers.append(row)
    closers = []
    for item in (answer.get('closers') or [])[:5]:
        row = keep(item, ())
        if row:
            closers.append(row)
    catchphrases = []
    for item in (answer.get('catchphrases') or [])[:6]:
        row = keep(item, ('phrase',), required_quote=False)
        if row is None:
            continue
        phrase = row.get('phrase') or ''
        quote = row.get('quote') or ''
        if not phrase:
            rejected += 1
            continue
        if quote and row.get('work_id') and _norm_quote(quote) in _norm_quote(text_map[row['work_id']]):
            pass
        else:
            # 引用无效时，从原文里找真实句子补证；补不到就丢弃。
            hits = [wid for wid, text in text_map.items() if phrase in text]
            if len(hits) != 1:
                rejected += 1
                continue
            sentence = _real_sentence(text_map[hits[0]], phrase)
            if not sentence:
                rejected += 1
                continue
            row['quote'] = sentence
            row['work_id'] = hits[0]
        catchphrases.append(row)
    rhythm_notes = []
    for item in (answer.get('rhythm_notes') or [])[:4]:
        row = keep(item, ('note',))
        if row and row.get('note'):
            rhythm_notes.append(row)
    avoid_words = []
    for item in (answer.get('avoid_words') or [])[:6]:
        row = keep(item, ('word',))
        if row and row.get('word'):
            avoid_words.append(row)
    valid = len(observations) + len(openers) + len(closers) + len(catchphrases) + len(rhythm_notes) + len(avoid_words)
    insights = {
        'observations': observations,
        'openers': openers,
        'closers': closers,
        'catchphrases': catchphrases,
        'rhythm_notes': rhythm_notes,
        'avoid_words': avoid_words,
    }
    return insights, {'attempted': True, 'ok': valid > 0, 'valid': valid, 'rejected': rejected,
                      'reason': '' if valid else '没有可回查原文的引用'}


def _llm_payload(store, works, card):
    return {
        'account': card['source'].get('account_name') or '',
        'work_count': len(works),
        'works': [{'id': work['id'], 'text': clean_text(work.get('title'))} for work in works],
        'statistics': {
            'sentence': card['sentence'],
            'openers': card['openers'],
            'closers': card['closers'],
            'address': card['address'],
            'catchphrases': card['catchphrases'],
            'keywords': card['keywords'],
            'emotion': card['emotion'],
            'rhythm': card['rhythm'],
            'avoid_words': card['avoid_words'],
        },
    }


def _call_llm(llm, payload):
    if llm is None:
        if not providers.available().get('llm'):
            return None, {'attempted': False, 'ok': False, 'valid': 0, 'rejected': 0,
                          'reason': '生成模型未配置'}
        llm = providers.llm_json
    try:
        return llm(prompt_store.get('style.analyze'), payload), None
    except Exception as exc:
        return None, {'attempted': True, 'ok': False, 'valid': 0, 'rejected': 0,
                      'error': str(exc)[:300]}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def collect_works(store, account_id=None):
    """风格卡语料：**本人真实口播转写优先**；不足 5 条时才降级到已采集作品文案（并标明来源）。"""
    owner = collect_owner_corpus(store)
    if len(owner) >= MIN_WORKS:
        return owner
    works = store.list(KIND_BENCH_WORK, 10000)
    if account_id:
        works = [work for work in works if work.get('bench_account_id') == account_id]
    works.sort(key=lambda work: (str(work.get('published_at') or work.get('collected_at') or ''),
                                 str(work.get('id') or '')), reverse=True)
    return works


def build_style_card(store, account_id=None, use_llm=True, llm=None, works=None):
    """生成风格卡；样本不足返回 {ok:False,error}，不写档案。"""
    if works is None:
        works = collect_works(store, account_id)
    rows = _profile_rows(works)
    if len(rows) < MIN_WORKS:
        return {
            'ok': False,
            'error': '样本不足，先采集至少 5 条作品（当前可用文案 %d 条，至少需要 %d 条）' % (
                len(rows), MIN_WORKS),
            'work_count': len(rows),
            'total_work_count': len(works),
            'required': MIN_WORKS,
        }
    card = _build_statistics(store, works, account_id)
    owner_rows = collect_owner_corpus(store)
    card['corpus'] = {
        'source': 'owner_transcripts' if len(owner_rows) >= MIN_WORKS else 'bench_works_fallback',
        'owner_transcripts': len(owner_rows),
        'owner_chars': sum(char_len(r['text']) for r in owner_rows),
        'works_used': len(rows),
        'human_flavor_words': list(HUMAN_FLAVOR_WORDS)[:20],
    }
    card['catchphrase_candidates'] = catchphrase_candidates(store)[:12]
    card['confirmed_catchphrases'] = confirmed_catchphrases(store)
    if use_llm:
        payload = _llm_payload(store, works, card)
        answer, error = _call_llm(llm, payload)
        if error:
            card['llm'] = error
        else:
            insights, meta = _merge_llm(answer, works)
            card['llm'] = meta
            if meta.get('ok') and insights:
                card['llm_insights'] = insights
                card['method'] = 'statistics+llm'
    else:
        card['llm'] = {'attempted': False, 'ok': False, 'valid': 0, 'rejected': 0,
                       'reason': '调用方关闭了 LLM'}
    card['prompt_block'] = _prompt_block(card)
    return {'ok': True, 'style_card': card}


def write_style_card(store, card):
    """把风格卡写回 profile；保留旧档案其它字段，老档案没有该字段也能写。"""
    if not isinstance(card, dict) or card.get('schema') != SCHEMA:
        raise ValueError('风格卡格式无效，未写入档案。')
    old = store.get(PROFILE_KIND, PROFILE_KEY) or {}
    expected = old.get('revision', 0)
    record = {key: value for key, value in old.items()
              if key not in ('id', 'revision', 'created_at', 'updated_at')}
    record['style_card'] = card
    record['style_card_updated_at'] = card.get('generated_at')
    return store.put(PROFILE_KIND, record, PROFILE_KEY, expected)


def generate_and_write(store, account_id=None, use_llm=True, llm=None):
    """生成并写回；样本不足时原样返回拒绝结果，不创建/修改档案。"""
    result = build_style_card(store, account_id=account_id, use_llm=use_llm, llm=llm)
    if not result.get('ok'):
        return result
    profile = write_style_card(store, result['style_card'])
    return {**result, 'profile': profile}
