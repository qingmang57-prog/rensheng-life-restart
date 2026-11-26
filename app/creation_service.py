"""创作方法融合：复用选题、项目和建议；所有生成均保留输入快照。"""
import hashlib
import json
import re
import prompt_store
import providers
import long_text
import contracts
import branding
import style_profile
import auto_wash
import material
from db import Conflict, uid, now

STORE = None
NEW_JOB = None
PARTS_VALID = None
DEFAULT_PROFILE = {'name':'','account':'','audience':'',
    'style':'','redlines':'不虚构本人经历；不把参考观点当亲身验证',
    'industry':'','experience':'','product':'',
    'pain_points':'','after_view':'','monetization':'',
    'update_frequency':'','video_length':'','taboos':'','hotwords':''}
# PATCH /api/profile 允许写入的档案字段；新增字段与 DEFAULT_PROFILE 同步，老档案缺键时自动补空。
PROFILE_TEXT_KEYS = ('name','account','audience','style','redlines','industry','experience','product',
    'pain_points','after_view','monetization','update_frequency','video_length','taboos','hotwords')
STRUCTURES = [
    {'id':'knowledge','label':'教知识','icon':'◇','hint':'把一个问题讲明白','subtypes':[
        ['recommend','推荐型','美好愿景 → 圈定人群 → 引发好奇'],
        ['solve','解题型','场景难题 → 低行动成本 → 操作过程'],
        ['case','案例型','案例描述 → 知识总结 → 应用方法'],
        ['reveal','揭秘型','提出问题 → 讲清内幕 → 避免方法']]},
    {'id':'process','label':'晒过程','icon':'◉','hint':'让观众看见怎么做','subtypes':[
        ['show','过程展示','进货筹备、咨询成交、技术服务、售后'],
        ['test','测评产品','对比测评、极限测评、化验测评'],
        ['challenge','任务挑战','成本限制、限时、限定地点'],
        ['experience','事件体验','新奇体验、特殊规则、角色互换']]},
    {'id':'opinion','label':'聊观点','icon':'↗','hint':'说清你为什么这样想','subtypes':[
        ['people','人群观点','写作对象 → 人物关系 → 冲突事件'],
        ['industry','行业观点','核心对象 → 误解、批判、支持或建议']]},
    {'id':'story','label':'讲故事','icon':'⌁','hint':'用真实经历承载观点','subtypes':[
        ['achievement','小有成就','个人成就 → 困境转机 → 结尾感悟'],
        ['success','成功案例','他人成就 → 困境转机 → 感悟'],
        ['hero','平凡英雄','冲突事件 → 英雄时刻 → 事件总结'],
        ['hardship','苦难经历','缺陷描述 → 产生原因 → 困苦释怀'],
        ['journey','重走成功路','成功标的 → 艰辛经历 → 结尾感悟']]}]
ELEMENTS = [['cost','成本','时间、金钱与投入'],['crowd','人群','身份、第一次体验'],
    ['curiosity','奇葩','陌生规则与少见做法'],['leader','头牌','具有代表性的对象'],
    ['nostalgia','怀旧','过去与现在'],['contrast','反差','身份、选择与结果的反差'],
    ['worst','最差','失败与避坑'],['appeal','荷尔蒙','吸引力与个人魅力']]
REASONS = [['effect','效果好','真实效果与适用条件'],['reviews','好评多','真实评价与复购'],
    ['value','性价比','同等需求下的成本'],['owner','老板好','人物与经营原则'],
    ['convenience','便利性','距离、时间与使用成本'],['expertise','专业强','经验与专业能力'],
    ['service','服务好','响应、细致与售后'],['distinct','有特色','环境、产品或服务差异'],
    ['choice','选择多','品类与搭配'],['status','有面子','品味与使用场景'],
    ['quality','质量好','材料、耐用与可靠性'],['business','生意好','真实订单与客流'],
    ['scale','规模大','设备、团队与承接能力'],['cases','案例多','实际案例与适用范围'],
    ['appearance','颜值高','视觉体验与环境']]
CATALOG = {'structures':STRUCTURES,'elements':ELEMENTS,'reasons':REASONS,
    'goals':[['attention','吸引关注','找到值得点开的角度'],['conversion','促进成交','说清为什么值得选择'],['pain','探索痛点','找到观众正在纠结的问题']]}


def init(store, new_job, parts_valid):
    global STORE, NEW_JOB, PARTS_VALID
    STORE, NEW_JOB, PARTS_VALID = store, new_job, parts_valid


def require(kind, key):
    item = STORE.get(kind, key)
    if item is None:
        raise KeyError('没有找到该记录。')
    return item


def profile():
    record = {**DEFAULT_PROFILE, **(STORE.get('profile','owner') or {})}
    # 名字只认白牌配置源；没填过就是空，首启三问会写入。
    record['name'] = branding.configured_name(STORE)
    return record


def update_profile(data):
    old = STORE.get('profile','owner')
    record = {**DEFAULT_PROFILE, **(old or {})}
    pending_name = None
    for key in PROFILE_TEXT_KEYS:
        if key in data:
            if not isinstance(data[key],str) or len(data[key]) > 5000:
                raise ValueError('个人档案字段需为5000字以内的文字。')
            if key == 'name':
                pending_name = data[key]
                record[key] = data[key].strip()
            else:
                record[key] = data[key].strip()
    expected = data.get('revision')
    if expected is not None:
        expected = int(expected)
    else:
        expected = old['revision'] if old else 0
    if expected != (old['revision'] if old else 0):
        raise Conflict('个人档案已在另一页面更新，请刷新后比较再保存。')
    if pending_name is not None:
        record['name'] = branding.set_name(STORE, pending_name)
    return STORE.put('profile', record, 'owner', expected)


def text(data, key, maximum=5000):
    value = data.get(key,'')
    if not isinstance(value,str) or len(value)>maximum:
        raise ValueError(key+' 需要为长度合适的文字。')
    return value.strip()


def choices(value, allowed, maximum=3):
    if not isinstance(value,list) or len(value)>maximum or any(x not in allowed for x in value):
        raise ValueError('方法选项无效，请重新选择。')
    return list(dict.fromkeys(value))


SHARE_TO_OPTIONS = ('闺蜜', '老公老婆', '同事', '老板', '爸妈', '自己收藏')
CONTENT_TYPE_OPTIONS = ('情感', '认知思维', '怎么赚钱', '营销方法', '商家带货', '商家门店')
# 判法清单：与前端 BENCH_DIRECTIONS 的标签一致；一次生成的多条选题必须判法互不相同。
BASIS_LIST = ('唱个反调', '替观众说', '讲清后果', '起个名', '往后会怎样', '我改过主意', '两个选择', '给三条做法', '蹭个热点', '我后悔过')


def _norm_basis(value):
    """把模型返回的判法名归一到清单原文；对不上返回空串。"""
    v = str(value or '').strip()
    if v in BASIS_LIST:
        return v
    for item in BASIS_LIST:
        if v and (v in item or item in v):
            return item
    return ''


def knowledge_snapshot(keys):
    keys=choices(keys, [x['id'] for x in STORE.list('knowledge',10000)], 10)
    result=[]
    for key in keys:
        item=require('knowledge',key)
        if item.get('content_use')!='available' or item.get('lifecycle')!='active' or item.get('endorsement') in ('rejected','reference_only'):
            raise ValueError('认知尚未允许用于写稿，或已归档/不认同，请先在我的经验库核对。')
        result.append({k:item.get(k) for k in ('id','revision','title','claim','boundary','endorsement','validation')})
    return result


# 三观默认注入内容工厂：用户没有显式选择认知（knowledge_ids）时，默认带最多 3 条本人三观。
# 开关字段 use_default_principles，默认 True；显式选了 knowledge_ids 就只用自己的，不覆盖。
PRINCIPLES_DEFAULT_COUNT = 3
PRINCIPLES_TOTAL_CHARS = 600
PRINCIPLES_CLAIM_CHARS = 130
PRINCIPLES_BOUNDARY_CHARS = 50


def flag(value, default=True):
    """把前端传来的开关解析成布尔：缺省/空值按默认，明确 false/0/no/off 才算关。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ('0', 'false', 'no', 'off')


def _clip(value, limit):
    text = str(value or '').strip()
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in '。；！？!?;')
    if cut >= int(limit * 0.6):
        return head[:cut + 1]
    return head


def _is_principle(item):
    # 2026-11-14 分类合并后，「三观」并入「认知底座」：新分类、旧分类、别名与标题都要认，
    # 否则默认三观会被静默跳过，写稿时不再带本人立场。
    names = ['三观', '认知底座']
    legacy = str(item.get('category_legacy') or '')
    aliases = item.get('category_aliases') or []
    if item.get('category') in names or legacy in names:
        return True
    if any(str(x) in names for x in aliases):
        return True
    return '三观' in str(item.get('title') or '') or '三观' in str(item.get('legacy_id') or '')


def default_principles_snapshot(limit=PRINCIPLES_DEFAULT_COUNT):
    """取可用于写稿的本人三观条目；空知识库返回空列表，不报错。"""
    items = []
    for item in STORE.list('knowledge', 10000):
        if not _is_principle(item):
            continue
        if item.get('content_use') != 'available' or item.get('lifecycle') != 'active':
            continue
        if item.get('endorsement') in ('rejected', 'reference_only'):
            continue
        if not str(item.get('claim') or '').strip():
            continue
        items.append(item)
    # 优先用建档生成的三观卡（sanguan_*），其次按 三观-01/02/03 顺序，其余按 id 稳定排序。
    card_order = {'sanguan_worldview': 0, 'sanguan_life': 1, 'sanguan_values': 2}

    def order(item):
        ident = str(item.get('id') or '')
        if ident in card_order:
            return (0, card_order[ident], ident)
        tail = str(item.get('legacy_id') or '')[-2:]
        if tail.isdigit():
            return (1, int(tail), ident)
        return (2, 0, ident)

    items.sort(key=order)
    limit = max(0, int(limit))
    if limit <= 0:
        return []
    result = []
    seen = set()
    for item in items:
        # 同一 legacy_id 只取一条：有当前三观卡时不重复带旧导入的同号条目。
        marker = str(item.get('legacy_id') or item.get('title') or item.get('id') or '')
        if marker in seen:
            continue
        seen.add(marker)
        result.append({k: item.get(k) for k in ('id', 'title', 'claim', 'boundary')})
        if len(result) >= limit:
            break
    return result


def principles_block(items, total_chars=PRINCIPLES_TOTAL_CHARS):
    """把三观压成提示词块：每条主张/边界各自截断，整块不超过 total_chars。"""
    lines = []
    for index, item in enumerate(list(items or [])[:PRINCIPLES_DEFAULT_COUNT], 1):
        claim = _clip(str(item.get('claim') or '').replace('\r', ' ').replace('\n', ' '), PRINCIPLES_CLAIM_CHARS)
        if not claim:
            continue
        line = '%d. %s：%s' % (index, str(item.get('title') or '我的三观'), claim)
        boundary = _clip(str(item.get('boundary') or '').replace('\r', ' ').replace('\n', ' '), PRINCIPLES_BOUNDARY_CHARS)
        if boundary:
            line += '（边界：' + boundary + '）'
        lines.append(line)
    block = '\n'.join(lines)
    return block if len(block) <= int(total_chars) else _clip(block, total_chars)


# ---------------------------------------------------------------------------
# 2026-11-17 激活闭环（L8）：主线轴上下文＋源积累回溯＋内容人格引擎最小版。
# 全部只读本地真实记录；读不到就如实返回空，不编造主线/信念。
# ---------------------------------------------------------------------------

def axis_context(store=None):
    """读主线（growth_goal root）与命题：只读，不写。

    主线未建立时返回 {'main_goal': None, 'thesis': ''}，下游按「无主线」处理，不报错。
    命题（thesis）字段由立轴引导写入；老库没有该字段时为空串。
    目标含赚钱是正当的：这里原样透传用户自己写的主线，不做价值评判。
    """
    store = store or STORE
    root = None
    try:
        for goal in store.list('growth_goal', 2000):
            if goal.get('level') == 'root' and not goal.get('archived'):
                root = goal
                break
    except Exception:
        root = None
    if root is None:
        return {'main_goal': None, 'thesis': ''}
    return {'main_goal': {'id': root['id'], 'title': str(root.get('title') or '')[:160],
                          'description': str(root.get('description') or '')[:600]},
            'thesis': str(root.get('thesis') or '').strip()[:1200]}


def source_knowledge_snapshot(store, project):
    """回溯项目的源积累：显式选用的 knowledge_ids＋写稿时生成的 knowledge_candidate_ids。

    逐条回验真实存在；查不到的 ID 丢弃不编造。返回 (ids, items)；
    直接写稿（未关联积累）的项目返回 ([], [])，不报错。
    items 保留 endorsement/validation 原值，供下游如实展示「待验证/仅作参考」。
    """
    ids = []
    for key in list(project.get('knowledge_ids') or []) + list(project.get('knowledge_candidate_ids') or []):
        if isinstance(key, str) and key and key not in ids:
            ids.append(key)
    valid, items = [], []
    for key in ids[:20]:
        item = store.get('knowledge', key)
        if item is None:
            continue
        valid.append(key)
        items.append({'id': key, 'title': str(item.get('title') or '')[:160],
                      'claim': str(item.get('claim') or '')[:600],
                      'boundary': str(item.get('boundary') or '')[:200],
                      'category': item.get('category') or '',
                      'endorsement': item.get('endorsement') or '',
                      'validation': item.get('validation') or ''})
    return valid, items


PERSONA_BELIEF_LIMIT = 5
PERSONA_CLAIM_CHARS = 160
PERSONA_STYLE_CHARS = 1500
PERSONA_TOTAL_CHARS = 2400


def persona_block(knowledge_items, principles, style_text, axis, hooks=None):
    """内容人格引擎：信念×口吻×主线 → 提示词块。

    信念只取本人真实认知/三观（claim 原文截断），口吻取风格卡/档案，
    主线取 axis_context；任何一块缺失就省略该块，不补写、不编造。
    2026-11-17 t30：可选 hooks（conviction 钩子候选，均带源 id）追加为钩子段；
    钩子只从本块信念/素材原话长出来，无源不编。
    """
    beliefs = list(knowledge_items or [])
    beliefs += [{'title': p.get('title'), 'claim': p.get('claim'), 'boundary': p.get('boundary')}
                for p in (principles or [])]
    lines, seen = [], set()
    for item in beliefs:
        claim = _clip(str(item.get('claim') or '').replace('\r', ' ').replace('\n', ' '), PERSONA_CLAIM_CHARS)
        if not claim or claim in seen:
            continue
        seen.add(claim)
        line = '%d. %s：%s' % (len(lines) + 1, str(item.get('title') or '我的信念'), claim)
        boundary = _clip(str(item.get('boundary') or ''), 60)
        if boundary:
            line += '（边界：' + boundary + '）'
        lines.append(line)
        if len(lines) >= PERSONA_BELIEF_LIMIT:
            break
    parts = []
    if lines:
        parts.append('我的信念（只用这些我真实写过的主张，不虚构我的经历）：\n' + '\n'.join(lines))
    style_text = _clip(str(style_text or '').replace('\r', ' '), PERSONA_STYLE_CHARS)
    if style_text:
        parts.append('我的口吻：\n' + style_text)
    axis = axis or {}
    main_goal = axis.get('main_goal')
    if main_goal and main_goal.get('title'):
        goal_line = '主线（引力中心；年度目标只是里程碑参考，不要求每条硬扣）：' + str(main_goal['title'])
        if main_goal.get('description'):
            goal_line += '（' + _clip(str(main_goal['description']), 120) + '）'
        parts.append(goal_line)
    if axis.get('thesis'):
        parts.append('我的命题：' + _clip(str(axis['thesis']), 300))
    if hooks:
        parts.append(conviction_hook_section(hooks))
    return _clip('\n'.join(parts), PERSONA_TOTAL_CHARS)


# 人格引擎的本地口径（随 payload 下发，不改云端提示词模板）：
# 信念×口吻产出「更像我」的选题/角度/初稿；赚钱是正当目标；结论一律待验证。
PERSONA_RULE_TOPICS = ('persona 块是本人真实信念×口吻×主线。主线是引力中心：默认多数选题从主线/命题长出来，保持垂直；'
                       '但允许真实旁支（生活、经验、其他主题），不硬扣年度目标——年度目标只是里程碑参考。'
                       '每条选题从其中一条信念长出来，并把对应信念的 id 写进该条 knowledge_ids；标题与切入角度用本人口吻。'
                       '旁支选题请在 angle 里用一句自然说明「这条与你主线/声音的关联」（可空、不强制，不为了关联硬扯）。'
                       '所有选题都是待验证假设，不得声称必然火/已被验证；不虚构本人经历。')
POLISH_RULE = (
    '【人味润色】这是把已有草稿改成本人说话，不是重写观点：\n'
    '1) 只改表达，不改观点、事实、数字、例子；【待补】原样保留，不许编。\n'
    '2) 沿用语感：短句优先、允许停顿和口语连接、允许本人常用词；不要为了「顺」把句子拉长。\n'
    '3) 删掉套话连接词（首先/其次/总之/换句话说）、排比升华、号召腔（让我们一起…）。\n'
    '4) 保留清单：具体人名/数字/时间/地点、本人原话里的比喻、口癖、反问句。\n'
    '5) 自查（写前对自己说一遍）：每句能否想象本人张口说出来？有没有一句是「AI 会写但他说不出口」的？'
    '有就改写或删掉。只返回 {"parts":[{"role","text"}]}，三段齐全。'
)

PERSONA_RULE_SCRIPT = ('初稿用 persona 块里本人的真实信念与口吻来写。默认服务主线/命题，保持方向一致；'
                       '允许真实旁支：年度目标只是里程碑参考，不硬扣每条，也不把稿写成 KPI 汇报。'
                       '旁支稿用一句自然带过它与你主线/声音的关联，不硬扯、不空喊。'
                       '「你为什么信」那句确信留给本人自己补，不虚构本人经历与数据；不写已被验证/必然有效类结论。')


# ---------------------------------------------------------------------------
# 2026-11-17 t30 · conviction 钩子（钩子＝真话本身，不是诱导模板）
# 依据：2026-11-16/短视频专业方法集_20261117.md §二（主理人批准 conviction-first）。
# 硬约束：①从信念×口吻×主线长出真话，不套 6 种诱导模板；②必须溯源本人真实积累
# （knowledge_ids/三观卡），无源不编；③合规自检：禁绝对化/夸大/虚假悬念；
# ④6 种工厂类型只作表达参考，套用后变操纵则弃；⑤钩子＝真话，不制造悬念诱骗完播。
# ---------------------------------------------------------------------------
HOOK_MAX_CHARS = 18
HOOK_SOURCE_LIMIT = 5
# 绝对化/夸大字眼（命中即不合规；"最"/"第一"另用正则判，避免误伤"最近/第一步"等常用词）
HOOK_ABSOLUTE = ('100%', '百分百', '根治', '绝对', '唯一', '永久', '永远',
                 '必火', '包治', '万能', '无敌', '史上最', '全网最')
_HOOK_ABSOLUTE_RE = re.compile('最(?!近|后|初|终|新|少|多)')
_HOOK_ORDINAL_RE = re.compile('第一(?!步|次|天|年|周|月|季|期|眼|时间|件|种|回|层|遍|条|晚|早|秒|分钟|阶段|轮|笔|批|套|版|集|页|章|段|堂课)')
# 虚假悬念/诱导句式（工厂钩子特征；命中即判操纵）
HOOK_CLICKBAIT = ('你一定想不到', '万万没想到', '看完你就', '惊呆', '震惊', '揭秘', '内幕',
                  '不看后悔', '后悔一辈子', '99%的人', '大多数人不知道', '真相是', '偷偷告诉你')
_HOOK_PUNCT = '，。！？、；：“”‘’《》（）()—…·-'

PERSONA_RULE_HOOK = ('开头前 3 秒用一句真话钩子（台词不超过 18 字）：这句话只能从 persona 的信念/素材原话里'
                     '长出来，来源写进 hook_source_id；不套 6 种工厂钩子模板，不用绝对化/夸大字眼，'
                     '不制造虚假悬念骗完播；没有可溯源的真实积累就不写钩子（留空，不编）。钩子是真话本身，'
                     '不是诱饵。')


def _first_sentence(text, limit=60):
    """取第一句（到。！？；为止）；用于钩子取 claim 原话，不改写、不扩写。"""
    s = str(text or '').strip()
    if not s:
        return ''
    return _clip(re.split(r'[。！？；!?;]', s)[0].strip(), limit)


def conviction_sources(knowledge_items, principles=None):
    """钩子可溯源的真实积累：只收有 id 且 claim 有内容的条目；无 id/无 claim 不收、不编。"""
    items = list(knowledge_items or []) + list(principles or [])
    out, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = str(item.get('id') or '').strip()
        first = _first_sentence(item.get('claim'))
        if not key or not first or key in seen:
            continue
        seen.add(key)
        out.append({'id': key, 'title': _clip(str(item.get('title') or ''), 60), 'claim_first': first})
        if len(out) >= HOOK_SOURCE_LIMIT:
            break
    return out


def _hook_from_claim(first):
    """从 claim 首句提炼规则钩子（≤18 字）：优先取一个完整短分句，其次截断；不改写原话。"""
    text = str(first or '').strip()
    if not text:
        return ''
    if _hook_chars(text) <= HOOK_MAX_CHARS:
        return text
    for seg in re.split(r'[，,、；;：:]', text):
        seg = seg.strip()
        if 4 <= _hook_chars(seg) <= HOOK_MAX_CHARS:
            return seg
    return _clip(text, HOOK_MAX_CHARS)


def _hook_chars(text):
    """钩子字数：去掉空白与标点后计数（台词 ≤18 字）。"""
    return len(re.sub(r'\s+', '', str(text or '')).translate({ord(c): None for c in _HOOK_PUNCT}))


def hook_source(text, sources):
    """把钩子回溯到某条真实积累：与 claim 首句/标题的最长公共子串 ≥4 字才算有源。"""
    text = str(text or '').strip()
    best_id, best_len = None, 0
    for src in sources or []:
        for field in (str(src.get('claim_first') or ''), str(src.get('title') or '')):
            if not field:
                continue
            for i in range(len(text)):
                for j in range(len(text), i + 3, -1):
                    piece = text[i:j]
                    if len(piece) > best_len and piece in field:
                        best_len, best_id = len(piece), src.get('id')
    return best_id


def hook_compliance(hook_text, source_id=None, source_pool=None):
    """合规自检（本地规则）：绝对化词/夸大、虚假悬念、超长、无源。

    返回 {'ok':bool,'chars':int,'issues':[{code,detail}],'source_id':...}；
    source_id 显式给出时不再回溯；否则用 source_pool 回溯，回溯不到记 no_source（无源不编）。
    """
    text = str(hook_text or '').strip()
    issues = []
    if not text:
        issues.append({'code': 'empty', 'detail': '钩子为空'})
    chars = _hook_chars(text)
    if text and chars > HOOK_MAX_CHARS:
        issues.append({'code': 'too_long', 'detail': '钩子 %d 字，超过 %d 字' % (chars, HOOK_MAX_CHARS)})
    hits = [w for w in HOOK_ABSOLUTE if w in text]
    if _HOOK_ABSOLUTE_RE.search(text):
        hits.append('最')
    if _HOOK_ORDINAL_RE.search(text):
        hits.append('第一')
    if hits:
        issues.append({'code': 'absolute', 'detail': '绝对化/夸大字眼：' + '、'.join(sorted(set(hits)))})
    bait = [w for w in HOOK_CLICKBAIT if w in text]
    if bait:
        issues.append({'code': 'clickbait', 'detail': '诱导句式：' + '、'.join(bait)})
    src = source_id
    if src is None and source_pool:
        src = hook_source(text, source_pool)
    if not src:
        issues.append({'code': 'no_source', 'detail': '未能溯源到本人真实积累（无源不编）'})
    return {'ok': not issues, 'chars': chars, 'issues': issues, 'source_id': src}


def conviction_hooks(knowledge_items, principles=None, axis=None):
    """规则档（不调模型）：从本人 claim 首句提炼 conviction 钩子候选，逐条带源与合规结果。"""
    hooks = []
    for src in conviction_sources(knowledge_items, principles):
        text = _hook_from_claim(src['claim_first'])
        if not text:
            continue
        hooks.append({'text': text, 'source_id': src['id'], 'source_title': src['title'],
                      'basis': 'claim_first', 'origin': 'rule', 'status': '待本人核对',
                      'compliance': hook_compliance(text, src['id'])})
    return hooks


def conviction_hook_block(sources):
    """钩子素材块（随 payload 下发）：只列真实积累原话，出处 id 可见，无源不编。"""
    sources = list(sources or [])
    if not sources:
        return ''
    lines = ['%d. [%s] %s：%s' % (i, src['id'], src['title'] or '我的积累', src['claim_first'])
             for i, src in enumerate(sources, 1)]
    return ('我的 conviction 钩子素材（只能从下面这些我真实写过的积累/经历里长出来，'
            '出处必须写进 hook_source_id；无源不编）：\n' + '\n'.join(lines))


def conviction_hook_section(hooks):
    """persona 块内的钩子段：规则候选＋合规标记（供模型参考，不强迫套用）。"""
    lines = []
    for i, h in enumerate(list(hooks or [])[:HOOK_SOURCE_LIMIT], 1):
        mark = '' if (h.get('compliance') or {}).get('ok') else '（合规待改）'
        lines.append('%d. [源 %s] %s%s' % (i, h.get('source_id'), h.get('text'), mark))
    return ('conviction 钩子（前 3 秒那句真话，台词≤%d字；从上面某条信念原话长出来，'
            '不套模板、禁绝对化/夸大/虚假悬念；无源不编）：\n' % HOOK_MAX_CHARS + '\n'.join(lines))


def _pick_hook(hooks, knowledge_ids=None):
    """给选题挑钩子：优先与选题引用信念（knowledge_ids）同源，其次第一条合规候选；无则 None。"""
    kids = set(knowledge_ids or [])
    good = [h for h in (hooks or []) if (h.get('compliance') or {}).get('ok')]
    for h in good:
        if h.get('source_id') in kids:
            return h
    return good[0] if good else None


# t34 缓存：per-user 稳定内容（档案/人格/三观）前置到功能 system 之后、变量之前。
PERSONA_PAYLOAD_KEYS = ('persona', 'principles_block', 'profile_block', 'axis',
                        'conviction_sources', 'conviction_hooks', 'conviction_hook_block', 'hook_rule')


def persona_stable_blocks(profile_text, persona, principles_text, extras=None):
    """按固定顺序拼稳定块：档案→persona（信念×口吻×主线）→三观→额外常量（目录/规则）。"""
    blocks = []
    profile_text = str(profile_text or '').strip()
    if profile_text:
        blocks.append('我的档案：\n' + profile_text)
    for text in (persona, principles_text):
        text = str(text or '').strip()
        if text:
            blocks.append(text)
    for text in (extras or []):
        text = str(text or '').strip()
        if text:
            blocks.append(text)
    return blocks


def slim_brief(brief):
    """payload 只留变量/引用字段；稳定内容已在 system 稳定段（缓存前缀）。"""
    return {k: v for k, v in (brief or {}).items() if k not in PERSONA_PAYLOAD_KEYS}


# ---------------------------------------------------------------------------
# 2026-11-17 t34 flywheel：积累喂前缀，越用越高。
# 已验证积累（endorsement=accepted）按 created_at 升序 append-only 序列化：
# 新认可的卡追加到末尾，旧前缀字节不变保持命中；编辑/删除才会 break（罕见）。
# 规范 per-user 稳定段跨功能字节一致（档案块与 wash.stage1/2 的 cache_head 同文本）。
# ---------------------------------------------------------------------------
ACCUMULATION_CARD_LIMIT = 60
ACCUMULATION_TOTAL_CHARS = 8000
ACCUMULATION_CLAIM_CHARS = 160
ACCUMULATION_BOUNDARY_CHARS = 60


def accumulation_items(store=None):
    """本人已认可（accepted）且可用于写稿的活跃认知卡；created_at 升序＝append-only。

    排序键固定 (created_at, id)：新卡 created_at 更晚→追加末尾，绝不重排旧卡。
    老卡新认可会落在其历史位置（一次性 break 后恢复稳定）；严格认可序需 accepted_at
    字段（growth 线协调），当前用 created_at 保证可复现的字节恒定。
    """
    store = store or STORE
    items = []
    for item in store.list('knowledge', 10000):
        if item.get('lifecycle', 'active') != 'active':
            continue
        if item.get('content_use') != 'available':
            continue
        if item.get('endorsement') != 'accepted':
            continue
        if not str(item.get('claim') or '').strip():
            continue
        items.append(item)
    items.sort(key=lambda x: (str(x.get('created_at') or ''), str(x.get('id') or '')))
    return items[:ACCUMULATION_CARD_LIMIT]


def append_only_accumulation_block(store=None):
    """稳定序列化：固定行格式、不编号（纯追加），整块限长防失控。"""
    lines = []
    for item in accumulation_items(store):
        claim = _clip(str(item.get('claim') or '').replace('\r', ' ').replace('\n', ' '),
                      ACCUMULATION_CLAIM_CHARS)
        if not claim:
            continue
        line = '%s：%s' % (str(item.get('title') or '我的积累'), claim)
        boundary = _clip(str(item.get('boundary') or '').replace('\r', ' ').replace('\n', ' '),
                         ACCUMULATION_BOUNDARY_CHARS)
        if boundary:
            line += '（边界：' + boundary + '）'
        lines.append(line)
    block = '\n'.join(lines)
    return block if len(block) <= ACCUMULATION_TOTAL_CHARS else _clip(block, ACCUMULATION_TOTAL_CHARS)


def user_stable_prefix(store=None):
    """规范 per-user 稳定段（跨功能字节一致），2026-11-17 t3 返修后的固定顺序：
    档案 profile → 口吻（风格卡；红线/禁忌在档案块）→ 命题 thesis＋主线 main_goal
    → append-only 已验证积累（**段尾**）。

    为什么积累必须放段尾：积累是本段唯一「会一直长」的块。它一旦在中间，每新增一张
    认可卡都会把后面的字节整体后移，档案/口吻/功能 extras 全要重暖一次——实测就是
    阶段内稳态命中率不单调（t3：87.2/57.7/66.4）。放段尾后新增卡只把末尾延长，前面
    字节逐字不变：旧前缀持续命中，hit_rate 随积累单调爬升（越用越高）。
    档案/口吻/命题+主线排在积累之前：它们编辑频率远低于积累增长，编辑时只重暖其后的
    尾段，属罕见一次性 dip（预期内）。
    所有功能（选题/写稿/问答/复盘/bench）都经本函数取稳定段，顺序逐字一致。
    注意：wash_service（不在本线写域）只传档案作 stable_prefix，与本段共享全局前缀；
    如需 wash 也共享规范段，由 wash 线改调本函数（队长协调项）。
    """
    store = store or STORE
    blocks = []
    pb = style_profile.profile_block(profile())
    if str(pb or '').strip():
        blocks.append(str(pb))
    style = style_profile.profile_style_block(store)
    if str(style or '').strip():
        blocks.append('我的口吻：\n' + _clip(str(style).replace('\r', ' '), PERSONA_STYLE_CHARS))
    axis = axis_context(store)
    if str(axis.get('thesis') or '').strip():
        blocks.append('我的命题（待验证假设）：' + _clip(str(axis['thesis']), 1200))
    mg = axis.get('main_goal')
    if mg and mg.get('title'):
        line = '主线（引力中心；年度目标只是里程碑参考，不要求每条硬扣）：' + str(mg['title'])
        if mg.get('description'):
            line += '（' + _clip(str(mg['description']), 120) + '）'
        blocks.append(line)
    acc = append_only_accumulation_block(store)
    if acc:
        blocks.append('我的已验证积累（本人认可，只增不重排）：\n' + acc)
    return blocks


def stable_blocks_for(extras=None, store=None):
    """所有功能统一的稳定块入口：规范 per-user 段 → 功能 extras。

    规范段由 user_stable_prefix 生成，跨功能逐字一致（跨功能 warming 的前提）；
    段内契约序：档案→口吻→命题+主线→append-only 积累（段尾）——积累在段尾，
    新增认可卡只延长末尾、不掀冷前面的字节。
    extras 只放功能常量（目录/规则），随本次认知变的 persona/钩子放 extras 最后，
    这样只 break 尾部，规范段与常量仍然命中；变量（body/query/时间戳/随机 ID）一律
    留在最后的 payload，不进这里。
    """
    blocks = user_stable_prefix(store)
    for text in (extras or []):
        text = str(text or '').strip()
        if text:
            blocks.append(text)
    return blocks

def guidance_stable_blocks(store=None, extras=None):
    """非写作功能（第二大脑问答／复盘洞察／洞察开题）的规范稳定段。

    组成：规范 per-user 段（档案→口吻→命题+主线→append-only 积累·段尾）
    → persona（信念×主线×命题；口吻已在规范段，不再重复）→ 功能 extras（常量在前、本条命题在后）。

    persona 不带 conviction 钩子段：钩子是写稿/选题的「前 3 秒真话」，塞进问答/复盘
    会跑偏；三观已在 persona 的信念里（claim 原文），不再重复拼三观块（重复只会让
    前缀更长但内容重复，白花未命中 token）；口吻也只在规范段出现一次（t3 去重 ~264 字）。
    这些块都在功能模板之前、字节稳定 → 跨功能共享 warming；本次问题/历史/证据/评论
    等变量一律留在最后 payload。
    """
    store = store or STORE
    axis = axis_context(store)
    principles = default_principles_snapshot()
    persona = persona_block([], principles, '', axis)  # t3：口吻只在规范段出现一次
    return stable_blocks_for([persona] + list(extras or []), store)


def make_brief(data):
    goal=data.get('goal','attention')
    if goal not in ('attention','conversion','pain'):
        raise ValueError('创作目标无效。')
    structure=data.get('structure') or ''
    subtype=data.get('subtype') or ''
    selected=next((x for x in STRUCTURES if x['id']==structure),None)
    if structure and not selected or subtype and (not selected or subtype not in [x[0] for x in selected['subtypes']]):
        raise ValueError('脚本细分方法不属于当前结构。')
    share_to=text(data,'share_to',20)
    if share_to and share_to not in SHARE_TO_OPTIONS:
        raise ValueError('转发对象选项无效。')
    content_type=text(data,'content_type',12)
    if content_type and content_type not in CONTENT_TYPE_OPTIONS:
        raise ValueError('内容类型选项无效。')
    p=profile()
    explicit=knowledge_snapshot(data.get('knowledge_ids',[]))
    use_default=flag(data.get('use_default_principles'), True)
    # 显式选过认知就只用自己的；否则按开关默认带 3 条三观。
    principles=default_principles_snapshot() if (use_default and not explicit) else []
    # 2026-11-17 内容人格引擎最小版：信念（显式认知/默认三观）×口吻（风格卡）×主线。
    # t30 conviction 钩子：素材与规则候选全部本地生成（无模型也能给），随 payload 下发。
    axis=axis_context()
    hook_sources=conviction_sources(explicit, principles)
    hooks=conviction_hooks(explicit, principles)
    # t3 去重：口吻已在上面的规范稳定段，persona 不再重复内嵌（省 ~264 字/次）。
    persona=persona_block(explicit, principles, '', axis, hooks=hooks)
    brief={'goal':goal,'context':text(data,'context',12000),
        'profile':style_profile.profile_context(p),
        'profile_block':style_profile.profile_block(p),
        'structure':structure,'subtype':subtype,'share_to':share_to,'content_type':content_type,
        'elements':choices(data.get('elements',[]),[x[0] for x in ELEMENTS]) if goal=='attention' else [],
        'reasons':choices(data.get('reasons',[]),[x[0] for x in REASONS],5) if goal=='conversion' else [],
        'knowledge':explicit,'principles':principles,'principles_block':principles_block(principles),
        'use_default_principles':use_default,'evidence':[],
        'axis':axis,'persona':persona,
        'voice_samples':(style_profile.voice_samples(STORE)[:6] if hasattr(style_profile,'voice_samples') else []),
        'voice_catchphrases':(style_profile.confirmed_catchphrases(STORE) if hasattr(style_profile,'confirmed_catchphrases') else []),
        'conviction_sources':hook_sources,'conviction_hooks':hooks,
        'conviction_hook_block':conviction_hook_block(hook_sources),'hook_rule':PERSONA_RULE_HOOK}
    if goal=='pain' and (not p.get('industry') or not p.get('audience')):
        raise ValueError('请先在个人档案补充行业背景与目标受众，再探索痛点。')
    if goal!='pain' and not brief['context']:
        raise ValueError('请先写下这次想讲的问题、经历或产品。')
    keys=choices(data.get('work_ids',[]),[x['id'] for x in STORE.list('bench_work',10000)],10)
    for key in keys:
        work=require('bench_work',key)
        comments=[{k:c.get(k) for k in ('id','content','like_count')} for c in STORE.list('bench_comment',10000) if (c.get('bench_work_id') or c.get('work_id'))==key][:20]
        brief['evidence'].append({'id':key,'title':work.get('title'),'source_url':work.get('source_url') or work.get('canonical_url'),'comments':comments})
    return brief


# All records in a generated batch commit together after the entire response is checked.
def insert_records(con, rows):
    ts=now()
    for kind, record in rows:
        key=record['id']
        clean={k:v for k,v in record.items() if k not in ('id','revision','created_at','updated_at')}
        con.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',(kind,key,json.dumps(clean,ensure_ascii=False),1,ts,ts))


# ---------------------------------------------------------------------------
# 2026-11-17 t79 单点聚焦：字数闸＋series_suggestions 校验＋认知卡拆系列模式。
# 方法论：2026-11-16/短视频单点聚焦方法论_20261117.md §4 条款 A–F。
# 提示词侧（云端 create.script/create.topics/wash.stage2/wash.rules v2）承载单点硬规则；
# 这里只做确定性校验与提示级 enforcement，不改写模型原话。
# ---------------------------------------------------------------------------
SCRIPT_MIN_CHARS=240
SCRIPT_MAX_CHARS=540


def script_length_gate(parts):
    """字数闸（条款D，提示级 enforcement）：全稿台词字数 vs 240–540（40–90秒，5–6字/秒）。

    超闸/欠闸不静默：返回 status＋收敛建议，随 suggestion/result 下发供界面提示；
    不硬阻断生成（避免回归既有长稿/polish 路径）；收敛动作由本人决定。
    """
    chars=sum(len(str(x.get('text') or '').strip()) for x in (parts or []) if isinstance(x,dict))
    if chars<SCRIPT_MIN_CHARS:
        status='under'
        note='低于单点口播下限（%d字＜%d字）：内容可能过薄，补一件真实经历细节把单一观点讲透。'%(chars,SCRIPT_MIN_CHARS)
    elif chars>SCRIPT_MAX_CHARS:
        status='over'
        note='超出单点口播上限（%d字＞%d字，约%d秒）：自检是否混入第二论点；建议收敛到单一观点或拆系列（见 series_suggestions）。'%(chars,SCRIPT_MAX_CHARS,round(chars/5.5))
    else:
        status='ok'; note=''
    return {'chars':chars,'min':SCRIPT_MIN_CHARS,'max':SCRIPT_MAX_CHARS,'status':status,'note':note}


def clean_series_suggestions(value, allowed_ids):
    """校验 create.script 的 series_suggestions（条款B契约）：每条 {title,thesis,hook,knowledge_id}。

    只收「输入中被降级的其余认知」：knowledge_id 必须可溯源到本次输入认知（knowledge/principles），
    无溯源的条目丢弃——不杜撰，也不把积累库其他卡硬塞进本条系列（单点输入不强行拆）。
    标题/钩子截≤20字；按标题去重。
    """
    if not isinstance(value,list): return []
    out=[]; seen=set()
    for item in value[:12]:
        if not isinstance(item,dict): continue
        title=str(item.get('title') or '').strip()[:20]
        thesis=str(item.get('thesis') or '').strip()[:300]
        hook=str(item.get('hook') or '').strip()[:20]
        kid=item.get('knowledge_id')
        if not title or not thesis or title in seen: continue
        if not isinstance(kid,str) or kid not in allowed_ids: continue
        seen.add(title)
        out.append({'title':title,'thesis':thesis,'hook':hook,'knowledge_id':kid})
    return out


def _series_from_result(result, brief, job_id):
    """t79 认知卡拆系列（条款E）：校验 mother_topic/season/closer，落库为候选选题。

    代码级互斥：标题两两互异＋同卡论点不得重复；语义级「标题对调测试」由 create.topics v2
    提示词承载（照抄 bench.angle 现成规则）。一卡一条为基线；不足6条时按三刀补拆的
    派生条可同源认知卡，但论点必须不同（条款E）。每条独立成立（origin=series），
    收口条 origin=series_closer；不做上集下集悬念。
    """
    mother=str((result or {}).get('mother_topic') or '').strip()[:160]
    season=(result or {}).get('season')
    if not mother or not isinstance(season,list) or not season:
        raise ValueError('拆系列模式需要 mother_topic 与非空 season；未保存本次结果。')
    if len(season)>12:
        raise ValueError('一季最多12条（方法论建议6–10条）；未保存本次结果。')
    allowed=[x['id'] for x in brief.get('knowledge',[])]
    rows=[]; titles=[]; pairs=[]
    for item in season:
        if not isinstance(item,dict): raise ValueError('season 条目必须是对象；未保存本次结果。')
        title=str(item.get('title') or '').strip()[:20]
        thesis=str(item.get('thesis') or '').strip()[:300]
        hook=str(item.get('hook') or '').strip()[:20]
        case_hint=str(item.get('case_hint') or '').strip()[:300]
        if not title or not thesis: raise ValueError('系列选题缺少标题或论点；未保存本次结果。')
        if title in titles: raise ValueError('系列选题标题重复（互斥自检不过）；未保存本次结果。')
        titles.append(title)
        kid=item.get('knowledge_id')
        kid=kid if isinstance(kid,str) and kid in allowed else None
        if (kid,thesis) in pairs: raise ValueError('同一认知卡出现相同论点的重复条（互斥自检不过）；未保存本次结果。')
        pairs.append((kid,thesis))
        record=contracts.create_topic(title,status='candidate',creation_job_id=job_id,knowledge_id=kid)
        record.update(angle=hook,thesis=thesis,origin='series',created_by='model_assisted',
            validation='unverified',markers=['模型推断','待验证'],knowledge_ids=[kid] if kid else [],
            note=('系列「%s」'%mother+(('；钩子:'+hook) if hook else ''))[:1200],
            brief_snapshot=brief,creation_job_id=job_id,
            data={'series':{'mother_topic':mother,'case_hint':case_hint,'hook':hook}})
        record['main_goal_id']=((brief.get('axis') or {}).get('main_goal') or {}).get('id')
        rows.append(('topic',record))
    closer=(result or {}).get('closer')
    if isinstance(closer,dict) and str(closer.get('title') or '').strip():
        ct=str(closer['title']).strip()[:20]; cth=str(closer.get('thesis') or '').strip()[:300]
        rec=contracts.create_topic(ct,status='candidate',creation_job_id=job_id)
        rec.update(angle='收口条',thesis=cth,origin='series_closer',created_by='model_assisted',
            validation='unverified',markers=['模型推断','待验证'],knowledge_ids=[],
            note=('系列「%s」收口条：回顾最值得回看的条＋预告下一季'%mother)[:1200],
            brief_snapshot=brief,creation_job_id=job_id,
            data={'series':{'mother_topic':mother,'closer':True}})
        rows.append(('topic',rec))
    with STORE.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        insert_records(con,rows)
    saved=[require(k,r['id']) for k,r in rows]
    season_saved=[x for x in saved if x.get('origin')=='series']
    closer_saved=next((x for x in saved if x.get('origin')=='series_closer'),None)
    warn='' if len(season_saved)>=6 else ' 不足6条：建议按「换对象/换场景/换问题」三刀补拆同一母题至6–10条。'
    return {'series':{'mother_topic':mother,'season':season_saved,'closer':closer_saved},
            'goal':brief['goal'],'count':len(season_saved),
            'note':'系列选题已入待拍清单（每条独立成立，待本人核对）。'+warn}


def generate_topics(data):
    brief=make_brief(data)
    # t79 认知卡拆系列模式：多张认知卡→母题＋一季单点选题，不写合并稿。
    series_mode=flag(data.get('series_mode'),False)
    if series_mode: brief['series_mode']=True
    if not providers.available()['llm']:
        raise providers.ProviderMissing('生成模型待连接；可以继续使用原有作品与手动写稿。')
    count=int(data.get('count',3))
    if not 1<=count<=8:
        raise ValueError('每次生成1至8条选题。')
    kind='creation_series' if series_mode else ('creation_pain' if brief['goal']=='pain' else 'creation_topics')
    share_to=brief.get('share_to','')
    share_rule=('读者转发对象固定为「'+share_to+'」：这条内容要让读者想转发给'+share_to+'，说清那个人为什么需要它。'
                if share_to else '')
    ctype=brief.get('content_type','')
    type_rule=('内容类型固定为「'+ctype+'」，标题、角度和论点都要贴合这个类型。' if ctype else '')
    basis_rule=('count大于1时，各条选题必须两两角度不同：每条从判法清单里挑一个互不重复的判法，'
                '把判法名原样写进basis字段；三条不得是同一个意思换说法。'
                '判法清单：'+'、'.join(BASIS_LIST)+'。')
    # t34 flywheel／t1 契约序：规范 per-user 稳定段（命题+主线→口吻→append-only 积累→档案）
    # 跨功能一致，统一经 stable_blocks_for 取，不在调用点各自拼；功能常量（目录/规则）次之；
    # persona/钩子（随本次选用认知变）放稳定段末尾；变量（context/知识引用/证据）只留最后 brief。
    stable_blocks = stable_blocks_for([
        '选项目录（只解释选项，不是内容来源）：\n' + providers.stable_json(CATALOG),
        PERSONA_RULE_TOPICS, PERSONA_RULE_HOOK,
        brief.get('persona', ''), brief.get('conviction_hook_block', '')])
    def work(job_id):
        result=providers.llm_json(
            prompt_store.get('create.topics', share_rule=share_rule, type_rule=type_rule,
                             basis_rule=basis_rule),
            {'brief':slim_brief(brief),'count':count},
            stable_blocks=stable_blocks)
        if series_mode:
            return _series_from_result(result, brief, job_id)
        items=result.get('topics')
        if not isinstance(items,list) or len(items)!=count:
            raise ValueError('模型未返回约定数量的选题，未保存本次结果。')
        rows=[];bases=[]
        for item in items:
            if not isinstance(item,dict):raise ValueError('选题格式无效，未保存本次结果。')
            title=text(item,'title',160);angle=text(item,'angle',1000);thesis=text(item,'thesis',3000)
            if not title or not angle:raise ValueError('选题缺少标题或切入点，未保存本次结果。')
            basis=_norm_basis(item.get('basis'))
            if not basis:raise ValueError('选题未按判法清单标注角度，未保存本次结果。')
            if basis in bases:raise ValueError('模型返回了判法重复的选题，未保存本次结果。')
            bases.append(basis)
            refs=choices(item.get('evidence_work_ids',[]),[x['id'] for x in brief['evidence']],10)
            kids=choices(item.get('knowledge_ids',[]),
                [x['id'] for x in brief['knowledge']]+[x['id'] for x in brief.get('principles',[])],10)
            record=contracts.create_topic(title,status='candidate',creation_job_id=job_id)
            record.update(angle=angle,thesis=thesis,origin='manual',created_by='model_assisted',
                validation='unverified',markers=['模型推断','待验证'],bench_work_ids=refs,knowledge_ids=kids,
                brief_snapshot=brief,creation_job_id=job_id,data={})
            # 人格引擎溯源：这条选题服务哪条主线（无主线为 None，不编造）。
            record['main_goal_id']=((brief.get('axis') or {}).get('main_goal') or {}).get('id')
            # t30 conviction 钩子：只从本人该选题引用的信念/三观原话里挑；无合规源就留空并说明。
            hook=_pick_hook(brief.get('conviction_hooks'), kids)
            record['conviction_hook']=hook
            if hook is None:
                record['conviction_hook_note']='没有可溯源的本人的真实积累，未写钩子（无源不编）'
            if share_to:record['share_to']=share_to
            if ctype:record['content_type']=ctype
            record['basis']=basis
            if brief['goal']=='pain':record['id']=uid('pain_point')
            rows.append(('pain_point' if brief['goal']=='pain' else 'topic',record))
        with STORE.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            insert_records(con,rows)
        return {'items':[require(k,r['id']) for k,r in rows],'goal':brief['goal'],'note':'待本人核对；痛点为未验证假设。'}
    return NEW_JOB('owner',kind,{'brief':brief,'count':count,'series_mode':series_mode},work)


def adopt_pain(key):
    with STORE.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        item=STORE.unpack(con.execute('SELECT * FROM records WHERE kind=? AND id=?',('pain_point',key)).fetchone())
        if not item:raise KeyError('痛点记录不存在。')
        if item.get('topic_id'):return {'topic_id':item['topic_id'],'reused':True}
        topic={**item,'id':uid('topic'),'pain_point_id':key,'note':'来源：未验证痛点假设，需结合真实反馈核对。'}
        insert_records(con,[('topic',topic)])
        clean={k:v for k,v in item.items() if k not in ('id','revision','created_at','updated_at')}
        clean['topic_id']=topic['id']
        con.execute('UPDATE records SET data=?,revision=revision+1,updated_at=? WHERE kind=? AND id=?',
            (json.dumps(clean,ensure_ascii=False),now(),'pain_point',key))
    return {'topic_id':topic['id'],'reused':False}


def start_project(topic_id, roles, store=None):
    store = store or STORE
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        topic=store.unpack(con.execute('SELECT * FROM records WHERE kind=? AND id=?',('topic',topic_id)).fetchone())
        if not topic:raise KeyError('没有找到该选题。')
        linked=(topic.get('data') or {}).get('project_id') or topic.get('project_id')
        if linked and con.execute('SELECT 1 FROM records WHERE kind=? AND id=?',('project',linked)).fetchone():
            return {'project_id':linked,'reused':True}
        source={'id':uid('source'),'title':topic['title'],'url':'','share_text':'','text':'','kind':'text','status':'awaiting_transcript'}
        # 2026-11-17 激活闭环：复盘洞察立项保留来源标注；knowledge_ids 随选题带入新项目，
        # 写稿/复盘可继续回溯源积累（直接写稿选题没有 knowledge_ids，保持空列表）。
        origin_note=('由复盘洞察立项：'+topic['review_insight_id'] if topic.get('origin')=='feedback' and topic.get('review_insight_id')
                     else '由待拍清单立项：'+topic['id'])
        project={'id':uid('project'),'title':topic['title'],'source_id':source['id'],
            'draft':[{'id':r+'_01','role':r,'label':l,'text':''} for r,l in roles], 'draft_revision':1,
            'is_test':False,'draft_changed':True,'confirmed_version_id':None,'stage':'draft',
            'knowledge_ids':topic.get('knowledge_ids',[]),'tags':[],'director':[],'title_origin':'manual',
            'topic_id':topic['id'],'selected_topic':{k:topic.get(k) for k in ('id','title','angle','thesis','validation','bench_work_ids','content_type','origin','knowledge_ids','review_insight_id')},
            'creation_brief':topic.get('brief_snapshot',{}),'origin_note':origin_note}
        insert_records(con,[('source',source),('project',project)])
        clean={k:v for k,v in topic.items() if k not in ('id','revision','created_at','updated_at')}
        clean.update(project_id=project['id'],data={**(topic.get('data') or {}),'project_id':project['id'],'topic_id':topic['id']})
        con.execute('UPDATE records SET data=?,revision=revision+1,updated_at=? WHERE kind=? AND id=?',
            (json.dumps(clean,ensure_ascii=False),now(),'topic',topic_id))
    return {'project_id':project['id'],'reused':False}


def writing(project, data):
    mode=data.get('mode','draft')
    # 2026-11-17 激活闭环 G4：定稿讲稿一键捕获回 growth 积累（规则建议支柱，不调模型）。
    # 复用本作品既有入口（app.py 路由不动）：预览不写库，confirm=true 才写，可拒可去重。
    if mode=='capture':
        return capture_script(project, data)
    if mode not in ('draft','recreate','polish'):raise ValueError('写作方式无效。')
    if data.get('draft_revision')!=project['draft_revision']:raise Conflict('稿件已变化，请刷新后再生成。')
    reference=text(data,'reference',120000)
    if mode=='recreate' and not reference:raise ValueError('请粘贴参考原文。')
    if mode=='polish' and not any(x['text'].strip() for x in project['draft']):raise ValueError('请先写下或接受一版草稿，再做口语化润色。')
    if not providers.available()['llm']:raise providers.ProviderMissing('生成模型待连接，可以先手动编辑。')
    source=require('source',project['source_id'])
    # Recheck current permission rather than trusting an old saved brief.
    knowledge=knowledge_snapshot(project.get('knowledge_ids',[]))
    # 没显式选认知时按开关默认带 3 条三观；开关随 brief 快照保存，老项目缺字段按默认开。
    use_default=flag((project.get('creation_brief') or {}).get('use_default_principles'), True)
    principles=default_principles_snapshot() if (use_default and not knowledge) else []
    evidence=[]
    for key in (project.get('selected_topic') or {}).get('bench_work_ids') or []:
        item=require('bench_work',key)
        evidence.append({k:item.get(k) for k in ('id','title','source_url','transcript')})
    style_block=style_profile.profile_style_block(STORE)
    p=profile()
    materials=material.context(STORE,(str(project.get('title') or '')+' '+str(project.get('creation_brief') or '')[:500]
        +' '+str(source.get('text') or '')[:2000]),5)
    # 2026-11-17 人格引擎最小版：初稿同样由本人信念×口吻×主线驱动（本地口径）。
    # t30：conviction 钩子素材/候选随快照下发；生成后对开头做合规与溯源自检（不改写模型原话）。
    axis=axis_context()
    hook_sources=conviction_sources(knowledge, principles)
    hooks=conviction_hooks(knowledge, principles)
    persona=persona_block(knowledge, principles, '', axis, hooks=hooks)  # t3：口吻只在稳定段出现一次
    snapshot={'mode':mode,'profile':style_profile.profile_context(p),
        'profile_block':style_profile.profile_block(p),'style_card':style_block,
        'topic':project.get('selected_topic') or {'title':project['title']},
        'brief':project.get('creation_brief',{}),'context':text(data,'context',12000),'knowledge':knowledge,
        'principles':principles,'principles_block':principles_block(principles),'evidence':evidence,
        'materials':materials['items'],'material_ids':materials['ids'],
        'reference':reference,'source':{'id':source['id'],'revision':source['revision'],'text':source.get('text','')},
        'draft':project['draft'],'draft_revision':project['draft_revision'],
        'persona':persona,'persona_rule':PERSONA_RULE_SCRIPT,
        'main_goal':axis['main_goal'],'thesis':axis['thesis'],
        'voice_samples':(style_profile.voice_samples(STORE)[:6] if hasattr(style_profile,'voice_samples') else []),
        'voice_catchphrases':(style_profile.confirmed_catchphrases(STORE) if hasattr(style_profile,'confirmed_catchphrases') else []),
        'conviction_sources':hook_sources,'conviction_hooks':hooks,
        'conviction_hook_block':conviction_hook_block(hook_sources),'hook_rule':PERSONA_RULE_HOOK}
    # t34 flywheel／t1 契约序：规范 per-user 段（命题+主线→口吻→积累→档案，经
    # stable_blocks_for 统一取段）＋功能常量＋persona/钩子（随项目认知变）放稳定段末尾；
    # 变量（正文切段/reference/source）留最后 payload。
    # 2026-11-17 t80：本人逐字样本＋口癖＋说话禁区进稳定段（few-shot 锚定本人语感）。
    try:
        voice_block = style_profile.voice_sample_block(STORE)
    except Exception:
        voice_block = ''
    # 2026-11-17 t80：把「洗稿·共用写作约束」(wash.rules) 也带进讲稿生成（反模板负面约束同源）。
    try:
        wash_rules = prompt_store.get('wash.rules')
    except Exception:
        wash_rules = ''
    extras = [x for x in (voice_block, wash_rules, POLISH_RULE if mode == 'polish' else '',
                          PERSONA_RULE_SCRIPT, PERSONA_RULE_HOOK,
                          persona, conviction_hook_block(hook_sources)) if x]
    stable_blocks = stable_blocks_for(extras)
    snapshot_payload={k:v for k,v in snapshot.items() if k not in (
        'profile_block','persona','principles_block','persona_rule','style_card','main_goal','thesis',
        'conviction_sources','conviction_hooks','conviction_hook_block','hook_rule')}
    snapshot_payload['brief']=slim_brief(snapshot.get('brief') or {})
    def work(job_id):
        long_value=reference if len(reference)>=len(source.get('text') or '') else str(source.get('text') or '')
        def _payload(seg):
            if seg==long_value:
                return snapshot_payload
            # 长文本切段后统一放进 reference，source 只留身份，避免同一段正文被重复送两遍。
            return {**snapshot_payload,'reference':seg,
                    'source':{'id':source['id'],'revision':source['revision'],'text':''}}
        def _call(payload,tag):
            return providers.llm_json(prompt_store.get('create.script'),payload,
                                      stable_blocks=stable_blocks,tag=tag)
        def _progress(index,total,merging):
            STORE.patch('job',job_id,{'stage':'正在合并 %d 段初稿'%total if merging
                else '参考原文较长，正在写第 %d/%d 段'%(index,total)})
        def _merge(results):
            try:
                merged=long_text.merge_json(
                    '仅返回 {"parts":[{"role","text"}]}，按 hook/content/ending 顺序给出；'
                    '把多段初稿合成一份完整讲稿，重复的合并，不新增分段里没有的观点。',
                    results,'create_script')
                if isinstance(merged,dict):
                    PARTS_VALID(merged.get('parts'))
                    return merged
            except Exception:
                pass
            for part in results:
                try:
                    PARTS_VALID(part.get('parts'))
                    return part
                except (AttributeError,ValueError):
                    continue
            raise ValueError('合并后的初稿缺少开头、主体或结尾，本次未生成建议。')
        result=long_text.run(long_value,_payload,_call,'create_script',
                             progress=_progress,merge=_merge)
        # t79 单点聚焦（条款B）：被降级认知的拆系列建议（draft 模式契约；溯源校验，不杜撰）。
        # 长文本切段合并路径的 merge 契约仍只有 parts，series_suggestions 以单次调用返回为准。
        allowed_kids=[x['id'] for x in knowledge]+[x['id'] for x in principles]
        # t81：单点输入（<2 张认知/原则）不强行拆系列，守住 t79 单点约束。
        series_suggestions=clean_series_suggestions(result.get('series_suggestions'),allowed_kids) if mode=='draft' and len(allowed_kids)>=2 else []
        raw_parts=PARTS_VALID(result.get('parts'))
        if any(not x['text'].strip() for x in raw_parts if x['role'] in ('hook','content','ending')):
            raise ValueError('模型返回缺少开头、主体或结尾，本次未生成建议。')
        # t79 字数闸收敛（条款D/⑥）：draft 超闸/欠闸时带反馈重写一次（先于洗稿/钩子自检，
        # 后续管线对收敛稿统一执行）。采用规则：优先入闸版；否则取更接近闸的版本。
        # 仍不入闸则保留 length_gate 提示，不硬阻断；收敛尝试随 suggestion/result 留痕。
        length_retry=None
        if mode=='draft':
            gate0=script_length_gate(raw_parts)
            if gate0['status']!='ok':
                feedback=('上一稿%d字，%s 请保持单一观点与五段节奏，把台词量收敛到240–540字（40–90秒）重写一版。'
                          %(gate0['chars'],gate0['note']))
                retry_payload={**snapshot_payload,
                    'context':(str(snapshot_payload.get('context') or '')+'\n'+feedback).strip(),
                    'length_gate_feedback':feedback}
                def _payload_retry(seg):
                    if seg==long_value: return retry_payload
                    return {**retry_payload,'reference':seg,
                            'source':{'id':source['id'],'revision':source['revision'],'text':''}}
                try:
                    result2=long_text.run(long_value,_payload_retry,_call,'create_script',
                                          progress=_progress,merge=_merge)
                    raw2=PARTS_VALID(result2.get('parts'))
                    if not any(not x['text'].strip() for x in raw2 if x['role'] in ('hook','content','ending')):
                        gate2=script_length_gate(raw2)
                        better=(gate2['status']=='ok'
                                or (gate0['status']=='under' and gate2['chars']>gate0['chars'])
                                or (gate0['status']=='over' and gate2['chars']<gate0['chars']))
                        length_retry={'before':gate0,'after':gate2,'applied':bool(better)}
                        if better:
                            raw_parts=raw2; result=result2
                            series_suggestions=clean_series_suggestions(result.get('series_suggestions'),allowed_kids)
                except Exception as exc:
                    length_retry={'before':gate0,'after':None,'applied':False,'error':str(exc)[:200]}
        # 生成后自动洗：只做安全改法；判定改坏就整篇放弃并留原因，绝不留半成品。
        wash=auto_wash.wash_parts(raw_parts)
        parts=wash['parts_after'] if wash['outcome']=='applied' else raw_parts
        wash_record=STORE.put('auto_wash',{'project_id':project['id'],'job_id':job_id,'mode':mode,
            'outcome':wash['outcome'],'reason':wash['reason'],'changes':wash['changes'],
            'parts_before':wash['parts_before'],'parts_after':wash['parts_after'],
            'chars_before':wash['chars_before'],'chars_after':wash['chars_after'],
            'delta_pct':wash['delta_pct'],'hits_before':wash['hits_before'],'hits_after':wash['hits_after'],
            'source':'auto_generation'})
        wash_summary=auto_wash.summary(wash_record)
        # t30 conviction 钩子自检：取开头第一句做合规＋溯源（≤18字/禁绝对化/禁虚假悬念/有源），
        # 只判定与给候选，不改写模型原话；不合规给出规则候选供本人替换。
        hook_part=next((str(x.get('text') or '') for x in parts if x.get('role')=='hook'), '')
        hook_line=_first_sentence(hook_part, 60)
        hook_check=hook_compliance(hook_line, source_pool=hook_sources)
        hook_fallback=_pick_hook(hooks, project.get('knowledge_ids') or [])
        # t79 字数闸（条款D）：240–540 字提示级 enforcement，超闸不静默、不硬阻断。
        length_gate=script_length_gate(parts)
        suggestion=STORE.put('suggestion',{'project_id':project['id'],'base_draft_revision':project['draft_revision'],
            'parts':parts,'source_id':source['id'],'mode':mode,'input_snapshot':snapshot,'creation_job_id':job_id,
            'auto_wash_id':wash_record['id'],'auto_wash':wash_summary,
            'series_suggestions':series_suggestions,'length_gate':length_gate,'length_retry':length_retry,
            'conviction_hook':{'line':hook_line,'origin':'model','source_id':hook_check.get('source_id'),
                               'compliance':hook_check,'status':'待本人核对' if not hook_check['ok'] else '合规'},
            'conviction_hook_fallback':hook_fallback,'hook_rule':PERSONA_RULE_HOOK})
        filled=False
        if mode=='draft':
            current=require('project',project['id'])
            if current['draft_revision']==project['draft_revision'] and not any(str(x.get('text') or '').strip() for x in current['draft']):
                try:
                    STORE.patch('project',project['id'],{'draft':parts,'draft_revision':current['draft_revision']+1,
                        'draft_changed':True,'stage':'draft','auto_wash_id':wash_record['id'],
                        'auto_wash':wash_summary},current['revision']);filled=True
                except Conflict:pass
        return {'suggestion':suggestion,'draft_filled':filled,'auto_wash':wash_summary,
            'series_suggestions':series_suggestions,'length_gate':length_gate,'length_retry':length_retry,
            'materials':[{'id':x['id'],'qid':x.get('qid'),'excerpt':x.get('excerpt')} for x in materials['items']],
            'note':'初稿已直接填入空草稿，并留了一份可对比的建议。' if filled else '建议已保存；比较并接受后才会修改草稿。'}
    return NEW_JOB(project['id'],'creation_'+mode,snapshot,work)


# ---------------------------------------------------------------------------
# 2026-11-17 激活闭环 G4：定稿讲稿一键捕获 → growth 碎片＋建议支柱。
# 规则建议（不调模型）；预览不写库；confirm=true 才写；goal_ids 由本人确认；
# 同一讲稿文本指纹去重；直接写稿（无源积累）项目同样可捕获。
# ---------------------------------------------------------------------------

# 关键词→支柱（与 growth_service.PILLARS 同名）；只作建议，最终归位由本人确认。
CAPTURE_PILLAR_RULES = (
    ('认知', ('认知', '方法', '逻辑', '思考', '复盘', '经验', '教训', '判断')),
    ('意义', ('意义', '为什么', '信念', '值得', '初心', '使命', '方向')),
    ('能量', ('精力', '健康', '习惯', '状态', '休息', '运动', '节奏')),
    ('关系', ('关系', '信任', '客户', '朋友', '家人', '粉丝', '观众', '评论')),
    ('价值', ('赚钱', '收入', '成交', '价格', '成本', '价值', '生意', '变现', '付费')),
)


def _suggest_pillars(store, project, text):
    """按源积累分类映射＋讲稿关键词给支柱建议（最多 3 个，附理由）；无支柱体系返回空。"""
    import growth_service
    goals = [g for g in store.list('growth_goal', 2000)
             if g.get('level') == 'pillar' and not g.get('archived')]
    by_name = {g['title']: g for g in goals}
    if not by_name:
        return []
    scores, reasons = {}, {}
    mapping = growth_service.pillar_mapping()
    for key in list(project.get('knowledge_ids') or []) + list(project.get('knowledge_candidate_ids') or []):
        item = store.get('knowledge', key)
        if not item:
            continue
        pillar = mapping.get(str(item.get('category') or ''))
        if pillar in by_name:
            scores[pillar] = scores.get(pillar, 0) + 2
            reasons.setdefault(pillar, '源积累分类「%s」按映射归此支柱' % (item.get('category') or ''))
    for name, words in CAPTURE_PILLAR_RULES:
        if name not in by_name:
            continue
        hits = [w for w in words if w in text]
        if hits:
            scores[name] = scores.get(name, 0) + len(hits)
            reasons.setdefault(name, '讲稿含关键词：' + '、'.join(hits[:4]))
    ranked = sorted(scores, key=lambda n: (-scores[n], n))[:3]
    return [{'goal_id': by_name[n]['id'], 'title': n,
             'reason': reasons.get(n, ''), 'score': scores[n]} for n in ranked]


def capture_script(project, data):
    """定稿讲稿捕获为 growth 碎片＋建议支柱；固定返回 dict（同步，无 job）。

    - 未定稿（无 confirmed_version_id 或 draft_changed）如实拒绝，不捕获半成品；
    - confirm 非 true：只返回预览（标题/正文/建议支柱），不写任何记录——可见可拒；
    - confirm=true：经 growth_service.Growth.fragment 写入（goal_ids 校验复用成长系统），
      goal_ids 可为空＝等待归位；本人确认后才关联支柱，不确认不写；
    - 去重：讲稿去空白指纹相同→复用已有碎片（duplicate=True），不重复入库；
    - 碎片 endorsement=accepted：这是本人定稿的原话且本人明确确认捕获；
    - 溯源：碎片补记 origin_project_id / origin_script_version_id / capture_source。
    """
    data = data or {}
    version_id = project.get('confirmed_version_id')
    if not version_id or project.get('draft_changed'):
        raise ValueError('请先在讲稿页确认当前稿件（定稿）再捕获进积累；不会捕获未定稿的半成品。')
    version = require('script_version', version_id)
    if version.get('project_id') != project['id']:
        raise Conflict('定稿版本不属于这个作品，请刷新后重试。')
    parts = version.get('parts') or []
    text = '\n\n'.join(str(p.get('text') or '').strip() for p in parts
                       if str(p.get('text') or '').strip())
    if not text.strip():
        raise ValueError('定稿讲稿为空，没有可捕获的内容。')
    digest = hashlib.sha1(re.sub(r'\s+', '', text).encode('utf-8')).hexdigest()[:20]
    key = 'growth_fragment_cap_' + digest
    import growth_service
    existing = STORE.get('growth_fragment', key)
    pillars = _suggest_pillars(STORE, project, text)
    title = str(data.get('title') or '').strip()[:160] or ('讲稿·' + str(project.get('title') or ''))[:160]
    if existing is not None:
        return {'duplicate': True, 'fragment': existing, 'suggested_pillars': pillars,
                'message': '这份定稿讲稿已在积累里，未重复写入。'}
    if not flag(data.get('confirm'), False):
        return {'duplicate': False, 'confirm_required': True, 'fragment_key': key,
                'suggestion': {'title': title, 'text': text[:40000], 'char_count': len(text),
                               'suggested_pillars': pillars,
                               'origin_project_id': project['id'],
                               'origin_script_version_id': version_id},
                'message': '仅预览，未写入。确认后再带 confirm=true 与 goal_ids 调用才入库；'
                           '不确认即拒绝，不会写任何记录。'}
    goal_ids = data.get('goal_ids') or []
    if not isinstance(goal_ids, list) or any(not isinstance(x, str) for x in goal_ids):
        raise ValueError('goal_ids 必须是字符串数组；可从建议支柱里选，或留空等待归位。')
    growth = growth_service.Growth(STORE)
    saved = growth.fragment({'title': title, 'text': text, 'goal_ids': goal_ids,
                             'endorsement': 'accepted', 'type': 'text'}, create_key=key)
    saved = STORE.patch('growth_fragment', saved['id'],
                        {'origin_project_id': project['id'],
                         'origin_script_version_id': version_id,
                         'capture_source': 'finalized_script',
                         'suggested_pillars': [p['title'] for p in pillars]}, saved['revision'])
    return {'duplicate': False, 'fragment': saved, 'suggested_pillars': pillars,
            'goal_ids': saved.get('goal_ids') or [],
            'message': '定稿讲稿已捕获进成长积累'
                       + ('，并关联了本人确认的目标。' if saved.get('goal_ids')
                          else '（等待归位，可稍后关联目标）。')}


def history(query):
    kind=query.get('kind',['all'])[0]
    project_id=query.get('project_id',[''])[0]
    kinds={'creation_topics','creation_pain','creation_series','creation_draft','creation_recreate','creation_polish','rewrite','prepare_content','prepare_reference'}
    items=[j for j in STORE.list('job',5000) if j.get('kind') in kinds and (kind=='all' or j.get('kind')==kind)
           and (not project_id or j.get('project_id')==project_id)]
    return {'items':[{k:j.get(k) for k in ('id','kind','status','stage','error','project_id','created_at','updated_at')} |
        {'goal':((j.get('inputs') or {}).get('brief') or {}).get('goal'),'count':len((j.get('result') or {}).get('items',[]))} for j in items[:50]],'total':len(items)}
