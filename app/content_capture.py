# -*- coding: utf-8 -*-
"""吃文件出文案：本地音/视频 → 转写（音轨）或画字幕识别（OCR）。

只做最短路径：给一个文件路径，返回 {text, source, segments, confidence_note}。
不生成成片、不进包装流程。

识别优先级：
1) 音轨转写：providers.asr（auto=本地优先、失败自动切云端），复用现有链路；
2) 画面字幕：语音为空/极短（<20 字）或显式要求时抽帧 OCR；
   - 优先用包内离线 RapidOCR（onnxruntime 推理，模型随包，不联网、不依赖系统语言包），
   - 其次用 Windows 自带 OCR（Windows.Media.Ocr，需中文语言包），
   - 都不可用时明确提示改走语音转写。

安全边界：只读用户给的本地文件；临时音频/帧落在 <IP_DATA_DIR>/capture_tmp，用完即删；
不改 app.py、不改 app/web/**。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import media  # noqa: E402
import providers  # noqa: E402

DATA = Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))

AUDIO_EXTS = {'.mp3', '.m4a', '.wav', '.amr', '.aac', '.flac', '.ogg', '.wma', '.opus'}
VIDEO_EXTS = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv', '.ts', '.m4v', '.wmv', '.mpg', '.mpeg'}
# t198：单对话入口要能接住截图/照片。图片没有时长，不能走 _probe 的 30 分钟口径，
# 单独用 _probe_image 读尺寸，再复用同一套 OCR 后端（RapidOCR → Windows OCR → pytesseract）。
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tif', '.tiff', '.heic'}
SUPPORTED = AUDIO_EXTS | VIDEO_EXTS | IMAGE_EXTS

MAX_SECONDS = 30 * 60          # 与现有转写/剪辑一致的上限
MAX_BYTES = 4 * 1024 * 1024 * 1024
ASR_MIN_CHARS = 20             # 少于这个字数视为"没转到内容"，改走 OCR
OCR_INTERVAL = 2.0             # 每 2 秒一帧
OCR_MAX_FRAMES = 600
SIMILAR = 0.72                 # 跨帧字幕合并阈值


class CaptureError(Exception):
    """带人话提示的失败：message 可直接展示给用户。"""


def _now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 文件与探测
# ---------------------------------------------------------------------------
def _ffprobe():
    exe = media.binary('ffprobe')
    if not exe:
        raise CaptureError('找不到 ffprobe，无法读取这个文件。请重新解压便携包（tools\\ffmpeg 缺失）。')
    return exe


def _ffmpeg():
    exe = media.binary('ffmpeg')
    if not exe:
        raise CaptureError('找不到 ffmpeg，无法处理这个文件。请重新解压便携包（tools\\ffmpeg 缺失）。')
    return exe


def _probe(path):
    """音/视频都支持（media.probe 只认视频，这里用 ffprobe 直接读）。"""
    try:
        raw = subprocess.run([str(_ffprobe()), '-v', 'error', '-show_format', '-show_streams',
                              '-of', 'json', str(path)], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired:
        raise CaptureError('读取文件超时（文件可能损坏或磁盘很慢），请换一份素材再试。') from None
    if raw.returncode != 0 or not raw.stdout.strip():
        raise CaptureError('这个文件读不出来（可能没下载完整或已损坏），请重新导出后再试。')
    try:
        obj = json.loads(raw.stdout)
    except ValueError:
        raise CaptureError('这个文件读不出来（格式信息解析失败），请重新导出后再试。') from None
    streams = obj.get('streams') or []
    has_video = any(s.get('codec_type') == 'video' for s in streams)
    has_audio = any(s.get('codec_type') == 'audio' for s in streams)
    duration = float((obj.get('format') or {}).get('duration') or 0)
    if duration <= 0:
        raise CaptureError('读不到这个文件的时长（可能只有图片或空文件），请换一份有内容的素材。')
    if duration > MAX_SECONDS:
        raise CaptureError('这个文件长 %d 分 %d 秒，超过单次 30 分钟上限。'
                           '请先截取你要的那一段（30 分钟内）再上传。'
                           % (int(duration // 60), int(duration % 60)))
    size = int((obj.get('format') or {}).get('size') or 0)
    if size <= 0:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    if size > MAX_BYTES:
        raise CaptureError('这个文件超过 4GB，单次处理不了。请先截取片段或压缩后再试。')
    return {'duration_ms': int(duration * 1000), 'video': has_video, 'audio': has_audio,
            'width': next((s.get('width') for s in streams if s.get('codec_type') == 'video'), 0),
            'height': next((s.get('height') for s in streams if s.get('codec_type') == 'video'), 0),
            'size': size}


def _probe_image(path):
    """图片没有时长，单独读尺寸和大小，避免走 _probe 的 30 分钟口径。"""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size <= 0:
        raise CaptureError('这个图片是空的，换一张再试。')
    if size > MAX_BYTES:
        raise CaptureError('这个图片超过 4GB，单次处理不了。请先压缩或截取后再试。')
    width = height = 0
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            width, height = im.size
    except Exception:
        # 没有 Pillow 时退回 ffprobe 读宽高（不依赖时长）。
        try:
            raw = subprocess.run([str(_ffprobe()), '-v', 'error', '-show_streams',
                                  '-of', 'json', str(path)], capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=60,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            obj = json.loads(raw.stdout or '{}')
            stream = next((s for s in (obj.get('streams') or []) if s.get('codec_type') == 'video'), {})
            width, height = int(stream.get('width') or 0), int(stream.get('height') or 0)
        except Exception:
            width = height = 0
    if width <= 0 or height <= 0:
        raise CaptureError('这张图片读不出来（格式可能不受支持）。请换成 jpg/png/webp 再试。')
    return {'duration_ms': 0, 'video': False, 'audio': False, 'width': width,
            'height': height, 'size': size, 'image': True}


def _temp_dir():
    base = DATA / 'capture_tmp'
    base.mkdir(parents=True, exist_ok=True)
    work = base / ('cap_' + uuid.uuid4().hex[:12])
    work.mkdir(parents=True, exist_ok=True)
    return work


def _extract_audio(src, work):
    out = work / 'audio16k.wav'
    try:
        media.extract_audio(str(src), str(out))
    except Exception as exc:
        raise CaptureError('提取音轨失败（%s）。如果这个文件只是画面、没有声音，请改用「画面字幕」模式。'
                           % str(exc).strip()[:120]) from None
    if not out.is_file() or out.stat().st_size == 0:
        raise CaptureError('提取音轨失败：没有生成可用的音频，可能这个文件本身没有声音。')
    return out


# ---------------------------------------------------------------------------
# 音轨转写
# ---------------------------------------------------------------------------
def transcribe_audio(audio_path, stage=None):
    """复用 providers.asr：auto=本地优先，失败自动切云端。返回 (result, error_text)。"""
    notify = stage or (lambda _m: None)
    try:
        result = providers.asr(audio_path, stage=notify)
        return result, None
    except providers.ProviderMissing as exc:
        msg = str(exc).strip()
        if not providers.cloud_allowed():
            # 账号未开通云端转写：原样给原因（含“不是本机 Key 配置问题”），别改写成“本机没装”。
            return None, msg[:600]
        return None, ('本机没有可用的语音识别（%s）。'
                      '下一步：在设置里确认本地模型已就绪，或填好自己的云端转写 Key。'
                      % msg[:200])
    except Exception as exc:
        if not providers.cloud_allowed():
            return None, str(exc).strip().replace('\n', ' ')[:600]
        return None, ('语音转写失败（%s）。如果只想拿画面上的字幕，可改用「画面字幕」模式。'
                      % str(exc).strip().replace('\n', ' ')[:200])


# ---------------------------------------------------------------------------
# 画面字幕 OCR
# ---------------------------------------------------------------------------
_RAPIDOCR = None
_RAPIDOCR_LOADED = False


def _rapidocr():
    """包内离线 RapidOCR。返回 {'name', 'engine'}；没装或装坏返回 None。"""
    global _RAPIDOCR, _RAPIDOCR_LOADED
    if _RAPIDOCR_LOADED:
        return _RAPIDOCR
    _RAPIDOCR_LOADED = True
    # rapidocr≥3 是新包名；rapidocr_onnxruntime 是旧包，返回结构不同，都兼容。
    for mod_name in ('rapidocr', 'rapidocr_onnxruntime'):
        try:
            module = __import__(mod_name, fromlist=['RapidOCR'])
            _RAPIDOCR = {'name': mod_name, 'engine': module.RapidOCR()}
        except Exception:
            _RAPIDOCR = None
        if _RAPIDOCR:
            break
    return _RAPIDOCR


def ocr_backends():
    """按优先级返回所有可用的画面文字识别后端。

    顺序：RapidOCR（离线、不依赖系统语言包）→ Windows 自带 OCR → pytesseract（装了才有）。
    """
    backends = []
    fast = _rapidocr()
    if fast:
        backends.append({'available': True, 'name': fast['name'], 'engine': fast['engine']})
    ps = Path(os.getenv('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    if os.name == 'nt' and ps.is_file():
        backends.append({'available': True, 'name': 'windows_ocr', 'engine': None, 'powershell': str(ps)})
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        backends.append({'available': True, 'name': 'pytesseract', 'engine': None})
    except Exception:
        pass
    return backends


def ocr_backend():
    """返回优先级最高的画面文字识别后端（兼容旧调用方）。"""
    backends = ocr_backends()
    if backends:
        return backends[0]
    return {'available': False, 'name': None,
            'hint': ('这台电脑上没找到画面文字识别组件（便携包里的离线识别没装好）。'
                     '下一步：改用「语音转写」，或重新解压完整便携包后再试。')}


def _prepare_image(src, work):
    """把任意支持的图片统一成 PNG（Windows OCR 只吃图片文件，RapidOCR 也稳定）。"""
    frames = work / 'frames'
    frames.mkdir(parents=True, exist_ok=True)
    target = frames / 'f_0001.png'
    try:
        from PIL import Image  # type: ignore
        with Image.open(src) as im:
            im.convert('RGB').save(target, 'PNG')
    except Exception:
        result = subprocess.run([str(_ffmpeg()), '-y', '-hide_banner', '-loglevel', 'error',
                                 '-i', str(src), '-frames:v', '1', str(target)],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=120,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0 or not target.is_file():
            raise CaptureError('这张图片转不出可识别的画面（%s）。请换成 jpg/png/webp 再试。'
                               % (result.stderr or '').strip()[:120]) from None
    return frames, [target]


def ocr_image(src, work, stage=None):
    """图片文字识别：复用与画面字幕相同的后端顺序，返回按出现顺序的行。"""
    notify = stage or (lambda _m: None)
    frames, files = _prepare_image(src, work)
    backends = ocr_backends()
    if not backends:
        raise CaptureError('这台电脑上没找到图片文字识别组件。下一步：重新解压完整便携包后再试，'
                           '或直接粘贴图片里的文字。')
    errors = []
    for candidate in backends:
        notify('识别图片文字（1 张，%s）' % candidate['name'])
        try:
            if str(candidate['name']).startswith('rapidocr'):
                rows = _rapidocr_rows(files, candidate['engine'])
            elif candidate['name'] == 'pytesseract':
                rows = _tesseract_rows(files)
            else:
                rows = _win_ocr(frames, candidate, work)
            merged = merge_subtitle_lines(rows, 1.0)
            lines = []
            for row in merged:
                text = _clean_line(row.get('text'))
                if _keep_line(text) and (not lines or lines[-1] != text):
                    lines.append(text)
            if lines:
                return lines
            errors.append('%s 没读到文字' % candidate['name'])
        except CaptureError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append('%s 识别失败（%s）' % (candidate['name'], str(exc).strip()[:120]))
    raise CaptureError('这张图片里没有识别到可用文字（%s）。可以换一张更清晰的，或直接粘贴文字。'
                       % '；'.join(errors[-2:]))


def _capture_image(src, ext, mode, notify):
    info = _probe_image(src)
    work = _temp_dir()
    try:
        lines = ocr_image(src, work, stage=notify)
        text = '\n'.join(lines).strip()
        if not text:
            raise CaptureError('这张图片里没有识别到可用文字，换一张更清晰的，或直接粘贴文字。')
        segments = [{'start_ms': 0, 'end_ms': 0, 'text': line, 'route': 'image_ocr'} for line in lines]
        return {
            'ok': True,
            'text': text,
            'source': 'image_ocr',
            'segments': segments,
            'confidence_note': ('图片文字识别：按图上文字顺序提取，共 %d 行；'
                                '手写、艺术字、复杂排版可能漏字，重要内容建议对照原图。' % len(lines)),
            'file': {'name': src.name, 'path': str(src), 'kind': 'image', 'ext': ext,
                     'duration_ms': 0, 'size_bytes': info['size'],
                     'width': info['width'], 'height': info['height']},
            'asr': None,
            'ocr': {'backend': 'image', 'frames': 1, 'segments': len(segments), 'error': None},
            'mode': mode,
            'captured_at': _now_ms(),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _extract_frames(src, work, duration_ms):
    frames = work / 'frames'
    frames.mkdir(parents=True, exist_ok=True)
    interval = max(OCR_INTERVAL, duration_ms / 1000.0 / OCR_MAX_FRAMES)
    result = subprocess.run([str(_ffmpeg()), '-y', '-hide_banner', '-loglevel', 'error',
                             '-i', str(src), '-vf', 'fps=1/%.3f' % interval,
                             '-frames:v', str(OCR_MAX_FRAMES), str(frames / 'f_%04d.png')],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=600,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode != 0:
        raise CaptureError('抽帧失败（%s）。请确认这个文件能正常播放，或换一份录屏。'
                           % result.stderr.strip()[:120])
    files = sorted(frames.glob('*.png'))
    if not files:
        raise CaptureError('这个文件里没有可识别的画面（抽帧为空），请确认是视频文件而不是纯音频。')
    return frames, files, interval


def _win_ocr(frames_dir, backend, work):
    out = work / 'ocr.json'
    script = ROOT / 'win_ocr.ps1'
    if not script.is_file():
        raise CaptureError('缺少画面识别组件（app\\win_ocr.ps1 不在包里），请重新解压便携包。')
    proc = subprocess.run([backend['powershell'], '-NoProfile', '-ExecutionPolicy', 'Bypass',
                           '-File', str(script), '-Dir', str(frames_dir), '-Out', str(out)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=900,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if not out.is_file():
        tail = (proc.stdout or '') + (proc.stderr or '')
        if 'NO_OCR_LANGUAGE' in tail:
            raise CaptureError('这台电脑的 Windows 自带文字识别缺中文语言包，读不出画面字幕。'
                               '下一步：改用「语音转写」，或重新解压完整便携包'
                               '（包内自带离线识别，不需要系统语言包）。')
        raise CaptureError('画面识别没有跑起来（系统 OCR 出错：%s）。'
                           '下一步：改用「语音转写」，或手动粘贴文案。'
                           % (tail.strip().replace('\n', ' ')[:220] or '没有返回结果'))
    try:
        rows = json.loads(out.read_text(encoding='utf-8-sig'))
    except ValueError:
        raise CaptureError('画面识别结果解析失败，请重试一次。')
    return rows if isinstance(rows, list) else [rows]


_CJK_GAP = re.compile(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])')


def _clean_line(text):
    text = _CJK_GAP.sub('', str(text or ''))
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return text


def _keep_line(text):
    if len(text) < 2:
        return False
    if re.fullmatch(r'[\d\W_]+', text):
        return False
    if re.fullmatch(r'(点赞|评论|分享|收藏|关注|首页|推荐|搜索|转发|说点什么|留下你的精彩评论)', text):
        return False
    return True


def merge_subtitle_lines(rows, interval):
    """跨帧字幕去重合并：相同/包含/高度相似视为同一句，保留更长的那条。"""
    merged = []
    prev_key = None
    prev_count = 0
    for row in rows:
        try:
            idx = int(re.findall(r'\d+', str(row.get('name') or ''))[-1])
        except (IndexError, ValueError):
            idx = len(merged) + 1
        cleaned = [_clean_line(raw) for raw in (row.get('lines') or [])]
        cleaned = [text for text in cleaned if _keep_line(text)]
        key = tuple(sorted(cleaned))
        if cleaned and key == prev_key:
            # 同一帧画面/同一屏文字重复出现：只把上一组字幕的结束时间往后延，不重复成稿。
            for item in merged[-prev_count:]:
                item['end_ms'] = int(idx * interval * 1000)
            continue
        prev_key, prev_count = key, len(cleaned)
        for text in cleaned:
            if merged and _same_subtitle(merged[-1]['text'], text):
                if len(text) > len(merged[-1]['text']):
                    merged[-1]['text'] = text
                merged[-1]['end_ms'] = int(idx * interval * 1000)
                continue
            start = int((idx - 1) * interval * 1000)
            merged.append({'start_ms': start, 'end_ms': int(idx * interval * 1000), 'text': text})
    return merged


def _same_subtitle(a, b):
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= SIMILAR


def ocr_video(src, work, duration_ms, backend, stage=None):
    notify = stage or (lambda _m: None)
    notify('抽取画面帧（每 %.1f 秒一帧）' % OCR_INTERVAL)
    frames_dir, files, interval = _extract_frames(src, work, duration_ms)
    notify('识别画面字幕：%d 帧（%s）' % (len(files), backend['name']))
    if str(backend['name']).startswith('rapidocr'):
        rows = _rapidocr_rows(files, backend['engine'])
    elif backend['name'] == 'pytesseract':
        rows = _tesseract_rows(files)
    else:
        rows = _win_ocr(frames_dir, backend, work)
    merged = merge_subtitle_lines(rows, interval)
    return merged


def _rapidocr_rows(files, engine):
    rows = []
    for f in files:
        result = engine(str(f))
        if isinstance(result, tuple):  # 旧版 rapidocr_onnxruntime：(rows, elapse)
            raw = result[0]
            lines = [item[1] for item in (raw or [])]
        else:  # rapidocr≥3：RapidOCROutput(txts=..., scores=..., boxes=...)
            lines = list(getattr(result, 'txts', ()) or ())
        lines = [str(x).strip() for x in lines if str(x).strip()]
        rows.append({'name': f.name, 'lines': lines})
    return rows


def _tesseract_rows(files):
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
    rows = []
    for f in files:
        text = pytesseract.image_to_string(Image.open(f), lang='chi_sim')
        rows.append({'name': f.name, 'lines': [x for x in text.splitlines() if x.strip()]})
    return rows


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def capture(path, mode='auto', stage=None):
    """把一个本地音/视频/图片文件变成文案。

    mode: auto（默认：音视频先音轨，空了再画面字幕；图片走文字识别）/
          asr（只走音轨）/ ocr（只走画面/图片文字识别）
    返回：{ok, text, source, segments, confidence_note, file, asr, ocr, captured_at}
    """
    notify = stage or (lambda _m: None)
    src = Path(str(path)).expanduser()
    if not src.is_file():
        raise CaptureError('找不到这个文件：%s' % src)
    ext = src.suffix.lower()
    if ext not in SUPPORTED:
        raise CaptureError('不支持的格式（%s）。可用音频：%s；可用视频：%s；可用图片：%s。'
                           % (ext or '无扩展名', '、'.join(sorted(AUDIO_EXTS)[:6]),
                              '、'.join(sorted(VIDEO_EXTS)[:8]),
                              '、'.join(sorted(IMAGE_EXTS)[:6])))
    mode = (mode or 'auto').lower()
    if mode not in ('auto', 'asr', 'ocr'):
        raise CaptureError('未知的识别方式：%s（可选 auto / asr / ocr）。' % mode)
    if ext in IMAGE_EXTS:
        if mode == 'asr':
            raise CaptureError('这是图片，没有音轨可转写。请改用图片文字识别，或直接粘贴文字。')
        notify('读取图片信息')
        return _capture_image(src, ext, mode, notify)

    notify('读取文件信息')
    info = _probe(src)
    work = _temp_dir()
    asr_result = None
    asr_error = None
    ocr_segments = []
    ocr_error = None
    backend = None
    try:
        # ---- 第 1 路：音轨转写 ----
        if mode in ('auto', 'asr') and info['audio']:
            notify('提取音轨')
            audio = _extract_audio(src, work)
            notify('语音转写中')
            asr_result, asr_error = transcribe_audio(audio, stage=notify)

        asr_text = _clean_asr_text((asr_result or {}).get('text') or '')
        has_asr = len(asr_text) >= ASR_MIN_CHARS

        # ---- 第 2 路：画面字幕（空/极短/显式要求） ----
        need_ocr = mode == 'ocr' or not has_asr
        if need_ocr:
            if not info['video']:
                if mode == 'ocr':
                    raise CaptureError('这是音频文件，画面里没有字幕可识别。请改用「语音转写」。')
                ocr_error = '这个文件只有音轨，没有画面字幕可识别。'
            else:
                backends = ocr_backends()
                if not backends:
                    ocr_error = ('这台电脑上没找到画面文字识别组件。'
                                 '下一步：改用「语音转写」，或重新解压完整便携包后再试。')
                else:
                    for candidate in backends:
                        backend = candidate
                        try:
                            ocr_segments = ocr_video(src, work, info['duration_ms'], candidate, stage=notify)
                            if ocr_segments:
                                ocr_error = None
                            else:
                                ocr_error = ('画面里没有可识别的字幕。可能是字幕太小、被遮挡，或这段是纯画面；'
                                             '建议手动粘贴文案，或换一段带字幕的录屏。')
                            break
                        except CaptureError as exc:
                            ocr_error = str(exc)
                        except Exception as exc:
                            ocr_error = '画面字幕识别失败（%s）。' % str(exc).strip()[:150]
                        if candidate is not backends[-1]:
                            notify('这种画面识别方式没跑起来，换下一种')
                    else:
                        # 全部识别方式都失败：只有显式「画面字幕」模式才补跑音轨。
                        if mode == 'ocr' and info['audio']:
                            notify('画面识别不可用，改为语音转写')
                            audio = _extract_audio(src, work)
                            asr_result, asr_error = transcribe_audio(audio, stage=notify)

        asr_text = _clean_asr_text((asr_result or {}).get('text') or '')
        ocr_text = _join_segments(ocr_segments, 60)
        if not asr_text and not ocr_text:
            raise CaptureError(_nothing_message(info, mode, asr_error, ocr_error))

        # ---- 合并输出 ----
        if asr_text and len(asr_text) >= ASR_MIN_CHARS:
            source = 'asr'
            text = asr_text
            note = ('语音转写：按音轨识别整段口播，准确度高；口误、同音词可能有个别偏差（%s）。'
                    % ((asr_result or {}).get('asr_route_note') or '本机/云端自动选择'))
            if ocr_error:
                note += ' 画面字幕不可用：' + ocr_error
        elif ocr_text:
            if asr_text:
                source = 'mixed'
                note = ('语音只转出 %d 个字，正文以画面字幕为准；字幕识别准确度中等，'
                        '遇到花字、遮挡或小字可能漏句。' % len(asr_text))
            else:
                source = 'ocr'
                note = '画面字幕识别（每 %.0f 秒一帧）：准确度中等，花字/遮挡/小字可能漏句。' % OCR_INTERVAL
                if asr_error:
                    note += ' 语音转写未成功：' + asr_error
            text = ocr_text
        else:
            source = 'asr'
            text = asr_text
            note = '语音转写：只识别到很少的文字，可能这段几乎没人说话。'
            if ocr_error:
                note += ' 画面字幕没拿到：' + ocr_error

        segments = []
        if asr_result and asr_text:
            for cue in (asr_result.get('cues') or []):
                segments.append({'start_ms': int(cue.get('start_ms') or 0),
                                 'end_ms': int(cue.get('end_ms') or 0),
                                 'text': _clean_line(cue.get('text')), 'route': 'asr'})
        for seg in ocr_segments:
            segments.append({**seg, 'route': 'ocr'})

        return {
            'ok': True,
            'text': text,
            'source': source,
            'segments': segments,
            'confidence_note': note,
            'file': {'name': src.name, 'path': str(src), 'kind': 'video' if info['video'] else 'audio',
                     'ext': ext, 'duration_ms': info['duration_ms'], 'size_bytes': info['size'],
                     'width': info['width'], 'height': info['height']},
            'asr': ({'route': asr_result.get('asr_route'),
                     'route_note': asr_result.get('asr_route_note'),
                     'chars': len(asr_text), 'error': asr_error,
                     'audio_seconds': asr_result.get('audio_seconds') or 0,
                     'cloud_elapsed_s': asr_result.get('cloud_elapsed_s') or 0}
                    if asr_result or asr_error else None),
            'ocr': ({'backend': (backend or {}).get('name'), 'frames': None,
                     'segments': len(ocr_segments), 'error': ocr_error}
                    if need_ocr else None),
            'mode': mode,
            'captured_at': _now_ms(),
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clean_asr_text(text):
    text = str(text or '').strip()
    if not text:
        return ''
    lines = [_clean_line(x) for x in re.split(r'[\r\n]+', text)]
    return '\n'.join([x for x in lines if x])


def _join_segments(segments, gap_ms):
    parts = []
    prev_end = None
    for seg in segments:
        if prev_end is not None and seg['start_ms'] - prev_end > gap_ms:
            parts.append('\n')
        parts.append(seg['text'])
        prev_end = seg['end_ms']
    return ''.join(parts).strip()


def _nothing_message(info, mode, asr_error, ocr_error):
    if not info['audio']:
        if mode == 'asr':
            return ('这个文件没有音轨，语音转写没有内容。'
                    '下一步：确认导出时带了声音，或改用「画面字幕」模式。')
        if ocr_error:
            return '这个文件没有音轨。' + ocr_error
        return ('这个文件没有音轨，画面里没有可识别的字幕。'
                '下一步：确认录屏时字幕已经显示在画面上，或改用带声音的版本；也可以手动粘贴文案。')
    if mode == 'asr':
        return ('语音转写没有拿到内容：%s 下一步：改用「画面字幕」模式，或检查音频是否有声音。'
                % (asr_error or '音频里几乎没有语音'))
    return ('两种方式都没拿到内容。语音：%s 画面：%s 下一步：换一段带人声或带字幕的素材，'
            '或直接手动粘贴文案。' % (asr_error or '未识别到语音', ocr_error or '未识别到字幕'))


# ---------------------------------------------------------------------------
# 落记录：与贴链接的「对标作品」放同一个地方（供后续 app.py 接线调用）
# ---------------------------------------------------------------------------
def save_to_store(store, result, display_name='本地文件'):
    """把一次文件转写存成 bench_work（platform=file），返回作品记录。

    同一路径 + 同大小 + 同一修改时间视为同一条，重复上传复用已有记录。
    标题取正文第一句（最多 40 字），没有正文则用文件名。
    """
    import contracts  # 延迟导入，避免模块测试时拉起整条链路
    src = Path(result['file']['path'])
    try:
        stamp = int(src.stat().st_mtime)
    except OSError:
        stamp = 0
    fingerprint = 'file_%s_%d_%d' % (re.sub(r'\W+', '', src.stem)[:24], result['file']['size_bytes'], stamp)
    existing = next((x for x in store.list(contracts.KIND_BENCH_WORK, 20000)
                     if x.get('platform') == 'file' and x.get('platform_video_id') == fingerprint), None)
    if existing:
        return existing, False

    account = next((x for x in store.list(contracts.KIND_BENCH_ACCOUNT, 5000)
                    if x.get('platform') == 'file' and x.get('external_id') == 'local_files'), None)
    if not account:
        account = store.put(contracts.KIND_BENCH_ACCOUNT, contracts.create_bench_account(
            'file', display_name, home_url='', external_id='local_files'))
    first = re.split(r'[。！？!?\n]', result['text'].strip())[0].strip()
    title = (first[:40] or src.stem)[:60]
    work = contracts.create_bench_work(
        account['id'], 'file', fingerprint, title, source_url='',
        duration_ms=result['file']['duration_ms'],
        raw={'file': result['file'], 'capture_source': result['source'],
             'confidence_note': result['confidence_note'], 'transcript_text': result['text'],
             'asr': result.get('asr'), 'ocr': result.get('ocr')})
    return store.put(contracts.KIND_BENCH_WORK, work), True


def ocr_ready():
    """给前端/自检用的一句话能力说明。"""
    backend = ocr_backend()
    if backend['available']:
        return {'available': True, 'backend': backend['name']}
    return {'available': False, 'backend': None, 'hint': backend.get('hint')}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='本地音/视频 → 文案')
    parser.add_argument('path')
    parser.add_argument('--mode', default='auto', choices=('auto', 'asr', 'ocr'))
    args = parser.parse_args()
    try:
        out = capture(args.path, mode=args.mode,
                      stage=lambda m: print('[%s] %s' % (time.strftime('%H:%M:%S'), m), file=sys.stderr, flush=True))
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except CaptureError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        sys.exit(1)
