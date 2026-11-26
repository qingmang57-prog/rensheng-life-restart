# -*- coding: utf-8 -*-
"""第二大脑 · 深挖访谈。

分工（2026-11-16 定）：
- 模型只决定「这一轮问什么」，问句必须过服务端 6 条硬校验；
  不合规就回退到本地 74 题题库，不让模型自由发挥。
- 最多 3 层，第 3 层答完必须收口。
- 访谈原话落成 interview_session / interview_turn / creator_profile 三种记录，
  不建新表；原话只在用户点「一键应用」后才写进三观卡/成长档案/素材，
  应用与撤销都留日志，可一键回退。
- 默认走云端；设置里关掉后只问题库。访谈不扣积分。
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import content_index
import growth_ask
import prompt_store
import providers
from db import now, uid

TZ = timezone(timedelta(hours=8))
MAX_LEVEL = 3
PROFILE_ID = 'main'

TARGETS = {
    'value': ('knowledge', '三观卡 / 认知条目'),
    'self_view': ('flow', '成长档案'),
    'expression': ('source', '素材'),
}

KIND_NOTES = {
    'value': '价值观',
    'self_view': '自我认知',
    'expression': '可写稿的表达',
}

BANK_FALLBACK = {
    1: '最近一次让你心里不舒服、但嘴上说没事的事，是什么？',
    2: '那件事里，你最不想被别人看出来的那一层是什么？',
    3: '如果这段内容发出去，你最怕谁看到、怕他想到你什么？',
}

PRIVACY_WORDS = ('密码', '密钥', '验证码', '身份证', '银行卡', '住址', '手机号',
                 '微信', '邮箱', '私钥', '公钥')
DIAGNOSIS_WORDS = ('抑郁症', '焦虑症', '人格障碍', '心理疾病', '精神病', '你有病',
                   '违法', '犯罪', '犯法')
JUDGE_WORDS = ('你应该', '你就是', '你这种人', '你必须')
PARALLEL_WORDS = ('另外', '还有', '同时', '以及')
SAFETY_WORDS = ('自杀', '自残', '不想活', '结束生命', '伤害自己')
AVOID_WORDS = ('不想说', '不想聊', '跳过', '不知道', '换个话题', '换一个话题')


# ---------------------------------------------------------------- 记录读写
def _profile(store):
    rec = store.get('creator_profile', PROFILE_ID) or {}
    rec.setdefault('settings', {'cloud': True})
    rec.setdefault('pending', [])
    rec.setdefault('applied', [])
    rec.setdefault('remembered', [])
    return rec


def _settings(store):
    settings = _profile(store).get('settings') or {}
    return {'cloud': bool(settings.get('cloud', True))}


def save_settings(store, cloud):
    rec = _profile(store)
    rec['settings'] = {'cloud': bool(cloud)}
    rec['updated_at'] = now()
    store.put('creator_profile', rec, PROFILE_ID)
    return {'cloud': bool(cloud)}


def _turns(store, session_id):
    rows = [t for t in store.list('interview_turn', 3000) if t.get('session_id') == session_id]
    rows.sort(key=lambda x: int(x.get('seq') or 0))
    return rows


def _pid(kind, text):
    return 'p_' + hashlib.sha1((kind + '|' + ' '.join(str(text).split())).encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------- 问句校验（6 条）
def validate_question(text, last_answer, session, history_questions):
    """服务端 6 条硬校验。返回 (ok, reasons)。"""
    reasons = []
    raw = str(text or '')
    t = ' '.join(raw.split())
    if not t:
        return False, ['空问题']
    if len(t) < 6 or len(t) > 90:
        reasons.append('长度不在 6-90 字')
    if t.count('？') + t.count('?') > 1:
        reasons.append('一次问了不止一个问题')
    if not (t.endswith('？') or t.endswith('?')):
        reasons.append('不是问句')
    if any(w in t for w in PRIVACY_WORDS):
        reasons.append('索要隐私凭证')
    if any(w in t for w in DIAGNOSIS_WORDS):
        reasons.append('做诊断或定性')
    if any(w in t for w in JUDGE_WORDS):
        reasons.append('人身评价')
    if re.search(r'https?://|```|<[a-zA-Z/]|[{}]', t):
        reasons.append('含链接或代码')
    if any(w in t for w in PARALLEL_WORDS):
        reasons.append('并列追问')
    # 复述上一答：连续 16 字相同即视为鹦鹉学舌
    answer = ' '.join(str(last_answer or '').split())
    if len(answer) >= 16:
        for i in range(0, len(answer) - 15):
            if answer[i:i + 16] in t:
                reasons.append('复述上一答')
                break
    # 主题靠 payload 里的 theme + last_answer 约束；这里不再做字面「跑题」硬判：
    # 中文同义改写（报价/嫌贵 vs 不敢涨价）会被整词匹配误杀，实测把合规追问打回题库。
    if t in (history_questions or []):
        reasons.append('重复问过')
    return (not reasons), reasons


def _bank_pick(store, session, level):
    used = set(session.get('used_ids') or [])
    # t197：跨入口 / 已答持久去重——成长链答过或跳过过的同义题，访谈链不再问一遍；
    # 本场已经问过同组的另一题，也不再问同组（规则见 question_intents）。
    intents = growth_ask.question_intents
    try:
        blocked = intents.growth_blocked_ids(growth_ask.answer_map(store))
    except Exception:
        blocked = set()
    used_groups = {g for g in (intents.growth_group_of(i) for i in used) if g}
    pool = [q for q in growth_ask.questions()
            if q.get('enabled', True) and q.get('level') == 'L%d' % min(level, 3)]
    pool += [q for q in growth_ask.questions() if q.get('enabled', True)]
    for item in pool:
        if item['id'] in used or item['id'] in blocked:
            continue
        group = intents.growth_group_of(item['id'])
        if group and group in used_groups:
            continue
        used.add(item['id'])
        if group:
            used_groups.add(group)
        session['used_ids'] = sorted(used)
        return item['text'], item['id']
    return BANK_FALLBACK.get(min(level, 3), BANK_FALLBACK[3]), ''


def _ask(store, session, level, last_answer, switch=False):
    history = [t.get('question') for t in _turns(store, session['id']) if t.get('question')]
    prof = _profile(store)
    remembered = [m.get('text') for m in (prof.get('remembered') or [])][-6:]
    if session.get('mode') != 'bank' and providers.available()['llm']:
        payload = {
            # 2026-11-16 缓存：先放本场不变/只追加的内容（主题、上限、记住的事、最近问答），
            # 再放逐轮变化的 asked / 上一轮回答 / 本轮层级，让上一轮请求成为这一轮的前缀。
            'theme': session.get('theme') or '',
            'max_level': MAX_LEVEL,
            'remembered': remembered,
            'recent': [{'q': t.get('question'), 'a': str(t.get('answer') or '')[:600]}
                       for t in _turns(store, session['id'])[-3:]],
            'asked': history[-6:],
            'last_answer': '' if switch else str(last_answer or '')[:1500],
            'level': level,
        }
        if switch:
            payload['note'] = '对方不想继续这个话题，下一问换一个不同方向，不要追问上一轮。'
        try:
            data = providers.llm_json(prompt_store.get('interview.ask'), payload)
            text = str((data or {}).get('question') or '').strip()
            ok, reasons = validate_question(text, '' if switch else last_answer, session, history)
            if ok:
                return {'text': text, 'source': 'model', 'level': level, 'reasons': []}
            session.setdefault('fallbacks', []).append(
                {'at': now(), 'text': text[:160], 'reasons': reasons})
        except Exception as exc:
            session.setdefault('fallbacks', []).append(
                {'at': now(), 'error': '%s: %s' % (type(exc).__name__, str(exc)[:120])})
    text, ref = _bank_pick(store, session, level)
    return {'text': text, 'source': 'bank', 'ref': ref, 'level': level, 'reasons': []}


# ---------------------------------------------------------------- 收口与提炼
def _sentence_split(text):
    return [s.strip() for s in re.split(r'[。！？!?\n；;]+', str(text or '')) if len(s.strip()) >= 8]


def _heuristic_items(turns):
    items = []
    for turn in turns:
        for sent in _sentence_split(turn.get('answer')):
            kind = None
            if any(w in sent for w in ('我其实', '我一直', '我不敢', '我害怕', '我怕',
                                       '我不想让', '我不想承认', '心里')):
                kind = 'self_view'
            elif any(w in sent for w in ('我觉得', '我认为', '我信', '我看重', '底线',
                                         '宁可', '不值', '值得')):
                kind = 'value'
            elif len(sent) >= 22:
                kind = 'expression'
            if kind:
                items.append({'kind': kind, 'text': sent[:60], 'quote': sent[:200],
                              'turn_ids': [turn['id']]})
    return items


def _valid_item(item, answers_text):
    kind = item.get('kind')
    text = ' '.join(str(item.get('text') or '').split())
    quote = ' '.join(str(item.get('quote') or '').split())
    if kind not in TARGETS or not text or len(text) > 60:
        return None
    if not quote or len(quote) < 6:
        return None
    # 原句必须真的出现在回答里，模型改写过的直接丢掉，避免编造。
    if quote not in answers_text:
        return None
    return {'kind': kind, 'text': text, 'quote': quote[:300]}


def _dedup_items(items):
    """同一轮里模型常把一句原话拆成几条：短的被长的包含就只留大的。"""
    def norm(text):
        return re.sub(r'\s+', '', str(text or ''))
    order = sorted(range(len(items)), key=lambda i: -len(norm(items[i].get('text'))))
    kept = []
    for i in order:
        t = norm(items[i].get('text'))
        if any(t in norm(items[j].get('text')) or norm(items[j].get('text')) in t for j in kept):
            continue
        kept.append(i)
    return [items[i] for i in sorted(kept)]


def _extract(store, session, turns, use_model=True):
    answers_text = ' '.join(' '.join(str(t.get('answer') or '').split()) for t in turns)
    items, summary = [], ''
    if use_model and providers.available()['llm'] and answers_text:
        try:
            data = providers.llm_json(prompt_store.get('interview.extract'), {
                'theme': session.get('theme') or '',
                'turns': [{'q': t.get('question'), 'a': str(t.get('answer') or '')[:1500]}
                          for t in turns],
            })
            summary = str((data or {}).get('summary') or '').strip()[:400]
            raw = (data or {}).get('items')
            if isinstance(raw, list):
                for item in raw[:10]:
                    if isinstance(item, dict):
                        good = _valid_item(item, answers_text)
                        if good:
                            items.append(good)
        except Exception:
            items = []
    if not items:
        items = _heuristic_items(turns)
    items = _dedup_items(items)
    if not summary:
        summary = '这轮聊了 %d 层。原话都留着，下面是可以回填的条目。' % len(turns) if turns else '这轮没有留下可回填的内容。'
    caps = {'value': 3, 'self_view': 2, 'expression': 5}
    picked, seen = [], set()
    for kind in ('value', 'self_view', 'expression'):
        group = 0
        for item in items:
            if item.get('kind') != kind:
                continue
            key = _pid(kind, item['text'])
            if key in seen or group >= caps[kind]:
                continue
            seen.add(key)
            item = {**item, 'pid': key, 'target': TARGETS[kind][0],
                    'target_label': TARGETS[kind][1], 'note': KIND_NOTES[kind],
                    'session_id': session['id'], 'status': 'pending', 'created_at': now()}
            picked.append(item)
            group += 1
    return picked, summary


def _merge_pending(store, items):
    prof = _profile(store)
    have = {p.get('pid') for p in prof.get('pending') or []}
    added = 0
    for item in items:
        if item['pid'] in have:
            continue
        prof['pending'].append(item)
        have.add(item['pid'])
        added += 1
    prof['updated_at'] = now()
    store.put('creator_profile', prof, PROFILE_ID)
    return added


def _close(store, session, safety=False):
    turns = _turns(store, session['id'])
    if safety:
        items, summary = [], ('这段话不适合继续深挖，先停在这里。原话已保留，没有写入任何卡片；'
                              '如果情绪很重，先找身边可信的人聊聊。')
    else:
        # 题库模式 = 用户明确关掉云端：收口也不再把回答发出去，只做本地启发式提炼。
        items, summary = _extract(store, session, turns, use_model=session.get('mode') != 'bank')
    added = _merge_pending(store, items)
    session.update({'status': 'closed', 'closed_at': now(), 'summary': summary,
                    'current_question': {}, 'pending_added': added,
                    'safety': bool(safety), 'updated_at': now()})
    store.put('interview_session', session, session['id'])
    return session


# ---------------------------------------------------------------- 对外接口
def state(store, session_id=None):
    session = store.get('interview_session', session_id) if session_id else None
    if session is None:
        sessions = store.list('interview_session', 50)
        session = sessions[0] if sessions else None
    turns = _turns(store, session['id']) if session else []
    prof = _profile(store)
    pending = [p for p in prof.get('pending') or [] if p.get('status') == 'pending']
    applied = [a for a in prof.get('applied') or [] if not a.get('undone_at')]
    return {
        'ok': True,
        'settings': _settings(store),
        'cloud_available': providers.available()['llm'],
        'session': ({k: v for k, v in session.items() if k != 'fallbacks'}
                    if session else None),
        'turns': turns[-12:],
        'fallbacks': (session or {}).get('fallbacks', [])[-3:],
        'pending': pending[-20:],
        'applied': applied[-10:],
        'remembered': (prof.get('remembered') or [])[-8:],
        'max_level': MAX_LEVEL,
    }


def start(store, theme='', cloud=None):
    prof = _profile(store)
    use_cloud = bool(prof['settings'].get('cloud', True)) if cloud is None else bool(cloud)
    sid = uid('iv')
    session = {'title': (theme or '深挖访谈 ' + datetime.now(TZ).strftime('%m-%d')),
               'theme': str(theme or '')[:120], 'status': 'active', 'level': 1,
               'turn_count': 0, 'mode': 'cloud' if use_cloud else 'bank',
               'used_ids': [], 'summary': '', 'started_at': now(), 'updated_at': now(),
               'current_question': {}, 'fallbacks': []}
    session['id'] = sid
    question = _ask(store, session, 1, '')
    session['current_question'] = question
    session.pop('id', None)
    store.put('interview_session', session, sid)
    return state(store, sid)


def answer(store, session_id, text):
    session = store.get('interview_session', session_id)
    if not session:
        raise ValueError('没有找到这次访谈，可能已结束，请重新开始。')
    if session.get('status') != 'active':
        raise ValueError('这次访谈已经收口了，看看下面的回填条目。')
    content = str(text or '').strip()[:4000]
    if not content:
        raise ValueError('先写点内容再提交；一句真话就够。')
    level = int(session.get('level') or 1)
    question = session.get('current_question') or {}
    turn = {'session_id': session_id, 'seq': int(session.get('turn_count') or 0) + 1,
            'level': level, 'question': question.get('text', ''),
            'question_source': question.get('source', 'bank'),
            'question_ref': question.get('ref', ''), 'answer': content,
            'at': now(), 'updated_at': now()}
    store.put('interview_turn', turn)
    session['turn_count'] = turn['seq']
    session['updated_at'] = now()
    if any(w in content for w in SAFETY_WORDS):
        _close(store, session, safety=True)
        return state(store, session_id)
    if level >= MAX_LEVEL:
        _close(store, session)
        return state(store, session_id)
    # 「不想说 / 跳过 / 不知道」不追，换方向问（方案里的第 6 条硬规则）。
    switch = any(w in content for w in AVOID_WORDS)
    nxt = _ask(store, session, level + 1, content, switch=switch)
    session['level'] = level + 1
    session['current_question'] = nxt
    store.put('interview_session', session, session_id)
    return state(store, session_id)


def close(store, session_id):
    session = store.get('interview_session', session_id)
    if not session:
        raise ValueError('没有找到这次访谈。')
    if session.get('status') == 'active':
        _close(store, session)
    return state(store, session_id)


# ---------------------------------------------------------------- 回填：应用 / 撤销
def _delete_record(store, kind, key):
    with store.connect() as con:
        con.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, key))


def _apply(store, item):
    kind, pid = item['target'], item['pid']
    if kind == 'knowledge':
        key = 'interview_k_' + pid[2:]
        store.put('knowledge', {
            'legacy_id': '访谈-' + pid[-6:],
            'title': '访谈补录 · ' + item.get('note', '价值观'),
            'category': '认知底座', 'category_legacy': '三观',
            'category_aliases': ['认知底座', '三观'],
            'claim': item['text'][:200],
            'boundary': '来自深挖访谈，本人点「一键应用」后才写入；可一键撤销。',
            'body': '原话：' + item.get('quote', '') + '\n\n来源：' +
                    '深挖访谈 ' + str(item.get('created_at') or '')[:10],
            'source_id': None, 'source_locator': {}, 'links': [],
            'markers': ['访谈补录'], 'endorsement': 'accepted',
            'validation': 'personal_case', 'lifecycle': 'active',
            'content_use': 'available',
            'interview_pid': pid, 'interview_session': item.get('session_id', ''),
        }, key)
        return key
    if kind == 'flow':
        flow = store.get('flow', 'owner') or {}
        notes = flow.get('interview_notes') or []
        if not any(n.get('pid') == pid for n in notes):
            notes.append({'pid': pid, 'text': item['text'], 'quote': item.get('quote', ''),
                          'session_id': item.get('session_id', ''), 'at': now()})
        flow['interview_notes'] = notes
        flow['updated_at'] = now()
        store.put('flow', flow, 'owner')
        return 'flow:owner'
    if kind == 'source':
        key = 'interview_s_' + pid[2:]
        store.put('source', {
            'title': '访谈原话 · ' + item['text'][:24], 'url': '', 'share_text': '',
            'text': item.get('quote') or item['text'], 'kind': 'text', 'status': 'ready',
            'markers': ['访谈补录'], 'interview_pid': pid,
            'interview_session': item.get('session_id', ''),
        }, key)
        return key
    raise ValueError('未知的回填目标：' + str(kind))


def apply_items(store, pids):
    prof = _profile(store)
    wanted = [str(x) for x in (pids or [])]
    applied, skipped = [], []
    for item in prof.get('pending') or []:
        if item.get('status') != 'pending' or item.get('pid') not in wanted:
            continue
        record_id = _apply(store, item)
        item['status'] = 'applied'
        item['applied_at'] = now()
        entry = {'aid': 'a_' + item['pid'][2:], 'pid': item['pid'],
                 'kind': item['kind'], 'target': item['target'],
                 'target_label': item['target_label'], 'text': item['text'],
                 'quote': item.get('quote', ''), 'record_id': record_id,
                 'session_id': item.get('session_id', ''), 'applied_at': now()}
        prof['applied'].append(entry)
        remembered = prof.get('remembered') or []
        if not any(m.get('pid') == item['pid'] for m in remembered):
            remembered.append({'pid': item['pid'], 'kind': item['kind'],
                               'text': item['text'], 'at': now()})
        prof['remembered'] = remembered[-40:]
        applied.append(entry)
    for pid in wanted:
        if not any(a.get('pid') == pid for a in applied):
            skipped.append(pid)
    prof['updated_at'] = now()
    store.put('creator_profile', prof, PROFILE_ID)
    if applied:
        content_index.start_rebuild(store)
    return {'ok': True, 'applied': applied, 'skipped': skipped, 'state': state(store)}


def undo_items(store, aids):
    prof = _profile(store)
    wanted = {str(x) for x in (aids or [])}
    undone = []
    for entry in prof.get('applied') or []:
        if entry.get('undone_at') or entry.get('aid') not in wanted:
            continue
        if entry['target'] == 'knowledge':
            _delete_record(store, 'knowledge', entry['record_id'])
        elif entry['target'] == 'source':
            _delete_record(store, 'source', entry['record_id'])
        elif entry['target'] == 'flow':
            flow = store.get('flow', 'owner') or {}
            notes = [n for n in (flow.get('interview_notes') or [])
                     if n.get('pid') != entry['pid']]
            flow['interview_notes'] = notes
            flow['updated_at'] = now()
            store.put('flow', flow, 'owner')
        entry['undone_at'] = now()
        for item in prof.get('pending') or []:
            if item.get('pid') == entry['pid']:
                item['status'] = 'pending'
                item.pop('applied_at', None)
        prof['remembered'] = [m for m in (prof.get('remembered') or [])
                              if m.get('pid') != entry['pid']]
        undone.append(entry)
    prof['updated_at'] = now()
    store.put('creator_profile', prof, PROFILE_ID)
    if undone:
        content_index.start_rebuild(store)
    return {'ok': True, 'undone': undone, 'state': state(store)}


def profile_summary(store):
    prof = _profile(store)
    remembered = prof.get('remembered') or []
    applied = [a for a in prof.get('applied') or [] if not a.get('undone_at')]
    return {
        'ok': True,
        'settings': _settings(store),
        'remembered': remembered[-20:],
        'applied': applied[-20:],
        'counts': {
            'remembered': len(remembered),
            'applied': len(applied),
            'pending': len([p for p in prof.get('pending') or [] if p.get('status') == 'pending']),
        },
        'sessions': [{k: s.get(k) for k in ('id', 'title', 'status', 'summary', 'turn_count', 'updated_at')}
                     for s in store.list('interview_session', 10)],
    }
