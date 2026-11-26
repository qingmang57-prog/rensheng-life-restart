"""人生重启：MotionGit 口播适配器（MG01 现代图文 / MG02 纸面透视）。

宿主 = MotionGit 官方 assistant helper（包内 assistant/motion.mjs，1.0.0-rc.54，
sha256 已对照官网清单核验）。链路按包内 workflow contract 直译：
  start --summary → new 实例 → 写 kit params → run setup/capabilities/check-params
  → plan → prepare → render --out 成片 → 拷为 work/visual.mp4
首版编排放大：首段 title（line1=计划标题），其余句 keyword(≤10字)/quote 轮换，
t0/t1 取 ASR 句时界，不改写原文。
人像窗：presenter.src 指向去音轨代理（presenter.audio 默认取 source，去音轨 =
渲染成片无声，原声由 MS bgm_mix 单次合回，避免双声）。
首次渲染触发 MotionGit 在线授权（包内 Key，由用户在界面触发；此后信任缓存离线复用）。
"""
from pathlib import Path
import json
import shutil
import subprocess


def is_style(style):
    return str((style or {}).get('id', '')).startswith('ms:MG')


def binding(style_id=None):
    """helper 指纹进方案 fingerprint（rc.54 官网清单核验值）。"""
    if not style_id:
        return {}
    import eva_packaging as eva
    root = eva._root_of(style_id)
    if not root:
        return {}
    try:
        meta = json.loads((root / 'ms-meta.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    if meta.get('helperVersion'):
        out['template_version'] = meta['helperVersion']
    if meta.get('helperSha256'):
        out['template_digest'] = meta['helperSha256']
    return out


def local_music_status():
    import bgm_mix
    st = bgm_mix.library_status()
    return dict(local_bgm_available=bool(st.get('usable')),
                local_bgm_title='随包曲库 %d 首可用' % st.get('usable', 0))


def render_silent(plan, video, duration, work, preview, stage):
    import eva_packaging as eva
    import media
    style = plan['selected']
    root = eva._root_of(style['id'])
    if not root:
        raise ValueError('MotionGit 模板包不在了：' + style['id'] + '，请刷新包装方案。')
    try:
        meta = json.loads((root / 'ms-meta.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meta = {}
    pkg = (root / str(meta.get('package') or '')).resolve()
    if not pkg.is_file():
        raise ValueError('MotionGit 原始包装包缺失：' + str(pkg))
    node = eva._node()
    if not node:
        raise ValueError('MS 包装环境缺少 Node')
    ffmpeg = media.binary('ffmpeg')
    if not ffmpeg:
        raise ValueError('MS 包装环境缺少 FFmpeg')
    env = media.runtime_environment()
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               CHROME_PATH=eva._chrome_exe() or '')
    helper = root / 'assistant' / 'motion.mjs'
    box = Path(work) / 'mg-run'
    box.mkdir(parents=True, exist_ok=True)
    log = Path(work) / 'render.log'

    def run(args, tag):
        cmd = [str(node), str(helper)] + [str(a) for a in args] + ['--language', 'zh']
        with log.open('a', encoding='utf-8') as fh:
            fh.write('[mg] ' + tag + ' → ' + ' '.join(cmd[2:]) + '\n')
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=5400, env=env, cwd=str(box),
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        tail = ((r.stdout or '') + (r.stderr or ''))[-800:]
        with log.open('a', encoding='utf-8') as fh:
            fh.write('[mg] ' + tag + ' rc=' + str(r.returncode) + ' tail=' + tail + '\n')
        if r.returncode != 0:
            raise RuntimeError('MotionGit ' + tag + ' 失败（详见任务日志 render.log）：'
                               + tail[-400:])
        return r.stdout or ''

    lines = eva._lines_of(plan, duration)
    title = str((plan.get('context') or {}).get('title') or '').strip()[:40]
    short = title or lines[0]['speak'][:24]
    total = round(float(duration), 2)

    stage('MotionGit 校验包装包 · start --summary')
    run(['start', str(pkg), '--summary'], 'start')
    stage('建立渲染实例 · new')
    inst = box / 'instance'
    run(['new', str(pkg), str(inst)], 'new')

    stage('人像代理（去音轨）· presenter 窗')
    proxy_raw = box / 'host.mp4'
    proxy = box / 'host-mute.mp4'
    eva._host_proxy(str(video), str(proxy_raw), 360 if preview else 600,
                    ffmpeg, env, log, Path(work))
    media.run([ffmpeg, '-y', '-hide_banner', '-i', str(proxy_raw),
               '-an', '-c:v', 'copy', str(proxy)], 1200)
    if not proxy.is_file():
        raise RuntimeError('人像代理未产出')

    segs = [{'control': 'title', 't0': 0.0, 't1': min(3.0, total),
             'props': {'line1': short}}]
    for l in lines:
        a = float(l['start'])
        b = min(float(l['end']), total)
        text = str(l['speak']).strip()
        if not text or b <= a or a >= total:
            continue
        if len(text) <= 10:
            segs.append({'control': 'keyword', 't0': round(a, 2), 't1': round(b, 2),
                         'props': {'word': text}})
        else:
            segs.append({'control': 'quote', 't0': round(a, 2), 't1': round(b, 2),
                         'props': {'text': text}})
    params = {
        'workflow': 'motion-kit',
        'header': {'shortTitle': short[:80]},
        'timeline': {
            'layout': 'landscape',
            'duration': total,
            'presenter': {'src': str(proxy.resolve())},
            'voiceover': '',
            'music': '',
            'segments': segs,
            'language': 'zh',
        },
    }
    params_file = box / 'params.json'
    params_file.write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding='utf-8')

    stage('MotionGit 渲染链 · setup → capabilities → check-params → plan → prepare → render')
    run(['run', pkg, '--instance', inst, 'setup', '--workflow', 'motion-kit'], 'setup')
    run(['run', pkg, '--instance', inst, 'capabilities'], 'capabilities')
    run(['run', pkg, '--instance', inst, 'check-params', params_file,
         '--inputs', box], 'check-params')
    run(['run', pkg, '--instance', inst, 'plan', params_file,
         '--inputs', box, '--out', inst / 'plan.json'], 'plan')
    run(['run', pkg, '--instance', inst, 'prepare', '@vault',
         '--inputs', box, '--plan', inst / 'plan.json',
         '--out', inst / 'project.json'], 'prepare')
    run(['run', pkg, '--instance', inst, 'render', inst / 'project.json',
         '--out', inst / 'video.mp4'], 'render')

    produced = inst / 'video.mp4'
    if not produced.is_file() or produced.stat().st_size == 0:
        raise RuntimeError('MotionGit 未产出画面')
    target = Path(work) / 'visual.mp4'
    if target.exists():
        raise FileExistsError(str(target))
    shutil.copy2(produced, target)
    return target
