"""Local multi-signal style matching and controlled MotionTemplates rendering.

The cut transcript is authoritative. No model call, upload, arbitrary client paths,
or shared engine work directory. Plans and previews are bound to source + version.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import eva_packaging
import jd_packaging
import media
import music_library
import bgm_mix
import subtitle_segment
import reference_packaging
import spatial_packaging
from db import Conflict

ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / 'tools' / 'smart-packaging'
# 配乐曲库：只读取用户放进来的已授权曲目，包内不预置任何音乐文件。
MUSIC_DIR = KIT / 'engine/audio/music'
MUSIC_EXTS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}
# 关键词高亮词表（顺序=优先级，移植自切片工坊 hi_words 的匹配方式）
HIGHLIGHT_WORDS = ('本质', '真相', '核心', '关键', '真正的', '反而是', '恰恰', '其实是',
                   '秘密', '重点', '记住', '认知', '边界', '价值', '机会', '代价')


class NoTranscript(ValueError):
    """Raised when a cut artifact has no transcript yet; callers add a next step."""
PLAN_LOCK = threading.RLock()
RENDER_LOCK = threading.Lock()
VERSION = 'smart-packaging/3'
# MS 模板没有历史题材登记，按模板名/版式给出轻量亲和度，避免全部分数相同后按目录顺序乱推。
MS_AFFINITY = {
    '像素游戏': {'playful': 3, 'technical': 1},
    '开源T01': {'energetic': 3, 'opinion': 1},
    '开源T02': {'technical': 3, 'analytical': 1},
    '开源T03': {'business': 2, 'reflective': 1},
    '开源T04': {'reflective': 2, 'calm': 2},
    '开源T05': {'opinion': 3, 'energetic': 1},
    '开源T06': {'practical': 2, 'business': 1},
    '开源T07': {'playful': 3, 'emotional': 1},
    '开源T08': {'technical': 2, 'analytical': 1},
    '开源T09': {'analytical': 2, 'business': 1},
    '开源T10': {'technical': 2, 'analytical': 1},
    '开源T11': {'analytical': 3, 'business': 1},
    '开源T12': {'reflective': 3, 'calm': 1},
    '开源T13': {'technical': 3},
    '开源T14': {'technical': 2, 'energetic': 1},
    '开源T15': {'practical': 3, 'growth': 2},
    '开源T16': {'playful': 4, 'energetic': 1},
    '开源T17': {'analytical': 3, 'technical': 2},
    '开源T19': {'business': 2, 'reflective': 1},
    '开源T20': {'energetic': 4, 'opinion': 2},
    '开源T21': {'calm': 2, 'practical': 2},
    '开源T23': {'playful': 3, 'opinion': 1},
    '开源T24': {'emotional': 2, 'calm': 2},
    '硬核解说': {'analytical': 3, 'technical': 1},
    '竖屏学习': {'practical': 3, 'growth': 2},
    '竖屏锐金': {'business': 3, 'energetic': 2},
}

# JD 竖屏模板（2026-11-05 批次）按题材给亲和度，避免和 26 套 MS 混排后按目录顺序乱推。
JD_AFFINITY = {
    'jd-pain-solution': {'practical': 3, 'business': 2},
    'jd-price-anchor': {'business': 4},
    'jd-before-after': {'practical': 3, 'business': 2},
    'jd-scene-seed': {'business': 2, 'calm': 1},
    'jd-flash-sale': {'business': 4, 'energetic': 2},
    'jd-concept-breakdown': {'technical': 3, 'analytical': 2},
    'jd-three-step': {'practical': 4},
    'jd-spec-decode': {'technical': 3, 'analytical': 2},
    'jd-quote-line': {'opinion': 4, 'reflective': 1},
    'jd-story-hook': {'reflective': 3, 'emotional': 2},
    'jd-beauty-glow': {'business': 3, 'energetic': 1},
    'jd-beauty-glow-34': {'business': 3, 'energetic': 1},
}
SIGNALS = {
    'business': ('经营复盘', r'创业|生意|经营|客户|现金流|利润|开店|合伙|订单|成交|营收|成本|售价|毛利|下单|优惠|折扣'),
    'growth': ('成长行动', r'成长|习惯|自律|学习|行动|复盘|改变|坚持|目标'),
    'emotional': ('情绪与关系', r'孤独|难过|失落|失眠|疲惫|崩溃|委屈|陪伴|拥抱|爱人|亲情|婚姻|分手|关系|难熬|痛苦'),
    'reflective': ('经历与反思', r'曾经|那年|后来|经历|回头|回忆|失去|终于|我才明白|从前|失败'),
    'practical': ('方法与步骤', r'首先|其次|第一|第二|第三|步骤|方法|清单|如何|怎么做|建议|技巧|注意'),
    'analytical': ('分析与对照', r'数据|比例|成本|增长率|百分之|对比|比较|原因|模型|逻辑|效率|[0-9]+[%％]'),
    'technical': ('技术与工具', r'人工智能|算法|编程|软件|代码|科技|系统|自动化|大模型|AI|工具'),
    'opinion': ('观点强调', r'本质|真相|记住|观点|底层|真正|我认为|说白了|不是.{1,18}而是'),
    'energetic': ('有力与号召', r'冲刺|出发|马上|立刻|加油|热血|突破|赢|燃|别等|一起行动|做到|！|!'),
    'playful': ('轻松与趣味', r'哈哈|好玩|有趣|搞笑|笑死|可爱|趣事|游戏|轻松|笑话'),
    'calm': ('舒缓与陪伴', r'慢慢|别急|安静|温柔|陪你|没关系|允许自己|不必|放下|平静|休息'),
}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def catalog():
    data = json.loads((KIT / 'schemas/catalog.json').read_text(encoding='utf-8'))
    data['reference_templates'] = reference_packaging.catalog()
    import rs_eva_packaging
    data['rs_eva_talking'] = rs_eva_packaging.binding()
    import jd_motion_packaging
    data['jd_motion'] = jd_motion_packaging.binding()
    return data


# ---------------------------------------------------------------- 本地包装模板（t122 暂接）
# 主理人 2026-11-17：先接本地三套；打包后由 t58 中控下发，本文件只改配置不改逻辑。
#   ms:JDXH-01      格栏口播（本机离线全链已验证，6s/180 帧/1080p30）
#   ms:MG01-现代图文 MotionGit 现代图文（20261118 t38 已下架：available=False，模板本体留归档，运行时池不加载）
#   ms:MG02-纸面透视 MotionGit 纸面透视（20261118 t38 已下架：同上）
LOCAL_TEMPLATE_META = {
    'ms:JDXH-01': {'source': 'local', 'available': True, 'offline_available': True, 'needs_authorization': False,
                   'delivery': '本机离线渲染（MS 逐帧）', 'name': 'JDXH 格栏口播',
                   'caption': '格栏底板＋人像框＋逐句字幕 · 16:9 · 本地离线'},
    'ms:MG01-现代图文': {'source': 'local', 'available': False, 'offline_available': False, 'needs_authorization': True,
                         'delivery': '已下架 20261118（t38：淘汰在线授权线，模板本体留归档，运行时池不加载）', 'name': 'MG01 现代图文',
                         'caption': '已下架 20261118：MotionGit 线属淘汰批次，运行时池不再提供'},
    'ms:MG02-纸面透视': {'source': 'local', 'available': False, 'offline_available': False, 'needs_authorization': True,
                         'delivery': '已下架 20261118（t38：淘汰在线授权线，模板本体留归档，运行时池不加载）', 'name': 'MG02 纸面透视',
                         'caption': '已下架 20261118：MotionGit 线属淘汰批次，运行时池不再提供'},
}


def packaging_source():
    """local（默认，本机模板）/ cloud（打包后走中控，t58）。只改配置不改逻辑。"""
    return (os.getenv('IP_PACKAGING_SOURCE') or 'local').strip().lower() or 'local'


def local_template_root():
    """本地模板根目录：IP_PACKAGING_LOCAL_DIR > IP_MS_ROOT > 包内 local-templates。

    20261118 MG 下架（t38）：工作区归档（_模板归档-20261117-淘汰在线授权，含 MG01/MG02）
    候选已摘除——淘汰线模板不再回流运行时池；包内 local-templates 正常兜底保持不变。
    """
    if packaging_source().startswith('cloud'):
        return None
    for key in ('IP_PACKAGING_LOCAL_DIR', 'IP_MS_ROOT'):
        raw = (os.getenv(key) or '').strip()
        if raw and Path(raw).is_dir():
            return Path(raw)
    base = ROOT.parent
    cand = base / 'local-templates'
    if cand.is_dir():
        return cand
    return None


def _activate_local_templates():
    """把本地模板根并入 eva_packaging 扫描路径（只加环境变量，幂等）。"""
    root = local_template_root()
    if root:
        os.environ.setdefault('IP_MS_ROOT', str(root))
    return root


def _annotate_local_styles(items):
    out = []
    for st in items or []:
        item = dict(st)
        meta = LOCAL_TEMPLATE_META.get(str(item.get('id') or ''))
        if meta:
            item.update({k: v for k, v in meta.items() if k not in ('name', 'caption')})
            item['name'] = meta.get('name') or item.get('name')
            item['caption'] = meta.get('caption') or item.get('caption')
            item['local'] = packaging_source().startswith('local')
        item.setdefault('source', 'local' if item.get('eva_template') or item.get('jd_template') or item.get('spatial_template') else 'cloud')
        out.append(item)
    return out


def _merge_style_meta(items):
    """老 plan 缓存里的风格元信息可能过期（caption/授权标记）；按当前 styles() 合并。"""
    fresh = {s['id']: s for s in styles()}
    out = []
    for it in items or []:
        item = dict(it)
        src = fresh.get(str(item.get('id') or ''))
        if src:
            for key in ('name', 'caption', 'note', 'needs_authorization', 'offline_available', 'source', 'local'):
                if src.get(key) is not None:
                    item[key] = src[key]
        out.append(item)
    return out


def styles():
    # 旧版 11 套 HTML 风格与 3 套参考模板已于 2026-11-05 换新下线；catalog 只保留引擎控件定义。
    # 「包装」目录里的 MS 模板包：自动登记，不硬编码清单。
    # JD 竖屏/3:4 是独立一批（12 套），与 MS 并列可选，互不替代。
    _activate_local_templates()
    items = reference_packaging.styles() + eva_packaging.styles() + jd_packaging.styles() + spatial_packaging.styles()
    return _annotate_local_styles(items)


def runtime():
    py = Path(os.getenv('IP_PACKAGING_ENGINE_PYTHON', str(ROOT.parent / 'tools/python/python.exe')))
    node = Path(os.getenv('IP_PACKAGING_NODE', str(ROOT.parent / 'tools/node/bin/node.exe')))
    # 浏览器统一走包内 chromium 优先（买家机器可能没装 Chrome/Edge）。
    chrome = eva_packaging._chrome_exe()
    gaps = [label for label, ok in [('包装引擎', (KIT / 'engine/adapter.py').is_file()),
        ('模板字体', (KIT / 'engine/assets/fonts/NotoSansSC.ttf').is_file()),
        ('Python', py.is_file()), ('Node', node.is_file()), ('本地浏览器', bool(chrome)),
        ('Playwright', (KIT / 'node_modules/playwright-core/package.json').is_file()),
        ('FFmpeg', bool(media.binary('ffmpeg'))), ('FFprobe', bool(media.binary('ffprobe')))] if not ok]
    return {'ready': not gaps, 'missing': gaps, 'python': str(py), 'node': str(node), 'chrome': chrome}


def style_gaps(style):
    """单个包装风格的缺依赖清单（与 render() 的检查一一对应）。"""
    sid = str(style.get('id') or '')
    rt = runtime()
    if style.get('spatial_template'):
        return spatial_packaging.capabilities()['missing'] + list(rt['missing'])
    if style.get('jd_template') or sid.startswith('jd:'):
        return [] if jd_packaging.available() else list(jd_packaging.missing())
    if style.get('eva_template') or sid.startswith('ms:'):
        return [] if eva_packaging.available() else list(eva_packaging.missing())
    return list(rt['missing']) if not rt['ready'] else []


def annotate_styles(items):
    """复制风格条目并加 available/missing（不污染常量表）。"""
    out = []
    for st in items or []:
        item = dict(st)
        gaps = style_gaps(st)
        item['available'] = not gaps
        item['missing'] = gaps
        out.append(item)
    return out


def capabilities():
    rt = runtime()
    music = music_library.status()
    return {'smart_packaging': rt['ready'], 'packaging_missing': rt['missing'],
            'packaging_styles': len(styles()) if (KIT / 'schemas/catalog.json').is_file() else 0,
        'reference_packaging_templates': [s['id'] for s in reference_packaging.styles()],
        'jd_packaging': jd_packaging.available(), 'jd_packaging_missing': jd_packaging.missing(),
        'jd_templates': [s['id'] for s in jd_packaging.styles()],
        'spatial_packaging': spatial_packaging.capabilities()['ready'],
        'spatial_packaging_missing': spatial_packaging.capabilities()['missing'],
        'packaging_music_tracks': music['count'],
        'packaging_music_dir': music['folder'],
        'packaging_music_hint': music['hint']}


def validate_clean(project, artifact):
    if artifact.get('project_id') != project['id'] or artifact.get('kind') != 'clean_video':
        raise ValueError('智能包装需要本作品的清理底片。')
    if project.get('mode')=='direct':
        # 2026-11-17（t114）：直剪没有定稿版本，clean 的 script_version_id 为 None；
        # 只要底片属于本作品且时间轴映射完整即可排包装。
        if artifact.get('script_version_id'):
            raise Conflict('直剪底片不应绑定讲稿版本，请重新生成直剪。')
    elif not project.get('confirmed_version_id') or project.get('draft_changed') or artifact.get('script_version_id') != project.get('confirmed_version_id'):
        raise Conflict('清理与当前定稿不一致，请先确认稿件并完成清理。')
    if not artifact.get('mapping') or not artifact.get('asset_id'):
        raise ValueError('清理缺少时间轴映射，请重新生成清理。')
    if not 5000 <= artifact.get('info', {}).get('duration_ms', 0) <= 600000:
        raise ValueError('智能包装当前支持 5 秒至 10 分钟的清理视频。')


def cut_cues(transcript, mapping, duration_ms):
    """Use retained words when available; never reintroduce deleted words via a cue.

    Cue-only sources must have whole retained cues; partial cues cannot be guessed.
    """
    words = transcript.get('words') or []
    source = words if words else transcript.get('cues', [])
    timed = []
    for c in source:
        a, b = c.get('start_ms', 0), c.get('end_ms', 0)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or not math.isfinite(a + b) or b <= a:
            continue
        mid = (a + b) / 2
        for seg in mapping:
            # 词级转写用中点判存活：横跨切点的词只要中点落在被删区间就不显示。
            # 只有句级转写时才退回整句包含，避免把删掉的半句猜回来。
            if words and not seg['start_ms'] <= mid < seg['end_ms']:
                continue
            start, end = max(a, seg['start_ms']), min(b, seg['end_ms'])
            if not words and end - start < (b - a) * 0.999:
                continue
            x = max(0, round(seg['output_start_ms'] + start - seg['start_ms']))
            y = min(duration_ms, round(seg['output_start_ms'] + end - seg['start_ms']))
            if y > x and str(c.get('text', '')).strip():
                timed.append({'start_ms': x, 'end_ms': y, 'text': str(c['text']).strip()})
            break
    timed.sort(key=lambda c: (c['start_ms'], c['end_ms']))
    if not timed:
        raise ValueError('没有能安全映射到清理的字幕，请先完成原片转写并核对剪切。')
    if not words and len(timed) < len([c for c in source if any(c.get('start_ms', 0) < s['end_ms'] and c.get('end_ms', 0) > s['start_ms'] for s in mapping)]):
        raise ValueError('剪切穿过了句级字幕，需要词级转写后再包装，避免把已剪掉的话显示回来。')
    cues = []
    for c in timed:
        if words and cues and c['start_ms'] - cues[-1]['end_ms'] < 420 and len(cues[-1]['text'] + c['text']) <= 24 and c['end_ms'] - cues[-1]['start_ms'] < 5000 and not re.search(r'[。！？!?；;]$', cues[-1]['text']):
            previous = cues[-1]
            joiner = ' ' if re.search(r'[A-Za-z0-9]$', previous['text']) and re.match(r'[A-Za-z0-9]', c['text']) else ''
            previous['text'] += joiner + c['text']
            previous['end_ms'] = c['end_ms']
            previous['lines'] = subtitle_segment.split_for_display(previous['text'])
        else:
            cues.append(dict(c))
    for i, c in enumerate(cues):
        c['id'] = 'pack_cue_' + str(i + 1)
    return cues


def inputs(store, project, artifact):
    validate_clean(project, artifact)
    transcript = next((t for t in store.list('transcript', 10000) if t.get('asset_id') == artifact['asset_id']), None)
    if not transcript:
        raise NoTranscript('这版底片还没转写，排不了卡片和字幕。下一步：回剪辑页点「一键字幕出片」，'
            '它会自动转写并烧好字幕；完成后回到本页再排卡片。若转写失败，设置里可把语音识别通道切到 auto/cloud。')
    cues = cut_cues(transcript, artifact['mapping'], artifact['info']['duration_ms'])
    correction = store.get('packaging_captions', artifact['id'])
    if correction and correction.get('base_cue_digest') == digest(cues):
        cues = correction['cues']
    profile = store.get('profile', 'owner') or {}
    # Title/identity are weak context only. Emotion is read from the actual spoken words.
    context = {'title': project.get('title', ''), 'tags': project.get('tags', []),
               'persona': {k: profile.get(k, '') for k in ('name', 'account', 'style', 'industry')}}
    fingerprint = digest({'version': VERSION, 'artifact': artifact['id'], 'path': artifact.get('path'),
        'script': artifact['script_version_id'], 'info': artifact['info'], 'mapping': artifact['mapping'],
        'cues': cues, 'context': context, 'catalog': catalog(),
        'template_content': (store.get('packaging_content', artifact['id']) or {}).get('by_style', {})})
    return cues, context, fingerprint


def match_styles(cues, duration_ms, context, available=None, layout=None, mood=None):
    # t26 情绪桥：mood 为导演三件套 overall 枚举（温情陪伴/燃/沉郁叙事/轻快）；
    # 缺省 None 时行为与改前完全一致（回归可证）。偏置表单一事实源在 mood_bridge.py。
    text = ''.join(c['text'] for c in cues)
    features = {key: min(1.0, len(re.findall(pattern, text, re.I)) / 3) for key, (_, pattern) in SIGNALS.items()}
    # Evidence from pacing supplements lexical signals; does not infer feelings from a title.
    rate = len(re.sub(r'[^\w\u4e00-\u9fff]', '', text)) / max(1, duration_ms / 1000)
    if rate > 4.7: features['energetic'] = min(1, features['energetic'] + .2)
    persona = context.get('persona', {}).get('style', '')
    calm_bias = .45 if re.search(r'沉稳|克制|温和|自然|简洁|直接', persona) else 0
    lively_bias = .45 if re.search(r'活泼|幽默|热情|有趣', persona) else 0
    mood_bias = {}
    if mood:
        try:
            import mood_bridge
            mood_bias = mood_bridge.MOOD_STYLE_BIAS.get(mood, {})
        except ImportError:
            mood_bias = {}
    pool = list(available if available is not None else styles())
    if available is None and layout in ('portrait', 'landscape'):
        # 画幅路由：竖屏/3:4 用竖屏模板，16:9 仍走 MS 横屏；通用 HTML 模板不挑画幅。
        want = ('9:16', '3:4') if layout == 'portrait' else ('16:9',)
        narrowed = [s for s in pool if not s.get('fixed_aspect') or s.get('fixed_aspect') in want]
        if narrowed:
            pool = narrowed
    ranked = []
    for style in [s for s in pool if not s.get('manual_only')]:
        sid = style['id']
        if sid.startswith('ms:'):
            weights = MS_AFFINITY.get(sid[3:], {})
        elif sid.startswith('jd:'):
            weights = JD_AFFINITY.get(sid[3:], {})
        else:
            weights = MS_AFFINITY.get(sid, {})
        score = sum(features[k] * w for k, w in weights.items())
        if sid == 'ms:开源T21': score += .6  # 极简干净：线索少时的清晰默认，明确标注低置信
        if sid in ('ms:开源T04', 'ms:开源T12', 'ms:开源T21', 'ms:开源T24', 'ms:竖屏学习'): score += calm_bias
        if sid in ('ms:开源T01', 'ms:开源T07', 'ms:开源T16', 'ms:开源T20', 'ms:开源T23', 'ms:像素游戏'): score += lively_bias
        if sid in mood_bias: score += mood_bias[sid]
        evidence = sorted([(features[k] * w, SIGNALS[k][0]) for k, w in weights.items() if w > 0 and features[k] > 0], reverse=True)
        reason = '、'.join(label for _, label in evidence[:2]) or '表达线索较少，先用清晰易读的版式'
        if sid in mood_bias: reason += '；导演情绪「%s」偏置' % mood
        ranked.append({**style, 'score': round(score, 3), 'reason': reason})
    ranked.sort(key=lambda x: x['score'], reverse=True)
    strongest = sorted([(v, SIGNALS[k][0]) for k, v in features.items() if v > 0], reverse=True)
    gap = ranked[0]['score'] - ranked[1]['score'] if len(ranked) > 1 else 0
    return ranked, {'signals': [name for _, name in strongest[:3]], 'speech_rate': round(rate, 2),
        'confidence': '较明确' if ranked and ranked[0]['score'] >= 3 and gap >= 1 else '建议试看',
        'method': '本地多线索匹配：正文、表达节奏与个人口吻；话题不固定绑定风格'}


def _norm_style_id(sid):
    """历史方案存的是 eva: 前缀，MS 改名后归一到 ms:，保证老方案平滑继续用。"""
    if isinstance(sid, str) and sid.startswith('eva:'):
        return 'ms:' + sid[4:]
    return sid


def ensure_plan(store, project, artifact, style_id=None, content=None, expected_plan_id=None):
    style_id = _norm_style_id(style_id)
    choice = store.get('spatial_choice', project['id']) or {}
    if style_id == spatial_packaging.STYLE or (style_id is None and choice.get('active')):
        with PLAN_LOCK:
            return _ensure_spatial_plan(store, project, artifact, content, expected_plan_id)
    if style_id is not None and choice.get('active') and content is None and any(s['id']==style_id for s in styles()):
        inputs(store, project, artifact)
        store.put('spatial_choice', {'project_id':project['id'],'active':False}, key=project['id'], expected=choice.get('revision',0))
    with PLAN_LOCK:
        if content is not None:
            if not expected_plan_id: raise Conflict('请刷新包装方案后保存内容。')
            old = store.get('packaging_plan', expected_plan_id)
            if not old or old.get('project_id') != project['id'] or old.get('clean_artifact_id') != artifact['id']:
                raise Conflict('模板内容不属于当前作品或底片。')
            _, _, before = inputs(store, project, artifact)
            latest = next((p for p in store.list('packaging_plan',2000) if p.get('project_id')==project['id'] and p.get('fingerprint')==before),None)
            if old.get('fingerprint') != before or not latest or latest['id']!=old['id'] or _norm_style_id(old['selected']['id'])!=style_id:
                raise Conflict('包装方案已更新，请刷新后保存。')
            saved = store.get('packaging_content', artifact['id']) or {}
            by_style = dict(saved.get('by_style',{}))
            by_style[style_id] = reference_packaging.validate_content(style_id,content)
            store.put('packaging_content', {'project_id':project['id'],'by_style':by_style},key=artifact['id'],expected=saved.get('revision',0))
        cues, context, fingerprint = inputs(store, project, artifact)
        _brief = next((x for x in store.list('director_brief', 50) if x.get('script_version_id') == artifact.get('script_version_id')), None)
        _mood = (_brief.get('mood_arc') or {}).get('overall') if _brief else None
        layout = 'landscape' if artifact['info']['width'] > artifact['info']['height'] else 'portrait'
        ranked, analysis = match_styles(cues, artifact['info']['duration_ms'], context, layout=layout, mood=_mood)
        existing = next((p for p in store.list('packaging_plan', 2000) if p.get('project_id') == project['id'] and p.get('fingerprint') == fingerprint), None)
        if existing and style_id is None and _norm_style_id(existing['selected']['id']) in {s['id'] for s in ranked}:
            existing = dict(existing)
            existing['styles'] = annotate_styles(_merge_style_meta(existing.get('styles') or []))
            existing['recommended'] = _merge_style_meta([existing.get('recommended') or {}])[0]
            return existing
        available_ids = {s['id'] for s in annotate_styles(ranked) if s['available']}
        if style_id:
            chosen = next((s for s in ranked if s['id'] == style_id), None)
        else:
            chosen = next((s for s in ranked if s['id'] in available_ids), None) or ranked[0]
        if not chosen: raise ValueError('包装风格未登记或当前不可用。')
        recommended_pick = next((s for s in ranked if s['id'] in available_ids), None) or ranked[0]
        if existing and _norm_style_id(existing['selected']['id']) == chosen['id']:
            return existing
        return store.put('packaging_plan', {'project_id': project['id'], 'script_version_id': artifact['script_version_id'],
            'clean_artifact_id': artifact['id'], 'fingerprint': fingerprint, 'selected': chosen,
            'recommended': recommended_pick, 'alternatives': [s for s in ranked if s['id'] != chosen['id']][:2],
            'styles': annotate_styles(ranked + spatial_packaging.styles()), 'analysis': analysis, 'selection': 'manual' if style_id else 'automatic',
            'cues': cues, 'context': context, 'version': VERSION,
            'template_content': reference_packaging.content_for(store,artifact['id'],chosen['id'],cues,context) if chosen.get('reference_template') else None})


def _latest_clean(store, project, direct=None):
    """当前作品可用于包装的清理底片；直剪取最新 clean（无讲稿版本）。"""
    direct = (project.get('mode') == 'direct') if direct is None else direct
    cands = [a for a in store.list('artifact', 10000)
             if a.get('project_id') == project['id'] and a.get('kind') == 'clean_video']
    if direct:
        cands = [a for a in cands if not a.get('script_version_id')]
    else:
        cands = [a for a in cands if a.get('script_version_id') == project.get('confirmed_version_id')]
    cands.sort(key=lambda a: (a.get('created_at') or 0, a.get('id') or ''), reverse=True)
    return cands[0] if cands else None


def project_plan(store, project):
    direct = project.get('mode') == 'direct'
    clean = _latest_clean(store, project, direct)
    if not clean or project.get('draft_changed'): return {'plan': None, 'note': '清理完成后自动匹配包装'}
    try:
        return {'plan': ensure_plan(store, project, clean), 'note': ''}
    except (ValueError, Conflict) as exc:
        return {'plan': None, 'note': str(exc), 'clean_artifact_id': clean['id']}


def validate_plan(store, project, plan):
    if plan.get('selected',{}).get('spatial_template'):
        return _validate_spatial_plan(store, project, plan)
    if plan.get('project_id') != project['id']: raise KeyError('packaging_plan')
    clean = store.get('artifact', plan['clean_artifact_id'])
    if not clean: raise KeyError('artifact')
    _, _, fingerprint = inputs(store, project, clean)
    if fingerprint != plan.get('fingerprint'):
        raise Conflict('内容或个人档案已变化，请刷新包装方案后重试。')
    latest_clean = _latest_clean(store, project)
    if not latest_clean or latest_clean['id'] != clean['id']:
        raise Conflict('已有更新的清理底片，请刷新包装方案。')
    current = ensure_plan(store, project, clean)
    if current['id'] != plan['id']:
        raise Conflict('包装风格已经改变，请刷新后重试。')
    return clean


def music_dir(data_root=None):
    """用户自选配乐目录；环境变量 > 设置里保存的路径 > 旧默认位置。"""
    return music_library.folder(data_root)


def music_tracks(data_root=None):
    """已授权曲库：目录不存在或为空即视为未配置，绝不自动生成或塞入素材。"""
    return music_library.tracks(data_root)


def music_status(data_root=None):
    return music_library.status(data_root)


def music_set(folder, data_root=None):
    return music_library.set_folder(folder, data_root)


def music_pick_dialog(data_root=None):
    """弹 Windows 原生选文件夹框；用户取消时保持原设置不变。"""
    chosen = music_library.pick_folder_dialog()
    if not chosen:
        return {'ok': False, 'cancelled': True, 'message': '没有选择文件夹，原设置不变。',
                **music_library.status(data_root)}
    return music_library.set_folder(chosen, data_root)


# 断行规则照搬切片工坊 jy_bridge/product_to_draft.py 的 split_two_lines：
# 只在强语义边界切、两行均衡；找不到强边界就不拆（宁可少花不可乱花）。
_PUNCT = '，。！？；：、,.!?;:'
_TAIL_PARTICLES = '的地得着了过们啊吧呢嘛'
_WORDS = {'现在', '这个', '那个', '然后', '因为', '所以', '其实', '但是', '就是',
          '还是', '依然', '可以', '如果', '咱们', '他们', '我们', '你们', '不是',
          '没有', '而是', '而且', '接着', '那么', '这样', '这些', '那些', '只是',
          '不过', '确实', '觉得', '可能', '一定', '肯定', '包括', '比如', '另外',
          '除了', '对于', '于是', '终于', '已经', '一直', '第一', '第二', '第三',
          '第四', '第五', '它肯定', '有一种'}
_WORD_TAILS = {w[1] for w in _WORDS}


def split_two_lines(text, min_chars=8):
    t = (text or '').strip()
    n = len(t)
    if n < min_chars: return [t]
    target = n / 2.0
    lo, hi = max(2, int(target) - 4), min(n - 2, int(target) + 4)
    best, best_j = float('-inf'), None
    for j in range(lo, hi + 1):
        strong = (t[j - 1] in _PUNCT) or (t[j:j + 2] in _WORDS) or (t[j - 1] in '的了着过吧呢啊嘛')
        if not strong: continue
        if j / float(n) < .32 or j / float(n) > .68: continue
        if min(j, n - j) < 4: continue
        score = -abs(j - target) * 2.0
        if t[j - 1] in _PUNCT: score += 100
        if t[j:j + 2] in _WORDS: score += 40
        if t[j] in _WORD_TAILS: score -= 50
        if t[j] in _TAIL_PARTICLES: score -= 60
        if t[j - 1] in '的了着过吧呢啊嘛': score += 30
        if score > best: best, best_j = score, j
    if best_j is None: return [t]
    up, down = t[:best_j].strip(), t[best_j:].strip()
    return [up, down] if up and down else [t]


def _strip_punct(text):
    return re.sub('[%s]' % re.escape(_PUNCT), '', text or '').strip()


def wrap_title(title, per_line=12, max_lines=2):
    """开场卡标题最多两行（移植切片工坊 wrap_title 的断句顺序）。"""
    t = (title or '').strip()
    if not t: return []
    if len(t) <= per_line: return [t]
    seps = '-—−…·/｜、，,。:：（）()【】[] '
    cut = next((j for j in range(min(per_line + 3, len(t)), max(2, per_line - 5), -1) if t[j - 1] in seps), None) or per_line
    up = t[:cut].strip(seps)
    rest = t[cut:].lstrip(seps)
    out = [up] if up else []
    if rest: out.append(rest[:per_line + 8].strip(seps))
    return out[:max_lines]


def caption_style(cues, enabled=True):
    """双行花字 + 关键词高亮：有强边界拆两行；拆不开才用高亮，二者不叠加。"""
    text = ''.join(c['text'] for c in cues)
    hi_words = [w for w in HIGHLIGHT_WORDS if w in text]
    return {'mode': 'double' if enabled else 'single', 'hiWords': hi_words}


def decorate_captions(cues, enabled=True):
    hi_words = caption_style(cues, enabled)['hiWords']
    out = []
    for c in cues:
        item = dict(c)
        if enabled:
            lines = c.get('lines') or split_two_lines(c['text'])
            if len(lines) == 2:
                item['lines'] = [_strip_punct(line) or line for line in lines]
            else:
                item['hi'] = next((w for w in hi_words if w in c['text']), '')
        out.append(item)
    return out


def make_params(plan, video, duration_s):
    style = plan['selected']
    cue_data = [{'start': c['start_ms'] / 1000, 'end': min(c['end_ms'] / 1000, duration_s), 'text': c['text']}
                for c in plan['cues'] if c['start_ms'] / 1000 < duration_s]
    cues = [c for c in cue_data if c['end'] > c['start']]
    if not cues: raise ValueError('预览范围内没有字幕。')
    # Short clauses are copied verbatim. Cards contain only words actually spoken here.
    groups, group = [], []
    span = 11 if style['id'] == 'night-ledger-warm' else 8
    for cue in cues:
        if group and (cue['end'] - group[0]['start'] > span or len(''.join(c['text'] for c in group + [cue])) > 58):
            groups.append(group); group = []
        group.append(cue)
    if group: groups.append(group)
    segments, narration = [], []
    cursor = 0.0
    name = str(plan.get('context', {}).get('persona', {}).get('name') or '')[:20]
    for i, group in enumerate(groups):
        end = duration_s if i == len(groups) - 1 else group[-1]['end']
        text = ''.join(c['text'] for c in group)
        # Keep one readable excerpt; continuous captions carry the complete narration.
        clauses = [p.strip() for p in re.split(r'[，,。；;！!？?]', text) if p.strip()]
        excerpt = next((p for p in clauses if 5 <= len(p) <= 18), text[:18])
        control, props = 'quote', {'text': excerpt, 'who': name}
        # Split on sentence boundaries so 'first' never becomes a standalone step.
        sentences = [p.strip() for p in re.split(r'[。；;！!？?]', text) if p.strip()]
        if 2 <= len(sentences) <= 4 and all(len(p) <= 18 for p in sentences):
            if re.search(r'首先|其次|第一|第二|第三|步骤', text):
                control, props = 'steps', {'title': '', 'steps': sentences}
            elif re.search(r'清单|要点|分别|包括', text):
                control, props = 'list', {'title': '', 'items': sentences}
        segments.append({'control': control, 't0': round(cursor, 3), 't1': round(end, 3), 'props': props})
        inter = [j for j, c in enumerate(cues) if c['start'] < end and c['end'] > cursor]
        narration.append({'shot': i, 'start': round(cursor, 3), 'end': round(end, 3),
            'spokenText': text, 'purpose': 'verbatim-emphasis', 'captionIndices': inter,
            'timingSource': 'cut-transcript', 'captionBinding': 'continuous',
            'beats': [{'at': cues[j]['start'], 'until': cues[j]['end'], 'spokenText': cues[j]['text'], 'ci': j} for j in inter]})
        cursor = end
    title = str(plan.get('context', {}).get('title') or '').strip() or cues[0]['text'][:14]
    brand = name or str(plan.get('context', {}).get('persona', {}).get('account') or '').strip()
    opening = {'brand': brand, 'lines': wrap_title(title), 'subtitle': '本期看点',
               'seconds': round(min(3.0, max(1.2, duration_s * .35)), 2)} if title else None
    return {'workflow': 'dreamweaver-kit/1', 'header': {'shortTitle': plan['context']['title'][:80] or '口播'},
        'timeline': {'family': style['family'], 'mood': style['mood'], 'layout': plan['layout'],
            'duration': round(duration_s, 3), 'presenter': {'src': str(video), 'framing': 'none', 'audio': 'source'},
            'voiceover': '', 'subtitles': '', 'music': '', 'captionsData': decorate_captions(cues),
            'openingCard': opening, 'captionStyle': caption_style(cues),
            'segments': segments, 'narrationMap': narration}}


def _run(cmd, env, cwd, log, timeout=7200):
    result = subprocess.run([str(x) for x in cmd], cwd=str(cwd), env=env,
        capture_output=True, encoding='utf-8', errors='replace', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with log.open('a', encoding='utf-8') as out:
        out.write(result.stdout + '\n' + result.stderr + '\n')
    if result.returncode:
        raise ValueError('包装渲染未完成，清理底片已保留。可重试，详细原因见本机任务日志。')
    return result


def render(store, project, plan, job_id, data_root, preview, stage, bgm=False):
    rt = runtime()
    jd = bool(plan['selected'].get('jd_template'))
    spatial = bool(plan['selected'].get('spatial_template'))
    # MS 模板包走自己的运行时，不依赖 Motion 引擎那套字体/适配器；JD 是第三套渲染泵。
    if spatial:
        gaps = spatial_packaging.capabilities()['missing'] + rt['missing']
        if gaps: raise ValueError('空间包装环境缺少：'+'、'.join(gaps))
    elif jd:
        if not jd_packaging.available():
            raise ValueError('JD 包装环境缺少：' + '、'.join(jd_packaging.missing()))
    elif plan['selected'].get('eva_template'):
        if not eva_packaging.available():
            raise ValueError('MS 包装环境缺少：' + '、'.join(eva_packaging.missing()))
    elif not rt['ready']:
        raise ValueError('包装环境缺少：' + '、'.join(rt['missing']))
    stage('等待包装渲染器')
    with RENDER_LOCK:
        project = store.get('project', project['id'])
        clean = validate_plan(store, project, plan)
        if not Path(clean['path']).is_file(): raise ValueError('清理文件不存在，请重新生成清理。')
        duration = min(8, clean['info']['duration_ms'] / 1000) if preview else clean['info']['duration_ms'] / 1000
        if jd:
            # 2026-11-06：画面走「设计时间轴」、人像帧与字幕走「素材真实时间轴」，
            # 按素材时长成片，不再截到 6 秒（渲染泵见 tools/jd/render.mjs 的 designTime）。
            design = jd_packaging.clamp_duration(plan['selected']['id'], 9999)
            stage('JD 按素材时长 %.1f 秒成片（版式设计 %.1f 秒，片尾保留淡出）'
                  % (duration, design))
        work = (Path(data_root) / 'packages' / job_id).resolve()
        base = (Path(data_root) / 'packages').resolve()
        if work.parent != base or work.exists(): raise ValueError('包装任务目录无效或已存在。')
        work.mkdir(parents=True)
        env = media.runtime_environment()
        env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', CHROME_PATH=rt['chrome'],
            NODE_PATH=str(KIT / 'node_modules'))
        log = work / 'render.log'
        ffmpeg = media.binary('ffmpeg')
        video = Path(clean['path']).resolve()
        if preview and not spatial:
            stage('截取前 8 秒试看，保留清理原声')
            video = work / 'preview-source.mp4'
            _run([ffmpeg, '-v', 'error', '-n', '-i', clean['path'], '-t', duration,
                  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-c:a', 'aac', video], env, work, log)
        packed_plan = {**plan, 'layout': 'landscape' if clean['info']['width'] > clean['info']['height'] else 'portrait'}
        eva = bool(plan['selected'].get('eva_template'))
        if spatial:
            rt = dict(rt, ffmpeg=ffmpeg)
            instance, template_details = spatial_packaging.prepare(dict(plan,_spatial_preview=preview),clean,video,work,duration,rt,env,_run,log)
            params = {'timeline':{'segments':[{'control':'spatial-voice'}]}}
        elif jd:
            # JD 竖屏/3:4：逐帧驱动 JDKit timeline，音轨同样在下面合回清理原声
            stage('JD 竖屏模板 · 抽人像帧并逐帧渲染')
            silent = jd_packaging.render_silent(plan, video, duration, work, preview, stage)
            params = {'timeline': {'segments': []}}
            instance = None
            (work / 'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        elif eva:
            # MS 模板包自己出画面（逐帧重画整帧）；音轨仍由下面同一步用清理原声合回
            silent = eva_packaging.render_silent(plan, video, duration, work, preview, stage)
            params = {'timeline': {'segments': []}}
            instance = None
            (work / 'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        elif plan['selected'].get('reference_template'):
            stage('装配' + plan['selected']['name'] + ' · 本期内容与连续字幕')
            instance, template_details = reference_packaging.prepare(plan,clean,video,duration,work,preview,rt,env,_run,log)
            params = {'timeline':{'segments':[{'control':x} for x in template_details['controls']]}}
        else:
            params = make_params(packed_plan, video, duration)
            params_file = work / 'params.json'
            params_file.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding='utf-8')
            (work / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
            stage('装配' + plan['selected']['name'] + ' · 原句卡片与连续字幕')
            instance = work / 'instance'
            _run([rt['python'], KIT / 'engine/adapter.py', 'prepare', '--input', params_file, '--out', instance], env, KIT, log)
            # A smaller preview uses the same template and layout. Final output is full HD.
            settings_file = instance / 'settings.json'
            settings = json.loads(settings_file.read_text(encoding='utf-8'))
            settings['fps'] = 24 if preview else 30
            if preview:
                settings['width'], settings['height'] = ((960, 540) if packed_plan['layout'] == 'landscape' else (540, 960))
            settings_file.write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')
            (instance / 'user-input.js').write_text('window.USER_INPUT=' + json.dumps(settings, ensure_ascii=False) + ';', encoding='utf-8')
            (instance / 'config.js').write_text('window.CONFIG=' + json.dumps({'account': params['header']['shortTitle'],
                'width': settings['width'], 'height': settings['height'], 'fps': settings['fps'], 'end': duration,
                'fontChecks': ['900 40px NotoSC', '40px Anton']}, ensure_ascii=False) + ';', encoding='utf-8')
        (work / 'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
        stage(('渲染 8 秒试看 · ' if preview else '渲染完整成片 · ') + plan['selected']['name'])
        silent = work / 'visual.mp4'
        if not eva and not jd:
            _run([rt['node'], KIT / 'engine/render.mjs', '--root', instance, '--out', silent], env, KIT, log)
        output = work / ('preview.mp4' if preview else 'packaged.mp4')
        audio_note = '清理原声；未添加配乐和音效'
        mixed_from = None
        bgm_meta = None
        import rs_eva_packaging
        import jd_motion_packaging
        if rs_eva_packaging.is_style(plan['selected']):
            mixed_from,bgm_meta=rs_eva_packaging.mix(silent,video,work,duration,bgm,stage)
            audio_note=('原声＋低音量提示音；'+('BGM《'+str(bgm_meta.get('musicTitle'))+'》已做人声闪避' if bgm_meta.get('musicMixed') else '未添加BGM'))
        elif jd_motion_packaging.is_style(plan['selected']):
            mixed_from,bgm_meta=jd_motion_packaging.mix(silent,video,work,duration,bgm,stage)
            audio_note=('原声；'+('BGM《'+str(bgm_meta.get('musicTitle'))+'》已做人声闪避' if bgm_meta.get('musicMixed') else '未添加BGM'))
        if bgm and not rs_eva_packaging.is_style(plan['selected']) and not jd_motion_packaging.is_style(plan['selected']):
            # 随包曲库按情绪选曲；曲库没有可用曲目时才用用户自备目录。
            mood_src = {'title': project['title'], 'category': plan['selected'].get('name'),
                        'mood': plan['selected'].get('mood'),
                        'content': ' '.join(c['text'] for c in plan['cues'][:60])}
            # 用户在设置里明确选过配乐文件夹就用他的；否则用随包曲库按情绪选曲。
            picked = None
            if music_library.source(data_root) != '默认曲库位置':
                own = music_library.pick(plan, data_root)
                if own:
                    picked = {'path': str(own), 'title': own.stem, 'author': '用户自备',
                              'license': '用户自备曲目，请自行确认版权', 'source': str(own.parent),
                              'mood': 'own', 'mood_label': '自备', 'credit': 'BGM：%s（用户自备）' % own.stem}
            if not picked:
                picked = bgm_mix.choose(mood_src, seed=plan.get('fingerprint') or plan.get('id'))
            if not picked:
                audio_note = bgm_mix.library_status()['hint'] + '；保留清理原声'
                stage('随包曲库没有已核验配乐，本次不加配乐')
            else:
                stage('混入配乐 · ' + picked['title'] + '（' + str(picked.get('mood_label') or '') + '）')
                try:
                    mixed = work / 'packaged-with-bgm.mp4'
                    switches = [s.get('t0') for s in params.get('timeline', {}).get('segments', [])
                                if isinstance(s.get('t0'), (int, float)) and s['t0'] > 0.3]
                    bgm_meta = bgm_mix.mix(silent, video, mixed, picked, duration,
                                           switch_points=switches or None,
                                           opts={'sfx': 'auto'} if switches else None)
                    mixed_from = mixed
                    if not mixed_from.is_file(): raise ValueError('混音器未产出成片')
                    audio_note = ('已加入配乐《%s》（%s，%s BPM，卡点 %d 处），人声自动闪避；%s'
                                  % (picked['title'], picked.get('mood_label') or picked.get('mood', ''),
                                     bgm_meta.get('bpm') or '?', len(bgm_meta.get('sfx_points') or []),
                                     picked['credit']))
                except Exception:
                    mixed_from = None
                    bgm_meta = None
                    audio_note = '配乐混音失败，已回退清理原声（详情见任务日志）'
        stage('混入配乐并核对成片时长' if mixed_from else '合成清理原声并核对成片时长')
        if mixed_from:
            shutil.move(str(mixed_from), str(output))
        else:
            # 不加配乐时保持原有链路：保留清理原声，不合成音乐或无关音效。
            _run([ffmpeg, '-v', 'error', '-n', '-i', silent, '-i', video,
                  '-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                  '-af', 'apad', '-t', duration, '-movflags', '+faststart', output], env, work, log)
        info = media.probe(output)
        diff = abs(info['duration_ms'] - round(duration * 1000))
        # 2026-11-10（试用反馈 P0-4）：毫秒级差异标需复核而非直报失败；与其他剪辑链路同型。
        if diff > max(2000, round(duration * 1000) * 0.05):
            raise ValueError(f'包装时长 {info["duration_ms"]}ms 与清理 {round(duration*1000)}ms 相差 {diff}ms，超出可播放容差；结果已保留待核对：{output}')
        needs_review = diff > 300
        if clean['info'].get('audio') and not info.get('audio'):
            raise ValueError('未检测到原声音轨，不能标记成片成功。')
        subtitle = work / 'captions.srt'
        def stamp(ms):
            total = max(0, round(ms)); secs, millis = divmod(total, 1000)
            hours, secs = divmod(secs, 3600); minutes, secs = divmod(secs, 60)
            return f'{hours:02}:{minutes:02}:{secs:02},{millis:03}'
        subtitle.write_text('\n\n'.join(f"{i+1}\n{stamp(c['start_ms'])} --> {stamp(min(c['end_ms'], info['duration_ms']))}\n{c['text']}"
            for i, c in enumerate(plan['cues']) if c['start_ms'] < info['duration_ms']) + '\n', encoding='utf-8')
        recipe = {'version': VERSION, 'style': plan['selected']['id'], 'family': plan['selected']['family'],
            'mood': plan['selected']['mood'], 'plan_id': plan['id'], 'fingerprint': plan['fingerprint'],
            'source_artifact_id': clean['id'], 'preview': preview, 'duration_ms': info['duration_ms'],
            'audio': audio_note, 'bgm': bool(bgm), 'template_content': plan.get('template_content'), 'controls': [s['control'] for s in params['timeline']['segments']],
            **({'bgm_credit': bgm_meta['credit'], 'bgm_meta': bgm_meta} if bgm_meta else {})}
        if spatial:
            report_file=instance/'spatial-render-report.json'
            if not report_file.is_file(): raise ValueError('空间渲染缺少诊断报告')
            report=json.loads(report_file.read_text(encoding='utf-8'))
            recipe['jd_spatial']={'engine_binding':spatial_packaging.engine_binding(),'mode':report['mode'],'report':str(report_file),'report_sha256':spatial_packaging.file_hash(report_file)}
        (work / 'recipe.json').write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding='utf-8')
        item = store.put('artifact', {'project_id': project['id'], 'script_version_id': plan['script_version_id'],
            'asset_id': clean['asset_id'], 'kind': 'packaging_preview' if preview else 'packaged_video',
            'path': str(output), 'filename': ('包装试看-' if preview else '智能包装-') + project['title'][:30] + '.mp4',
            'info': info, 'clean_artifact_id': clean['id'], 'packaging_plan_id': plan['id'],
            'packaging_fingerprint': plan['fingerprint'], 'packaging_style': plan['selected']['id'],
            'packaging_method': VERSION, 'packaging_note': recipe['audio'],
            'caption_cues': [c for c in plan['cues'] if c['start_ms'] < info['duration_ms']],
            'recipe_path': str(work / 'recipe.json'), 'subtitle_path': str(subtitle),
            **({'bgm_credit': bgm_meta['credit']} if bgm_meta else {}),
            **({'jd_spatial':recipe['jd_spatial']} if spatial else {}),
            'needs_review': needs_review, 'duration_diff_ms': diff})
        # Do not silently replace a newer cut, changed plan or draft after slow rendering.
        stale = False
        try: validate_plan(store, store.get('project', project['id']), plan)
        except (Conflict, ValueError, KeyError): stale = True
        return {'artifact': item, 'stale': stale, 'preview': preview,
                'stage': '旧版本包装已保留，请刷新当前方案' if stale else (('试看已就绪，请播放后生成成片' if preview else '智能包装已完成，请播放审片')
                    + (f'（时长与清理相差 {diff}ms，需人工核对音画）' if needs_review else ''))}


def _ensure_spatial_plan(store, project, artifact, content=None, expected_plan_id=None):
    cues, context, base = inputs(store, project, artifact)
    choice = store.get('spatial_choice', project['id']) or {}
    saved = store.get('packaging_content', artifact['id']) or {}
    raw = saved.get('by_style', {}).get(spatial_packaging.STYLE, {})
    options = spatial_packaging.validate_options(raw, cues)
    duration = artifact['info']['duration_ms'] / 1000
    packet = spatial_packaging.normalize_input(cues, options, duration)
    previous_fp = spatial_packaging.binding_fingerprint(base, artifact, packet, options)
    if content is not None:
        old = store.get('packaging_plan', expected_plan_id) if expected_plan_id else None
        if not old or old['id'] != choice.get('plan_id') or old.get('fingerprint') != previous_fp or old.get('project_id') != project['id']:
            raise Conflict('空间方案或原句已变化，请刷新后保存。')
        options = spatial_packaging.validate_options(content, cues)
        by_style = dict(saved.get('by_style', {})); by_style[spatial_packaging.STYLE] = options
        store.put('packaging_content', {'project_id':project['id'], 'by_style':by_style}, key=artifact['id'], expected=saved.get('revision',0))
        cues, context, base = inputs(store, project, artifact)
        packet = spatial_packaging.normalize_input(cues, options, duration)
    fingerprint = spatial_packaging.binding_fingerprint(base, artifact, packet, options)
    existing = next((p for p in store.list('packaging_plan',2000) if p.get('project_id')==project['id'] and p.get('fingerprint')==fingerprint and p.get('selected',{}).get('spatial_template')), None)
    _brief = next((x for x in store.list('director_brief', 50) if x.get('script_version_id') == artifact.get('script_version_id')), None)
    _mood = (_brief.get('mood_arc') or {}).get('overall') if _brief else None
    ranked, analysis = match_styles(cues, artifact['info']['duration_ms'], context, layout='landscape' if artifact['info']['width']>artifact['info']['height'] else 'portrait', mood=_mood)
    spatial_styles = spatial_packaging.styles()
    if not spatial_styles: raise ValueError('空间模板尚未安装或核心包不可用。')
    chosen = spatial_styles[0]
    item = existing or store.put('packaging_plan', {'project_id':project['id'],'script_version_id':artifact['script_version_id'],
        'clean_artifact_id':artifact['id'],'base_fingerprint':base,'fingerprint':fingerprint,'selected':chosen,
        'recommended':ranked[0] if ranked else chosen,'alternatives':ranked[:2],'styles':ranked+spatial_styles,
        'analysis':analysis,'selection':'manual','cues':cues,'context':context,'version':VERSION,'template_content':None,
        'spatial_options':options,'spatial_input':packet,'engine_binding':spatial_packaging.engine_binding()})
    if choice.get('plan_id') != item['id'] or not choice.get('active'):
        store.put('spatial_choice', {'project_id':project['id'],'active':True,'plan_id':item['id']}, key=project['id'], expected=choice.get('revision',0))
    return item

def _validate_spatial_plan(store, project, plan):
    if plan.get('project_id')!=project['id']: raise KeyError('packaging_plan')
    clean = store.get('artifact',plan['clean_artifact_id'])
    if not clean: raise KeyError('artifact')
    cues, context, base = inputs(store,project,clean)
    options = spatial_packaging.validate_options(plan.get('spatial_options'),cues)
    packet = spatial_packaging.normalize_input(cues,options,clean['info']['duration_ms']/1000)
    if spatial_packaging.binding_fingerprint(base,clean,packet,options)!=plan['fingerprint']:
        raise Conflict('空间核心、字幕、源文件或参数已变化，请重新排版并试看。')
    latest = next((a for a in store.list('artifact',10000) if a.get('project_id')==project['id'] and a.get('kind')=='clean_video' and a.get('script_version_id')==project.get('confirmed_version_id')),None)
    choice=store.get('spatial_choice',project['id']) or {}
    if not latest or latest['id']!=clean['id'] or not choice.get('active') or choice.get('plan_id')!=plan['id']:
        raise Conflict('底片或当前包装风格已变化，请重新试看。')
    return clean
