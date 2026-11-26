# -*- coding: utf-8 -*-
"""画像卡产出骨架（需求单 A；t185 按 Codex 核查返工）。

设计约束：
- **零 LLM**：全部按答案做确定性归类与措辞，不调模型、不猜用户身份。
- **不预设用户**：没说伴侣/孩子/负债就按「未提供」处理，卡里不替他补。
- **不评判**：只复述事实与可执行下一步，不评价对错、不贴人格标签。
- **C2 只计真实回答**：skipped / na 的行既不算事实、也不算资源。
- **C4 行动卡真实化**：
  · 统计文案是「已回答 N 条」，不再把回答条数说成「能借力的人 / 手上有用的东西」；
  · 否定回答（没有/没/无…）不计为资源；
  · 每条动作必须带 `basis`（引用具体题号+回答原话）；没有依据的写「待补充」；
  · `note` 不再声称已经给了方案，只说明卡的性质；
  · 每张卡带 `confirm` / `correct` 状态位，供前端做「确认这张卡 / 修正」。
"""

import re

CARD_TITLES = {
    'debt_profile': '现状与负债卡',
    'ability_profile': '能力与过往卡',
    'diagnosis_report': '模式卡',
    'action_plan': '资源与下一步',
}

STAGE_FAMILIES = {
    'debt_profile': {'cash': [1], 'debt': [2]},
    'ability_profile': {'skill': [3], 'fall': [4], 'cost': [5]},
    'diagnosis_report': {'decision': [6], 'character': [7], 'blind': [12]},
    'action_plan': {'people': [8], 'resource': [9], 'state': [10], 'goal': [11]},
}

NEGATIVE = ('没有', '没', '无', '不是', '不想', '不适用', '暂无', '谈不上', '说不清', '不确定')
_MONEY_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(万|千|百|块|元|w|W)?')


def _is_negative(text):
    """否定/拒答的答案不算资源、也不算依据。"""
    t = str(text or '').strip()
    if not t:
        return True
    return any(t.startswith(w) for w in NEGATIVE) and len(t) <= 12


def _rows(rows):
    """只保留真实回答（C2：skipped/na 不算）。"""
    out = []
    for no in sorted(rows or {}):
        row = rows[no]
        if row.get('skipped') or row.get('na'):
            continue
        if not str(row.get('text') or '').strip():
            continue
        out.append(row)
    return out


def _pick(rows, families):
    return [r for r in _rows(rows) if r.get('family') in families]


def _facts(rows, limit=6):
    facts = []
    for row in rows[:limit]:
        text = str(row.get('text') or '').strip()
        facts.append({'question_no': row.get('question_no'), 'question': row.get('question'),
                      'answer': text[:200], 'has_number': bool(_MONEY_RE.search(text)),
                      'is_negative': _is_negative(text)})
    return facts


def _basis(row, max_len=60):
    text = str(row.get('text') or '').strip().replace('\n', ' ')
    return {'question_no': row.get('question_no'), 'question': row.get('question'),
            'quote': text[:max_len]}


def _counts(rows, families):
    picked = _pick(rows, families)
    return {'answered': len(picked),
            'negative': len([r for r in picked if _is_negative(r.get('text'))])}


def build(card_type, stage, rows, confirm_state=None, corrections=None):
    families = []
    for fams in STAGE_FAMILIES.get(card_type, {}).values():
        families.extend(fams)
    picked = _pick(rows, families)
    card = {'type': card_type, 'title': CARD_TITLES.get(card_type, card_type),
            'stage': stage['id'], 'stage_code': stage['code'], 'stage_name': stage['name'],
            'status': 'ready', 'basis': _counts(rows, families),
            'facts': _facts(picked, 8), 'signals': [], 'note': '',
            'confirm': {'state': 'confirmed' if confirm_state else 'pending',
                        'confirmed_at': confirm_state},
            'correct': {'state': 'corrected' if corrections else 'none',
                        'items': corrections or []},
            'actions': [], 'pending': []}

    if card_type == 'debt_profile':
        card['signals'] = [
            {'key': 'answered', 'label': '已回答', 'value': card['basis']['answered']},
            {'key': 'with_numbers', 'label': '其中给了具体数字', 'value': len([f for f in card['facts'] if f['has_number']])},
        ]
        card['note'] = '这张卡只复述你自己说的数字和处境，不替你估；没说的部分标「未采集」。'
    elif card_type == 'ability_profile':
        c3, c4, c5 = (_counts(rows, [3]), _counts(rows, [4]), _counts(rows, [5]))
        card['signals'] = [
            {'key': 'skill', 'label': '「靠什么赚钱」已回答', 'value': c3['answered']},
            {'key': 'fall', 'label': '「跌下来的原因」已回答', 'value': c4['answered']},
            {'key': 'cost', 'label': '「付出的代价」已回答', 'value': c5['answered']},
        ]
        card['note'] = '能力只看你做过什么、做成了多久，不看头衔。'
    elif card_type == 'diagnosis_report':
        card['signals'] = [
            {'key': 'decision', 'label': '关键决定：已回答', 'value': _counts(rows, [6])['answered']},
            {'key': 'character', 'label': '行为习惯：已回答', 'value': _counts(rows, [7])['answered']},
            {'key': 'blind', 'label': '自我盲区：已回答', 'value': _counts(rows, [12])['answered']},
        ]
        card['note'] = '只归纳你自己说过的重复模式，不做人格判断；哪条不像你，点「修正」告诉我。'
    elif card_type == 'action_plan':
        card = _action_plan(card, rows)
    card['render'] = render_payload(card)
    return card


def _action_plan(card, rows):
    """C4：动作必须有回答依据；否定回答不算资源；没依据写「待补充」。"""
    people = _pick(rows, [8])
    resource = _pick(rows, [9])
    state = _pick(rows, [10])
    goals = _pick(rows, [11])

    def usable(picked):
        return [r for r in picked if not _is_negative(r.get('text'))]

    people_ok, resource_ok = usable(people), usable(resource)
    card['signals'] = [
        {'key': 'answered_people', 'label': '「身边人」已回答', 'value': len(people)},
        {'key': 'usable_people', 'label': '其中不是否定的', 'value': len(people_ok)},
        {'key': 'answered_resource', 'label': '「资源」已回答', 'value': len(resource)},
        {'key': 'usable_resource', 'label': '其中不是否定的', 'value': len(resource_ok)},
        {'key': 'answered_state', 'label': '「当前状态」已回答', 'value': len(state)},
        {'key': 'answered_goal', 'label': '「目标」已回答', 'value': len(goals)},
    ]

    actions = []
    if goals and not _is_negative(goals[0].get('text')):
        actions.append({'action': '把最近 7 天的动作对齐到你自己写下的目标：%s'
                                  % str(goals[0].get('text'))[:60],
                        'basis': _basis(goals[0])})
    if people_ok:
        actions.append({'action': '找一个你说过能商量事的人，把现在的处境讲一遍（只讲事实）。',
                        'basis': _basis(people_ok[0])})
    if resource_ok:
        actions.append({'action': '先把你说过的这项资源盘清楚：能不能一周内换成现金或时间。',
                        'basis': _basis(resource_ok[0])})
    if state:
        actions.append({'action': '按你描述的当前状态安排节奏：先做一件今天就能做完的小事。',
                        'basis': _basis(state[0])})
    if not actions:
        card['pending'].append('还没有足够回答支撑一条具体动作（至少答一道目标 / 身边人 / 资源题）。')

    card['actions'] = actions
    card['note'] = ('这张卡只列出有回答依据的动作；没有依据的部分标「待补充」，不替你编。'
                    '确认或修正后，补充题再答会刷新这张卡。')
    return card


def render_payload(card):
    return {
        'title': card['title'],
        'badge': '%s · %s' % (card.get('stage_code') or '', card.get('stage_name') or ''),
        'summary': card.get('note') or '',
        'signals': [{'label': s['label'], 'value': s['value']} for s in card.get('signals') or []],
        'facts': [{'q': f.get('question_no'), 'question': f.get('question'),
                   'answer': f.get('answer'), 'has_number': f.get('has_number'),
                   'is_negative': f.get('is_negative')} for f in card.get('facts') or []],
        'actions': [{'action': a['action'], 'basis': a.get('basis')} for a in card.get('actions') or []],
        'pending': card.get('pending') or [],
        'basis': card.get('basis') or {},
        'confirm': card.get('confirm') or {},
        'correct': card.get('correct') or {},
        'disclaimer': '内容来自你的回答；没答的不推测，跳过的也不算。',
    }
