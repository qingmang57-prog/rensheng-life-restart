# -*- coding: utf-8 -*-
"""240 问分阶段诊断 · 题库解析与四阶段调度（需求单 A；t185 按 Codex 核查返工）。

t185 四点返工（把「弹药库不是问卷 / 不预设 / 不强迫」变成代码事实）：
  C1 代表题制：每阶段只要求 6-8 道**代表题**（REPRESENTATIVES 常量写死，取自该阶段各族的
     关键题），代表题处理完（回答或跳过/不适用）即可出卡；其余题为可选补充，
     补充后 profile/cards 刷新。240 题全量仍可按批取用（每批 ≤8）。
  C2 跳过入库：answer() 识别 skip=True、na=True（不适用）与明确跳过语（'不想说' 等），
     保存层落 skipped / na 一等状态；统计与画像 facts 不计入「已答事实」。
  C3 中性化与 gate 分流：阶段名与引导语中性（不再有「先摸清窟窿」式预设）；
     每阶段开头有 gate 题（是否有负债 / 是否有孩子 / 是否有伴侣 等）；
     gate 答否 → 该族剩余题自动标记 na（不适用），用户不需要逐题跳过。
     题库文件只读，不改一个字节；gate 为调度层的合成题，不写入题库。
  C4 行动卡真实化在 knowledge_cards.py。

提问纪律（写进代码，供测试断言）：
  不评判 / 不问隐私 / 不想说就跳过 / 不问提问者经历；链式追问是规则，不调模型。
"""

import re
import time
from pathlib import Path

import question_intents

BANK_PATH = Path(__file__).resolve().parent / 'assets' / 'diagnosis_questions_240.md'

# ---- 阶段定义（t185：名称与引导语中性化，去掉「先摸清窟窿」式预设）----
STAGES = [
    {'id': 1, 'code': 'S1', 'name': '现状盘点', 'card': 'debt_profile',
     'families': [1, 2], 'batch': 8, 'card_title': '现状与负债卡',
     'intro': '先把你现在的收入、支出和负债情况摸一遍；不想说的题可以直接跳过。'},
    {'id': 2, 'code': 'S2', 'name': '能力与过往', 'card': 'ability_profile',
     'families': [3, 4, 5], 'batch': 8, 'card_title': '能力与过往卡',
     'intro': '再看你靠什么赚过钱、哪次摔得最狠、付出了什么代价。'},
    {'id': 3, 'code': 'S3', 'name': '反复出现的模式', 'card': 'diagnosis_report',
     'families': [6, 7, 12], 'batch': 8, 'card_title': '模式卡',
     'intro': '看看你在关键决定、日常习惯和自我判断上有没有重复出现的模式。'},
    {'id': 4, 'code': 'S4', 'name': '资源与下一步', 'card': 'action_plan',
     'families': [8, 9, 10, 11], 'batch': 8, 'card_title': '资源与下一步',
     'intro': '最后看身边能借力的人、手上的资源、现在的状态和你想达成的目标。'},
]

# ---- C1：代表题（每阶段 6-8 题；规则可解释：各族各取前 2 题，再按需补关键题）----
# 取值依据：各族 20 题按「先现状、再结构」排列，前 2 题是最基础的事实题；
# S4 各族各取 2 题（共 8 题），覆盖关系/资源/状态/目标四个维度。
REPRESENTATIVES = {
    1: [1, 2, 4, 5, 21, 22, 39, 40],        # 收入/支出/欠款/最急一笔 + 债主结构/清债意愿
    2: [41, 42, 61, 62, 81, 82],            # 靠什么赚钱/赚最多 + 跌下来的原因 + 代价
    3: [101, 102, 121, 122, 221, 222],      # 决策/止损 + 行为习惯 + 自我盲区
    4: [141, 142, 161, 162, 181, 182, 201, 202],   # 关系/资源/状态/目标 各 2 题
}

# ---- C3（t186）：按**题目ID**的适用条件清单 ----
# 说明：清单由题库题面逐条筛出（欠债/伴侣/孩子三类），只对清单内的题号落 na；
# 「整族标 na」的旧路径已删除（它会误屏蔽父母/朋友等适用题）。
#   gate_s1          → 无负债：所有直接问欠款/债主/还款/逾期的题（含代表题 4/5/21/39）
#   gate_s4_partner  → 无伴侣：所有预设伴侣的题（141/142/144/145/146/147/149/151/152）
#   gate_s4_kids     → 无孩子：158
#   gate_s4_people   → 身边没人可商量：**不屏蔽任何题目**（父母/朋友/同事都可能适用），故清单为空
APPROVAL_CONDITIONS = {
    'gate_s1': [4, 5, 16, 21, 24, 25, 26, 27, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 142],
    'gate_s4_partner': [141, 142, 144, 145, 146, 147, 149, 151, 152],
    'gate_s4_kids': [158],
    'gate_s4_people': [],
}

# gate 题（调度层合成，不写入题库）。on_no 字段已废弃，统一走 APPROVAL_CONDITIONS。
GATES = {
    1: {'id': 'gate_s1', 'text': '你现在有负债吗？（没有就选「没有」，和债务有关的题会自动跳过）',
        'families': [1, 2]},
    4: {'id': 'gate_s4_people', 'text': '你现在身边有可以商量事的家人或朋友吗？（没有也能继续）',
        'families': [8, 9, 10, 11]},
    4.1: {'id': 'gate_s4_partner',
          'text': '你现在有伴侣吗？（没有就选「没有」，涉及伴侣的题会自动跳过）',
          'families': [8]},
    4.2: {'id': 'gate_s4_kids', 'text': '你有小孩吗？（没有就选「没有」，涉及孩子的题会自动跳过）',
          'families': [8]},
}
S4_GATES = [GATES[4], GATES[4.1], GATES[4.2]]

FAMILY_RE = re.compile(r'^#\s*族\s*(\d+)\s*·\s*([^（(]+)[（(]\s*(\d+)\s*问\s*[）)]\s*$')
QUESTION_RE = re.compile(r'^\s*(\d{1,3})[.、]\s*(.+?)\s*$')

BANNED_TERMS = [
    '老婆', '老公', '媳妇', '丈夫', '妻子', '我妈', '我爸', '你妈', '你爸',
    '儿子', '女儿', '闺女', '做互联网', '剪片子', '资金盘', '养青蛙', '表哥',
]

_NUMBER_HINT = ('多少', '几', '多久', '几个月', '几年', '多大', '几次')
# C2：明确跳过语（保存层识别，不再只用于 followup）
_SKIP_WORDS = ('不想说', '不想答', '跳过', '不答', 'pass', '不用问', '不聊这个', '不方便说')
NO_WORDS = ('没有', '没', '无', '不是', '不适用', 'n/a', 'na', '不欠', '没欠', '没有负债')


class BankError(RuntimeError):
    """题库缺失/格式不符：宁可报错，也不编题。"""


class StageLocked(RuntimeError):
    code = 'stage_locked'


_BANK_CACHE = {}


def parse_bank(text):
    families = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = FAMILY_RE.match(line)
        if m:
            current = {'no': int(m.group(1)), 'name': m.group(2).strip(),
                       'expected': int(m.group(3)), 'questions': []}
            families.append(current)
            continue
        mq = QUESTION_RE.match(line)
        if mq and current is not None:
            current['questions'].append({'no': int(mq.group(1)), 'text': mq.group(2)})
    if len(families) != 12:
        raise BankError('题库应有 12 族，实际解析到 %d 族。' % len(families))
    by_no = {}
    total = 0
    for fam in families:
        if len(fam['questions']) != fam['expected']:
            raise BankError('族 %d 应有 %d 问，实际 %d 问。'
                            % (fam['no'], fam['expected'], len(fam['questions'])))
        for q in fam['questions']:
            q['family'] = fam['no']
            q['family_name'] = fam['name']
            if q['no'] in by_no:
                raise BankError('题号 %d 重复。' % q['no'])
            by_no[q['no']] = q
            total += 1
    if total != 240:
        raise BankError('题库应有 240 问，实际 %d 问。' % total)
    nums = sorted(by_no)
    if nums != list(range(1, 241)):
        raise BankError('题号必须是从 1 到 240 的连续编号。')
    return {'families': families, 'by_no': by_no, 'total': total}


def load_bank(path=None):
    key = str(path or BANK_PATH)
    if key in _BANK_CACHE:
        return _BANK_CACHE[key]
    target = Path(path) if path else BANK_PATH
    if not target.is_file():
        raise BankError('题库文件不存在：%s（需要 240 问题库原样放入 app/assets/）。' % target.name)
    bank = parse_bank(target.read_text(encoding='utf-8'))
    _BANK_CACHE[key] = bank
    return bank


def stage_def(stage_id):
    for s in STAGES:
        if s['id'] == int(stage_id):
            return s
    raise ValueError('阶段不存在：%s（只有 1-4）。' % stage_id)


def stage_questions(stage_id):
    bank = load_bank()
    fams = stage_def(stage_id)['families']
    out = []
    for fam in bank['families']:
        if fam['no'] in fams:
            out.extend(fam['questions'])
    return out


def gate_questions(stage_id):
    sid = int(stage_id)
    if sid == 1:
        return [GATES[1]]
    if sid == 4:
        return list(S4_GATES)
    return []


def representatives(stage_id):
    """代表题号（按题库存在性过滤，防止写错号）。"""
    bank = load_bank()
    valid = set(bank['by_no'])
    return [n for n in REPRESENTATIVES.get(int(stage_id), []) if n in valid]


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _session(store, session_id):
    rec = store.get('diagnosis_session', session_id)
    if not rec:
        raise ValueError('诊断会话不存在：%s' % session_id)
    return rec


def _answers(store, session_id):
    rows = [a for a in store.list('diagnosis_answer', 5000) if a.get('session_id') == session_id]
    rows.sort(key=lambda a: (a.get('stage') or 0, a.get('answered_at') or ''))
    return rows


def _status_of(row):
    """C2：一等状态 —— answered / skipped / na。"""
    if row.get('na'):
        return 'na'
    if row.get('skipped'):
        return 'skipped'
    return 'answered'


def _answered_ids(store, session_id):
    return {int(a['question_no']) for a in _answers(store, session_id) if a.get('question_no')}


def _pick_session(store):
    """t203（缺陷⑧）：没有指定会话时，「当前该看的那个会话」怎么选。

    客户现场的库里有两条会话：一条做完的（complete），一条旧版「点开始」误建的空白 running。
    只按 status 挑 running 会一直停在空档案上，做完的画像卡永远看不到——所以要按「有没有真答过」
    分层，而不是只按状态：

      1) 进行中、且已经答过题的会话（正在做，继续答）；
      2) 已完成的会话（做完的，随时回看画像卡）；
      3) 一条都没答过的进行中会话（刚开的新一轮，或旧版误建的空白会话）。

    同层里取最近更新的；updated_at 只精确到秒，同一秒多个按 id（毫秒时间戳）兜底，保证每次都选同一个。
    返回 None 表示真·还没开始。
    """
    recs = [r for r in store.list('diagnosis_session', 200) if r.get('id')]
    if not recs:
        return None
    answered = {}
    for row in store.list('diagnosis_answer', 5000):
        sid = row.get('session_id')
        if sid and str(row.get('text') or '').strip() and not row.get('skipped') and not row.get('na'):
            answered[sid] = answered.get(sid, 0) + 1

    def tier(rec):
        if rec.get('status') == 'running' and answered.get(rec['id']):
            return 2
        if rec.get('status') == 'complete':
            return 1
        return 0

    recs.sort(key=lambda r: (tier(r), str(r.get('updated_at') or ''), str(r['id'])), reverse=True)
    return recs[0]


def start(store, resume=True):
    """开始诊断。t203：已有会话就复用，不新建。

    - resume=True（默认，前端当前就是这条）：有进行中 → 续答；只有已完成的 → 返回那次会话
      （status=complete，前端拿到就渲染画像卡回看），**不再造新 sid 造出空档案**。
    - resume=False：明确「另起一轮」时才建新会话（目前前端不用，留给以后加「重新做一轮」按钮，
      免得这里变成「一辈子只能做一次诊断」的硬锁）。
    """
    if resume:
        rec = _pick_session(store)
        if rec is not None:
            return {'session_id': rec['id'], 'reused': True, 'resumed': True,
                    **state(store, rec['id'])}
    sid = 'diag_' + str(int(time.time() * 1000))
    rec = {'id': sid, 'status': 'running', 'stage': 1,
           'stage_status': {str(s['id']): 'pending' for s in STAGES},
           'card_ack': {}, 'gates': {}, 'started_at': _now(), 'updated_at': _now()}
    rec['stage_status']['1'] = 'running'
    store.put('diagnosis_session', rec, sid)
    return {'session_id': sid, 'reused': False, 'resumed': False, **state(store, sid)}


def _kept(rows, stage_id):
    """该阶段真正可计入事实的行（answered 且非跳过）。"""
    return [r for r in rows if r.get('stage') == stage_id and _status_of(r) == 'answered'
            and str(r.get('text') or '').strip()]


def _dedupe_state(rows):
    """t197：跨阶段题意去重状态。

    同一次诊断里，只要某个意图组里有一题被摸过（answered / skipped / na 都算），
    该组的其它题就不再问——不重复问同一件事（含恢复/跨轮次/跨阶段）。
    返回 (closed_by, deduped_nos)：closed_by 是 {组ID: 先摸过的那题号}；
    deduped_nos 是「本该可问、但因同组已问过而跳过」的题号集合。
    """
    touched_all = {int(r['question_no']) for r in rows if r.get('question_no')}
    _groups, closed_by = question_intents.closed_diagnosis_groups(touched_all)
    deduped = {}
    for no in touched_all:
        for other in question_intents.diagnosis_members(question_intents.diagnosis_group_of(no) or ''):
            try:
                other = int(other)
            except (TypeError, ValueError):
                continue
            if other in touched_all:
                continue
            reason = question_intents.diagnosis_skip_reason(other, closed_by)
            if reason:
                deduped[other] = reason
    return closed_by, deduped


def _stage_progress(store, rec, stage_id):
    """C1：finished 只看**代表题**是否处理完（回答/跳过/不适用都算处理完），不看整族。

    t197：因同组题意已问过而被去重跳过的代表题，也算「处理完」（否则阶段永远出不了卡）。
    """
    rows = _answers(store, rec['id'])
    qs = stage_questions(stage_id)
    by_no = {q['no']: q for q in qs}
    reps = representatives(stage_id)
    touched = {int(r['question_no']) for r in rows if r.get('stage') == stage_id}
    _closed, deduped = _dedupe_state(rows)
    handled = [n for n in reps if n in touched or n in deduped]
    answered = {int(r['question_no']) for r in _kept(rows, stage_id)}
    skipped = {int(r['question_no']) for r in rows
               if r.get('stage') == stage_id and _status_of(r) == 'skipped'}
    na = {int(r['question_no']) for r in rows
          if r.get('stage') == stage_id and _status_of(r) == 'na'}
    gates = [g for g in gate_questions(stage_id)
             if any(int(r['question_no']) == 0 and r.get('gate_id') == g['id'] for r in rows)]
    gate_done = len(gates) >= len(gate_questions(stage_id))
    finished = len(handled) >= len(reps) and gate_done
    deduped_here = {n for n in deduped if n in by_no}
    return {'stage': stage_id, 'total': len(qs), 'answered': len(answered & set(by_no)),
            'skipped': len(skipped & set(by_no)), 'na': len(na & set(by_no)),
            'deduped': len(deduped_here),
            'representatives_total': len(reps), 'representatives_handled': len(handled),
            'remaining': max(0, len(reps) - len(handled)),
            'supplementary_remaining': max(0, len(qs) - len(handled) - len(answered & set(by_no))
                                            - len(skipped & set(by_no)) - len(na & set(by_no))
                                            - len(deduped_here)),
            'gate_done': gate_done, 'finished': finished}


def next_batch(store, session_id, stage=None):
    """取一批题（≤8）：先 gate，再代表题，最后补充题。"""
    rec = _session(store, session_id)
    want = int(stage or rec.get('stage') or 1)
    cur = int(rec.get('stage') or 1)
    if want > cur and not rec.get('card_ack', {}).get(str(cur)):
        prev = stage_def(cur)
        raise StageLocked('上一阶段（%s · %s）的卡还没确认，不能跳到第 %d 阶段。'
                          % (prev['code'], prev['name'], want))
    rows = _answers(store, session_id)
    touched = {int(r['question_no']) for r in rows if r.get('stage') == want}
    gate_done_ids = {r.get('gate_id') for r in rows if r.get('stage') == want}
    _closed, deduped = _dedupe_state(rows)
    deduped_notes = []
    batch = []
    for g in gate_questions(want):
        if g['id'] not in gate_done_ids and len(batch) < 8:
            batch.append({'no': 0, 'gate_id': g['id'], 'text': g['text'], 'family': 0,
                          'family_name': '前置确认', 'representative': False, 'gate': True})
    reps = [n for n in representatives(want) if n not in touched]
    qmap = {q['no']: q for q in stage_questions(want)}
    for n in reps:
        if len(batch) >= 8:
            break
        if n in deduped:
            deduped_notes.append({'no': n, 'reason': deduped[n]})
            continue
        q = qmap[n]
        batch.append({'no': n, 'text': q['text'], 'family': q['family'],
                      'family_name': q['family_name'], 'representative': True, 'gate': False})
    # 补充题：不适用的题已被 gate 逐题写成 na（touched 会包含它们），这里不需要再按族屏蔽；
    # t197：同组题意已问过的题直接跳过，并在 deduped 里说明原因。
    for q in stage_questions(want):
        if len(batch) >= 8:
            break
        if q['no'] in touched or q['no'] in reps:
            continue
        if q['no'] in deduped:
            deduped_notes.append({'no': q['no'], 'reason': deduped[q['no']]})
            continue
        batch.append({'no': q['no'], 'text': q['text'], 'family': q['family'],
                      'family_name': q['family_name'], 'representative': False, 'gate': False})
    prog = _stage_progress(store, rec, want)
    # deduped 只用于解释「这题为什么不问了」；同题在多轮都算一次，按题号排序并封顶。
    deduped_notes = sorted({int(x['no']): x for x in deduped_notes}.values(),
                           key=lambda x: x['no'])[:20]
    return {'session_id': session_id, 'stage': want, 'stage_code': stage_def(want)['code'],
            'stage_name': stage_def(want)['name'], 'stage_intro': stage_def(want)['intro'],
            'batch_size': len(batch), 'questions': batch, 'progress': prog,
            'card_ready': prog['finished'], 'card': str(stage_def(want)['card']),
            'representatives': representatives(want),
            'deduped': deduped_notes,
            'supplementary_available': max(0, len(stage_questions(want)) - len(representatives(want)))}


def followup_for(question, text):
    clean = str(text or '').strip()
    if not clean or any(w in clean for w in _SKIP_WORDS):
        return None
    qtext = str((question or {}).get('text') or '')
    has_number = bool(re.search(r'\d', clean))
    if any(h in qtext for h in _NUMBER_HINT) and not has_number and len(clean) <= 40:
        return '你说的时候没提具体数字，给个大概就行（不用精确）。'
    if len(clean) <= 4:
        return '能再多说一句吗？一句话就够。'
    return None


def _is_skip_text(text):
    t = str(text or '').strip()
    return bool(t) and any(w in t for w in _SKIP_WORDS)


def _is_no(text):
    t = str(text or '').strip().lower()
    return t in NO_WORDS or any(t.startswith(w) for w in ('没有', '没', '不欠', '无')) and len(t) <= 8


def answer(store, session_id, question_no, text='', skip=False, na=False):
    """记录回答 / 跳过 / 不适用。C2：三者都是一等状态，落库可查。"""
    rec = _session(store, session_id)
    qno = int(question_no or 0)
    bank = load_bank()
    if qno == 0:
        raise ValueError('gate 题请调用 answer_gate()。')
    q = bank['by_no'].get(qno)
    if not q:
        raise ValueError('题号不存在：%s（题库只有 1-240）。' % question_no)
    stage_id = next((s['id'] for s in STAGES if q['family'] in s['families']), None)
    if not stage_id:
        raise ValueError('题号 %d 不属于任何阶段。' % qno)
    cur = int(rec.get('stage') or 1)
    if stage_id > cur:
        raise StageLocked('第 %d 阶段还没开始（上一阶段的卡未确认）。' % stage_id)
    status_skip = bool(skip) or _is_skip_text(text)
    status_na = bool(na)
    aid = 'ans_%s_%s' % (session_id, qno)
    row = {'id': aid, 'session_id': session_id, 'question_no': qno, 'stage': stage_id,
           'family': q['family'], 'question': q['text'],
           'text': '' if (status_skip or status_na) else str(text or '')[:4000],
           'skipped': status_skip, 'na': status_na, 'answered_at': _now()}
    store.put('diagnosis_answer', row, aid)
    return _after_answer(store, rec, session_id, stage_id, q, row)


def answer_gate(store, session_id, gate_id, text='', no=False):
    """C3：gate 题（是否有负债 / 是否有孩子 / 是否有伴侣…）。答否 → 对应族标 na。"""
    rec = _session(store, session_id)
    gate = None
    for sid in (1, 4):
        for cand in gate_questions(sid):
            if cand['id'] == gate_id:
                gate = cand
                gate_stage = sid
                break
        if gate:
            break
    if not gate:
        raise ValueError('未知的 gate：%s' % gate_id)
    no = bool(no) or _is_no(text)
    aid = 'gate_%s_%s' % (session_id, gate_id)
    row = {'id': aid, 'session_id': session_id, 'question_no': 0, 'stage': gate_stage,
           'family': 0, 'question': gate['text'], 'gate_id': gate_id,
           'gate_no': {'condition_ids': sorted(APPROVAL_CONDITIONS.get(gate_id) or [])},
           'text': str(text or '')[:400],
           'skipped': False, 'na': False, 'gate_negative': bool(no), 'answered_at': _now()}
    store.put('diagnosis_answer', row, aid)
    rec.setdefault('gates', {})[gate_id] = bool(no)
    store.put('diagnosis_session', rec, session_id)
    applied = []
    if no:
        # t186：只对清单内的题号落 na，逐题写库（不再按族批量）。
        listed = set(APPROVAL_CONDITIONS.get(gate_id) or [])
        bank = load_bank()
        for qno in sorted(listed):
            if qno not in bank['by_no']:
                continue
            if qno in _answered_ids(store, session_id):
                continue
            q = bank['by_no'][qno]
            na_row = {'id': 'ans_%s_%s' % (session_id, qno), 'session_id': session_id,
                      'question_no': qno,
                      'stage': next((st['id'] for st in STAGES if q['family'] in st['families']), gate_stage),
                      'family': q['family'], 'question': q['text'], 'text': '',
                      'skipped': False, 'na': True, 'na_reason': 'gate:%s' % gate_id,
                      'answered_at': _now()}
            store.put('diagnosis_answer', na_row, na_row['id'])
            applied.append(qno)
    prog = _stage_progress(store, rec, gate_stage)
    return {'ok': True, 'session_id': session_id, 'gate_id': gate_id, 'negative': bool(no),
            'na_applied': applied, 'na_applied_families': [],
            'progress': prog, 'card_ready': prog['finished'],
            'next': next_batch(store, session_id, gate_stage)}


def _after_answer(store, rec, session_id, stage_id, q, row):
    prog = _stage_progress(store, rec, stage_id)
    if prog['finished'] and rec['stage_status'].get(str(stage_id)) != 'complete':
        rec['stage_status'][str(stage_id)] = 'complete'
        rec['updated_at'] = _now()
        store.put('diagnosis_session', rec, session_id)
    follow = None if (_status_of(row) != 'answered') else followup_for(q, row.get('text'))
    return {'ok': True, 'session_id': session_id, 'stage': stage_id,
            'question_no': q['no'], 'status': _status_of(row),
            'skipped': bool(row.get('skipped')), 'na': bool(row.get('na')),
            'progress': prog, 'card_ready': prog['finished'], 'followup': follow,
            'next': next_batch(store, session_id, stage_id) if not prog['finished'] else None}


def ack_card(store, session_id, stage=None):
    """确认卡 → 推进阶段。C1：只要求代表题处理完（不再要求整族）。"""
    rec = _session(store, session_id)
    sid_stage = int(stage or rec.get('stage') or 1)
    prog = _stage_progress(store, rec, sid_stage)
    if not prog['finished']:
        if not prog.get('gate_done'):
            raise ValueError('这一阶段的前置确认（gate）还没答，先答它才能出卡。')
        raise ValueError('还有 %d 道代表题没处理（跳过也算处理），处理完就能出卡。'
                         % prog['remaining'])
    rec.setdefault('card_ack', {})[str(sid_stage)] = _now()
    rec['stage_status'][str(sid_stage)] = 'done'
    if sid_stage >= 4:
        rec['stage'] = 4
        rec['status'] = 'complete'
        rec['completed_at'] = _now()
    else:
        rec['stage'] = sid_stage + 1
        rec['stage_status'][str(sid_stage + 1)] = 'running'
    rec['updated_at'] = _now()
    store.put('diagnosis_session', rec, session_id)
    return {'ok': True, 'session_id': session_id, 'stage': rec['stage'],
            'status': rec['status'],
            'next_stage': None if rec['status'] == 'complete' else rec['stage']}


def state(store, session_id=None):
    if session_id:
        rec = _session(store, session_id)
    else:
        rec = _pick_session(store)  # t203：没有进行中的会话时回落到最近完成的，别再报 idle
        if rec is None:
            return {'status': 'idle', 'session_id': None}
    stages = []
    for s in STAGES:
        prog = _stage_progress(store, rec, s['id'])
        stages.append({'id': s['id'], 'code': s['code'], 'name': s['name'],
                       'intro': s['intro'],
                       'status': rec.get('stage_status', {}).get(str(s['id']), 'pending'),
                       'answered': prog['answered'], 'total': prog['total'],
                       'representatives_total': prog['representatives_total'],
                       'representatives_handled': prog['representatives_handled'],
                       'remaining': prog['remaining'],
                       'card': s['card'],
                       'card_acked': bool(rec.get('card_ack', {}).get(str(s['id'])))})
    return {'status': rec.get('status'), 'session_id': rec['id'], 'stage': rec.get('stage'),
            'stages': stages, 'started_at': rec.get('started_at'), 'updated_at': rec.get('updated_at')}


def answers_map(store, session_id):
    return {int(a['question_no']): a for a in _answers(store, session_id) if a.get('question_no')}


def profile(store, session_id=None):
    """卡只在阶段代表题处理完后出现；未完成时明确「进行中」，不给半成品。"""
    st = state(store, session_id)
    if st.get('status') == 'idle':
        return {'status': 'idle', 'message': '还没有开始的诊断。', 'stages': [], 'cards': []}
    sid = st['session_id']
    rows = answers_map(store, sid)
    rec = _session(store, sid)
    import knowledge_cards
    cards = []
    for s in STAGES:
        prog = _stage_progress(store, rec, s['id'])
        if not prog['finished']:
            continue
        cards.append(knowledge_cards.build(s['card'], s, rows,
                                           confirm_state=(rec.get('card_ack', {}) or {}).get(str(s['id'])),
                                           corrections=(rec.get('card_corrections', {}) or {}).get(str(s['id']))))
    complete = st['status'] == 'complete'
    return {'status': 'complete' if complete else 'in_progress',
            'session_id': sid, 'stage': st['stage'], 'stages': st['stages'], 'cards': cards,
            'message': ('诊断已完成。' if complete
                        else '还在采集（第 %d 阶段 · %s）。代表题处理完就能出这张卡，剩下的题可选补充。'
                             % (int(st['stage'] or 1), stage_def(st['stage'] or 1)['name']))}


def confirm_card(store, session_id, stage, action='confirm', corrections=None):
    """C4：卡片确认 / 修正状态位（不阻塞后续阶段，仅记录用户对卡的确认）。"""
    rec = _session(store, session_id)
    sid_stage = str(int(stage))
    if action == 'correct':
        rec.setdefault('card_corrections', {})[sid_stage] = {
            'corrected_at': _now(), 'items': corrections or []}
    else:
        rec.setdefault('card_confirm', {})[sid_stage] = _now()
    rec['updated_at'] = _now()
    store.put('diagnosis_session', rec, session_id)
    return {'ok': True, 'session_id': session_id, 'stage': int(stage), 'action': action,
            'confirmed': action != 'correct'}
