"""我的形象：可替换的人像资产（透明 PNG），按内容 SHA 幂等复用。

三个来源，优先级：
  1. 视频取帧（默认首选）：从项目成片/素材里抽帧，按「正脸、脸够大、不糊」打分挑一帧，
     抠图后存为形象；封面里的人是视频里的同一个真人。
  2. 上传照片：用户选一张半身/正面照。
  3. AI 生成虚构真人形象照：先探测图像生成能力（本机/包内模型或已配置的图像 API key），
     能跑就出图，跑不了如实返回「没有可用生成能力」，不假装。
  三条来源都走同一份 save()：按内容 SHA 幂等，同图/同帧重复使用不重算、不重扣。

计费口径（一次消耗、永久复用）：
  - 第一次保存一张新形象 = 1 次形象解锁（pricing_action='avatar_unlock'）；
  - 抠图结果落成 records 单条 kind='my_avatar' + PNG 文件，之后所有封面模板、所有次生成复用，0 消耗；
  - 只有换新形象才再记一次；同一内容重复提交幂等，不重算、不重复扣；
  - 本地版没有积分账本：只记录一次「消耗意图」并明示未扣分，LEDGER_HOOK 接入后自动生效。

抠图优先 rembg（U2Net）：模型随包放在 tools/avatar-model/models/u2net/u2net.onnx，
不依赖 .runtime；包内 venv 已固化 rembg + onnxruntime，离线可用。

红线：不做换脸（face swap），不把真实他人的脸贴到封面上；本模块只处理用户自己的形象。
"""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import prompt_store
from db import now

KIND = 'my_avatar'
KEY = 'owner'
LICENSE_KIND = 'avatar_license'
PRICING_ACTION = 'avatar_unlock'
# 账本挂点：ledger(payload) -> {'ok': True, 'ledger_id': '...'}；当前 None = 本地占位、不扣分。
LEDGER_HOOK = None

MAX_BYTES = 12 * 1024 * 1024
MIN_SIDE = 240
TARGET_MAX = 1600
PNG_MAX_H = 1200

DATA = Path(os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent / '.runtime'))).resolve()
BUNDLE = Path(__file__).resolve().parent.parent
AVATAR_DIR = DATA / 'avatars'
FRAME_DIR = DATA / 'avatar-frames'
# 随包模型：tools/avatar-model/models/u2net/u2net.onnx（rembg 的 U2NET_HOME 布局）
BUNDLE_MODEL_HOME = BUNDLE / 'tools' / 'avatar-model'

SOURCES = {'video_frame': '视频取帧', 'upload': '上传照片', 'ai_generated': 'AI 生成（虚构人物）'}

_LOCK = threading.Lock()
_SESSION = {'model': None, 'session': None}
_PROBE = {'done': False, 'data': None}


def _spec(name):
    import importlib.util
    return bool(importlib.util.find_spec(name))


def probe():
    """轻量自检：不 import rembg（重依赖），只看装没装、模型在不在。"""
    if not _PROBE['done']:
        home, source = _model_home()
        model_file = home / 'models' / os.getenv('IP_AVATAR_MODEL', 'u2net') / (os.getenv('IP_AVATAR_MODEL', 'u2net') + '.onnx')
        _PROBE['data'] = {
            'rembg': _spec('rembg'),
            'onnxruntime': _spec('onnxruntime'),
            'opencv': _spec('cv2'),
            'pillow': _spec('PIL'),
            'model': os.getenv('IP_AVATAR_MODEL', 'u2net'),
            'model_home': str(home), 'model_source': source, 'model_ready': model_file.is_file(),
            'engine': 'rembg' if _spec('rembg') and _spec('onnxruntime') else 'grabcut',
            'generation': generation_probe(),
        }
        _PROBE['done'] = True
    return dict(_PROBE['data'])


def _model_home():
    """模型查找：环境变量 > 随包 tools/avatar-model > 旧 .runtime 缓存。

    随包路径存在时优先用，保证 .runtime 被排除后抠图仍可用；
    返回 (home, 来源说明)，home 直接作为 rembg 的 U2NET_HOME。
    """
    env = os.getenv('IP_AVATAR_MODEL_HOME')
    if env:
        return Path(env).expanduser().resolve(), 'IP_AVATAR_MODEL_HOME'
    model = os.getenv('IP_AVATAR_MODEL', 'u2net')
    bundled = BUNDLE_MODEL_HOME / 'models' / model / (model + '.onnx')
    if bundled.is_file():
        return BUNDLE_MODEL_HOME, 'bundled'
    return DATA / 'models' / 'u2net', 'runtime-cache'


def _decode(payload):
    text = str(payload or '').strip()
    if not text:
        raise ValueError('没有收到图片数据。')
    if text.startswith('data:'):
        text = text.split(',', 1)[1] if ',' in text else ''
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:
        raise ValueError('图片数据不是合法的 base64。')
    return _decode_bytes(raw)


def _decode_bytes(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('照片太大（上限 12MB），请压缩后再传。')
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert('RGB')
    except Exception:
        raise ValueError('读不出这张图，请换 JPG/PNG/WEBP 格式再试。')
    if min(im.size) < MIN_SIDE:
        raise ValueError('照片太小（最短边需 ≥%d 像素），请换一张清晰的半身/正面照。' % MIN_SIDE)
    if max(im.size) > TARGET_MAX:
        scale = TARGET_MAX / max(im.size)
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return im, hashlib.sha256(buf.getvalue()).hexdigest()


def _rembg_session():
    model = os.getenv('IP_AVATAR_MODEL', 'u2net')
    if _SESSION['session'] is not None and _SESSION['model'] == model:
        return _SESSION['session']
    home, source = _model_home()
    # 显式覆盖，避免环境里旧的 U2NET_HOME 把查找带回 .runtime
    os.environ['U2NET_HOME'] = str(home)
    from rembg import new_session
    _SESSION['session'] = new_session(model)
    _SESSION['model'] = model
    return _SESSION['session']


def _cutout(im):
    try:
        from rembg import remove
        session = _rembg_session()
        out = remove(im, session=session, post_process_mask=True)
        return out.convert('RGBA'), 'rembg-u2net', 'U2Net 抠图（rembg）', ''
    except Exception as exc:
        out, warning = _grabcut(im)
        return out, 'grabcut-fallback', 'GrabCut 兜底（实测不可用：几乎保留原背景）', str(exc)[:200]


def _grabcut(im):
    """降级方案：GrabCut 矩形初始化。

    实测结论（2026-11-03，本机）：**不可用**。输入一张参考海报约 3.7 秒，
    输出 643×871，几乎保留整张原图（背景没被去掉），等于没抠。
    因此它只是「不崩溃」的存在性兜底，不要把它当成可用的降级方案；
    正式可用路径只有 rembg U2Net。不要在文档/界面里宣称 GrabCut 可替代。
    """
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        raise ValueError('没有可用的抠图引擎（rembg 与 cv2 都不可用）：' + str(exc)[:160])
    arr = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    h, w = arr.shape[:2]
    scale = min(1.0, 960 / max(h, w))
    if scale < 1:
        arr = cv2.resize(arr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = arr.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    rect = (int(w * 0.06), int(h * 0.04), int(w * 0.88), int(h * 0.92))
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(arr, mask, rect, bgd, fgd, 5, cv2.GC_INIT_WITH_RECT)
    alpha = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype('uint8')
    alpha = cv2.GaussianBlur(alpha, (5, 5), 0)
    rgba = np.dstack([cv2.cvtColor(arr, cv2.COLOR_BGR2RGB), alpha])
    from PIL import Image
    return Image.fromarray(rgba, 'RGBA'), 'GrabCut 实测不可用（3.7s 仍保留整张背景），仅作不崩溃兜底。'


def _finish(im):
    from PIL import Image
    im = im.convert('RGBA')
    bbox = im.getchannel('A').getbbox()
    if bbox:
        im = im.crop(bbox)
    pad = 10
    out = Image.new('RGBA', (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    out.paste(im, (pad, pad), im)
    if out.height > PNG_MAX_H:
        scale = PNG_MAX_H / out.height
        out = out.resize((max(1, int(out.width * scale)), PNG_MAX_H), Image.LANCZOS)
    return out


def path(store):
    rec = store.get(KIND, KEY)
    if not rec:
        return None
    p = Path(rec.get('png_path') or '')
    return p if p.is_file() else None


def _license_message(mode, ledger_id):
    return '已保存，可复用；之后所有封面直接用这张形象。'


def status(store):
    rec = store.get(KIND, KEY)
    p = Path(rec.get('png_path') or '') if rec else None
    if not rec or not p or not p.is_file():
        return {'exists': False, 'probe': probe(), 'pricing_action': PRICING_ACTION,
                'message': '优先从你在剪的视频里取一帧真人正脸；没有视频，也可以上传照片。'}
    mode = rec.get('mode') or 'local_placeholder'
    source = rec.get('source') or 'upload'
    return {
        'exists': True, 'url': '/api/avatar/image?v=' + str(rec.get('source_sha') or '')[:8],
        'source_sha': rec.get('source_sha'), 'width': rec.get('width'), 'height': rec.get('height'),
        'engine': rec.get('engine'), 'engine_label': rec.get('engine_label'),
        'warning': rec.get('warning') or '', 'elapsed_ms': rec.get('elapsed_ms'),
        'mode': mode, 'charged': mode == 'ledger', 'ledger_id': rec.get('ledger_id'),
        'source': source, 'source_label': SOURCES.get(source, source),
        'source_note': rec.get('source_note') or '',
        'pricing_action': PRICING_ACTION, 'reusable': True,
        'created_at': rec.get('created_at'), 'updated_at': rec.get('updated_at'),
        'message': _license_message(mode, rec.get('ledger_id')),
        'probe': probe(),
    }


def _charge(store, sha):
    """一次形象解锁的计费挂点。同一 sha 已有 license 记录则直接复用，不重复扣。"""
    key = 'avatar:' + sha[:16]
    existing = store.get(LICENSE_KIND, key)
    if existing:
        return existing
    ledger_id = None
    if LEDGER_HOOK is not None:
        res = LEDGER_HOOK({'pricing_action': PRICING_ACTION, 'source_sha': sha})
        if not res or not res.get('ok'):
            raise ValueError('形象解锁扣费未完成，未保存。' + str((res or {}).get('message', '')))
        ledger_id = res.get('ledger_id')
    mode = 'ledger' if ledger_id else 'local_placeholder'
    rec = store.put(LICENSE_KIND, {
        'source_sha': sha, 'pricing_action': PRICING_ACTION, 'granted_at': now(),
        'ledger_id': ledger_id, 'mode': mode,
        'note': '',
    }, key=key)
    return rec


def save(store, data):
    """保存形象（上传照片 / 视频帧 / AI 生成图）。返回 status + duplicate / elapsed_ms / message。"""
    data = data or {}
    source = str(data.get('source') or 'upload')
    if source not in SOURCES:
        source = 'upload'
    source_note = str(data.get('source_note') or '')[:160]
    with _LOCK:
        frame = data.get('frame')
        if frame:
            frame_path = frame_file(frame.get('token'), frame.get('n'))
            im, sha = _decode_bytes(frame_path.read_bytes())
            source = 'video_frame'
            frame_meta = _read_frame_meta(frame_path)
            source_note = (frame_meta.get('note') or source_note or ('视频取帧 ' + frame_path.name))[:160]
        else:
            im, sha = _decode(data.get('image') or data.get('data'))
        rec = store.get(KIND, KEY)
        existing_png = Path(rec.get('png_path') or '') if rec else None
        if rec and rec.get('source_sha') == sha and existing_png and existing_png.is_file():
            out = status(store)
            out.update({'duplicate': True,
                        'message': '这张形象已保存过，直接复用，不会重复处理。'})
            return out

        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()
        cut, engine, engine_label, warning = _cutout(im)
        cut = _finish(cut)
        elapsed = int((time.perf_counter() - t0) * 1000)

        png_path = AVATAR_DIR / ('my-avatar-%s.png' % sha[:12])
        source_path = AVATAR_DIR / ('source-%s.png' % sha[:12])
        cut.save(png_path, 'PNG', optimize=True)
        im.save(source_path, 'PNG', optimize=True)

        license_rec = _charge(store, sha)
        mode = license_rec.get('mode') or 'local_placeholder'
        record = store.put(KIND, {
            'source_sha': sha, 'source_path': str(source_path), 'png_path': str(png_path),
            'width': cut.width, 'height': cut.height,
            'engine': engine, 'engine_label': engine_label, 'warning': warning,
            'elapsed_ms': elapsed, 'pricing_action': PRICING_ACTION,
            'source': source, 'source_label': SOURCES.get(source, source), 'source_note': source_note,
            'mode': mode, 'ledger_id': license_rec.get('ledger_id'),
            'created_at': (rec or {}).get('created_at') or now(), 'updated_at': now(),
            'message': _license_message(mode, license_rec.get('ledger_id')),
        }, key=KEY)

        # 只保留最新一份形象资产；旧文件删除，旧 license 记录留作历史
        if rec:
            for old in (rec.get('png_path'), rec.get('source_path')):
                try:
                    if old and Path(old).is_file() and Path(old) != png_path and Path(old) != source_path:
                        Path(old).unlink()
                except OSError:
                    pass

        out = status(store)
        out.update({'duplicate': False, 'elapsed_ms': elapsed,
                    'message': '形象已保存，可复用。' + _license_message(mode, license_rec.get('ledger_id')).split('；', 1)[-1]})
        return out


# ---- 来源一：从项目视频取真人帧 -------------------------------------------------
# 挑帧规则可解释：正脸（cv2 frontalface 级联）→ 脸够大（脸宽 ≥ 12% 画面宽）→ 不糊（拉普拉斯方差 ≥ 40），
# 再按 脸大小 / 清晰度 / 居中 打分排序。只抽有限帧，不跑全片。
_CASCADE = {'clf': None}


def _cascade():
    if _CASCADE['clf'] is None:
        import cv2
        src = Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'
        clf = cv2.CascadeClassifier()
        # OpenCV 的 XML 读取不吃中文路径（包目录常含中文）：把 XML 内容读进内存再加载。
        try:
            text = src.read_text(encoding='utf-8', errors='ignore')
            fs = cv2.FileStorage(text, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
            node = fs.getNode('cascade')
            if not node.empty():
                clf.read(node)
        except Exception:
            pass
        if clf.empty():
            clf = cv2.CascadeClassifier(str(src))
        if clf.empty():
            raise ValueError('人脸检测器加载失败：无法读取 OpenCV 级联文件。')
        _CASCADE['clf'] = clf
    return _CASCADE['clf']


def _frame_token(src):
    st = src.stat()
    return hashlib.sha256(('%s|%s|%s' % (src, st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:16]


def _extract_frame(ffmpeg, src, at_ms, out):
    env = os.environ.copy()
    try:
        from media import runtime_environment
        env = runtime_environment()
    except Exception:
        pass
    args = [ffmpeg, '-v', 'error', '-ss', '%.3f' % (at_ms / 1000.0), '-i', str(src),
            '-frames:v', '1', '-vf', 'scale=900:-2', '-q:v', '3', '-y', str(out)]
    subprocess.run(args, capture_output=True, env=env, timeout=60,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def frames(video_path, samples=10, limit=5):
    """从视频抽帧并挑正脸清晰帧。返回候选（含缩略图 data URL 与取帧地址），不直接保存形象。"""
    src = Path(video_path)
    if not src.is_file():
        raise ValueError('视频文件不在了，无法取帧。')
    try:
        from media import binary, probe as media_probe
        info = media_probe(str(src))
        ffmpeg = binary('ffmpeg')
    except Exception as exc:
        raise ValueError('读不出这个视频：' + str(exc)[:140])
    if not ffmpeg:
        raise ValueError('缺少 ffmpeg，无法从视频取帧。')
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception as exc:
        raise ValueError('包内缺 cv2/Pillow，无法挑正脸帧；可直接上传照片。' + str(exc)[:100])
    duration = int(info.get('duration_ms') or 0)
    token = _frame_token(src)
    outdir = FRAME_DIR / token
    outdir.mkdir(parents=True, exist_ok=True)
    start, end = int(duration * 0.08), int(duration * 0.92)
    if end <= start:
        start, end = 0, max(0, duration - 1)
    times = [start + int(i * (end - start) / max(1, samples - 1)) for i in range(samples)]
    clf = _cascade()
    found, meta = [], {}
    for i, at in enumerate(times):
        f = outdir / ('%d.jpg' % i)
        _extract_frame(ffmpeg, src, at, f)
        if not f.is_file():
            continue
        arr = cv2.imdecode(np.fromfile(str(f), dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            continue
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        faces = clf.detectMultiScale(gray, 1.1, 5, minSize=(60, 60))
        if len(faces) == 0:
            continue
        x, y, fw, fh = max(faces, key=lambda v: int(v[2]) * int(v[3]))
        ratio = fw / float(w)
        sharp = float(cv2.Laplacian(gray[y:y + fh, x:x + fw], cv2.CV_64F).var())
        center = 1.0 - abs((x + fw / 2.0) - w / 2.0) / (w / 2.0)
        score = 0.55 * min(ratio / 0.35, 1.0) + 0.30 * min(sharp / 150.0, 1.0) + 0.15 * center
        qualified = ratio >= 0.12 and sharp >= 40
        reason = '正脸 · 脸宽 %d%% · 清晰度 %d · %s' % (round(ratio * 100), round(sharp), '合格' if qualified else '脸偏小或偏糊')
        im = Image.open(f).convert('RGB')
        thumb = im.copy()
        thumb.thumbnail((320, 320))
        buf = io.BytesIO()
        thumb.save(buf, 'JPEG', quality=80)
        item = {'n': i, 'time_ms': at, 'score': round(score, 3), 'qualified': qualified,
                'face_ratio': round(ratio, 3), 'sharpness': round(sharp, 1), 'reason': reason,
                'thumb': 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode(),
                'url': '/api/avatar/frame?token=%s&n=%d' % (token, i)}
        meta[str(i)] = {'time_ms': at, 'reason': reason, 'note': '视频取帧 %d:%02d' % (at // 60000, at % 60000 // 1000)}
        found.append(item)
    (outdir / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    found.sort(key=lambda x: (x['qualified'], x['score']), reverse=True)
    candidates = found[:limit]
    _prune_frame_dirs(keep=4)
    return {'video': src.name, 'duration_ms': duration, 'token': token,
            'samples': len(times), 'candidates': candidates,
            'message': ('' if candidates else '这段视频没挑到正脸清晰的帧：换一段正脸出镜的视频，或直接上传照片。')}


def _prune_frame_dirs(keep=4):
    try:
        dirs = sorted([p for p in FRAME_DIR.iterdir() if p.is_dir()], key=lambda p: p.stat().st_mtime, reverse=True)
        for old in dirs[keep:]:
            import shutil
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


def frame_file(token, n):
    """按令牌取候选帧文件；令牌与帧号都做白名单校验。"""
    if not re.fullmatch(r'[0-9a-f]{12,32}', str(token or '')):
        raise ValueError('取帧令牌不合法。')
    try:
        idx = int(n)
    except Exception:
        raise ValueError('帧号不合法。')
    p = FRAME_DIR / token / ('%d.jpg' % idx)
    if 0 <= idx < 20 and p.is_file():
        return p
    raise ValueError('这一帧已经不在了，请重新挑帧。')


def _read_frame_meta(frame_path):
    try:
        meta = json.loads((frame_path.parent / 'meta.json').read_text(encoding='utf-8'))
        return meta.get(frame_path.stem) or {}
    except Exception:
        return {}


# ---- 来源三：AI 生成虚构真人形象照 ---------------------------------------------

def generation_probe():
    """探测图像生成能力：已配置 API key 优先；否则看包内/本机有没有本地模型与 runner。"""
    api = os.getenv('IP_IMAGE_API_URL') or os.getenv('IP_IMAGE_URL')
    key = os.getenv('IP_IMAGE_API_KEY') or os.getenv('IP_IMAGE_KEY')
    if api and key:
        host = re.sub(r'https?://', '', api).split('/')[0]
        return {'available': True, 'kind': 'api', 'detail': '已配置图像 API：' + host}
    dirs = [Path(os.getenv('IP_IMAGE_MODEL_DIR') or ''), BUNDLE / 'tools' / 'image-model', DATA / 'models' / 'image']
    model = next((d for d in dirs if d and str(d) and d.is_dir() and (
        (d / 'model_index.json').is_file() or any(p.suffix in ('.safetensors', '.ckpt') for p in d.glob('*')))), None)
    if model and _spec('diffusers') and _spec('torch'):
        return {'available': True, 'kind': 'local', 'path': str(model),
                'detail': '本地生图模型：' + model.name}
    return {'available': False, 'kind': None,
            'detail': '没有可用的图像生成能力：未配置图像 API key，包内也没有本地生图模型。'}


def generate(store, data=None):
    """AI 生成虚构真人形象照 → 走同一份 save()（SHA 幂等、一次解锁永久复用）。"""
    data = data or {}
    g = generation_probe()
    if not g['available']:
        return {'available': False, 'generated': False, 'message': g['detail'], 'generation': g}
    prompt = str(data.get('prompt') or '').strip()[:400] or prompt_store.get('avatar.portrait')
    try:
        if g['kind'] == 'api':
            raw = _api_generate(prompt)
        else:
            raw = _local_generate(prompt, g.get('path'))
    except Exception as exc:
        return {'available': True, 'generated': False, 'message': '生成失败：' + str(exc)[:180], 'generation': g}
    out = save(store, {'image': base64.b64encode(raw).decode(), 'source': 'ai_generated',
                       'source_note': 'AI 生成虚构形象：' + prompt[:60]})
    out.update({'available': True, 'generated': True, 'generation': g})
    return out


def _api_generate(prompt):
    import urllib.request
    url = (os.getenv('IP_IMAGE_API_URL') or os.getenv('IP_IMAGE_URL')).rstrip('/')
    if not url.endswith('/images/generations'):
        url += '/images/generations'
    key = os.getenv('IP_IMAGE_API_KEY') or os.getenv('IP_IMAGE_KEY')
    model = os.getenv('IP_IMAGE_MODEL', 'gpt-image-1')
    payload = {'model': model, 'prompt': prompt, 'n': 1,
               'size': os.getenv('IP_IMAGE_SIZE', '1024x1536')}
    if 'gpt-image' not in model:
        payload['response_format'] = 'b64_json'
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=180) as r:
        obj = json.load(r)
    item = (obj.get('data') or [{}])[0]
    if item.get('b64_json'):
        return base64.b64decode(item['b64_json'])
    if item.get('url'):
        with urllib.request.urlopen(item['url'], timeout=120) as r:
            return r.read()
    raise ValueError('图像接口没有返回图片。')


def _local_generate(prompt, model_dir=None):
    """本地 diffusers 路径（仅在使用方装好模型与 runner 时进入；无模型环境不会执行）。"""
    import torch
    from diffusers import AutoPipelineForText2Image
    model = model_dir or os.getenv('IP_IMAGE_MODEL_DIR') or ''
    pipe = AutoPipelineForText2Image.from_pretrained(
        model, torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32)
    pipe = pipe.to('cuda' if torch.cuda.is_available() else 'cpu')
    im = pipe(prompt=prompt, num_inference_steps=28).images[0]
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return buf.getvalue()
