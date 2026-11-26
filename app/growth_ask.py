"""Guided self-interview for the growth system.

Answers are the source of truth; growth fragments are projections.
No AI, no cloud calls, no second goal tree.
"""
import hashlib
import json
import re
from pathlib import Path

from db import Conflict, now, uid
from growth_service import Growth, LOCK, PILLARS, text as clean_text
import question_intents

ASSETS = Path(__file__).resolve().parent / 'assets'
QUESTION_FILE = ASSETS / 'growth_questions_v1.json'
PILLAR_NAMES = [name for name, _color in PILLARS]
MAX_ROUND = 5
STATUSES = ('answered', 'skipped', 'empty')


def _cloud_questions():
    """2026-11-17（t42）：74 题成长题库只会从云端下发；本地文件已是空壳。"""
    try:
        import asset_store
        value = asset_store.get('growth.questions')
    except Exception:
        value = None
    return value if isinstance(value, dict) else None


def load_questions():
    cloud = _cloud_questions()
    if cloud and cloud.get('questions'):
        data = cloud
    else:
        data = json.loads(QUESTION_FILE.read_text(encoding='utf-8'))
    questions = data.get('questions') or []
    if questions and data.get('total') not in (None, len(questions)):
        raise ValueError('题库总数与题目数量不一致。')
    if not questions:
        return data
    ids = [q.get('id') for q in questions]
    if len(ids) != len(set(ids)) or any(not i for i in ids):
        raise ValueError('题库存在空 ID 或重复 ID。')
    for q in questions:
        if q.get('pillar') not in PILLAR_NAMES or q.get('level') not in ('L1', 'L2', 'L3'):
            raise ValueError('题库层级或支柱不合法：' + str(q.get('id')))
    return data


def questions():
    return load_questions().get('questions') or []


def question(key):
    for item in questions():
        if item['id'] == key:
            return item
    raise KeyError(key)


def answer_key(question_id):
    return 'growth_answer_' + hashlib.sha256(question_id.encode('utf-8')).hexdigest()[:20]


def fragment_key(key):
    return 'growth_fragment_a_' + key[len('growth_answer_'):]


def public_question(item):
    return {k: item.get(k) for k in
            ('id', 'text', 'level', 'pillar', 'stage', 'tags', 'sensitive', 'onboarding')}


def active_pillars(goals):
    result = []
    for expected, _name in enumerate(PILLARS):
        candidates = [g for g in goals
                      if g.get('level') == 'pillar' and not g.get('archived')
                      and g.get('sort_order') == expected]
        if not candidates:
            candidates = [g for g in goals
                          if g.get('level') == 'pillar' and not g.get('archived')
                          and g.get('title') == _name]
        if candidates:
            result.append(candidates[0]['id'])
    return result


def has_setup(goals):
    return any(g.get('level') == 'root' for g in goals)


def answers(store):
    return Growth(store).all('growth_answer')


def answer_map(store):
    return {a['question_id']: a for a in answers(store)}


def progress(store):
    growth = Growth(store)
    qs = questions()
    existing = answer_map(store)
    sessions = growth.all('growth_ask_session')
    active = next((s for s in sessions if s.get('status') == 'active'), None)
    counts = {'answered': 0, 'skipped': 0, 'empty': 0, 'pending': 0, 'projected': 0, 'disabled': 0}
    for item in qs:
        record = existing.get(item['id'])
        if not record:
            continue
        status = record.get('status')
        if status in counts:
            counts[status] += 1
        if status == 'answered':
            projection = record.get('projection_status', 'pending')
            if projection in counts:
                counts[projection] += 1
    return {
        'total': len(qs),
        'onboarding_pool': sum(bool(q.get('onboarding')) for q in qs),
        'counts': counts,
        'unanswered': sum(q['id'] not in existing for q in qs),
        'first_use': not existing and not sessions,
        'active_session': active,
        'has_goals': has_setup(growth.all('growth_goal')),
    }


def _score(item, existing):
    record = existing.get(item['id'])
    if record and record.get('status') in ('answered', 'empty'):
        return None
    if record and record.get('status') == 'skipped':
        rank = 0
    elif item.get('onboarding'):
        rank = 1
    else:
        rank = 2
    level = {'L1': 0, 'L2': 1, 'L3': 2}[item['level']]
    return (rank, level, item['id'])


def select_round(store):
    """本轮取 5 题：按意图组去重，同一件事只问一次（t197）。

    去重键是「意图组 + 已答持久状态」：答案按题 ID 全局落库（answer_key），
    所以成长链、每日提问与访谈链共用同一份已答状态——组内任一题答过或跳过，
    整组不再换一种问法重问。规则见 question_intents.GROWTH_GROUPS。
    """
    existing = answer_map(store)
    blocked = question_intents.growth_blocked_ids(existing)
    candidates = []
    for item in questions():
        if item['id'] in blocked:
            continue
        score = _score(item, existing)
        if score is not None:
            candidates.append((score, item))
    by_pillar = {name: [] for name in PILLAR_NAMES}
    for score, item in sorted(candidates, key=lambda x: x[0]):
        by_pillar.setdefault(item['pillar'], []).append(item)
    picked = []
    picked_groups = set()
    while len(picked) < MAX_ROUND:
        added = False
        for name in PILLAR_NAMES:
            if len(picked) >= MAX_ROUND:
                break
            queue = by_pillar.get(name) or []
            while queue:
                item = queue.pop(0)
                if item['id'] in {x['id'] for x in picked}:
                    continue
                group = question_intents.growth_group_of(item['id'])
                # 同一轮里，同一意图组只出 1 题（等价题不能并排问）。
                if group and group in picked_groups:
                    continue
                picked.append(item)
                if group:
                    picked_groups.add(group)
                added = True
                break
        if not added:
            break
    return picked


def selection_audit(store):
    """把本轮选择与跳过原因摊开（自检/报告用，接口不返回）。"""
    existing = answer_map(store)
    closed = question_intents.closed_growth_groups(existing)
    picked = select_round(store)
    return {
        'picked': [q['id'] for q in picked],
        'closed_groups': closed,
        'blocked_ids': sorted(question_intents.growth_blocked_ids(existing)),
        'picked_groups': question_intents.group_ids_for([q['id'] for q in picked], 'growth'),
    }


def create_session(store):
    growth = Growth(store)
    sessions = growth.all('growth_ask_session')
    active = next((s for s in sessions if s.get('status') == 'active'), None)
    if active:
        return active
    picked = select_round(store)
    value = {'question_ids': [q['id'] for q in picked], 'position': 0,
             'status': 'active' if picked else 'completed', 'created_at': now(), 'updated_at': now()}
    return store.put('growth_ask_session', value, uid('growth_ask_session'))


def get_session(store, key):
    session = Growth(store).need('growth_ask_session', key)
    if session.get('status') != 'active':
        return {'session': session, 'current': None, 'progress': progress(store)}
    position = min(max(0, int(session.get('position', 0))), max(0, len(session.get('question_ids', [])) - 1))
    current = None
    if position < len(session.get('question_ids', [])):
        current = public_question(question(session['question_ids'][position]))
        record = store.get('growth_answer', answer_key(current['id']))
        if record:
            current['previous'] = {k: record.get(k) for k in ('status', 'text', 'probes', 'revision')}
    return {'session': session, 'current': current, 'progress': progress(store)}


def advance(store, session):
    position = int(session.get('position', 0)) + 1
    ids = session.get('question_ids', [])
    value = {**session, 'position': position,
             'status': 'active' if position < len(ids) else 'completed', 'updated_at': now()}
    return store.put('growth_ask_session', value, session['id'], session['revision'])


def save_answer(store, question_id, status, body_text, session_id=None, expected=None):
    if status not in STATUSES:
        raise ValueError('回答状态不正确。')
    item = question(question_id)
    key = answer_key(question_id)
    old = store.get('growth_answer', key) or {}
    if status == 'answered':
        value_text = clean_text(body_text, '回答', 40000, True)
    elif status == 'empty':
        value_text = clean_text(body_text or '没有 / 不知道', '回答', 4000, True)
    else:
        value_text = ''
    snapshot = public_question(item)
    history = list(old.get('history') or [])
    if old and old.get('text') != value_text:
        history.append({'text': old.get('text', ''), 'status': old.get('status', ''),
                        'saved_at': old.get('updated_at', '')})
    value = {
        'question_id': question_id,
        'question_version': 'v1',
        'question_snapshot': snapshot,
        'session_id': session_id or old.get('session_id', ''),
        'status': status,
        'text': value_text,
        'probes': [] if status != 'answered' else list(old.get('probes') or []),
        'history': history,
        'projection_status': old.get('projection_status', 'pending'),
        'fragment_id': old.get('fragment_id', ''),
        'answered_at': old.get('answered_at') or (now() if status == 'answered' else ''),
    }
    if status != 'answered' and old.get('fragment_id'):
        value['projection_status'] = 'disabled'
    saved = store.put('growth_answer', value, key, old.get('revision') if old is not None and expected is None else expected)
    projected = project_answer(store, saved)
    return projected


def should_probe(item, answer_text):
    if not item.get('probe') or not answer_text:
        return False
    if len(answer_text) < 30:
        return True
    if re.search(r'\d', answer_text):
        return True
    if re.search(r'我妈|我爸|父母|亲戚|合伙人|老婆|老公|伴侣|家人|朋友|老板|表哥|表姐', answer_text):
        return True
    if re.search(r'后来|结果|没想到|但是|从那以后|之后', answer_text):
        return True
    return False


def save_probe(store, key, answer_text, skip=False):
    record = store.get('growth_answer', key)
    if not record:
        raise KeyError(key)
    if record.get('status') != 'answered':
        raise ValueError('只有实际回答可以保存追问。')
    item = question(record['question_id'])
    prompt = item.get('probe')
    if not prompt:
        raise ValueError('这道题没有追问模板。')
    if not skip:
        if record.get('probes'):
            raise Conflict('这道题已经保存过追问。')
        value = clean_text(answer_text, '追问回答', 40000, True)
        record['probes'] = [{'question': prompt, 'answer': value}]
        saved = store.put('growth_answer', record, key, record['revision'])
    else:
        saved = record
    session_key = record.get('session_id')
    if session_key:
        session = store.get('growth_ask_session', session_key)
        if session and session.get('status') == 'active':
            advance(store, session)
    return project_answer(store, saved)


def fragment_body(record):
    item = record.get('question_snapshot') or question(record['question_id'])
    rows = ['原问题：' + item['text'], '', record.get('text', '')]
    for probe in record.get('probes') or []:
        rows += ['', '追问：' + probe.get('question', ''), '回答：' + probe.get('answer', '')]
    return '\n'.join(rows)


def project_answer(store, record):
    if not record or record.get('status') != 'answered':
        return record
    if record.get('projection_status') == 'disabled':
        return record
    growth = Growth(store)
    goals = growth.all('growth_goal')
    if not has_setup(goals):
        return store.patch('growth_answer', record['id'], {'projection_status': 'pending'}, record['revision'])
    pillars = active_pillars(goals)
    item = record.get('question_snapshot') or question(record['question_id'])
    try:
        pillar_index = PILLAR_NAMES.index(item.get('pillar'))
    except ValueError:
        pillar_index = 0
    goal_ids = []
    if pillar_index < len(pillars):
        goal_ids = [pillars[pillar_index]]
    key = fragment_key(record['id'])
    old = store.get('growth_fragment', key)
    payload = {
        'title': ('问自己 · ' + item['text'])[:160],
        'text': fragment_body(record),
        'source_url': '',
        'type': 'text',
        'goal_ids': (old.get('goal_ids') if old else None) or goal_ids,
        'recorded_at': record.get('answered_at') or now(),
        'endorsement': 'reference_only',
        'archived': False,
    }
    if old:
        payload['revision'] = old['revision']
        fragment = growth.fragment(payload, key=old['id'], answer=record)
    else:
        fragment = growth.fragment(payload, create_key=key, answer=record)
    latest = store.get('growth_answer', record['id'])
    return store.patch('growth_answer', latest['id'],
                       {'projection_status': 'projected', 'fragment_id': fragment['id']}, latest['revision'])


def project_pending(store):
    growth = Growth(store)
    goals = growth.all('growth_goal')
    if not has_setup(goals):
        return {'projected': 0, 'pending': sum(a.get('projection_status') == 'pending' for a in answers(store))}
    result = {'projected': 0, 'pending': 0}
    for record in answers(store):
        if record.get('status') != 'answered' or record.get('projection_status') != 'pending':
            continue
        fresh = project_answer(store, store.get('growth_answer', record['id']))
        if fresh and fresh.get('projection_status') == 'projected':
            result['projected'] += 1
        else:
            result['pending'] += 1
    return result


def update_answer(store, key, body):
    record = store.get('growth_answer', key)
    if not record:
        raise KeyError(key)
    expected = body.get('revision')
    if type(expected) is not int or expected != record['revision']:
        raise Conflict('答案已在另一页面更新，请刷新后再保存。')
    value = clean_text(body.get('text', ''), '回答', 40000, True)
    record['status'] = 'answered'
    record['text'] = value
    record['answered_at'] = record.get('answered_at') or now()
    saved = store.put('growth_answer', record, key, expected)
    return project_answer(store, saved)


def delete_answer(store, key, delete_fragment=False):
    record = store.get('growth_answer', key)
    if not record:
        raise KeyError(key)
    fragment_id = record.get('fragment_id')
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT revision FROM records WHERE kind='growth_answer' AND id=?", (key,)).fetchone()
        if not row or row['revision'] != record['revision']:
            raise Conflict('答案已更新，请刷新后再删除。')
        if delete_fragment and fragment_id:
            con.execute("DELETE FROM records WHERE kind='growth_fragment' AND id=?", (fragment_id,))
        con.execute("DELETE FROM records WHERE kind='growth_answer' AND id=?", (key,))
    if not delete_fragment and fragment_id:
        growth = Growth(store)
        old = store.get('growth_fragment', fragment_id)
        if old and old.get('origin_answer_id') == key:
            growth.store.patch('growth_fragment', fragment_id, {'origin_answer_id': '', 'origin_question_id': ''}, old['revision'])
    return {'deleted': True, 'fragment_deleted': bool(delete_fragment and fragment_id)}


def submit(store, session_key, body):
    session = Growth(store).need('growth_ask_session', session_key)
    if session.get('status') != 'active':
        raise Conflict('这一轮问答已经结束，请开新一轮。')
    question_id = str(body.get('question_id') or '')
    if question_id not in session.get('question_ids', []):
        raise ValueError('这道题不在当前问答轮次里。')
    if question_id != session['question_ids'][min(int(session.get('position', 0)), len(session['question_ids']) - 1)]:
        raise Conflict('请按当前题目提交，刷新后再试。')
    status = str(body.get('status') or '')
    saved = save_answer(store, question_id, status, body.get('text', ''), session['id'])
    result = {'answer': saved, 'session': session}
    if status == 'answered' and should_probe(question(question_id), saved.get('text', '')):
        result['probe'] = question(question_id).get('probe')
        return result
    result['session'] = advance(store, session)
    result['progress'] = progress(store)
    return result


def end_session(store, session_key):
    session = Growth(store).need('growth_ask_session', session_key)
    value = {**session, 'status': 'ended', 'updated_at': now()}
    return store.put('growth_ask_session', value, session['id'], session['revision'])


def get_answer(store, key):
    record = Growth(store).need('growth_answer', key)
    return {k: record.get(k) for k in
            ('id', 'revision', 'question_id', 'question_snapshot', 'status', 'text', 'probes',
             'answered_at', 'fragment_id', 'projection_status', 'updated_at')}


def route(handler, method, bits, query, store):
    if bits[:2] != ['api', 'growth']:
        return False
    tail = bits[2:]
    if method == 'GET' and tail == ['ask', 'progress']:
        return handler.json(progress(store))
    if method == 'GET' and len(tail) == 2 and tail[0] == 'answers':
        return handler.json(get_answer(store, tail[1]))
    if method == 'POST' and tail == ['ask', 'sessions']:
        return handler.json(create_session(store), 201)
    if method == 'GET' and len(tail) == 3 and tail[:2] == ['ask', 'sessions']:
        return handler.json(get_session(store, tail[2]))
    if method == 'POST' and len(tail) == 4 and tail[:2] == ['ask', 'sessions']:
        if tail[3] == 'answers':
            return handler.json(submit(store, tail[2], handler.body()), 201)
        if tail[3] == 'end':
            return handler.json(end_session(store, tail[2]))
    if method == 'PATCH' and len(tail) == 2 and tail[0] == 'answers':
        return handler.json(update_answer(store, tail[1], handler.body()))
    if method == 'POST' and len(tail) == 3 and tail[0] == 'answers':
        if tail[2] == 'probes':
            body = handler.body()
            return handler.json(save_probe(store, tail[1], body.get('answer', ''), bool(body.get('skip'))), 201)
        if tail[2] == 'delete':
            return handler.json(delete_answer(store, tail[1], bool(handler.body().get('delete_fragment'))))
    return False
