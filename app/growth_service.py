"""Goal-linked personal growth. Local-only; no models, migrations or remote writes."""
import base64
import hashlib
import json
import re
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from db import Conflict, now, uid

LOCK = threading.RLock()
TZ = timezone(timedelta(hours=8))
PILLARS = [('认知', '#9b8dd6'), ('意义', '#c496be'), ('能量', '#d3ad70'), ('关系', '#83b5ac'), ('价值', '#86a6cf')]

# 「内容分类 → 支柱」自动统计映射。默认值只在配置文件缺失时兜底；
# 日常调整改 assets/growth_pillar_mapping.json，不要改这里。
PILLAR_MAPPING_FILE = Path(__file__).resolve().parent / 'assets' / 'growth_pillar_mapping.json'
PILLAR_MAPPING_DEFAULT = {'认知底座': '认知', '赚钱逻辑': '认知', '成功心法': '认知', '操盘手法': '认知'}
_PILLAR_MAPPING_CACHE = {'mtime': None, 'mapping': None}


def pillar_mapping():
    """读分类到支柱的映射；文件坏了或缺省时回落到内置默认值。"""
    try:
        mtime = PILLAR_MAPPING_FILE.stat().st_mtime
    except OSError:
        return dict(PILLAR_MAPPING_DEFAULT)
    if _PILLAR_MAPPING_CACHE['mtime'] == mtime and _PILLAR_MAPPING_CACHE['mapping'] is not None:
        return _PILLAR_MAPPING_CACHE['mapping']
    try:
        raw = json.loads(PILLAR_MAPPING_FILE.read_text(encoding='utf-8'))
        source = raw.get('category_to_pillar') if isinstance(raw, dict) else None
        mapping = {str(k).strip(): str(v).strip() for k, v in (source or {}).items()
                   if isinstance(k, str) and isinstance(v, str) and v.strip()}
        if not isinstance(source, dict):
            mapping = dict(PILLAR_MAPPING_DEFAULT)
    except (OSError, ValueError, TypeError):
        mapping = dict(PILLAR_MAPPING_DEFAULT)
    _PILLAR_MAPPING_CACHE.update(mtime=mtime, mapping=mapping)
    return mapping


def text(value, label, maximum=40000, required=False):
    if not isinstance(value, str):
        raise ValueError(label + '格式不正确。')
    value = value.strip()
    if required and not value:
        raise ValueError('请填写' + label + '。')
    if len(value) > maximum:
        raise ValueError(label + '最多 ' + str(maximum) + ' 字，请分条保存。')
    return value


# ---------------------------------------------------------------- 立轴引导（2026-11-17 · t27）
# 提示词 growth.thesis 的本地草稿：云端批准下发后由 prompt_store 60 秒热更覆盖（版本可回滚）。
# 本次不改云端、不改 prompt_store.py，只 setdefault 登记成本地默认（与 brain_coach 同一做法）。
THESIS_PROMPT_KEY = 'growth.thesis'
THESIS_PROMPT_DRAFT = (
    '你是用户的立轴教练。任务：从他已有的回答里，结晶出 2–3 条「他要用作品验证的命题」候选。\n'
    '【输入】三观卡：世界观={{worldview}}／人生观={{life}}／价值观={{values}}；'
    '关键回答：q6(图什么)={{q6}}、q8(最贵取舍)={{q8}}、q9(底线)={{q9}}、a1(道理何时不成立)={{a1}}；'
    '赛道={{track}}；主业={{industry}}。\n'
    '【铁律】1.用他自己的词、他自己的经历；禁止通用模板、禁止替他编主张。'
    '2.每条命题必须锚定主业优势（具体作品），不许浮成人生口号。'
    '3.命题≠目标：「100万粉/年入50万」是刻度不是命题；命题是「我相信X，要用作品验证它」。'
    '4.每条命题必须可证伪：给出「什么情况下它就不成立」（接 a1）。'
    '5.你只给候选，定稿必须用户自己改/拍；措辞像他说话，不像 AI。'
    '6.不评判人格，只连接「他的作品与思考」；不否定他的实际目标（赚钱也正当）。\n'
    '【输出】只返回 JSON：{"candidates":[{"thesis":string,"source":string,"boundary":string,"connect":string}]}；'
    'thesis 不超过 120 字；source 写引用他哪句原话；boundary 写可证伪边界；connect 是拿他自己的答案问回去的一句问句。'
)
_THESIS_REGISTERED = {'done': False}


def register_thesis_prompt():
    """把本地草稿登记进提示词中心（setdefault，绝不覆盖云端/已有条目）。"""
    if _THESIS_REGISTERED['done']:
        return
    try:
        import prompt_store
        prompt_store.DEFAULTS.setdefault(THESIS_PROMPT_KEY, THESIS_PROMPT_DRAFT)
        prompt_store.NAMES.setdefault(THESIS_PROMPT_KEY, '立轴·命题结晶')
    except Exception:  # noqa: BLE001  提示词中心不可用也不能让立轴挂掉
        pass
    _THESIS_REGISTERED['done'] = True


def thesis_prompt(values=None):
    """取提示词（云端 > 缓存 > 本地草稿）并填占位符。"""
    register_thesis_prompt()
    try:
        import prompt_store
        body = prompt_store.get(THESIS_PROMPT_KEY)
    except Exception:  # noqa: BLE001
        body = ''
    if not body or not str(body).strip():
        body = THESIS_PROMPT_DRAFT
    for key, value in (values or {}).items():
        body = str(body).replace('{{%s}}' % key, '' if value is None else str(value))
    return body


def _first_sentence(value, limit=60):
    cleaned = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not cleaned:
        return ''
    return re.split(r'[。！？!?；;\n]', cleaned)[0].strip()[:limit]


def axis_inputs(store):
    """立轴引导的输入：三观卡＋关键回答＋赛道＋主业（全本地读，不外发到除模型外的任何地方）。"""
    import flow_service
    summary = flow_service.summary(store)
    answers = summary.get('answers') if isinstance(summary.get('answers'), dict) else {}
    advanced = summary.get('advanced_answers') if isinstance(summary.get('advanced_answers'), dict) else {}
    pool = dict(answers)
    pool.update({k: v for k, v in advanced.items() if v})
    card = summary.get('card') if isinstance(summary.get('card'), dict) else {}
    track = summary.get('track') or ''
    profile = store.get('creator_profile', 'main') or {}
    fields = profile.get('fields') if isinstance(profile.get('fields'), dict) else {}

    def pick(*names):
        for name in names:
            value = profile.get(name) or fields.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
        return ''

    industry = pick('industry', '主业', 'business') or _first_sentence(pool.get('q8'), 80)
    return {
        'card': {k: str(card.get(k) or '').strip()[:400]
                 for k in ('worldview', 'life', 'values') if str(card.get(k) or '').strip()},
        'answers': {k: str(pool.get(k) or '').strip()[:400]
                    for k in ('q6', 'q8', 'q9', 'a1') if str(pool.get(k) or '').strip()},
        'track': track if track in flow_service.TRACKS else '',
        'track_label': flow_service.TRACKS.get(track, ''),
        'industry': industry,
        'experience': pick('experience', '经历'),
        'completed': bool(summary.get('completed')),
        'stage': summary.get('stage') or '',
        'knowledge_ids': list(summary.get('knowledge_ids') or [])[:8],
    }


def _rule_candidates(inputs):
    """模型不可用时的规则拼接：只用他自己的话，不空转、不编经历。"""
    card = inputs.get('card') or {}
    answers = inputs.get('answers') or {}
    anchor = inputs.get('industry') or _first_sentence(answers.get('q8'), 50) or '你自己的经验'
    label = inputs.get('track_label') or '内容'
    q6 = _first_sentence(answers.get('q6'), 50)
    q8 = _first_sentence(answers.get('q8'), 50)
    a1 = _first_sentence(answers.get('a1'), 60)
    connect = ('你说你最贵的取舍是「%s」，你图的是「%s」——那你做内容，是不是想证明「%s」？'
               % (q8 or '（这题还没答）', q6 or '（这题还没答）',
                  _first_sentence(card.get('values') or card.get('life') or '', 40) or '这件事'))
    rows = []
    values = _first_sentence(card.get('values'), 50)
    life = _first_sentence(card.get('life'), 50)
    world = _first_sentence(card.get('worldview'), 50)
    if values:
        rows.append({'thesis': '我相信「%s」，我要用%s通过%s验证它。' % (values, anchor, label),
                     'source': '三观卡·价值观', 'boundary': a1, 'connect': connect})
    if life:
        rows.append({'thesis': '我相信「%s」，我要用%s把它讲成只有我能讲的样子。' % (life, anchor),
                     'source': '三观卡·人生观', 'boundary': a1, 'connect': connect})
    if world:
        rows.append({'thesis': '我相信「%s」，我要拿%s去检验它——不是讲道理，是拿作品给人看。'
                               % (world, anchor),
                     'source': '三观卡·世界观', 'boundary': a1, 'connect': connect})
    if not rows and q8:
        rows.append({'thesis': '我相信「%s」，我要用它换来的经验通过%s验证它。' % (q8, label),
                     'source': 'q8 最贵取舍', 'boundary': a1, 'connect': connect})
    return rows[:3]


def month_value(value):
    value = str(value or datetime.now(TZ).strftime('%Y-%m'))
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value) or not 1900 <= int(value[:4]) <= 9999:
        raise ValueError('请选择有效月份。')
    return value


def record_month(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(TZ).strftime('%Y-%m')
    except (ValueError, TypeError, AttributeError):
        return ''


def record_day(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ).strftime('%Y-%m-%d')
    except (ValueError, TypeError, AttributeError):
        return ''


def source_types(fragment):
    kinds = set()
    if fragment.get('text'):
        kinds.add('text')
    if fragment.get('source_url') or fragment.get('type') == 'link':
        kinds.add('link')
    if fragment.get('voice_id') or fragment.get('type') == 'voice':
        kinds.add('voice')
    return kinds or {'text'}


class Growth:
    def __init__(self, store):
        self.store = store

    def all(self, kind):
        # Deliberately no Store.list default limit: totals/export must include all rows.
        with self.store.connect() as con:
            return [self.store.unpack(r) for r in con.execute('SELECT * FROM records WHERE kind=? ORDER BY created_at DESC,id', (kind,))]

    def need(self, kind, key):
        result = self.store.get(kind, key)
        if not result:
            raise KeyError(key)
        return result

    def _root_extras(self, data, old=None):
        """立轴字段（只增不改）：thesis / milestone / track / thesis_source；缺省沿用旧值。"""
        old = old or {}
        value = {}
        if 'thesis' in data or 'thesis' in old:
            value['thesis'] = text(data.get('thesis', old.get('thesis', '')), '命题', 600)
        if 'milestone' in data or 'milestone' in old:
            value['milestone'] = text(data.get('milestone', old.get('milestone', '')), '主业目标', 160)
        if 'track' in data or 'track' in old:
            track = str(data.get('track', old.get('track', '')) or '')
            if track:
                import flow_service
                if track not in flow_service.TRACKS:
                    raise ValueError('赛道选项无效。')
            value['track'] = track
        if 'thesis_source' in data or 'thesis_source' in old:
            source = data.get('thesis_source', old.get('thesis_source', []))
            if source in (None, ''):
                source = []
            if not isinstance(source, list) or any(not isinstance(x, str) for x in source):
                raise ValueError('命题来源格式不正确。')
            value['thesis_source'] = list(dict.fromkeys(source))[:20]
        return value

    def setup(self, data):
        if not isinstance(data, dict):
            raise ValueError('请求体必须是 JSON 对象。')
        raw_title = str(data.get('title') or '').strip()
        # 立轴引导：主线名可以由命题或主业目标充当（目标可以很实际，含赚钱）。
        if not raw_title:
            raw_title = (str(data.get('thesis') or '').strip()
                         or str(data.get('milestone') or '').strip())[:160]
        title = text(raw_title, '人生或年度目标', 160, True)
        extras = self._root_extras(data)
        with LOCK, self.store.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute("SELECT 1 FROM records WHERE kind='growth_goal' LIMIT 1").fetchone():
                raise Conflict('目标体系已经建立，请刷新查看；没有重复创建。')
            root_id = uid('growth_goal')
            stamp = now()
            rows = [(root_id, dict(title=title, parent_id=None, level='root', archived=False,
                                   color='#9b8dd6', description='', sort_order=-1, **extras))]
            rows += [(uid('growth_goal'), dict(title=name, parent_id=root_id, level='pillar', archived=False, color=color, description='', sort_order=i)) for i, (name, color) in enumerate(PILLARS)]
            for key, value in rows:
                con.execute('INSERT INTO records VALUES (?,?,?,?,?,?)', ('growth_goal', key, json.dumps(value, ensure_ascii=False), 1, stamp, stamp))
        return {'root_id': root_id}

    def descendants(self, goals, key):
        keys = {g['id'] for g in goals}
        if key and key not in keys:
            raise KeyError(key)
        result = {key} if key else keys
        while True:
            expanded = result | {g['id'] for g in goals if g.get('parent_id') in result}
            if expanded == result:
                return result
            result = expanded

    def path(self, goals, key):
        lookup = {g['id']: g for g in goals}
        result, seen = [], set()
        while key and key in lookup and key not in seen:
            seen.add(key)
            result.append(lookup[key]['title'])
            key = lookup[key].get('parent_id')
        return ' / '.join(reversed(result))

    def goal(self, data, key=None):
        with LOCK:
            goals = self.all('growth_goal')
            old = self.need('growth_goal', key) if key else None
            title = text(data.get('title', old.get('title') if old else ''), '目标名称', 160, True)
            parent = data.get('parent_id', old.get('parent_id') if old else None) or None
            if key and old.get('level') == 'root':
                if parent:
                    raise ValueError('主目标不能放到子目标下面。')
                level = 'root'
            else:
                if not parent:
                    raise ValueError('请选择上级目标。')
                target = next((g for g in goals if g['id'] == parent), None)
                if not target:
                    raise KeyError(parent)
                archived = {g['id'] for g in goals if g.get('archived')}
                if any(parent in self.descendants(goals, p) for p in archived):
                    if not old or old.get('parent_id') != parent:
                        raise ValueError('请先恢复上级目标，再向它添加内容。')
                if key and parent in self.descendants(goals, key):
                    raise ValueError('目标不能放到自己或后代目标下面。')
                level = 'pillar' if target.get('level') == 'root' else 'goal'
            color = data.get('color', old.get('color') if old else '#9b8dd6')
            if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise ValueError('颜色格式不正确。')
            value = dict(title=title, parent_id=parent, level=level, color=color,
                         description=text(data.get('description', old.get('description', '') if old else ''), '目标说明', 4000),
                         archived=data.get('archived', old.get('archived', False) if old else False))
            value['sort_order'] = old.get('sort_order', 0) if old else max((g.get('sort_order', 0) for g in goals if g.get('parent_id') == parent), default=-1) + 1
            if not isinstance(value['archived'], bool):
                raise ValueError('归档状态格式不正确。')
            if old and old.get('level') == 'root' and value['archived']:
                raise ValueError('主目标请修改名称，不能归档整个体系。')
            if value['level'] == 'root':
                # 立轴字段：已建用户走「调整目标」补命题，不强制重建。
                value.update(self._root_extras(data, old))
            expected = data.get('revision') if key else 0
            if key and (type(expected) is not int or expected < 1):
                raise ValueError('请刷新目标后再保存。')
            return self.store.put('growth_goal', value, key, expected)

    def goal_ids(self, value, previous=None):
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ValueError('请选择有效目标标签。')
        goals = self.all('growth_goal')
        lookup = {g['id']: g for g in goals}
        result = list(dict.fromkeys(value))
        if len(result) > 100:
            raise ValueError('每条碎片最多关联 100 个目标。')
        archived = set()
        for g in goals:
            if g.get('archived'):
                archived.update(self.descendants(goals, g['id']))
        for key in result:
            if key not in lookup:
                raise ValueError('目标已不存在，请刷新后再选择。')
            if key in archived and key not in (previous or []):
                raise ValueError('归档目标不能增加新关联，请先恢复。')
        return result

    def fragment(self, data, key=None, create_key=None, origin=None, answer=None):
        with LOCK:
            old = self.need('growth_fragment', key) if key else {}
            body = text(data.get('text', old.get('text', '')), '碎片正文')
            from growth_transcript import source_url
            url = text(source_url(data.get('source_url', old.get('source_url', ''))), '来源链接', 4000)
            if url:
                parsed = urlsplit(url)
                if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('请粘贴不含登录凭据的 http 或 https 链接。')
            if not body and not url:
                raise ValueError('写下一点想法，或粘贴来源链接。')
            title = text(data.get('title', old.get('title', '')), '碎片标题', 160)
            title = title or (body.split('\n')[0][:60] if body else urlsplit(url).hostname)
            endorsement = data.get('endorsement', old.get('endorsement', 'reference_only'))
            if endorsement not in ('accepted', 'reference_only'):
                raise ValueError('请选择本人认可或仅作参考。')
            recorded = data.get('recorded_at', old.get('recorded_at') or now())
            try:
                dt = datetime.fromisoformat(recorded.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=TZ)
                recorded = dt.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('记录日期格式不正确。')
            kind = data.get('type', old.get('type', 'link' if url else 'text'))
            if kind not in ('text', 'link', 'voice'):
                raise ValueError('碎片类型不正确。')
            archived = data.get('archived', old.get('archived', False))
            if not isinstance(archived, bool):
                raise ValueError('归档状态格式不正确。')
            expected = data.get('revision') if key else 0
            if key and (type(expected) is not int or expected < 1):
                raise ValueError('请刷新碎片后再保存。')
            value = {**old, 'title': title, 'text': body, 'source_url': url, 'type': kind,
                     'goal_ids': self.goal_ids(data.get('goal_ids', old.get('goal_ids', [])), old.get('goal_ids')),
                     'recorded_at': recorded, 'archived': archived, 'endorsement': endorsement}
            voice_id = data.get('voice_id', old.get('voice_id', ''))
            if voice_id:
                if not isinstance(voice_id, str):
                    raise ValueError('录音引用格式不正确。')
                self.need('growth_voice', voice_id)
            value['voice_id'] = voice_id
            if origin:
                value.update(origin_knowledge_id=origin['id'], origin_category=origin.get('category', ''))
            if answer:
                value.update(origin_answer_id=answer['id'], origin_question_id=answer.get('question_id', ''))
            saved = self.store.put('growth_fragment', value, key or create_key, expected)
            # 2026-11-14 打通：碎片同时写进知识池（随手记），一处录入、处处可见。
            # 从已有认知导入的碎片不镜像，避免同一条内容存两份。
            if not origin:
                import content_index
                content_index.mirror_fragment(self.store, saved)
            return saved

    def import_knowledge(self, data):
        with LOCK:
            original = self.need('knowledge', data.get('knowledge_id'))
            if original.get('scope') == 'example':
                raise ValueError('示例库不是本人的积累，请在原库中核对。')
            key = 'growth_fragment_k_' + hashlib.sha256(original['id'].encode()).hexdigest()[:20]
            existing = self.store.get('growth_fragment', key)
            if existing:
                return {'fragment': existing, 'duplicate': True}
            value = self.fragment({'title': original['title'], 'text': original.get('body') or original.get('claim') or original['title'],
                                   'goal_ids': data.get('goal_ids', []), 'recorded_at': original['created_at'],
                                   'endorsement': 'accepted' if original.get('endorsement') == 'accepted' else 'reference_only'}, create_key=key, origin=original)
            return {'fragment': value, 'duplicate': False}

    def selected(self, query):
        goals = self.all('growth_goal')
        fragments = self.all('growth_fragment')
        if query.get('archived', [''])[0] != '1':
            fragments = [f for f in fragments if not f.get('archived')]
        goal = query.get('goal', [''])[0]
        if goal:
            scope = self.descendants(goals, goal)
            fragments = [f for f in fragments if scope.intersection(f.get('goal_ids', []))]
        if query.get('inbox', [''])[0] == '1':
            fragments = [f for f in fragments if not f.get('goal_ids')]
        q = query.get('q', [''])[0].strip().lower()
        if q:
            fragments = [f for f in fragments if q in (' '.join(str(f.get(k, '')) for k in ('title', 'text', 'source_url'))).lower()]
        period = query.get('month', [''])[0]
        if period:
            period = month_value(period)
            fragments = [f for f in fragments if record_month(f.get('recorded_at')) == period]
        day = query.get('day', [''])[0]
        if day:
            try:
                if datetime.strptime(day, '%Y-%m-%d').strftime('%Y-%m-%d') != day:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('请选择有效日期。')
            fragments = [f for f in fragments if record_day(f.get('recorded_at')) == day]
        kind = query.get('type', [''])[0]
        if kind:
            if kind not in ('text', 'link', 'voice'):
                raise ValueError('请选择有效来源类型。')
            fragments = [f for f in fragments if kind in source_types(f)]
        fragments.sort(key=lambda x: (x.get('recorded_at', ''), x['id']), reverse=True)
        return goals, fragments

    def _auto_candidates(self, goals):
        """返回 (分类→支柱目标, 候选内容行)。

        候选只来自第二大脑知识池：非示例、有效、未手动关联目标、本身没有目标标签。
        手动关联过的条目（碎片 origin_knowledge_id / knowledge_id 指向的原认知）跳过，
        保证同一条内容不会被手动和自动各算一次。只读，不写任何记录。

        2026-11-17（t27 修 bug①）：分类映射先按支柱默认名找，找不到就按默认顺序的
        sort_order 找——这样用户把「认知」改名成任何名字，自动归类都不会掉到 0。
        """
        mapping = pillar_mapping()
        if not mapping:
            return {}, []
        pillar_goals = [g for g in goals
                        if g.get('level') == 'pillar' and not g.get('archived')]
        by_title = {g['title']: g for g in pillar_goals}
        by_order = {}
        for goal in pillar_goals:
            try:
                by_order[int(goal.get('sort_order', -1))] = goal
            except (TypeError, ValueError):
                continue
        default_order = [name for name, _color in PILLARS]

        def resolve(name):
            if name in by_title:
                return by_title[name]
            if name in default_order:
                return by_order.get(default_order.index(name))
            return None

        targets = {}
        for category, name in mapping.items():
            goal = resolve(name)
            if goal is not None:
                targets[category] = goal
        if not targets:
            return {}, []
        import content_index
        content_index.ensure_fresh(self.store)
        linked = set()
        with self.store.connect() as con:
            for row in con.execute("SELECT data FROM records WHERE kind='growth_fragment'"):
                data = json.loads(row['data'])
                if not data.get('goal_ids'):
                    continue
                for key in ('origin_knowledge_id', 'knowledge_id'):
                    if data.get(key):
                        linked.add(data[key])
            columns = {row['name'] for row in con.execute('PRAGMA table_info(content_index)')}
            pool_sql = " AND pool='pool'" if 'pool' in columns else ''
            rows = con.execute(
                "SELECT source_kind,source_id,title,category,goal_ids,recorded_at,snippet FROM content_index "
                "WHERE (scope IS NULL OR scope!='example') "
                "AND (lifecycle IS NULL OR lifecycle='' OR lifecycle='active')" + pool_sql).fetchall()
        candidates, seen = [], set()
        for row in rows:
            if row['source_kind'] != 'knowledge':
                continue
            if row['source_id'] in linked or row['source_id'] in seen:
                continue
            try:
                if json.loads(row['goal_ids'] or '[]'):
                    continue
            except (ValueError, TypeError):
                continue
            seen.add(row['source_id'])
            candidates.append(row)
        return targets, candidates

    def auto_counts(self, goals, month):
        """第二大脑内容按分类自动归入支柱的统计结果（只读聚合）。"""
        targets, rows = self._auto_candidates(goals)
        if not targets:
            return {'goal_counts': {}, 'pillar_month': {}}
        import content_index
        lookup = {g['id']: g for g in goals}
        counts, month_counts = {}, {}
        for row in rows:
            target = targets.get(row['category'])
            if target is None:
                continue
            key = target['id']
            chain = set()
            while key in lookup and key not in chain:
                chain.add(key)
                counts[key] = counts.get(key, 0) + 1
                key = lookup[key].get('parent_id')
            if content_index.month_of(row['recorded_at']) == month:
                month_counts[target['id']] = month_counts.get(target['id'], 0) + 1
        return {'goal_counts': counts, 'pillar_month': month_counts}

    def goal_content(self, query):
        """某个目标节点下：手动标注条数 + 自动归类的第二大脑内容列表。"""
        goal_id = str(query.get('goal', [''])[0] or '')
        goals = self.all('growth_goal')
        lookup = {g['id']: g for g in goals}
        if goal_id not in lookup:
            raise ValueError('没有找到这个目标。')
        scope = self.descendants(goals, goal_id)
        manual = sum(1 for f in self.all('growth_fragment')
                     if not f.get('archived') and scope.intersection(f.get('goal_ids', [])))
        targets, rows = self._auto_candidates(goals)
        target_ids = {target['id'] for target in targets.values()}
        items, categories = [], {}
        for row in rows:
            target = targets.get(row['category'])
            if target is None or target['id'] not in scope or target['id'] not in target_ids:
                continue
            categories[row['category']] = categories.get(row['category'], 0) + 1
            excerpt = re.sub(r'\s+', ' ', str(row['snippet'] or '')).strip()
            items.append({'id': row['source_id'], 'title': row['title'],
                          'category': row['category'], 'recorded_at': row['recorded_at'],
                          'excerpt': excerpt[:140]})
        items.sort(key=lambda x: (x.get('recorded_at') or '', x['id']), reverse=True)
        return {'goal': {k: lookup[goal_id].get(k) for k in ('id', 'title', 'level', 'color')},
                'manual_count': manual, 'auto_count': len(items),
                'categories': categories, 'items': items}

    def overview(self, query):
        import growth_ask
        growth_ask.project_pending(self.store)
        goals, items = self.selected(query)
        active = [f for f in self.all('growth_fragment') if not f.get('archived')]
        month = month_value(query.get('month', [''])[0])
        period = [f for f in active if record_month(f.get('recorded_at')) == month]
        auto = self.auto_counts(goals, month)
        pillars = []
        for goal in sorted((g for g in goals if g.get('level') == 'pillar'), key=lambda x: (x.get('sort_order', 0), x['created_at'], x['id'])):
            scope = self.descendants(goals, goal['id'])
            pillars.append({**goal, 'count': sum(bool(scope.intersection(f.get('goal_ids', []))) for f in period)
                            + auto['pillar_month'].get(goal['id'], 0)})
        archived = set()
        for g in goals:
            if g.get('archived'):
                archived.update(self.descendants(goals, g['id']))
        try:
            page = max(1, int(query.get('page', ['1'])[0]))
        except ValueError:
            raise ValueError('页码格式不正确。')
        page = min(page, max(1, (len(items) + 23) // 24))
        review = self.store.get('growth_review', 'growth_review_' + month)
        months = sorted({record_month(f.get('recorded_at')) for f in active} - {''}, reverse=True)
        lookup = {g['id']: g for g in goals}
        counts = {g['id']: 0 for g in goals}
        for fragment in active:
            covered = set()
            for key in fragment.get('goal_ids', []):
                seen = set()
                while key in lookup and key not in seen:
                    seen.add(key)
                    covered.add(key)
                    key = lookup[key].get('parent_id')
            for key in covered:
                counts[key] += 1
        manual_counts = dict(counts)
        for key, number in auto['goal_counts'].items():
            counts[key] = counts.get(key, 0) + number
        days = {}
        for fragment in period:
            day = record_day(fragment.get('recorded_at'))
            if day:
                days[day] = days.get(day, 0) + 1
        from growth_transcript import decorate
        # 2026-11-14 新口径：首页三个计数按统一内容池算，不再只数成长碎片。
        import content_index
        pool = content_index.summary(self.store, month)
        # t69：milestone 结构化（后端只给结构；60 天进度由前端用 /checkin/state 计算，避免跨服务耦合）。
        root = next((g for g in goals if g.get('level') == 'root'), None)
        if root is not None:
            root['milestone_detail'] = {'text': root.get('milestone') or '', 'progress': None,
                                        'state': 'unknown', 'basis': '60天打卡',
                                        'thesis_evolution': len(root.get('thesis_history') or [])}
        journey = {}
        return {'goals': goals, 'items': [decorate(self.store, f) for f in items[(page-1)*24:page*24]], 'total': len(items), 'page': page, 'page_size': 24, 'journey': journey,
                'month': month, 'review': review, 'pillars': pillars, 'archived_goal_ids': sorted(archived),
                'stats': {'goals': sum(g['id'] not in archived for g in goals), 'fragments': pool['all'], 'month': pool['month'],
                          'inbox': pool['inbox'], 'by_category': pool['by_category']},
                'timeline': [{'month': m, 'count': sum(record_month(f.get('recorded_at')) == m for f in active)} for m in months[:12]],
                'goal_counts': counts, 'goal_counts_manual': manual_counts,
                'goal_counts_auto': auto['goal_counts'], 'daily_counts': days, 'features': {'local_video_transcript': True, 'fragment_delete': True},
                'storage': 'local', 'count_note': '全部积累=统一内容池里非示例、有效的条目（含随手记碎片）；本月=记录时间在本月；等待归位=还没有关联目标。'}

    def suggest_thesis(self):
        """立轴步骤：从三观卡/回答/赛道/主业结晶 2–3 条命题候选；模型不可用退回规则，不空转。

        隐私：只本地读；模型已配置时才把三观卡与四个关键回答发出去凝句（与 flow.anchors 同一既有口径），
        没配置就纯本地规则拼接。候选不是定稿，用户改/拍后才进 root.thesis。
        """
        inputs = axis_inputs(self.store)
        register_thesis_prompt()   # 规则回落也要登记，云端批准后才能同名覆盖
        if not inputs['card'] and not inputs['answers']:
            return {'ok': False, 'source': 'none', 'candidates': [], 'inputs': inputs,
                    'reason': '还没有三观卡或回答；先去建档，立轴引导才有你自己的话可用。'}
        candidates, source, error = [], 'rule', ''
        try:
            import providers
            if providers.available().get('llm'):
                answer = providers.llm_json(
                    thesis_prompt(inputs),
                    {'card': inputs['card'], 'answers': inputs['answers'],
                     'track': inputs['track_label'] or '（未选）',
                     'industry': inputs['industry'] or '（未填）'},
                    tag='growth_thesis')
                raw = answer.get('candidates') if isinstance(answer, dict) else None
                for item in (raw or [])[:3]:
                    if not isinstance(item, dict):
                        continue
                    thesis = str(item.get('thesis') or '').strip()
                    if not thesis:
                        continue
                    candidates.append({
                        'thesis': thesis[:600],
                        'source': str(item.get('source') or '').strip()[:200],
                        'boundary': str(item.get('boundary') or '').strip()[:300],
                        'connect': str(item.get('connect') or '').strip()[:300]})
                if candidates:
                    source = 'model'
        except Exception as exc:  # noqa: BLE001  模型失败不许挡住立轴
            error = type(exc).__name__
        if not candidates:
            candidates, source = _rule_candidates(inputs), 'rule'
        return {'ok': bool(candidates), 'source': source, 'candidates': candidates,
                'error': error, 'inputs': inputs, 'prompt_key': THESIS_PROMPT_KEY,
                'note': '候选不是定稿：改到你认领为止。命题可选，不否定你的实际目标（含赚钱）。'}

    def inbox(self, query):
        """等待归位：统一内容池里还没关联目标的条目——与首页 stats.inbox 同一口径、同一批数据。"""
        import content_index
        content_index.ensure_fresh(self.store)
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT source_kind,source_id,title,category,recorded_at,snippet,goal_ids "
                "FROM content_index WHERE pool='pool' AND (scope IS NULL OR scope!='example') "
                + content_index._active_sql()).fetchall()
        items = []
        for row in rows:
            try:
                if json.loads(row['goal_ids'] or '[]'):
                    continue
            except (ValueError, TypeError):
                continue
            items.append({'id': row['source_id'], 'kind': row['source_kind'],
                          'title': row['title'] or '未命名',
                          'category': row['category'] or '',
                          'recorded_at': row['recorded_at'] or '',
                          'excerpt': re.sub(r'\s+', ' ', str(row['snippet'] or '')).strip()[:140]})
        items.sort(key=lambda x: (x.get('recorded_at') or '', x['id']), reverse=True)
        return {'items': items[:300], 'total': len(items),
                'note': '统一内容池里还没有关联目标的条目（含知识库内容与随手记碎片），与首页「等待归位」同一批数据。'}

    def graph(self, query):
        goals, items = self.selected(query)
        scope = self.descendants(goals, query.get('goal', [''])[0])
        wanted = set(scope)
        lookup = {g['id']: g for g in goals}
        # Preserve the real cross-branch links of every returned fragment.
        wanted.update(key for f in items[:200] for key in f.get('goal_ids', []) if key in lookup)
        for key in list(wanted):
            parent = lookup[key].get('parent_id')
            seen = set()
            while parent and parent not in seen and parent in lookup:
                seen.add(parent); wanted.add(parent); parent = lookup[parent].get('parent_id')
        shown_goals = sorted((g for g in goals if g['id'] in wanted), key=lambda x: (len(self.path(goals, x['id']).split(' / ')), x['created_at'], x['id']))[:200]
        nodes = [{'id': g['id'], 'title': g['title'], 'kind': 'goal', 'level': g['level'], 'parent_id': g.get('parent_id'), 'color': g['color']} for g in shown_goals]
        nodes += [{'id': f['id'], 'title': f['title'], 'kind': 'fragment', 'goal_ids': f.get('goal_ids', [])} for f in items[:200]]
        known = {n['id'] for n in nodes}
        edges = [{'source': g['parent_id'], 'target': g['id']} for g in shown_goals if g.get('parent_id') in known]
        edges += [{'source': g, 'target': f['id']} for f in items[:200] for g in f.get('goal_ids', []) if g in known]
        return {'nodes': nodes, 'edges': edges, 'shown_goals': len(shown_goals), 'total_goals': len(wanted), 'shown_fragments': min(200, len(items)), 'total_fragments': len(items)}

    def review(self, data):
        month = month_value(data.get('month'))
        key = 'growth_review_' + month
        revision = data.get('revision')
        if type(revision) is not int or revision < 0:
            raise ValueError('请刷新复盘后再保存。')
        return self.store.put('growth_review', {'month': month, **{k: text(data.get(k, ''), label, 12000) for k, label in [('observation', '本月观察'), ('action', '已做的行动'), ('next', '下期关注')]}}, key, revision)

    def knowledge(self, key):
        with LOCK:
            fragment = self.need('growth_fragment', key)
            if fragment.get('archived'):
                raise ValueError('请先恢复这条碎片。')
            if fragment.get('endorsement') != 'accepted':
                raise ValueError('收藏不等于本人认同。请先编辑碎片，确认本人认可后再用于创作。')
            origin = fragment.get('origin_knowledge_id')
            if origin:
                knowledge = self.need('knowledge', origin)
                if knowledge.get('content_use') == 'review' or knowledge.get('endorsement') != 'accepted' or knowledge.get('lifecycle', 'active') != 'active':
                    raise ValueError('原认知仍有认同或用途限制，请先到原认知详情核对。')
                if fragment['text'] != (knowledge.get('body') or knowledge.get('claim') or knowledge['title']) or fragment['title'] != knowledge['title']:
                    raise ValueError('碎片与原认知内容已不同，请先到原认知详情核对；不会把旧原文当作新修改后的内容。')
                return {'knowledge_id': origin}
            existing = fragment.get('knowledge_id')
            if existing:
                knowledge = self.need('knowledge', existing)
                if knowledge.get('content_use') == 'review' or knowledge.get('endorsement') != 'accepted' or knowledge.get('lifecycle', 'active') != 'active':
                    raise ValueError('关联认知的状态已变化，请先到认知详情核对。')
                if knowledge.get('growth_fragment_revision') != fragment['revision']:
                    raise ValueError('碎片已更新，已转入的认知保留原版本。请到认知详情核对来源后使用。')
                return {'knowledge_id': existing}
            # 2026-11-14 打通后，新碎片在保存时就已镜像进知识池；这里直接复用那条，不再存第二份。
            import content_index
            mirror = self.store.get('knowledge', content_index.mirror_key(key))
            if mirror:
                return {'knowledge_id': mirror['id']}
            source = self.store.put('source', {'kind': 'growth_fragment', 'title': fragment['title'],
                                             'text': fragment['text'] + ('\n\n来源：' + fragment['source_url'] if fragment.get('source_url') else ''),
                                             'growth_fragment_id': key, 'source_url': fragment.get('source_url', '')})
            knowledge = self.store.put('knowledge', {'title': fragment['title'], 'claim': fragment['text'] or fragment['source_url'],
                'body': source['text'], 'category': '随手记', 'category_legacy': '成长积累',
                'category_aliases': ['随手记', '成长积累'], 'legacy_id': '成长积累', 'boundary': '', 'source_id': source['id'],
                'source_locator': {}, 'links': [], 'markers': ['由成长碎片转入'], 'endorsement': 'accepted', 'validation': 'unverified',
                'lifecycle': 'active', 'content_use': 'available', 'growth_fragment_id': key, 'growth_fragment_revision': fragment['revision'] + 1})
            self.store.patch('growth_fragment', key, {'knowledge_id': knowledge['id']}, fragment['revision'])
            return {'knowledge_id': knowledge['id']}

    def export(self, query):
        goals, items = self.selected(query)
        goal = query.get('goal', [''])[0]
        month = query.get('month', [''])[0]
        rows = ['# 我的成长积累', '', '导出时间：' + datetime.now(TZ).isoformat(timespec='seconds'),
                '范围：' + (self.path(goals, goal) if goal else '全部目标') + (' · ' + month if month else ' · 全部月份'),
                '共 ' + str(len(items)) + ' 条（含子目标，按碎片去重）。', '']
        for item in items:
            manuscript = item.get('transcript_text') or ''
            body = item.get('text', '')
            if manuscript and body == manuscript[:40000]:
                body = manuscript
            rows += ['## ' + item['title'], '', '记录时间：' + item['recorded_at'],
                     '目标：' + ('；'.join(self.path(goals, key) for key in item.get('goal_ids', [])) or '待整理'),
                     '本人判断：' + ('本人认可' if item.get('endorsement') == 'accepted' else '仅作参考'), '']
            answer = self.store.get('growth_answer', item.get('origin_answer_id')) if item.get('origin_answer_id') else None
            if answer and answer.get('status') == 'answered':
                snapshot = answer.get('question_snapshot') or {}
                rows += ['原问题：' + (snapshot.get('text') or answer.get('question_id', '')), '',
                         '回答：' + answer.get('text', ''), '']
                for probe in answer.get('probes') or []:
                    rows += ['追问：' + probe.get('question', ''), '', '追问回答：' + probe.get('answer', ''), '']
                rows += ['出处：问问自己 · ' + answer['id'], '']
            else:
                rows += [body, '']
            if manuscript and manuscript != body:
                rows += ['### 视频文字稿', '', manuscript, '']
            if item.get('source_url'):
                rows += ['来源：' + item['source_url'], '']
            if item.get('origin_knowledge_id'):
                rows += ['原认知：' + item['origin_knowledge_id'], '']
        if month:
            review = self.store.get('growth_review', 'growth_review_' + month)
            if review:
                rows += ['# 月度复盘 ' + month, '', '## 本月观察', review.get('observation', ''), '', '## 已做的行动', review.get('action', ''), '', '## 下期关注', review.get('next', ''), '']
        return '\n'.join(rows)

    def voice(self, payload):
        import local_asr
        info = local_asr.preflight()
        if not info.get('ok'):
            raise ValueError('本地语音环境未就绪，请直接打字或使用系统键盘语音输入。不会回落云端。')
        encoded = payload.get('audio', '')
        if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
            raise ValueError('录音数据过大，请缩短后重录。')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError('录音数据不完整，请重录。')
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ValueError('请录制 8MB 以内的语音。')
        mime = str(payload.get('mime', '')).split(';')[0]
        extensions = {'audio/webm': 'webm', 'audio/ogg': 'ogg', 'audio/mp4': 'm4a', 'audio/wav': 'wav', 'audio/x-wav': 'wav'}
        if mime not in extensions:
            raise ValueError('浏览器录音格式暂不支持，请使用系统键盘语音输入。')
        key = uid('growth_voice')
        directory = self.store.root / 'uploads' / 'growth-recordings'
        directory.mkdir(parents=True, exist_ok=True)
        audio = directory / (key + '.' + extensions[mime])
        audio.write_bytes(raw)
        self.store.put('growth_voice', {'path': str(audio), 'mime': mime, 'bytes': len(raw), 'status': 'recorded'}, key)
        # Call the local worker directly: provider auto/cloud fallback is intentionally unreachable.
        try:
            result = local_asr.transcribe(audio)
            transcript = str(result.get('text', '')).strip()
            if not transcript:
                raise ValueError('未识别出文字，录音仍保存在本机，可重新录制或打字。')
            self.store.patch('growth_voice', key, {'status': 'transcribed', 'text': transcript})
            return {'text': transcript, 'voice_id': key, 'route': 'local'}
        except Exception:
            self.store.patch('growth_voice', key, {'status': 'failed'})
            raise ValueError('本地转写未完成，录音已保存在本机。可重录或直接打字；不会自动调用云端。')



# ---------------------------------------------------------------------------
# 2026-11-17 t68 回流闭环：命题修订候选（AI 只提候选，命题永远用户拍板）
#   存根：root goal 上的 thesis_proposals（待拍板/已采纳/已拒绝）＋ thesis_history（版本史）。
#   采纳才改 root.thesis；拒绝只留痕。可回看每版命题及其来源 insight。
# ---------------------------------------------------------------------------
THESIS_PROPOSAL_LIMIT = 20
THESIS_HISTORY_LIMIT = 50


def _root_goal(store):
    goals = [g for g in store.list('growth_goal', 500) if g.get('level') == 'root']
    return goals[0] if goals else None


def _clean_thesis(text, label='命题', limit=600, required=True):
    value = ' '.join(str(text or '').split()).strip()
    if required and not value:
        raise ValueError('%s不能为空。' % label)
    if len(value) > limit:
        value = value[:limit]
    return value


def thesis_proposals(store):
    """当前命题＋修订候选＋版本史。字段缺省一律空列表，前端可直接渲染。"""
    root = _root_goal(store)
    if not root:
        return {'thesis': '', 'proposals': [], 'history': [], 'pending': 0}
    proposals = [p for p in (root.get('thesis_proposals') or []) if isinstance(p, dict)]
    history = [h for h in (root.get('thesis_history') or []) if isinstance(h, dict)]
    return {'thesis': str(root.get('thesis') or ''), 'proposals': proposals,
            'history': history,
            'pending': len([p for p in proposals if p.get('status') == 'pending'])}


def create_thesis_proposal(store, proposed, original='', reason='', insight_id='',
                           source='review_insight', evidence_count=None):
    """登记一条命题修订候选（不自动采纳）。去重：同一候选文本已有 pending 时直接返回旧的。"""
    root = _root_goal(store)
    if not root:
        raise ValueError('还没有立主线；先立轴，再谈命题修订候选。')
    current = str(root.get('thesis') or '')
    proposed = _clean_thesis(proposed, '候选命题', 600, True)
    if proposed == current:
        raise ValueError('候选命题与当前命题相同；不生成候选。')
    existing = [p for p in (root.get('thesis_proposals') or [])
                if isinstance(p, dict) and p.get('status') == 'pending'
                and p.get('proposed') == proposed]
    if existing:
        return existing[0]
    stamp = now()
    proposal = {'id': uid('thesis_proposal'), 'proposed': proposed,
                'original': current or _clean_thesis(original, '原命题', 600, False),
                'reason': str(reason or '')[:1200], 'insight_id': insight_id or '',
                'source': source, 'evidence_count': evidence_count,
                'status': 'pending', 'created_at': stamp, 'decided_at': None}
    proposals = [p for p in (root.get('thesis_proposals') or []) if isinstance(p, dict)]
    proposals = [proposal] + proposals[:THESIS_PROPOSAL_LIMIT - 1]
    store.patch('growth_goal', root['id'], {'thesis_proposals': proposals})
    return proposal


def decide_thesis_proposal(store, proposal_id, accept, thesis=None, decided_by='owner'):
    """用户拍板：accept=True 采纳（可带改写后的 thesis）；False 拒绝。采纳才更新命题＋写版本史。

    2026-11-17 t75（主理人拍板）：命题是核心信念，**不允许自动漂移**——任何
    decided_by='reflow_auto'（或其它非人工来源）的决策一律拒绝，候选保持 pending，
    等本人在 growth 页批量审/一键采纳。人工采纳后仍可用 undo 回滚。
    """
    if (decided_by or 'owner') != 'owner':
        raise ValueError('命题候选必须由本人拍板：自动采纳已关闭，请到成长页审核后采纳。')
    root = _root_goal(store)
    if not root:
        raise KeyError(proposal_id)
    proposals = [p for p in (root.get('thesis_proposals') or []) if isinstance(p, dict)]
    target = next((p for p in proposals if p.get('id') == proposal_id), None)
    if not target:
        raise KeyError(proposal_id)
    if target.get('status') != 'pending':
        raise ValueError('这条候选已经拍板过了（%s），不能重复采纳/拒绝。' % target.get('status'))
    stamp = now()
    target['decided_at'] = stamp
    target['decided_by'] = decided_by or 'owner'
    changes = {'thesis_proposals': proposals}
    if accept:
        final = _clean_thesis(thesis or target.get('proposed'), '命题', 600, True)
        previous = str(root.get('thesis') or '')
        history = [h for h in (root.get('thesis_history') or []) if isinstance(h, dict)]
        history.append({'thesis': final, 'previous': previous, 'at': stamp,
                        'from_proposal': target.get('id'), 'insight_id': target.get('insight_id') or '',
                        'source': target.get('source') or '',
                        'decided_by': decided_by or 'owner'})
        target['status'] = 'accepted'
        target['thesis'] = final
        changes['thesis'] = final
        changes['thesis_history'] = history[-THESIS_HISTORY_LIMIT:]
        sources = [x for x in (root.get('thesis_source') or []) if isinstance(x, str)]
        if target.get('insight_id'):
            sources.append('reflow:' + str(target['insight_id']))
        changes['thesis_source'] = list(dict.fromkeys(sources))[:20]
    else:
        target['status'] = 'rejected'
    updated = store.patch('growth_goal', root['id'], changes)
    return {'goal_id': root['id'], 'thesis': str(updated.get('thesis') or ''),
            'proposal': target, 'history': updated.get('thesis_history') or [],
            'decided': 'accepted' if accept else 'rejected'}



def undo_thesis_decision(store, proposal_id, decided_by='owner'):
    """撤销一次已采纳的命题修订（一键回滚）：把命题恢复到该候选之前的上一版，
    并在版本史里追加一条「撤销」记录；候选标记 rolled_back（可再审/再提）。"""
    root = _root_goal(store)
    if not root:
        raise KeyError(proposal_id)
    proposals = [p for p in (root.get('thesis_proposals') or []) if isinstance(p, dict)]
    target = next((p for p in proposals if p.get('id') == proposal_id), None)
    if not target:
        raise KeyError(proposal_id)
    if target.get('status') not in ('accepted', 'rolled_back'):
        raise ValueError('这条候选还没被采纳，没有可撤销的版本。')
    if target.get('status') == 'rolled_back':
        raise ValueError('这次自动采纳已经撤销过了。')
    history = [h for h in (root.get('thesis_history') or []) if isinstance(h, dict)]
    entry = next((h for h in reversed(history) if h.get('from_proposal') == proposal_id), None)
    previous = str((entry or {}).get('previous') or '')
    if not entry or not previous:
        raise ValueError('没有上一版命题可以回滚。')
    current = str(root.get('thesis') or '')
    stamp = now()
    history.append({'thesis': previous, 'previous': current, 'at': stamp,
                    'undo_of': proposal_id, 'source': 'undo', 'decided_by': decided_by or 'owner'})
    target['status'] = 'rolled_back'
    target['rolled_back_at'] = stamp
    updated = store.patch('growth_goal', root['id'], {
        'thesis': previous, 'thesis_history': history[-THESIS_HISTORY_LIMIT:],
        'thesis_proposals': proposals})
    return {'goal_id': root['id'], 'thesis': str(updated.get('thesis') or ''),
            'restored_from': proposal_id, 'proposal': target,
            'history': updated.get('thesis_history') or []}

# 2026-11-18 t138：我的资产仪表盘——只读聚合，不新增模型调用。
def _asset_day(value):
    text = str(value or '')
    if len(text) >= 10 and text[4:5] == '-' and text[7:8] == '-':
        return text[:10]
    return ''


# 2026-11-19 t166：时间胶囊——封存预测/决定，到期对照现实，累积判断校准率。
CAPSULE_DAYS = (30, 90)
CAPSULE_RESULTS = ('right', 'partial', 'wrong')


def _cap_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def create_capsule(store, text, days=30, reason='', knowledge_ids=None):
    value = str(text or '').strip()
    if len(re.sub(r'\s+', '', value)) < 6:
        raise ValueError('胶囊内容太短：先写一条具体的预测或决定。')
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30
    if days not in CAPSULE_DAYS:
        days = 30
    now = _cap_now()
    record = {'text': value[:500], 'reason': str(reason or '')[:300], 'days': days,
              'created_at': now.isoformat(timespec='seconds'),
              'open_at': (now + __import__('datetime').timedelta(days=days)).isoformat(timespec='seconds'),
              'status': 'sealed', 'calibration': None, 'result_note': '',
              'knowledge_ids': [str(x) for x in (knowledge_ids or []) if x][:20]}
    return store.put('capsule', record)


def list_capsules(store):
    from datetime import datetime, timezone
    now = _cap_now()
    out = []
    for item in store.list('capsule', 10000):
        value = dict(item)
        try:
            open_at = datetime.fromisoformat(str(value.get('open_at') or ''))
        except Exception:
            open_at = now
        value['due'] = value.get('status') == 'sealed' and open_at <= now
        value['open_text'] = open_at.strftime('%Y-%m-%d')
        value['days_left'] = max(0, (open_at.date() - now.date()).days)
        out.append(value)
    out.sort(key=lambda x: (not x.get('due'), str(x.get('open_at') or '')))
    return out


def open_capsule(store, capsule_id, result, note=''):
    result = str(result or '').strip()
    if result not in CAPSULE_RESULTS:
        raise ValueError('校准只能选：对 / 部分 / 错。')
    item = store.get('capsule', str(capsule_id or ''))
    if not item:
        raise KeyError('胶囊不存在。')
    try:
        from datetime import datetime
        due_at = datetime.fromisoformat(str(item.get('open_at') or ''))
    except Exception:
        due_at = None
    if due_at is not None and due_at > _cap_now():
        raise ValueError('这颗胶囊还没到期，先不打扰你。')
    return store.patch('capsule', str(capsule_id), {
        'status': 'opened', 'calibration': result,
        'result_note': str(note or '')[:500],
        'opened_at': _cap_now().isoformat(timespec='seconds')})


def capsule_stats(store):
    counts = {'right': 0, 'partial': 0, 'wrong': 0}
    for item in store.list('capsule', 10000):
        key = str(item.get('calibration') or '')
        if key in counts:
            counts[key] += 1
    total = sum(counts.values())
    rate = None
    if total >= 3:
        rate = round((counts['right'] + counts['partial'] * 0.5) / total * 100, 1)
    return {'counts': counts, 'total': total, 'rate': rate,
            'note': ('已校准 %d 次：对 %d / 部分 %d / 错 %d；校准率 %.1f%%。'
                     % (total, counts['right'], counts['partial'], counts['wrong'], rate))
                    if rate is not None else
                    ('已校准 %d 次：对 %d / 部分 %d / 错 %d；样本还少，先看计数，不装百分比。'
                     % (total, counts['right'], counts['partial'], counts['wrong']))}


# 2026-11-19 t167：每日晨报＋每日一次睡前巩固（候选需确认，不自动合并）。
def _day_key(value):
    return str(value or '')[:10]


def _today():
    from datetime import date
    return date.today().isoformat()


def nightly_consolidation(store):
    """每日一次跑 t157 巩固：只沉底+产出候选计数，不自动合并；同一天重入不重跑。"""
    today = _today()
    try:
        last = store.get('brain_nightly', 'last') or {}
    except Exception:
        last = {}
    if last.get('date') == today:
        return last
    summary = {'date': today, 'at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
               'sunk': 0, 'duplicates': 0, 'themes': 0, 'error': ''}
    try:
        scan = consolidation_scan(store, do_sink=True)
        summary['sunk'] = int(scan.get('sunk') or 0)
        summary['duplicates'] = len(scan.get('duplicates') or [])
        summary['themes'] = len(scan.get('themes') or [])
        summary['empty'] = bool(scan.get('empty'))
    except Exception as exc:
        summary['error'] = str(exc)[:100]
    try:
        store.put('brain_nightly', summary, 'last')
    except Exception:
        pass
    return summary


def morning_brief(store):
    """晨报：昨日新增/一个盲点/里程碑进度/主线相关一张卡，最多 4 条；无内容不硬凑。"""
    import content_index
    nightly = nightly_consolidation(store)
    today = _today()
    try:
        seen = (store.get('brain_morning_seen', 'last') or {}).get('date') == today
    except Exception:
        seen = False
    from datetime import date, timedelta
    yday = (date.today() - timedelta(days=1)).isoformat()
    try:
        cards = [x for x in store.list('knowledge', 100000)
                 if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        cards = []
    new_cards = [x for x in cards if _day_key(x.get('recorded_at') or x.get('created_at')) == yday]
    items = []
    if new_cards:
        items.append({'key': 'new', 'label': '昨日新增认知',
                      'text': '昨天新增 %d 条认知；最近一条：「%s」。' % (len(new_cards), str(new_cards[-1].get('title') or '')[:60])})
    try:
        blind = content_index.blind_spots(store, limit=1)
        if blind.get('dimensions'):
            d = blind['dimensions'][0]
            items.append({'key': 'blind', 'label': '一个盲点',
                          'text': '「%s」还没有记录：%s' % (d.get('title'), d.get('reason') or '')})
    except Exception:
        pass
    try:
        ms = milestones(store)
        cur = ms.get('current') or {}
        has_progress = bool(ms.get('achieved')) or any(int(cur.get(k) or 0) > 0 for k in ('knowledge', 'works', 'streak'))
        if ms.get('next') and has_progress:
            items.append({'key': 'milestone', 'label': '里程碑进度',
                          'text': ms['next']['text'] + '；已达成 ' + '、'.join(x['label'] for x in ms['achieved'][-2:]) if ms['achieved'] else ms['next']['text']})
    except Exception:
        pass
    try:
        context = content_index.mainline_context(store)
        if context:
            found = content_index.search(store, context.get('title') or '主线', limit=12, include_body=False)
            card = next((x for x in (found.get('items') or [])
                         if x.get('source_kind') == 'knowledge' and x.get('mainline')), None)
            if card:
                items.append({'key': 'focus', 'label': '今日主线相关',
                              'text': '按「%s」，先看：「%s」' % (context.get('title'), str(card.get('title') or '')[:60]),
                              'card_id': card.get('source_id') or ''})
    except Exception:
        pass
    items = items[:4]
    arranged = nightly.get('sunk', 0) + nightly.get('duplicates', 0) + nightly.get('themes', 0)
    header = ''
    if arranged:
        header = ('昨晚帮你整理了 %d 条：沉底 %d；合并候选 %d / 提炼候选 %d（都要你确认）。'
                  % (arranged, nightly.get('sunk', 0), nightly.get('duplicates', 0), nightly.get('themes', 0)))
    text = ('早上好。' + (header + '\n\n' if header else '') +
            ('\n'.join('· ' + x['text'] for x in items) if items else '今天没有新内容，先不硬凑；想开始就记一条。'))
    return {'date': today, 'already_seen': seen, 'items': items, 'empty': not items,
            'header': header, 'nightly': nightly, 'text': text,
            'note': '晨报只挑 4 条以内；没内容就不硬凑。'}


def mark_morning_seen(store):
    try:
        return store.put('brain_morning_seen', {'date': _today(),
                         'at': __import__('datetime').datetime.now().isoformat(timespec='seconds')}, 'last')
    except Exception:
        return {}


def milestones(store):
    """t161：真实里程碑（第N条认知/第一个作品/连续打卡）＋下一目标进度；不空夸。"""
    try:
        cards = [x for x in store.list('knowledge', 100000)
                 if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        cards = []
    try:
        works = [x for x in store.list('project', 10000) if not x.get('is_test')]
    except Exception:
        works = []
    streak = 0
    try:
        import checkin
        state = checkin.state(store, store.root)
        days = state.get('days') or []
        for day in reversed(days):
            if day.get('state') == 'checked':
                streak += 1
            elif day.get('state') in ('missed', 'today'):
                break
    except Exception:
        streak = 0
    achieved = []

    def add(label, current, target, unit):
        if current >= target:
            achieved.append({'label': label, 'target': target, 'unit': unit})
            return True
        return False

    for n in (1, 10, 50, 100, 200, 500):
        add('第 %d 条认知' % n, len(cards), n, '条')
    for n in (1, 5, 10):
        add('第 %d 个作品' % n, len(works), n, '个')
    for n in (7, 30, 60):
        add('连续打卡 %d 天' % n, streak, n, '天')
    current = {'knowledge': len(cards), 'works': len(works), 'streak': streak}
    candidates = [('knowledge', n, '第 %d 条认知' % n) for n in (10, 50, 100, 200, 500)
                  if len(cards) < n]
    candidates += [('works', n, '第 %d 个作品' % n) for n in (1, 5, 10) if len(works) < n]
    candidates += [('streak', n, '连续打卡 %d 天' % n) for n in (7, 30, 60) if streak < n]
    candidates.sort(key=lambda x: (x[1] - current[x[0]]))
    nxt = None
    if candidates:
        key, target, label = candidates[0]
        nxt = {'label': label, 'current': current[key], 'target': target,
               'remaining': target - current[key],
               'text': '下一个：%s（%d/%d）' % (label, current[key], target)}
    return {'achieved': achieved, 'next': nxt, 'current': current,
            'note': '只按真实记录计算；没到就不显示已达成。'}


def assets_summary(store):
    """真实资产计数＋按记录日期的累计曲线（近 30/90 天）＋一句基于真实数的资产厚度。"""
    from datetime import date, timedelta

    def items(kind):
        try:
            return store.list(kind, 100000)
        except Exception:
            return []

    knowledge = [x for x in items('knowledge') if (x.get('lifecycle') or 'active') == 'active']
    works = [x for x in items('project') if not x.get('is_test')]
    import style_profile
    try:
        corpus = style_profile.owner_corpus_stats(store)
    except Exception:
        corpus = {'count': 0, 'chars': 0}
    voice_count = int(corpus.get('count') or 0)
    voices = [None] * voice_count
    fragments = items('growth_fragment')

    dated = []
    for group in (knowledge, works, voices, fragments):
        for x in group:
            if not isinstance(x, dict):
                continue
            day = _asset_day(x.get('recorded_at') or x.get('created_at') or x.get('updated_at'))
            if day:
                dated.append(day)
    days = sorted(set(dated))
    today = date.today()
    all_cards = items('knowledge')
    counts = {'knowledge': len(knowledge), 'works': len(works),
              'voices': voice_count, 'voice_chars': int(corpus.get('chars') or 0),
              'fragments': len(fragments), 'days': len(days),
              'dormant': sum(1 for x in all_cards if x.get('dormant')),
              'consolidated': sum(1 for x in all_cards
                                  if x.get('merged_into') or '主题提炼' in (x.get('markers') or []))}
    total = counts['knowledge'] + counts['works'] + counts['voices'] + counts['fragments']

    def series(days_count):
        start = today - timedelta(days=days_count - 1)
        out = []
        for offset in range(days_count):
            day = start + timedelta(days=offset)
            upto = sum(1 for d in dated if d <= day.isoformat())
            out.append({'date': day.isoformat(), 'total': upto})
        return out

    if total == 0:
        thickness = '资产从第一条开始：先记一条认知，这里就会长出第一笔。'
    elif counts['days'] == 0:
        thickness = ('已经有 %d 段真实口播语料；再记一条带日期的认知或作品，资产曲线就从今天开始。'
                     % counts['voices'])
    elif total < 5 or counts['days'] <= 7:
        thickness = ('刚起步：你已经留下 %d 条资产、跨 %d 天。先别急着多，先让每一条都真的是你的判断。'
                     % (total, counts['days']))
    else:
        thickness = ('你已积累 %d 条资产、跨 %d 天。它们越用越值钱：写稿、选题、复盘都会回来找你；'
                     '这些带得走，也抄不走；声音样本让你的稿子更像你、少点 AI 味。' % (total, counts['days']))
    return {'counts': counts, 'total': total, 'first_day': days[0] if days else '',
            'milestones': milestones(store), 'morning': morning_brief(store),
            'today': today.isoformat(), 'series': {30: series(30), 90: series(90)},
            'thickness': thickness,
            'note': '只统计本机真实记录；没有记录时不估算、不补数。'}



# ---------------------------------------------------------------- 天赋发现（t149）
# 24 项 VIA 简化勾选；不引入新依赖、不调模型。证据只取本人本机记录。
TALENT_VIA = [
    ('creativity', '创造力', '想出新点子、做出不一样的东西', ['创意', '想法', '设计', '创作', '拍', '写', '灵感']),
    ('curiosity', '好奇心', '对新鲜事愿意追问', ['好奇', '为什么', '探索', '研究', '学']),
    ('judgment', '开放思维', '看多面，不急着下结论', ['角度', '判断', '多面', '反思', '权衡']),
    ('learning', '好学', '愿意学新东西', ['学习', '学会', '看书', '课程', '练习']),
    ('perspective', '洞察力', '看透本质、抓关键', ['本质', '洞察', '关键', '规律', '看透']),
    ('bravery', '勇敢', '敢说敢做、不怕难', ['敢', '勇气', '突破', '冒险', '争取']),
    ('perseverance', '毅力', '坚持做完', ['坚持', '持续', '完成', '长期', '积累']),
    ('honesty', '正直', '真实、不装', ['真实', '诚实', '不装', '原则', '底线']),
    ('zest', '热情', '有劲头、有生命力', ['热情', '有劲', '喜欢', '热爱', '兴奋']),
    ('love', '爱', '关心、亲近人', ['关心', '陪伴', '爱', '家人', '朋友']),
    ('kindness', '善良', '愿意帮人', ['帮助', '善意', '照顾', '支持', '温暖']),
    ('social', '社交智慧', '懂人、会相处', ['沟通', '理解人', '关系', '倾听', '相处']),
    ('teamwork', '团队', '愿意协作', ['合作', '团队', '一起', '协作', '配合']),
    ('fairness', '公平', '讲理、不偏', ['公平', '公正', '道理', '规则', '平等']),
    ('leadership', '领导', '能带人、能组织', ['带领', '组织', '负责', '决定', '发起']),
    ('forgiveness', '宽恕', '能放下、不记仇', ['原谅', '放下', '宽容', '算了', '和解']),
    ('humility', '谦逊', '不端、愿意听', ['谦逊', '低调', '学习别人', '不吹', '承认']),
    ('prudence', '谨慎', '想清楚再动', ['谨慎', '风险', '核对', '小心', '准备']),
    ('selfcontrol', '自控', '管住自己、按计划做', ['自律', '控制', '计划', '克制', '习惯']),
    ('appreciation', '审美', '看得出好坏与美', ['审美', '好看', '质感', '美', '品味']),
    ('gratitude', '感恩', '记得别人的好', ['感谢', '感恩', '幸运', '记得', '珍惜']),
    ('hope', '希望', '往好处看、有盼头', ['希望', '未来', '期待', '相信', '可能']),
    ('humor', '幽默', '会逗、能轻松', ['幽默', '好笑', '开玩笑', '轻松', '有趣']),
    ('spirituality', '灵性', '关心意义与更大的东西', ['意义', '使命', '灵魂', '价值', '信仰']),
]
TALENT_VIA_MAP = {k: (label, hint, words) for k, label, hint, words in TALENT_VIA}


def _talent_texts(store):
    """从 active 认知、碎片、打卡记录里取真实文本；返回可回源的条目。"""
    out = []
    try:
        for x in store.list('knowledge', 10000):
            if (x.get('lifecycle') or 'active') != 'active':
                continue
            body = ' '.join(str(x.get(k) or '') for k in ('title', 'claim', 'body', 'boundary'))
            if body.strip():
                out.append({'source': 'knowledge', 'id': x.get('id'), 'title': x.get('title') or '',
                            'text': body.strip(), 'date': (x.get('updated_at') or x.get('created_at') or '')[:10]})
    except Exception:
        pass
    try:
        for x in store.list('growth_fragment', 10000):
            if x.get('archived'):
                continue
            body = ' '.join(str(x.get(k) or '') for k in ('title', 'text'))
            if body.strip():
                out.append({'source': 'fragment', 'id': x.get('id'), 'title': x.get('title') or '',
                            'text': body.strip(), 'date': (x.get('recorded_at') or x.get('created_at') or '')[:10]})
    except Exception:
        pass
    try:
        import os
        root = Path(os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent / '.runtime'))) / 'checkin'
        for path in sorted(root.glob('*.jsonl')) if root.is_dir() else []:
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                body = str(item.get('text') or item.get('note') or '').strip()
                if body:
                    out.append({'source': 'checkin', 'id': item.get('id') or path.stem,
                                'title': item.get('date') or path.stem, 'text': body,
                                'date': str(item.get('date') or '')[:10]})
    except Exception:
        pass
    return out


def talent_mining(store):
    texts = _talent_texts(store)
    blob = '\n'.join(x['text'] for x in texts)
    signals = []
    for key, label, hint, words in TALENT_VIA:
        hits = []
        for x in texts:
            if any(w and w in x['text'] for w in words):
                hits.append(x)
        if hits:
            examples = []
            for x in hits[:2]:
                snippet = x['text'].replace('\n', ' ')
                examples.append({'source': x['source'], 'id': x['id'], 'title': x['title'],
                                 'quote': snippet[:120], 'date': x['date']})
            signals.append({'key': key, 'label': label, 'hint': hint, 'count': len(hits),
                            'examples': examples})
    signals.sort(key=lambda x: (-x['count'], x['label']))
    return {'texts': len(texts), 'signals': signals,
            'counts': {'knowledge': sum(1 for x in texts if x['source'] == 'knowledge'),
                       'fragments': sum(1 for x in texts if x['source'] == 'fragment'),
                       'checkins': sum(1 for x in texts if x['source'] == 'checkin')}}


def talent_get(store):
    profile = store.get('talent_profile', 'current')
    mining = talent_mining(store)
    return {'via': [{'key': k, 'label': l, 'hint': h} for k, l, h, _ in TALENT_VIA],
            'mining': mining, 'profile': profile,
            'need_evidence': (not profile) and mining['texts'] == 0}


def talent_assess(store, data):
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    selected = data.get('via') or []
    if not isinstance(selected, list):
        raise ValueError('VIA 选择格式不正确。')
    selected = [str(x) for x in selected if str(x) in TALENT_VIA_MAP][:24]
    ikigai = data.get('ikigai') if isinstance(data.get('ikigai'), dict) else {}
    ikigai = {k: text(ikigai.get(k, ''), 'Ikigai ' + k, 500) for k in ('love', 'good', 'needs', 'paid')}
    best = data.get('best') if isinstance(data.get('best'), list) else []
    best = [text(x, '最佳时刻', 1200) for x in best if isinstance(x, str) and x.strip()][:3]
    mining = talent_mining(store)
    evidence_pool = {s['key']: s for s in mining['signals']}
    scored = []
    for key, label, hint, words in TALENT_VIA:
        if key not in selected:
            continue
        ev = evidence_pool.get(key)
        count = (ev or {}).get('count', 0)
        scored.append({'key': key, 'label': label, 'hint': hint, 'count': count,
                       'examples': (ev or {}).get('examples', [])})
    scored.sort(key=lambda x: (-x['count'], x['label']))
    if not scored:
        scored = [{'key': s['key'], 'label': s['label'], 'hint': s['hint'],
                   'count': s['count'], 'examples': s['examples']} for s in mining['signals'][:5]]
    if not scored:
        raise ValueError('还没有可用的天赋证据：先记几条认知或碎片，再做轻评估。')
    talents = []
    fallback_best = next((b for b in best if b), '')
    for item in scored[:5]:
        ev = item['examples'][:2]
        if not ev and fallback_best:
            ev = [{'source': 'self', 'id': '', 'title': '最佳自我', 'quote': fallback_best[:120], 'date': ''}]
        if not ev:
            continue
        direction = '用「%s」做一期你熟悉的真实案例：把过程、判断和踩过的坑讲清楚。' % item['label']
        if ikigai.get('needs'):
            direction = '用「%s」讲你看到的真实需要：%s。先写一条具体经历，再给方法。' % (
                item['label'], ikigai['needs'][:80])
        elif ikigai.get('good'):
            direction = '把你擅长「%s」的过程拆成一条口播：从一次真实任务讲起，最后给一个可执行动作。' % item['label']
        talents.append({'key': item['key'], 'label': item['label'],
                        'why': '你的记录里出现 %d 次相关线索' % item['count'] if item['count'] else '来自你的轻评估与最佳时刻',
                        'evidence': ev, 'direction': direction})
    if not talents:
        raise ValueError('证据不足：先记几条认知或碎片，再来生成画像。')
    portrait = {'talents': talents, 'ikigai': ikigai, 'best': best,
                'counts': mining['counts'], 'texts': mining['texts'],
                'generated_at': now()}
    saved = store.put('talent_profile', {'portrait': portrait, 'via': selected,
                                         'ikigai': ikigai, 'best': best,
                                         'generated_at': now()}, 'current')
    return {'profile': saved, 'portrait': portrait, 'mining': mining}


def talent_topic(store, title, talent_key=''):
    title = text(title, '选题标题', 160, True)
    first = None
    try:
        for x in store.list('knowledge', 10000):
            if (x.get('lifecycle') or 'active') == 'active' and (x.get('title') or x.get('claim')):
                first = x
                break
    except Exception:
        pass
    import contracts
    rec = contracts.create_topic(title, knowledge_id=(first or {}).get('id'), status='candidate',
                                 note='来自天赋画像：%s' % str(talent_key)[:80])
    return store.put('topic', rec)


# 2026-11-19 t157：大脑巩固＋遗忘（只做机制，不预置知识；合并/提炼需本人确认）。
FORGET_DAYS = 90
MAINTENANCE_DAYS = 7


def _norm_claim(text):
    return re.sub(r'\s+', '', str(text or '')).strip()


def _bigrams(text):
    chars = re.sub(r'[^\u4e00-\u9fff]', '', str(text or ''))
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def _similar(a, b):
    left, right = _bigrams(a), _bigrams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / float(len(left | right))


def _active_knowledge(store):
    try:
        return [x for x in store.list('knowledge', 100000)
                if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        return []


def consolidation_scan(store, do_sink=True):
    """扫描重复/主题/可沉底；返回候选（需确认）与计数。空库返回空，不做操作。"""
    from datetime import datetime, timedelta, timezone
    cards = _active_knowledge(store)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=FORGET_DAYS)).isoformat()
    duplicates = []
    seen = set()
    for i, left in enumerate(cards):
        if len(duplicates) >= 8:
            break
        for right in cards[i + 1:]:
            key = tuple(sorted((str(left.get('id')), str(right.get('id')))))
            if key in seen:
                continue
            sim = _similar(left.get('claim') or left.get('title'), right.get('claim') or right.get('title'))
            if sim >= 0.55:
                seen.add(key)
                duplicates.append({'ids': [left.get('id'), right.get('id')],
                                   'titles': [left.get('title'), right.get('title')],
                                   'claims': [(left.get('claim') or '')[:80], (right.get('claim') or '')[:80]],
                                   'similarity': round(sim, 2)})
                break
    by_cat = {}
    for card in cards:
        by_cat.setdefault(str(card.get('category') or '未分类'), []).append(card)
    themes = []
    for cat, group in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 3:
            continue
        sample = max(group, key=lambda x: len(_norm_claim(x.get('claim') or '')))
        claim = _norm_claim(sample.get('claim') or sample.get('title'))[:80]
        themes.append({'category': cat, 'count': len(group), 'pattern': True,
                       'source_ids': [x.get('id') for x in group[:6]],
                       'draft': '你似乎在坚持一个原则：在「%s」上，我反复回到「%s」' % (cat, claim)})
        if len(themes) >= 12:
            break
    dormant = [x for x in cards if x.get('dormant')]
    eligible = []
    for card in cards:
        if card.get('dormant'):
            continue
        if (card.get('endorsement') or '') == 'accepted':
            continue
        if int(card.get('used_count') or 0) > 0 or card.get('last_used_at'):
            continue
        stamp = str(card.get('last_used_at') or card.get('recorded_at') or card.get('created_at') or '')
        if stamp and stamp < cutoff:
            eligible.append(str(card.get('id')))
    sunk = 0
    if do_sink and eligible:
        for kid in eligible:
            try:
                store.patch('knowledge', kid, {'dormant': True, 'dormant_at': now.isoformat(timespec='seconds')})
                sunk += 1
            except Exception:
                pass
    dormant_cards = [{'id': str(x.get('id')), 'title': str(x.get('title') or '')[:60]}
                     for x in cards if x.get('dormant')]
    return {'duplicates': duplicates, 'themes': themes,
            'eligible': eligible, 'sunk': sunk, 'dormant_cards': dormant_cards,
            'dormant_count': len(dormant) + sunk,
            'consolidated_count': sum(1 for x in store.list('knowledge', 100000)
                                      if x.get('merged_into') or '主题提炼' in (x.get('markers') or [])),
            'cutoff': cutoff, 'empty': not cards}


def merge_knowledge(store, ids, title='', note=''):
    """确认后合并：保留全部原文，重复卡归档并指向主卡；不删除。"""
    ids = [str(x) for x in (ids or []) if x]
    if len(ids) < 2:
        raise ValueError('合并至少选两条。')
    cards = [store.get('knowledge', kid) for kid in ids]
    if any(card is None for card in cards):
        raise KeyError('有认知卡不存在，请刷新后重试。')
    from datetime import datetime
    canonical = cards[0]
    canonical_id = str(canonical.get('id'))
    merged = []
    for card in cards[1:]:
        if str(card.get('id')) == canonical_id:
            continue
        merged.append(str(card.get('id')))
        store.patch('knowledge', str(card.get('id')),
                    {'lifecycle': 'archived', 'merged_into': canonical_id,
                     'merged_at': datetime.now().isoformat(timespec='seconds'),
                     'markers': list(dict.fromkeys((card.get('markers') or []) + ['已合并（原文保留）']))})
    patch = {'merged_ids': list(dict.fromkeys((canonical.get('merged_ids') or []) + merged)),
             'markers': list(dict.fromkeys((canonical.get('markers') or []) + ['合并主卡']))}
    if title:
        patch['title'] = str(title)[:160]
    if note:
        patch['merge_note'] = str(note)[:300]
    store.patch('knowledge', canonical_id, patch)
    return {'ok': True, 'canonical_id': canonical_id, 'merged_ids': merged}


def save_principle(store, text, source_ids=None):
    """确认后把主题提炼成「我的一句原则」；客户不确认不调用。"""
    value = str(text or '').strip()
    if len(re.sub(r'\s+', '', value)) < 8:
        raise ValueError('原则太短，先写一句完整判断（至少 8 字）。')
    sources = [str(x) for x in (source_ids or []) if x]
    from db import now
    record = {'legacy_id': '原则', 'title': value[:60], 'category': '原则', 'category_legacy': '原则',
              'category_aliases': [], 'claim': value, 'body': value, 'boundary': '',
              'source_id': None, 'source_url': '', 'source_locator': {}, 'links': [],
              'markers': ['本人确认', '主题提炼'], 'endorsement': 'accepted',
              'validation': 'unverified', 'lifecycle': 'active', 'content_use': 'available',
              'goal_ids': [], 'recorded_at': now(), 'principle_from': sources}
    saved = store.put('knowledge', record)
    return {'ok': True, 'knowledge': saved}


def restore_dormant(store, ids):
    restored = []
    for kid in [str(x) for x in (ids or []) if x]:
        item = store.get('knowledge', kid)
        if not item:
            continue
        store.patch('knowledge', kid, {'dormant': False, 'dormant_at': None})
        restored.append(kid)
    return {'ok': True, 'restored': restored}


def maintenance_status(store):
    last = store.get('brain_maintenance', 'last')
    cards = store.list('knowledge', 100000)
    dormant = sum(1 for x in cards if x.get('dormant'))
    consolidated = sum(1 for x in cards if x.get('merged_into') or '主题提炼' in (x.get('markers') or []))
    from datetime import datetime, timezone
    last_at = str((last or {}).get('at') or '')
    due = True
    if last_at:
        try:
            due = (datetime.now(timezone.utc) - datetime.fromisoformat(last_at)).days >= MAINTENANCE_DAYS
        except Exception:
            due = True
    return {'last_scan_at': last_at, 'due': due, 'dormant': dormant, 'consolidated': consolidated}


def mark_maintenance(store):
    from db import now
    store.put('brain_maintenance', {'at': now()}, 'last')


def route(handler, method, bits, query, store, submit_job=None):
    if bits[:2] != ['api', 'growth']:
        return False
    import growth_ask
    if growth_ask.route(handler, method, bits, query, store):
        return True
    import growth_search
    if growth_search.route(handler, method, bits, query, store):
        return True
    service = Growth(store)
    import growth_transcript
    tail = bits[2:]
    if method == 'GET' and tail == ['morning']:
        handler.json({'ok': True, **morning_brief(store)})
    elif method == 'POST' and tail == ['morning', 'seen']:
        mark_morning_seen(store)
        handler.json({'ok': True})
    elif method == 'GET' and tail == ['overview']:
        handler.json(service.overview(query))
    elif method == 'GET' and tail == ['consolidate']:
        handler.json({'ok': True, **consolidation_scan(store, do_sink=False),
                      'status': maintenance_status(store)})
    elif method == 'POST' and tail == ['consolidate', 'run']:
        result = consolidation_scan(store, do_sink=True)
        mark_maintenance(store)
        handler.json({'ok': True, **result, 'status': maintenance_status(store)})
    elif method == 'POST' and tail == ['consolidate', 'merge']:
        body = handler.body()
        handler.json(merge_knowledge(store, body.get('ids') or [], body.get('title') or '', body.get('note') or ''))
    elif method == 'POST' and tail == ['consolidate', 'principle']:
        body = handler.body()
        handler.json(save_principle(store, body.get('text') or '', body.get('source_ids') or []), 201)
    elif method == 'POST' and tail == ['consolidate', 'restore']:
        handler.json(restore_dormant(store, handler.body().get('ids') or []))
    elif method == 'GET' and tail == ['assets']:
        handler.json(assets_summary(store))
    elif method == 'GET' and tail == ['talent']:
        handler.json(talent_get(store))
    elif method == 'POST' and tail == ['talent', 'assess']:
        handler.json(talent_assess(store, handler.body()), 201)
    elif method == 'POST' and tail == ['talent', 'topic']:
        body = handler.body()
        handler.json(talent_topic(store, body.get('title') or '', body.get('talent') or ''), 201)
    elif method == 'GET' and tail == ['goal-content']:
        handler.json(service.goal_content(query))
    elif method == 'GET' and tail == ['graph']:
        handler.json(service.graph(query))
    # 立轴引导（t27）：命题候选 + 等待归位清单（与 stats.inbox 同源）
    elif method == 'GET' and tail == ['thesis', 'suggest']:
        handler.json(service.suggest_thesis())
    elif method == 'GET' and tail == ['thesis', 'proposals']:
        handler.json(thesis_proposals(store))
    elif method == 'POST' and tail == ['thesis', 'proposals']:
        body = handler.body()
        handler.json({'proposal': create_thesis_proposal(
            store, body.get('proposed') or body.get('thesis') or '',
            original=body.get('original') or '', reason=body.get('reason') or '',
            insight_id=body.get('insight_id') or '', source=body.get('source') or 'manual',
            evidence_count=body.get('evidence_count'))}, 201)
    elif (method == 'POST' and len(tail) == 4 and tail[:2] == ['thesis', 'proposals']
          and tail[3] == 'decide'):
        body = handler.body()
        handler.json(decide_thesis_proposal(store, tail[2], bool(body.get('accept')),
                                            thesis=body.get('thesis')))
    elif (method == 'POST' and len(tail) == 4 and tail[:2] == ['thesis', 'proposals']
          and tail[3] == 'undo'):
        handler.json(undo_thesis_decision(store, tail[2]))
    elif method == 'GET' and tail == ['inbox']:
        handler.json(service.inbox(query))
    elif method == 'POST' and tail == ['setup']:
        handler.json(service.setup(handler.body()), 201)
    elif method == 'POST' and tail == ['goals']:
        handler.json(service.goal(handler.body()), 201)
    elif method == 'PATCH' and len(tail) == 2 and tail[0] == 'goals':
        handler.json(service.goal(handler.body(), tail[1]))
    elif method == 'POST' and tail == ['fragments']:
        body = handler.body()
        if body.get('transcribe') is True and body.get('source_url'):
            growth_transcript.validate_video_url(body['source_url'])
        fragment = service.fragment(body)
        if body.get('transcribe') is True and fragment.get('source_url'):
            if submit_job is None:
                raise ValueError('转写任务尚未加载，请重新启动工作台。')
            fragment = growth_transcript.start(store, fragment['id'], fragment['revision'], submit_job)
        handler.json(growth_transcript.decorate(store, fragment), 201)
    elif method == 'POST' and tail == ['from-knowledge']:
        handler.json(service.import_knowledge(handler.body()), 201)
    elif method == 'GET' and len(tail) == 2 and tail[0] == 'fragments':
        handler.json(growth_transcript.decorate(store, service.need('growth_fragment', tail[1])))
    elif method == 'PATCH' and len(tail) == 2 and tail[0] == 'fragments':
        handler.json(service.fragment(handler.body(), tail[1]))
    elif method == 'POST' and len(tail) == 3 and tail[0] == 'fragments' and tail[2] == 'transcribe':
        if submit_job is None:
            raise ValueError('转写任务尚未加载，请重新启动工作台。')
        handler.json(growth_transcript.start(store, tail[1], handler.body().get('revision'), submit_job), 202)
    elif method == 'POST' and len(tail) == 3 and tail[0] == 'fragments' and tail[2] == 'delete':
        body = handler.body()
        handler.json(growth_transcript.delete(store, tail[1], body.get('revision'), body.get('confirm')))
    elif method == 'GET' and len(tail) == 3 and tail[0] == 'fragments' and tail[2] == 'transcript':
        item = service.need('growth_fragment', tail[1])
        extension = 'md' if query.get('format', [''])[0] == 'md' else 'txt'
        target = growth_transcript.transcript_file(store, item, extension)
        handler.send_file(target, download=True, filename='文字稿-' + item['id'][-8:] + '.' + extension)
    elif method == 'POST' and len(tail) == 3 and tail[0] == 'fragments' and tail[2] == 'knowledge':
        handler.json(service.knowledge(tail[1]))
    elif method == 'POST' and tail == ['reviews']:
        handler.json(service.review(handler.body()))
    elif method == 'POST' and tail == ['voice']:
        handler.json(service.voice(handler.body(limit=12 * 1024 * 1024)))
    elif method == 'GET' and tail == ['export']:
        raw = service.export(query).encode('utf-8-sig')
        extension = 'txt' if query.get('format', [''])[0] == 'txt' else 'md'
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/plain; charset=utf-8')
        handler.send_header('Content-Disposition', 'attachment; filename="growth-' + month_value(query.get('month', [''])[0]) + '.' + extension + '"')
        handler.send_header('Content-Length', str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)
    else:
        handler.json({'error': '成长系统接口不存在。'}, 404)
    return True
