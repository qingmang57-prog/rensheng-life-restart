"""竖屏文案槽生面器 —— 给「竖屏学习」「竖屏锐金」这类四段骨架模板填字。

为什么不能复用 scenes：这两个模板不吃 `scenes` 数组，它们读的是**固定命名的
文案槽**（`_P.s1_h`、`_P.s3_i2`…），骨架、坐标、动画全写在模板里，调用方只
负责把四段台词压成槽里的短句。把它当 scenes 硬塞会静默出空画面。

四段时间边界直接抄模板 `scenes.js` 的 C.scene({t0,t1})：
    0 – 3.22 / 3.22 – 7.02 / 7.02 – 11.55 / 11.55 – 15（按 15s 基准，片长走 SC 等比拉伸）
所以这里按同样的比例切台词，不另定节奏。

槽的语义取自模板包自带的 `params.test.json`（那是作者认可的一份填法）。
"""
import re

import prompt_store

# 四段边界（15s 基准秒数），与模板 scenes.js 逐字对齐
SEG_EDGES = (3.22, 7.02, 11.55, 15.0)

# 学习风：11 槽。键=槽名，值=(中文说明, 字数上限)
SLOTS_LEARN = {
    's1_h':  ('开场钩子标题，全片最抓人的一句', 8),
    's1_n':  ('钩子的补充说明，交代这条片子在讲什么', 16),
    's2_h':  ('第二段的小标题，用提问口吻', 8),
    's2_t':  ('第二段的承接引导词，一两字到六字的过渡话', 6),
    's2_g':  ('第二段的核心陈述，就是紧接着标题说的那句', 14),
    's3_h':  ('第三段的小标题，用提问口吻', 8),
    's3_g1': ('第三段第一个要点', 12),
    's3_g2': ('第三段第二个要点', 12),
    's3_g3': ('第三段第三个要点', 12),
    's4_h':  ('收尾升华标题，落到一句结论上', 8),
    's4_n':  ('收尾的补充说明，留一句余地', 16),
}

# 锐金风：19 槽（brand 由调用方填，不在这里生成）
SLOTS_SHARP = {
    's1_tag': ('开场顶部的小标签，两个字到六个字，像栏目标签', 8),
    's1_l1':  ('开场大字第一行，两三个字，最狠的那个词', 6),
    's1_l2':  ('开场大字第二行，接着第一行把钩子说完', 6),
    's2_tag': ('第二段顶部的章节标签，可以带一句英文', 12),
    's2_h':   ('第二段主标题，用提问或断言口吻', 10),
    's2_en':  ('第二段的英文编号，形如 CHAPTER 01', 14),
    's3_h':   ('第三段清单的标题', 10),
    's3_i1':  ('清单第一项，一条具体做法或事实', 12),
    's3_i2':  ('清单第二项，一条具体做法或事实', 12),
    's3_i3':  ('清单第三项，一条具体做法或事实', 12),
    's4_l1':  ('收尾金句第一行', 6),
    's4_l2':  ('收尾金句第二行', 6),
    's4_en':  ('收尾金句的英文，大写两三个词', 18),
}

KITS = {'learn': SLOTS_LEARN, 'sharp': SLOTS_SHARP}


def _clean(text):
    t = re.sub(r'\s+', '', (text or '').strip())
    t = re.sub(r'[，。！？、；,.!?;：:]+', '，', t).strip('， ')
    return t


def _short(text, n):
    """取一个不超过 n 字、尽量完整的片段。断在标点优先，其次不切数字。"""
    t = _clean(text)
    if len(t) <= n:
        return t
    for part in t.split('，'):
        if 2 <= len(part) <= n:
            return part
    return t[:n]


def _pick(segs, lo, hi):
    """取出落在 [lo, hi) 秒区间的句子，按时间顺序。

    按句子**中点**归段，不按起点：起点归段会把跨边界的那句全塞给上一段，
    后三段跟着空掉（实测 4 句台词只剩第 1 段有内容）。中点归段等于「这句话
    播到一半时画面在哪一段」，与模板骨架的节奏对齐。
    """
    out = []
    for s in segs:
        mid = (float(s['t0']) + float(s['t1'])) / 2.0
        if lo <= mid < hi:
            out.append(s)
    return out


def _first(segs, n, dft=''):
    for s in segs:
        t = _short(s['text'], n)
        if t:
            return t
    return dft


def _points(segs, n, cap, dft=''):
    """要点槽（s3_g1..g3 / s3_i1..i3）：先把整段的罗列句按序号词与标点拆开，
    拆不够就复用最后一个，保证模板不会拿到空串（空串=该行整块不显示）。"""
    text = ''.join(s['text'] for s in segs)
    parts = [p.strip(' ，,') for p in re.split(
        r'第[一二三四五六七八九][是：:]?|其[次实][是：:]?|首先|最后|一是|二是|三是|[，。；,;]', text)]
    out = []
    for p in parts:
        if len(p) < 2:
            continue
        t = _short(p, cap)
        if t and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    while len(out) < n:
        out.append(out[-1] if out else dft)
    return out[:n]


def segment_text(segs, duration):
    """按模板四段边界切台词。返回 [[句,...], ...] 四组。"""
    scale = (float(duration) / 15.0) if duration and float(duration) > 1 else 1.0
    edges = [0.0] + [e * scale for e in SEG_EDGES]
    out = []
    for i in range(4):
        out.append(_pick(segs, edges[i], edges[i + 1] if i < 3 else edges[-1] + 1e6))
    return out


def _rule_learn(quart, title):
    """规则兜底：只在模型不可用时用。按四段位置取句，质量不如模型，但不会空。"""
    q1, q2, q3, q4 = quart
    allsegs = q1 + q2 + q3 + q4
    hook = _first(q1, 8) or _short(title, 8) or _first(allsegs, 8) or '先看这条'
    p3 = _points(q3, 3, 12)
    return {
        's1_h':  hook,
        's1_n':  _first(q1[1:], 16) or _first(q2, 16) or _short(title, 16) or hook,
        's2_h':  (_first(q2, 7) or '为什么会这样') + '？',
        's2_t':  '但是',
        's2_g':  _first(q2[1:], 14) or _first(q2, 14) or _first(q1, 14),
        's3_h':  (_first(q3, 7) or '关键在哪') + '？',
        's3_g1': p3[0], 's3_g2': p3[1], 's3_g3': p3[2],
        's4_h':  _first(q4, 8) or hook,
        's4_n':  _first(q4[1:], 16) or _first(q4, 16) or _first(q3, 16),
    }


def _rule_sharp(quart, title):
    q1, q2, q3, q4 = quart
    allsegs = q1 + q2 + q3 + q4
    hook = _short(_first(q1, 10) or title or '先看这条', 12)
    half = max(2, min(4, len(hook) // 2))
    p3 = _points(q3, 3, 12)
    tail = _short(_first(q4, 10) or hook, 12)
    tail_half = max(2, min(4, len(tail) // 2))
    return {
        's1_tag': '实拍提醒',
        's1_l1':  hook[:half],
        's1_l2':  hook[half:] or hook,
        's2_tag': '为什么',
        's2_h':   _first(q2, 10) or '为什么会这样',
        's2_en':  'CHAPTER 01',
        's3_h':   _first(q3, 10) or '具体怎么做',
        's3_i1':  p3[0], 's3_i2': p3[1], 's3_i3': p3[2],
        's4_l1':  tail[:tail_half],
        's4_l2':  tail[tail_half:] or tail,
        's4_en':  'KEY POINT',
    }


def _llm_fill(quart, kit, title, slots):
    """让模型按槽位语义填字。任何失败返回 None（调用方回退规则法）。"""
    try:
        import providers
        if not providers.available()['llm']:
            return None
    except (ImportError, KeyError, TypeError):
        return None
    spec = '\n'.join('- %s：%s（不超过 %d 字）' % (k, v[0], v[1]) for k, v in slots.items())
    system = prompt_store.get('eva.slots', slots_spec=spec)
    payload = {'视频标题': title or '（无）',
               '第一段': ' '.join(s['text'] for s in quart[0]),
               '第二段': ' '.join(s['text'] for s in quart[1]),
               '第三段': ' '.join(s['text'] for s in quart[2]),
               '第四段': ' '.join(s['text'] for s in quart[3])}
    try:
        raw = providers.llm_json(system, payload)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, (_, cap) in slots.items():
        v = raw.get(k)
        if v is None:
            continue
        t = _clean(str(v))
        if not t:
            continue
        out[k] = t[:cap]
    return out or None


def build(lines, duration, title, kit):
    """台词 → 槽位字典。

    kit: 'learn'（竖屏学习, 11 槽）或 'sharp'（竖屏锐金, 19 槽）
    返回 (slots, meta)，meta 里说明哪些槽走了规则兜底，便于页面如实标注。
    """
    slots = KITS[kit]
    segs = []
    for l in lines or []:
        text = (l.get('speak') or '').strip()
        if not text:
            continue
        t0 = float(l.get('start') or 0)
        if t0 >= float(duration):
            break
        segs.append({'t0': t0, 't1': min(float(l.get('end') or duration), float(duration)),
                     'text': text})
    if not segs:
        raise ValueError('这版底片没有可用台词，无法填充竖屏文案槽。')

    quart = segment_text(segs, duration)
    # 某段没台词（片子短或断句偏）→ 借用相邻段，避免空槽
    for i in range(4):
        if not quart[i]:
            ref = quart[i - 1] if i else (quart[1] if len(quart) > 1 else segs)
            quart[i] = ref or segs

    rule = _rule_learn if kit == 'learn' else _rule_sharp
    base = rule(quart, title)
    filled = _llm_fill(quart, kit, title, slots)
    if filled:
        merged = dict(base)
        merged.update({k: v for k, v in filled.items() if v})
        return merged, {'source': 'llm', 'missing': sorted(set(slots) - set(filled))}
    return base, {'source': 'rule', 'missing': sorted(slots)}
