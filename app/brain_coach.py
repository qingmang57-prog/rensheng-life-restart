# -*- coding: utf-8 -*-
"""第二大脑教练魂（L7）：七个可施工的机制。

规格：[2026-11-16/第二大脑教练魂_落地设计_20261117.md]（M1–M7）。
一句话：引擎是骨，教练是魂；教练只教「作品＋作品背后的思考」，
不教心理、不辅导情绪、不评判人格（work-not-psyche 硬红线）。

七机制与函数：
  M1 镜子        mirror(store)                  读 content_index 分类分布＋图谱度数 → 集中在X/优势Y/缺Z
  M2 give/ask    classify() / turn()            要事实→直给；有货→问回去；没货→脚手架；要初稿→苏格拉底
  M3 过去对质    past(store, task)              用他自己的旧卡（久未碰/未验证）对质当前任务
  M4 炼 conviction  conviction(claim, said)     逼 specifics → 逼代价 → 逼边界 → 一句「我相信X，我拿Y换来的」
  M5 护栏        guard_text() / psyche_question() 出口过滤心理疏导与人格评判，命中改写成「回到作品」
  M6 权利挣来    mode(store) / set_mode()      默认助手；被邀请并同意才开教练；一上来不苏格拉底
  M7 最难挑战    challenge(store, thesis)      表现 N 次不支持 → 提出「前提需改」，改不改由本人定

边界（写域）：本文件只读 content_index / graph_view / prompt_store / providers / applog；
不碰 growth / feedback / app.js；不改提示词云端，不写云端任何东西。
隐私：内容、主张、表现全部本地读本地算；只有 mirror 在模型已配置时才把聚合后的分布发出去
（不含原文，只含分类计数、最高度数的那句主张、缺哪个分类），不发原始记录。

提示词（2026-11-17 t40 封壳）：**正文只在云端**。本地只留键名/标签 + 一条「未解锁」空壳
（登记进 prompt_store.DEFAULTS 只为让云端热更能按同名键覆盖）。云端没下发时
prompt_text() 抛 PromptUnavailable，教练层静默退场回助手模式，绝不回落本地正文；
active 拿全量 5 条，trial 只有 mirror / coach.ask 两条（深度三条仅 active）。
本模块不主动改云端、不改 prompt_store.py。
"""
import re
import time
import statistics
from datetime import datetime, timezone, timedelta

import applog
import content_index
import growth_service as growth
import prompt_store

TZ = timezone(timedelta(hours=8))
TODAY = lambda: datetime.now(TZ).date()

# ---------------------------------------------------------------- 教练提示词（云端下发，本地只有空壳）
# 2026-11-17（t40）：产品魂不随包。这里只留键名/标签和一条「未解锁」占位壳，
# 正文由云端 prompts 表下发（active 全量；trial 只有 mirror / coach.ask 两条）。
# 壳仍登记进 prompt_store.DEFAULTS：prompt_store 的云端/缓存只认 DEFAULTS 里有的键，
# 登记壳 → 云端热更仍能覆盖；离线/未登录时 get() 拿到的是壳 → 视为未解锁，绝不回头读本地正文。
PROMPT_KEYS = ('brain.mirror', 'brain.coach.ask', 'brain.coach.past',
               'brain.coach.conviction', 'brain.coach.challenge')
PROMPT_LABELS = {
    'brain.mirror': '知识库·镜子',
    'brain.coach.ask': '知识库·教练问回去',
    'brain.coach.past': '知识库·过去对质',
    'brain.coach.conviction': '知识库·炼conviction',
    'brain.coach.challenge': '知识库·最难挑战',
}
DEEP_KEYS = ('brain.coach.past', 'brain.coach.conviction', 'brain.coach.challenge')
PROMPT_SHELL = '【教练提示词未解锁：登录后由云端下发；本地不保存正文】'
LOCK_HINT = ('教练功能要登录后解锁：登录并连上云端，五个机制（镜子 / 问回去 / 过去对质 / 炼主张 / 最难挑战）'
             '按会员权益下发；离线时只做助手，记录和出稿不受影响。')
_REGISTERED = {'done': False}


def register_shells():
    """把空壳登记进提示词中心（setdefault，绝不写正文、不覆盖已有条目）。"""
    if _REGISTERED['done']:
        return
    for key in PROMPT_KEYS:
        try:
            prompt_store.DEFAULTS.setdefault(key, PROMPT_SHELL)
            prompt_store.NAMES.setdefault(key, PROMPT_LABELS[key])
        except Exception:  # noqa: BLE001  点检环境里提示词中心不可用也不能让导入失败
            pass
    _REGISTERED['done'] = True


def _raw_prompt(key):
    try:
        return str(prompt_store.get(key) or '').strip()
    except Exception:  # noqa: BLE001
        return ''


def prompt_ready(key):
    """这条教练提示词是否拿到了云端正文（壳 = 没拿到）。"""
    text = _raw_prompt(key)
    return bool(text) and text != PROMPT_SHELL


def coach_unlocked():
    """基础教练层（镜子 + 问回去）：trial 也有。"""
    return prompt_ready('brain.mirror') and prompt_ready('brain.coach.ask')


def deep_unlocked():
    """深度教练层（过去对质 / 炼主张 / 最难挑战）：仅 active。"""
    return all(prompt_ready(key) for key in DEEP_KEYS)


def lock_hint():
    return LOCK_HINT


class PromptUnavailable(Exception):
    """云端没下发这条教练提示词（离线 / 未登录 / 该层未解锁）。"""

    def __init__(self, key):
        self.key = key
        super().__init__(LOCK_HINT)


def prompt_text(key, **values):
    """只取云端/缓存正文；没有就抛 PromptUnavailable——不再回落任何本地正文。"""
    text = _raw_prompt(key)
    if not text or text == PROMPT_SHELL:
        raise PromptUnavailable(key)
    for name, value in values.items():
        text = text.replace('{{%s}}' % name, '' if value is None else str(value))
    return text


def unavailable(key, what='教练'):
    """统一的未解锁返回：静默退场、页面不崩，带解锁提示（不输出任何方法论）。"""
    try:
        applog.info('brain.coach.locked', 'prompt not available', key=key)
    except Exception:  # noqa: BLE001
        pass
    return {'ok': False, 'unavailable': True, 'status': 403, 'mode': 'locked',
            'prompt_key': key, 'reason': LOCK_HINT, 'text': LOCK_HINT,
            'hint': LOCK_HINT, 'what': what}


register_shells()

# ---------------------------------------------------------------- M5 work-not-psyche 护栏（硬红线）
# 出口过滤词表：心理疏导 / 人格评判。命中就整句丢掉，换「回到作品」。
# 只拦这些明确越界的措辞，不拦作品里的正常词（例如标题里的「焦虑」按引文保护，不动）。
PSYCHE_TERMS = (
    # 人格评判
    '你这个人', '你这种人', '你的性格', '你性格', '你就是个', '你就是那种',
    '你很失败', '你不够努力', '你太懒', '你的问题在于', '你没救',
    # 心理疏导 / 心理定性
    '心理问题', '心理疏导', '心理咨询', '心理医生', '心理治疗', '原生家庭',
    '童年创伤', '疗愈', '抑郁', '焦虑症', '自卑', '缺爱', '自我价值',
    '想开点', '放轻松', '别想太多', '你的情绪', '情绪管理', '你需要被理解',
)

# 用户自己的心理自评：不接疏导，转向作品（M5 验收口径）。
PSYCHE_QUESTION_TERMS = (
    '我是不是很失败', '我是不是失败', '我是不是很没用', '我是不是没用', '我是不是废物',
    '我就是个废物', '我是不是不行', '我是不是没救', '我不行', '我太差', '我没用',
    '我是不是不配', '我不配', '我很失败', '我活不下去', '我不想活了',
    '我是不是有病', '我有心理问题', '我是不是抑郁', '我抑郁',
    '我原生家庭', '我是不是性格', '我这个人是不是',
)

PSYCHE_REDIRECT = '这条我不接：我只教作品和作品背后的思考，不做那类辅导，也不给人下判断。'
WORKS_QUESTION = '回到作品——你最近哪一条内容或哪一个选题，让你自己产生了这个感觉？把那条拿出来，我们看它。'
# 正文里已经有一个问句时，护栏只补陈述句，避免一句话里两个问号（界面口径：一行最多一个问）。
WORKS_STATEMENT = '回到作品——把你最近那条内容拿出来，我们看它。'
# 否定式不算命中：「不是你不够努力」「不是你很失败」是否定句，不能当人格评判拦掉。
# （M7 的批准措辞里就有「也许不是你不够努力」——这里必须让否定句通过。）
NEGATIONS = ('不是', '并不是', '而不是', '不算', '并非')


def _sentences(text):
    """按句切分，保留标点；空串返回空列表。"""
    return [p for p in re.split(r'(?<=[。！？!?\n])', str(text or '')) if p.strip()]


def psyche_hits(text):
    """返回命中的心理疏导/人格评判措辞列表（去重、保序）；否定式不算。"""
    value = str(text or '')
    hits = []
    for term in PSYCHE_TERMS:
        start = 0
        while True:
            index = value.find(term, start)
            if index < 0:
                break
            prefix = value[max(0, index - 4):index]
            if not any(prefix.endswith(neg) for neg in NEGATIONS):
                if term not in hits:
                    hits.append(term)
                break
            start = index + len(term)
    return hits


def psyche_question(question):
    """用户这句话是不是心理自评（要疏导）——是则不接，转向作品。"""
    value = str(question or '')
    return [term for term in PSYCHE_QUESTION_TERMS if term in value]


def guard_text(text, quoted=None):
    """出口护栏：命中词表就丢掉整句，改写为「回到作品」；永不返回空。

    quoted 里的片段是用户自己的原话（引文），整段保护、不参与过滤——
    否则会把他自己的话改掉，还会让引文比对失败。
    """
    raw = str(text or '')
    if not raw.strip():
        return {'ok': True, 'text': '', 'hits': [], 'redirected': False}
    protected = [str(q) for q in (quoted or []) if str(q or '').strip()]
    masked = raw
    marks = []
    for index, quote in enumerate(protected):
        token = '\u0001Q%d\u0001' % index
        if quote and quote in masked:
            masked = masked.replace(quote, token)
            marks.append((token, quote))
    hits = psyche_hits(masked)
    if not hits:
        return {'ok': True, 'text': raw.strip(), 'hits': [], 'redirected': False}
    kept = []
    for sentence in _sentences(masked):
        if psyche_hits(sentence):
            continue
        kept.append(sentence)
    body = ''.join(kept).strip()
    for token, quote in marks:
        body = body.replace(token, quote)
    # 引文被整句丢掉时，把保护标记还原不回来的情况兜住：masked 里还有标记就还原原文
    body = re.sub(r'\u0001Q\d+\u0001', '', body)
    follow = WORKS_STATEMENT if ('？' in body or '?' in body) else WORKS_QUESTION
    if body:
        value = body + PSYCHE_REDIRECT + '　' + follow
    else:
        value = PSYCHE_REDIRECT + '　' + WORKS_QUESTION
    try:
        applog.info('brain.coach.guard', 'work-not-psyche 拦截并改写', hits=hits)
    except Exception:  # noqa: BLE001
        pass
    return {'ok': False, 'text': value.strip(), 'hits': hits, 'redirected': True}


# ---------------------------------------------------------------- 小工具
def _now():
    return datetime.now(timezone.utc)


def _dt(value):
    try:
        text = str(value or '').strip().replace('Z', '+00:00')
        got = datetime.fromisoformat(text)
        return got if got.tzinfo else got.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _md(value, with_year=False):
    got = _dt(value)
    if not got:
        return str(value or '')[:10]
    return got.astimezone(TZ).strftime('%Y-%m-%d' if with_year else '%m-%d')


def _flat(text):
    return ' '.join(str(text or '').replace('**', '').replace('__', '').split())


def _clip(text, limit=40):
    value = _flat(text)
    return value if len(value) <= limit else value[:limit].rstrip() + '…'


def _session(store):
    """brain_session 主记录：开场状态、上次卡、教练开关都落这一条。"""
    try:
        rows = store.list('brain_session', 1)
        return rows[0] if rows else {}
    except Exception:  # noqa: BLE001
        return {}


def _degrees(store):
    """图谱度数；取不到就全按 0（宁可说孤立，不让镜子崩）。"""
    try:
        import graph_view
        cards = graph_view.build(store).get('cards') or []
        return {str(c.get('id')): int(c.get('degree') or 0) for c in cards if c.get('id')}
    except Exception:  # noqa: BLE001
        return {}


def _knowledge_rows(store, limit=400):
    try:
        return content_index.rows_of(store, ('knowledge',), limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _is_active(row):
    return str(row.get('lifecycle') or 'active') in ('active', '')


# ---------------------------------------------------------------- 犀利追问＋人话（t153）
# 硬问题：每句都要逼出可证伪的答案；不要「你觉得呢/你想怎么」。
# 2026-11-19 t153（定义归云端）：六类硬问题/语气规则/元定义主体在云端 `brain.soul`，
# 客户端只留最小兜底（断网/未登录时一个通用追问），不携带完整定义明文。
SOUL_FALLBACK = {
    'version': 0,
    'questions': [{'id': 'fallback', 'type': '通用追问',
                   'text': '具体指什么？举个昨天的例子。'}],
    'fallback_question': '具体指什么？举个昨天的例子。',
    'tone': {'labels': {'mine': '你的语气', 'direct': '更直接', 'friend': '朋友', 'mentor': '导师'},
             'prefixes': {'mine': '', 'direct': '', 'friend': '', 'mentor': ''}},
    'meta': {}, 'guard': {},
}
_SOUL_CACHE = {'at': 0.0, 'data': None}


def soul_config():
    """云端灵魂配置（brain.soul）＋本地缓存；取不到只用最小兜底，不回退全量定义。"""
    now = time.time()
    if _SOUL_CACHE['data'] is not None and now - _SOUL_CACHE['at'] < 60:
        return _SOUL_CACHE['data']
    data = None
    try:
        import json
        import prompt_store
        raw = prompt_store.get('brain.soul')
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get('questions'), list) and parsed['questions']:
            data = parsed
    except Exception:
        data = None
    if data is None:
        data = SOUL_FALLBACK
    _SOUL_CACHE.update(at=now, data=data)
    return data


def hard_questions():
    return list((soul_config() or {}).get('questions') or SOUL_FALLBACK['questions'])


# 供测试/兜底：完整问题库不落本地源码，只有云端下发后由 hard_questions() 取。
HARD_QUESTION_LIST = [q['text'] for q in SOUL_FALLBACK['questions']]
SOFT_PHRASES = ('你觉得呢', '你想怎么', '你怎么看', '有没有想过', '不妨', '可以试着')
AI_PHRASES = ('作为', '总的来说', '综上所述', '让我们', '希望对你有帮助',
              '值得注意的是', '在当今', '随着', '赋能', '闭环', '抓手', '首先', '其次')
TONES = ('mine', 'direct', 'friend', 'mentor')
TONE_LABELS = {'mine': '你的语气', 'direct': '更直接', 'friend': '朋友', 'mentor': '导师'}


def _pick_hard(question, anchor=''):
    q = str(question or '')
    anchor = _clip(anchor or q, 28) or '这件事'
    if any(word in q for word in ('方向', '未来', '迷茫', '不知道', '感觉')):
        kind = 'clarify'
    elif any(word in q for word in ('要不要', '该不该', '必须', '一定', '应该')):
        kind = 'assumption'
    elif any(word in q for word in ('为什么', '凭什么', '真的', '证据')):
        kind = 'evidence'
    elif any(word in q for word in ('别人', '大家', '客户', '观众', '对手')):
        kind = 'perspective'
    elif any(word in q for word in ('风险', '怕', '担心', '放弃', '代价')):
        kind = 'consequence'
    elif any(word in q for word in ('怎么', '如何', '方法', '步骤')):
        kind = 'problem'
    else:
        kind = 'evidence'
    for item in hard_questions():
        if item.get('id') == kind and item.get('text'):
            return str(item['text']).format(anchor=anchor)
    fallback = (soul_config() or {}).get('fallback_question') or SOUL_FALLBACK['fallback_question']
    return str(fallback).format(anchor=anchor)


def _style_sentence_target(store):
    """用本人真实口播语料的平均句长定短句目标；取不到就用 18 字。"""
    try:
        import style_profile
        rows = style_profile.collect_owner_corpus(store)
        lengths = []
        for row in rows or []:
            text = str(row.get('text') or '')
            for sentence in re.split(r'[。！？!?\n]+', text):
                sentence = sentence.strip()
                if 4 <= len(sentence) <= 60:
                    lengths.append(len(sentence))
        if lengths:
            lengths.sort()
            avg = lengths[len(lengths) // 2]
            return max(12, min(24, int(avg)))
    except Exception:
        pass
    return 18


def _shorten(text, target):
    """长句拆短：只在标点处切，不造新事实；保留原词。"""
    out = []
    for sentence in re.split(r'(?<=[。！？!?])', str(text or '')):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= target * 1.8:
            out.append(sentence)
            continue
        parts = re.split(r'(?<=[，、；,;])', sentence)
        buf = ''
        for part in parts:
            if buf and len(buf) + len(part) > target:
                out.append(buf)
                buf = part
            else:
                buf += part
        if buf:
            out.append(buf)
    return ''.join(out)


def humanize(text, store=None):
    """去 AI 套话＋短句口语；用本人语料句长做目标，不改事实与判断。"""
    value = str(text or '')
    value = re.sub(r'作为[^，。！？]{0,24}[，,]\s*', '', value)
    value = re.sub(r'希望[^。！？]*[。！？]?', '', value)
    for phrase in AI_PHRASES:
        value = value.replace(phrase, '')
    replacements = (('因此', '所以'), ('此外', '还有'), ('例如', '比如'), ('进行', '做'),
                    ('需要注意的是', '注意'), ('可以', '能'), ('需要', '得'), ('是否', '是不是'))
    for old, new in replacements:
        value = value.replace(old, new)
    value = re.sub(r'[ \t]{2,}', ' ', value)
    target = _style_sentence_target(store) if store is not None else 18
    value = _shorten(value, target)
    value = re.sub(r'^[，,。；;：:\s]+', '', value)
    value = re.sub(r'[，,]{2,}', '，', value)
    return value.strip()


def _tone_text(text, tone_name):
    """只借语气，不冒充身份：mine=本人句长，direct=去掉缓冲，friend=熟人，mentor=推进。"""
    value = str(text or '').strip()
    tone_name = tone_name if tone_name in TONES else 'mine'
    prefix = (((soul_config() or {}).get('tone') or {}).get('prefixes') or {}).get(tone_name, '')
    if tone_name == 'direct':
        value = re.sub(r'(?<!最)(可能|也许|我觉得|有点|稍微|比较)', '', value)
    return (prefix or '') + value


def _tone(store):
    try:
        value = _coach_state(store).get('tone')
        return value if value in TONES else 'mine'
    except Exception:
        return 'mine'


def set_tone(store, tone_name):
    if tone_name not in TONES:
        return False
    _save_coach_state(store, {'tone': tone_name})
    return True


# ---------------------------------------------------------------- M1 镜子
MIRROR_TEMPLATE = ('集中在：{x}。你最独特的是：{y}。一直没碰的是：{z}。')


def _mirror_facts(store):
    """算镜子三段要用的原料：分类分布、最高度数主张、没碰过的分类。"""
    summary = content_index.summary(store)
    by_category = dict(summary.get('by_category') or {})
    total = int(summary.get('all') or 0)
    rows = [r for r in _knowledge_rows(store) if _is_active(r)]
    degree = _degrees(store)
    ranked = sorted(
        by_category.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    top = [(name, count) for name, count in ranked if count > 0][:2]
    empty = [name for name, count in ranked if count == 0]
    share = int(round(100.0 * top[0][1] / total)) if total and top else 0
    advantages = []
    for row in sorted(rows, key=lambda r: (int(degree.get(str(r.get('id'))) or 0),
                                           int(r.get('links') and len(r.get('links')) or 0),
                                           1 if r.get('validation') == 'personal_case' else 0),
                      reverse=True):
        claim = _flat(row.get('claim') or row.get('body') or '')
        if len(claim) >= 8:
            advantages.append({'claim': _clip(claim, 46), 'title': _clip(row.get('title'), 24),
                               'degree': int(degree.get(str(row.get('id'))) or 0),
                               'recorded_at': str(row.get('recorded_at') or '')[:10]})
        if len(advantages) >= 3:
            break
    return {'summary': summary, 'by_category': by_category, 'total': total,
            'rows': rows, 'degree': degree, 'top': top, 'empty': empty,
            'share': share, 'advantages': advantages}


def _mirror_rule_text(facts):
    top = facts['top']
    if top:
        x = '、'.join('%s（%d 条，占 %d%%）' % (name, count, facts['share'])
                      if index == 0 else '%s（%d 条）' % (name, count)
                      for index, (name, count) in enumerate(top))
    else:
        x = '还没有成型的分类'
    adv = facts['advantages']
    if adv:
        y = '你那句「%s」是别人给不了的（图谱里 %d 条关联，别人没有）' % (adv[0]['claim'], adv[0]['degree'])
    else:
        y = '还没看到能撑住的那一句——先记一条只有你能说的话'
    if facts['empty']:
        z = '「%s」你一条都没碰过' % '」「'.join(facts['empty'][:2])
    elif top:
        low = min((kv for kv in facts['by_category'].items() if kv[1] > 0), key=lambda kv: kv[1])
        z = '「%s」你只碰过 %d 条，一直没往深里走' % (low[0], low[1])
    else:
        z = '还看不出缺口'
    return MIRROR_TEMPLATE.format(x=x, y=y, z=z)


def mirror(store, allow_model=None):
    """M1 镜子：给一句「被看见」的洞察。空库不硬造。

    allow_model=True 才发模型凝句（只发聚合分布与一句主张，不发原始记录）；
    默认按 providers 是否可用自动判断；模型不可用/失败退回规则模板。
    云端没下发 brain.mirror（离线/未登录/无权益）→ 直接退场，只给解锁提示。
    """
    if not prompt_ready('brain.mirror'):
        return unavailable('brain.mirror', '镜子')
    try:
        facts = _mirror_facts(store)
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'reason': '镜子算不出来：%s' % type(exc).__name__}
    if not facts['total'] and not facts['rows']:
        text = '你还什么都没记。先记一条你昨天说过的一句话（原话就行），我再把你照给你看。'
        return {'ok': False, 'empty': True, 'text': text, 'source': 'rule',
                'distribution': {}, 'advantage': '', 'gap': ''}
    text = _mirror_rule_text(facts)
    source = 'rule'
    if allow_model is None:
        try:
            import providers
            allow_model = bool(providers.available().get('llm'))
        except Exception:  # noqa: BLE001
            allow_model = False
    if allow_model:
        try:
            import providers
            answer = providers.llm_json(prompt_text(
                'brain.mirror',
                distribution='、'.join('%s=%d' % kv for kv in sorted(facts['by_category'].items(),
                                                                     key=lambda kv: -kv[1])),
                advantages='；'.join('「%s」（%d 度）' % (a['claim'], a['degree'])
                                     for a in facts['advantages']) or '（还没有）',
                degrees='、'.join(str(d) for d in list(facts['degree'].values())[:20]),
                main_goal=''), {'facts': {'by_category': facts['by_category']}},
                tag='brain_mirror')
            candidate = str((answer or {}).get('text') or '').strip()
            if candidate:
                text, source = candidate, 'model'
        except Exception:  # noqa: BLE001  模型失败不影响镜子，退回规则句
            source = 'rule'
    guarded = guard_text(text, quoted=[a['claim'] for a in facts['advantages']])
    return {'ok': True, 'empty': False, 'text': guarded['text'], 'source': source,
            'guard': guarded,
            'distribution': {name: count for name, count in facts['by_category'].items()},
            'advantage': facts['advantages'][0]['claim'] if facts['advantages'] else '',
            'gap': ('、'.join(facts['empty'][:2]) if facts['empty'] else
                    (min((kv for kv in facts['by_category'].items() if kv[1] > 0),
                         key=lambda kv: kv[1])[0] if facts['top'] else '')),
            'prompt_key': 'brain.mirror'}


# ---------------------------------------------------------------- 出口压力（防收藏家谬误）
# 2026-11-17 t117：记录够多、近期没成稿时，温和地把一条认知推向作品；空库/有近期作品不提示。
EXIT_MIN_RECORDS = 10
EXIT_RECENT_DAYS = 14
_EXIT_CACHE = {}


def _recent_work(store, days=EXIT_RECENT_DAYS):
    """近 N 天有没有成稿/作品（project 有稿、script_version、artifact）。"""
    now = _now()
    def recent(value):
        when = _dt(value)
        return bool(when and (now - when).days <= days)
    try:
        for project in store.list('project', 500):
            if not isinstance(project, dict):
                continue
            if not (project.get('confirmed_version_id') or any(str(x.get('text') or '').strip()
                    for x in (project.get('draft') or []) if isinstance(x, dict))):
                continue
            if recent(project.get('updated_at') or project.get('created_at')):
                return True
        for kind in ('script_version', 'artifact'):
            for row in store.list(kind, 500):
                if isinstance(row, dict) and recent(row.get('created_at') or row.get('confirmed_at')
                                                     or row.get('updated_at')):
                    return True
    except Exception:
        return False
    return False


def _exit_candidate(store):
    rows = []
    try:
        for row in store.list('knowledge', 5000):
            if not isinstance(row, dict):
                continue
            if str(row.get('lifecycle') or 'active') != 'active':
                continue
            claim = _flat(row.get('claim') or row.get('title') or row.get('body') or '')
            if len(claim) < 8:
                continue
            rows.append((str(row.get('recorded_at') or row.get('updated_at') or ''), row, claim))
    except Exception:
        return None
    if not rows:
        return None
    rows.sort(key=lambda item: item[0], reverse=True)
    _when, row, claim = rows[0]
    return {'id': str(row.get('id') or ''), 'title': str(row.get('title') or '')[:60],
            'claim': _clip(claim, 80), 'create_url': '#/create'}


def exit_pressure(store):
    """记录够多且近期没作品 → 一句温和出口引导＋一条真实候选；不满足返回 None。
    2026-11-17 t119：危机刚发生时绝不推作品（安全边界优先）。"""
    if _CRISIS_SEEN_AT and time.time() - _CRISIS_SEEN_AT < 5:
        return None
    try:
        knowledge = [x for x in store.list('knowledge', 5000)
                     if isinstance(x, dict) and str(x.get('lifecycle') or 'active') == 'active']
    except Exception:
        return None
    if len(knowledge) < EXIT_MIN_RECORDS:
        return None
    if _recent_work(store):
        return None
    candidate = _exit_candidate(store)
    if not candidate:
        return None
    claim = candidate['claim'].rstrip('。！？!?；;，,')
    text = ('你已记了 %d 条，够挑一条变成作品了。比如这条：%s。'
            '想动手的话，我可以陪你先搭个开头，不用一次写完整。'
            % (len(knowledge), claim))
    return {'text': text, 'count': len(knowledge), 'candidate': candidate}


# ---------------------------------------------------------------- M2 give / ask 校准
FACT_WORDS = ('哪天', '什么时候', '多少', '几次', '几个', '几条', '最近', '上次',
              '上一次', '有没有', '是不是', '在哪', '哪一个', '哪个', '是什么', '什么')
DRAFT_WORDS = ('帮我写', '写一条', '写一篇', '写个', '起草', '初稿', '起稿', '开头',
               '文案', '脚本', '讲稿', '口播稿', '帮我起')
ASK_FOR_ANSWER_WORDS = ('给我答案', '直接给', '别问我', '不要问我', '你直接说', '告诉我答案',
                        '直接告诉我', '别反问')

BRANCH_LABELS = {
    'meta': '元问题：诚实答＋一句轻问回作品',
    'give': '事实直给（本地，不扣额度）',
    'ask_back': '有货：问回去，拿他自己的卡',
    'scaffold': '没货：给脚手架，不替定',
    'socrates': '初稿：苏格拉底，搭骨架留观点',
    'guard_redirect': 'work-not-psyche：不接心理，回到作品',
}
BRANCH_PROMPTS = {
    'ask_back': 'brain.coach.ask',
    'socrates': 'brain.coach.conviction',
}


# ---------------------------------------------------------------- 头脑风暴模式（t148）
# 2026-11-19：给第二大脑加「一起想未来方向」——四镜头发散、每条四要素、证据只取本人真实记录。
BRAINSTORM_WORDS = ('头脑风暴', '一起想', '未来方向', '往哪走', '往哪里走', '未来一年',
                    '接下来的方向', '规划未来', '帮我想方向', '想几个方向', '发散一下')
BRAINSTORM_FRAMES = ('SCAMPER 换角度', '六顶思考帽', '第一性原理', '反向假设')


def is_brainstorm(question):
    """保守识别：只接「我/自己」的方向类问题，避免把「未来一年经济怎样」当头脑风暴。"""
    q = str(question or '').strip()
    if not q or is_crisis(q):
        return False
    if re.search(r'选第\s*\d+\s*条', q):
        return True
    if len(q) < 6:
        return False
    if any(word in q for word in ('头脑风暴', '一起想', '帮我想方向', '想几个方向',
                                  '发散一下', '未来方向', '接下来的方向', '规划未来')):
        return True
    if any(word in q for word in ('往哪走', '往哪里走', '怎么走')) and ('我' in q or '自己' in q):
        return True
    if '未来一年' in q and ('我' in q or '自己' in q or '方向' in q):
        return True
    return False


def _bs_card(item):
    claim = _flat(item.get('claim') or item.get('body') or item.get('snippet') or item.get('title') or '')
    return {'id': str(item.get('source_id') or item.get('idx_id') or ''),
            'title': str(item.get('title') or '')[:60], 'claim': claim[:160],
            'date': str(item.get('recorded_at') or '')[:10]}


def _bs_evidence(store, theme):
    """只取真实记录：认知卡（当前使用）＋作品/口播语料计数；不补数、不编。"""
    cards = []
    try:
        found = content_index.search(store, theme, limit=10, include_body=True)
        for item in (found.get('items') or []):
            if item.get('source_kind') != 'knowledge':
                continue
            if (item.get('lifecycle') or 'active') != 'active':
                continue
            card = _bs_card(item)
            if len(_flat(card['claim'])) < 8:
                continue
            cards.append(card)
    except Exception:
        cards = []
    if not cards:
        try:
            for item in store.list('knowledge', 50):
                if (item.get('lifecycle') or 'active') != 'active':
                    continue
                card = {'id': str(item.get('id') or ''), 'title': str(item.get('title') or '')[:60],
                        'claim': _flat(item.get('claim') or item.get('body') or '')[:160],
                        'date': str(item.get('recorded_at') or item.get('created_at') or '')[:10]}
                if len(_flat(card['claim'])) >= 8:
                    cards.append(card)
                if len(cards) >= 3:
                    break
        except Exception:
            pass
    knowledge = len([x for x in store.list('knowledge', 100000)
                     if (x.get('lifecycle') or 'active') == 'active'])
    works = len([x for x in store.list('project', 10000) if not x.get('is_test')])
    try:
        import style_profile
        voices = int(style_profile.owner_corpus_stats(store).get('count') or 0)
    except Exception:
        voices = 0
    return {'cards': cards[:4], 'knowledge': knowledge, 'works': works, 'voices': voices}


def _bs_counts_line(ev):
    return '你已有 %d 条认知、%d 个作品、%d 段真实口播语料' % (ev['knowledge'], ev['works'], ev['voices'])


def _bs_directions(theme, ev):
    card = ev['cards'][0] if ev['cards'] else None
    claim = card['claim'] if card else ''
    title = card['title'] if card else '你已有的判断'
    counts = _bs_counts_line(ev)
    date = ('（%s 记的）' % card['date']) if card and card['date'] else ''
    why_base = ('依据：你%s写过「%s」；%s。' % (date, _clip(claim, 80), counts)) if card else ('依据：%s；这条方向还需要一条具体记录来验证。' % counts)
    dirs = [
        {'lens': 'SCAMPER 换角度',
         'direction': '把「%s」换一个用法，放到「%s」这条线上先做一个小实验。' % (title, theme),
         'why': why_base,
         'first_step': '写出一条 100 字以内的实验说明：给谁、解决什么、一周内怎么验证。',
         'risk': '换角度的方向容易自嗨；如果一周内找不到 3 个真实对象愿意看，就停。'},
        {'lens': '六顶思考帽',
         'direction': '先把「%s」拆成「值得做的机会」和「最可能翻车的风险」两栏，再选一边先动。' % theme,
         'why': why_base,
         'first_step': '各写 3 条机会、3 条风险，风险必须写上发生条件和应对。',
         'risk': '只写机会不写风险，会在投入后才发现方向不成立。'},
        {'lens': '第一性原理',
         'direction': '回到最底层：%s 里你真正想换掉的是什么？用你反复记过的判断当锚，而不是追热点。' % theme,
         'why': why_base,
         'first_step': '用一句话回答「我现在最想改变的一个具体处境」，再对照你的认知卡找依据。',
         'risk': '底层问题没想清就做内容，会变成换题不换困境。'},
        {'lens': '反向假设',
         'direction': '假设一年后这件事没做成，最可能死在「没有稳定输出」；反过来，先做能避免它的一步。',
         'why': why_base,
         'first_step': '定一个两周内可交付的最小作品（一条口播/一篇稿），先把输出跑通。',
         'risk': '反向假设容易变成焦虑清单；只保留一个能立刻做的动作。'},
    ]
    return dirs


def brainstorm(store, question):
    """第二大脑头脑风暴：四镜头方向＋每条四要素；证据只引用真实记录；无证据不硬造。"""
    q = str(question or '').strip()
    m = re.search(r'选第\s*(\d+)\s*条', q)
    theme = re.sub(r'[？?。！!，,：:\s]+', ' ', q).strip()
    theme = re.sub(r'^(请|帮我|一起|我们)?(来)?(想想|想|看)?', '', theme).strip()
    if len(theme) < 4:
        theme = q
    theme = theme[:80]
    ev = _bs_evidence(store, theme)
    if m and ev['cards']:
        if re.fullmatch(r'选第\s*\d+\s*条[：:]?.*', q) and len(theme) < 6:
            theme = '未来一年往哪走'
        idx = max(1, min(4, int(m.group(1))))
        dirs = _bs_directions(theme, ev)
        pick = dirs[idx - 1]
        text = ('好，先只做这一条（%s）：%s\n\n为什么是它：%s\n两周内的第一步：%s\n最大风险：%s\n\n'
                '先把这一步做完，再回来告诉我结果，我们再决定要不要加码。' %
                (pick['lens'], pick['direction'], pick['why'], pick['first_step'], pick['risk']))
        return {'text': text, 'mode': 'brainstorm', 'theme': theme,
                'brainstorm': {'theme': theme, 'frameworks': list(BRAINSTORM_FRAMES),
                               'counts': {'knowledge': ev['knowledge'], 'works': ev['works'], 'voices': ev['voices']},
                               'cards': ev['cards'], 'directions': [], 'converged': pick,
                               'need_evidence': False, 'converge': '先做这两周；做完再回来收敛。'}}
    if not ev['cards']:
        text = ('想认真想未来方向，得先有可引用的记录：现在库里还没有能引用的认知卡。'
                '先记 3 条你真正想清楚的判断（哪怕很短），我再按 SCAMPER／六顶思考帽／第一性原理／反向假设'
                '四个镜头，各给你一条带证据的方向；现在不硬编。')
        return {'text': text, 'mode': 'brainstorm', 'theme': theme,
                'brainstorm': {'theme': theme, 'frameworks': list(BRAINSTORM_FRAMES),
                               'counts': {'knowledge': ev['knowledge'], 'works': ev['works'], 'voices': ev['voices']},
                               'cards': [], 'directions': [], 'converged': None,
                               'need_evidence': True,
                               'converge': '先去记 3 条认知，再回来一起想。'}}
    dirs = _bs_directions(theme, ev)
    for i, d in enumerate(dirs, 1):
        d['n'] = i
        d['evidence_id'] = (ev['cards'][min(i - 1, len(ev['cards']) - 1)].get('id') if ev['cards'] else '')
    text = ('我们按四个镜头各看一条，不急着定。每条都带：方向、你凭什么、第一步、最大风险——证据只从你自己的记录里取。\n\n' +
            '\n\n'.join('%d. 【%s】%s\n凭什么：%s\n第一步：%s\n最大风险：%s' %
                          (d['n'], d['lens'], d['direction'], d['why'], d['first_step'], d['risk']) for d in dirs) +
            '\n\n先别全做。你愿意先用两周试哪 1–2 条？回我「选第N条」，我再陪你拆到下一步。')
    return {'text': text, 'mode': 'brainstorm', 'theme': theme,
            'brainstorm': {'theme': theme, 'frameworks': list(BRAINSTORM_FRAMES),
                           'counts': {'knowledge': ev['knowledge'], 'works': ev['works'], 'voices': ev['voices']},
                           'cards': ev['cards'], 'directions': dirs, 'converged': None,
                           'need_evidence': False,
                           'converge': '先别全做。你愿意先用两周试哪 1–2 条？回我「选第N条」。'}}


def convert_request(store, question):
    """「转选题：…」/「转目标：…」：一键落到现有选题池/目标树；失败如实说，不假装成功。"""
    q = str(question or '').strip()
    m = re.match(r'^(转选题|转目标)\s*[：:]\s*(.+)$', q, re.S)
    if not m:
        return None
    kind, title = m.group(1), m.group(2).strip()[:160]
    if not title:
        return {'ok': False, 'text': '要转的标题是空的，先给我一句话方向。'}
    if kind == '转选题':
        try:
            import contracts
            source_id = ''
            try:
                source_id = next((str(x.get('id')) for x in store.list('knowledge', 500)
                                  if (x.get('lifecycle') or 'active') == 'active'), '')
            except Exception:
                source_id = ''
            if not source_id:
                return {'ok': False, 'text': '选题池要求至少一个真实来源；现在库里还没有可引用的认知卡，先记一条再转。'}
            record = contracts.create_topic(title, knowledge_id=source_id,
                                            note='来自知识库头脑风暴（四镜头方向），待本人核对。',
                                            status='candidate')
            record.update({'origin': 'brainstorm', 'markers': ['待验证', '头脑风暴'],
                           'validation': 'unverified', 'angle': title, 'thesis': ''})
            saved = store.put('topic', record)
            return {'ok': True, 'kind': 'topic', 'text': '已存进选题池：「%s」。去「找选题」里就能看到，也可以直接开始写。' % saved['title'],
                    'topic': {'id': saved['id'], 'title': saved['title']}}
        except Exception as exc:
            return {'ok': False, 'text': '这次没存进选题池：%s' % str(exc)[:120]}
    try:
        import growth_service
        goals = [g for g in store.list('growth_goal', 1000) if g.get('level') == 'root' and not g.get('archived')]
        if not goals:
            return {'ok': False, 'text': '还没有主线目标；先去「重启系统」立一条主线，再把它转成目标。'}
        saved = growth_service.Growth(store).goal({'title': title, 'parent_id': goals[0]['id'],
                                                   'description': '来自知识库头脑风暴；待本人核对。'})
        return {'ok': True, 'kind': 'goal', 'text': '已加到目标树「%s」下面：「%s」。去「重启系统 · 目标体系」就能看到。' % (goals[0]['title'], saved['title']),
                'goal': {'id': saved['id'], 'title': saved['title'], 'parent_id': saved['parent_id']}}
    except Exception as exc:
        return {'ok': False, 'text': '这次没加进目标树：%s' % str(exc)[:120]}


# ---------------------------------------------------------------- 元问题（关于第二大脑/产品本身）
# 2026-11-17 t88：主理人定「接元问题，且是轻教练」。元问题不是「库里没记录」，不能 deflect，
# 要诚实说明它是什么/与通用 AI 的区别/教练魂，再给一句轻问回到他的作品或目标；
# 用户明确要答案时直给，不强行反问（权利挣来）。
META_PATTERNS = (
    '你是谁', '你是什么', '第二大脑是什么', '什么是第二大脑', '你这个系统', '这套系统是什么',
    '你和其他ai', '你跟其他ai', '和其他ai', '跟其他ai', '和通用ai', '跟通用ai', '和chatgpt',
    '跟chatgpt', '和gpt', '跟gpt', '有什么区别', '有什么不同', '有啥区别', '有啥不同',
    '你能做什么', '你会什么', '你能帮我做什么', '你有什么用', '你能干嘛', '你能干啥',
    '你怎么教练', '你如何教练', '你怎么教', '你教我什么', '教练魂', '你跟教练',
    '你记得我什么', '你为什么会记得', '你怎么记得', '你是ai吗', '你是ai', '你是人工智能吗',
    '这个产品是什么', '这个软件是什么', '这个工作台是什么', '你和别的ai',
)


def is_meta_question(question):
    """问的是第二大脑/AI/产品本身（而不是作品内容）。中英混写与大小写都兜住。
    2026-11-17 t119：危机问句也走这条早分支（brain_ask 不调模型，直接转介真人）。"""
    q = str(question or '').strip().lower()
    if not q:
        return False
    if is_crisis(q):
        return True
    compact = re.sub(r'\s+', '', q)
    return any(pat in compact for pat in META_PATTERNS)


# 2026-11-17 t115：能力类问句给具体清单；鼓励只基于真实记录，不空夸。
CAPABILITY_WORDS = ('你能做什么', '你会什么', '能帮我做什么', '你能帮我做什么', '你能帮我',
                    '有什么用', '能干什么', '你能干嘛', '有什么功能', '你能做啥', '会做什么')

CAPABILITY_TEXT = (
    '能帮你做这些，都是围绕你自己的内容：\n'
    '① 查你的记录：打卡、成片、选题、复盘、你认可过的认知卡，能说是哪天记的；\n'
    '② 找旧认知卡：把你以前写过、后来忘了的判断翻出来；\n'
    '③ 接选题：从你的积累或一条对标视频，拆出能讲的角度；\n'
    '④ 写稿、复盘稿子：改成你平时说话的口吻，不编你没经历过的事；\n'
    '⑤ 把模糊想法问清楚：用你自己的旧判断问回你，帮你想通；\n'
    '⑥ 通用知识直给：不是你记录里的问题也答，会标明「AI 通用回答·非你的记录」；\n'
    '⑦ 抖音链接拆解：抓文案和赞/评/藏/转/播放，再拆成选题角度。'
)

CAPABILITY_ASK = '你想先从哪件小事开始？'

FRUSTRATION_WORDS = ('没头绪', '不知道从哪', '不知道写什么', '卡住', '做不好', '搞不定',
                     '很挫败', '挫败', '焦虑', '迷茫', '没灵感', '烦', '没动力')


# 2026-11-17 t119：鼓励边界——危机/深度痛苦时接住一句并转介真人/专业，不教练、不诊断。
# 保守匹配：只收强信号，避免把普通抱怨（累、烦、不想工作）误判成危机。
CRISIS_PHRASES = (
    '不想活', '不想活了', '活着没意思', '活着没意义', '没意思活着', '想死', '想自杀',
    '自杀', '结束生命', '结束自己', '伤害自己', '自残', '跳楼', '轻生', '一了百了',
    '活着好累', '撑不下去', '想离开这个世界', '没有活下去的意义',
)

CRISIS_REPLY = (
    '听起来你现在很难受，这比任何选题都重要。先不用一个人扛：找一个你信任的人聊聊，'
    '或者联系当地心理援助热线（全国 12356 心理援助热线；北京 010-82951332）。'
    '如果你现在有伤害自己的念头，请立刻联系身边人，或拨打 120 / 110。'
    '知识库只陪你做作品，不处理心理危机，也不会替你诊断；你的感受值得被真人认真接住。'
)


_CRISIS_SEEN_AT = 0


def is_crisis(question):
    global _CRISIS_SEEN_AT
    q = re.sub(r'\s+', '', str(question or ''))
    hit = any(word in q for word in CRISIS_PHRASES)
    if hit:
        _CRISIS_SEEN_AT = time.time()
    return hit


def crisis_reply():
    return CRISIS_REPLY


def is_capability_question(question):
    q = re.sub(r'\s+', '', str(question or ''))
    return any(word in q for word in CAPABILITY_WORDS)


def is_frustrated(question):
    q = str(question or '')
    return any(word in q for word in FRUSTRATION_WORDS)


def encouragement(store):
    """基于真实记录给一句鼓励；没有数据就不说（不空夸、不编数）。"""
    def count(kind):
        try:
            return len([x for x in store.list(kind, 5000) if isinstance(x, dict)])
        except Exception:
            return 0
    knowledge = count('knowledge')
    fragments = count('growth_fragment')
    if knowledge >= 5:
        return '你已经记了 %d 条认知，这不是空话，是真在积累。' % knowledge
    if knowledge > 0:
        return '你已经记了 %d 条认知，第一条最难，你已经开始了。' % knowledge
    if fragments >= 5:
        return '你已经留下 %d 条碎片，这些都算数，慢慢会连成线。' % fragments
    if fragments > 0:
        return '你已经留下 %d 条碎片，先攒着，它们会连起来。' % fragments
    return ''


def _with_encouragement(text, store, question=''):
    """给回复加一句有依据的鼓励；挫败/没头绪时先接住情绪。"""
    parts = []
    if is_frustrated(question):
        parts.append('听起来你现在有点卡，这很正常，先不用急着想通。')
    if text:
        parts.append(text)
    note = encouragement(store)
    if note:
        parts.append(note)
    return '　'.join(parts)


def meta_answer(question):
    """轻教练式元问题回答：诚实说明（是什么/跟通用 AI 区别/教练魂）＋一句轻问回作品。

    不吹捧、不说「作为 AI」、不教练人格；用户明确要答案（给我答案/直接说）时
    只给答案、不强行反问。危机问句优先转介真人/专业，不做教练。
    """
    if is_crisis(question):
        return crisis_reply()
    if is_capability_question(question):
        # 明确要答案时只给清单，不强行反问（权利挣来）。
        return CAPABILITY_TEXT if _asks_for_answer(question) else CAPABILITY_TEXT + '　' + CAPABILITY_ASK
    # 2026-11-19 t153：定义归云端（brain.soul.meta）；本地只留最小兜底句。
    meta = (soul_config() or {}).get('meta') or {}
    positioning = str(meta.get('positioning') or SOUL_POSITIONING)
    definition = str(meta.get('definition') or
                     '我是你的知识库，不是通用聊天机器人：只从你自己的记录里找依据。')
    text = (positioning + ' ' + definition +
            ' 跟其他 AI 的区别：我记得你以前想过什么；我会按你的旧判断问回你；我只教练作品，不教练你这个人。'
            '现在能查记录、找旧卡、接选题、复盘一条稿子。')
    if _asks_for_answer(question):
        return text + '　要我从哪件事开始，直接说；我按你说的给，不绕。'
    return text + '　那你现在最想用它想通哪件事——一个选题、一条稿子，还是某个卡住的决定？'


def _has_material(items=None, focus=None):
    """他手上有没有「货」：只有够贴的卡才算，弱相关/关键词没命中兜底的那张不算。"""
    if isinstance(focus, dict) and not focus.get('weak') and (focus.get('quote') or focus.get('claim')):
        return True
    for item in items or []:
        if item.get('source_kind') != 'knowledge':
            continue
        try:
            score = float(item.get('score') or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score >= 30 and (item.get('body') or item.get('snippet')):
            return True
    return False


def _wants_draft(question):
    q = str(question or '')
    return any(word in q for word in DRAFT_WORDS)


def _asks_for_answer(question):
    q = str(question or '')
    return any(word in q for word in ASK_FOR_ANSWER_WORDS)


def classify(question, intent='', local_answer='', items=None, focus=None):
    """五档判定：元问题 / 要事实 / 要答案＋有货 / 真没货 / 要初稿。"""
    q = str(question or '').strip()
    if is_meta_question(q):
        return 'meta'
    if local_answer and (intent in ('checkin', 'artifact', 'topic', 'knowledge', 'profile')
                         or any(word in q for word in FACT_WORDS)):
        return 'give'
    if _asks_for_answer(q):
        return 'give' if local_answer or _has_material(items, focus) else 'scaffold'
    if _wants_draft(q):
        return 'socrates'
    if _has_material(items, focus):
        return 'ask_back'
    return 'scaffold'


def _card_quote(focus=None, items=None):
    """取一张够贴的卡来引用；弱相关卡不当证据（与 brain_ask 的弱相关口径一致）。"""
    if isinstance(focus, dict) and focus.get('quote') and not focus.get('weak'):
        return {'quote': focus['quote'], 'date': str(focus.get('date') or '')[:10],
                'title': focus.get('title') or '',
                'confidence': (focus.get('confidence') or {}).get('label') if isinstance(focus.get('confidence'), dict) else str(focus.get('confidence') or '')}
    for item in items or []:
        if item.get('source_kind') != 'knowledge':
            continue
        try:
            score = float(item.get('score') or 0)
        except (TypeError, ValueError):
            score = 0.0
        quote = _flat(item.get('claim') or item.get('snippet') or item.get('body'))
        if score >= 30 and len(quote) >= 8:
            conf = item.get('confidence') if isinstance(item.get('confidence'), dict) else None
            return {'quote': _clip(quote, 60), 'date': str(item.get('recorded_at') or '')[:10],
                    'title': item.get('title') or '', 'confidence': (conf or {}).get('label') or ''}
    return {}


def confidence_label(record):
    try:
        return content_index.confidence(record).get('label') or '只是猜想'
    except Exception:
        return '只是猜想'


# 2026-11-19 t160：预测/事前验尸＋按客户价值观决策（本地、只读客户记录）。
FORECAST_WORDS = ('事前验尸', '预演', '预测一下', '如果这么做', '如果这样做', '三个月后最可能',
                  '最可能死', '最好会怎样', '会怎样')
DECISION_WORDS = ('决策', '权衡', '选哪个', '该选', '二选一', '怎么选', '选A还是B')


def is_forecast(question):
    q = str(question or '')
    return any(word in q for word in FORECAST_WORDS) and not is_crisis(q)


def is_decision(question):
    q = str(question or '')
    if is_crisis(q):
        return False
    if not any(word in q for word in DECISION_WORDS):
        return False
    return len(_decision_options(q)) >= 2 or '哪个' in q


def _decision_options(question):
    q = re.sub(r'^(?:帮我|请你|我想|想请你|我该|我要|我)?\s*(?:决策|权衡|选哪个|怎么选|该选|选)[：:，,\s]*', '', str(question or ''))
    q = re.sub(r'[？?。！!]+$', '', q).strip()
    parts = re.split(r'\s*(?:还是|或者|VS|vs|／|/|、|，|,)\s*', q)
    out = []
    for part in parts:
        part = re.sub(r'^(选|选择|做|要|该)', '', part.strip())
        part = re.sub(r'(更好|更合适|吧|呢)$', '', part).strip()
        if part and part not in out:
            out.append(part[:40])
    return out[:5]


def _client_values(store):
    """只读客户写下的主线/命题/目标/价值观认知；没有就返回空，不补通用价值观。"""
    values = []
    try:
        goals = [g for g in store.list('growth_goal', 1000) if not g.get('archived')]
    except Exception:
        goals = []
    root = next((g for g in goals if g.get('level') == 'root'), None)
    if root:
        if root.get('thesis'):
            values.append({'label': '你写的命题', 'text': str(root['thesis'])[:160]})
        if root.get('milestone'):
            values.append({'label': '你的主业目标', 'text': str(root['milestone'])[:120]})
        values.append({'label': '你的主线', 'text': str(root.get('title') or '')[:80]})
    for goal in goals:
        if goal.get('level') in ('pillar', 'goal') and goal.get('title'):
            values.append({'label': '你的目标', 'text': str(goal['title'])[:80]})
    try:
        cards = [x for x in store.list('knowledge', 100000)
                 if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        cards = []
    for card in cards:
        cat = str(card.get('category') or '')
        if '价值观' in cat or '价值' in cat:
            claim = _flat(card.get('claim') or card.get('title'))
            if len(claim) >= 6:
                values.append({'label': '你写过的价值观', 'text': claim[:120]})
    return values[:8]


def _value_hits(option, values):
    option_terms = set(re.findall(r'[\u4e00-\u9fff]{2}', _flat(option)))
    scored = []
    for value in values:
        text = _flat(value.get('text'))
        hits = sum(1 for term in option_terms if term in text)
        if hits:
            scored.append((hits, value))
    scored.sort(key=lambda x: -x[0])
    return scored


def forecast(store, question):
    """事前验尸：2–3 个情景＋定性概率；复用头脑风暴的假设/反向框架；只引真实记录。"""
    action = re.sub(r'^(如果|假如|要是)?\s*', '', str(question or '')).strip()
    action = _clip(action or '这件事', 40)
    cards = []
    try:
        found = content_index.search(store, action, limit=8, include_body=True)
        cards = [{'quote': _flat(x.get('claim') or x.get('snippet') or x.get('body'))[:90],
                  'date': str(x.get('recorded_at') or '')[:10], 'confidence': (x.get('confidence') or {}).get('label', '')}
                 for x in (found.get('items') or []) if x.get('source_kind') == 'knowledge']
        cards = [x for x in cards if len(x['quote']) >= 8][:2]
    except Exception:
        cards = []
    try:
        total = len([x for x in store.list('knowledge', 100000)
                     if (x.get('lifecycle') or 'active') == 'active'])
        works = len([x for x in store.list('project', 10000) if not x.get('is_test')])
    except Exception:
        total, works = 0, 0
    evidence = ('你已有 %d 条认知、%d 个作品' % (total, works))
    if cards:
        evidence += '；可引用：「%s」%s' % (cards[0]['quote'], ('（' + cards[0]['date'] + '）') if cards[0]['date'] else '')
    else:
        evidence += '；库里没有能贴住这件事的卡，下面只是情景框架，不是你的记录结论'
    scenarios = [
        {'key': 'worst', 'label': '最可能死在哪', 'probability': '偏高',
         'text': '三个月后最可能死在「一直准备、没有最小验证」：没有落到一条具体作品或一次真实交付，热情先耗尽。'},
        {'key': 'best', 'label': '最好会怎样', 'probability': '偏低',
         'text': ('最好情况：它成为你已有判断「%s」的第一次真实检验，三个月后留下一个可复用模板和一条真实反馈。' % (cards[0]['quote'] if cards else '你想验证的那条判断'))},
        {'key': 'middle', 'label': '中间态', 'probability': '中',
         'text': '中间态：做成半成品，既没验证也没放弃，占掉接下来三个月的注意力。'},
    ]
    text = ('先当它三个月后已经发生，倒回来看：%s。\n\n' % evidence) + '\n'.join(
        '%d. 【%s · 概率%s】%s' % (i, item['label'], item['probability'], item['text'])
        for i, item in enumerate(scenarios, 1))
    text += ('\n\n概率只给相对高低，不装百分比；这是情景推演，不是预测。'
             '先问自己：哪个情景最像你会走进去的？')
    return {'text': text, 'scenarios': scenarios, 'evidence': evidence,
            'cards': cards, 'action': action,
            'source_label': '知识库 · 事前验尸（你的记录＋情景框架）'}


def decision(store, question):
    """按客户写下的价值观/主线/目标给选项排序；不给通用建议，拍板归客户。"""
    options = _decision_options(question)
    values = _client_values(store)
    if len(options) < 2:
        return {'text': '把选项写出来，我才能按你在乎的东西排：比如「A 还是 B」。不替你凭空造选项。',
                'options': [], 'ranked': [], 'values': values,
                'source_label': '知识库 · 决策权衡（只按你的记录）'}
    if not values:
        return {'text': '你还没写下主线/命题/目标/价值观，我不拿通用价值观替你选。先去「重启系统」写一句主线，再回来权衡。',
                'options': options, 'ranked': [], 'values': [],
                'source_label': '知识库 · 决策权衡（缺你的价值观）'}
    ranked = []
    for option in options:
        hits = _value_hits(option, values)
        score = sum(h[0] for h in hits)
        ranked.append({'option': option, 'score': score,
                       'value': (hits[0][1] if hits else None), 'hit_count': len(hits)})
    ranked.sort(key=lambda x: -x['score'])
    top = ranked[0]
    if top['score'] == 0:
        text = ('按你写下的这些，我没法把哪个选项排前面——它们都没直接对上你的主线/价值观。'
                '先把每个选项「服务哪条主线」写出来，再选；拍板是你。')
    else:
        value = top['value'] or {}
        text = ('按你在乎的「%s」：我会把「%s」排前面。\n因为它更贴近你写下的「%s」。\n\n'
                % (value.get('label') or '价值观', top['option'], _clip(value.get('text'), 80)))
        text += '排序：' + '；'.join('%d. %s' % (i, x['option']) for i, x in enumerate(ranked, 1))
        text += '\n\n这是按你的记录做的对照，不是通用建议；拍板是你。'
    return {'text': text, 'options': options, 'ranked': ranked, 'values': values,
            'source_label': '知识库 · 决策权衡（只按你的记录）'}


def is_capsule_question(question):
    q = str(question or '')
    return any(word in q for word in ('时间胶囊', '我的胶囊', '封存：', '封存:', '封一个时间胶囊'))


def _capsule_days(text):
    m = re.search(r'(30|90)\s*天', text)
    return int(m.group(1)) if m else 30


def capsule_answer(store, question):
    """t166：封存/查看时间胶囊；到期才提示，未到期不打扰；空库不造。"""
    q = str(question or '').strip()
    m = re.match(r'^封存\s*[：:]\s*(.+)$', q, re.S)
    if m:
        body = m.group(1).strip()
        days = _capsule_days(body)
        body = re.sub(r'[，,。；;]?\s*(30|90)\s*天\s*$', '', body).strip()
        reason = ''
        rm = re.search(r'理由\s*[：:]\s*(.+)$', body, re.S)
        if rm:
            reason = rm.group(1).strip()
            body = body[:rm.start()].strip()
        try:
            item = growth.create_capsule(store, body, days, reason=reason)
            text = ('封好了：你赌「%s」，%d 天后（%s）开封。到期我会先问你「现在对了吗」，不提前打扰。'
                    % (item['text'][:80], days, item['open_at'][:10]))
            return {'text': text, 'capsule': item, 'mode': 'capsule_create'}
        except Exception as exc:
            return {'text': '没封成：%s' % str(exc)[:100], 'capsule': None, 'mode': 'capsule_create'}
    if '封存' in q:
        return {'text': '写全一点：封存：<你现在的预测或决定>，30天（或 90天）。我会在到期日拿它对照现实。',
                'capsule': None, 'mode': 'capsule_create'}
    items = growth.list_capsules(store)
    stats = growth.capsule_stats(store)
    if not items:
        return {'text': '还没有封存的时间胶囊。说一句「封存：<你的预测或决定>，30天」就开始；空库我不编。',
                'capsules': [], 'stats': stats, 'mode': 'capsule_list'}
    due = [x for x in items if x.get('due')]
    lines = []
    for item in items[:8]:
        if item.get('status') == 'opened':
            label = {'right': '对', 'partial': '部分对', 'wrong': '错'}.get(item.get('calibration'), '已开封')
            lines.append('「%s」——已开封：%s。' % (item.get('text', '')[:60], label))
        elif item.get('due'):
            lines.append('「%s」——到期了。你当时赌这个，现在对了吗？' % (item.get('text', '')[:60]))
        else:
            lines.append('「%s」——还有 %d 天开封（%s）。' % (item.get('text', '')[:60], int(item.get('days_left') or 0), item.get('open_text')))
    head = ('有 %d 个到期，先对照现实；没到期的我不提前打扰。' % len(due)) if due else '还没到期的胶囊，先不追问；到期我会主动提。'
    return {'text': head + '\n\n' + '\n'.join(lines) + '\n\n' + stats['note'],
            'capsules': items, 'stats': stats, 'mode': 'capsule_list'}


def apply_capsule_open(store, capsule_id, result, note=''):
    return growth.open_capsule(store, capsule_id, result, note)


def capsule_due_note(store):
    """到期主动浮现：只取第一个到期胶囊；未到期/无胶囊返回 None。"""
    try:
        items = growth.list_capsules(store)
    except Exception:
        return None
    due = next((x for x in items if x.get('due')), None)
    if not due:
        return None
    return {'id': due.get('id'), 'text': due.get('text'), 'open_text': due.get('open_text'),
            'note': '时间胶囊到期：你当时赌「%s」（%s 封的），现在对了吗？对/部分/错，记一笔给以后校准。'
                    % (str(due.get('text') or '')[:80], due.get('open_text') or '')}


def is_revision_question(question):
    q = str(question or '')
    return any(word in q for word in ('矛盾', '改旧判断', '自我更新', '我变了吗', '观点冲突', '前后不一致'))


def revision_answer(store):
    """t162：新记录与旧判断矛盾 → 先问要不要改；确认走 apply_revision，不自动改。"""
    candidates = []
    try:
        candidates = content_index.revision_candidates(store)
    except Exception:
        candidates = []
    if not candidates:
        return {'text': '暂未发现需要修订的矛盾判断；我不会为了更新而更新。',
                'revisions': [], 'source_label': '知识库 · 信念修订（只按真实记录）'}
    lines, payload = [], []
    for item in candidates:
        hard = _pick_hard('新证据 改旧判断', item.get('old_claim') or '')
        line = ('你以前写过「%s」（%s），最近写了「%s」（%s）。新证据要不要改旧判断？\n%s'
                % (_clip(item.get('old_claim'), 70), item.get('old_date') or '之前',
                   _clip(item.get('new_claim'), 70), item.get('new_date') or '最近', hard))
        lines.append(line)
        payload.append({**item, 'question': hard})
    text = ('先看这几组前后判断。改不改由你拍板；不确认，旧卡原文一直保留。\n\n' + '\n\n'.join(lines))
    return {'text': text, 'revisions': payload,
            'source_label': '知识库 · 信念修订（新证据对照）'}


def apply_revision(store, old_id, new_id='', new_title='', new_text=''):
    return content_index.revise_card(store, old_id, new_id=new_id,
                                      new_title=new_title, new_text=new_text)


def is_blind_spot(question):
    q = str(question or '')
    return any(word in q for word in ('盲点', '从没考虑', '没考虑过', '忽略了什么', '漏掉了什么'))


def blind_spot_answer(store):
    """对照主线/目标找没记录过的维度，用六类硬问题追问；没有缺口不硬造。"""
    facts = content_index.blind_spots(store)
    dims = facts.get('dimensions') or []
    if not dims:
        note = facts.get('note') or '没有发现明显盲点。'
        return {'text': '先不硬找盲点：' + note,
                'blind_spots': [], 'facts': facts,
                'source_label': '知识库 · 盲点对照（只按真实缺口）'}
    lines = []
    payload = []
    for item in dims:
        title = str(item.get('title') or '')
        question = _pick_hard('我从没考虑过' + title, title)
        lines.append('你从没考虑过「%s」：%s' % (title, question))
        payload.append({'title': title, 'question': question, 'knowledge': int(item.get('knowledge') or 0),
                        'reason': item.get('reason') or ''})
    text = ('先对照你的主线看：%s\n\n' % (facts.get('note') or '')) + '\n'.join(lines)
    text += '\n\n这是缺口，不是判断；先答一个，再决定要不要补记录。'
    return {'text': text, 'blind_spots': payload, 'facts': facts,
            'source_label': '知识库 · 盲点对照（只按真实缺口）'}


def _ask_back_text(question, focus=None, items=None):
    card = _card_quote(focus, items)
    if not card:
        return ''
    date = _md(card['date'])
    label = card.get('confidence') or '只是猜想'
    head = ('你写过「%s」（%s · %s）' % (card['quote'], date, label)) if date else ('你写过「%s」（%s）' % (card['quote'], label))
    return '%s。先别解释。回答这个：%s' % (head, _pick_hard(question, card['quote']))


def _scaffold_text(question):
    q = _clip(question, 28)
    return ('库里没有能贴住「%s」的卡，我不替你定。先回答一个：' % q) + _pick_hard(question, q)


def _socrates_text(question):
    return ('先不写整篇，骨架我搭，观点留给你：'
            '① 开头：一个你亲身经历的具体场景（不许编）；'
            '② 中间：你为什么信这件事——说一件你拿它换来的真事，包括代价；'
            '③ 结尾：看完的人能做的一件事。'
            '那句「你为什么信」得你自己写——我替你写的就是抄。'
            '先说一件真事：你为这条观点付出过什么？')


def turn(store, question, intent='', local_answer='', items=None, focus=None, status=''):
    """教练模式一轮：四档分支 + 护栏。返回可直接渲染的决定，不调云端模型（零新增调用）。
    基础教练提示词没下发（离线/未登录/无权益）→ 退场回助手，只给解锁提示。"""
    q = str(question or '').strip()
    # 2026-11-17 t119：危机优先于一切教练行为（即使教练提示词未下发也要接住）。
    if is_crisis(q):
        return {'mode': MODE_COACH, 'action': 'crisis_referral', 'branch': 'crisis',
                'branch_label': '危机/深度痛苦：转介真人专业，不教练',
                'text': crisis_reply(),
                'guard': {'ok': True, 'hits': [], 'redirected': False},
                'prompt_key': '', 'status': status, 'crisis': True}
    if not coach_unlocked():
        return unavailable('brain.coach.ask', '教练')
    induced = psyche_question(q)
    if induced:
        text = PSYCHE_REDIRECT + '　' + WORKS_QUESTION
        try:
            applog.info('brain.coach.guard', '心理诱导拦截', hits=induced)
        except Exception:  # noqa: BLE001
            pass
        return {'mode': MODE_COACH, 'action': 'guard_redirect', 'branch': 'guard_redirect',
                'branch_label': BRANCH_LABELS['guard_redirect'], 'text': text,
                'guard': {'ok': False, 'hits': ['提问:' + induced[0]], 'redirected': True},
                'induced': induced, 'prompt_key': '', 'status': status}
    if is_meta_question(q):
        return {'mode': MODE_COACH, 'action': 'meta', 'branch': 'meta',
                'branch_label': BRANCH_LABELS['meta'], 'text': meta_answer(q),
                'guard': {'ok': True, 'hits': [], 'redirected': False},
                'prompt_key': '', 'status': status}
    branch = classify(q, intent, local_answer, items, focus)
    if branch == 'meta':
        return {'mode': MODE_COACH, 'action': 'meta', 'branch': 'meta',
                'branch_label': BRANCH_LABELS['meta'], 'text': meta_answer(q),
                'guard': {'ok': True, 'hits': [], 'redirected': False},
                'prompt_key': '', 'status': status}
    if branch == 'give':
        text = str(local_answer or '').strip()
        if not text:
            card = _card_quote(focus, items)
            text = ('你要答案，我先按你自己记过的这条给：%s' % card['quote']) if card else ''
        if not text:
            branch = 'scaffold'
            text = _scaffold_text(q)
    elif branch == 'ask_back':
        text = _ask_back_text(q, focus, items)
        if not text:
            branch = 'scaffold'
            text = _scaffold_text(q)
    elif branch == 'socrates':
        text = _socrates_text(q)
    else:
        branch = 'scaffold'
        text = _scaffold_text(q)
    quoted = [x for x in [(focus or {}).get('quote')] if x]
    # 2026-11-19 t153：教练回复先去 AI 套话、短句，再按语气选择调整措辞。
    text = humanize(text, store)
    # 2026-11-17 t115：有依据的鼓励＋挫败先接住情绪（不空夸、不评判人格）。
    text = _with_encouragement(text, store, q)
    text = _tone_text(text, _tone(store))
    guarded = guard_text(text, quoted=quoted)
    action = 'give' if branch == 'give' else branch
    return {'mode': MODE_COACH, 'action': action, 'branch': branch,
            'branch_label': BRANCH_LABELS.get(action, ''),
            'text': guarded['text'], 'guard': guarded, 'induced': [],
            'prompt_key': BRANCH_PROMPTS.get(action, ''), 'status': status}


# ---------------------------------------------------------------- 主动记忆（浮现旧判断/变化）
# 2026-11-17 t118：不等人问，主动递上被遗忘的旧判断；同主题旧卡与近期记录观点不同时温和指出「变了」。
RECALL_MIN_AGE_DAYS = 30
RECALL_CHANGE_GAP_DAYS = 60
RECALL_RECENT_DAYS = 45
NEGATION_WORDS = ('不', '别', '不要', '并非', '不是', '未必', '反而', '其实不是')


def _cjk_terms(text):
    return [t for t in re.findall(r'[\u4e00-\u9fff]{2,6}', _flat(text)) if len(t) >= 2]


def _bigrams(text):
    """中文 2-gram（跨词也能对上，如「追新流量」vs「追新流量没用」）。"""
    chars = re.sub(r'[^\u4e00-\u9fff]', '', _flat(text))
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def _knowledge_items(store, question):
    try:
        found = content_index.search(store, question, limit=20, include_body=True)
    except Exception:
        return []
    items = []
    for item in (found.get('items') or []):
        if not isinstance(item, dict) or item.get('source_kind') != 'knowledge':
            continue
        claim = _flat(item.get('claim') or item.get('body') or item.get('snippet') or item.get('title') or '')
        if len(claim) < 8:
            continue
        items.append({'id': str(item.get('source_id') or item.get('idx_id') or item.get('id') or ''),
                      'claim': claim,
                      'title': str(item.get('title') or '')[:60],
                      'date': str(item.get('recorded_at') or item.get('updated_at') or '')[:10],
                      'score': float(item.get('score') or 0),
                      'when': _dt(item.get('recorded_at') or item.get('updated_at'))})
    return items


def recall_old(store, question, answer=''):
    """主动浮现：更早的旧判断（对照）／同主题观点变化（温和指出）；无旧卡返回 None。"""
    q = str(question or '').strip()
    if is_crisis(q):
        return None
    if not q:
        return None
    items = _knowledge_items(store, q)
    if not items:
        return None
    now = _now()
    old = [x for x in items if x['when'] and (now - x['when']).days >= RECALL_MIN_AGE_DAYS]
    old.sort(key=lambda x: (-x['score'], x['when']))
    if not old:
        return None
    recent = [x for x in items if x['when'] and (now - x['when']).days <= RECALL_RECENT_DAYS]
    changed = None
    for item in recent:
        if not item['when']:
            continue
        gap = (item['when'] - old[0]['when']).days if item['when'] > old[0]['when'] else 0
        if gap < RECALL_CHANGE_GAP_DAYS:
            continue
        if _flat(item['claim']) == _flat(old[0]['claim']):
            continue
        overlap = _bigrams(old[0]['claim']) & _bigrams(item['claim'])
        if len(overlap) < 3:
            continue
        changed = (old[0], item)
        break
    if changed:
        before, after = changed
        return {'kind': 'changed',
                'text': '你这几个月想法变了：你 %s 写过「%s」，最近又记过「%s」。两个都留着，看看你现在更认哪个。'
                        % (before['date'] or '之前', _clip(before['claim'], 60), _clip(after['claim'], 60)),
                'before': {'id': before['id'], 'claim': before['claim'], 'date': before['date']},
                'after': {'id': after['id'], 'claim': after['claim'], 'date': after['date']}}
    pick = old[0]
    opposite = (any(w in pick['claim'] for w in NEGATION_WORDS)
                != any(w in (str(answer or '') + q) for w in NEGATION_WORDS))
    note = '（这条和你现在的说法可能相反，值得对照。）' if opposite else ''
    return {'kind': 'contrast',
            'text': '你以前写过：「%s」（%s）。和现在这条对照看看，别急着覆盖它。%s'
                    % (_clip(pick['claim'], 60), pick['date'] or '之前', note),
            'old': {'id': pick['id'], 'claim': pick['claim'], 'date': pick['date'],
                    'possibly_opposite': opposite}}


# ---------------------------------------------------------------- M3 用你的过去，教练你的现在
STALE_DAYS = 14
PAST_TEMPLATE = ('你{date}写过『{claim}』。今天这个{task}，正好在考验它——'
                 '你现在还信吗？这条片要不要就讲这个挣扎？')


def past(store, task=''):
    """M3 过去对质：取相关、久未碰/未验证的旧卡，生成一句对质（用他的原话与日期）。

    深度教练层：只有 active（云端下发了 brain.coach.past）才做；trial/离线直接退场。
    """
    if not prompt_ready('brain.coach.past'):
        return unavailable('brain.coach.past', '过去对质')
    task = _flat(task)
    if not task:
        return {'ok': False, 'reason': '没给任务，不硬造对质'}
    try:
        terms = [t for t in content_index._terms(task) if t]
    except Exception:  # noqa: BLE001
        terms = [t for t in re.split(r'[\s,，、。]+', task) if len(t) >= 2]
    if not terms:
        return {'ok': False, 'reason': '任务里没抽到关键词，不硬造对质'}
    rows = [r for r in _knowledge_rows(store) if _is_active(r)]
    degree = _degrees(store)
    now = _now()
    picked = []
    for row in rows:
        cid = str(row.get('id') or '')
        text = _flat((row.get('title') or '') + ' ' + (row.get('claim') or row.get('body') or ''))
        rel = sum(1 for t in terms if t in text)
        if not rel:
            continue
        when = _dt(row.get('recorded_at')) or _dt(row.get('updated_at'))
        age = (now - when).days if when else 999
        deg = int(degree.get(cid) or 0)
        unverified = str(row.get('validation') or 'unverified') != 'confirmed'
        if age < STALE_DAYS and not unverified and deg > 0:
            continue
        claim = _flat(row.get('claim') or row.get('body') or '')
        if len(claim) < 8:
            continue
        picked.append({'id': cid, 'claim': _clip(claim, 60),
                       'title': _clip(row.get('title'), 24),
                       'date': str(row.get('recorded_at') or '')[:10],
                       'age_days': age, 'degree': deg, 'relevance': rel,
                       'unverified': unverified})
    if not picked:
        return {'ok': False, 'reason': '没有相关的旧卡（久未碰/未验证），不硬造对质',
                'task': task, 'terms': terms[:6]}
    picked.sort(key=lambda c: (-c['relevance'], -min(c['age_days'], 999), -int(c['unverified'])))
    card = picked[0]
    # 输出句走规则模板（他自己的原话＋真实日期），提示词 brain.coach.past 是同一口径的模型版。
    text = PAST_TEMPLATE.format(date=_md(card['date']), claim=card['claim'],
                                task=_clip(task, 30))
    guarded = guard_text(text, quoted=[card['claim']])
    return {'ok': True, 'text': guarded['text'], 'card': card, 'guard': guarded,
            'prompt_key': 'brain.coach.past', 'candidates': len(picked), 'task': task}


# ---------------------------------------------------------------- M4 炼 conviction
VAGUE_MARKERS = ('努力', '坚持', '运气', '天赋', '热爱', '认知', '格局', '自律',
                 '财务自由', '选择大于努力', '相信自己')
CONVICTION_STEPS = (
    ('specifics', '这是你拿什么经历换来的？说一件真事——哪天、谁在场、你手里正干着什么。'),
    ('cost', '你为它付出过什么代价？钱、时间、关系，说一个具体的。'),
    ('boundary', '什么情况下它就不成立？说一个反例。'),
)


def _vague(text):
    value = _flat(text)
    if len(value) < 6:
        return True
    return any(word in value for word in VAGUE_MARKERS)


def conviction(claim='', said=None):
    """M4 炼 conviction：飘的观点 → 逼 specifics/代价/边界 → 一句他认领的主张。

    said 是本轮之前他已经答出来的三项（键：specifics / cost / boundary）。
    返回 done=False 时 question 是下一句逼问；done=True 时 sentence 是待他改/拍的定稿句。
    深度教练层：只有 active 才做（云端下发了 brain.coach.conviction）。
    """
    if not prompt_ready('brain.coach.conviction'):
        return unavailable('brain.coach.conviction', '炼主张')
    claim = _flat(claim)
    if not claim:
        return {'ok': False, 'reason': '先给我一句你的观点（一句话就行）'}
    said = dict(said or {})
    for key, question in CONVICTION_STEPS:
        answer = _flat(said.get(key))
        if not answer:
            return {'ok': True, 'done': False, 'step': key, 'claim': _clip(claim, 60),
                    'question': '「%s」——%s' % (_clip(claim, 40), question),
                    'said': said, 'prompt_key': 'brain.coach.conviction'}
        if _vague(answer):
            return {'ok': True, 'done': False, 'step': key, 'claim': _clip(claim, 60),
                    'question': '「%s」这句还是飘的，不算具体。%s' % (_clip(answer, 30), question),
                    'said': said, 'vague': True, 'prompt_key': 'brain.coach.conviction'}
    sentence = ('我相信「%s」，我拿「%s」换来的；它不成立的时候是「%s」。'
                % (_clip(claim, 60), _clip(said['specifics'], 50), _clip(said['boundary'], 50)))
    guarded = guard_text(sentence)
    return {'ok': True, 'done': True, 'claim': _clip(claim, 60), 'said': said,
            'sentence': guarded['text'], 'guard': guarded, 'text': guarded['text'],
            'prompt_key': 'brain.coach.conviction',
            'note': '这句你自己改，改到你认领为止——我不替你拍。'}


# ---------------------------------------------------------------- M6 教练权利是挣来的
MODE_ASSISTANT = 'assistant'
MODE_COACH = 'coach'
MODES = (MODE_ASSISTANT, MODE_COACH)
INVITE_AFTER_TURNS = 3          # 有用交互几次后邀请开教练（不是自动开）
SOUL_POSITIONING = '我主要帮你用自己的记录想清楚、写出来；通用知识只是顺手。'
INVITE_TEXT = ('这几轮你都用得挺实。要不要开「教练模式」？开了之后我会在关键处把问题问回给你，'
               '让你自己说；不开就还是现在这样，我直接给。')


def _coach_state(store):
    session = _session(store)
    value = session.get('coach')
    return dict(value) if isinstance(value, dict) else {}


def _save_coach_state(store, changes):
    session = _session(store)
    payload = dict(_coach_state(store))
    payload.update(changes)
    payload['updated_at'] = datetime.now(TZ).isoformat(timespec='seconds')
    try:
        if session.get('id'):
            store.patch('brain_session', session['id'], {'coach': payload})
        else:
            store.put('brain_session', {'coach': payload}, 'main')
    except Exception:  # noqa: BLE001
        pass
    return payload


def useful_turns(store):
    """有用交互次数：问答记录条数（不含开场/浏览）。"""
    try:
        return len(store.list('question', 500))
    except Exception:  # noqa: BLE001
        return 0


def mode(store):
    """当前模式：默认助手；只有被邀请、本人同意、且云端下发了基础教练提示词才是教练。"""
    state = _coach_state(store)
    if state.get('mode') == MODE_COACH and state.get('consent') and coach_unlocked():
        return MODE_COACH
    return MODE_ASSISTANT


def state(store):
    turns = useful_turns(store)
    current = mode(store)
    return {'mode': current, 'consent': bool(_coach_state(store).get('consent')),
            'since': _coach_state(store).get('since') or '',
            'turns': turns, 'invite_after': INVITE_AFTER_TURNS,
            'invite': current == MODE_ASSISTANT and turns >= INVITE_AFTER_TURNS and coach_unlocked(),
            'invite_text': INVITE_TEXT,
            'unlocked': coach_unlocked(), 'deep_unlocked': deep_unlocked(),
            'lock_hint': '' if coach_unlocked() else LOCK_HINT}


def invite(store):
    """是否该邀请开教练（只读，不偷偷开）。"""
    info = state(store)
    return {'invite': info['invite'], 'turns': info['turns'],
            'invite_after': info['invite_after'],
            'text': INVITE_TEXT if info['invite'] else ''}


def set_mode(store, mode_name, consent=False):
    """切模式。开教练必须本人同意（consent=True）或此前已被邀请过；默认助手。"""
    name = str(mode_name or '').strip().lower()
    if name not in MODES:
        return {'ok': False, 'reason': '模式只能是 assistant 或 coach'}
    current = _coach_state(store)
    if name == MODE_COACH and not coach_unlocked():
        return {'ok': False, 'reason': LOCK_HINT, 'mode': MODE_ASSISTANT,
                'need_unlock': True, 'hint': LOCK_HINT}
    if name == MODE_COACH and not (consent or current.get('consent')):
        return {'ok': False, 'reason': '教练模式要你同意才开（默认是助手，直接给）。',
                'mode': MODE_ASSISTANT, 'need_consent': True}
    stamp = datetime.now(TZ).isoformat(timespec='seconds')
    changes = {'mode': name, 'consent': bool(consent or current.get('consent')),
               'invited': bool(consent or current.get('invited')),
               'since': stamp}
    _save_coach_state(store, changes)
    return {'ok': True, 'mode': name, 'state': state(store)}


def assistant_note(store):
    """助手模式附带的提示：未解锁时给「登录后解锁教练」，解锁且到门槛时给邀请。"""
    if not coach_unlocked():
        return {'locked': True, 'text': LOCK_HINT, 'hint': LOCK_HINT,
                'positioning': SOUL_POSITIONING}
    info = invite(store)
    if not info['invite']:
        return {'positioning': SOUL_POSITIONING}
    return {'invite': True, 'text': info['text'], 'turns': info['turns'],
            'positioning': SOUL_POSITIONING}


# ---------------------------------------------------------------- M7 最难的教练
CHALLENGE_MIN = 3   # 验证几次不支持才提「前提需改」——默认 3，待主理人拍板


def _latest_metric(work):
    for key in ('play_count', 'view_count', 'views'):
        try:
            value = int(work.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def _match_thesis(work, thesis, terms):
    text = _flat((work.get('title') or '') + ' ' + (work.get('thesis') or ''))
    return sum(1 for t in terms if t and t in text)


def challenge(store, thesis='', min_evidence=None, evidence=None):
    """M7 最难挑战：该命题验证 N 次、多数表现不支持 → 提出「也许前提要改」。

    证据只用本地已采集的表现：platform_work 的播放（同账号内中位数做基准）。
    改不改由本人定；证据不足不硬挑战（不判死刑、不 flattering）。
    深度教练层：只有 active 才做（云端下发了 brain.coach.challenge）；trial 返回 403 语义的未解锁。
    """
    if not prompt_ready('brain.coach.challenge'):
        return unavailable('brain.coach.challenge', '最难挑战')
    thesis = _flat(thesis)
    min_ev = int(min_evidence or CHALLENGE_MIN)
    if not thesis:
        return {'ok': False, 'reason': '没给命题（要先有一条可验证的主张）', 'threshold': min_ev}
    try:
        terms = [t for t in content_index._terms(thesis) if t]
    except Exception:  # noqa: BLE001
        terms = [t for t in re.split(r'[\s,，、。]+', thesis) if len(t) >= 2]
    works = []
    try:
        works = store.list('platform_work', 2000)
    except Exception:  # noqa: BLE001
        works = []
    if evidence:
        works = list(evidence)
    plays = [_latest_metric(w) for w in works if _latest_metric(w) > 0]
    matched = [w for w in works if _match_thesis(w, thesis, terms) and _latest_metric(w) > 0]
    if len(matched) < min_ev:
        return {'ok': False, 'reason': '证据不足：贴合这条的已发布作品只有 %d 条（要 %d 条）'
                                       % (len(matched), min_ev),
                'matched': len(matched), 'threshold': min_ev, 'thesis': thesis}
    median = statistics.median(plays) if plays else 0
    unsupported = [w for w in matched if _latest_metric(w) < median]
    supported = [w for w in matched if _latest_metric(w) >= median]
    if len(unsupported) < min_ev or len(unsupported) <= len(supported):
        return {'ok': False, 'reason': '证据还不足以挑战前提（不支持 %d 条 / 支持 %d 条，门槛 %d；中位 %s 播）'
                                       % (len(unsupported), len(supported), min_ev, median),
                'unsupported': len(unsupported), 'supported': len(supported),
                'median': median, 'threshold': min_ev, 'thesis': thesis}
    evidence_rows = ['《%s》%s 播' % (_clip(w.get('title'), 30), _latest_metric(w))
                     for w in sorted(unsupported, key=_latest_metric)[:min_ev]]
    data = '；'.join(evidence_rows)
    text = ('这条你验证了 %d 次，%d 次低于同账号中位（%s 播），证据多不支持：%s。'
            '也许不是你不够努力，是前提需要改了。改不改由你定——我只摆数据。'
            % (len(matched), len(unsupported), median, data))
    guarded = guard_text(text)
    return {'ok': True, 'text': guarded['text'], 'guard': guarded,
            'thesis': thesis, 'n': len(matched), 'n_unsupported': len(unsupported),
            'n_supported': len(supported), 'median': median, 'threshold': min_ev,
            'evidence': [{'id': w.get('id'), 'title': w.get('title') or '',
                          'plays': _latest_metric(w), 'url': w.get('source_url') or '',
                          'project_id': w.get('project_id') or ''} for w in matched],
            'prompt_key': 'brain.coach.challenge'}


def status():
    """诊断：提示词键/来源/解锁层级（给点体检用，不含任何用户内容与提示词正文）。"""
    try:
        info = prompt_store.status()
    except Exception:  # noqa: BLE001
        info = {}
    ready = {key: prompt_ready(key) for key in PROMPT_KEYS}
    return {'prompt_keys': sorted(PROMPT_KEYS), 'prompt_ready': ready,
            'prompt_origin': info.get('origin') or '',
            'coach_unlocked': coach_unlocked(), 'deep_unlocked': deep_unlocked(),
            'mode_threshold': INVITE_AFTER_TURNS, 'challenge_threshold': CHALLENGE_MIN,
            'stale_days': STALE_DAYS, 'psyche_terms': len(PSYCHE_TERMS)}
