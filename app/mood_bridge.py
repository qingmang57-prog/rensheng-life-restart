"""情绪桥（t26）：人生侧导演三件套 → rs_eva 引擎 director_choices，引擎零改。

原则（参考片学习笔记 §2）：
1. 语义优先、情绪调制：引擎语义映射（rule_choices 的 hook/close/process/contrast 词判定，
   rs_eva_engine.py:59）是底座；mood 只在 NEUTRAL 版式上调制，绝不覆盖 PROTECTED 语义。
2. 卡词精度：只决定切镜点（cue 边界即词级时间戳），不改任何字幕时间；组件切换天然卡词。
3. 规格可审：以下三张表就是全部映射规则，写死、无随机、无 LLM 调用。

输入约定：
  caps     : 引擎 clean_captions 输出 [{'start':秒,'end':秒,'text':str}, ...]
  segments : [{'cue_from':int,'cue_to':int(含), 'mood':MOODS之一,'intensity':1..5}, ...]
             —— 由调用方从 director_pass.mood_arc 的 part_id→cue 区间映射得到
  quote_cues: [cue索引,...] 来自 visual_plan.quote_cards（金句卡逐字所在 cue）
输出：choices 列表（经引擎 validate_choices 自验后返回），可直接塞
  job['director_choices']=dict(scenes=choices)（rs_eva_packaging.py:52 同构）。
"""
from pathlib import Path
import importlib.util

# ── 规则表 1：情绪枚举 → (版式偏置序列, 单镜句数上限) ──
MOOD_LAYOUT_BIAS = {
    '燃':      (['contrast', 'paper'], 2),   # 高张力：对照/转折优先，快切
    '沉郁叙事': (['paper', 'quote'], 4),      # 长镜慢叙，转折落卡
    '温情陪伴': (['quote', 'points'], 3),     # 金句/并列，标准节奏
    '轻快':    (['points', 'contrast'], 2),   # 并列明快，快切
}

# ── 规则表 2：intensity → 单镜句数上限（None=不收紧，沿用语义规则 3 句/9s/56 字）──
INTENSITY_CAP = {1: 4, 2: 3, 3: None, 4: 2, 5: 2}

# ── 规则表 3：情绪 → 模板匹配偏置（match_styles 用，系数 <0.5 保持特征分主导=语义优先）──
# t38 下架：20261118 MG01/MG02 彻底下架（兜底链摘除+偏置删键），现役池=MS 3 套（像素游戏/竖屏锐金/
# 观点变形口播-无角标，tools/eva 实存）+ JD 12 套（9:16）+ spatial（manual_only）。
# 映射理由（台账 §t36）：横竖屏各 ≥1 命中对象；横屏池=16:9，竖屏池=9:16/3:4。
#   燃＝对照/转折高张力（contrast 优先，快切）：概念拆解=信息对照主打；前后对比=张力锚点；
#      像素游戏=横屏高张力对撞（快切帧感）。
#   沉郁叙事＝长镜慢叙、去促销感（paper+quote，≤4句慢节奏）：无角标口播=去广告感人物特写慢叙；
#      jd-quote-line=竖屏金句线落卡。
#   温情陪伴＝金句/并列标准节奏（quote+points）：
#      jd-beauty-glow(±34)=柔光人像陪伴感（9:16 与 3:4 双画幅）。
#   轻快＝并列明快快切（points+contrast，2句）：像素游戏=像素风活泼；jd-flash-sale=促销快节奏；
#      jd-spec-decode=卖点速览节奏。
MOOD_STYLE_BIAS = {
    '燃':      {'jd:jd-concept-breakdown': .40, 'jd:jd-before-after': .35, 'ms:像素游戏': .30},
    '沉郁叙事': {'ms:观点变形口播-无角标': .40, 'jd:jd-quote-line': .30},
    '温情陪伴': {'jd:jd-beauty-glow': .35, 'jd:jd-beauty-glow-34': .30},
    '轻快':    {'ms:像素游戏': .40, 'jd:jd-flash-sale': .35, 'jd:jd-spec-decode': .25},
}

NEUTRAL = ('points', 'quote', 'paper', 'process')   # 允许情绪调制的版式
PROTECTED = ('hook', 'close', 'contrast')           # 语义判定结果，情绪不覆盖
_MAX_SECONDS, _MAX_CHARS = 9.0, 56                  # 沿用引擎 rule_choices 语义守则（:52）


def _engine():
    """只读加载共享引擎（零改动），用于自验。"""
    core = Path(__file__).resolve().parent.parent / 'tools' / 'rs-eva-talking' / 'rs_eva_engine.py'
    spec = importlib.util.spec_from_file_location('rs_eva_core_moodbridge', core)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mood_cap(seg):
    cap = MOOD_LAYOUT_BIAS.get(seg.get('mood'), (None, 3))[1]
    icap = INTENSITY_CAP.get(int(seg.get('intensity', 3)))
    return min(cap, icap) if icap else cap


def _seg_at(segments, i):
    for s in segments:
        if s['cue_from'] <= i <= s['cue_to']:
            return s
    return None


def _regroup(caps, segments):
    """按 mood/intensity 容量重新切镜（只收紧或放宽句数，时间守则不放宽）。
    情绪段界=必然镜界：跨段成镜会让版式切换点漂移，情绪切换不可读。"""
    groups, ids = [], []
    for i, c in enumerate(caps):
        seg = _seg_at(segments, i)
        cap = min(3, _mood_cap(seg)) if seg else 3
        prev = _seg_at(segments, ids[-1]) if ids else None
        crossed = ids and prev is not None and seg is not None and prev is not seg
        if ids and (crossed or len(ids) >= cap
                    or c['end'] - caps[ids[0]]['start'] > _MAX_SECONDS
                    or sum(len(caps[k]['text']) for k in ids) + len(c['text']) > _MAX_CHARS):
            groups.append(ids)
            ids = []
        ids.append(i)
    if ids:
        groups.append(ids)
    return groups


def mood_to_director_choices(caps, segments, quote_cues=(), base=None, llm_base=False):
    """导演三件套 → 引擎 choices。语义优先：PROTECTED 不动、金句卡指派、其余 NEUTRAL 调制。

    体验优先（方案C P0-4，2026-11-19）：llm_base=True（base 来自 experience-first LLM 导演）时
    mood 只调制强度——重切镜容量照旧（INTENSITY_CAP），镜像改按 cue 投影（重切后组仍承接原组版式），
    金句卡 override 保留，MOOD_LAYOUT_BIAS 不再改写 LLM 版式指派（「mood 只调制强度不配额」）。
    llm_base=False（rule 兜底链）行为与改前逐字节一致：镜像→语义规则（含 j%3 轮播）→bias 调制。
    """
    if not caps:
        raise ValueError('情绪桥：字幕为空')
    moods = {s.get('mood') for s in segments}
    bad = moods - set(MOOD_LAYOUT_BIAS)
    if bad:
        raise ValueError('情绪桥：未知情绪枚举 %s，允许 %s' % (sorted(bad), sorted(MOOD_LAYOUT_BIAS)))
    groups = _regroup(caps, segments)
    cue_layout = {}
    if llm_base and base is not None:
        for b in base:
            for i in b.get('ids', []):
                cue_layout[i] = b['layout']
    choices = []
    import re
    from collections import Counter
    for j, ids in enumerate(groups):
        text = ''.join(caps[i]['text'] for i in ids)
        layout = None
        if base is not None and not llm_base:
            layout = next((b['layout'] for b in base if set(b['ids']) == set(ids)), None)
        elif llm_base:
            # 重切后组边界变化：按组内 cue 对原 LLM 组版式多数投票承接（内容驱动不丢）。
            votes = Counter(cue_layout.get(i) for i in ids if i in cue_layout)
            layout = votes.most_common(1)[0][0] if votes else None
        if layout is None:  # 重分组后基线无同组镜像：回退引擎语义规则（rule_choices :59 同构）
            layout = ('hook' if j == 0 else 'close' if j == len(groups) - 1 else None)
            if layout is None:
                layout = ('process' if re.search('首先|其次|第一|第二|最后', text)
                          else 'contrast' if re.search('不是|但是|而是', text) and len(ids) <= 3
                          else ('quote', 'points', 'paper')[j % 3])
        if any(i in set(quote_cues) for i in ids):
            layout = 'quote'                       # 导演语义：金句卡所在镜（优先于情绪调制）
        elif layout in NEUTRAL and not llm_base:   # 情绪只调制中性版式（llm_base 时跳过 bias 改写）
            seg = _seg_at(segments, ids[len(ids) // 2])
            bias = MOOD_LAYOUT_BIAS[seg['mood']][0] if seg else []
            if seg and int(seg.get('intensity', 3)) >= 4 and 'contrast' in bias:
                layout = 'contrast'                # 高强度×高张力情绪：升级对照张力
            elif bias:
                layout = bias[0]                   # 版式跟随情绪主色（沉郁/温情不受强度改写）
        choices.append(dict(ids=ids, layout=layout))
    core = _engine()
    core.validate_choices(dict(scenes=choices), caps)   # 引擎既有校验兜底（零改引擎）
    return choices


def brief_to_mood(brief, caps):
    """导演三件套 brief（director_pass.ensure_brief 产物）→ mood_to_director_choices 的输入。

    part_id→cue 区间映射（t35）：brief 只带 part_id/quote_cards，无稿本全文，故
    1) 金句卡 quote 逐字命中 cue 文本时，把对应段边界校准到锚 cue（不掏空邻段才生效）；
    2) 其余按段序均分 cue（等分兜底，弧线值仍真实，对齐方式如实标注 'ratio'）。
    段 mood ∉ MOODS 时归一为 overall（导演校验允许自由 mood 字符串 ≤20 字）。
    返回 (segments, quote_cues, info)；brief 无有效 mood_arc 时返回 None。
    """
    if not brief or not isinstance(brief.get('mood_arc'), dict) or not caps:
        return None
    arc = brief['mood_arc']
    overall = arc.get('overall')
    raw = [s for s in (arc.get('segments') or []) if isinstance(s, dict) and s.get('part_id')]
    if overall not in MOOD_LAYOUT_BIAS or not raw:
        return None
    rows = []
    for s in raw:
        rows.append({'part_id': s['part_id'],
                     'mood': s.get('mood') if s.get('mood') in MOOD_LAYOUT_BIAS else overall,
                     'intensity': max(1, min(5, int(s.get('intensity', 3))))})
    dedup = [rows[0]]
    for r in rows[1:]:
        if r['part_id'] != dedup[-1]['part_id']:
            dedup.append(r)
    rows = dedup[:len(caps)]
    n = len(caps)
    bounds = [round(k * n / len(rows)) for k in range(len(rows) + 1)]
    segments = [dict(cue_from=bounds[k], cue_to=bounds[k + 1] - 1, mood=r['mood'],
                     intensity=r['intensity']) for k, r in enumerate(rows)]
    # 金句逐字对齐 → 锚点校准（quote ⊂ cue 文本；平移后两段均非空才生效）
    quote_cues = []
    part_idx = {r['part_id']: k for k, r in enumerate(rows)}
    cards = (brief.get('visual_plan') or {}).get('quote_cards') or []
    for card in cards:
        q = str(card.get('quote', '')).strip()
        k = part_idx.get(card.get('part_id'))
        if not q or k is None:
            continue
        hits = [i for i, c in enumerate(caps) if q in c['text']]
        if not hits:
            continue
        anchor = hits[0]
        if anchor not in quote_cues:
            quote_cues.append(anchor)
        seg = segments[k]
        if anchor < seg['cue_from'] and (k == 0 or segments[k - 1]['cue_from'] <= anchor - 1) and anchor <= seg['cue_to'] - 0:
            if k > 0:
                segments[k - 1]['cue_to'] = anchor - 1
            seg['cue_from'] = anchor
        elif anchor > seg['cue_to'] and (k == len(segments) - 1 or segments[k + 1]['cue_to'] >= anchor + 1):
            seg['cue_to'] = anchor
            if k < len(segments) - 1:
                segments[k + 1]['cue_from'] = anchor + 1
    segments = [s for s in segments if s['cue_from'] <= s['cue_to']]
    if not segments:
        return None
    method = 'text-anchor' if quote_cues else 'ratio'
    info = {'method': method, 'overall': overall,
            'brief_id': brief.get('id') or ('director_brief_' + str(brief.get('script_version_id', ''))),
            'script_version_id': brief.get('script_version_id'),
            'segments': len(segments), 'quote_cues': len(quote_cues)}
    return segments, quote_cues, info
