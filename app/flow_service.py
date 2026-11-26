"""主流程：序言、12 题语音建档、三观卡与赛道。

题库与「深度锚点」只保存在服务端：锚点用于校准模型归纳深度，
绝不通过任何接口返回给前端，避免用户照抄示例。
"""
import providers
import prompt_store
import material
from db import now

# 进阶层：只给已经答完 12 题、愿意深挖的人。4 题分别对应三观的“适用边界”：
# a1 世界观边界（道理什么时候不成立）、a2 人生观边界（60 岁回头）、
# a3 价值观边界（底线）、a4 价值观边界（自由的代价）。
# 进阶层答案不生成新卡，而是挂到对应组的 knowledge.boundary，并写进 card.boundaries。
# 2026-11-17（t42 题库封壳）：题库正文只存云端（method_assets: flow.questions），
# 本地只留 id/section 骨架——够 UI 摆占位、够拦非法输入，但拿不到题目原文。
# 取值一律走 asset_store；未登录 / 无缓存 / 缓存超 72h → 返回空，由调用方降级为登录引导。
QUESTION_SKELETON = [('q1', '世界观'), ('q2', '世界观'), ('q3', '世界观'), ('q4', '世界观'), ('q5', '人生观'), ('q6', '人生观'), ('q7', '人生观'), ('q8', '人生观'), ('q9', '价值观'), ('q10', '价值观'), ('q11', '价值观'), ('q12', '价值观')]
ADVANCED_SKELETON = [('a1', '世界观'), ('a2', '人生观'), ('a3', '价值观'), ('a4', '价值观')]


def _asset_questions():
    try:
        import asset_store
        value = asset_store.get('flow.questions')
    except Exception:
        value = None
    if not isinstance(value, dict):
        return [], []
    qs = value.get('questions') or []
    adv = value.get('advanced') or []
    return (list(qs), list(adv)) if isinstance(qs, list) and isinstance(adv, list) else ([], [])


def questions():
    return _asset_questions()[0]


def advanced_questions():
    return _asset_questions()[1]


def assets_ready():
    return bool(questions())
SECTIONS = ['世界观', '人生观', '价值观']
SECTION_KEYS = {'世界观': 'worldview', '人生观': 'life', '价值观': 'values'}
TRACKS = {'selfmedia': '做自己的自媒体', 'clip': '做切片'}
STAGES = ('intro', 'question', 'card', 'track', 'done')

# 仅后台提示词锚点：把模型压到“有判断、有故事、有态度”的深度，不展示给用户。
ANCHORS = (
    '世界观：世界不按你的心意走，大环境往往比个人用力更管用；见得少，机会摆到眼前也认不出来。'
    '年轻时不信命，现在信一点；但自己不折腾，命也不会自己变好。人和人的差距靠自己想明白，'
    '不会独立想，就只会跟着别人做。世界本来就不公平，公平才是例外；改不了的就先接受，能改的才动手。\n'
    '人生观：清零重来还带着原来的经验，吃过亏的经历比钱值钱，活着就得折腾。'
    '到了这个年纪做事，是想去看更大的世界、看清自己，不再为证明给谁看。'
    '“成了”不是自己多有钱，是后来的人因为你，起点不一样：留下的是想法、知识和家里的规矩。'
    '卖房还债是学费，亏掉的是钱，换回来的是经历和教训。\n'
    '价值观：真话和有用的话能凑到一起最好；只能选一个时，选有人听、能把事办成的那句。'
    '钱和脸面要能屈能伸，穷过的人才知道难堪是什么滋味。义气和钱，现在更看重钱和规矩，'
    '不拿人性去赌。自由是最后想保住的东西，一直上班，人就活成一颗螺丝钉。'
)


def _defaults():
    return {'intro_seen': False, 'answers': {}, 'current': 1, 'stage': 'intro',
            'completed': False, 'track': '', 'card': None, 'skipped': False,
            'deferred': False, 'defer_hint_at': '',
            'knowledge_ids': [], 'updated_at': ''}


def _record(store):
    return {**_defaults(), **(store.get('flow', 'owner') or {})}


# 三观三句 → 知识卡的固定映射（t197：定稿与知识卡必须是一份事实，不能两处各说一套）
SANGUAN_LEGACY = {'worldview': '01', 'life': '02', 'values': '03'}
SANGUAN_KEYS = ('worldview', 'life', 'values')


def _sanguan_record(key, claim, boundary='', body='', markers=None):
    """建档生成与后来补建共用同一份结构，避免两种来源的三观卡字段不一样。"""
    return {
        'legacy_id': '三观-' + SANGUAN_LEGACY[key],
        'title': '我的三观 · ' + {'worldview': '世界观', 'life': '人生观', 'values': '价值观'}[key],
        # 2026-11-14 起归入「认知底座」；旧词「三观」留在 category_legacy/别名里，仍可搜到。
        'category': '认知底座',
        'category_legacy': '三观',
        'category_aliases': ['认知底座', '三观'],
        'claim': claim,
        'boundary': boundary or '来自本人定稿，后续可随时修正。',
        'body': body,
        'source_id': None,
        'source_locator': {},
        'links': [],
        'markers': list(markers or ['本人定稿']),
        'endorsement': 'accepted',
        'validation': 'personal_case',
        'lifecycle': 'active',
        'content_use': 'available',
    }


def _all_questions():
    return questions() + advanced_questions()


def summary(store):
    rec = _record(store)
    answers = rec.get('answers') if isinstance(rec.get('answers'), dict) else {}
    clean = {q['id']: str(answers.get(q['id'], '')) for q in questions()}
    advanced_clean = {q['id']: str(answers.get(q['id'], '')) for q in advanced_questions()}
    card = rec.get('card') if isinstance(rec.get('card'), dict) else None
    stage = rec.get('stage') if rec.get('stage') in STAGES else 'intro'
    try:
        # 老库第一次打开时把已有答案补进素材层；已同步过的题不会再写。
        material.sync_answers(store, rec, questions())
    except Exception:
        pass
    return {
        'intro_seen': bool(rec.get('intro_seen')),
        'current': max(1, min(len(questions()), int(rec.get('current') or 1))),
        'stage': stage,
        'completed': bool(rec.get('completed')),
        'track': rec.get('track') if rec.get('track') in TRACKS else '',
        'skipped': bool(rec.get('skipped')),
        # 首启欢迎屏选了「稍后再说」：记下来，工作台首屏只提醒一次（defer_hint_at 非空即已提醒过）。
        'deferred': bool(rec.get('deferred')),
        'defer_hint_at': str(rec.get('defer_hint_at') or ''),
        'card': card,
        'knowledge_ids': rec.get('knowledge_ids') if isinstance(rec.get('knowledge_ids'), list) else [],
        'answers': clean,
        'answered': sum(1 for v in clean.values() if v.strip()),
        'materials': material.count(store),
        'questions': [{'id': q['id'], 'section': q['section'], 'text': q['text'],
                       'hint': q.get('hint', ''), 'exit': q.get('exit', '')} for q in questions()],
        # 进阶层单独一组返回，不混进 12 题题库，避免现有页面把 12 题变成 16 题。
        'advanced_answers': advanced_clean,
        'advanced_answered': sum(1 for v in advanced_clean.values() if v.strip()),
        'advanced_questions': [{'id': q['id'], 'section': q['section'], 'text': q['text'],
                                'hint': q.get('hint', ''), 'exit': q.get('exit', '')} for q in advanced_questions()],
        'sections': SECTIONS,
        'tracks': TRACKS,
        # 2026-11-17（t42）：题库正文在云端。拿不到就明确告诉界面「要登录」，由界面给引导，
        # 不硬跑建档、也不回退本地全文（本地已无正文）。
        'assets_ready': assets_ready(),
        'need_login': not assets_ready(),
    }


def save(store, data):
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    rec = _record(store)
    if 'intro_seen' in data:
        rec['intro_seen'] = bool(data['intro_seen'])
    if 'skipped' in data:
        rec['skipped'] = bool(data['skipped'])
    if 'deferred' in data:
        rec['deferred'] = bool(data['deferred'])
    # 首屏提醒只出现一次：前端显示过就回写时间戳，之后不再提醒。
    if data.get('defer_hint') is True and not rec.get('defer_hint_at'):
        rec['defer_hint_at'] = now()
    if 'track' in data:
        track = str(data['track'] or '')
        if track and track not in TRACKS:
            raise ValueError('赛道选项无效。')
        rec['track'] = track
    if 'stage' in data:
        stage = str(data['stage'] or '')
        if stage not in STAGES:
            raise ValueError('建档阶段无效。')
        rec['stage'] = stage
    if 'current' in data:
        try:
            current = int(data['current'])
        except (TypeError, ValueError):
            raise ValueError('题号无效。') from None
        rec['current'] = max(1, min(len(questions()), current))
    if 'answers' in data:
        if not isinstance(data['answers'], dict):
            raise ValueError('答案格式无效。')
        valid = {q['id'] for q in _all_questions()}
        merged = dict(rec.get('answers') or {})
        for key, value in data['answers'].items():
            if key not in valid:
                continue
            if not isinstance(value, str):
                raise ValueError('答案需要是文字。')
            merged[key] = value.strip()[:6000]
        rec['answers'] = merged
    if 'answer_meta' in data:
        if not isinstance(data['answer_meta'], dict):
            raise ValueError('答案来源格式无效。')
        valid = {q['id'] for q in _all_questions()}
        meta = dict(rec.get('answer_meta') or {})
        for key, value in data['answer_meta'].items():
            if key not in valid or not isinstance(value, dict):
                continue
            meta[key] = {'via': str(value.get('via') or 'text')[:20], 'captured_at': now()}
        rec['answer_meta'] = meta
    if 'card' in data:
        # 2026-11-17（t28 · P1 ownership）：三观主张由本人改/拍才算数。
        # 只收三句（worldview/life/values），每句 ≤120 字；写回 flow.card，
        # 并同步对应三观知识卡的 claim（只改这句主张，边界/原文不动），留 edited_by=user 痕迹。
        if not isinstance(data['card'], dict):
            raise ValueError('三观定稿格式无效。')
        incoming = data['card']
        old_card = rec.get('card') if isinstance(rec.get('card'), dict) else {}
        card = dict(old_card)
        changed = {}
        for key, label in (('worldview', '世界观'), ('life', '人生观'), ('values', '价值观')):
            if key not in incoming:
                continue
            value = incoming.get(key)
            if not isinstance(value, str):
                raise ValueError(label + '定稿需要是文字。')
            value = value.strip()
            if len(value) > 120:
                raise ValueError(label + '定稿最多 120 字，请压缩后再存。')
            card[key] = value
            changed[key] = value
        if not changed:
            raise ValueError('没有要保存的三观定稿。')
        card['edited_at'] = now()
        card['edited_by'] = 'user'
        # 记住本人亲手动过哪几句：以后重新凝成三观卡时，这几句不许被模型文案覆盖。
        edited_keys = [k for k in (card.get('edited_keys') or []) if k in SANGUAN_KEYS]
        for key in changed:
            if key not in edited_keys:
                edited_keys.append(key)
        card['edited_keys'] = edited_keys
        rec['card'] = card
        for key, value in changed.items():
            if not value:
                continue
            try:
                item = store.get('knowledge', 'sanguan_' + key)
                markers = list((item or {}).get('markers') or ['本人建档'])
                if '本人定稿' not in markers:
                    markers.append('本人定稿')
                if item:
                    store.patch('knowledge', item['id'], {'claim': value, 'markers': markers})
                else:
                    # 旧档案没有建档生成的三观卡（或卡被删过）：当场补建一张，
                    # 否则定稿只写进 flow.card，写稿仍拿旧主张——用户改了却不生效。
                    store.put('knowledge', _sanguan_record(key, value, markers=markers),
                              'sanguan_' + key)
            except Exception:
                # 知识卡同步失败不影响三观定稿本身（flow.card 已写回）。
                pass
    if data.get('completed') is True:
        if rec.get('track') not in TRACKS:
            raise ValueError('先选赛道，再完成建档。')
        rec['completed'] = True
        rec['stage'] = 'done'
    elif 'completed' in data and data['completed'] is False:
        rec['completed'] = False
    rec['updated_at'] = now()
    store.put('flow', rec, 'owner')
    try:
        material.sync_answers(store, rec, questions())
    except Exception:
        # 素材层同步失败不能影响建档保存本身；答案仍在 flow 里，可重试。
        pass
    return summary(store)


def _first_sentence(text, limit=80):
    text = (text or '').strip().replace('\r', '').replace('\n', ' ')
    for mark in ('。', '！', '？', '；'):
        if mark in text:
            text = text.split(mark, 1)[0]
            break
    return text[:limit]


def _fallback_card(filled):
    card = {}
    for section in SECTIONS:
        items = filled.get(section) or []
        key = SECTION_KEYS[section]
        card[key] = _first_sentence(items[0][1]) if items else ''
    return card


def _condense(filled):
    """用模型把口述压成三句主张；模型不可用/失败时退回用户原话首句。"""
    if providers.available()['llm']:
        try:
            payload = {
                'answers': {section: [{'question': q, 'answer': a} for q, a in items]
                            for section, items in filled.items() if items},
                'anchors': ANCHORS,
            }
            result = providers.llm_json(
                prompt_store.get('flow.anchors'),
                payload)
            card = {key: str(result.get(key) or '').strip()[:120]
                    for key in ('worldview', 'life', 'values')}
            if any(card.values()):
                return {**card, 'method': 'llm'}
        except Exception:
            pass
    return {**_fallback_card(filled), 'method': 'answers'}


def finalize(store):
    """12 题（或部分）答完 → 凝成三观卡 → 写入知识库。"""
    rec = _record(store)
    answers = rec.get('answers') if isinstance(rec.get('answers'), dict) else {}
    filled = {}
    for section in SECTIONS:
        filled[section] = [(q['text'], str(answers.get(q['id'], '')).strip())
                           for q in questions()
                           if q['section'] == section and str(answers.get(q['id'], '')).strip()]
    has_answers = any(filled.values())
    if not has_answers:
        # 没有一句回答就生成“三观”，等于空转出稿：明确拒绝，并告诉用户先去建档。
        raise ValueError('你还没答任何一题，现在生成出来的三观是空的。'
                         '先回答至少一题（答得越多越像你），再回来点“生成我的三观”。')
    # 进阶层 4 题按组挂到对应三观条目上，不另起新卡：
    # a1→世界观，a2→人生观，a3/a4→价值观。
    advanced = {}
    for section in SECTIONS:
        advanced[section] = [(q['text'], str(answers.get(q['id'], '')).strip())
                             for q in advanced_questions()
                             if q['section'] == section and str(answers.get(q['id'], '')).strip()]
    boundaries = {SECTION_KEYS[section]: '\n'.join('Q：' + q + '\nA：' + a for q, a in advanced[section])[:2000]
                  for section in SECTIONS}
    card = _condense(filled)
    # t197：本人已经定稿过的那几句，重新凝成时不许被模型文案盖掉——
    # 用户改过的意见就是最终意见，这里只用新答案补「没定过」的部分。
    old_card = rec.get('card') if isinstance(rec.get('card'), dict) else {}
    edited_keys = [k for k in (old_card.get('edited_keys') or []) if k in SANGUAN_KEYS]
    if old_card.get('edited_by') != 'user':
        edited_keys = []
    for key in edited_keys:
        value = str(old_card.get(key) or '').strip()
        if value:
            card[key] = value[:120]
    if card and any(boundaries.values()):
        # 三观卡仍是三句主张；边界单独存，方便前端展示，也方便写稿时防止说过头。
        card['boundaries'] = {k: v for k, v in boundaries.items() if v}
    if card and edited_keys:
        card['edited_by'] = 'user'
        card['edited_at'] = old_card.get('edited_at') or now()
        card['edited_keys'] = edited_keys
        card['regenerated_at'] = now()
    knowledge_ids = []
    if card:
        for section in SECTIONS:
            items = filled.get(section) or []
            key = SECTION_KEYS[section]
            claim = str(card.get(key) or '').strip()
            if not items or not claim:
                continue
            body = '\n\n'.join('Q：' + q + '\nA：' + a for q, a in items)
            if advanced[section]:
                body += '\n\n【进阶层·边界】\n' + '\n\n'.join(
                    'Q：' + q + '\nA：' + a for q, a in advanced[section])
            markers = ['本人建档']
            if key in edited_keys:
                markers.append('本人定稿')
            item = store.put('knowledge', _sanguan_record(
                key, claim,
                boundary=boundaries.get(key) or '来自本人建档口述，后续可随时补充与修正。',
                body=body, markers=markers), 'sanguan_' + key)
            knowledge_ids.append(item['id'])
    rec['card'] = card
    rec['knowledge_ids'] = knowledge_ids
    if card:
        rec['stage'] = 'card'
    else:
        rec['stage'] = 'track'
    rec['current'] = len(questions())
    rec['updated_at'] = now()
    store.put('flow', rec, 'owner')
    return summary(store)


def authored_question_count(answers):
    return sum(1 for q in questions() if str((answers or {}).get(q['id'], '')).strip())
