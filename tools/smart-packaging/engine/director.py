# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""director.py — 导演层 v1（P1：转写 → 语义路由 → 参数 JSON + 导演阐述）
规则版（LLM 增强留 --llm 钩子）。流程：
  1. 读句级转写（faster-whisper 产物格式 [{start,end,text}]）
  2. 语义切镜：钩子/金句/枚举/对比/警示/误区/章节/收尾 规则识别
  3. 家族选择：内容类型 → 试点三家族（catalog 注册表）
  4. 组装配：segments + narrationMap（连续字幕契约三条）+ face auto
  5. 导演阐述.md：分镜表 + 情绪弧线 + 每个选择的理由（方案门的输入）
验收：卯叔序言 导演产出 vs 人工 15 场景方案的结构重合度。
用法：python director.py --transcript 字幕.json --video 口播.mp4 --music bgm.mp3
       --family auto --out params.json --report 导演阐述.md"""
from pathlib import Path
import argparse, json, re, subprocess, sys

ROOT = Path(__file__).resolve().parent

def load_cues(p):
    d = json.loads(Path(p).read_text(encoding='utf-8'))
    return [{'start': float(c['start']), 'end': float(c['end']), 'text': c['text'].strip()} for c in d if c.get('text', '').strip()]

def join(cues, i, j): return ''.join(c['text'] for c in cues[i:j])

# ---------- 语义切镜规则 ----------
HOOK_PAT = re.compile(r'以前|曾经|想当年|我当初')
NOW_PAT = re.compile(r'现在|如今|轮到|这一次')
QUOTE_PAT = re.compile(r'记住|本质|真相|道理|说白了|实话')
NEG_POS_PAT = re.compile(r'不是.{1,12}[，,]?.{0,4}是')
CONTRAST_PAT = re.compile(r'风光|落寞|一边.{1,8}一边|有人.{1,12}有人')
ENUM_PAT = re.compile(r'第[一二三四五]件|第[一二三四五]个|首先|其次|最后|三件事|两件事')
LIST_PAT = re.compile(r'回报|三件套|三件事|几个|三件|两件|四件')
TIP_PAT = re.compile(r'比如|注意|千万|别忘|记住我的|建议')
MYTH_PAT = re.compile(r'不是.{1,14}(而是|是)')
END_PAT = re.compile(r'关注|陪伴|陪你|下期|下一期|评论区|记得|再见|最后(说|补充)')
KEY_PAT = re.compile(r'^\S{2,8}(人|力|气|钱|局|坑|道|感|心)$')

def classify(cues):
    """返回镜头列表 [{control, i0, i1, why}]（cue 索引区间，左闭右开）"""
    n = len(cues)
    shots = []
    used = [False] * n
    def take(i, j, control, why):
        shots.append({'control': control, 'i0': i, 'i1': j, 'why': why})
        for k in range(i, j): used[k] = True
    # 1. 开场钩子：前 1-4 句内出现 以前→现在 反转 → myth；否则 quote
    head_end = min(8, n)
    head = join(cues, 0, head_end)
    if HOOK_PAT.search(head) and NOW_PAT.search(head):
        j = head_end
        while j < n and cues[j]['end'] - cues[0]['start'] < 16: j += 1
        take(0, min(j, n), 'myth', '开场自我反转（以前划走 vs 现在轮到我）')
    else:
        j = 2
        while j < n and cues[j]['end'] - cues[0]['start'] < 12 and not ENUM_PAT.search(cues[j]['text']): j += 1
        take(0, min(j, n), 'quote', '开场陈述作引语卡')
    # 2. 枚举段：steps + 前置 chapter
    i = 0
    while i < n:
        if used[i]: i += 1; continue
        t = cues[i]['text']
        m = re.search(r'第([一二三四五])件', t)
        if m or ENUM_PAT.search(t) and LIST_PAT.search(join(cues, i, min(i + 2, n))):
            j = i + 1
            while j < n and cues[j]['end'] - cues[i]['start'] < 14 and not re.search(r'第[一二三四五]件', cues[j]['text']):
                if END_PAT.search(cues[j]['text']) and j > i + 2: break
                j += 1
            if m:
                take(i, min(i + 1, n), 'chapter', f'章节卡：{t[:12]}')
                seg = [k for k in range(i + 1, min(j, n)) if not used[k]]
                if seg: take(seg[0], seg[-1] + 1, 'steps', '枚举步骤卡')
            else:
                take(i, min(j, n), 'steps', '枚举结构')
            i = j; continue
        i += 1
    # 3. 对比段
    for i in range(n):
        if used[i]: continue
        if CONTRAST_PAT.search(cues[i]['text']) or (NEG_POS_PAT.search(cues[i]['text']) and len(cues[i]['text']) >= 10):
            j = min(i + 4, n)
            take(i, j, 'compare', '对照结构（A vs B + 结论）')
    # 4. 误区反转
    for i in range(n):
        if used[i]: continue
        if MYTH_PAT.search(cues[i]['text']) and '不是' in cues[i]['text']:
            take(i, min(i + 3, n), 'myth', '误区→真相反转')
    # 5. 警示贴士
    for i in range(n):
        if used[i]: continue
        if TIP_PAT.search(cues[i]['text']) and len(cues[i]['text']) <= 22:
            take(i, min(i + 3, n), 'tip', '经验贴士/避坑')
    # 6. 金句引语（剩余长句中的观点句）
    for i in range(n):
        if used[i]: continue
        t = cues[i]['text']
        if len(t) >= 16 and (QUOTE_PAT.search(t) or (NEG_POS_PAT.search(t) and re.search(r'(。！!|道理|真相|本质)$', t))):
            take(i, min(i + 2, n), 'quote', '观点金句')
    # 7. 清单卡（含列举词 + 顿号/逗号可拆 ≥2 项）
    for i in range(n):
        if used[i]: continue
        t = cues[i]['text']
        if LIST_PAT.search(t):
            j = min(i + 5, n)
            take(i, j, 'list', '要点清单')
    # 8. 关键词卡（收紧：独立短句 + 后续有展开 + 非碎片词尾）
    kw = 0
    for i in range(n):
        if used[i] or kw >= 3: continue
        t = cues[i]['text']
        if 2 <= len(t) <= 8 and not re.search(r'[，。！？的了吗是在呢吧啊呀嘛]$', t) and i + 1 < n and not used[i + 1]:
            j = min(i + 4, n)
            take(i, j, 'keyword', '关键词焦点 + 逐句展开'); kw += 1
    # 9. 收尾（最后未用的 2-4 句）
    tail = [k for k in range(n) if not used[k]]
    if tail:
        k0 = max(tail[0], n - 4)
        take(k0, n, 'outro', '收尾感谢 + 关注引导')
    # 10. 剩余填空：引语卡承接
    i = 0
    while i < n:
        if used[i]: i += 1; continue
        j = i + 1
        while j < n and not used[j] and cues[j]['end'] - cues[i]['start'] < 10: j += 1
        take(i, j, 'quote', '过渡承接')
        i = j
    shots.sort(key=lambda s: s['i0'])
    merged = []
    for s in shots:
        if merged and merged[-1]['control'] == s['control']:
            merged[-1]['i1'] = s['i1']; merged[-1]['why'] += '；' + s['why']
        else:
            merged.append(dict(s))
    # 碎镜并入前邻
    final = []
    for s in merged:
        if final and cues[s['i1'] - 1]['end'] - cues[s['i0']]['start'] < 2.0 and s['control'] in ('quote', 'tip', 'keyword'):
            final[-1]['i1'] = s['i1']
        else:
            final.append(s)
    # 超 18 镜：最短的 quote/tip 向前合并
    while len(final) > 18:
        lens = [(cues[s['i1'] - 1]['end'] - cues[s['i0']]['start'], k)
                for k, s in enumerate(final) if s['control'] in ('quote', 'tip') and k > 0]
        if not lens: break
        _, k = min(lens)
        final[k - 1]['i1'] = final[k]['i1']
        final.pop(k)
    return final

def family_pick(cues, shots):
    text = ''.join(c['text'] for c in cues)
    if re.search(r'打卡|水位|关|升级|期数|复利|成长记录', text): return 'tech-ring', '成长/打卡叙事 → 光环科技'
    if re.search(r'观点|态度|本质|真相|记住', text) and shots and sum(1 for s in shots if s['control'] == 'quote') >= 2:
        return 'type-drama', '观点金句密集 → 字场'
    return 'tech-ring', '默认试点家族（catalog 注册表扩容后放开自动）'

def props_for(control, seg_text, cues, i0, i1):
    t = seg_text
    if control == 'myth':
        m = re.search(r'不是(.{1,18}?)[，,]?(而是|是)(.{1,30})', t)
        myth = ('“' + (m.group(1) if m else t[:18]) + '”') if m else t[:20]
        fact = (m.group(3) if m else t[-20:])[:30]
        return {'kicker': '先说清楚', 'myth': myth[:28], 'fact': fact[:28]}
    if control == 'quote':
        return {'text': t[:60], 'who': '主播'}
    if control == 'keyword':
        word = cues[i0]['text'].strip('，。！？')[:8]
        subs = [cues[k]['text'] for k in range(i0 + 1, i1)][:3]
        return {'kicker': '本期关键词', 'word': word, 'sub': subs}
    if control == 'list':
        items = re.split(r'[，,、；;]|还有|以及', t)
        items = [x.strip() for x in items if 3 <= len(x.strip()) <= 18][:5]
        return {'title': t[:12] if len(t) > 12 else '这一组要点', 'items': items or [t[:14]]}
    if control == 'compare':
        return {'left': {'label': 'A 面', 'big': t[:8] or '这样', 'lines': []},
                'right': {'label': 'B 面', 'big': '另一面', 'lines': []},
                'verdict': t[-14:] or '结论', 'winner': 'none'}
    if control == 'tip':
        kind = 'warn' if re.search(r'别|千万|注意', t) else 'info'
        return {'kicker': '小贴士', 'kind': kind, 'title': cues[i0]['text'][:14], 'text': t[:40]}
    if control == 'steps':
        parts = re.split(r'[，,；;]|然后|接着', t)
        steps = [x.strip() for x in parts if 3 <= len(x.strip()) <= 14][:4]
        return {'title': cues[i0]['text'][:14], 'steps': steps or [t[:12]]}
    if control == 'chapter':
        return {'no': cues[i0]['text'][:6], 'name': cues[min(i0 + 1, len(cues) - 1)]['text'][:14], 'sub': ''}
    if control == 'intro':
        return {'title': t[:16], 'subtitle': '', 'episode': ''}
    if control == 'outro':
        return {'thanks': t[:14] or '感谢观看', 'sub': '', 'name': '本频道',
                'follow': '关注', 'items': [{'text': '下期预告'}]}
    return {}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--transcript', required=True)
    ap.add_argument('--video', required=True)
    ap.add_argument('--music', default='')
    ap.add_argument('--family', default='auto')
    ap.add_argument('--layout', default='auto')
    ap.add_argument('--presenter-label', default='主播')
    ap.add_argument('--title', default='自动导演成片')
    ap.add_argument('--out', required=True)
    ap.add_argument('--report', required=True)
    a = ap.parse_args()
    cues = load_cues(a.transcript)
    n = len(cues)
    dur = cues[-1]['end']
    shots = classify(cues)
    # 时间窗：cue 边界；合并相邻同控
    segs = []
    for s in shots:
        t0, t1 = cues[s['i0']]['start'], cues[s['i1'] - 1]['end']
        if segs and segs[-1]['control'] == s['control'] and t0 - segs[-1]['t1'] < 1.0:
            segs[-1]['t1'] = t1; segs[-1]['_why'] += '；' + s['why']; continue
        segs.append({'control': s['control'], 't0': round(t0, 2), 't1': round(t1, 2),
                     '_why': s['why'], '_i0': s['i0'], '_i1': s['i1']})
    # 强制首段从 0 起、段首尾相接（适配器契约）
    cursor = 0.0
    for s in segs:
        s['t0'] = cursor
        cursor = s['t1']
    segs[-1]['t1'] = round(dur, 2)
    fam, fam_why = (a.family, '人工指定') if a.family != 'auto' else family_pick(cues, shots)
    # narrationMap（连续字幕三条契约）
    nmap = []
    for idx, s in enumerate(segs):
        inter = [i for i, c in enumerate(cues) if c['start'] < s['t1'] and c['end'] > s['t0']]
        beats = []
        for j, ci in enumerate(inter):
            c = cues[ci]
            beats.append({'at': round(c['start'], 2), 'until': round(c['end'], 2),
                          'spokenText': c['text'], 'timing': 'sentence', 'target': 'props.text', 'ci': ci})
        nmap.append({'shot': idx, 'start': s['t0'], 'end': s['t1'],
                     'spokenText': join(cues, s['_i0'], s['_i1'])[:200],
                     'purpose': 'neutral-emphasis', 'timingSource': 'provided-captions',
                     'captionIndices': inter, 'captionBinding': 'continuous', 'beats': beats})
    layout = a.layout
    if layout == 'auto':
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                'stream=width,height', '-of', 'csv=p=0', a.video], capture_output=True, text=True)
            w, h = r.stdout.strip().split(',')
            layout = 'landscape' if int(w) > int(h) else 'portrait'
        except Exception:
            layout = 'portrait'
    params = {'workflow': 'dreamweaver-kit/1',
              'header': {'shortTitle': a.title, 'source': {'kind': 'assistant-summary', 'field': 'director.py v1'}},
              'timeline': {'layout': layout, 'family': fam, 'duration': round(dur, 2),
                           'presenter': {'src': str(Path(a.video).resolve()), 'badge': a.presenter_label,
                                         'audio': 'source', 'framing': 'auto'},
                           'voiceover': '', 'subtitles': '',
                           'music': str(Path(a.music).resolve()) if a.music else '',
                           'captionsData': cues,
                           'segments': [{'control': s['control'], 't0': s['t0'], 't1': s['t1'],
                                         'props': props_for(s['control'], join(cues, s['_i0'], s['_i1']), cues, s['_i0'], s['_i1'])}
                                        for s in segs],
                           'narrationMap': nmap}}
    Path(a.out).write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding='utf-8')
    # 导演阐述（方案门输入）
    rows = '\n'.join(f"| {i+1} | {s['t0']:7.1f}-{s['t1']:7.1f}s | {s['control']} | {s['_why']} |"
                     for i, s in enumerate(segs))
    text_all = ''.join(c['text'] for c in cues)
    arc = '自嘲开场 → 亮明立场 → 干货展开 → 鼓劲收尾' if re.search(r'认了|陪着', text_all) else '引入 → 展开 → 收束'
    Path(a.report).write_text(f"""# 导演阐述（方案门 · 待确认）

片长 {dur:.1f}s · {n} 句 · 家族 **{fam}**（{fam_why}）
情绪弧线判读：{arc}

## 分镜表

| # | 时间 | 控件 | 选镜理由 |
| --- | --- | --- | --- |
{rows}

## 需要你确认的 3 件事

1. 分镜表的选镜是否符合你对这条片的表达意图（尤其 myth/quote 的判读）；
2. 家族选择 {fam} 是否合适（可强制指定 --family）；
3. compare/keyword 的文案是规则抽取版，正式版建议人工润色 5 分钟。

**确认后**：adapter plan → prepare → render（全流程无手工中间步骤）。
""", encoding='utf-8')
    print(json.dumps({'shots': len(segs), 'family': fam, 'out': a.out, 'report': a.report,
                      'controls': [s['control'] for s in segs]}, ensure_ascii=False))

if __name__ == '__main__':
    main()
