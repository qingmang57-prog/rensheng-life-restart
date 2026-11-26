"""人生重启：JD motion-kit 口播适配器（影院 16:9 / 空间 9:16）。

自包含核心拷贝 tools/jd-motion-kit（仿 rs_eva_packaging.py 模式）：
  MS cues(end_ms) → 秒 caps → motion-kit dw_adapter.run_pipeline
  导演桥接 MS providers.llm_json（继承当前模型与任务授权，不读不拷密钥）
  渲染用 MS 自带 node/chromium/ffmpeg + 拷贝的 playwright-core
  render_silent 出无声 visual.mp4；mix 由 MS bgm_mix 单次合原声+BGM（防重复叠乐）

身份：ms:jd-cinema-voice-h1 / ms:jd-spatial-voice-v1（不复用旧样式身份）。
离线夹具：环境变量 JD_MOTION_DIRECTOR_FIXTURE 指向计划 JSON 时，导演用夹具（标明），真模型另验。
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys

BASE = Path(__file__).resolve().parent.parent
KIT = BASE / 'tools' / 'jd-motion-kit'
ENGINE = KIT / 'engine'
LIB = KIT / 'motion-kit' / 'templates' / 'motion-three'
STYLES = {
    'ms:jd-cinema-voice-h1': 'jd-cinema-voice-h1',
    'ms:jd-spatial-voice-v1': 'jd-spatial-voice-v1',
}
_FOLDERS = {
    'jd-cinema-voice-h1': 'jd/horizontal/jd-cinema-voice-h1',
    'jd-spatial-voice-v1': 'jd/vertical/jd-spatial-voice-v1',
}


def is_style(style):
    return str((style or {}).get('id', '')) in STYLES


def _key(style):
    return STYLES[str(style.get('id', ''))]


def _import_engine():
    if str(ENGINE) not in sys.path:
        sys.path.insert(0, str(ENGINE))
    import dw_adapter
    import wrap_engine
    return dw_adapter, wrap_engine


def _setup_runtime(wrap_engine):
    """把 motion-kit 渲染链指向 MS 自带运行时（不默认全局 Node/cv2）。"""
    import eva_packaging as eva
    import media
    ff = Path(media.binary('ffmpeg'))
    if not ff.is_file():
        raise ValueError('MS 包装环境缺少 FFmpeg')
    wrap_engine.FFMPEG_DIR = ff.parent
    wrap_engine.FFPROBE_DIR = ff.parent
    node = eva._node()
    if not node:
        raise ValueError('MS 包装环境缺少 Node')
    wrap_engine.NODE_EXE = str(node)
    chrome = eva._chrome_exe()
    if not chrome:
        raise ValueError('MS 包装环境缺少渲染浏览器')
    os.environ['CHROME_PATH'] = chrome
    return ff


def _install_bridges(wrap_engine, caps, fixture):
    """三个桩点：转写=MS cues；导演=MS providers.llm_json（或离线夹具）；模型配置=MS 环境。"""
    wrap_engine.load_llm_cfg = lambda: {'model': os.environ.get('IP_LLM_MODEL') or 'ms-providers'}
    wrap_engine.transcribe = lambda video, work: caps

    def _llm_chat(cfg, system, user, stage='planner'):
        if fixture:
            return Path(fixture).read_text(encoding='utf-8')
        import providers
        payload = json.loads(user) if isinstance(user, str) else user
        answer = providers.llm_json(system, payload, tag='jd_motion.director')
        return json.dumps(answer, ensure_ascii=False)

    wrap_engine.llm_chat = _llm_chat


def _caps_from_plan(plan, duration):
    caps = []
    for c in plan.get('cues') or []:
        a = float(c.get('start_ms', 0)) / 1000.0
        b = min(float(c.get('end_ms', 0)) / 1000.0, float(duration))
        text = str(c.get('text') or '').strip()
        if b > a and text:
            caps.append({'i': len(caps), 'start': a, 'end': b, 'text': text})
    return caps


def render_silent(plan, video, duration, work, preview, stage):
    dw_adapter, wrap_engine = _import_engine()
    _setup_runtime(wrap_engine)
    key = _key(plan['selected'])
    caps = _caps_from_plan(plan, duration)
    if not caps:
        raise ValueError('这版底片没有可用台词，无法驱动 JD 口播包装。')
    fixture = os.environ.get('JD_MOTION_DIRECTOR_FIXTURE') or None
    _install_bridges(wrap_engine, caps, fixture)
    if fixture:
        stage('JD 口播：导演使用离线夹具（真模型另验）')
    context = plan.get('context') or {}
    title = context.get('title') or plan.get('title') or ''
    run_work = Path(work) / 'jd-motion-run'
    options = dict(style=key, quality='high', arrangement='content', illustrations='none',
                   sfx='off', bgm='off', duration=float(duration), ip_id='')
    stage('JD 口播 · motion-kit 原文编排渲染（' + key + '）')
    dw_adapter.run_pipeline(Path(video), run_work, title=title, max_images=0,
                            log=lambda m: stage(str(m)), options=options)
    # motion-kit 内部 bgm/sfx=off：motion-run/video.mp4 为纯画面帧（无声），合原声交给 mix。
    silent = run_work / 'motion-run' / 'video.mp4'
    if not silent.is_file() or silent.stat().st_size == 0:
        raise RuntimeError('JD motion-kit 未产出无声画面')
    target = Path(work) / 'visual.mp4'
    if target.exists():
        raise FileExistsError(str(target))
    shutil.copy2(silent, target)
    return target


def mix(silent, video, work, duration, bgm, stage):
    import media
    import bgm_mix
    silent, video, work = Path(silent), Path(video), Path(work)
    out = work / 'packaged-with-bgm.mp4'
    duration = float(duration)
    if bgm:
        mood_src = {'title': '', 'category': 'JD口播', 'mood': 'warm', 'content': ''}
        plan_file = work / 'plan.json'
        if plan_file.is_file():
            try:
                p = json.loads(plan_file.read_text(encoding='utf-8'))
                mood_src['title'] = (p.get('context') or {}).get('title') or p.get('title') or ''
                mood_src['content'] = ' '.join(str(c.get('text', '')) for c in (p.get('cues') or [])[:60])
            except (OSError, ValueError):
                pass
        picked = bgm_mix.choose(mood_src, seed=str(work))
        if picked:
            stage('JD 口播：原声＋BGM人声闪避（单次混音）')
            meta = bgm_mix.mix(silent, video, out, picked, duration, opts={'sfx': 'off', 'duck': True})
            meta['musicTitle'] = meta.get('title')
            meta['musicMixed'] = True
            return out, meta
        stage('随包曲库无可用配乐，保留原声')
    stage('JD 口播：原声合回（无BGM）')
    ffmpeg = media.binary('ffmpeg')
    media.run([ffmpeg, '-y', '-hide_banner', '-i', str(silent), '-i', str(video),
               '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-af', 'apad',
               '-c:a', 'aac', '-b:a', '192k', '-t', '%.2f' % duration,
               '-movflags', '+faststart', str(out)], 3600)
    if not out.is_file():
        raise ValueError('JD 口播混音未产出成片')
    return out, {'musicMixed': False, 'musicTitle': None}


def binding(style_id=None):
    """模板核心 version+digest 进入方案 fingerprint。给 style_id 返回单模板平铺，否则聚合。"""
    def one(key):
        raw = (LIB / _FOLDERS[key] / 'bundle-manifest.json').read_bytes()
        data = json.loads(raw.decode('utf-8'))
        return dict(template_version=data.get('version') or data.get('coreVersion'),
                    template_digest=hashlib.sha256(raw).hexdigest())
    if style_id is not None:
        return one(_key({'id': style_id}))
    return {key: one(key) for key in _FOLDERS}


def local_music_status():
    import bgm_mix
    st = bgm_mix.library_status()
    return dict(local_bgm_available=bool(st.get('usable')),
                local_bgm_title='随包曲库 %d 首可用' % st.get('usable', 0))