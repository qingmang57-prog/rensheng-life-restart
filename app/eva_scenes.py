"""14 版式（scenes）生成器 —— 逐条移植自切片工坊 `eva_service.py`。

为什么单独一个文件：EVA 的 `scenes` 契约是「一镜一句话 + 一个版式 + 该版式
需要的字段」，不是可以随手拼的 JSON。切片工坊那套已经在真实片子上调过两轮
（2026-10-30 / 10-31 的实测修正都写在注释里），照抄比重新发明安全。

移植时只改了三处依赖，其余判定逻辑、坐标表、兜底顺序一律保持原样：
  config.FFMPEG_PATH      → media.binary('ffmpeg')
  llm_service.complete    → providers.llm_json（工作台的模型入口）
  logger.warning          → 静默（失败本来就走规则兜底，不需要噪音）

用法：
    from eva_scenes import build
    scenes, captions, wipes, flashes = build(lines, duration, title, host_mp4, work)
"""
import json
import math
import re
import subprocess
from pathlib import Path

import media
import prompt_store


# ---------------------------------------------------------------- 文案 → 版式

_NUM_RE = re.compile(r'\d')
_ENUM_RE = re.compile(r'(第一|第二|第三|第四|首先|其次|最后|一是|二是|三是)')
_QUESTION_RE = re.compile(r'[？?]$')
_YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')


def _condense(text, max_len=14):
    """口播整句 → 标题级短语。EVA 版式是给短标题设计的，整句塞进去会顶出容器。"""
    # ASR 逐字时间戳出来的文本是「这 个 赚 你 们 钱」，字间带空格，必须先去干净
    t = re.sub(r'\s+', '', (text or '').strip())
    t = re.sub(r'[，。！？、；,.!?;：:]+', '，', t).strip('， ')
    t = re.sub(r'^(其实|那么|然后|所以说|就是说|这个|那个|你看|你想想)', '', t)
    t = re.sub(r'(的话|对吧|是不是|对不对)$', '', t)
    if len(t) <= max_len:
        return t
    parts = [p for p in t.split('，') if p]
    if parts:
        best = max(parts, key=len)
        if len(best) <= max_len:
            return best
    return t[:max_len]


def _pick_type(t, idx, total):
    t = (t or '').strip()
    if idx >= total - 1 and total > 2 and len(t) <= 16:
        return 'chapter'
    if _ENUM_RE.search(t):
        return 'list'
    if _YEAR_RE.search(t):          # 年份会被 fmtNum 写成 2,032
        return 'astro'
    nums = _NUM_RE.findall(t)
    if len(nums) >= 2 or (nums and len(t) <= 16):
        return 'stat'
    if _QUESTION_RE.search(t):
        return 'astro'
    if len(t) <= 12:
        return 'wall'
    return 'astro'


def _split_lines(lines, dur):
    """一镜一句，只有短于 1.2s 的碎句才并进上一镜（多句合并会让 list/stat 字段抓串）。"""
    segs = []
    for l in lines:
        t0 = float(l.get('start') or 0)
        t1 = float(l.get('end') or dur)
        text = (l.get('speak') or '').strip()
        if not text or t1 <= t0:
            continue
        words = _words_of(l, t0, t1)
        if segs and (t1 - t0) < 1.2:
            segs[-1]['t1'] = t1
            segs[-1]['text'] += text
            segs[-1]['words'] = segs[-1].get('words', []) + words
        else:
            segs.append({'t0': t0, 't1': t1, 'text': text, 'words': words})
    if segs:
        segs[-1]['t1'] = max(segs[-1]['t1'], dur)
    return segs


def _words_of(l, t0, t1):
    """speak「姐 妹 我」+ wts[[s,e],...] → 词块级 (文本, s, e)。

    wts 数量按空白分块数对齐才可信；对不上返回 []（字幕层自动退回字数均摊）。
    """
    wts = l.get('wts')
    blocks = [b for b in re.split(r'\s+', (l.get('speak') or '').strip()) if b]
    if not isinstance(wts, list) or len(wts) != len(blocks):
        return []
    out = []
    for b, w in zip(blocks, wts):
        try:
            s, e = float(w[0]), float(w[1])
        except (TypeError, ValueError, IndexError):
            return []
        if e > s:
            out.append((b, s, e))
    return out if len(out) == len(blocks) else []


_PAUSE_GAP = 0.22   # 词块间隙超过这个值（秒）算一次停顿，优先在这断句
_GOOD_BREAK_AFTER = set('的了着过吗呢吧啊和跟')
_GOOD_BREAK_BEFORE = set('我你他她它咱就也都还但又而并很最更没不对')
_NUM_CHARS = set('0123456789一二三四五六七八九十百千万几两点半')
_BAD_PAIRS = {('每', '单'), ('这', '个'), ('那', '个'), ('真', '的'), ('说', '的'),
              ('赚', '钱'), ('运', '费'), ('成', '本'), ('黑', '粉'), ('姐', '妹'),
              ('效', '果'), ('成', '分'), ('浓', '度'), ('价', '格')}


def _bad_break(chars, j):
    """断在 j 之后（j 是本行末字）是否切坏了不可分单元：数字序列/高频词组中间。"""
    if j + 1 >= len(chars):
        return False
    a, b = chars[j][0], chars[j + 1][0]
    if a in _NUM_CHARS and b in _NUM_CHARS:
        return True
    return (a, b) in _BAD_PAIRS


def _caption_split(seg, max_chars=12):
    """字幕条：优先用 ASR 词块时间戳断句（停顿处 > 语义边界 > 硬切）。

    有 words 时时间是真实的（每个字跟着语音走）；没有则退回字数均摊。
    一行最多 12 字（实测超过就顶出画面）。
    """
    t0, t1, text = seg['t0'], seg['t1'], seg['text']
    t = re.sub(r'\s+', '', text or '')
    t = re.sub(r'[。！？；!?;]', '', t)
    t = t.replace('，', '').replace(',', '').replace('、', '')
    if not t:
        return []
    if len(t) <= max_chars:
        return [{'t0': round(t0, 2), 't1': round(t1, 2), 'zh': t, 'en': ''}]

    words = seg.get('words') or []
    if words and sum(len(w[0]) for w in words) == len(t):
        out = _caption_split_by_words(words, t, max_chars)
        if out:
            return out

    # 兜底：无词块时间戳，按字数均摊（旧行为）
    n = math.ceil(len(t) / max_chars)
    per = len(t) / n
    span = max(0.2, t1 - t0)
    out = []
    for i in range(n):
        a, b = round(i * per), round((i + 1) * per)
        out.append({'t0': round(t0 + span * i / n, 2),
                    't1': round(t0 + span * (i + 1) / n, 2),
                    'zh': t[a:b], 'en': ''})
    return out


def _caption_split_by_words(words, text, max_chars):
    """按词块断句：每块 (块文本, s, e)，块内多字按时间线性内插。

    断点优先级：凑满 max_chars 前 0.5s 内的停顿 > 语义边界字 > 凑满硬切。
    """
    chars = []
    for wtxt, ws, we in words:
        n = len(wtxt)
        if n == 1:
            chars.append((wtxt, ws, we))
        else:
            step = (we - ws) / n
            for k in range(n):
                chars.append((wtxt[k], ws + step * k, ws + step * (k + 1)))
    if len(chars) != len(text):
        return []

    out = []
    start = 0
    i = 0
    L = len(chars)
    while i < L:
        hard_end = min(start + max_chars, L) - 1
        if hard_end == L - 1:
            out.append(_mk_cap(chars, start, L - 1))
            break
        best = -1
        pauses = [j for j in range(max(start + 2, 3), hard_end + 1)
                  if j < L - 1 and chars[j + 1][1] - chars[j][2] >= _PAUSE_GAP
                  and not _bad_break(chars, j)]
        if pauses:
            best = pauses[-1]              # 靠近上限的停顿最自然
        if best < 0:
            for j in range(hard_end, start + 1, -1):
                if j < L and not _bad_break(chars, j - 1) and (
                        chars[j - 1][0] in _GOOD_BREAK_AFTER
                        or chars[j][0] in _GOOD_BREAK_BEFORE):
                    best = j - 1
                    break
        if best < 0:
            best = hard_end                # 硬切
        out.append(_mk_cap(chars, start, best))
        start = best + 1
        i = start
    return out


def _mk_cap(chars, a, b):
    zh = ''.join(c[0] for c in chars[a:b + 1])
    return {'t0': round(chars[a][1], 2), 't1': round(chars[b][2], 2),
            'zh': zh, 'en': ''}


def _wall_rows(zh):
    """文字墙跑马灯公式（app.js BUILD.wall）：dir=i%2?1:-1，translateX 含 -700px 常量，
    只有 1 行时 dir 恒 -1，唯一一行永远在画面左侧之外 → 空色块（20261031 实测）。
    复读填充到 4 行、方向交替，跑马灯本来就是短语复读墙。"""
    return [{'txt': zh, 'imp': i % 2 == 0} for i in range(4)]


def _scene_from(seg, idx, total):
    text = seg['text']
    typ = _pick_type(text, idx, total)
    base = {'t0': round(seg['t0'], 2), 't1': round(seg['t1'], 2), 'type': typ,
            'chapter': '%02d' % (idx + 1), 'page': idx + 1}
    if typ == 'chapter':
        base.update({'kicker': '重点', 'zh': _condense(text, 12), 'en': 'KEY POINT',
                     'suben': 'THE POINT'})
    elif typ == 'list':
        parts = [p.strip(' ，,') for p in re.split(
            r'(?:第[一二三四五六七八九]|其[次实]|首先|最后|一是|二是|三是)[是：:]?', text) if p.strip(' ，,')]
        if len(parts) < 2:
            parts = [p.strip() for p in re.split(r'[，。；,;]', text) if p.strip()]
        base['items'] = [{'zh': _condense(p, 12), 'en': ''} for p in parts[:5]]
    elif typ == 'stat':
        m = re.search(r'\d+(?:\.\d+)?', text)
        base.update({'num': m.group(0) if m else '0', 'unit': '',
                     'cap': _condense(text, 16)})
    elif typ == 'wall':
        base.update({'rows': _wall_rows(_condense(text, 18))})
    else:
        # astro 用单行大字。试过拆双行（拆词双色），但 ASR 逐字稿没有标点、
        # 一句 20-30 字，规则断句切出来的第二行永远是残尾。单行取一个语义片段
        # 更干净，完整台词由底部字幕条承载。9 字上限：字号 170px，画宽 1920
        # 只装得下 9–10 字，超了自动折行会压字。
        core = _condense(text, 9)
        base.update({'lines': [{'txt': core, 'cls': 'hot'}], 'sub': '', 'mesh': True})
    return base


# ---------------------------------------------------------------- LLM 标题层

_LLM_TYPES = {'astro', 'wall', 'chapter', 'list', 'stat', 'hero', 'rail', 'broll',
              'comic', 'diagram', 'paper', 'replay'}


def _llm_plan(segs, title):
    """LLM 读整句生成每镜标题+版式。任何失败返回 None（调用方回退规则法）。"""
    if not segs:
        return None
    try:
        import providers
        if not providers.available()['llm']:
            return None
    except (ImportError, KeyError, TypeError):
        return None
    lines = ['第%d镜（%.0fs）：%s' % (i, s['t1'] - s['t0'],
                                    re.sub(r'[^一-龥a-zA-Z0-9]', '', s['text'])[:60])
             for i, s in enumerate(segs)]
    payload = {'视频标题': title or '（无）', '台词': lines}
    try:
        raw = providers.llm_json(prompt_store.get('eva.scene_titles'), payload)
    except Exception:
        return None
    plans = raw.get('scenes') if isinstance(raw, dict) else raw
    if not isinstance(plans, list):
        return None
    out = {}
    for p in plans:
        if not isinstance(p, dict):
            continue
        try:
            i = int(p.get('i'))
        except (TypeError, ValueError):
            continue
        zh = re.sub(r'\s+', '', str(p.get('zh') or ''))
        typ = str(p.get('type') or '').strip()
        if not (0 <= i < len(segs)) or not zh or typ not in _LLM_TYPES:
            continue
        item = {'zh': zh[:12], 'type': typ}
        if typ == 'list' and isinstance(p.get('items'), list):
            its = [re.sub(r'\s+', '', str(x))[:10] for x in p['items'] if str(x).strip()][:4]
            if len(its) >= 2:
                item['items'] = its
        if typ == 'rail' and isinstance(p.get('nodes'), list):
            ns = [str(x) for x in p['nodes'] if str(x).strip()][:5]
            if len(ns) >= 2:
                item['nodes'] = ns
        if typ == 'broll' and isinstance(p.get('chips'), list):
            cs = [str(x) for x in p['chips'] if str(x).strip()][:4]
            if len(cs) >= 2:
                item['chips'] = cs
        if typ == 'comic' and isinstance(p.get('bubbles'), list):
            bs = [str(x) for x in p['bubbles'] if str(x).strip()][:3]
            if len(bs) >= 2:
                item['bubbles'] = bs
        if typ == 'replay' and isinstance(p.get('tags'), list):
            ts = [str(x) for x in p['tags'] if str(x).strip()][:4]
            if len(ts) >= 2:
                item['tags'] = ts
        out[i] = item
    if not out:
        return None
    return [out.get(i) for i in range(len(segs))]


# broll 标签的预设落位（错开中轴与底部字幕带 y>940、顶部 header y<90），
# 模型只给文本，坐标由这里按标签个数查表——几何不归模型（NaN 教训）。
_BROLL_SLOTS = {2: [(240, 300), (1180, 540)],
                3: [(200, 250), (1120, 420), (420, 650)],
                4: [(200, 230), (1100, 360), (320, 570), (1180, 690)]}

# comic 气泡落位（偏左，comic 场小窗在右）。w/h 按 106px 级气泡字放大留白。
_COMIC_SLOTS = {2: [(130, 170, 540, 240), (820, 430, 600, 260)],
                3: [(120, 150, 480, 220), (760, 320, 520, 230), (200, 540, 540, 230)]}
_COMIC_EMOJIS = [{'x': 170, 'y': 120, 'ch': '💥'}, {'x': 950, 'y': 880, 'ch': '😮'}]

# diagram 节点落位（node 默认宽 420，取 w=380 保证 4 节点不出界），edges 顺序连线。
_DIAGRAM_SLOTS = {2: [(160, 430), (1250, 430)],
                  3: [(120, 280), (740, 480), (1360, 280)],
                  4: [(60, 250), (580, 540), (1060, 250), (1480, 540)]}
_DIAGRAM_W = 380

# paper 照片落位（照片是白边拍立得样式，cap 区在照片内底部）。
_PAPER_SLOTS = {1: [(500, 430, 640, 520)],
                2: [(230, 410, 600, 460), (960, 470, 600, 460)]}


def _apply_llm(scenes, plans):
    """把 LLM 计划应用到 scenes（按 type 重建该镜文案字段，几何字段不动）。"""
    if not plans:
        return scenes
    for i, (sc, pl) in enumerate(zip(scenes, plans)):
        if not pl:
            continue
        zh, typ = pl['zh'], pl['type']
        base = {'t0': sc['t0'], 't1': sc['t1'], 'type': typ,
                'chapter': '%02d' % (i + 1), 'page': i + 1}
        if typ == 'chapter':
            base.update({'kicker': '重点', 'zh': zh, 'en': 'KEY POINT', 'suben': 'THE POINT'})
        elif typ == 'list':
            base['items'] = [{'zh': it, 'en': ''} for it in pl.get('items') or [zh]]
        elif typ == 'stat':
            m = re.search(r'\d+(?:\.\d+)?', zh + sc.get('cap', ''))
            if m:
                base.update({'num': m.group(0), 'unit': '', 'cap': zh})
            else:
                # LLM 选了 stat 但句中无数字：模板把 num 喂给数字计数器，非数字
                # 直接渲染成「NaN」大字（20261031 实测「天天拉肚子」）→ 降级文字墙
                base['type'] = 'wall'
                base.update({'rows': _wall_rows(zh)})
        elif typ == 'wall':
            base.update({'rows': _wall_rows(zh)})
        elif typ == 'hero':
            base.update({'word': zh[:6], 'sub': '', 'kicker': '重点'})
        elif typ == 'rail':
            ns = [re.sub(r'\s+', '', str(x))[:8] for x in (pl.get('nodes') or [zh]) if str(x).strip()]
            if len(ns) < 2:
                base['type'] = 'astro'             # 节点不足撑不起轨道，回大字
                base.update({'lines': [{'txt': zh, 'cls': 'hot'}], 'sub': '', 'mesh': True})
            else:
                base.update({'nodes': [{'zh': n, 'en': ''} for n in ns[:5]],
                             'hint': '', 'hintEn': ''})
        elif typ == 'broll':
            cs = [re.sub(r'\s+', '', str(x))[:8] for x in (pl.get('chips') or [zh]) if str(x).strip()]
            slots = _BROLL_SLOTS.get(min(len(cs), 4), _BROLL_SLOTS[2])
            base.update({'chips': [{'x': x, 'y': y, 'txt': t, 'big': j == 0}
                                   for j, (t, (x, y)) in enumerate(zip(cs, slots))]})
        elif typ == 'comic':
            bs = [re.sub(r'\s+', '', str(x))[:10] for x in (pl.get('bubbles') or [zh]) if str(x).strip()][:3]
            slots = _COMIC_SLOTS.get(len(bs), _COMIC_SLOTS[2])
            base.update({'bubbles': [{'x': x, 'y': y, 'w': w, 'h': h, 'txt': t}
                                     for t, (x, y, w, h) in zip(bs, slots)],
                         'tags': [], 'emojis': _COMIC_EMOJIS,
                         'punch': zh[:6], 'punchX': 480, 'punchY': 700})
        elif typ == 'diagram':
            ns = [re.sub(r'\s+', '', str(x))[:6] for x in (pl.get('nodes') or [zh]) if str(x).strip()][:4]
            if len(ns) < 2:
                base['type'] = 'astro'             # 节点不足撑不起连线，回大字
                base.update({'lines': [{'txt': zh, 'cls': 'hot'}], 'sub': '', 'mesh': True})
            else:
                slots = _DIAGRAM_SLOTS.get(len(ns), _DIAGRAM_SLOTS[2])
                nodes = [{'x': x, 'y': y, 'w': _DIAGRAM_W, 'zh': n}
                         for n, (x, y) in zip(ns, slots)]
                base.update({'nodes': nodes,
                             'edges': [{'from': j, 'to': j + 1} for j in range(len(nodes) - 1)],
                             'head': ''})
        elif typ == 'paper':
            base.update({'mast': zh[:8] or '重磅', 'sub': '', 'photos': []})
        elif typ == 'replay':
            ts = [re.sub(r'\s+', '', str(x))[:6] for x in (pl.get('tags') or []) if str(x).strip()][:4]
            base.update({'zh': zh, 'en': 'REPLAY', 'tag': '', 'thumbs': [], 'tags': ts})
        else:  # astro
            base.update({'lines': [{'txt': zh, 'cls': 'hot'}], 'sub': '', 'mesh': True})
        scenes[i] = base
    return scenes


# ---------------------------------------------------------------- 画面调度
# 2026-10-31 青芒：「小窗位置不变吗？背景就这几个？」→ 实测 18/18 镜全挂左下角
# 小窗、18/18 镜同一个 navy 底，等于把 12 版式模板用成了单一版式。
# 下面两张表把模板已有的 5 窗位 × 3 形状 × 6 背景按版式排开。
# 窗位避让依据模板 CSS 的真实坐标（不是猜）：
#   astro-line 左对齐 top150 字号 170（9 字约到 x1640/y320）→ 窗放下方 br
#   ch-wrap 左对齐 top96 → 窗放右 br / right
#   lst 是 right:96 width:640（清单在右！）→ 窗必须放左 tl / bl
#   stat-wrap 左对齐 top150，gauge 在 left bottom210 → 窗放右上 tr / 右下 br
#   hero 居中 top78 字号 238 满宽、paper 满版 → 藏窗
_HOST_PLAN = {
    'astro':   [('br', 'circle'), ('br', 'rect')],
    'chapter': [('br', 'circle'), ('right', 'circle')],
    'list':    [('tl', 'circle'), ('bl', 'circle')],   # 清单在右侧，窗反过来放左
    'stat':    [('tr', 'circle'), ('br', 'circle')],
    'wall':    [('br', 'circle'), ('tr', 'circle')],   # 跑马灯主行在上半，窗走下半
    'rail':    [('br', 'circle'), ('tr', 'circle')],
    'comic':   [('tr', 'circle'), ('br', 'circle')],
    'diagram': [('br', 'circle'), ('tr', 'circle')],
    'replay':  [('br', 'circle'), ('tr', 'circle')],   # 回放标题在左上，窗走右侧
    'broll':   [('tr', 'circle'), ('br', 'circle')],
    'hero':    [],                                     # 超大空心字居中满宽，放窗必压字
    'paper':   [],                                     # 报纸满版
}

# 背景候选（模板 bgCSS 支持 navy/space/ink/dark/lime；wall 自带 lime 满版、paper 自带纸底）
_BG_PLAN = {'astro':   ['space', 'navy'],
            'chapter': ['ink', 'dark'],
            'list':    ['dark', 'navy'],
            'stat':    ['ink', 'dark'],
            'hero':    ['navy', 'space'],
            'rail':    ['navy', 'ink'],
            'comic':   ['dark', 'ink'],
            'diagram': ['dark', 'ink'],
            'replay':  ['dark', 'navy'],
            'broll':   ['ink', 'dark']}

# 场景级材质（ai-fx 的 #aitex 层，轻量两张先上，避免花到抢字）
_TEX_PLAN = {'chapter': 'grain', 'stat': 'halftone'}


def _decorate(scenes):
    """按版式给每镜配背景 / 小窗位置与形状 / 质感（确定性规则，不依赖 LLM）。

    规则：同一版式的候选里左右交替；与上一镜窗位相同时强制换下一个；
    满版版式（hero/paper）藏窗。LLM 挂了走规则法也照样有这些变化。
    """
    prev_corner = ''
    prev_bg = ''
    alt = 0
    for sc in scenes:
        typ = sc.get('type') or 'astro'

        cands = _BG_PLAN.get(typ) or ['dark', 'navy']
        bg = cands[alt % len(cands)]
        if bg == prev_bg:                      # 相邻不同底，切换才有换场感
            bg = next((c for c in cands if c != prev_bg), bg)
        sc['bg'] = bg
        prev_bg = bg

        if typ in _TEX_PLAN:
            sc['texture'] = _TEX_PLAN[typ]
        if typ == 'wall':
            # wall 自带 lime 满版亮底，白 header/时间码压上去不可读（20261031 实测）
            sc['chrome'] = {'header': False}

        plan = _HOST_PLAN.get(typ, [('br', 'circle')])
        if not plan:
            sc['host'] = False                 # 满版版式，藏窗
            alt += 1
            continue
        corner, shape = plan[alt % len(plan)]
        if corner == prev_corner and len(plan) > 1:
            corner, shape = plan[(alt + 1) % len(plan)]
        prev_corner = corner
        # 尺寸三档轮换（right 预设固定 588×720，不吃 size）
        size = (300, 340, 380)[alt % 3] if corner != 'right' else 340
        sc['host'] = {'corner': corner, 'shape': shape, 'size': size}
        alt += 1
    return scenes


def _wipes_of(scenes):
    """转场稀疏化：只在版式切换处插擦除带（官方参考片 12 镜只 3 次；
    原实现每镜都插，17 次刷红条 = 廉价感的另一半来源）。"""
    wipes, flashes = [], []
    for i in range(1, len(scenes)):
        if scenes[i].get('type') == scenes[i - 1].get('type'):
            continue
        at = round(float(scenes[i]['t0']), 2)
        if len(wipes) < 6:
            wipes.append({'at': at})
        if len(flashes) < 3 and scenes[i].get('type') in ('chapter', 'stat'):
            flashes.append({'at': at, 'op': 0.18})
    return wipes, flashes


def _grab_frames(host_mp4, work, dur, n=4):
    """从 host 代理均匀抽 n 帧给 paper/replay 当照片素材（代理已是 600px I 帧，抽帧快）。"""
    rel = []
    ffmpeg = media.binary('ffmpeg')
    for j in range(n):
        t = dur * (j + 0.5) / n
        dst = Path(work) / ('pf%d.jpg' % j)
        cmd = [str(ffmpeg), '-y', '-hide_banner', '-loglevel', 'error',
               '-ss', '%.2f' % t, '-i', str(host_mp4), '-frames:v', '1',
               '-q:v', '4', str(dst)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if dst.is_file():
            rel.append('work/%s/%s' % (Path(work).name, dst.name))
    return rel


def _fill_frames(scenes, frames):
    """把抽好的帧填进 paper/replay 场（模型/规则都拿不到图，只有这里知道路径）。"""
    if not frames:
        return scenes
    fi = 0
    for sc in scenes:
        if sc.get('type') == 'paper':
            k = min(2, len(frames) - fi)
            if k < 1:
                continue
            slots = _PAPER_SLOTS[k]
            sc['photos'] = [{'x': x, 'y': y, 'w': w, 'h': h, 'src': frames[fi + j], 'cap': ''}
                            for j, (x, y, w, h) in enumerate(slots)]
            fi += k
        elif sc.get('type') == 'replay':
            tags = sc.get('tags') or []
            k = min(max(2, len(tags)), 4, len(frames) - fi)
            if k < 2:
                continue
            sc['thumbs'] = [{'src': frames[fi + j], 't': (tags[j] if j < len(tags) else '')}
                            for j in range(k)]
            fi += k
        if fi >= len(frames):
            break
    return scenes


def _diversify(scenes):
    """版式多样性兜底：连续 ≥3 镜同类型时把中间镜换成 wall（对任何文本都安全）。"""
    run_start = 0
    for i in range(1, len(scenes) + 1):
        if i == len(scenes) or scenes[i]['type'] != scenes[run_start]['type']:
            run = i - run_start
            if run >= 3 and scenes[run_start]['type'] in ('astro', 'wall'):
                mid = scenes[run_start + run // 2]
                zh = mid.get('zh') or (mid.get('lines') or [{}])[0].get('txt') \
                    or (mid.get('rows') or [{}])[0].get('txt') \
                    or (mid.get('cap') or '重点')
                mid.update({'type': 'chapter', 'kicker': '重点', 'zh': zh[:12],
                            'en': 'KEY POINT', 'suben': 'THE POINT'})
                mid.pop('lines', None), mid.pop('rows', None), mid.pop('sub', None)
            run_start = i
    return scenes


# ---------------------------------------------------------------- 对外

def build(lines, duration, title, host_mp4, work):
    """台词 → (scenes, captions, wipes, flashes)。

    lines: [{'start': 秒, 'end': 秒, 'speak': '原句'}]
    与切片工坊 render_sync 里的顺序完全一致：
      切镜 → 规则版式 → LLM 标题（失败回退）→ 补图 → 装饰 → 转场
    """
    dur = float(duration)
    picked = []
    for l in (lines or []):
        if not (l.get('speak') or '').strip():
            continue
        t0 = float(l.get('start') or 0)
        if t0 >= dur:
            break
        picked.append({**l, 'start': t0, 'end': min(float(l.get('end') or dur), dur)})
    segs = _split_lines(picked, dur) if picked else []
    if not segs:
        segs = [{'t0': 0.0, 't1': dur, 'text': title or ' '}]
    elif segs[-1]['t1'] < dur - 1.0:
        # 字幕覆盖不到片尾 → 收片长，否则最后一镜定格几十秒（实测）
        dur = round(segs[-1]['t1'], 2)

    scenes = [_scene_from(s, i, len(segs)) for i, s in enumerate(segs)]
    scenes = _apply_llm(scenes, _llm_plan(segs, title))
    # paper/replay 版式需要照片：从 host 代理均匀抽帧（代理 600px I 帧，秒抽）
    if host_mp4 and Path(host_mp4).is_file() and any(s.get('type') in ('paper', 'replay') for s in scenes):
        scenes = _fill_frames(scenes, _grab_frames(host_mp4, work, dur))
    scenes = _decorate(_diversify(scenes))
    wipes, flashes = _wipes_of(scenes)
    captions = [c for s in segs for c in _caption_split(s)]
    return scenes, captions, wipes, flashes, dur
