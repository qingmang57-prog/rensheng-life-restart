# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""service.py — 包装中台 job 运行器（P2：两个产品共用的唯一入口）
job 契约（与施工方案一致）：
  输入: {video, transcript?(缺则自动转写), title, family?, music?, presenter_label?, layout?}
  输出: {video: 成片路径, plan: 方案json, recipe: 配方记录, audio_report: 混音报告}
用法：python service.py <job.json> [--log]
配方记录（数据闭环埋点，P2.4）：家族/控件序列/音效事件/主题版本/渲染引擎版本。"""
from pathlib import Path
import argparse, json, os, shutil, subprocess, sys, time

ENGINE = Path(__file__).resolve().parent
ROOT = ENGINE.parent
BASE = ROOT.parent  # 包内 tools/
RUNTIME_ERROR = '包装运行环境缺失，请重新下载完整安装包。'


def _python_exe():
    """转写/混音统一用包内解释器；环境变量可覆盖，最后才回落当前解释器。"""
    for cand in (os.environ.get('DW_PACKAGING_PY'), str(BASE / 'python/python.exe'),
                 str(BASE / 'motion-base-python/python.exe')):
        if cand and Path(cand).is_file():
            return cand
    return sys.executable


def _ffmpeg_dir():
    """ffmpeg 目录：环境变量 → 包内 tools/ffmpeg/bin → 空（交给系统 PATH）。"""
    for cand in (os.environ.get('DW_FFMPEG'), str(BASE / 'ffmpeg/bin')):
        if cand and Path(cand).is_dir():
            return str(cand)
    return ''


FACE_PY = os.environ.get('DW_FACE_PY') or _python_exe()
MIX_PY = os.environ.get('DW_MIX_PY') or _python_exe()
FF_DIR = Path(_ffmpeg_dir()) if _ffmpeg_dir() else None


def _fail(what):
    """缺件统一走人话错误，不把文件缺失抛成裸栈。"""
    raise RuntimeError(RUNTIME_ERROR + '（缺少：' + what + '）')


def _node_exe():
    """优先级：包内 tools/node → 环境变量 → 系统 node。"""
    for cand in (BASE / 'node/bin/node.exe', BASE / 'node/node.exe'):
        if cand.is_file():
            return str(cand)
    for key in ('DW_NODE', 'IP_PACKAGING_NODE'):
        value = os.environ.get(key, '')
        if value and Path(value).is_file():
            return value
    found = shutil.which('node')
    if not found:
        _fail('Node 运行时')
    return found


def _node_path():
    """playwright-core 所在目录：包内 tools/node → 包内 tools/smart-packaging → 环境变量。"""
    for cand in (BASE / 'node/node_modules', ROOT / 'node_modules'):
        if (cand / 'playwright-core/package.json').is_file():
            return str(cand)
    for entry in (os.environ.get('NODE_PATH', '') or '').split(os.pathsep):
        if entry and (Path(entry) / 'playwright-core/package.json').is_file():
            return entry
    _fail('包装渲染库 playwright-core')


def _bundle_chrome():
    """包内 chromium：app/.runtime/browser-env/** 与 tools/chromium/**。"""
    for root in (BASE.parent / 'app/.runtime/browser-env', BASE / 'chromium'):
        if not root.is_dir():
            continue
        for name in ('chrome.exe', 'headless_shell.exe'):
            for exe in sorted(root.rglob(name)):
                if exe.is_file():
                    return str(exe)
    return ''


def _chrome_exe():
    """优先级：环境变量 → 包内 chromium → 系统 Chrome/Edge。"""
    for key in ('IP_PACKAGING_CHROME', 'CHROME_PATH'):
        value = os.environ.get(key, '')
        if value and Path(value).is_file():
            return value
    bundled = _bundle_chrome()
    if bundled:
        return bundled
    for cand in (BASE / 'chromium/chrome.exe', BASE / 'chromium/chrome-win/chrome.exe',
                 ROOT / 'chromium/chrome.exe'):
        if cand.is_file():
            return str(cand)
    for cand in (Path(r'C:/Program Files/Google/Chrome/Application/chrome.exe'),
                 Path(r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'),
                 Path(r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')):
        if cand.is_file():
            return str(cand)
    _fail('本地浏览器（Chrome/Edge）')


def sh(cmd, log):
    log('$ ' + ' '.join(str(c) for c in cmd[:3]) + ' ...')
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"步骤失败: {cmd[0]}\n{(r.stderr or r.stdout or '')[-500:]}")
    return r

def ensure_transcript(video: Path, work: Path, log):
    tp = work / 'transcript.json'
    if tp.exists(): return tp
    log('[转写] faster-whisper large-v3-turbo（包内 Python）')
    sh([_python_exe(), str(ENGINE / 'audio' / 'transcribe.py'), str(video), str(tp),
        'large-v3-turbo', '4'], log)
    return tp

def run_job(job: dict, log=print) -> dict:
    video = Path(job['video']).resolve()
    work = Path(job.get('work') or (video.parent / ('dwjob-' + video.stem))).resolve()
    if (work / 'instance').exists():            # 重跑自清理：work 全为派生品
        import shutil; shutil.rmtree(work)
        log('[重跑] 清理派生 work 目录')
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if FF_DIR:
        env['PATH'] = str(FF_DIR) + os.pathsep + env.get('PATH', '')
    env['DW_FACE_PY'] = FACE_PY
    def run(cmd, py=None):
        r = subprocess.run([str(py or sys.executable)] + [str(c) for c in cmd],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=env, cwd=str(ROOT))
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(str(c) for c in cmd[:2])} 失败:\n{r.stderr[-600:]}")
        return r
    # 1. 转写
    tp = Path(job['transcript']).resolve() if job.get('transcript') else ensure_transcript(video, work, log)
    # 2. 节拍（有配乐才需要；失败不阻断）
    beats = work / 'beats.json'
    if job.get('music'):
        try:
            run(['engine/audio/analyze_beats.py', job['music'], str(beats)])
        except Exception as e:
            log('[节拍] 失败降级无节拍: ' + str(e)[:120])
    # 3. 导演（有 params 则跳过）
    params = work / 'params.json'
    if job.get('params'):
        params = Path(job['params']).resolve()
    else:
        report = work / '导演阐述.md'
        r = run(['engine/director.py', '--transcript', tp, '--video', video,
                 '--music', job.get('music', ''), '--family', job.get('family', 'auto'),
                 '--layout', job.get('layout', 'auto'),
                 '--presenter-label', job.get('presenter_label', '主播'),
                 '--title', job.get('title', video.stem),
                 '--out', params, '--report', report], py=sys.executable)
        log('[导演] ' + r.stdout.strip().splitlines()[-1][:200])
    # 4. plan → prepare
    inst = work / 'instance'
    run(['engine/adapter.py', 'plan', '--input', params], py=sys.executable)
    log('[plan] PASS')
    run(['engine/adapter.py', 'prepare', '--input', params, '--out', inst], py=sys.executable)
    if beats.exists(): beats.replace(inst / 'beats.json')
    log('[prepare] instance 就绪')
    # 5. render（帧泵）
    out = work / 'video.mp4'
    node = _node_exe()
    env['NODE_PATH'] = _node_path()
    env['CHROME_PATH'] = _chrome_exe()
    r = subprocess.run([node, str(ENGINE / 'render.mjs'), '--root', inst, '--out', out],
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       env=env, cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError('render 失败:\n' + r.stderr[-600:])
    log('[render] 完成')
    # 6. mix
    run(['engine/audio/mix.py', '--instance', inst, '--silent-video', out], py=MIX_PY)
    final = out.with_name(out.stem + '-with-audio.mp4')
    log('[mix] ' + str(final))
    # 7. 配方记录（数据闭环埋点）
    settings = json.loads((inst / 'settings.json').read_text(encoding='utf-8'))
    events = json.loads((inst / 'runtime-events.json').read_text(encoding='utf-8')) if (inst / 'runtime-events.json').exists() else {'sfx': []}
    recipe = {'profile': 'dreamweaver.recipe/1', 'job': {'title': job.get('title'), 'family': settings.get('family'),
              'layout': settings.get('layout'), 'duration': settings.get('duration')},
              'controls': [s['control'] for s in settings.get('segments', [])],
              'sfxEvents': len(events.get('sfx', [])),
              'engine': {'catalog': 'schemas/catalog.json'},
              'time': time.strftime('%Y-%m-%d %H:%M:%S')}
    (work / 'recipe.json').write_text(json.dumps(recipe, ensure_ascii=False, indent=1), encoding='utf-8')
    result = {'video': str(final), 'plan': str(params), 'recipe': str(work / 'recipe.json'),
              'instance': str(inst), 'work': str(work)}
    # 结果落盘：runner 读文件而非解析 stdout（stdout 混 [中台] 日志会假失败，坑㉒）
    (work / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('job_json')
    a = ap.parse_args()
    job = json.loads(Path(a.job_json).read_text(encoding='utf-8'))
    print('RESULT::' + json.dumps(run_job(job, log=lambda m: print('[中台]', m)), ensure_ascii=False))

if __name__ == '__main__':
    main()
