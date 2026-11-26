"""JD 竖屏 / 3:4 模板接入（第 12 批，2026-11-05 catalog）。

和 MS（`eva_packaging.py`）的关系：两套独立渲染泵，互不替代。
  - MS 26 套：16:9 + 9:16 混合，走 `assistant/motion.mjs`
  - JD 12 套：只有 9:16（11 套）与 3:4（1 套），走 `tools/jd/render.mjs`
JD 模板的文案槽只有固定几个字段（kicker/brand/headline/note/closing/step1-3/card_count），
由 `_fields()` 从本项目的 cues 归一化后填入；字幕走 `captions` 数组逐句出现。

模板代码：`tools/jd/{catalog.json,kit,packs}`，不含 frames/audio-output/samples/thumbs/bgm。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import media
import eva_packaging

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
JD = BASE / 'tools' / 'jd'
CATALOG = JD / 'catalog.json'
RENDER = JD / 'render.mjs'

CATEGORY_CN = {'commerce': '带货', 'knowledge': '知识', 'opinion': '观点', 'story': '故事'}
COUNT_UNIT = {'commerce': '件', 'knowledge': '步', 'opinion': '句', 'story': '段'}


# ── 目录 / 环境 ────────────────────────────────────────────────────────────────
def _catalog():
    try:
        return json.loads(CATALOG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def templates():
    found = {}
    for tid, meta in (_catalog().get('templates') or {}).items():
        if not meta.get('enabled', True):
            continue
        folder = JD / str(meta.get('folder') or '')
        if (folder / 'index.html').is_file() and (folder / 'template-job.json').is_file():
            found[tid] = meta
    return found


def _job_of(tid):
    meta = templates().get(tid)
    if not meta:
        return {}
    try:
        return json.loads((JD / meta['folder'] / 'template-job.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def available():
    return bool(templates()) and bool(_node()) and bool(_node_path()) and bool(media.binary('ffmpeg')) \
        and bool(eva_packaging._chrome_exe())


def missing():
    gaps = []
    if not templates():
        gaps.append('JD 模板包')
    if not _node():
        gaps.append('Node')
    if not _node_path():
        gaps.append('playwright')
    if not media.binary('ffmpeg'):
        gaps.append('ffmpeg')
    if not eva_packaging._chrome_exe():
        gaps.append('本地浏览器')
    return gaps


def _node():
    return eva_packaging._node()


def _node_path():
    return eva_packaging._node_path()


def _root_of(style_id):
    tid = style_id.split(':', 1)[1] if ':' in style_id else style_id
    meta = templates().get(tid)
    return (JD / meta['folder']) if meta else None


# ── 对外样式列表 ──────────────────────────────────────────────────────────────
def styles():
    out = []
    for tid, meta in templates().items():
        w, h = int(meta.get('width') or 1080), int(meta.get('height') or 1920)
        cat = str(meta.get('category') or '')
        aspect = '3:4' if abs(w / h - 0.75) < 0.02 else '9:16'
        job = _job_of(tid)
        out.append({
            'id': 'jd:' + tid,
            'family': 'jd',
            'jd_template': True,
            'name': meta.get('name') or tid,
            'caption': '%d×%d · %s · %sfps' % (w, h, CATEGORY_CN.get(cat, cat or '通用'), meta.get('fps') or 60),
            'mood': '',
            'colors': {'bg': '#f6f1e7', 'ink': '#221c15', 'accent': '#b98a3c'},
            'theme': meta.get('theme') or '',
            'category': cat,
            'category_cn': CATEGORY_CN.get(cat, cat or '通用'),
            'fixed_aspect': aspect,
            'width': w,
            'height': h,
            'fps': int(meta.get('fps') or 60),
            'duration': float(job.get('durationSec') or job.get('duration') or 6.0),
            'note': meta.get('hook') or '',
        })
    return out


def clamp_duration(style_id, duration):
    """JD 模板是固定时长版式（6s），超出会把整页淡出盖住，按模板设计时长截断。"""
    meta = templates().get(style_id.split(':', 1)[1] if ':' in style_id else style_id) or {}
    job = _job_of(style_id.split(':', 1)[1] if ':' in style_id else style_id)
    design = float(job.get('durationSec') or job.get('duration') or 6.0)
    return min(float(duration), design if design > 0 else 6.0)


# ── 文案 → fields ─────────────────────────────────────────────────────────────
def _clean(text):
    text = re.sub(r'\*\*', '', str(text or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    return text.strip('，。！？、；：,.!?;: "“”')


def _cut(text, n):
    """按模板字数上限取词：优先取能放下的最后一个短句（通常是结论），
    没有短句放得下才截断加省略号，避免出现“别人穿显”这种半句话。"""
    text = _clean(text)
    n = max(1, int(n))
    if len(text) <= n:
        return text
    fits = [p for p in _clauses(text) if len(p) <= n]
    if fits:
        return fits[-1]
    return text[:max(1, n - 1)] + '…'


def _clauses(text):
    return [x.strip() for x in re.split(r'[，。！？；、,.!?;:]', _clean(text)) if x.strip()]


def _highlight(text, n):
    """只给句首短语加 **：模板里 ** 会触发强调下划线。"""
    parts = _clauses(text)
    if len(parts) < 2:
        return text
    head = parts[0]
    if 2 <= len(head) <= 6 and len(head) < len(text):
        return '**' + head + '**' + text[len(head):]
    return text


def _fields(plan, tid, duration):
    job = _job_of(tid)
    maxc = job.get('maxChars') or {}
    ctx = plan.get('context') or {}
    cues = [c for c in (plan.get('cues') or []) if str(c.get('text') or '').strip()]
    texts = [_clean(c.get('text')) for c in cues if _clean(c.get('text'))]
    if not texts:
        texts = [_clean(job.get('fields', {}).get('headline') or '这一条先说结论')]
    cat = str((templates().get(tid) or {}).get('category') or '')

    headline = _cut(texts[0], maxc.get('headline', 14))
    note_src = texts[1] if len(texts) > 1 else texts[0]
    note = _cut(note_src, maxc.get('note', 26))

    # 步骤只从中段句子取；最后一句留作 closing，避免「一整天不塌」既是第 3 步又是结尾。
    used = set(_clauses(headline)) | set(_clauses(note))
    steps = []
    for text in texts[2:-1]:
        for part in _clauses(text):
            if part not in used and part not in steps:
                steps.append(part)
        if len(steps) >= 3:
            break
    closing_src = texts[-1]
    if len(steps) < 3:
        # 模板固定三行，空着的编号行很难看：不足三条时从结尾句补，
        # 结尾句同时留作 closing（短句重复一次，好过空行）。
        parts = _clauses(closing_src)
        for part in parts:
            if part not in used and part not in steps:
                steps.append(part)
            if len(steps) >= 3:
                break
        if parts:
            closing_src = parts[-1]
    steps = steps[:3]
    while len(steps) < 3:
        steps.append('')
    steps = [_cut(s, maxc.get('step%d' % (i + 1), 16)) for i, s in enumerate(steps[:3])]

    closing = _cut(closing_src, maxc.get('closing', 12))
    persona = str((ctx.get('persona') or {}).get('name') or '').strip()
    brand = _cut(persona or '本期分享', maxc.get('brand', 14))
    kicker = _cut('JD · ' + (_job_of(tid).get('name') or tid), maxc.get('kicker', 20))
    n = len([s for s in steps if s])
    count = '%d %s' % (n, COUNT_UNIT.get(cat, '条')) if n else ''

    return {
        'kicker': kicker,
        'brand': brand,
        'headline': _highlight(headline, maxc.get('headline', 14)),
        'note': note,
        'step1': steps[0], 'step2': steps[1], 'step3': steps[2],
        'closing': closing,
        'card_count': _cut(count, maxc.get('card_count', 6)),
    }


def _captions(plan, duration, tid=''):
    maxc = (_job_of(tid) or {}).get('maxChars') or {}
    out = []
    for c in plan.get('cues') or []:
        start = float(c.get('start_ms') or 0) / 1000.0
        end = min(float(c.get('end_ms') or 0) / 1000.0, duration)
        text = _clean(c.get('text'))
        if text and end > start:
            out.append({'start': round(start, 2), 'end': round(end, 2),
                        'text': text[:int(maxc.get('caption', 22))]})
    return out


# ── 渲染 ──────────────────────────────────────────────────────────────────────
def _run(cmd, env, cwd, log, timeout=3600):
    with log.open('a', encoding='utf-8') as fh:
        fh.write('$ ' + ' '.join(str(x) for x in cmd) + '\n')
    result = subprocess.run([str(x) for x in cmd], cwd=str(cwd), env=env,
                            capture_output=True, encoding='utf-8', errors='replace',
                            timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with log.open('a', encoding='utf-8') as fh:
        fh.write((result.stdout or '') + '\n' + (result.stderr or '') + '\n')
    return result


def render_silent(plan, video, duration, work, preview, stage):
    """按 JD 模板出无声画面，写到 work/visual.mp4，返回路径。"""
    style = plan['selected']
    tid = style['id'].split(':', 1)[1]
    root = _root_of(style['id'])
    if not root:
        raise ValueError('这个 JD 模板包不在了：' + style['id'] + '，请刷新包装方案。')
    if not available():
        raise ValueError('JD 包装环境缺少：' + '、'.join(missing()))
    job0 = _job_of(tid)
    w = int((templates().get(tid) or {}).get('width') or job0.get('width') or 1080)
    h = int((templates().get(tid) or {}).get('height') or job0.get('height') or 1920)
    fps = int((templates().get(tid) or {}).get('fps') or job0.get('fps') or 60)
    # 2026-11-06（JD 按素材时长出片）：dur = 素材真实时长，成片不再被 6 秒版式截断；
    # design = 模板动画设计时长，交给 render.mjs 做「画面设计时间 / 人像字幕真实时间」双轴映射。
    dur = float(duration)
    design = clamp_duration(style['id'], 9999)
    # JDKit.loadFrames 一次性把整段人像帧读进内存：30s×24fps=720 张解码图会撑崩
    # Chromium。限制总帧数 ≤360，fps 随时长下调；kit 取帧用同一个 presenter.fps。
    host_fps = max(6, min(24, int(360 / max(dur, 1.0))))

    ffmpeg = media.binary('ffmpeg')
    node = _node()
    env = media.runtime_environment()
    # 系统 TEMP 无写权限时 Chromium 会在第 200 帧左右开始截图失败（0x5 拒绝访问），
    # 必须把 TEMP/TMP/TMPDIR 指到本次任务的包外工作目录。
    tmp = Path(work) / 'tmp'
    tmp.mkdir(parents=True, exist_ok=True)
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               NODE_PATH=_node_path(), FFMPEG=str(ffmpeg),
               TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp))
    chrome = eva_packaging._chrome_exe()
    if not chrome:
        raise ValueError('包装运行环境缺失，请重新下载完整安装包。（缺少：本地浏览器）')
    env['CHROME'] = chrome
    env['PATH'] = str(Path(str(ffmpeg)).parent) + os.pathsep + env.get('PATH', '')

    log = Path(work) / 'render.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as fh:
        fh.write('[jd] browser = ' + chrome + '\n[jd] template = ' + str(root) + '\n')

    stamp = time.strftime('%m%d-%H%M%S') + '-%03d' % (int(time.time() * 1000) % 1000)
    box = Path(work) / ('jd-' + stamp)
    shutil.copytree(root, box)
    for junk in ('frames', 'audio-output', 'samples', 'thumbs', 'assets'):
        target = box / junk
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    stage('抽人像帧序列 · %dfps · ' % host_fps + ('预览' if preview else '成片'))
    raw = box / '_raw'
    # 720×405：JDKit 一次性把整段人像帧读进内存，1080p+30fps 在 6GB 空闲以下的
    # 机器上会把 Chromium 撑崩（实测第 185 帧截图失败）。人像窗最大 880×495，
    # 720 宽放大 1.22 倍肉眼几乎无损。长片再按上面的 host_fps 限总帧数。
    got = eva_packaging._host_frames(str(video), raw, ffmpeg, env, log, Path(work),
                                     w=720, h=405, fps=host_fps, duration=dur)
    frames_dir = box / 'frames'
    frames_dir.mkdir(parents=True, exist_ok=True)
    # JDKit loadFrames 读 dir/%06d.jpg 且 start 缺省为 1（start:0 会被 || 吞掉）
    kept = 0
    for i, src in enumerate(sorted(raw.glob('f_*.jpg'))):
        if i == 0:
            continue
        shutil.move(str(src), str(frames_dir / ('%06d.jpg' % (kept + 1))))
        kept += 1
    shutil.rmtree(raw, ignore_errors=True)
    if kept < 2:
        raise ValueError('人像帧序列抽取失败，详细原因见任务日志。')

    job = dict(job0)
    job.update({
        'templateId': tid,
        'width': w, 'height': h, 'fps': fps,
        'duration': round(dur, 2), 'durationSec': round(dur, 2),
        'designSec': round(design, 2),
        'frames': int(round(dur * fps)),
        'presenter': {'dir': 'frames', 'start': 1, 'format': 'jpg', 'fit': 'cover',
                      'fps': host_fps, 'frames': kept},
        'fields': _fields(plan, tid, dur),
        'captions': _captions(plan, dur, tid),
        'sfx': [s for s in (job0.get('sfx') or []) if float(s.get('t') or 0) < dur],
    })
    (box / 'template-job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2),
                                           encoding='utf-8')

    stage('JD 逐帧渲染 · ' + str(style.get('name') or tid))
    silent = box / 'silent.mp4'
    result = _run([node, RENDER, box / 'index.html', '--out', silent,
                   '--w', w, '--h', h, '--fps', fps, '--dur', '%.2f' % dur,
                   '--design', '%.2f' % design],
                  env, box, log)
    if result.returncode != 0 or not silent.is_file() or silent.stat().st_size < 1024:
        detail = (result.stderr or result.stdout or '')[-1200:]
        raise ValueError('JD 渲染没有产出画面（退出码 %s），详细原因见任务日志。\n%s'
                         % (result.returncode, detail))

    target = Path(work) / 'visual.mp4'
    shutil.move(str(silent), str(target))
    (Path(work) / 'jd-render.json').write_text(json.dumps({
        'template': tid, 'width': w, 'height': h, 'fps': fps, 'duration': round(dur, 2),
        'design_sec': round(design, 2), 'presenter_fps': host_fps,
        'frames': job['frames'], 'presenter_frames': kept,
        'fields': job['fields'], 'captions': job['captions'],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(target)
