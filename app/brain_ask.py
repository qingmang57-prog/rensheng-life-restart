# -*- coding: utf-8 -*-
"""第二大脑问答：先查本地，再问模型。

顺序（2026-11-16 定）：
1. 先跑 content_index.search；索引没建好时它自己走 LIKE 兜底。
2. 问句能归到固定意图（打卡 / 成片 / 选题 / 画像 / 认知），且本地有记录：
   直接按记录拼答案，标「来自你的记录」，不调模型、不扣额度。
3. 归不到意图，但命中很强、问的又是事实（哪天 / 几次 / 多少 / 最近 / 上次）：
   直接引用原文回答，同样标「来自你的记录」。
4. 其余才调云端模型，标「AI 补充」；提示词要求区分「你的记录」「行业热点」「我的推导」。
"""
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote as urlquote

import applog
import content_index
import growth_service
import creation_service
import prompt_store
import providers
import auth_service
import billing_route
# 2026-11-17 L7 教练魂：教练模式分支、工作守护栏、镜子/对质/挑战都收在 brain_coach。
# 这里只保留第二大脑原来的检索/本地直答/模型问答链路；助手模式行为不变。
import brain_coach

TZ = timezone(timedelta(hours=8))

LOCAL_LABEL = '来自你的记录'
MODEL_LABEL = 'AI 补充'
EMPTY_LABEL = '没找到'

# 意图 -> 触发词 -> 该意图优先看的索引类型。
INTENTS = (
    ('checkin', ('打卡', '连续', '坚持', '断更', '补卡', '签到'), ('checkin_day', 'checkin_profile')),
    ('artifact', ('成片', '视频', '封面', '导出', '片子'), ('artifact', 'script_version')),
    ('topic', ('选题', '题目', '拍什么', '做什么内容', '内容方向'), ('topic',)),
    ('profile', ('我是谁', '我这个人', '我的画像', '画像', '性格', '口吻', '说话风格',
                 '价值观', '三观'), ('flow', 'creator_profile', 'growth_fragment', 'knowledge')),
    ('knowledge', ('认知', '原则', '观点', '看法', '怎么想', '我的判断'), ('knowledge',)),
)

# 这些问法是在要事实，命中够强就直接引用原文，不必绕模型。
FACT_WORDS = ('哪天', '什么时候', '多少', '几次', '几个', '几条', '最近', '上次',
              '上一次', '有没有', '是不是', '在哪', '哪一个', '哪个')

# 命中条目在前端点开时该去哪一页。
ROUTE_LINKS = {
    'checkin_day': '#/checkin', 'checkin_profile': '#/checkin',
    'artifact': '#/create', 'script_version': '#/create', 'project': '#/create',
    'topic': '#/create', 'source': '#/bench', 'bench_work': '#/bench',
    'bench_account': '#/bench', 'bench_comment': '#/bench',
    'flow': '#/restart', 'growth_goal': '#/restart', 'growth_answer': '#/restart',
    'growth_fragment': '#/restart',
    'creator_profile': '#/brain', 'interview_session': '#/brain',
    'interview_turn': '#/brain', 'question': '#/brain', 'news': '#/brain',
}


def _intent(question):
    q = str(question or '').lower()
    best = None
    for name, words, kinds in INTENTS:
        hits = sum(1 for w in words if w in q)
        if hits and (best is None or hits > best[0]):
            best = (hits, name, kinds)
    if best:
        return best[1], best[2]
    return 'general', ()


def _meta(item):
    value = item.get('meta')
    if isinstance(value, dict):
        return value
    try:
        import json
        return json.loads(value or '{}')
    except ValueError:
        return {}


def _clip(text, limit=260):
    value = ' '.join(str(text or '').split())
    return value if len(value) <= limit else value[:limit].rstrip() + '…'


def _link(kind, source_id):
    if kind == 'knowledge':
        return '#/knowledge/' + urlquote(str(source_id))
    return ROUTE_LINKS.get(kind, '#/brain')


def _ref(item):
    kind = item.get('source_kind') or 'knowledge'
    source_id = item.get('source_id') or ''
    return {
        'id': source_id,
        'kind': kind,                    # 兼容旧前端：news 单独一组
        'source_kind': kind,
        'kind_label': item.get('kind_label') or '',
        'title': item.get('title') or '',
        'claim': item.get('snippet') or _clip(item.get('body'), 160),
        'recorded_at': item.get('recorded_at') or '',
        'link': _link(kind, source_id),
        'source_url': item.get('source_url') or '',
        'confidence': item.get('confidence') if isinstance(item.get('confidence'), dict) else None,
    }


def _refs(items, limit=6):
    return [_ref(x) for x in (items or [])[:limit]]


# ---------------------------------------------------------------- 固定意图
def _answer_checkin(store, q):
    rows = content_index.rows_of(store, ('checkin_day',))
    days = []
    for row in rows:
        meta = _meta(row)
        if meta.get('date'):
            days.append((str(meta['date']), row, meta))
    days.sort(key=lambda x: x[0], reverse=True)
    if days:
        latest_date, latest_row, latest_meta = days[0]
        streak = int(latest_meta.get('streak') or 0)
        month = latest_meta.get('month') or latest_date[:7]
        month_count = int(latest_meta.get('month_count') or 0)
        total = len(days)
        if any(w in q for w in ('连续', '坚持', '多久')):
            text = '你最近一次打卡是 %s，连续第 %d 天。' % (latest_date, streak)
        elif any(w in q for w in ('本月', '这个月', '当月', '这月')):
            text = '%s 你已经打卡 %d 天。' % (month, month_count)
        elif any(w in q for w in ('几次', '多少天', '一共', '总共', '累计')):
            text = '从开始到现在，你一共打卡 %d 天；%s 累计 %d 天。' % (total, month, month_count)
        else:
            text = '最近一次打卡是 %s（连续第 %d 天，%s 累计 %d 天）。' % (
                latest_date, streak, month, month_count)
        return text, [latest_row] + [r for _d, r, _m in days[1:4]]
    profiles = content_index.rows_of(store, ('checkin_profile',))
    if profiles:
        return _clip(profiles[0].get('body'), 400), profiles[:3]
    return '', []


def _answer_artifact(store, q):
    rows = content_index.rows_of(store, ('artifact',))
    if not rows:
        return '', []
    if any(w in q for w in ('几条', '几个', '多少', '一共', '总共')):
        return '你的成片记录一共 %d 条（含封面）。' % len(rows), rows[:5]
    latest = rows[0]
    when = str(latest.get('recorded_at') or '')[:10]
    detail = _clip(latest.get('body'), 240) or latest.get('title') or ''
    text = '最近一条成片：%s' % detail + ('（%s）' % when if when else '')
    return text, rows[:5]


def _answer_topic(store, q):
    rows = content_index.rows_of(store, ('topic',))
    if not rows:
        return '', []
    titles = []
    for row in rows[:5]:
        title = _clip(row.get('title'), 60)
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return '', []
    if any(w in q for w in ('几条', '几个', '多少', '一共', '总共')):
        return '你记下了 %d 条选题，最近的几条是：%s。' % (
            len(rows), '、'.join(titles[:3])), rows[:5]
    return '你最近记下的选题：%s。都能在「开始创作」里直接接着写。' % (
        '；'.join('%d) %s' % (i + 1, t) for i, t in enumerate(titles[:3]))), rows[:5]


def _answer_knowledge(items):
    picked = [x for x in items if x.get('source_kind') == 'knowledge'][:3]
    if not picked:
        return '', []
    quotes = []
    for item in picked:
        body = _clip(item.get('body') or item.get('snippet'), 200)
        if body:
            quotes.append('「%s」（《%s》）' % (body, _clip(item.get('title'), 40)))
    if not quotes:
        return '', []
    return '你记录里写过：\n' + '\n'.join(quotes), picked


def _answer_profile(store, items):
    flows = [x for x in items if x.get('source_kind') == 'flow'] or \
        content_index.rows_of(store, ('flow',), limit=1)
    if flows:
        body = _clip(flows[0].get('body'), 500)
        if body:
            return '你的成长档案里写着：%s' % body, flows[:1] + [x for x in items if x.get('source_kind') != 'flow'][:4]
    picked = [x for x in items if x.get('source_kind') != 'news'][:5]
    if not picked:
        return '', []
    body = _clip(picked[0].get('body') or picked[0].get('snippet'), 320)
    if not body:
        return '', picked
    return '跟你最相关的一条记录是《%s》：%s' % (_clip(picked[0].get('title'), 40), body), picked


def _strong_factual(q, items):
    """问的是事实、且第一条命中明显强于噪声，才直接引用，避免答非所问。"""
    if not items:
        return False
    if not any(w in q for w in FACT_WORDS):
        return False
    top = items[0]
    try:
        score = float(top.get('score') or 0)
    except (TypeError, ValueError):
        score = 0.0
    return score >= 8 and len(items) >= 1


def _quote_answer(items):
    top = items[0]
    body = _clip(top.get('body') or top.get('snippet'), 400)
    if not body:
        return ''
    label = top.get('kind_label') or '记录'
    return '在你的「%s」里记着：%s' % (label, body)


# ---------------------------------------------------------------- 行业热点（只给模型做外部补充）
def _news_refs(store, question, limit=3):
    try:
        news_all = store.list('news', 1000)
    except Exception:
        news_all = []
    if not news_all:
        return []
    terms = content_index._terms(question)
    scored = [(sum(1 for t in terms if t in (str(it.get('title') or '') + str(it.get('summary') or ''))), it)
              for it in news_all]
    picked = [it for s, it in sorted(scored, key=lambda x: x[0], reverse=True) if s > 0][:limit]
    if len(picked) < limit:
        seen = {it['id'] for it in picked}
        for it in news_all:
            if it['id'] in seen:
                continue
            picked.append(it)
            seen.add(it['id'])
            if len(picked) >= limit:
                break
    return [{'id': it['id'], 'kind': 'news', 'source_kind': 'news', 'kind_label': '行业热点',
             'title': ' '.join(str(it.get('title') or '').split()),
             'claim': _clip(it.get('summary'), 160),
             'boundary': '外部行业资讯（%s），不是本人观点' % (it.get('source') or ''),
             'recorded_at': it.get('published') or it.get('fetched_at') or '',
             'link': it.get('url') or '#/brain', 'source_url': it.get('url') or '',
             'source': it.get('source') or ''} for it in picked]


# ---------------------------------------------------------------- 对话卡与会话
PILLARS = list(content_index.TARGET_CATEGORIES)
# 弱相关阈值：实测强命中相关度 53（"钱从差里来"）、中等 28–22，仅命中"一条/知道"这类虚词时 19。
# 取 30 作为分界：>=30 视为够贴，照常引用；<30 仍挂卡，但如实标注"库里没有很贴的"。
WEAK_SCORE = 30


def _quote_sentence(text, limit=120):
    # 库里有些 claim 带 markdown 加粗（**…**），引文条按纯文本显示，先把标记去掉再加引号。
    value = ' '.join(str(text or '').replace('**', '').replace('__', '').split())
    if len(value) <= limit:
        return value
    cut = max(value.rfind(ch, 0, limit) for ch in '。！？!?\n')
    if cut >= 20:
        return value[:cut + 1]
    return value[:limit].rstrip() + '…'


def _card_of(store, item, terms, rank, where):
    """把一条检索命中转成可引用的卡；字段取库里的最新版本，引文逐字来自原文。"""
    source_id = item.get('source_id') or ''
    if not source_id:
        return None
    record = {}
    try:
        record = store.get('knowledge', source_id) or {}
    except Exception:
        record = {}
    body = str(record.get('claim') or item.get('claim') or item.get('snippet') or item.get('body') or '')
    title = str(record.get('title') or item.get('title') or '')
    if not body and not title:
        return None
    category, legacy = content_index.normalize_category(
        record.get('category'), record.get('category_legacy'))
    matched = [t for t in terms if t and t in (title + body)][:3]
    try:
        score = int(float(item.get('score') or 0))
    except (TypeError, ValueError):
        score = 0
    return {
        'id': source_id, 'title': title, 'category': category,
        'category_legacy': legacy, 'quote': _quote_sentence(body or title),
        'date': str(record.get('recorded_at') or item.get('recorded_at') or '')[:10],
        'confidence': content_index.confidence(record),
        'link': _link('knowledge', source_id),
        'revision': record.get('revision') or 0,
        'score': score, 'rank': rank, 'weak': score < WEAK_SCORE,
        'reason': '命中「%s」；相关度 %d；%s%s'
                  % ('、'.join(matched) or '语义相近', score, where,
                     '' if score >= WEAK_SCORE else '；低于阈值 %d，按弱相关标注' % WEAK_SCORE),
    }


def _focus_card(store, items, question, rank_limit=12):
    """每次回答必须挂一张卡：通用检索没有认知卡时，单独在 195 张卡里再检索一次。

    依据都可复现：先按检索名次，再按认知卡二次检索名次，最后取最近一张并写明依据。
    """
    terms = content_index._terms(question)
    for rank, item in enumerate(items[:rank_limit], 1):
        if item.get('source_kind') != 'knowledge':
            continue
        card = _card_of(store, item, terms, rank, '检索第 %d 条' % rank)
        if card:
            return card
    try:
        again = content_index.search(store, question, limit=80, include_body=True)
    except Exception:
        again = {}
    picked = [x for x in (again.get('items') or []) if x.get('source_kind') == 'knowledge']
    for rank, item in enumerate(picked[:rank_limit], 1):
        card = _card_of(store, item, terms, rank, '认知卡二次检索第 %d 条' % rank)
        if card:
            return card
    try:
        rows = content_index.rows_of(store, ('knowledge',), limit=300)
    except Exception:
        rows = []
    if rows:
        card = _card_of(store, rows[0], terms, 0, '关键词未命中，取最近一张卡')
        if card:
            return card
    return None


def _session(store):
    try:
        rows = store.list('brain_session', 1)
        return rows[0] if rows else {}
    except Exception:
        return {}


def _save_session(store, question, card):
    try:
        prev = _session(store)
        payload = {'last_question': str(question)[:120],
                   'last_card_id': (card or {}).get('id', ''),
                   'last_card_title': (card or {}).get('title', ''),
                   'last_category': (card or {}).get('category', ''),
                   'updated_at': datetime.now(TZ).isoformat(timespec='seconds')}
        if prev.get('id'):
            store.patch('brain_session', prev['id'], payload)
        else:
            store.put('brain_session', payload, 'main')
    except Exception:
        pass


# ---------------------------------------------------------------- 开场选题
# 2026-11-16：开场不再接上次，改成从库里挑一张“值得问”的卡，把知识往前推。
# 三类轮换（新鲜 → 孤立 → 久未改写），同一张卡问过就进 14 天冷却；状态落 brain_session，重启仍在。
OPENING_COOLDOWN_DAYS = 14
OPENING_MAX_CHARS = 30
OPENING_CLASSES = ('fresh', 'orphan', 'stale')
OPENING_CLASS_LABEL = {'fresh': '最近 24 小时新增', 'orphan': '孤立卡（图谱关联 0）',
                       'stale': '长期没改写'}


def _dt(value):
    try:
        text = str(value or '').strip().replace('Z', '+00:00')
        got = datetime.fromisoformat(text)
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _degrees(store):
    """图谱度数。graph_view 自己会兜底；这里再兜一层，取不到就全按 0（宁可多问孤立卡，不让开场崩）。"""
    try:
        import graph_view
        cards = graph_view.build(store).get('cards') or []
        return {str(c.get('id')): int(c.get('degree') or 0) for c in cards if c.get('id')}
    except Exception:
        return {}


def _opening_state(prev):
    value = (prev or {}).get('opening_state')
    return dict(value) if isinstance(value, dict) else {}


def _fit_title(title, budget):
    text = re.sub(r'\s+', '', str(title or ''))
    text = re.sub(r'[（(][^）)]*[）)]', '', text)
    text = re.sub(r'[《》「」“”"\']', '', text)
    # 标题本身可能带问句（“钱从哪来？…”），开场只能有一个问号：截到第一个标点为止
    text = re.split(r'[。！？!?；;，,、：:]', text)[0]
    return text[:max(4, budget)].strip()


def _opening_question(card, cls, degree, now):
    """一句、≤30 字、只问一个“内容为什么是这样”的问题，只说标题片段不贴原文。"""
    title = str(card.get('title') or '').strip()
    if not title:
        return ''
    created = _dt(card.get('created_at')) or _dt(card.get('updated_at'))
    updated = _dt(card.get('updated_at'))
    days = max(0, (now - updated).days) if updated else 0
    for budget in (14, 12, 10, 8, 6, 4):
        short = _fit_title(title, budget)
        if len(short) < 4:
            continue
        if cls == 'fresh':
            date = created.astimezone(TZ).strftime('%m-%d') if created else ''
            text = '《%s》是%s记的，当时为什么想到它？' % (short, date)
        elif cls == 'orphan':
            text = '《%s》只有你记过，当时为什么单独记它？' % short
        else:
            ago = '%d天没改了' % days if days >= 2 else '一直没改过'
            text = '《%s》%s，当初为什么这么定？' % (short, ago)
        if len(text) <= OPENING_MAX_CHARS:
            return text
    return ''


def _pick_opening(store, prev, now=None, task=''):
    """按 新鲜 / 孤立 / 久未改写 三类轮换挑一张；跳过 14 天内问过的和上一张。

    2026-11-17（L7 M3）：多一个 task 参数——任务触发时只在「跟这件事相关」的卡里挑，
    相关度高的优先；没有一张贴得上就返回空，不硬造对质。不传 task 时行为与原来完全一致。
    """
    now = now or datetime.now(timezone.utc)
    task = str(task or '').strip()
    try:
        terms = [t for t in content_index._terms(task) if t] if task else []
    except Exception:  # noqa: BLE001
        terms = [t for t in re.split(r'[\s,，、。]+', task) if len(t) >= 2] if task else []
    if task and not terms:
        # 任务里抽不出关键词：宁可不开始，也不拿一张无关的卡冒充对质。
        return {}
    state = _opening_state(prev)
    asked = dict(state.get('asked') or {})
    try:
        rows = store.list('knowledge', 1000)
    except Exception:
        rows = []
    degree = _degrees(store)
    last_id = str((prev or {}).get('last_card_id') or '')
    pools = {name: [] for name in OPENING_CLASSES}
    for row in rows:
        cid = str(row.get('id') or '')
        if not cid:
            continue
        if str(row.get('lifecycle') or 'active') not in ('active', ''):
            continue
        title = str(row.get('title') or '').strip()
        claim = str(row.get('claim') or row.get('body') or '').strip()
        if len(title) < 4:
            if len(claim) < 8:
                continue
            row = dict(row, title=_quote_sentence(claim, 16))
        since = _dt(asked.get(cid))
        if since and (now - since).total_seconds() < OPENING_COOLDOWN_DAYS * 86400:
            continue
        if cid == last_id:
            continue
        rel = 0
        if terms:
            text = title + ' ' + claim
            rel = sum(1 for t in terms if t in text)
            if not rel:
                continue
        row['_rel'] = rel
        updated_at = _dt(row.get('updated_at')) or _dt(row.get('created_at')) or now
        created_at = _dt(row.get('created_at')) or updated_at
        deg = int(degree.get(cid) or 0)
        row['_degree'] = deg
        row['_updated'] = updated_at
        if (now - created_at).total_seconds() <= 86400:
            pools['fresh'].append(row)
        if deg == 0:
            pools['orphan'].append(row)
        else:
            pools['stale'].append(row)
    if terms:
        # 任务触发：相关度优先，其次按原来的时间口径。
        for name in OPENING_CLASSES:
            pools[name].sort(key=lambda r: (r['_rel'], r['_updated']), reverse=True)
    else:
        pools['fresh'].sort(key=lambda r: r['_updated'], reverse=True)
        pools['orphan'].sort(key=lambda r: r['_updated'])
        pools['stale'].sort(key=lambda r: r['_updated'])
    last_class = str(state.get('last_class') or '')
    start = OPENING_CLASSES.index(last_class) + 1 if last_class in OPENING_CLASSES else 0
    for step in range(len(OPENING_CLASSES)):
        cls = OPENING_CLASSES[(start + step) % len(OPENING_CLASSES)]
        for card in pools[cls]:
            text = _opening_question(card, cls, card['_degree'], now)
            if text:
                return {'text': text, 'card': card, 'class': cls, 'degree': card['_degree'],
                        'asked_before': str(asked.get(card.get('id')) or ''), 'asked': asked,
                        'relevance': card.get('_rel') or 0, 'task': task}
    return {}


def _opening_reason(pick):
    if not pick:
        return ''
    return '%s · 图谱关联 %d · 上次被问 %s · 冷却 %d 天内不再问' % (
        OPENING_CLASS_LABEL.get(pick['class'], pick['class']), pick['degree'],
        pick['asked_before'] or '没被问过', OPENING_COOLDOWN_DAYS)


def _opening_brief(pick):
    """界面上开场句下面的那句说明：和“为什么是这张”同一套口径，只是更短。"""
    if not pick:
        return ''
    if pick['class'] == 'fresh':
        head = '最近 24 小时新增 · 刚记下'
    elif pick['class'] == 'orphan':
        head = '孤立卡 · 没人连过'
    else:
        updated = pick['card'].get('_updated')
        days = max(0, (datetime.now(timezone.utc) - updated).days) if updated else 0
        head = ('长期没改写 · %d 天没动' % days) if days >= 2 else '长期没改写 · 一直没动'
    return '%s · %d 天内不再问' % (head, OPENING_COOLDOWN_DAYS)


def _opening_pick(store, prev, history, task=''):
    """返回 (开场句, 选题说明)。没有可选卡时两者都空/带原因，不抛错。

    2026-11-16（第二次）：新会话必给开场，问句和上次完全相同也给；
    “同题抑制”只在同一个会话内的连续追问时生效。
    新会话的判定依据：history 为空——前端新建 / 清空对话后的第一问不带历史，
    同一会话里的后续追问会带上前面几轮，判定结论和依据都写进日志。
    2026-11-17（L7 M3）：带 task 时按任务挑相关旧卡，开场即「过去对质」的入口。
    """
    if history:
        applog.info('brain.opening', 'same session, opening not repeated',
                    session='continue', history_len=len(history))
        return '', {}
    pick = _pick_opening(store, prev, task=task)
    if not pick:
        info = {'card_id': '', 'class': '', 'class_label': '', 'degree': 0,
                'last_asked': '', 'cooldown_days': OPENING_COOLDOWN_DAYS, 'task': task,
                'reason': ('任务里没有贴得上的旧卡（%s）' % task) if task else
                          ('库里没有可选卡（空库，或候选都在 %d 天冷却期内）' % OPENING_COOLDOWN_DAYS)}
        try:
            applog.info('brain.opening', 'no card available', task=task or 'none')
        except Exception:
            pass
        return '', info
    stamp = datetime.now(TZ).isoformat(timespec='seconds')
    try:
        asked = dict(pick['asked'])
        asked[pick['card']['id']] = stamp
        if len(asked) > 200:
            for key in list(asked)[:len(asked) - 200]:
                asked.pop(key, None)
        state = {'last_class': pick['class'], 'asked': asked, 'updated_at': stamp}
        if prev.get('id'):
            store.patch('brain_session', prev['id'], {'opening_state': state})
        else:
            store.put('brain_session', {'opening_state': state}, 'main')
    except Exception:
        pass
    reason = _opening_reason(pick)
    if task:
        reason = '%s · 任务相关度 %d · 任务「%s」' % (reason, pick.get('relevance') or 0, task[:24])
    info = {'card_id': pick['card'].get('id'), 'title': pick['card'].get('title') or '',
            'class': pick['class'], 'class_label': OPENING_CLASS_LABEL.get(pick['class'], ''),
            'degree': pick['degree'], 'last_asked': pick['asked_before'],
            'cooldown_days': OPENING_COOLDOWN_DAYS, 'reason': reason, 'task': task,
            'relevance': pick.get('relevance') or 0,
            'brief': _opening_brief(pick)}
    try:
        applog.info('brain.opening', info['reason'], card_id=info['card_id'], cls=info['class'],
                    degree=info['degree'], last_asked=info['last_asked'] or 'none',
                    session='new', history_len=0)
    except Exception:
        pass
    return pick['text'], info


def _quiet(text):
    value = str(text or '').replace('！', '。').replace('!', '。')
    for bad in ('太棒了', '非常好的问题', '作为 AI', '作为AI', '希望对你有帮助'):
        value = value.replace(bad, '')
    # 模型偶尔带 markdown 粗体，界面是纯文本，去掉标记免得露出星号、也避免引文比对失败
    value = value.replace('**', '').replace('__', '')
    return value.strip()


def _one_question(text):
    value = str(text or '').strip()
    marks = [i for i, ch in enumerate(value) if ch in '？?']
    if len(marks) > 1:
        return value[:marks[0] + 1]
    return value


def _flat(text):
    return re.sub(r'[“”「」"\']', '', str(text or ''))


def _drop_causal(text):
    """弱相关卡不能当证据：把“正好印证/恰好说明”这类因果句整句去掉。"""
    bad = ('印证', '恰好说明', '正好说明', '正说明', '完全一致', '不谋而合')
    pieces = [p for p in re.split(r'(?<=[。？?！!])', str(text or '')) if p.strip()]
    return ''.join(p for p in pieces if not any(b in p for b in bad)).strip()


def _coach_answer(text, question_text, action, quote=''):
    """把模型输出收成教练口径：引文由程序统一加一次；一次只留一个问句。"""
    value = _quiet(text)
    # 开场不复读进回答（模型偶尔仍会写），交给界面 .bc-opening 单独一行
    value = re.sub(r'^上次你(?:在收|在聊)[^？?]*[？?]\s*', '', value).strip()
    # 引文统一由程序按卡原文加：模型自己写的引用句整句丢掉，避免同一张卡引两遍
    mark = _flat(quote)[:10]
    flat_quote = _flat(quote)
    pieces = [p for p in re.split(r'(?<=[。？?！!])', value) if p.strip()]
    kept = []
    for piece in pieces:
        flat_piece = _flat(piece).strip('。！？?! ')
        if '记过一句' in piece:
            continue
        if mark and mark in flat_piece:
            continue
        # 模型把引文后半句拆出来又说了一遍：凡是引文里的句子，都视为重复
        if flat_piece and flat_piece in flat_quote:
            continue
        kept.append(piece.strip())
    value = ''.join(kept).strip()
    # 删掉模型引用句后可能留下孤立的右引号/多余空格
    value = re.sub(r'(^|\s)[”」』](?=\S)', r'\1', value)
    value = re.sub(r'(?<=[。！？，、；：])[ \t]+', '', value)
    pieces = [p for p in re.split(r'(?<=[。？?！!])', value) if p.strip()]
    out, asked = [], False
    for piece in pieces:
        piece = piece.strip()
        if '？' in piece or '?' in piece:
            if asked:
                # 已经有反问：动作句交给底部动作行，其余问句降为陈述
                if piece.startswith('要不要'):
                    continue
                piece = piece.replace('？', '。').replace('?', '。')
            else:
                asked = True
        out.append(piece)
    value = ''.join(out).strip()
    if not asked and action:
        value = value.rstrip('。') + '。' + action
    return value


# ---------------------------------------------------------------- 主入口
# ---------------------------------------------------------------------------
# 2026-11-17 t105：第二大脑分级省 token——调云前先查预算；预算耗尽回退本地/如实提示。
# 契约（t106）：GET /api/brain/budget -> {limit, used_today, remaining, enabled}
# 网络失败按「允许但记录」降级，不阻塞用户。
_BUDGET_CACHE = {}
BUDGET_TTL = 30  # 秒：短缓存，避免每问都多打一次云


def _budget_contract(raw):
    """t106 契约（2026-11-17 最终：按 token）：{limit, used_today, remaining, enabled}，
    limit 默认 200000。金额字段（limit_yuan/remaining_yuan）只做兼容兜底，不硬依赖。"""
    if not isinstance(raw, dict):
        return {'status': 'unknown'}
    out = {'status': 'ok', 'unit': 'tokens'}
    for key in ('limit', 'used_today', 'remaining'):
        try:
            out[key] = int(raw.get(key))
        except (TypeError, ValueError):
            out[key] = None
    if out.get('limit') is None:
        out['limit'] = 200000
    for key in ('limit_yuan', 'used_yuan', 'remaining_yuan'):
        try:
            out[key] = float(raw.get(key))
        except (TypeError, ValueError):
            out[key] = None
    out['enabled'] = raw.get('enabled') is not False
    if out.get('remaining') is None and out.get('remaining_yuan') is not None:
        out['unit'] = 'yuan'
    return out


def _fmt_tokens(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ''
    if n >= 10000:
        text = ('%.1f' % (n / 10000.0)).rstrip('0').rstrip('.')
        return text + '万token'
    return '%d token' % int(n)


def _budget_note(budget):
    if not budget:
        return ''
    if budget.get('status') == 'ok':
        if budget.get('remaining') is not None and budget.get('unit') != 'yuan':
            return '今日云端额度剩余 %s（每天 %s）' % (_fmt_tokens(budget['remaining']),
                                                       _fmt_tokens(budget.get('limit') or 200000))
        if budget.get('remaining_yuan') is not None:
            limit = budget.get('limit_yuan')
            tail = ('（每天 ¥%.2f）' % limit) if limit is not None else ''
            return '今日云端预算剩余 ¥%.2f%s' % (budget['remaining_yuan'], tail)
        return '今日云端额度剩余未知'
    if budget.get('status') == 'unknown':
        return '云端额度状态暂时未知，本次按可用处理'
    return ''


def brain_budget(store, use_cache=True):
    """读云端今日预算；登录缺失/网络失败都返回 status=unknown（调用方按允许处理）。"""
    try:
        row = auth_service._latest_session(store)
    except Exception:
        row = None
    token = str((row or {}).get('cloud_token') or '')
    if not token:
        return {'status': 'login_required'}
    key = token[-12:]
    now_ts = time.time()
    if use_cache:
        cached = _BUDGET_CACHE.get(key)
        if cached and now_ts - cached[0] < BUDGET_TTL:
            return dict(cached[1])
    try:
        data, _ = auth_service._cloud('GET', '/api/brain/budget', None, token)
        budget = _budget_contract(data)
    except Exception as exc:  # 网络失败：允许但记录
        budget = {'status': 'unknown', 'error': str(exc)[:120]}
    _BUDGET_CACHE[key] = (now_ts, dict(budget))
    return budget


def _budget_exhausted(budget):
    if not budget or budget.get('status') != 'ok':
        return False
    if budget.get('enabled') is False:
        return True
    if budget.get('remaining_yuan') is not None:
        return budget['remaining_yuan'] <= 0
    return budget.get('remaining') is not None and budget['remaining'] <= 0


def ask(store, question, history=None, limit=8, task=''):
    """第二大脑问答主入口。

    2026-11-17（L7）：多一个可选 task（任务触发，给「过去对质」用）；
    教练模式（M6，默认关）在本地直答算完之后分流，行为见 brain_coach.turn。
    助手模式的行为保持 2026-11-16 定下的口径不变。
    """
    started = time.perf_counter()
    q = str(question or '').strip()
    # 2026-11-19 t153：聊天语气选择（隐藏 tag，不显示在对话里）；持久化到 brain_session。
    tone_match = re.match(r'^\[\[tone:(mine|direct|friend|mentor)\]\]\s*', q)
    if tone_match:
        try:
            brain_coach.set_tone(store, tone_match.group(1))
        except Exception:
            pass
        q = q[tone_match.end():].strip()
    # 2026-11-19 t166：确认开封时间胶囊（聊天卡按钮发送；对/部分/错）。
    capsule_open = re.match(r'^\[\[capsule:open\|([^|\]]+)\|(right|partial|wrong)(?:\|([^\]]*))?\]\]$', q)
    if capsule_open:
        try:
            done = growth_service.open_capsule(store, capsule_open.group(1), capsule_open.group(2),
                                               capsule_open.group(3) or '')
            stats = growth_service.capsule_stats(store)
            text = '记下了：这颗胶囊校准为「%s」。%s' % (
                {'right': '对', 'partial': '部分对', 'wrong': '错'}[capsule_open.group(2)], stats['note'])
            result = _finish(store, q, text, 'capsule', 'capsule', [], started,
                             card=None, opening='', opening_pick=None,
                             status='', index_source='', hit_count=0)
            result['mode'] = 'capsule'
            result['capsule'] = done
            result['capsule_stats'] = stats
            result['source_label'] = '知识库 · 时间胶囊（已校准）'
            _save_session(store, q, None)
            return result
        except Exception as exc:
            return {'ok': False, 'message': '这次没开封成：%s' % str(exc)[:120]}
    # 2026-11-19 t162：确认修订（隐藏命令由聊天卡按钮发送；不自动改）。
    revise_match = re.match(r'^\[\[revise:([^|\]]+)(?:\|([^\]]*))?\]\]\s*$', q)
    if revise_match:
        old_id, new_id = revise_match.group(1), (revise_match.group(2) or '')
        try:
            done = brain_coach.apply_revision(store, old_id, new_id)
            text = ('已修订：旧判断标为「已修订 / 被新判断取代」，原文保留；现在以新判断为准。'
                    '旧卡置信降为「只是猜想」。')
            result = _finish(store, q, text, 'revision', 'revision', [], started,
                             card=None, opening='', opening_pick=None,
                             status='', index_source='', hit_count=0)
            result['mode'] = 'revision'
            result['revision'] = {'old_id': done.get('old_id'), 'new_id': done.get('new_id')}
            result['source_label'] = '知识库 · 信念修订（已确认）'
            _save_session(store, q, None)
            return result
        except Exception as exc:
            return {'ok': False, 'message': '这次没修订成：%s' % str(exc)[:120]}
    if not q:
        return {'ok': False, 'message': '想问我什么？'}
    intent, kinds = _intent(q)
    prev = _session(store)
    opening, opening_pick = _opening_pick(store, prev, history, task=task)
    # 2026-11-19：问答日志也进索引；多取一些再过滤，避免「问过的问题」把知识卡挤出前 12。
    found = content_index.search(store, q, limit=max(limit, 40), include_body=True)
    # 「问过的问题」是问答自身的日志，拿它当依据会形成自指循环，这里只给历史参考不用。
    items = [x for x in (found.get('items') or []) if x.get('source_kind') != 'question']
    focused = [x for x in items if x.get('source_kind') in kinds] if kinds else items
    focus = _focus_card(store, items, q)
    status = '' if focus else '库里没有检索到可引用的卡，这次只按记录回答。'

    answer, used, source = '', [], 'none'
    if intent == 'checkin':
        answer, used = _answer_checkin(store, q)
    elif intent == 'artifact':
        answer, used = _answer_artifact(store, q)
    elif intent == 'topic':
        answer, used = _answer_topic(store, q)
    elif intent == 'knowledge':
        answer, used = _answer_knowledge(focused or items)
    elif intent == 'profile':
        answer, used = _answer_profile(store, focused or items)

    if answer:
        source = 'local'
    elif _strong_factual(q, focused or items):
        answer = _quote_answer(focused or items)
        if answer:
            used = (focused or items)[:5]
            source = 'local'

    refs = _refs(used or focused or items, 8)
    # 2026-11-19 t157：被第二大脑引用过的认知记一次使用，供检索加权（不改原文）。
    try:
        kids = [str(r.get('id')) for r in refs if r.get('kind') == 'knowledge' or str(r.get('id','')).startswith('knowledge_')]
        if kids:
            content_index.mark_used(store, kids)
            if len(set(kids)) >= 2:
                content_index.record_cooccurrence(store, kids[:6])
    except Exception:
        pass
    news = _news_refs(store, q)

    def _coach_decor(res):
        """助手模式：带上当前模式与「该不该邀请开教练」，不改回答本身。"""
        res['coach_mode'] = brain_coach.mode(store)
        note = brain_coach.assistant_note(store)
        if note:
            res['coach_invite'] = note
        return res

    # 2026-11-17 t88：元问题（你是谁/跟其他 AI 区别/你能做什么/你怎么教练）不是「库里没记录」，
    # 用轻教练口径直接接住：诚实答＋一句轻问回作品；明确要答案就直给。不 deflect、不调模型。
    if brain_coach.is_meta_question(q):
        result = _finish(store, q, brain_coach.meta_answer(q), 'meta', 'meta', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-19 t160：事前验尸/决策权衡（本地、只读客户记录）。
    if brain_coach.is_forecast(q):
        data = brain_coach.forecast(store, q)
        result = _finish(store, q, data['text'], 'forecast', 'forecast', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'forecast'
        result['source_label'] = data.get('source_label') or '知识库 · 事前验尸'
        result['forecast'] = {'scenarios': data.get('scenarios') or [], 'evidence': data.get('evidence') or ''}
        _save_session(store, q, None)
        return _coach_decor(result)
    if brain_coach.is_decision(q):
        data = brain_coach.decision(store, q)
        result = _finish(store, q, data['text'], 'decision', 'decision', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'decision'
        result['source_label'] = data.get('source_label') or '知识库 · 决策权衡'
        result['decision'] = {'options': data.get('options') or [], 'ranked': data.get('ranked') or [],
                              'values': data.get('values') or []}
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-19 t167：今日晨报（≤4 条；无内容不硬凑）。
    if '晨报' in q:
        brief = growth_service.morning_brief(store)
        result = _finish(store, q, brief['text'], 'morning', 'morning', [], started,
                         card=None, opening='', opening_pick=None,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'morning'
        result['morning'] = brief
        result['source_label'] = '知识库 · 今日晨报（昨日/盲点/里程碑/主线）'
        _save_session(store, q, None)
        return result

    # 2026-11-19 t166：时间胶囊（封存/列表；到期才提示）。
    if brain_coach.is_capsule_question(q):
        data = brain_coach.capsule_answer(store, q)
        result = _finish(store, q, data['text'], 'capsule', 'capsule', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = data.get('mode') or 'capsule'
        result['capsules'] = data.get('capsules') or []
        result['capsule_stats'] = data.get('stats') or growth_service.capsule_stats(store)
        result['source_label'] = '知识库 · 时间胶囊（只按你的封存）'
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-19 t162：信念修订候选（只问，不自动改）。
    if brain_coach.is_revision_question(q):
        data = brain_coach.revision_answer(store)
        result = _finish(store, q, data['text'], 'revision', 'revision', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'revision'
        result['revisions'] = data.get('revisions') or []
        result['source_label'] = data.get('source_label') or '知识库 · 信念修订'
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-19 t159：盲点对照——只按目标/分类的真实缺口追问。
    if brain_coach.is_blind_spot(q):
        result_data = brain_coach.blind_spot_answer(store)
        result = _finish(store, q, result_data['text'], 'blind', 'blind', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'blind'
        result['source_label'] = result_data.get('source_label') or '知识库 · 盲点对照'
        result['blind_spots'] = result_data.get('blind_spots') or []
        result['blind_facts'] = result_data.get('facts') or {}
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-19 t148：头脑风暴模式（本地、免费）——四镜头方向＋证据；一键转选题/目标。
    conv = brain_coach.convert_request(store, q)
    if conv:
        result = _finish(store, q, conv['text'], 'convert', 'convert', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'convert'
        result['source_label'] = '知识库 · 已转成' + ('选题' if conv.get('kind') == 'topic' else '目标')
        result['convert'] = {'ok': bool(conv.get('ok')), 'kind': conv.get('kind') or '',
                             'topic': conv.get('topic'), 'goal': conv.get('goal')}
        _save_session(store, q, None)
        return _coach_decor(result)
    if brain_coach.is_brainstorm(q):
        bs = brain_coach.brainstorm(store, q)
        result = _finish(store, q, bs['text'], 'brainstorm', 'brainstorm', [], started,
                         card=None, opening=opening, opening_pick=opening_pick,
                         status='', index_source='', hit_count=0)
        result['mode'] = 'brainstorm'
        result['source_label'] = '知识库 · 头脑风暴（你的记录＋四个镜头）'
        result['brainstorm'] = bs.get('brainstorm')
        result['brainstorm_theme'] = bs.get('theme') or ''
        _save_session(store, q, None)
        return _coach_decor(result)

    # 2026-11-17 t96：通用知识问题（非本人记录、非元问题）——直接给模型通用回答，
    # 不以脚手架反问代替答案；教练模式也绕过 scaffold 先给答案。来源标「AI 通用回答·非你的记录」。
    def _strong_owner_material():
        if isinstance(focus, dict) and not focus.get('weak') and (focus.get('quote') or focus.get('claim')):
            return True
        for it in (focused or items):
            if it.get('source_kind') != 'knowledge':
                continue
            try:
                if float(it.get('score') or 0) >= 30 and (it.get('body') or it.get('snippet')):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    # 通用问题：没有本地直答、没有「够贴的本人聚焦卡」、不是要草稿/要事实的问法。
    no_owner_focus = (not isinstance(focus, dict)) or bool(focus.get('weak')) or not focus
    general_question = (not answer and intent in ('', 'general') and no_owner_focus
                        and not _strong_owner_material()
                        and not brain_coach._wants_draft(q)
                        and not any(word in q for word in FACT_WORDS))

    # L7 M2/M6：教练模式（本人同意过才开）——四档分支全部本地判、本地渲染，不新增模型调用。
    if (not general_question) and brain_coach.mode(store) == brain_coach.MODE_COACH:
        decision = brain_coach.turn(store, q, intent=intent, local_answer=answer,
                                    items=focused or items, focus=focus, status=status)
        coach_answer = decision.get('text') or ''
        if not coach_answer:
            coach_answer = '这次没接住。你换个说法，或者把相关的那条内容拿出来。'
        try:
            applog.info('brain.coach.turn', decision.get('branch_label') or '',
                        action=decision.get('action'), intent=intent,
                        guard_ok=(decision.get('guard') or {}).get('ok'),
                        hits=(decision.get('guard') or {}).get('hits'))
        except Exception:
            pass
        result = _finish(store, q, coach_answer, 'coach', intent, refs + news, started,
                         card=focus, opening=opening, opening_pick=opening_pick, status=status,
                         index_source=found.get('source'), hit_count=len(items),
                         coach=decision)
        _save_session(store, q, focus)
        return result

    # 模型没配置：保留原来的本地直答与引用兜底，不假装。
    if not providers.available()['llm']:
        if general_question:
            result = _finish(store, q,
                             '这条是通用知识问题，不是从你的记录里能查到的；本机生成模型还没配置，暂时答不了。'
                             '配置模型 Key 或登录云端后，我会直接给通用答案，并标明「AI 通用回答·非你的记录」。',
                             'retrieval_only', intent, [], started,
                             card=None, opening=opening, opening_pick=opening_pick,
                             status='生成模型未配置，通用问题暂时无法直接回答。',
                             index_source=found.get('source'), hit_count=0)
            _save_session(store, q, None)
            return _coach_decor(result)
        if answer:
            result = _finish(store, q, answer, 'local', intent, refs, started,
                             card=focus, opening=opening, opening_pick=opening_pick, status=status,
                             index_source=found.get('source'), hit_count=len(items))
        elif items:
            fallback = _quote_answer(items)
            result = _finish(store, q,
                             fallback or '找到了这些相关记录，但生成模型还没配置，先看下面的来源。',
                             'retrieval_only', intent, refs + news, started,
                             card=focus, opening=opening, opening_pick=opening_pick, status=status,
                             index_source=found.get('source'), hit_count=len(items))
        else:
            result = _finish(store, q,
                             '本地没有找到相关记录，生成模型也还没配置。可以去设置里填大模型 Key，或者换个说法再问。',
                             'retrieval_only', intent, [], started,
                             card=None, opening=opening, opening_pick=opening_pick,
                             status='库里没有检索到可引用的卡。',
                             index_source=found.get('source'), hit_count=0)
        _save_session(store, q, focus)
        return _coach_decor(result)

    # 2026-11-17 t102：本地直答免费——只读本人记录、不调模型、不扣额度；
    # 在计费闸之前直接返回（修 t100 上报的「0 额度连本地答案一起被拦」）。
    # 模型路径（general/AI 补充/教练需模型）仍走 start_brain_ask，charge_error 才拦。
    if answer and source == 'local':
        result = _finish(store, q, answer, 'local', intent, refs, started,
                         card=focus, opening=opening, opening_pick=opening_pick,
                         status=status, index_source=found.get('source'),
                         hit_count=len(items))
        _save_session(store, q, focus)
        return _coach_decor(result)

    # 2026-11-17 t105：调云前查预算；预算耗尽/关闭则 0 模型调用，回退本地或如实提示。
    budget = brain_budget(store)
    if _budget_exhausted(budget):
        if budget.get('enabled') is False:
            reason = '云端问答当前已关闭'
        else:
            limit = budget.get('limit') or 200000
            reason = '今日云端额度(%s)已用完' % _fmt_tokens(limit)
        result = _finish(store, q,
                         '%s，先用本地记录。直接问你的记录仍然免费，比如「我有哪些认知？」'
                         '「我最独特的观点是什么？」；通用/写作类等预算恢复后再问，我不会用假答案凑数。'
                         % reason,
                         'budget', intent, refs, started,
                         card=focus, opening=opening, opening_pick=opening_pick,
                         status='')
        _save_session(store, q, focus)
        return _coach_decor(result)

    import billing_route as _billing
    ask_job, charge_error = _billing.start_brain_ask()
    if charge_error:
        return charge_error
    payload_refs = [{'id': r['id'], 'kind': r['source_kind'], 'kind_label': r['kind_label'],
                     'title': r['title'], 'claim': r['claim'], 'boundary': r.get('boundary', ''),
                     'source': r.get('source', '')} for r in refs + news]
    # 2026-11-16：控成本——历史只带最近 6 条、每条截 600 字（原 10 条 × 1500 字）。
    kept = [{'role': str(h.get('role', ''))[:10], 'text': str(h.get('text', ''))[:600]}
            for h in (history or []) if isinstance(h, dict)][-6:]
    try:
        analysis = providers.llm_json(
            prompt_store.get('brain.coach'),
            # t34 缓存：本人稳定段（档案/命题/口吻/已验证积累）由 providers 前置跨功能共享；
            # t2 补刀：稳定段再并入 persona（信念×口吻×主线×命题）→ 问答也吃「更像我」的
            # 稳定前缀，且与选题/写稿/复盘字节同一前缀（跨功能 warming）。
            # payload 历史正序在前、focus/引用/本地事实次之，本轮问题与 last 严格最后——
            # 上一轮请求就是这一轮的前缀；last 每轮都变，放最前会击穿整个 payload 前缀。
            {'history': kept, 'focus_card': focus or {},
             'references': payload_refs, 'local_fact': answer,
             'general_question': bool(general_question),
             'general_rule': ('这是通用知识/常识问题：资料里没有是正常的，不要拒答、不要以反问代替回答。'
                              '用你自己的通用知识直接回答；界面会自动加「这是通用知识，不是你的记录」标签，'
                              '你不要再重复这句话，也不要自己加网名前缀。回答后可可选一句轻问回到他的作品。')
                             if general_question else '',
             'question': q,
             'last': {'card_title': str(prev.get('last_card_title') or '')[:30],
                      'question': str(prev.get('last_question') or '')[:60]}},
            stable_blocks=creation_service.guidance_stable_blocks(),
            tag='brain_coach')
    except Exception:
        billing_route.finish_brain_ask(ask_job, ok=False)
        raise
    billing_route.finish_brain_ask(ask_job, ok=True)

    raw = analysis if isinstance(analysis, dict) else {}
    detail = _quiet(raw.get('detail'))
    question_text = _one_question(_quiet(raw.get('question')))
    action = _quiet(raw.get('action')) or '要不要把这条存成卡？'
    if not action.startswith('要不要'):
        action = '要不要把这条存成卡？'
    answer = _coach_answer(raw.get('answer'), question_text, action,
                           (focus.get('quote') if focus else '') if not general_question else '')
    if general_question:
        answer = _quiet(raw.get('answer')) or answer
    if focus and not general_question:
        date_md = focus['date'][5:] if len(focus['date']) >= 10 else focus['date']
        quote = focus.get('quote') or ''
        weak = bool(focus.get('weak'))
        if quote:
            if weak:
                # 弱相关如实标注，不把“够不着”说成因果
                answer = _drop_causal(answer)
                answer = ('库里没有很贴的，最近的一张是 %s 记的：%s' % (date_md, quote)
                          + ('\n' + answer if answer else '')).strip()
            else:
                answer = ('你在 %s 记过一句：%s' % (date_md, quote)
                          + ('\n' + answer if answer else '')).strip()
    # L7 M5 work-not-psyche 硬红线：助手模式的模型输出也过一遍出口护栏。
    # 只做减法（命中才整句改成「回到作品」），用户自己的引文整段保护、不改。
    quote_guard = [focus.get('quote')] if (focus and focus.get('quote')) else []
    guarded = brain_coach.guard_text(answer, quoted=quote_guard)
    if not guarded['ok']:
        answer = guarded['text']
    guarded_detail = brain_coach.guard_text(detail, quoted=quote_guard)
    if not guarded_detail['ok']:
        detail = guarded_detail['text']
    if not answer:
        answer = _quote_answer(items) or '这次没有生成可用回答，先看下面的来源记录。'
    if '？' not in answer and '?' not in answer:
        answer = answer.rstrip('。') + '。' + action
    if general_question:
        # 2026-11-17 t124：正文不再前插标签；只剥离模型自带重复，来源说明交给前端小字。
        answer = _strip_general_prefix(answer)
        if brain_coach.mode(store) == brain_coach.MODE_COACH and not brain_coach._asks_for_answer(q) \
                and '？' not in answer and '?' not in answer:
            answer = answer.rstrip('。') + '。　你手上哪条稿子想先用上它？'
    # 2026-11-17 t123：不再把「今日云端额度剩余…」拼进聊天可见文本（payload.budget 内部保留）。
    result = _finish(store, q, answer, 'general' if general_question else 'model', intent, refs + news, started,
                     analysis=raw,
                     charged=0 if billing_route.membership_free() else 1, ask_job=ask_job,
                     card=focus, opening=opening, opening_pick=opening_pick, detail=detail,
                     question_text=question_text, action=action, status=status,
                     index_source=found.get('source'), hit_count=len(items),
                     guard=guarded)
    result['budget'] = budget
    _save_session(store, q, focus)
    return _coach_decor(result)


# 2026-11-17 t117：出口压力——记够多、近期没作品时，回答尾部温和推一把；300s 内只提一次。
_EXIT_LAST = {}
EXIT_TTL = 300


def _exit_nudge(store):
    if os.getenv('IP_BRAIN_EXIT_PRESSURE', '1') == '0':
        return None
    key = str(getattr(store, 'path', '') or 'default')
    now_ts = time.time()
    last = _EXIT_LAST.get(key)
    if last and now_ts - last < EXIT_TTL:
        return None
    try:
        nudge = brain_coach.exit_pressure(store)
    except Exception:
        return None
    if nudge:
        _EXIT_LAST[key] = now_ts
    return nudge


# 2026-11-17 t118：主动记忆——回答尾部递上旧判断/变化（600s 一次，IP_BRAIN_PROACTIVE=0 关）。
_PROACTIVE_LAST = {}
PROACTIVE_TTL = 600


def _proactive_note(store, question, answer=''):
    if os.getenv('IP_BRAIN_PROACTIVE', '1') == '0':
        return None
    key = str(getattr(store, 'path', '') or 'default')
    now_ts = time.time()
    last = _PROACTIVE_LAST.get(key)
    if last and now_ts - last < PROACTIVE_TTL:
        return None
    try:
        note = brain_coach.recall_old(store, question, answer)
    except Exception:
        return None
    if note:
        _PROACTIVE_LAST[key] = now_ts
    return note


# 2026-11-17 t123：通用回答的友好标签（网名优先，取不到用「第二大脑」），只出现一次。
_GENERAL_PREFIX_RE = re.compile(
    r'^\s*(?:[^\s：:\n]{1,12}[：:]\s*)?'
    r'(?:(?:这是)?通用知识[，,、]?\s*(?:不是你的记录)?[。！!]?\s*'
    r'|不是你的记录[。！!]?\s*)+'
)


def _strip_general_prefix(text):
    """剥掉模型自己重复输出的「这是通用知识，不是你的记录」及前缀网名。"""
    value = str(text or '').strip()
    for _ in range(3):
        cleaned = _GENERAL_PREFIX_RE.sub('', value).strip()
        if cleaned == value:
            break
        value = cleaned
    return value


def _attention_note(store, refs):
    """2026-11-19 t161：按当前主线提示这次先看哪张卡；无主线/无卡不硬造。"""
    try:
        context = content_index.mainline_context(store)
        if not context:
            return None
        card = next((r for r in (refs or []) if r.get('kind') == 'knowledge' and r.get('title')), None)
        if not card:
            return None
        return {'mainline': context.get('title') or '我的主线',
                'card': card.get('title') or '',
                'text': '按你当前主线「%s」，这次先看这张：「%s」。其余不相关的先降权，不挤你。'
                        % (context.get('title') or '我的主线', card.get('title') or '')}
    except Exception:
        return None


_CAPSULE_SEEN = {}


def _capsule_due(store):
    """t166：到期主动浮现；同一天同一颗只提一次，未到期不打扰。"""
    try:
        note = brain_coach.capsule_due_note(store)
    except Exception:
        note = None
    if not note:
        return None
    key = (str(getattr(store, 'path', '') or ''), note.get('id'), __import__('datetime').date.today().isoformat())
    if key in _CAPSULE_SEEN:
        return None
    _CAPSULE_SEEN[key] = True
    return note


def _finish(store, question, answer, source, intent, refs, started, analysis=None,
            charged=0, ask_job=None, index_source='', hit_count=0,
            card=None, opening='', detail='', question_text='', action='', status='',
            opening_pick=None, coach=None, guard=None):
    # 'coach' 是 2026-11-17 L7 新增的来源：教练模式的回答不是「你的记录」也不是「AI补充」。
    label = {'local': LOCAL_LABEL, 'model': MODEL_LABEL, 'coach': '教练',
             'meta': '知识库', 'general': 'AI 通用回答·非你的记录',
             'budget': '额度用完·先用本地记录'}.get(source, EMPTY_LABEL)
    mode = {'local': 'local', 'model': 'model', 'coach': 'coach',
            'meta': 'meta', 'general': 'general',
            'budget': 'budget'}.get(source, 'retrieval_only')
    answer = str(answer or '').replace('**', '').replace('__', '')
    # 2026-11-19 t153：模型/通用回答先去 AI 套话、拆短句；本地引文保持原样。
    if source in ('model', 'general') and answer:
        answer = brain_coach.humanize(answer, store)
    payload = analysis if isinstance(analysis, dict) else {}
    payload = {'answer': answer, 'missing_info': list(payload.get('missing_info') or []),
               'detail': detail, 'question': question_text, 'action': action}
    hits = []
    if isinstance(guard, dict):
        hits = list(guard.get('hits') or [])
    elif isinstance(coach, dict) and coach.get('guard'):
        hits = list(coach['guard'].get('hits') or [])
    if hits:
        payload['guard'] = {'ok': bool((guard or coach['guard']).get('ok')), 'hits': hits}
    balance = None
    try:
        balance = billing_route.service().balance()['balance_credits']
    except Exception:
        balance = None
    record = None
    try:
        record = store.put('question', {'question': question, 'references': [r['id'] for r in refs],
                                        'analysis': payload, 'answer_source': source,
                                        'intent': intent, 'history_turns': 0})
    except Exception:
        record = None
    # 2026-11-19 t161：按主线注意聚焦——只对本地/模型/通用回答提示，不打断专用分支。
    attention = None
    if source in ('local', 'model', 'general'):
        attention = _attention_note(store, refs)
        if attention and answer:
            answer = answer.rstrip('。') + '。　' + attention['text']
            payload['answer'] = answer
    # 2026-11-19 t166：到期胶囊主动浮现（未到期不打扰）。
    capsule_due = None
    if source in ('local', 'model', 'general'):
        capsule_due = _capsule_due(store)
        if capsule_due and answer:
            answer = answer.rstrip('。') + '。　' + capsule_due['note']
            payload['answer'] = answer
    # 2026-11-17 t117：出口压力挂回答尾部（不打断原回答；无候选/有近期作品不出现）。
    exit_nudge = _exit_nudge(store)
    if exit_nudge:
        if answer:
            answer = answer.rstrip('。') + '。　' + exit_nudge['text']
        else:
            answer = exit_nudge['text']
        payload['answer'] = answer
    # 2026-11-17 t118：主动浮现旧判断/变化（本地检索，不调模型）。
    proactive = _proactive_note(store, question, answer)
    if proactive:
        if answer:
            answer = answer.rstrip('。') + '。　' + proactive['text']
        else:
            answer = proactive['text']
        payload['answer'] = answer
    return {'ok': True,
            'attention': attention or None,
            'capsule_due': capsule_due or None,
            'exit_pressure': exit_nudge or None,
            'proactive_memory': proactive or None,
            'record_id': (record or {}).get('id', ''),
            'question': question,
            'answer': answer,
            'analysis': payload,
            'answer_source': source,
            'source_label': label,
            'intent': intent,
            'references': refs,
            'mode': mode,
            'card': card,
            'pick_reason': (card or {}).get('reason', ''),
            'opening': opening,
            'opening_pick': opening_pick or {},
            'detail': detail,
            'question_text': question_text,
            'action': action,
            'status': status,
            'pillars': PILLARS,
            'index_source': index_source,
            'hit_count': hit_count,
            'ms': int((time.perf_counter() - started) * 1000),
            'charged_credits': charged,
            'balance_credits': balance,
            'coach': coach if isinstance(coach, dict) else {},
            'coach_mode': (coach or {}).get('mode', '') if isinstance(coach, dict) else ''}


# ---------------------------------------------------------------- 教练魂对外入口（给 API 层接线）
# 本次写域只到 brain_ask / brain_coach，app.py 路由不在写域内：
# 下面这几个函数是给 app.py 加 3 行路由就能用的入口，函数本身已可独立调用/复测。
def mirror(store, allow_model=None):
    """M1 镜子：一句「被看见」的洞察（空库不硬造）。"""
    return brain_coach.mirror(store, allow_model=allow_model)


def past(store, task):
    """M3 过去对质：当前任务 vs 他自己久未碰/未验证的旧卡。"""
    return brain_coach.past(store, task)


def task_opening(store, task):
    """M3 任务触发开场：用 _pick_opening 的相关度挑一张旧卡当开场句。"""
    prev = _session(store)
    text, info = _opening_pick(store, prev, [], task=task)
    return {'ok': bool(text), 'opening': text, 'pick': info}


def conviction(claim='', said=None):
    """M4 炼 conviction：一次一步逼问；答全后给一句待他认领的主张。"""
    return brain_coach.conviction(claim, said)


def challenge(store, thesis='', min_evidence=None):
    """M7 最难挑战：表现 N 次不支持 → 提出前提需改（改不改由本人定）。"""
    return brain_coach.challenge(store, thesis, min_evidence=min_evidence)


def coach_state(store):
    """M6 当前模式 / 该不该邀请开教练。"""
    return brain_coach.state(store)


def set_coach_mode(store, mode_name, consent=False):
    """M6 切模式：默认助手；开教练必须本人同意（consent=True）。"""
    return brain_coach.set_mode(store, mode_name, consent)
