"""Real FFmpeg operations, using source coordinates and reversible plans."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import caption_style
import subtitle_segment


# 工具解析优先级：IP_FFMPEG/IP_FFPROBE 环境变量 > 便携包 tools/ffmpeg/bin > 系统 PATH。
PACKAGE_BIN=Path(__file__).resolve().parent.parent/'tools/ffmpeg/bin'


class MediaProcessError(RuntimeError):
    def __init__(self,stderr,stdout=''):
        self.stderr=stderr;self.stdout=stdout
        super().__init__('媒体处理失败：'+useful_error(stderr))


def useful_error(stderr):
    """给用户看的 ffmpeg 错误：去掉启动横幅（版本/编译配置 --enable-…）和空行，只留真正的报错行。

    2026-11-25 实测：extract_audio 没加 -hide_banner，失败时 stderr 最后 1400 字全是编译配置串，
    真正原因（如内存不足被中断）被盖住，客户看到一屏 --enable-nvenc 不知所措。
    """
    lines=[l.strip() for l in str(stderr or '').splitlines()]
    kept=[l for l in lines if l and 'configuration:' not in l and '--enable-' not in l
          and '--disable-' not in l and not l.startswith('ffmpeg version')
          and not l.startswith('built with') and not l.startswith('libav')]
    return ('\n'.join(kept[-8:]) if kept else (lines[-1] if lines else ''))[-600:]


# 2026-11-19（t168）跨平台：Windows 用 .exe，macOS/Linux 用无后缀可执行文件。
IS_WIN = (os.name == 'nt')
IS_MAC = (sys.platform == 'darwin')

def exe_name(name):
    """按平台给可执行文件名：Windows 加 .exe，macOS/Linux 原样。"""
    name = str(name)
    if IS_WIN and not name.lower().endswith('.exe'):
        return name + '.exe'
    return name

def binary(name):
    # 环境变量优先，但装错/换机（如从 G: 拷到 UNC）时不能让整条链路失效：
    # 指定路径不存在就继续找包内 tools/ffmpeg → 系统 PATH。
    env=os.getenv('IP_'+name.upper())
    candidates=[Path(env)] if env else []
    candidates+= [PACKAGE_BIN/exe_name(name)]
    if not IS_WIN:
        # mac 上有些构建会把二进制放 bin/ 下带版本后缀（ffmpeg-7.0），这里兜一层
        candidates+= sorted(PACKAGE_BIN.glob(name+'*')) if PACKAGE_BIN.is_dir() else []
    for candidate in candidates:
        if candidate and Path(candidate).is_file():return str(candidate)
    return shutil.which(name)


def _node_exe():
    """包内 node 优先；环境变量次之；最后才用系统 PATH（按平台解析 node/node.exe）。"""
    pkg_node = Path(__file__).resolve().parent.parent/'tools/node/bin'/exe_name('node')
    for cand in (os.getenv('IP_NODE',''), str(pkg_node)):
        if cand and Path(cand).is_file():return cand
    return shutil.which('node') or ''

def chromium_binary():
    """按平台解析 Chromium/Chrome：环境变量 > 包内 tools/chromium > 系统安装。

    包内目录结构随平台不同（Chrome 官方 zip 的三种布局），逐个候选探测，不硬编码盘符。
    """
    root = Path(__file__).resolve().parent.parent
    env = os.getenv('IP_CHROMIUM') or os.getenv('IP_CHROME')
    win = [
        root/'tools/chromium/chrome-win64/chrome.exe',
        root/'tools/chromium/chrome-win32/chrome.exe',
    ]
    mac = [
        root/'tools/chromium/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        root/'tools/chromium/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing',
        root/'tools/chromium/chrome-mac/Chromium.app/Contents/MacOS/Chromium',
        root/'tools/chromium/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium',
        Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        Path('/Applications/Chromium.app/Contents/MacOS/Chromium'),
    ]
    linux = [
        root/'tools/chromium/chrome-linux64/chrome',
        root/'tools/chromium/chrome-linux/chrome',
        Path('/usr/bin/google-chrome'), Path('/usr/bin/chromium'), Path('/usr/bin/chromium-browser'),
    ]
    # 只按当前平台顺序探测：mac 上不会误选 Windows 的 chrome.exe（反之亦然）
    cands = [Path(env)] if env else []
    cands += (win if IS_WIN else (mac if IS_MAC else linux)) + (mac if not IS_MAC and not IS_WIN else [])
    for cand in cands:
        try:
            if cand and Path(cand).is_file():return str(cand)
        except OSError:
            continue
    for name in ('google-chrome', 'chromium', 'chrome'):
        hit = shutil.which(name)
        if hit:return hit
    return ''


def runtime_environment():
    """Provide installed runtimes to child processes without changing global PATH."""
    env=os.environ.copy();folders=[]
    node=_node_exe()
    if node:folders.append(str(Path(node).parent))
    ffmpeg=binary('ffmpeg')
    if ffmpeg:folders.append(str(Path(ffmpeg).parent))
    env['PATH']=os.pathsep.join(folders+[env.get('PATH','')])
    # Playwright 会在 TEMP/TMPDIR 下建 playwright-artifacts-*；系统目录可能不可写（EPERM mkdtemp）。
    # 只在子进程 env 里改，不动系统环境：Windows 用 TEMP/TMP，mac/Linux 用 TMPDIR。
    scratch=Path(os.getenv('IP_DATA_DIR',str(Path(__file__).resolve().parent/'.runtime')))/'child-temp'
    try:
        scratch.mkdir(parents=True,exist_ok=True)
        resolved=str(scratch.resolve())
    except OSError:
        resolved=''
    if resolved:
        if IS_WIN:
            env.update(TEMP=resolved,TMP=resolved,TMPDIR=resolved)
        else:
            env.update(TMPDIR=resolved)
    return env


def run(args, timeout=900):
    kwargs={'capture_output':True,'encoding':'utf-8','errors':'replace','timeout':timeout,'env':runtime_environment()}
    if os.name=='nt':
        kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
    result=subprocess.run([str(x) for x in args],**kwargs)
    if result.returncode:
        raise MediaProcessError(result.stderr,result.stdout)
    return result


def probe(path):
    exe=binary('ffprobe')
    if not exe:
        raise ValueError('未找到 ffprobe，请配置 IP_FFPROBE。')
    obj=json.loads(run([exe,'-v','error','-show_format','-show_streams','-of','json',path],60).stdout)
    videos=[s for s in obj.get('streams',[]) if s.get('codec_type')=='video']
    if not videos:
        raise ValueError('文件不是可识别的视频。')
    v=videos[0]
    duration=float(obj['format'].get('duration',0))
    if duration<=0 or duration>1800:
        raise ValueError('本地首版支持时长 0–30 分钟的视频。')
    return {'duration_ms':round(duration*1000),'width':v['width'],'height':v['height'],
            'audio':any(s.get('codec_type')=='audio' for s in obj['streams']),
            'codec':v.get('codec_name'),'size':int(obj['format'].get('size',0))}


def silence_plan(path, info, mode='natural'):
    if not info['audio']:
        raise ValueError('素材没有音轨，无法自动检测停顿；原文件已保留。')
    minimum={'conservative':1.2,'natural':0.8,'tight':0.55}.get(mode,0.8)
    result=run([binary('ffmpeg'),'-hide_banner','-i',path,'-af',f'silencedetect=noise=-36dB:d={minimum}',
                '-vn','-f','null','-'])
    duration=info['duration_ms']/1000
    silences=[]
    start=None
    for line in result.stderr.splitlines():
        a=re.search(r'silence_start:\s*([\d.]+)',line)
        b=re.search(r'silence_end:\s*([\d.]+)',line)
        if a:
            start=float(a.group(1))
        if b and start is not None:
            silences.append((start,min(duration,float(b.group(1)))))
            start=None
    if start is not None:
        silences.append((start,duration))
    # Keep a little breath around speech. This is silence analysis, not word detection.
    removed=[(max(0,a+0.16),min(duration,b-0.16)) for a,b in silences if b-a>minimum]
    removed=[(a,b) for a,b in removed if b>a]
    keep=[]; cursor=0.0
    for a,b in removed:
        if a>cursor:
            keep.append({'start_ms':round(cursor*1000),'end_ms':round(a*1000)})
        cursor=max(cursor,b)
    if cursor<duration:
        keep.append({'start_ms':round(cursor*1000),'end_ms':round(duration*1000)})
    keep=[x for x in keep if x['end_ms']-x['start_ms']>=80]
    if not keep or sum(x['end_ms']-x['start_ms'] for x in keep)<300:
        raise ValueError('检测到的有效内容太少，请检查音量或改用手动保留区间。')
    return {'segments':keep,'removed':[{'start_ms':round(a*1000),'end_ms':round(b*1000),'reason':'silence'} for a,b in removed],
            'mode':mode,'method':'ffmpeg_silencedetect','warning':'本次只按音频静音检测；有声口误需要 ASR 和人工复核。'}


def silence_plan_compressed(path, info, mode='natural', compress_ms=400, over_ms=1500):
    """长静音压缩模式（t89-A）：超过 over_ms 的静音只留 compress_ms，其余照旧清理。

    与 silence_plan 返回同构（segments/removed/mode/method/warning），可直接替换管线输入。
    语义：长停顿是节奏资源，不该全删——留 0.4s 让听感有呼吸；短停顿仍整段清掉。
    """
    if not info['audio']:
        raise ValueError('素材没有音轨，无法自动检测停顿；原文件已保留。')
    minimum={'conservative':1.2,'natural':0.8,'tight':0.55}.get(mode,0.8)
    result=run([binary('ffmpeg'),'-hide_banner','-i',path,'-af',f'silencedetect=noise=-36dB:d={minimum}',
                '-vn','-f','null','-'])
    duration=info['duration_ms']/1000
    silences=[];start=None
    for line in result.stderr.splitlines():
        a=re.search(r'silence_start:\s*([\d.]+)',line)
        b=re.search(r'silence_end:\s*([\d.]+)',line)
        if a:start=float(a.group(1))
        if b and start is not None:
            silences.append((start,min(duration,float(b.group(1)))));start=None
    if start is not None:silences.append((start,duration))
    removed=[];compressed=0
    half=compress_ms/2000.0
    for a,b in silences:
        if b-a<=minimum:continue
        a2=min(duration,max(0,a+0.16)); b2=max(0,min(duration,b-0.16))
        if (b2-a2)*1000>=over_ms:
            # 长静音：只保留两端各 compress_ms/2（合计 0.4s），中间整段删掉
            keep_a=a2+half; keep_b=b2-half
            if keep_b>keep_a:
                removed.append((keep_a,keep_b));compressed+=1
            else:
                removed.append((a2,b2))
        else:
            removed.append((a2,b2))
    removed=[(a,b) for a,b in sorted(removed) if b>a]
    # 合并相邻/重叠
    merged=[]
    for a,b in removed:
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],b))
        else:merged.append((a,b))
    keep=[];cursor=0.0
    for a,b in merged:
        if a>cursor:keep.append({'start_ms':round(cursor*1000),'end_ms':round(a*1000)})
        cursor=max(cursor,b)
    if cursor<duration:keep.append({'start_ms':round(cursor*1000),'end_ms':round(duration*1000)})
    keep=[x for x in keep if x['end_ms']-x['start_ms']>=80]
    if not keep or sum(x['end_ms']-x['start_ms'] for x in keep)<300:
        raise ValueError('检测到的有效内容太少，请检查音量或改用手动保留区间。')
    return {'segments':keep,
            'removed':[{'start_ms':round(a*1000),'end_ms':round(b*1000),'reason':'silence'} for a,b in merged],
            'mode':mode,'method':'ffmpeg_silencedetect','compress_ms':compress_ms,
            'compressed_silences':compressed,
            'warning':'长静音压缩模式：≥%dms 的静音只保留 %dms；本次只按音频静音检测。' % (over_ms,compress_ms)}


def silence_intervals(path, minimum_s=0.05, noise_db=-36):
    """Detect even short silent gaps so cut edges can snap into pauses instead of mid-word."""
    result=run([binary('ffmpeg'),'-hide_banner','-i',path,'-af',f'silencedetect=noise={noise_db}dB:d={minimum_s}',
                '-vn','-f','null','-'])
    intervals=[];start=None
    for line in result.stderr.splitlines():
        a=re.search(r'silence_start:\s*([\d.]+)',line)
        b=re.search(r'silence_end:\s*([\d.]+)',line)
        if a:start=float(a.group(1))
        if b and start is not None:
            intervals.append((round(start*1000),round(float(b.group(1))*1000)));start=None
    return intervals


def snap_edges_to_silence(silences, removed, window_ms=250, extend_ms=200):
    """auto-editor-style margin: extend each removal edge into an adjacent silent gap so the cut
    lands in a pause, never clipping word onsets/offsets or leaving audible breath at the seam."""
    out=[]
    for seg in removed:
        s,e=int(seg['start_ms']),int(seg['end_ms'])
        for a,b in silences:
            if b<=s and s-b<=window_ms:
                s=max(a,s-extend_ms);break
        for a,b in silences:
            if a>=e and a-e<=window_ms:
                e=min(b,e+extend_ms);break
        if e>s:out.append({**seg,'start_ms':s,'end_ms':e})
    return out


def combine_removed(duration_ms, removed):
    """Merge removal intervals from several cleaners (semantic + silence) into one source keep plan."""
    clean=[]
    for seg in removed or []:
        a,b=float(seg['start_ms']),float(seg['end_ms'])
        if not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a or a>=duration_ms:
            raise ValueError('删除区间超出源文件，未执行。')
        clean.append({'start_ms':round(a),'end_ms':round(min(b,duration_ms)),'reason':str(seg.get('reason',''))[:120]})
    clean.sort(key=lambda x:x['start_ms'])
    merged=[]
    for seg in clean:
        if merged and seg['start_ms']<=merged[-1]['end_ms']:
            merged[-1]['end_ms']=max(merged[-1]['end_ms'],seg['end_ms'])
            merged[-1]['reason']=merged[-1]['reason']+'+'+seg['reason'] if seg['reason'] not in merged[-1]['reason'] else merged[-1]['reason']
        else:merged.append(dict(seg))
    keep=[];cursor=0
    for seg in merged:
        if seg['start_ms']>cursor:keep.append({'start_ms':cursor,'end_ms':seg['start_ms']})
        cursor=seg['end_ms']
    if cursor<duration_ms:keep.append({'start_ms':cursor,'end_ms':duration_ms})
    keep=[x for x in keep if x['end_ms']-x['start_ms']>=100]  # auto-editor min-clip 0.1s: absorb tiny fragments
    if not keep or sum(x['end_ms']-x['start_ms'] for x in keep)<300:
        raise ValueError('删除后保留内容太少，请减少删除区间；原始素材未改动。')
    if len(keep)>400:
        raise ValueError('删除区间太碎，请合并删除建议后重试。')
    return keep,merged


def validate_segments(segments, duration_ms):
    if not isinstance(segments,list) or not 1<=len(segments)<=100:
        raise ValueError('需保留 1–100 个片段。')
    clean=[]
    for seg in segments:
        a,b=float(seg['start_ms']),float(seg['end_ms'])
        if not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a or b>duration_ms+50:
            raise ValueError('片段起止超出源文件，未执行。')
        clean.append({'start_ms':round(a),'end_ms':round(min(b,duration_ms))})
    return clean


def complement_segments(segments, duration_ms):
    """保留区间取补，得到剪掉的区间。用于把一版清理方案还原成"删了哪些地方"。"""
    ok=[]
    for seg in segments or []:
        a,b=int(seg['start_ms']),int(seg['end_ms'])
        if b>a:ok.append({'start_ms':a,'end_ms':b})
    ok.sort(key=lambda x:x['start_ms'])
    out=[];cursor=0
    for seg in ok:
        if seg['start_ms']>cursor:out.append({'start_ms':cursor,'end_ms':seg['start_ms']})
        cursor=max(cursor,seg['end_ms'])
    if cursor<duration_ms:out.append({'start_ms':cursor,'end_ms':duration_ms})
    return out


def subtract_ranges(ranges, holes):
    """ranges 挖掉 holes，返回剩余区间。"""
    merged=[]
    for hue in sorted(({'start_ms':int(h['start_ms']),'end_ms':int(h['end_ms'])} for h in holes or []),
                      key=lambda x:x['start_ms']):
        if merged and hue['start_ms']<=merged[-1]['end_ms']:
            merged[-1]['end_ms']=max(hue['end_ms'],merged[-1]['end_ms'])
        else:merged.append(hue)
    out=[]
    for r in ranges or []:
        a,b=int(r['start_ms']),int(r['end_ms'])
        cur=a
        for hue in merged:
            if hue['end_ms']<=cur:continue
            if hue['start_ms']>=b:break
            if hue['start_ms']>cur:out.append({'start_ms':cur,'end_ms':hue['start_ms']})
            cur=max(cur,hue['end_ms'])
            if cur>=b:break
        if cur<b:out.append({'start_ms':cur,'end_ms':b})
    return [r for r in out if r['end_ms']-r['start_ms']>0]


def deleted_word_ids(words, segments, duration_ms):
    """反查：这一版保留计划里，哪些词被整词剪掉了。

    微调面板要一进来就显示"哪些话已经被剪了"，靠这个把毫秒区间映射回词。
    判定用重叠过半，避免把只被切掉尾巴的词误标成已删。
    """
    removed=complement_segments(segments,duration_ms)
    hits=set()
    for w in words or []:
        a,b=int(w['start_ms']),int(w['end_ms'])
        span=max(1,b-a)
        for r in removed:
            overlap=min(b,r['end_ms'])-max(a,r['start_ms'])
            if overlap>=span*0.5:hits.add(w['id']);break
    return hits, removed


_CUT_PAD_MS = 500          # 剪点前后 cushion：吸收 Paraformer CIF 时间戳误差（切片工坊 HEAD/TAIL_PAD_SECONDS 同源）
_MODULO_MS = 300000        # 词时间戳 mod 300s 回绕（切片工坊 _realign_modulo_words 同源）


def realign_modulo_words(words, duration_ms, cues=None):
    """词时间戳回绕修复：出现向后跳（< 上一词尾 -1s）时按 300s 整数倍前移，恢复正常单调。

    返回 (fixed_words, dropped)；越界/仍不单调的词丢弃（宁缺勿错，切片工坊同口径）。
    """
    fixed = []
    shift = 0
    prev_end = 0
    dropped = 0
    for w in words or []:
        item = dict(w)
        try:
            start = int(item.get('start_ms', 0)) + shift
            end = int(item.get('end_ms', 0)) + shift
        except (TypeError, ValueError):
            dropped += 1
            continue
        guard = 0
        while start < prev_end - 1000 and guard < 20:
            shift += _MODULO_MS
            start += _MODULO_MS
            end += _MODULO_MS
            guard += 1
        if start < 0 or end <= start or start > duration_ms + 1000:
            dropped += 1
            continue
        item['start_ms'] = start
        item['end_ms'] = min(end, max(duration_ms, start + 1))
        prev_end = item['end_ms']
        fixed.append(item)
    # 2026-11-17（t70）：行跨距 ±3s 校验（照抄 asr_service.py:149-173）；不过就放弃修复。
    rows = _cue_rows(cues)
    if rows and shift:
        bad = 0
        for item in fixed:
            mid = (item['start_ms'] + item['end_ms']) // 2
            if not any(st - 3000 <= mid <= en + 3000 for st, en in rows):
                bad += 1
        if bad > max(1, len(fixed) // 10):
            return [dict(w) for w in words], (dropped + len(fixed))
    return fixed, dropped


def pad_cut_ranges(ranges, duration_ms, pad_ms=_CUT_PAD_MS):
    """[已废弃，仅兼容] 旧实现给「剪掉区间」加 ±500ms——方向与切片工坊相反（会吃掉保留词）。

    2026-11-17（t70 P0-a）：改用 boundary_plan（只吸/扩保留段，不缩保留内容）。保留此函数
    仅供旧调用方过渡，新代码不要再用。
    """
    out = []
    for r in ranges or []:
        start = max(0, int(r['start_ms']) - pad_ms)
        end = min(duration_ms, int(r['end_ms']) + pad_ms)
        if end <= start:
            continue
        out.append({'start_ms': start, 'end_ms': end})
    out.sort(key=lambda x: x['start_ms'])
    merged = []
    for seg in out:
        if merged and seg['start_ms'] <= merged[-1]['end_ms']:
            merged[-1]['end_ms'] = max(merged[-1]['end_ms'], seg['end_ms'])
        else:
            merged.append(dict(seg))
    return merged


# ── 2026-11-17（t70 P0-a）：统一边界函数（照抄切片工坊 slice_service.py:1889-1974 方向）──
_SNAP_TOL_MS = 350          # 吸附容差 0.35s
_SNAP_MAX_EXPAND_MS = 2000  # 单侧最多外扩 2s
_KEEP_PAD_MS = 500          # 保留段 head/tail pad 0.5s（recover，不是多剪）
_KEEP_FLOOR_MS = 50         # 保留词 −50ms 硬限位：绝不侵入上一行/保留词 50ms 内
_MIN_KEEP_MS = 80           # 保留段短于此并入删除（静音/残响碎片，非语音）


def _cue_rows(cues):
    rows = []
    for c in cues or []:
        try:
            s = int(c.get('start_ms') or 0)
            e = int(c.get('end_ms') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if e > s:
            rows.append((s, e))
    return sorted(rows)


def snap_keep_segments(keep, cues, tol_ms=_SNAP_TOL_MS, max_expand_ms=_SNAP_MAX_EXPAND_MS):
    """保留段吸附字幕行界：容差 0.35s；超容差最多外扩 2s；**只吸/扩不缩**（ns<=s, ne>=e）。"""
    rows = _cue_rows(cues)
    if not keep:
        return [], {'snapped': 0, 'delta_ms': 0, 'skip': 'no-keep'}
    if not rows:
        return list(keep), {'snapped': 0, 'delta_ms': 0, 'skip': 'no-cues'}

    def _pick(t, is_start):
        for st, en in rows:
            if st <= t <= en:
                return st, en
        best, key = None, None
        for st, en in rows:
            k = abs(st - t) if is_start else abs(en - t)
            if key is None or k < key:
                key, best = k, (st, en)
        return best

    out, delta = [], 0
    for s, e in keep:
        line_s = (_pick(s, True) or (None, None))[0]
        if line_s is None or abs(s - line_s) > max_expand_ms:
            ns = s
        elif abs(s - line_s) <= tol_ms:
            ns = line_s
        else:
            ns = min(s, line_s)
        line_e = (_pick(e, False) or (None, None))[1]
        if line_e is None or abs(e - line_e) > max_expand_ms:
            ne = e
        elif abs(e - line_e) <= tol_ms:
            ne = line_e
        else:
            ne = max(e, line_e)
        ns, ne = min(ns, s), max(ne, e)   # 只吸/扩不缩
        delta += (s - ns) + (ne - e)
        out.append((max(0, int(ns)), int(ne)))
    return out, {'snapped': len(out), 'delta_ms': delta, 'skip': ''}


def pad_keep_segments(keep, cues, duration_ms, pad_ms=_KEEP_PAD_MS, floor_ms=_KEEP_FLOOR_MS):
    """保留段 head/tail pad：向前/后各最多 0.5s，硬限位=相邻行行尾+50ms，绝不侵入相邻行声音。"""
    rows = _cue_rows(cues)
    out = []
    for s, e in keep:
        # 只往「行间静音」里扩：边界落在字幕行内部（语音）时一律不 pad，避免把删掉的词又捞回来。
        in_speech_head = any(st < s < en for st, en in rows)
        in_speech_tail = any(st < e < en for st, en in rows)
        head_room = 0 if in_speech_head else s
        if rows and not in_speech_head:
            prev_ends = [en for _st, en in rows if en <= s]
            if prev_ends:
                head_room = s - max(prev_ends) - floor_ms
        tail_room = duration_ms - e
        if rows and not in_speech_tail:
            starts = [st for st, _en in rows if st >= e]
            if starts:
                tail_room = min(starts) - e - floor_ms
        head = max(0, min(pad_ms, head_room))
        tail = max(0, min(pad_ms, tail_room))
        out.append((max(0, int(s - head)), min(int(duration_ms), int(e + tail))))
    return out


def _merge_pairs(pairs):
    merged = []
    for s, e in sorted(pairs):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def boundary_plan(words, removed, duration_ms, cues=None,
                  snap_tol_ms=_SNAP_TOL_MS, max_expand_ms=_SNAP_MAX_EXPAND_MS,
                  pad_ms=_KEEP_PAD_MS):
    """统一边界函数（P0-a）：输入剪掉区间，输出「吸附+受限 pad 后」的剪掉/保留区间。

    与旧 pad_cut_ranges 相反：**只吸/扩保留段、绝不缩保留内容**；保留词硬限位 −50ms。
    返回 (removed, keep, meta)；meta['skip'] 非空表示「对齐不确定，不应自动删」。
    """
    dur = max(1, int(duration_ms or 0))
    removed = _merge_pairs([(int(r['start_ms']), int(r['end_ms'])) for r in (removed or [])])
    keep = []
    cursor = 0
    for s, e in removed:
        if s > cursor:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < dur:
        keep.append((cursor, dur))
    # 碎片保留段（<80ms）并入删除：静音/残响碎片不是语音
    cut = [p for p in keep if (p[1] - p[0]) < _MIN_KEEP_MS]
    if cut:
        keep = [p for p in keep if (p[1] - p[0]) >= _MIN_KEEP_MS]
    keep0 = list(keep)
    keep, snap_meta = snap_keep_segments(keep, cues, snap_tol_ms, max_expand_ms)
    keep = [(int(s), int(e)) for s, e in keep]
    keep = _merge_pairs(keep)
    padded = pad_keep_segments(keep, cues, dur, pad_ms) if snap_meta['skip'] == '' else keep
    padded = _merge_pairs([(max(0, s), min(dur, e)) for s, e in padded])
    # 吸附/pad 不得跨过原剪点（否则会把要删的词又捞回来）：夹回原 gap。
    clamped = []
    for seg_s, seg_e in padded:
        for gs, ge in keep0:
            s2, e2 = max(seg_s, gs), min(seg_e, ge)
            if e2 > s2:
                clamped.append((s2, e2))
    padded = _merge_pairs(clamped)
    # 保留词 −50ms 硬限位：任何保留段不得把「保留词」切掉；越界只扩不缩
    word_rows = []
    for w in words or []:
        try:
            ws, we = int(w.get('start_ms')), int(w.get('end_ms'))
        except (TypeError, ValueError):
            continue
        if we <= ws:
            continue
        # 只保护「保留词」：整词落在剪掉区间里的词不再把保留段拉回去
        if any(ws >= a and we <= b for a, b in removed):
            continue
        word_rows.append((ws, we))
    if word_rows:
        fixed = []
        for s, e in padded:
            # 2026-11-17（t86 实测修复）：只有「边界落在某个保留词内部」才扩到词界；
            # 旧实现把落在段内部的词也当越界，会把整段拖到别的词上（接缝缺陷 304 处）。
            for ws, we in word_rows:
                if ws < s < we:
                    s = ws
                if ws < e < we:
                    e = we
            fixed.append((s, e))
        padded = _merge_pairs(fixed)
    removed_out = []
    cursor = 0
    for s, e in padded:
        if s > cursor:
            removed_out.append({'start_ms': cursor, 'end_ms': s})
        cursor = max(cursor, e)
    if cursor < dur:
        removed_out.append({'start_ms': cursor, 'end_ms': dur})
    keep_out = [{'start_ms': s, 'end_ms': e} for s, e in padded]
    meta = {'snap_tol_ms': snap_tol_ms, 'max_expand_ms': max_expand_ms, 'pad_ms': pad_ms,
            'snapped': snap_meta.get('snapped'), 'snap_delta_ms': snap_meta.get('delta_ms'),
            'slivers_merged': len(cut), 'skip': snap_meta.get('skip') or ''}
    return removed_out, keep_out, meta


def word_cut_ranges(words, deleted_ids, duration_ms, base_removed=None, cues=None, snap=True):
    """把「要删的词」算成 (剪掉区间, 保留区间)。

    base_removed 是上一版清理方案原本剪掉的区间。手动微调必须以它为基底：
    否则「先一键剪好、再手动改一处」会把剪掉停顿和口误和停顿清理剪掉的内容全部还原。

    规则：落在某个词上的删除算「词删除」，点一下就能恢复；
    落在词与词之间的删除是空隙，不属于任何词，永远保持剪掉。
    """
    deleted_ids=[k for k in (deleted_ids or []) if isinstance(k,str)]
    # 2026-11-17（t60）：先修 mod-300s 回绕（词时间戳回绕会让剪点落到完全错误的位置）；
    # 再在函数末对剪掉区间加 0.5s head/tail cushion。
    words=[w for w in (words or []) if isinstance(w,dict)]
    words,dropped=realign_modulo_words(words,duration_ms,cues)
    lookup={w['id']:w for w in words}
    if any(key not in lookup for key in deleted_ids):raise ValueError('所选词不属于这份转写。')
    spans=[{'start_ms':int(w['start_ms']),'end_ms':int(w['end_ms'])} for w in words or []]
    gaps=[]
    if base_removed:
        gaps=subtract_ranges([{'start_ms':int(r['start_ms']),'end_ms':int(r['end_ms'])} for r in base_removed],spans)
    picked=[lookup[k] for k in set(deleted_ids)]
    removed=[]
    for w in picked:
        a,b=int(w['start_ms']),int(w['end_ms'])
        if b<=a or a<0 or b>duration_ms+50:raise ValueError('词时间戳超出源文件，未执行。')
        removed.append({'start_ms':a,'end_ms':min(b,duration_ms)})
    for r in gaps:
        if r['end_ms']>r['start_ms']:removed.append({'start_ms':r['start_ms'],'end_ms':min(r['end_ms'],duration_ms)})
    removed.sort(key=lambda x:x['start_ms'])
    merged=[]
    for seg in removed:
        if merged and seg['start_ms']<=merged[-1]['end_ms']:
            merged[-1]['end_ms']=max(seg['end_ms'],merged[-1]['end_ms'])
        else:merged.append(dict(seg))
    # 2026-11-17（t70 P0-a）：统一边界函数——只吸/扩保留段、不缩保留内容；
    # 保留词 −50ms 硬限位；无 cue 对齐时（cues=None）不加 pad，宁少删不切坏。
    plan_removed,plan_keep,meta=boundary_plan(words,merged,duration_ms,cues if snap else None)
    merged=[{'start_ms':seg['start_ms'],'end_ms':seg['end_ms']} for seg in plan_removed]
    keep=[{'start_ms':seg['start_ms'],'end_ms':seg['end_ms']} for seg in plan_keep]
    if sum(seg['end_ms']-seg['start_ms'] for seg in keep)<300:
        raise ValueError('保留内容太短，请重新核对所选词。')
    if len(keep)>400:
        raise ValueError('剪点太碎（超过 400 段），请少删几处再试。')
    return merged, keep


def remove_word_segments(words, deleted_ids, duration_ms, base_removed=None):
    return word_cut_ranges(words,deleted_ids,duration_ms,base_removed)[1]


def render_cut(path, info, segments, output):
    segments=validate_segments(segments,info['duration_ms'])
    try:threads=max(1,min(16,int(os.getenv('IP_FFMPEG_THREADS','4'))))
    except ValueError:raise ValueError('IP_FFMPEG_THREADS 必须是整数。') from None
    filters=[]; inputs=[]; cursor=0; mapping=[]
    for i,seg in enumerate(segments):
        a,b=seg['start_ms']/1000,seg['end_ms']/1000
        filters.append(f'[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}]')
        if info['audio']:
            filters.append(f'[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]')
            inputs.append(f'[v{i}][a{i}]')
        else:
            inputs.append(f'[v{i}]')
        size=seg['end_ms']-seg['start_ms']
        mapping.append({**seg,'output_start_ms':cursor,'output_end_ms':cursor+size});cursor+=size
    filters.append(''.join(inputs)+f'concat=n={len(segments)}:v=1:a={1 if info["audio"] else 0}[v]'+('[a]' if info['audio'] else ''))
    args=[binary('ffmpeg'),'-y','-hide_banner','-threads',str(threads),'-i',path,'-filter_complex_threads','2','-filter_complex',';'.join(filters),'-map','[v]']
    if info['audio']:
        args+=['-map','[a]','-c:a','aac','-b:a','128k']
    args+=['-c:v','libx264','-threads',str(threads),'-preset','veryfast','-crf','22','-pix_fmt','yuv420p','-movflags','+faststart',output]
    run(args)
    return mapping


def render_rebuild(path, info, plan, output, fps=60, max_width=1920, fade=0.010):
    """成片渲染：按段变速（setpts+atempo）+ 微淡化 + 拼接，只出干声，不做响度。

    响度必须单独一步做（normalize_loudness），因为静音占比/最长空隙只能在
    干声态测量——压缩器会把停顿抬到阈值以上，交付态测出的数字是假象。

    plan: [{'start_ms','end_ms','speed'}]
    """
    if not isinstance(plan,list) or not 1<=len(plan)<=100:
        raise ValueError('成片段落需为 1–100 段。')
    clean=[]
    for seg in plan:
        a,b=float(seg['start_ms']),float(seg['end_ms'])
        sp=float(seg.get('speed',1.0))
        if not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a or b>info['duration_ms']+50:
            raise ValueError('成片段落起止超出源文件，未执行。')
        if not (1.0<=sp<=1.35):
            raise ValueError(f'变速 {sp} 超出 1.00–1.35 允许范围。')
        clean.append({'start_ms':round(a),'end_ms':round(b),'speed':round(sp,4)})

    # 输出尺寸：不放大、不裁人脸，长边上限 max_width
    w,h=info['width'],info['height']
    scale=max(1,min(1.0,max_width/max(w,h)))
    out_w=max(2,round(w*scale/2)*2);out_h=max(2,round(h*scale/2)*2)

    filters=[];inputs=[];mapping=[];cursor=0.0
    for i,seg in enumerate(clean):
        a,b,sp=seg['start_ms']/1000,seg['end_ms']/1000,seg['speed']
        out_s=(b-a)/sp
        out_edge=max(0.0,out_s-fade)
        filters.append(f'[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{sp:.4f},'
                       f'scale={out_w}:{out_h}:flags=lanczos,setsar=1,format=yuv420p,fps={fps}[v{i}]')
        if info['audio']:
            filters.append(f'[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,atempo={sp:.4f},'
                           f'afade=t=in:st=0:d={fade:.3f},afade=t=out:st={out_edge:.3f}:d={fade:.3f}[a{i}]')
            inputs.append(f'[v{i}][a{i}]')
        else:
            inputs.append(f'[v{i}]')
        mapping.append({**seg,'output_start_ms':round(cursor*1000),'output_end_ms':round((cursor+out_s)*1000)})
        cursor+=out_s
    filters.append(''.join(inputs)+f'concat=n={len(clean)}:v=1:a={1 if info["audio"] else 0}[v]'+('[a]' if info['audio'] else ''))
    args=[binary('ffmpeg'),'-y','-hide_banner','-i',path,'-filter_complex',';'.join(filters),'-map','[v]']
    if info['audio']:
        args+=['-map','[a]','-c:a','aac','-b:a','192k']
    try:threads=max(1,min(16,int(os.getenv('IP_FFMPEG_THREADS','4'))))
    except ValueError:raise ValueError('IP_FFMPEG_THREADS 必须是整数。') from None
    args+=['-c:v','libx264','-threads',str(threads),'-preset','veryfast','-crf','20','-r',str(fps),
           '-pix_fmt','yuv420p','-movflags','+faststart',output]
    run(args,timeout=3600)
    return {'plan':mapping,'segments':len(clean),'cuts':max(0,len(clean)-1),
            'output_seconds':round(cursor,3),'size':[out_w,out_h],'fps':fps}


def loudness_measure(path):
    """EBU R128：集成响度 / 真峰值 / 动态范围。读不到返回 None，不猜。"""
    result=run([binary('ffmpeg'),'-hide_banner','-nostats','-i',path,'-vn',
                '-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'],600)
    text=result.stderr
    start=text.rfind('{')
    if start<0:raise ValueError('读不到响度数据。')
    data=json.loads(text[start:text.rfind('}')+1])
    return {'lufs':round(float(data['input_i']),1),'true_peak':round(float(data['input_tp']),1),
            'lra':round(float(data['input_lra']),1),'threshold':round(float(data['input_thresh']),2)}


def normalize_loudness(src, output, lufs=-9.5, true_peak=-0.3, lra=4.0):
    """两遍法响度归一：先测，再用实测值线性归一，最后限峰。视频流 copy，画质零损失。

    为什么不用 -14/-16：实测青芒人剪成片稳定在 -8.5~-10.1 LUFS，-14 会明显偏软。
    """
    measured=loudness_measure(src)
    gain=lufs-measured['lufs']
    if abs(gain)<0.15 and measured['lra']<=max(lra,6.0):
        shutil.copyfile(src,output)
        return {'method':'already_in_range','measured':measured,'adjusted_db':0.0,
                'final':loudness_measure(output) if os.path.exists(output) else measured}
    limit=10**(true_peak/20)
    chain=f'volume={gain:.2f}dB,alimiter=level_in=1:limit={limit:.4f}:attack=5:release=60'
    run([binary('ffmpeg'),'-y','-hide_banner','-i',src,'-c:v','copy','-af',chain,
         '-c:a','aac','-b:a','192k','-movflags','+faststart',output],1800)
    final=loudness_measure(output)
    if final['lufs']<lufs-1.0:
        # 限峰吃掉了一部分增益，补一次
        extra=lufs-final['lufs']
        run([binary('ffmpeg'),'-y','-hide_banner','-i',output,'-c:v','copy',
             '-af',f'volume={extra:.2f}dB,alimiter=level_in=1:limit={limit:.4f}:attack=5:release=60',
             '-c:a','aac','-b:a','192k','-movflags','+faststart',str(output)+'.pass2.mp4'],1800)
        os.replace(str(output)+'.pass2.mp4',output)
        final=loudness_measure(output)
    return {'method':'two_pass_gain_limit','measured':measured,'adjusted_db':round(gain,2),
            'final':final}


def silence_stats(path, floor_db=-35, minimum_s=0.05):
    """干声态的停顿体检：静音占比、最长空隙、段数。

    只在「未做响度归一」的渲染输出上测才有意义；交付态测出来的数字会被压缩器抹掉。
    """
    info=probe(path)
    total=info['duration_ms']/1000
    gaps=silence_intervals(path,minimum_s,floor_db)
    quiet=sum(b-a for a,b in gaps)/1000
    longest=max([(b-a)/1000 for a,b in gaps],default=0.0)
    return {'duration_s':round(total,2),'quiet_ratio':round(quiet/total,4) if total else None,
            'longest_gap_s':round(longest,2),'gap_count':len(gaps),
            'floor_db':floor_db,'method':'ffmpeg_silencedetect'}


def extract_audio(path, output):
    run([binary('ffmpeg'),'-y','-hide_banner','-i',path,'-vn','-ac','1','-ar','16000','-b:a','48k',output])


CAPTION_MAX_SECONDS=5.0
CAPTION_MAX_UNITS=84
CAPTION_GAP_MS=800


def _caption_units(text):
    """估算显示宽度：汉字按1、ASCII按0.5，用来卡每屏最多84字（约两行）。"""
    return sum(1 if ord(ch)>127 else .5 for ch in text)


def _caption_join(left,right):
    return ' ' if re.search(r'[A-Za-z0-9]$',left) and re.match(r'[A-Za-z0-9]',right) else ''


def cues_from_words(words,mapping):
    """保留词是字幕唯一真相：词中点落在保留区间才算留下，再从保留词重建整条字幕。

    判据用中点而不是"与区间重叠≥50%"：横跨切点的词只要中点落在被删区间就不显示，
    删掉的词不会再经字幕写回来；重建后只把保留词交给切分引擎，绝不在词中间切。

    分组改用 subtitle_segment（2026-11-14 自切片工坊搬运）：
    句末标点/真实停顿硬断，从句标点按两侧单位数断，长度按两行 36 格、单条 ≤5 秒。
    """
    kept=[]
    for word in words:
        text=str(word.get('text') or '')
        a,b=word.get('start_ms'),word.get('end_ms')
        if not text.strip() or not isinstance(a,(int,float)) or not isinstance(b,(int,float)) or b<=a:continue
        mid=(a+b)/2
        for segment in mapping:
            start,end=segment['start_ms'],segment['end_ms']
            if not start<=mid<end:continue
            out_start=segment['output_start_ms']
            out_end=segment.get('output_end_ms',out_start+(end-start))
            x=max(out_start,round(out_start+a-start));y=min(out_end,round(out_start+b-start))
            if y>x:kept.append({'text':text,'start_ms':x,'end_ms':y})
            break
    kept.sort(key=lambda w:(w['start_ms'],w['end_ms']))
    if not kept:return []
    cues=[]
    text='';marks=[]
    for i,w in enumerate(kept):
        if i:text+=_caption_join(kept[i-1]['text'],w['text'])
        marks.append({'index':len(text),'startMs':w['start_ms']})
        text+=w['text']
    line={'text':text,'startMs':kept[0]['start_ms'],'endMs':kept[-1]['end_ms'],'marks':marks}
    for piece in subtitle_segment.segment_line(line,subtitle_segment.TARGET_RULES):
        body=piece['text'].strip()
        if not body:continue
        start=round(piece['startMs']);end=round(piece['endMs'])
        if end<=start:continue
        cues.append({'id':f'caption_{len(cues)+1}','start_ms':start,'end_ms':end,'text':body,
                     'lines':subtitle_segment.split_for_display(body)})
    return cues


def caption_timeline(words,mapping):
    """Map actual source tokens through the cut plan, then group for readable captions."""
    cues=cues_from_words(words,mapping)
    if not cues:raise ValueError('没有可映射的词级字幕，未烧录猜测时间轴。请先核对转写。')
    return cues


def render_packaged(path,output,cues,title,overlays=None,style=None):
    """Fast delivery path: original voice, source framing, real captions and grounded callouts.

    style=None 保持既有行为不变；style={'font':key,'enhanced':bool} 时应用
    caption_style 注册表字体与增强字幕样式（阴影套餐/描边随字号/关键词放大，
    2026-11-02 自切片工坊移植，见 caption_style.py 出处注释）。
    """
    output=Path(output);info=probe(path);w,h=info['width'],info['height']
    width=1080 if h>w else 1920;height=round(h*width/w/2)*2
    # Cap portrait/square as well as landscape; do not crop a person's face.
    if height>1920:width=round(width*1920/height/2)*2;height=1920
    scale=min(1.4,max(0.8,float(style.get('scale',1)))) if style else 1.0
    font=max(24,round(width/40*scale));margin=max(40,round(height*.07))
    family,fontsdir=caption_style.resolve(style.get('font')) if style else ('Microsoft YaHei',None)
    enhanced=bool(style and style.get('enhanced'))
    rgb=str(style.get('color','FFFFFF')) if style else 'FFFFFF'
    if not re.fullmatch(r'[0-9A-Fa-f]{6}',rgb):rgb='FFFFFF'
    primary='&H00'+rgb[4:6]+rgb[2:4]+rgb[0:2]
    if enhanced:
        # 切片工坊 Top1+Top2：细描边(随字号) + 柔和黑阴影(70%不透明) 组合
        outline=max(caption_style.OUTLINE_MIN,round(font*caption_style.OUTLINE_RATIO))
        shadow=max(2,round(font*caption_style.SHADOW_DISTANCE_RATIO))
        back=f'&H{caption_style.SHADOW_ALPHA_HEX:02X}000000'
    else:
        outline,shadow,back=3,1,'&H90000000'
    def clock(ms):
        cs=max(0,round(ms/10));return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'
    def text(value):return str(value).replace('\\','＼').replace('{','｛').replace('}','｝').replace('\r','').replace('\n',r'\N')
    ass='[Script Info]\nScriptType: v4.00+\n'+f'PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n'
    ass+='[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
    ass+=f'Style: Caption,{family},{font},{primary},&H00FFFFFF,&H00191919,{back},-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,70,70,{margin},1\n'
    ass+=f'Style: Title,{family},{round(font*1.1)},&H0066D4EE,&H00FFFFFF,&H00191919,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,8,80,80,{margin},1\n'
    ass+=f'Style: Concept,{family},{round(font*.85)},&H0066D4EE,&H00FFFFFF,&H00191919,&H90000000,-1,0,0,0,100,100,0,0,3,2,0,8,90,90,{margin+round(font*2.5)},1\n'
    ass+='[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    def event(a,b,style_,value,raw=False):
        body=str(value) if raw else text(value)
        return f'Dialogue: 0,{clock(a)},{clock(b)},{style_},,0,0,0,,{body}\n'
    # Top3：增强模式下，被强调关键词在字幕行内放大 1.5×并着奶油黄（切片工坊预设公式 2）
    highlights={}
    if enhanced:
        hl_size=round(font*caption_style.HL_SCALE)
        for item in overlays or []:
            quote=str(item.get('quote','')).strip()
            cue=next((c for c in cues if c['id']==item.get('cue_id')),None)
            if cue and quote and quote in cue['text'] and len(quote)<=20:
                highlights[cue['id']]=(quote,hl_size)
    def caption_event(c):
        hit=highlights.get(c['id'])
        if not hit:
            # 有按词边界排好的两行就照排，避免 libass 自动换行把词劈开
            body='\\N'.join(text(line) for line in c['lines']) if c.get('lines') else text(c['text'])
            return event(c['start_ms'],min(c['end_ms'],info['duration_ms']),'Caption',body,raw=True)
        quote,hl_size=hit;i=c['text'].find(quote)
        body=(text(c['text'][:i])+f'{{\\fs{hl_size}\\c{caption_style.HL_COLOR_ASS}}}'+text(quote)
              +f'{{\\fs{font}\\c{primary}&}}'+text(c['text'][i+len(quote):]))
        return f'Dialogue: 0,{clock(c["start_ms"])},{clock(min(c["end_ms"],info["duration_ms"]))},Caption,,0,0,0,,{body}\n'
    title=str(title)[:40]
    if len(title)>28:
        middle=(len(title)+1)//2;title=title[:middle]+'\n'+title[middle:]
    ass+=event(0,min(4000,info['duration_ms']),'Title',title)
    for c in cues:ass+=caption_event(c)
    for item in overlays or []:
        cue=next((c for c in cues if c['id']==item.get('cue_id')),None)
        quote=str(item.get('quote','')).strip()
        if cue and quote and quote in cue['text'] and len(quote)<=20:
            ass+=event(cue['start_ms'],cue['end_ms'],'Concept',quote)
    subtitles=output.with_suffix('.ass');subtitles.write_text(ass,encoding='utf-8-sig')
    # cwd-independent safe path for FFmpeg's filter parser, including Windows drive colon.
    escaped=str(subtitles.resolve()).replace('\\','/').replace(':',r'\:').replace("'",r"\'").replace(',','\\,').replace('[','\\[').replace(']','\\]')
    vf=f"scale={width}:{height},setsar=1,ass=filename='{escaped}'"
    if fontsdir:
        # 非默认字体走 fontsdir 显式加载（libass 按family匹配，缺失时安静回落系统字体）
        fontsdir_esc=str(Path(fontsdir).resolve()).replace('\\','/').replace(':',r'\:').replace("'",r"\'").replace(',','\\,').replace('[','\\[').replace(']','\\]')
        vf+=f":fontsdir='{fontsdir_esc}'"
    run([binary('ffmpeg'),'-y','-threads','4','-i',path,'-vf',vf,
        '-c:v','libx264','-threads','4','-preset','veryfast','-crf','22','-r','30','-pix_fmt','yuv420p',
        '-c:a','copy','-movflags','+faststart',output],timeout=1800)
    return {'info':probe(output),'subtitle_path':str(subtitles),'method':'ffmpeg_grounded_captions',
        'note':'原声、原画面比例；字幕来自本地词级时间轴，强调仅摘自实际讲稿。'}


def finalize_render_audio(silent_video,voice_source,output):
    """Repair only a short codec tail mismatch; never invent speech or trim meaning."""
    silent_video,voice_source,output=map(Path,(silent_video,voice_source,output))
    if output.resolve() in (silent_video.resolve(),voice_source.resolve()):raise ValueError('混音输出不能覆盖输入文件。')
    info=probe(silent_video);duration=info['duration_ms']/1000
    wav=output.with_suffix('.audio-clock.wav')
    run([binary('ffmpeg'),'-y','-i',voice_source,'-vn','-ac','1','-ar','48000','-c:a','pcm_s16le',wav])
    with wave.open(str(wav),'rb') as f:audio_seconds=f.getnframes()/f.getframerate()
    gap=duration-audio_seconds
    if gap<-.1 or gap>.25:
        raise ValueError(f'音画时差 {gap:.3f}s 超过短尾修复范围，保留中间文件，需人工核对。')
    run([binary('ffmpeg'),'-y','-i',silent_video,'-i',voice_source,'-map','0:v:0','-map','1:a:0',
         '-c:v','copy','-af',f'apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS',
         '-c:a','aac','-b:a','128k','-t',f'{duration:.6f}','-movflags','+faststart',output])
    actual=probe(output)
    if not actual['audio'] or abs(actual['duration_ms']-info['duration_ms'])>100:raise ValueError('修复后音画时长校验未通过。')
    return {'method':'original_voice_short_tail_pad','voice_seconds':round(audio_seconds,6),
        'video_seconds':duration,'tail_padding_ms':round(max(0,gap)*1000,3),'video_reencoded':False,
        'note':'只修复不足250ms的编码尾差，保留原声内容；未生成语音或字幕。','output_info':actual}


def director(parts):
    plans=[]
    for p in parts:
        text=p.get('text','')
        role=p.get('role','content')
        visual='talking_head'; reason='优先让观众看见本人表达。'
        if role=='quote':
            visual='quote_emphasis';reason='值得被转发的一句话做短时关键词强调，随后回到真人。'
        elif role=='content' and re.search(r'步骤|第一|第二|先.*再',text):
            visual='steps';reason='内容包含先后动作，建议步骤卡帮助理解。'
        elif re.search(r'相比|对比|区别|不是.*而是',text):
            visual='comparison';reason='内容包含比较关系，先确认比较条件，再做对比卡。'
        elif role=='content' and re.search(r'因为|所以|导致|因果',text):
            visual='cause_chain';reason='内容解释因果，建议用已有论证画简短关系链。'
        plans.append({'block_id':p['id'],'role':role,'visual':visual,'reason':reason,
                      'overlay':text[:28], 'source':'rule_suggestion','needs_review':True})
    return plans
