# -*- coding: utf-8 -*-
"""字幕切分引擎（OF-2 / W3）：一行口语转写 → 若干字幕条。

移植自 OpenFilm 0.1.7 `studio/server/subtitle-segments.mjs`（MIT 许可），
规则与参数以它为唯一蓝本；CJK 分词用 jieba 替代 Intl.Segmenter。

本文件从切片工坊搬来时只做三处适配（2026-11-14）：
  1. jieba 改为优先用随包 `_vendor/jieba`（目标包 tools/python 没装 jieba），并关掉
     `jieba.cache` 落盘；都不可用时退化为纯标准库的二字切分，仍然只在字符间隙切。
  2. 新增 `TARGET_RULES`：单屏 ≤36 格（两行 ×18），单条 ≤5 秒——沿用 IP 工作台
     `media.CAPTION_MAX_SECONDS` 的硬约束，但按"两行"重新分配。
  3. 新增 `split_for_display`：把一条字幕按词边界拆成最多两行，行宽 ≤18 格，
     供双行花字使用；绝不在词中间断开。

原规则（序号即蓝本文章节号）：
  1. 单元：拉丁词整词（含内部 .'-/ 等）、CJK 按词典分词；标点归属（闭归前、开归后）。
  2. 硬断：句末标点（。！？!?… 及非小数/缩写/首字母的句号）与 ≥pauseMs 的真实停顿。
  3. 从句断：，；：,;:— 与括号边——两侧各 ≥minUnits 才断；顿号、不断。
  4. 长度：单行 ≤maxCells 或 ≤maxChars；仍超长按成本函数选断点（DP）。
  5. 时间：随首词起、末词止，holdMs 内多留不侵入下一条；<closeGapMs 缝隙合并；
     <minMs 延入后续静默或与邻条合并；行末条随行止。
  6. 文本：中文行尾剥 ，。、；：（？！… 与闭引号保留）；拉丁保留标点。

纯函数、零 IO：不读文件、不读时钟，同一行永远切出同样的条。
"""
from __future__ import annotations

import re
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 参数表（与蓝本 SUBTITLE_SEGMENT_RULES 逐项一致，冻结——改动即新版本）
# ---------------------------------------------------------------------------

SUBTITLE_SEGMENT_RULES: dict[str, Any] = {
    "maxCells": 16,      # CJK 单行上限（格：全角 1，其余 ½；≈手机屏宽）
    "maxChars": 42,      # 拉丁单行上限（广播/流媒体惯例）
    "minUnits": 4,       # 从句断两侧最小单位（CJK 字=1，拉丁词=2）
    "pauseMs": 500,      # 词间停顿 ≥ 此值断条
    "minMs": 700,        # 最短驻留（再短读不完）
    "maxMs": 7000,       # 最长驻留（再长是文字墙）
    "holdMs": 200,       # 末词后多留（不侵入下一条）
    "closeGapMs": 150,   # 缝隙 < 此值合并（闪烁比连播糟）
    "msPerCell": 220,    # 有词级时间时，字间文本按此语速铺
    "weight": {"latin": 0.32, "space": 0.15, "clause": 1.5, "sentence": 2.5},
    "cost": {
        "piece": 10, "balance": 4, "short": 6, "over": 50, "long": 20,
        "cjk": 1, "cjkSingle": 1, "mixed": 3, "bracket": 8,
        "leansBack": 8, "leansForward": 4,
        "conjunction": -4, "particle": -3, "pause": -10,
        "sentence": -6, "clause": -4, "distance": 20, "merge": 6,
    },
}

RULES_VERSION = "of-w3-20261017-v1"

# 目标系统约束：单屏最多两行、每行 18 格（约 34~36px 字号下 1080 宽可读），单条 ≤5 秒。
DISPLAY_LINE_CELLS = 18
DISPLAY_MAX_LINES = 2
TARGET_RULES = dict(SUBTITLE_SEGMENT_RULES)
TARGET_RULES.update({"maxCells": DISPLAY_LINE_CELLS * DISPLAY_MAX_LINES,
                    "maxMs": 4600, "minMs": 700})

# ---------------------------------------------------------------------------
# 字符分类（蓝本 91-133 行）
# ---------------------------------------------------------------------------

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3040-\u309f\u30a0-\u30ff\u3005\u3006\u30fc\uff70]")
HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff]")
LETTER = re.compile(r"[^\W_]", re.UNICODE)  # \p{L}\p{N} 近似（词字符去下划线；含数字——蓝本 LETTER=/[\p{L}\p{N}\p{M}]/u，数字是词核：1,000/10:30/第3 的 glue 全靠它）
DIGIT = re.compile(r"\d")
WIDE = re.compile(r"[\u1100-\u115f\u2e80-\ua4cf\ua960-\ua97f\uac00-\ud7a3\uf900-\ufaff\ufe10-\ufe19\ufe30-\ufe6f\uff00-\uff60\uffe0-\uffe6]")
FULL_PUNCT = re.compile(r"[\uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f]")
JOIN = set([".", "'", "\u2019", "-", "_", "/", "&"])   # 拉丁词内：3.5 / don't / R&D
NUM_JOIN = set([",", ":"])                              # 数字内：1,000 / 10:30
OPEN = set("\uff08([\u3010\uff3b\u300c\u300e\u300a\u3008\u201c\u2018\uff5b{")
CLOSE = set("\uff09)]\u3011\uff3d\u300d\u300f\u300b\u3009\u201d\u2019\uff5d}")
PAREN_OPEN = re.compile(r"[\uff08(\u3010\uff3b]")
PAREN_CLOSE = re.compile(r"[\uff09)\u3011\uff3d]")
CURRENCY = set("$\u00a5\u20ac\u00a3\u20a9#@")
SENTENCE_MARK = re.compile(r"[\u3002\uff01\uff1f!?\u2026\u203c\u2047\u2048\u2049\uff61]|\.{2,}")
CLAUSE_MARK = re.compile(r"[\uff0c,\uff1b;\uff1a:\u2014\uff5c|]")
LIST_MARK = re.compile(r"\u3001")
SPACED_DASH = re.compile(r"\s[-\u2013]+(?:\s|$)")
DOT = re.compile(r"[.\uff0e]")
NUMBER_RE = re.compile(r"^\d[\d.,:]*$")

NEVER_ENDS = frozenset(["mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs",
                        "e.g", "i.e", "cf", "approx", "dept", "gen", "gov", "sgt", "capt", "lt", "col", "rev", "hon"])
NUMBERED = frozenset(["no", "nos", "vol", "ch", "p", "pp", "fig", "art", "sec", "ep"])
ENDS_BEFORE_CAPITAL = frozenset(["etc", "inc", "ltd", "co", "corp", "jan", "feb", "mar", "apr", "jun",
                                 "jul", "aug", "sep", "sept", "oct", "nov", "dec"])
MEASURE = re.compile(r"^[秒分时个只元块岁年月日号天周次度米克斤里层件位名条张部本台种点帧行页分万亿千百毫厘小时十]")
UNITS = frozenset(["s", "ms", "sec", "secs", "min", "mins", "h", "hr", "hrs", "k", "m", "km", "cm", "mm",
                   "g", "kg", "mg", "lb", "lbs", "ft", "in", "b", "kb", "mb", "gb", "tb", "fps", "px", "pt",
                   "em", "x", "hz", "khz", "mhz", "ghz", "db", "am", "pm", "p", "mph", "kph", "percent",
                   "usd", "rmb", "yuan"])
CJK_CONJUNCTIONS = ["但是", "但", "所以", "因为", "然后", "而且", "如果", "和", "或", "或者", "还是",
                    "不过", "可是", "并且", "于是", "因此", "虽然", "然而", "而是", "以及", "只要", "即使"]
LATIN_CONJUNCTIONS = frozenset(["and", "but", "so", "because", "which", "that", "or", "when", "while",
                                "if", "where", "who", "though", "although", "then", "unless", "until"])
LEANS_BACK = frozenset(["了", "的", "着", "过", "们", "地", "得", "吗", "呢", "吧", "啊", "呀", "嘛", "么", "之", "的话"])
LEANS_FORWARD = frozenset(["a", "an", "the", "to", "of", "in", "on", "at", "by", "for", "with", "from",
                           "into", "about", "my", "your", "our", "their", "his", "her", "its", "this",
                           "these", "those", "very", "把", "被", "给", "对", "从", "向", "跟", "用", "在", "将"])
FILLERS = frozenset(["呃", "嗯", "额", "uh", "um", "erm", "mm", "mmm", "hmm", "mhm"])
CJK_PARTICLES = ["的话", "吧", "呢", "啊", "吗", "呀", "嘛"]


def _cjk_text(t: str) -> bool:
    return bool(CJK.search(t) or HANGUL.search(t) or FULL_PUNCT.search(t))


def subtitle_cells(t: str) -> float:
    """文本宽度（格）：全角/CJK 计 1，其余计 ½。"""
    return sum(1.0 if (WIDE.match(ch) or CJK.match(ch)) else 0.5 for ch in t)


def subtitle_end(text: str) -> str:
    """字幕落版行尾：中文剥 ，。、；：（…与闭引号保留）；拉丁保留标点。"""
    t = text.rstrip()
    if not _cjk_text(t) or re.search(r"(?:\.{2,}|\u2026)$", t):
        return t
    return re.sub(r"[\s\uff0c,\u3001\u3002\uff0e.\uff1b;\uff1a:]+$", "", t)


def split_for_display(text: str, max_cells: int = DISPLAY_LINE_CELLS,
                      max_lines: int = DISPLAY_MAX_LINES) -> list[str]:
    """把一条字幕拆成最多两行（每行 ≤max_cells 格），只在词边界断开。

    拆不出合规的两行时返回一行原文——绝不为了凑行数在词中间切。
    """
    t = (text or "").strip()
    if not t:
        return [""]
    if max_lines <= 1 or subtitle_cells(t) <= max_cells:
        return [t]
    atoms = _atoms_of(t)
    if len(atoms) < 2:
        return [t]
    best: Optional[tuple[float, int]] = None
    for k in range(1, len(atoms)):
        left = t[:atoms[k]["from"]].rstrip()
        right = t[atoms[k]["from"]:].strip()
        if not left or not right:
            continue
        wl, wr = subtitle_cells(left), subtitle_cells(right)
        if wl > max_cells or wr > max_cells:
            continue
        score = -abs(wl - wr)
        if atoms[k - 1]["trail"].strip():
            score += 1.5          # 优先在标点后断
        if atoms[k - 1]["core"] in LEANS_BACK:
            score += 1.0          # 语气词/助词后断更好读
        if best is None or score > best[0]:
            best = (score, k)
    if best is not None:
        k = best[1]
        return [t[:atoms[k]["from"]].rstrip(), t[atoms[k]["from"]:].strip()]
    for k in range(len(atoms) - 1, 0, -1):
        left = t[:atoms[k]["from"]].rstrip()
        if left and subtitle_cells(left) <= max_cells:
            return [left, t[atoms[k]["from"]:].strip()]
    return [t]


# ---------------------------------------------------------------------------
# 分词（蓝本 160-262 行）：CJK 用 jieba，拉丁整词含内部连接符
# ---------------------------------------------------------------------------

def _cjk_words(run: str) -> list[tuple[int, int]]:
    """CJK 串切成词，返回 [(from, to))。优先随包 jieba；不可用时退化为二字切分。"""
    out = []
    at = 0
    segmenter = _jieba_cut()
    if segmenter is None:
        # 退化路径：按两字一组切（中文词多为二字），仍然只在字符间隙断，不做句中插字
        while at < len(run):
            step = 2 if len(run) - at > 1 else 1
            out.append((at, at + step))
            at += step
        return out
    for w in segmenter(run):
        out.append((at, at + len(w)))
        at += len(w)
    return out


_JIEBA = None
_JIEBA_TRIED = False


def _jieba_cut():
    """惰性加载分词器：随包 _vendor/jieba → 系统 jieba → None。"""
    global _JIEBA, _JIEBA_TRIED
    if _JIEBA_TRIED:
        return _JIEBA
    _JIEBA_TRIED = True
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # 随包 jieba 的老式正则字面量会报 SyntaxWarning
            try:
                from _vendor import jieba  # 随包词典，买家机器无需 pip
            except ImportError:
                import jieba  # 开发机可能装了
        # 词典缓存写进包内 .runtime/cache，不落到工作目录；并关掉 jieba 的启动日志
        try:
            import tempfile
            from pathlib import Path
            cache_dir = Path(__file__).resolve().parent / ".runtime" / "cache"
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                jieba.dt.tmp_dir = str(cache_dir)   # setter 会把 cache_file 一起改成绝对路径
            except OSError:
                jieba.dt.tmp_dir = tempfile.gettempdir()
            jieba.setLogLevel(60)
        except Exception:
            pass
        jieba.initialize()
        _JIEBA = jieba.cut
    except Exception:
        _JIEBA = None
    return _JIEBA


def _cores_of(t: str) -> list[dict[str, Any]]:
    """文本的词核：[from, to) + 是否 CJK。"""
    out = []
    i = 0
    n = len(t)
    while i < n:
        ch = t[i]
        if CJK.match(ch):
            j = i
            while j < n and CJK.match(t[j]):
                j += 1
            for (a, b) in _cjk_words(t[i:j]):
                out.append({"from": i + a, "to": i + b, "cjk": True})
            i = j
            continue
        if not LETTER.match(ch):
            i += 1
            continue
        j = i + 1
        while j < n:
            c = t[j]
            if LETTER.match(c) and not CJK.match(c):
                j += 1
                continue
            nxt = t[j + 1] if j + 1 < n else ""
            joins = (nxt and LETTER.match(nxt) and not CJK.match(nxt)
                     and (c in JOIN or (c in NUM_JOIN and DIGIT.match(t[j - 1]) and DIGIT.match(nxt))))
            if not joins:
                break
            j += 1
        out.append({"from": i, "to": j, "cjk": False})
        i = j
    return out


def _lead_start(t: str, prev_to: int, frm: int) -> int:
    """词的开括号/引号/货币符号起点（prev_to..frm 之间向左归并）。"""
    i = frm
    while i > prev_to:
        ch = t[i - 1]
        opening = (ch in OPEN or ch in CURRENCY
                   or (ch == '"' and i - 1 > prev_to and not LETTER.match(t[i - 2])))
        if not opening:
            break
        i -= 1
    return i


def _atoms_of(t: str) -> list[dict[str, Any]]:
    """词核 + 归属标点 = 原子，覆盖全文端到端。"""
    cores = _cores_of(t)
    atoms = []
    for k, c in enumerate(cores):
        frm = 0 if k == 0 else _lead_start(t, cores[k - 1]["to"], c["from"])
        to = _lead_start(t, c["to"], cores[k + 1]["from"]) if k + 1 < len(cores) else len(t)
        core = t[c["from"]:c["to"]]
        atoms.append({
            "from": frm, "core_from": c["from"], "core_to": c["to"], "to": to,
            "core": core, "trail": t[c["to"]:to], "lead": t[frm:c["from"]],
            "cjk": c["cjk"], "number": bool(NUMBER_RE.match(core)),
            "units": 0 if core.lower() in FILLERS else (len(core) if c["cjk"] else 2),
            "start": 0.0, "end": 0.0,
        })
    return atoms


def _stop_ends(prev: dict, nxt: Optional[dict]) -> bool:
    """prev 后的句号是否结束句子（缩写/首字母/编号消歧）。"""
    if nxt is None or nxt["cjk"]:
        return True
    word = prev["core"].lower()
    if word in NEVER_ENDS:
        return False
    if nxt["number"] and word in NUMBERED:
        return False
    if re.match(r"^[A-Z]$", prev["core"]):
        return False
    if re.match(r"^(?:[A-Za-z]\.)+[A-Za-z]$", prev["core"]) or word in ENDS_BEFORE_CAPITAL:
        return bool(re.match(r"^[A-Z]", nxt["core"])) and nxt["core"] != "I"
    return True


# ---------------------------------------------------------------------------
# 计时（蓝本 264-302 行）
# ---------------------------------------------------------------------------

def _weight_of(ch: str, R: dict) -> float:
    if CJK.match(ch) or HANGUL.match(ch):
        return 1.0
    if LETTER.match(ch):
        return R["weight"]["latin"]
    if ch.isspace():
        return R["weight"]["space"]
    if SENTENCE_MARK.match(ch) or DOT.match(ch):
        return R["weight"]["sentence"]
    if CLAUSE_MARK.match(ch) or LIST_MARK.match(ch) or PAREN_OPEN.match(ch) or PAREN_CLOSE.match(ch):
        return R["weight"]["clause"]
    return 0.0


def _js_round(x: float) -> int:
    """蓝本 Math.round 语义（半向上）——Python 内置 round 是银行家舍入，
    时值落在 .5 会与蓝本分叉（对照锁断言口径）。"""
    import math
    return int(math.floor(x + 0.5))


def _timeline(line: dict, R: dict) -> dict[str, list[float]]:
    """每字符起止 ms：有 marks 段内按语速（≤msPerCell）铺、余量是停顿；无 marks 按权重摊。"""
    t = line["text"]
    n = len(t)
    start = [0.0] * n
    end = [0.0] * n
    weights = [_weight_of(ch, R) for ch in t]
    marks = [m for m in (line.get("marks") or []) if 0 <= m["index"] < n]
    if marks:
        spans = ([{"index": 0, "startMs": line["startMs"]}] if marks[0]["index"] > 0 else []) + marks
    else:
        spans = [{"index": 0, "startMs": line["startMs"]}]
    for k, m in enumerate(spans):
        to = spans[k + 1]["index"] if k + 1 < len(spans) else n
        until = spans[k + 1]["startMs"] if k + 1 < len(spans) else line["endMs"]
        room = max(0.0, until - m["startMs"])
        total = sum(weights[m["index"]:to])
        if not total:
            per = 0.0
        elif not marks:
            per = room / total
        else:
            per = min(R["msPerCell"], room / total)
        at = float(m["startMs"])
        for i in range(m["index"], to):
            start[i] = at
            at += weights[i] * per
            end[i] = at
    return {"start": start, "end": end}


# ---------------------------------------------------------------------------
# 边界分析（蓝本 304-353 行）
# ---------------------------------------------------------------------------

def _analyse(t: str, R: dict, times: Optional[dict], heard: bool):
    """原子 + 原子间边界（bounds[k] 在原子 k-1 与 k 之间）。heard=True 才信停顿。"""
    atoms = _atoms_of(t)
    if times:
        for a in atoms:
            a["start"] = times["start"][a["core_from"]]
            a["end"] = times["end"][a["core_to"] - 1]
    C = R["cost"]
    bounds: dict[int, dict[str, Any]] = {}
    depth = 0
    quoted = False
    for k in range(len(atoms) - 1):
        a, nxt = atoms[k], atoms[k + 1]
        for ch in a["lead"]:
            if ch in OPEN:
                depth += 1
            elif ch == '"':
                depth += 1
                quoted = True
        for ch in a["trail"]:
            if ch in CLOSE:
                depth = max(0, depth - 1)
            elif ch == '"' and quoted:
                depth = max(0, depth - 1)
                quoted = False
        trail = a["trail"].strip()
        gap = max(0.0, nxt["start"] - a["end"]) if heard else 0.0
        stop = bool(DOT.search(trail[-1:]) and not re.search(r"\.{2,}$", trail) and _stop_ends(a, nxt))
        sentence = bool(SENTENCE_MARK.search(trail)) or stop
        clause = bool(CLAUSE_MARK.search(trail) or SPACED_DASH.search(a["trail"])
                      or PAREN_CLOSE.search(trail) or PAREN_OPEN.search(nxt["lead"]))
        glue = (not trail
                and ((a["number"] and (MEASURE.match(nxt["core"]) if nxt["cjk"] else nxt["core"].lower() in UNITS))
                     or (a["cjk"] and a["core"].endswith("第") and nxt["number"])))
        cost = 0.0
        if a["cjk"] and nxt["cjk"]:
            cost += C["cjk"] + (C["cjkSingle"] if len(a["core"]) == 1 or len(nxt["core"]) == 1 else 0)
        elif a["cjk"] != nxt["cjk"]:
            cost += C["mixed"]
        if depth > 0:
            cost += C["bracket"]
        if nxt["cjk"] and nxt["core"] in LEANS_BACK:
            cost += C["leansBack"]
        if not trail and a["core"].lower() in LEANS_FORWARD:
            cost += C["leansForward"]
        if gap >= 120:
            cost += C["pause"] * min(1.0, (gap - 120) / max(1, R["pauseMs"] - 120))
        conjunction = (any(nxt["core"] == c or (len(c) > 1 and nxt["core"].startswith(c)) for c in CJK_CONJUNCTIONS)
                       if nxt["cjk"] else nxt["core"].lower() in LATIN_CONJUNCTIONS)
        if conjunction:
            cost += C["conjunction"]
        if a["cjk"] and any(a["core"] == p or (len(p) > 1 and a["core"].endswith(p)) for p in CJK_PARTICLES):
            cost += C["particle"]
        if sentence:
            cost += C["sentence"]
        elif clause or LIST_MARK.search(trail):
            cost += C["clause"]
        bounds[k + 1] = {
            "hard": gap >= R["pauseMs"] or (depth == 0 and sentence),
            "sentence": depth == 0 and sentence,
            "clause": depth == 0 and clause,
            "glue": glue, "cost": cost, "gap": gap, "depth": depth,
        }
    return atoms, bounds


def _measures(t: str, atoms: list, R: dict):
    cjk = _cjk_text(t)
    cap = R["maxCells"] if cjk else R["maxChars"]

    def width(i: int, j: int) -> float:
        shown = t[atoms[i]["from"]:atoms[j - 1]["core_to"]].strip()
        return subtitle_cells(shown) if cjk else float(len(shown))

    def units(i: int, j: int) -> float:
        return float(sum(atoms[k]["units"] for k in range(i, j)))

    def dur(i: int, j: int) -> float:
        return atoms[j - 1]["end"] - atoms[i]["start"]

    return {"cap": cap, "width": width, "units": units, "dur": dur}


def _cut_long(s: int, e: int, bounds: dict, m: dict, R: dict) -> list[int]:
    """[s, e) 超长：DP 选成本最低断点，返回各段起始原子索引。"""
    C = R["cost"]
    n = e - s
    INF = float("inf")
    best = [INF] * (n + 1)
    back = [0] * (n + 1)
    best[0] = 0.0
    for j in range(1, n + 1):
        for i in range(j - 1, -1, -1):
            if best[i] == INF or (i > 0 and bounds[s + i]["glue"]):
                continue
            w = m["width"](s + i, s + j)
            cost = best[i] + C["piece"] + C["balance"] * (min(w, m["cap"]) / m["cap"]) ** 2
            if w > m["cap"]:
                cost += C["over"] * (w - m["cap"])
            if m["units"](s + i, s + j) < R["minUnits"]:
                cost += C["short"]
            d = m["dur"](s + i, s + j)
            if d > R["maxMs"]:
                cost += C["long"] * (d / R["maxMs"])
            if i > 0:
                cost += bounds[s + i]["cost"]
            if cost < best[j]:
                best[j] = cost
                back[j] = i
    starts = []
    j = n
    while j > 0:
        starts.insert(0, s + back[j])
        j = back[j]
    return starts


# ---------------------------------------------------------------------------
# 主入口（蓝本 405-487 行）
# ---------------------------------------------------------------------------

def segment_line(line: dict, R: dict = SUBTITLE_SEGMENT_RULES) -> list[dict]:
    """一行口语切成字幕条。恒 ≥1 条；条覆盖全文、有序、只依赖行本身（确定性）。

    line: {"text", "startMs", "endMs", "marks"?: [{"index", "startMs"}]}
    返回: [{"text", "startMs", "endMs", "marks"?, "part", "from", "to"}]
    （字段名保持蓝本 camelCase，便于与蓝本对照与将来 shadow 比对。）
    """
    t = line["text"]

    def whole():
        lead = len(t) - len(t.lstrip())
        out = {"text": t.strip(), "startMs": line["startMs"], "endMs": line["endMs"],
               "part": 0, "from": lead, "to": len(t.rstrip())}
        if line.get("marks"):
            out["marks"] = [dict(m) for m in line["marks"]]
        return [out]

    times = _timeline(line, R)
    atoms, bounds = _analyse(t, R, times, bool(line.get("marks")))
    if len(atoms) < 2:
        return whole()
    m = _measures(t, atoms, R)

    # 2. 句断（含长停顿）→ 3. 从句断（两侧够单位）→ 4. 仍超长走 DP
    starts: list[int] = []
    sentence = 0
    for k in range(1, len(atoms) + 1):
        if k < len(atoms) and not bounds[k]["hard"]:
            continue
        piece = sentence
        clauses = [sentence]
        for c in range(sentence + 1, k):
            if not bounds[c]["clause"]:
                continue
            if m["units"](piece, c) >= R["minUnits"] and m["units"](c, k) >= R["minUnits"]:
                clauses.append(c)
                piece = c
        for ni, frm in enumerate(clauses):
            to = clauses[ni + 1] if ni + 1 < len(clauses) else k
            if m["width"](frm, to) <= m["cap"] and m["dur"](frm, to) <= R["maxMs"]:
                starts.append(frm)
            else:
                starts.extend(_cut_long(frm, to, bounds, m, R))
        sentence = k

    # 5. 时间：过短的延或并
    pieces = [{"a": a, "b": (starts[ni + 1] if ni + 1 < len(starts) else len(atoms)),
               "startMs": 0, "endMs": 0} for ni, a in enumerate(starts)]

    def strength(k: int) -> int:
        b = bounds.get(k)
        if not b:
            return 0
        return 3 if b["gap"] >= R["pauseMs"] else 2 if b["sentence"] else 1 if b["clause"] else 0

    def assign_time():
        for ni, p in enumerate(pieces):
            p["startMs"] = line["startMs"] if ni == 0 else max(_js_round(atoms[p["a"]]["start"]),
                                                               pieces[ni - 1]["startMs"] + 1)
        for ni, p in enumerate(pieces):
            nxt = pieces[ni + 1] if ni + 1 < len(pieces) else None
            if not nxt:
                p["endMs"] = max(line["endMs"], p["startMs"] + 1)
                return
            end = min(nxt["startMs"], max(_js_round(atoms[p["b"] - 1]["end"]), p["startMs"] + 1) + R["holdMs"])
            if end - p["startMs"] < R["minMs"]:
                end = min(nxt["startMs"], p["startMs"] + R["minMs"])
            if nxt["startMs"] - end < R["closeGapMs"]:
                end = nxt["startMs"]
            p["endMs"] = end

    def joinable(n: int, o: int) -> bool:
        if n < 0 or o < 0 or n >= len(pieces) or o >= len(pieces):
            return False
        p, q = pieces[n], pieces[o]
        a, b = min(p["a"], q["a"]), max(p["b"], q["b"])
        return m["width"](a, b) <= m["cap"] and m["dur"](a, b) <= R["maxMs"]

    assign_time()
    for _guard in range(len(pieces), 0, -1):
        target = -1
        for i, p in enumerate(pieces):
            if (p["endMs"] - p["startMs"] < R["minMs"] and len(pieces) > 1
                    and (joinable(i, i - 1) or joinable(i, i + 1))):
                target = i
                break
        if target < 0:
            break
        options = [o for o in (target - 1, target + 1) if joinable(target, o)]
        options.sort(key=lambda o: (strength(pieces[max(target, o)]["a"]),
                                    m["width"](pieces[o]["a"], pieces[o]["b"])))
        o = options[0]
        lo, hi = (o, target) if o < target else (target, o)
        pieces[lo:hi + 1] = [{"a": pieces[lo]["a"], "b": pieces[hi]["b"], "startMs": 0, "endMs": 0}]
        assign_time()
    if len(pieces) == 1:
        return whole()

    # 各条的文本与词级时间（marks 随条迁移，条首无 mark 补一条）
    marks = line.get("marks") or []
    out = []
    lead_ws = len(t) - len(t.lstrip())
    tail = len(t.rstrip())
    for part, p in enumerate(pieces):
        frm = lead_ws if p["a"] == 0 else atoms[p["a"]]["from"]
        to = tail if p["b"] == len(atoms) else frm + len(t[frm:atoms[p["b"]]["from"]].rstrip())
        own = [mk for mk in marks if frm <= mk["index"] < to]
        own = [{"index": mk["index"] - frm, "startMs": mk["startMs"]} for mk in own]
        if marks and (not own or own[0]["index"] != 0):
            own = [{"index": 0, "startMs": p["startMs"]}] + own
        piece = {"text": t[frm:to], "startMs": p["startMs"], "endMs": p["endMs"], "part": part,
                 "from": frm, "to": to}
        if own:
            piece["marks"] = own
        out.append(piece)
    return out


# ---------------------------------------------------------------------------
# 翻译跟随（蓝本 489-562 行）
# ---------------------------------------------------------------------------

def align_translation(text: str, pieces: list[dict], R: dict = SUBTITLE_SEGMENT_RULES) -> list[dict]:
    """译文按原文的切法切：每条原文给一份译文（切不好时一份译文跨多条）。"""
    lead = len(text) - len(text.lstrip())
    tail = len(text.rstrip())
    whole = {"text": text.strip(), "from": lead, "to": max(lead, tail)}
    if len(pieces) < 2 or not whole["text"]:
        return [dict(whole) for _ in pieces]
    atoms, bounds = _analyse(text, R, None, False)
    A, N = len(atoms), len(pieces)
    if A < 2:
        return [dict(whole) for _ in pieces]
    C = R["cost"]
    m = _measures(text, atoms, R)
    before = [0.0] * (A + 1)
    for k in range(A):
        before[k + 1] = before[k] + sum(_weight_of(ch, R) for ch in atoms[k]["core"])
    total = before[A] or 1.0
    span = max(1, pieces[N - 1]["endMs"] - pieces[0]["startMs"])
    target = [(p["startMs"] - pieces[0]["startMs"]) / span for p in pieces[1:]]

    def short_cost(a: int, b: int) -> float:
        return C["short"] if m["units"](a, b) < R["minUnits"] else 0.0

    INF = float("inf")
    best = [[INF] * A for _ in range(N)]
    back = [[-1] * A for _ in range(N)]
    best[0][0] = 0.0
    for j in range(N - 1):
        for p in range(A):
            here = best[j][p]
            if here == INF:
                continue
            if here + C["merge"] < best[j + 1][p]:
                best[j + 1][p] = here + C["merge"]
                back[j + 1][p] = p
            for q in range(p + 1, A):
                if bounds[q]["glue"]:
                    continue
                cost = here + C["distance"] * abs(before[q] / total - target[j]) + bounds[q]["cost"] + short_cost(p, q)
                if cost < best[j + 1][q]:
                    best[j + 1][q] = cost
                    back[j + 1][q] = p
    last, lowest = 0, INF
    for p in range(A):
        cost = best[N - 1][p] + short_cost(p, A)
        if cost < lowest:
            lowest, last = cost, p
    cuts = [-1] * (N - 1)
    p = last
    for j in range(N - 1, 0, -1):
        prev = back[j][p]
        if prev != p:
            cuts[j - 1] = p
        p = prev
    out = []
    n = 0
    at = 0
    while n < N:
        last_n = n
        while last_n < N - 1 and cuts[last_n] < 0:
            last_n += 1
        until = cuts[last_n] if last_n < N - 1 else A
        a = lead if at == 0 else atoms[at]["from"]
        b = tail if until == A else a + len(text[a:atoms[until]["from"]].rstrip())
        one = {"text": text[a:b], "from": a, "to": b}
        while n <= last_n:
            out.append(one)
            n += 1
        at = until
    return out


# ---------------------------------------------------------------------------
# 人工修正回写（蓝本 564-602 行）
# ---------------------------------------------------------------------------

def replace_piece(line: dict, piece: dict, text: str, start_ms: Optional[float] = None) -> dict:
    """把对某一条的修正写回整行：行尾被剥的标点补回（新文本自带标点除外）；
    清空该条则连尾部空白一起去掉；条外 marks 不动，本条按整体重计时。"""
    t = line["text"]
    raw = t[piece["from"]:piece["to"]]
    dropped = raw[len(subtitle_end(raw)):]
    said = re.sub(r"\s+", " ", text).strip()
    end = piece["to"]
    insert = said
    if not said:
        while end < len(t) and t[end].isspace():
            end += 1
    elif not re.search(r"[^\w\s]$", said, re.UNICODE):
        insert += dropped
    out = t[:piece["from"]] + insert + t[end:]
    delta = len(insert) - (end - piece["from"])
    marks = []
    for mk in line.get("marks") or []:
        if mk["index"] < piece["from"]:
            marks.append(dict(mk))
        elif mk["index"] >= end:
            marks.append({"index": mk["index"] + delta, "startMs": mk["startMs"]})
    if said and start_ms is not None and piece["from"] > 0 and line.get("marks"):
        marks.append({"index": piece["from"], "startMs": start_ms})
        marks.sort(key=lambda m: m["index"])
    lead = len(out) - len(out.lstrip())
    out = out.strip()
    marks = [{"index": mk["index"] - lead, "startMs": mk["startMs"]} for mk in marks]
    marks = [mk for mk in marks if 0 <= mk["index"] < len(out)]
    result: dict[str, Any] = {"text": out}
    if marks:
        result["marks"] = marks
    return result
