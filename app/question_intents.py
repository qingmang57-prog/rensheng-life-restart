# -*- coding: utf-8 -*-
"""题意去重（t197）：把「问的其实是同一件事」的题归到同一个 intent 组。

为什么要有这个文件
    题库里存在**题面不同、意思相同**的题。用户回答过其中一题之后，再被问另一题的
    同一件事，就是重问。逐字去重（difflib/精确文本）抓不到这种情况，所以这里用
    「intent 组」显式登记，并把互斥规则写成代码，方便测试断言与人工复核。

三层供给链都要过这里
    1) 成长/每日提问链：growth_ask.select_round()（云端下发题、本地壳回退题都走它）
    2) 访谈链：interview._bank_pick()（同一个 74 题库的第二个入口）
    3) 240 诊断链：diagnosis.next_batch()（分阶段、跨轮次/恢复后取题）

去重键 = 意图组 + 已答持久状态
    成长链的答案按题 ID 全局落库（growth_ask.answer_key），所以「已答」本身就是跨入口、
    跨会话的持久状态；组内任一题 answered/empty/skipped 即视为这件事已经问过，
    整组不再重问（skipped 也不换一种说法再问）。240 诊断链的已答状态在
    diagnosis_answer 行里（answered/skipped/na 都算摸过），跨阶段共用。

红线（不许误合并）
    - ST-03「你现在做的事，跟十年前的自己比，进步在哪？」问的是**现在 vs 过去的变化**，
      不是「对过去的自己说一句话」，与 L1-10/ST-05/L2-V05 不同意图，必须保留。
    - 反义题（失败归因 vs 成功归因）不算重复。
    - 疑问点（见 KEEP_SEPARATE 的 why 字段）没有十足把握的不合并，宁可多问一题。

本文件只放规则，不读库、不读云端；调用方把自己的「已答集合」传进来。
"""

# ---------------------------------------------------------------- 成长 74 题库
# 组内成员都是「同一件事的不同问法」。members 用题 ID（canonical ID），
# 云端下发题与本地壳回退题共用同一套 ID，因此换题库来源也不影响去重。
GROWTH_GROUPS = [
    {
        'id': 'past_self_advice',
        'label': '给过去的自己一句话 / 一封信',
        'members': ['L1-10', 'ST-05', 'L2-V05'],
        'rule': 'answered / empty → 整组关闭；skipped → 也不再换一种问法重问；同轮同组最多 1 题',
        'why': 'L1-10「最想跟十年前的自己说什么」/ ST-05「给十年前那个刚开始的自己写封信」/ '
               'L2-V05「给二十岁的自己写一句话」——都是让本人对过去的自己说一段话。',
    },
    {
        'id': 'three_year_no_result',
        'label': '做三年还没结果怎么办',
        'members': ['L1-12', 'L3-A02'],
        'rule': 'answered / empty → 整组关闭；skipped → 也不再重问；同轮同组最多 1 题',
        'why': 'L1-12「三年后没结果你怎么办」/ L3-A02「这条路做三年没结果你怎么办」，同一假设、同一答案。',
    },
    {
        'id': 'best_choice',
        'label': '这辈子最值 / 最不后悔的选择',
        'members': ['L1-03', 'L2-M06'],
        'rule': 'answered / empty → 整组关闭；skipped → 也不再重问；同轮同组最多 1 题',
        'why': 'L1-03「最不后悔的决定 / 只能留一个」/ L2-M06「这一辈子最值的那个选择」，问的是同一个判断。',
    },
]

# 逐条复核过、确认**不同意图**的相近题：写下来供测试与复核，禁止合并。
GROWTH_KEEP_SEPARATE = [
    {'ids': ['ST-03', 'L1-10'],
     'why': 'ST-03 问「现在做的事跟十年前比进步在哪」（变化/进度），L1-10 是「对十年前说一句」（回望寄语）。'
            '队长点名不得合并。'},
    {'ids': ['L3-N03', 'L1-10'],
     'why': 'L3-N03「最想跟现在的自己说什么」是对当下说话，与对过去说话相反。'},
    {'ids': ['L1-06', 'L3-F04'],
     'why': 'L1-06 问最惨的损失（事实），L3-F04 问最贵的学费（值不值），落点不同。'},
    {'ids': ['L2-C05', 'L2-C06'],
     'why': '「最对的盲目自信」与「最错的盲目自信」是反义两题，都答才知道边界。'},
    {'ids': ['L3-O01', 'L3-O04'],
     'why': '一个问「怎么看出机会不是能力」的方法，一个问「为什么容易把机会当能力」的原因。'},
    {'ids': ['L2-R01', 'L2-R04'],
     'why': '能不能讲真话的人（关系盘点）与上次交心（最近一次事件）不是同一件事。'},
    {'ids': ['L3-R01', 'L3-R02'],
     'why': '身边人怎么评价你的状态，与最亲的人怎么看你，指向不同人。'},
]

# ---------------------------------------------------------------- 240 诊断题库
# 240 题里没有逐字重复；下面两组是题面相似度 ≥0.79 且逐条读过后确认「同一意思」的。
DIAGNOSIS_GROUPS = [
    {
        'id': 'now_vs_before',
        'label': '现在做的事跟以前 / 出事前的区别',
        'members': [78, 196],
        'rule': 'answered / skipped / na → 整组关闭（同一次诊断不重复问）；跨阶段同样生效',
        'why': '#78「你现在做的事，跟出事前最大的区别是什么？」与 #196「你现在做事，跟以前最大的区别是什么？」'
               '只差「出事前/以前」一个限定词，落在同一个答案上。',
    },
    {
        'id': 'known_wrong_unchanged',
        'label': '明知不对但一直没改的那件事',
        'members': [114, 230],
        'rule': 'answered / skipped / na → 整组关闭；跨阶段同样生效',
        'why': '#114「有没有一件事，你明知道不对但一直没改？为什么？」与 #230「有没有一件事，你明知道不对，但一直在做？」'
               '是同一件事的两种说法。',
    },
]

# 复核过、保留为两题的相近题（含边界情况），写下来防止以后被误合并。
DIAGNOSIS_KEEP_SEPARATE = [
    {'ids': [115, 131],
     'why': '「被指出错误的第一反应」（具体纠错场景）与「被否定时最典型的反应」（人际评价场景）'
            '相似度 0.67，但场景不同；没十足把握，保留两题，交队长复核。'},
    {'ids': [235, 236], 'why': '「失败有多少因为运气」与「成功有多少因为运气」是反义题，必须都问。'},
    {'ids': [122, 194], 'why': '最大优点与最大问题是反义题。'},
    {'ids': [120, 122], 'why': '决策盲区与优点是不同维度。'},
    {'ids': [151, 152, 145, 146], 'why': '伴侣有没有哭过 / 放弃过 / 花过钱 / 挡过事，是四个不同行为。'},
    {'ids': [57, 60], 'why': '「哪项能力比当年更强」是增量，「哪项能力当年没练过」是缺口，方向不同。'},
    {'ids': [220, 240], 'why': '一个是在两个选项里选，一个是改一个地方翻身，问题对象不同。'},
]


def _index(groups):
    out = {}
    for group in groups:
        for qid in group['members']:
            out[qid] = group['id']
    return out


_GROWTH_INDEX = _index(GROWTH_GROUPS)
_DIAGNOSIS_INDEX = _index(DIAGNOSIS_GROUPS)


def growth_group_of(question_id):
    """成长题库某题所属 intent 组（没有则 None）。"""
    return _GROWTH_INDEX.get(str(question_id or ''))


def diagnosis_group_of(question_no):
    """240 诊断某题所属 intent 组（没有则 None）。"""
    try:
        return _DIAGNOSIS_INDEX.get(int(question_no))
    except (TypeError, ValueError):
        return None


def growth_members(group_id):
    for group in GROWTH_GROUPS:
        if group['id'] == group_id:
            return list(group['members'])
    return []


def diagnosis_members(group_id):
    for group in DIAGNOSIS_GROUPS:
        if group['id'] == group_id:
            return list(group['members'])
    return []


def closed_growth_groups(records):
    """records: {题 ID → 答题记录 dict}（growth_ask.answer_map 的返回值）。

    组内任一题 answered / empty / skipped → 该组关闭。
    返回 {group_id: {'by': 题ID, 'status': 状态}}，供调用方按组过滤与解释。
    """
    closed = {}
    for qid, record in (records or {}).items():
        group_id = growth_group_of(qid)
        if not group_id:
            continue
        status = str((record or {}).get('status') or '')
        if status in ('answered', 'empty', 'skipped'):
            closed.setdefault(group_id, {'by': qid, 'status': status})
    return closed


def growth_blocked_ids(records):
    """成长链：这次不该再问的题 ID 集合（含组内已答/已跳过的那几题本身）。"""
    closed = closed_growth_groups(records)
    blocked = set()
    for group_id in closed:
        blocked.update(growth_members(group_id))
    return blocked


def closed_diagnosis_groups(touched_nos):
    """touched_nos: 本次诊断里已经摸过的题号（answered / skipped / na 都算）。

    返回 (closed_groups, detail)：closed_groups 是组 ID 集合；detail 是
    {题号 → 因为它被关闭的另一个题号}，用于把「为什么不问这题」写进响应。
    """
    touched = set()
    for no in touched_nos or ():
        try:
            touched.add(int(no))
        except (TypeError, ValueError):
            continue
    closed = {}
    for no in touched:
        group_id = diagnosis_group_of(no)
        if group_id and group_id not in closed:
            closed[group_id] = no
    return set(closed), closed


def diagnosis_skip_reason(question_no, closed):
    """该题是否因为同组另一题已摸过而跳过；是则返回原因字符串。"""
    group_id = diagnosis_group_of(question_no)
    if not group_id or group_id not in (closed or {}):
        return None
    by = closed[group_id]
    try:
        if int(question_no) == int(by):
            return None       # 自己被摸过，走原有的 touched 逻辑
    except (TypeError, ValueError):
        return None
    return '同组已问过 #%s（intent=%s）' % (by, group_id)


def group_ids_for(question_ids, kind='growth'):
    """给一批题号求涉及的组（测试与审计用）。"""
    if kind == 'growth':
        return sorted({g for g in (growth_group_of(q) for q in question_ids) if g})
    return sorted({g for g in (diagnosis_group_of(q) for q in question_ids) if g})


def manifest():
    """可解释清单：给报告、测试与人工复核用（不含题面原文，题面在题库里）。"""
    def rows(groups):
        return [{'id': g['id'], 'label': g['label'], 'members': list(g['members']),
                 'rule': g['rule'], 'why': g['why']} for g in groups]

    return {
        'growth': {'groups': rows(GROWTH_GROUPS),
                   'keep_separate': [dict(x) for x in GROWTH_KEEP_SEPARATE]},
        'diagnosis': {'groups': rows(DIAGNOSIS_GROUPS),
                      'keep_separate': [dict(x) for x in DIAGNOSIS_KEEP_SEPARATE]},
    }
