"""MS 模板包装：直接驱动「包装」目录下已做好的模板包。

不是新写法 —— 参数契约、目录约束、全 I 帧代理、音轨回填这几条全部照
切片工坊已跑通的那条链路（`切片工坊/backend/app/services/eva_service.py`）：

  1. MS 的 http 根是 `content/template/`，素材必须写进它下面、params 里用相对路径
     → 工作目录建在模板包内（`content/template/work/wb-*`）
  2. host 视频必须是全 I 帧，否则逐帧截图 seek 到上一帧、人像窗一片灰
  3. MS 只出无声片，音轨由调用方用净剪原声合回

模板包有两套在用的入参契约，按包内**实际文件**判定，不猜：

  scenes  读 `P.scenes`（14 版式数组）    硬核解说      → eva_scenes.build()
  slots   读 `_P.s1_h` 这类固定命名文案槽  竖屏学习/竖屏锐金 → eva_slots.build()

第三套 `lines`（读 `P.lines` 整句台词）原本只有「变形窗口播」在用，该模板已移出
出厂集（2026-11-06，存于包外 `_removed_templates_20261106/`）；`lines` 分支保留，
放回该模板即可恢复。

竖屏那两个的人像窗吃**30fps jpg 帧序列**（`host.dir`），不是 mp4 —— 这是它们
与 16:9 模板最大的实现差异，必须单独抽帧。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import media
import eva_scenes
import eva_slots
from db import Conflict

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
# 模板包约定名：图标目录下 `模板-*-MS`（兼容旧 `模板-*-EVA`，平滑过渡）
PACK_GLOBS = ('模板-*-MS', '模板-*-EVA')


_BASES_ANNOUNCED = False


def _bases():
    """配置优先、包内目录兜底：配置指向的路径不存在时不再阻断。"""
    global _BASES_ANNOUNCED
    out = []
    env = (os.getenv('IP_MS_ROOT', '') or os.getenv('IP_EVA_ROOT', '')).strip()
    if env:
        out.append(Path(env))
    out.append(BASE / 'tools' / 'eva')
    existing = [p for p in out if p.is_dir()]
    if not _BASES_ANNOUNCED:
        shown = ' | '.join(str(p) for p in existing) or '<none>'
        print('[eva_packaging] template bases = ' + shown)
        _BASES_ANNOUNCED = True
    return existing


def _pack_ok(p):
    return (p / 'assistant' / 'motion.mjs').is_file() and (p / 'content' / 'template' / 'index.html').is_file()


def _motion_text(p):
    try:
        return (p / 'assistant' / 'motion.mjs').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _lines_driven(p):
    """整句驱动：motion.mjs 里读 P.lines。"""
    return 'P.lines' in _motion_text(p)


def _slot_kit(p):
    """竖屏文案槽包：content/template/params.test.json 的键就是槽名。

    learn（竖屏学习）= s1_h/s2_g/s3_g1 这套 11 槽
    sharp（竖屏锐金）= s1_tag/s1_l1/s3_i1 这套 19 槽
    判据只看键名，不认目录名。
    """
    f = p / 'content' / 'template' / 'params.test.json'
    if not f.is_file():
        return ''
    try:
        keys = set(json.loads(f.read_text(encoding='utf-8')).keys())
    except (OSError, ValueError):
        return ''
    if {'s1_h', 's2_g', 's3_g1'} & keys:
        return 'learn'
    if {'s1_l1', 's1_tag', 's3_i1'} & keys:
        return 'sharp'
    return ''


def _kind(p):
    """这个包要什么入参。返回 'lines' / 'scenes' / 'slots' / ''（ms-meta 显式 kind 优先）。"""
    f = p / 'ms-meta.json'
    if f.is_file():
        try:
            k = str(json.loads(f.read_text(encoding='utf-8')).get('kind') or '')
            if k:
                return k
        except (OSError, ValueError):
            pass
    if _lines_driven(p):
        return 'lines'
    if _slot_kit(p):
        return 'slots'
    if 'P.scenes' in _motion_text(p):
        return 'scenes'
    return ''


def roots():
    """扫描 MS 模板包（主），兼容旧 EVA 目录名；按 slug 去重、MS 优先，便于平滑过渡。"""
    out = []
    for base in _bases():
        if not base.is_dir():
            continue
        if _pack_ok(base):
            if base not in out:
                out.append(base)
            continue
    seen = set()
    for base in _bases():
        if not base.is_dir():
            continue
        for pat in PACK_GLOBS:
            for p in sorted(base.glob(pat)):
                if not (p.is_dir() and _pack_ok(p)):
                    continue
                slug = _slug(p)
                if slug in seen:
                    continue
                seen.add(slug)
                out.append(p)
    return out


def _slug(root):
    n = root.name
    for suf in ('-MS', '-EVA'):
        if n.startswith('模板-') and n.endswith(suf):
            return n[3:-len(suf)]
    return re.sub(r'[^0-9a-z]+', '-', n.lower()).strip('-') or 'ms'


def _meta(root):
    m = {'slug': _slug(root), 'name': 'MS · ' + _slug(root),
         'caption': '16:9 · 本地逐帧包装', 'width': 1920, 'height': 1080}
    f = root / 'ms-meta.json'
    if not f.is_file():
        f = root / 'eva-meta.json'  # 兼容旧命名，平滑过渡
    if f.is_file():
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
            for k in ('name', 'caption', 'width', 'height', 'note', 'theme'):
                if d.get(k) is not None:
                    m[k] = d[k]
        except (OSError, ValueError):
            pass
    return m


def _node():
    env = os.getenv('IP_NODE', '')
    for cand in (env, BASE / 'tools/node/bin/node.exe'):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def _node_path():
    """playwright 所在目录：包内 tools/node → 包内 tools/smart-packaging → 环境变量。"""
    env = os.getenv('IP_MS_NODE_PATH', '') or os.getenv('IP_EVA_NODE_PATH', '')
    for cand in (BASE / 'tools' / 'node' / 'node_modules',
                 BASE / 'tools' / 'smart-packaging' / 'node_modules', env):
        if cand and (Path(cand) / 'playwright').is_dir():
            return str(cand)
    return ''


def _bundle_chrome():
    """包内 chromium：app/.runtime/browser-env/** 与 tools/chromium/**（Playwright 自带构建）。"""
    for root in (BASE / 'app' / '.runtime' / 'browser-env', BASE / 'tools' / 'chromium'):
        if not root.is_dir():
            continue
        for name in ('chrome.exe', 'headless_shell.exe'):
            for exe in sorted(root.rglob(name)):
                if exe.is_file():
                    return str(exe)
    return ''


def _chrome_exe():
    """浏览器优先级：CHROME_PATH env → 包内 chromium → 系统 Chrome → 系统 Edge。"""
    for cand in (os.getenv('IP_PACKAGING_CHROME', ''), os.getenv('CHROME_PATH', '')):
        if cand and Path(cand).is_file():
            return str(cand)
    bundled = _bundle_chrome()
    if bundled:
        return bundled
    for cand in (r'C:/Program Files/Google/Chrome/Application/chrome.exe',
                 r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
                 r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'):
        if Path(cand).is_file():
            return str(cand)
    return ''


def _root_of(style_id):
    slug = style_id.split(':', 1)[1] if ':' in style_id else style_id
    for r in roots():
        if _slug(r) == slug:
            return r
    return None


def available():
    packs = [p for p in roots() if _kind(p)]
    return bool(packs) and bool(_node()) and bool(_node_path()) and bool(media.binary('ffmpeg'))


def missing():
    gaps = []
    if not [p for p in roots() if _kind(p)]:
        gaps.append('MS 模板包')
    if not _node():
        gaps.append('Node')
    if not _node_path():
        gaps.append('Playwright')
    if not media.binary('ffmpeg'):
        gaps.append('FFmpeg')
    return gaps


def styles():
    """接进智能包装的风格列表。id 统一 `ms:<slug>`（历史 `eva:` 由 ensure_plan 归一兼容）。"""
    out = []
    if not (media.binary('ffmpeg') and _node()):
        return out
    for r in roots():
        kind = _kind(r)
        if not kind:
            continue
        m = _meta(r)
        portrait = int(m.get('width') or 1920) < int(m.get('height') or 1080)
        out.append({'id': 'ms:' + m['slug'], 'family': 'ms', 'mood': '',
                    'name': m['name'], 'caption': m['caption'],
                    'colors': {'bg': '#0c1230', 'ink': '#f4f3ee', 'accent': '#d6b26a'},
                    'eva_template': True, 'eva_kind': kind,
                    'slot_kit': _slot_kit(r) if kind == 'slots' else '',
                    'theme': m.get('theme', ''),
                    'fixed_aspect': '9:16' if portrait else '16:9',
                    'note': m.get('note', '')})
    for item in out:
        if item['id'] == 'ms:观点变形口播':
            import rs_eva_packaging
            item.update(rs_eva_packaging.binding())
            item.update(rs_eva_packaging.local_music_status())
        elif item['id'] in ('ms:jd-cinema-voice-h1', 'ms:jd-spatial-voice-v1'):
            import jd_motion_packaging
            item.update(jd_motion_packaging.binding(item['id']))
            item.update(jd_motion_packaging.local_music_status())
        elif str(item.get('id', '')).startswith('ms:MG'):
            import mg_motion_packaging
            item.update(mg_motion_packaging.binding(item['id']))
            item.update(mg_motion_packaging.local_music_status())
    return out


def _run(cmd, env, cwd, log, timeout=5400):
    result = subprocess.run([str(x) for x in cmd], cwd=str(cwd), env=env,
        capture_output=True, encoding='utf-8', errors='replace', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with log.open('a', encoding='utf-8') as out:
        out.write('$ ' + ' '.join(str(x) for x in cmd) + '\n')
        out.write((result.stdout or '') + '\n' + (result.stderr or '') + '\n')
    return result


def _host_proxy(src, dst, size, ffmpeg, env, log, work):
    """裁正方 + 缩到 size + 全 I 帧。MS 坑 1：非全 I 帧会 seek 到上一帧、人像窗冻住。"""
    size = int(size // 2 * 2)
    vf = "crop='min(iw,ih)':'min(iw,ih)',scale=%d:%d:flags=lanczos" % (size, size)
    r = _run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
              '-vf', vf, '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
              '-pix_fmt', 'yuv420p', '-g', '1', '-keyint_min', '1', '-sc_threshold', '0',
              dst], env, work, log, 1800)
    if not Path(dst).is_file():
        raise ValueError('人像代理生成失败，详细原因见任务日志。')


def _host_frames(src, out_dir, ffmpeg, env, log, work, w=540, h=960, fps=30, duration=None):
    """抽人像帧序列给竖屏模板的 `host.dir`（engine 期望 dir/f_00000.jpg，30fps）。

    竖屏模板的人像窗是逐帧图，不是视频：engine 里 seq() 直接按 t*FPS 拼文件名，
    给 mp4 会找不到文件、窗口空白。尺寸按窗口最大的 520×924 留一点余量。
    源片是横屏时按中心裁 9:16——窗口本身就是 9:16，别的比例会把人头拉变形。
    duration 给定时只抽前 N 秒（JD 模板固定 6s，长片不必抽满）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = ('fps=%d,scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d'
          % (fps, w, h, w, h))
    cut = ['-t', '%.2f' % float(duration)] if duration else []
    r = _run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src, *cut,
              '-vf', vf, '-q:v', '4', '-start_number', '0', '-threads', '1',
              str(out_dir / 'f_%05d.jpg')], env, work, log, 3600)
    got = sorted(out_dir.glob('f_*.jpg'))
    if not got:
        raise ValueError('人像帧序列抽取失败，详细原因见任务日志。')
    return len(got)


def _lines_of(plan, duration):
    lines = [{'start': round(c['start_ms'] / 1000.0, 2),
              'end': round(min(c['end_ms'] / 1000.0, duration), 2),
              'speak': str(c.get('text') or '').strip()}
             for c in plan.get('cues') or [] if c['start_ms'] / 1000.0 < duration]
    lines = [l for l in lines if l['speak'] and l['end'] > l['start']]
    if not lines:
        raise ValueError('这版底片没有可用台词，无法驱动 MS 包装。')
    return lines


def render_silent(plan, video, duration, work, preview, stage):
    """按模板包出无声画面，写到 work/visual.mp4，返回其路径。

    三个分支只差「参数怎么来」，代理/渲染/回收流程完全共用：
      lines  → 台词原句直接给模板
      scenes → eva_scenes 生成 14 版式 + 字幕条
      slots  → eva_slots 填四段文案槽 + 抽人像帧序列
    """
    style = plan['selected']
    import rs_eva_packaging
    if rs_eva_packaging.is_style(style):
        return rs_eva_packaging.render_silent(plan,video,duration,work,preview,stage)
    import jd_motion_packaging
    if jd_motion_packaging.is_style(style):
        return jd_motion_packaging.render_silent(plan,video,duration,work,preview,stage)
    import mg_motion_packaging
    if mg_motion_packaging.is_style(style):
        return mg_motion_packaging.render_silent(plan,video,duration,work,preview,stage)
    root = _root_of(style['id'])
    if not root:
        raise ValueError('这个 MS 模板包不在了：' + style['id'] + '，请刷新包装方案。')
    if not available():
        raise ValueError('MS 包装环境缺少：' + '、'.join(missing()))
    kind = _kind(root)
    if not kind:
        raise ValueError('这个模板包既不吃台词也不吃版式，暂不支持：' + root.name)
    tpl = root / 'content' / 'template'
    ffmpeg = media.binary('ffmpeg')
    node = _node()
    # 必须走 runtime_environment()：它把 TEMP/TMP/TMPDIR 指到包内 child-temp。
    # 直接继承 os.environ 会把系统 TEMP（例如系统缓存盘上的 Temp 目录）带进 Playwright，
    # 该目录一旦不可写就报 browserType.launch: EPERM: mkdtemp。
    env = media.runtime_environment()
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', NODE_PATH=_node_path())
    chrome = _chrome_exe()
    if not chrome:
        raise ValueError('包装运行环境缺失，请重新下载完整安装包。（缺少：本地浏览器）')
    # 27 个 MS 模板自带的 render.mjs 都写死了开发机 Chrome 路径；
    # 这里统一注入解析后的浏览器，模板无需改动即可在新机上跑。
    env['CHROME_PATH'] = chrome
    env['PATH'] = str(Path(str(ffmpeg)).parent) + os.pathsep + env.get('PATH', '')
    log = Path(work) / 'render.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as fh:
        fh.write('[eva] browser = ' + chrome + '\n')
    print('[eva] browser = ' + chrome, flush=True)

    stamp = time.strftime('%m%d-%H%M%S') + '-%03d' % (int(time.time() * 1000) % 1000)
    box = tpl / 'work' / ('wb-' + stamp)
    box.mkdir(parents=True, exist_ok=True)

    lines = _lines_of(plan, duration)
    title = str((plan.get('context') or {}).get('title') or '').strip()[:40]
    brand = str(((plan.get('context') or {}).get('persona') or {}).get('name') or '').strip()[:20]
    theme = str(style.get('theme') or os.getenv('IP_MS_THEME', '') or os.getenv('IP_EVA_THEME', '') or '')
    cache_key = hashlib.md5(Path(video).read_bytes()).hexdigest()[:12]

    params = {'duration': round(float(duration), 2)}
    host_file = box / 'host.mp4'

    if kind == 'slots':
        kit = _slot_kit(root) or 'learn'
        stage('按四段骨架填文案槽 · ' + ('竖屏学习' if kit == 'learn' else '竖屏锐金'))
        filled, meta = eva_slots.build(lines, duration, title or lines[0]['speak'][:24], kit)
        stage('抽人像帧序列 · 30fps 逐帧可定位')
        got = _host_frames(str(video), box / 'eva', ffmpeg, env, log, Path(work))
        params.update(filled)
        params['lines'] = lines  # 逐字稿原句下传，供 MS 开源模板做 verbatim 逐字稿级中文化
        params['host'] = {'dir': 'work/%s/eva' % box.name}
        if kit == 'sharp':
            params['brand'] = brand or '本期分享'
        params['brand'] = params.get('brand') or brand or '本期分享'
        (box / 'fill.json').write_text(json.dumps({'kit': kit, 'source': meta.get('source'),
                                                   'missing': meta.get('missing'), 'slots': filled,
                                                   'frames': got}, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
    else:
        stage('人像代理 · 全 I 帧逐帧可定位')
        _host_proxy(str(video), str(host_file), 360 if preview else 600, ffmpeg, env, log, Path(work))
        if kind == 'scenes':
            stage('生成 14 版式分镜与逐句字幕')
            scenes, captions, wipes, flashes, dur = eva_scenes.build(
                lines, duration, title or lines[0]['speak'][:24], host_file, box)
            params.update({
                'duration': dur,
                'brand': brand or '本期分享',
                'pageTotal': len(scenes),
                'timecodeStart': 0,
                'host': {'shape': 'circle', 'corner': 'bl', 'size': 340,
                         'ring': '#FFFFFF', 'src': 'work/%s/host.mp4' % box.name, 'badge': ''},
                'media': {'src': '', 'veil': 0.42, 'kenburns': 0.06},
                'chrome': {'header': True, 'corners': True, 'dots': False, 'sourceNote': ''},
                'audio': {'bgm': None, 'sfx': []},
                'wipe': wipes, 'flash': flashes,
                'scenes': scenes, 'captions': captions,
                'lines': lines, 'title': title or lines[0]['speak'][:24],
                'cache_key': cache_key,
            })
        else:
            params.update({
                'title': title or lines[0]['speak'][:24],
                'brand': brand or '本期分享',
                'host': {'shape': 'rect', 'corner': 'bl', 'size': 340, 'ring': '#FFFFFF',
                         'src': 'work/%s/host.mp4' % box.name, 'badge': ''},
                'lines': lines,
                # 抠像缓存键用源片内容哈希：代理每次重编码字节都不同，按代理哈希永不命中
                'cache_key': cache_key,
            })
    if theme:
        params['theme'] = theme
    params_file = box / 'params.json'
    params_file.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding='utf-8')

    stage('MS 逐帧渲染 · ' + str(style.get('name') or style['id']))
    silent = box / 'silent.mp4'
    if silent.exists():
        silent.unlink()
    _run([node, root / 'assistant' / 'motion.mjs', 'render', '--params', params_file,
          '--out', silent, '--scale', '1', '--dur', '%.2f' % float(duration), '--force'],
         env, root, log)
    if not silent.is_file():
        raise ValueError('MS 渲染没有产出画面，详细原因见任务日志。')

    target = Path(work) / 'visual.mp4'
    shutil.move(str(silent), str(target))
    for f in (host_file,):
        try:
            f.unlink()
        except OSError:
            pass
    return target


def probe():
    """环境自检：给前端/诊断用。"""
    packs = []
    for r in roots():
        m = _meta(r)
        packs.append({'slug': m['slug'], 'name': m['name'], 'kind': _kind(r),
                      'slot_kit': _slot_kit(r), 'root': str(r)})
    return {'ready': available(), 'missing': missing(), 'node': str(_node() or ''),
            'node_path': _node_path(), 'packs': packs}
