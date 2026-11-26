# -*- coding: utf-8 -*-
"""收款方式配置（静态二维码）：以云端为准，断网时回落本机。

2026-11-16 起改成云端统一配置（ip-api `/api/payee`）：
  - 后台（admin.jidixh.com「收款方式」）上传一次，所有客户端看到同一张码；
  - 本机只缓存：云端 JSON 缓存 60 秒，图片按 hash 落 <数据目录>/payee/cloud-<hash>.<ext>；
  - 云端连不上才回落本机旧配置（app/.runtime/payee/payee.json + qrcode.*）；
  - 云端明确说未配置/已停用时以云端为准，不拿本机旧图冒充。

边界：
  - 不接支付 SDK、不验签、不收回调、不生成订单、不写"支付成功"；
  - 二维码图片里通常带商户信息：本模块不打印、不记录字节、不把内容写进异常；
  - 未配置时 configured=False，前端显示"未配置收款方式"，不显示空白框；
  - 兑换流程固定为：扫码付款 -> 联系客服/管理员 -> 拿卡密 -> 软件内兑换到账。
"""
import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

NOTICE = '扫码付款后请把付款截图发给客服，凭卡密在软件内兑换到账；非自动到账。'
TEXT_LIMITS = {'title': 500, 'contact': 120, 'note': 500}
IMAGE_EXTS = ('.png', '.jpg', '.webp')
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _root():
    """与 app.py 的 DATA 同一口径；每次现取环境变量，测试可临时覆盖。"""
    base = os.getenv('IP_DATA_DIR') or str(Path(__file__).resolve().parent / '.runtime')
    return Path(base) / 'payee'


def _config_path():
    return _root() / 'payee.json'


def image_path(v=''):
    """按 URL 里的 v 定位图片。

    v='local-<mtime>' 取本机旧图；其他非空值取云端缓存 cloud-<v>.<ext>；
    空值优先云端缓存，再回落本机旧图（兼容老链接与断网情形）。
    """
    root = _root()
    if not root.is_dir():
        return None
    key = str(v or '').strip()
    if key.startswith('local-'):
        for ext in IMAGE_EXTS:
            candidate = root / ('qrcode' + ext)
            if candidate.is_file():
                return candidate
        return None
    if key:
        for ext in IMAGE_EXTS:
            candidate = root / ('cloud-' + key + ext)
            if candidate.is_file():
                return candidate
        return None
    clouds = [p for p in root.glob('cloud-*') if p.suffix in IMAGE_EXTS]
    if clouds:
        return max(clouds, key=lambda p: p.stat().st_mtime)
    for ext in IMAGE_EXTS:
        candidate = root / ('qrcode' + ext)
        if candidate.is_file():
            return candidate
    return None


def _read_config():
    try:
        data = json.loads(_config_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_config(data):
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    target = _config_path()
    tmp = target.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, target)


def _clean_text(value, field):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('%s 必须是文本。' % field)
    value = value.strip()
    if len(value) > TEXT_LIMITS[field]:
        raise ValueError('%s 不能超过 %d 字。' % (field, TEXT_LIMITS[field]))
    return value


def _sniff_ext(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if raw.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return '.webp'
    return None


def _decode_image(payload):
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError('没有收到图片。')
    text = payload.strip()
    if text.startswith('data:'):
        head, sep, body = text.partition(',')
        if not sep or ';base64' not in head.lower():
            raise ValueError('图片格式不对，请上传 PNG/JPG/WEBP。')
        text = body
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('图片内容不是合法 base64。')
    if not raw:
        raise ValueError('图片内容为空。')
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('图片不能超过 4MB。')
    ext = _sniff_ext(raw)
    if ext is None:
        raise ValueError('只支持 PNG/JPG/WEBP 图片。')
    return raw, ext


def save_info(*, title=None, contact=None, note=None):
    data = _read_config()
    for field, value in (('title', title), ('contact', contact), ('note', note)):
        if value is not None:
            data[field] = _clean_text(value, field)
    data['updated_at'] = int(time.time())
    _write_config(data)
    return get()


def save_image(payload):
    raw, ext = _decode_image(payload)
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / ('qrcode' + ext)
    tmp = root / 'qrcode.tmp'
    with open(tmp, 'wb') as handle:
        handle.write(raw)
    os.replace(tmp, target)
    for old in IMAGE_EXTS:
        stale = root / ('qrcode' + old)
        if stale != target and stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    data = _read_config()
    data['updated_at'] = int(time.time())
    _write_config(data)
    return get()


def clear_image():
    root = _root()
    for ext in IMAGE_EXTS:
        stale = root / ('qrcode' + ext)
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    data = _read_config()
    data['updated_at'] = int(time.time())
    _write_config(data)
    return get()


def get():
    """云端优先；云端不可达才用本机旧配置（source 字段告诉前端当前口径）。"""
    cloud = cloud_snapshot()
    if cloud is not None:
        view = _cloud_view(cloud)
        if view is not None:
            return view
    return _local_view()


def _local_view():
    data = _read_config()
    image = None
    for ext in IMAGE_EXTS:
        candidate = _root() / ('qrcode' + ext)
        if candidate.is_file():
            image = candidate
            break
    image_url = ''
    if image is not None:
        try:
            stamp = int(image.stat().st_mtime)
        except OSError:
            stamp = 0
        image_url = '/api/billing/payee/image?v=local-%d' % stamp
    return {'configured': image is not None,
            'has_image': image is not None,
            'has_contact': bool(str(data.get('contact') or '').strip()),
            'paused': False,
            'source': 'local',
            'image_url': image_url,
            'title': str(data.get('title') or ''),
            'contact': str(data.get('contact') or ''),
            'note': str(data.get('note') or ''),
            'updated_at': data.get('updated_at'),
            'notice': NOTICE}


# ---- 云端配置（2026-11-16）------------------------------------------------
CLOUD_TTL = 60
_CLOUD = {'at': 0.0, 'payload': None}


def _cloud_base():
    return (os.getenv('IP_API_BASE') or 'https://api.jidixh.com').rstrip('/')


def _cloud_get(path, timeout=30.0):
    request = urllib.request.Request(_cloud_base() + path)
    request.add_header('Accept', 'application/json,image/*')
    request.add_header('User-Agent', 'RenshengRestartDesktop/1.0')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _prune_cloud_images(keep=''):
    root = _root()
    if not root.is_dir():
        return
    for item in root.glob('cloud-*'):
        if keep and item.name == keep:
            continue
        try:
            item.unlink()
        except OSError:
            pass


def cloud_snapshot():
    """云端 /api/payee，60 秒缓存；连不上或响应不可用时返回 None。"""
    now = time.time()
    cached = _CLOUD['payload']
    if cached is not None and now - _CLOUD['at'] < CLOUD_TTL:
        return cached
    try:
        data = json.loads(_cloud_get('/api/payee', 2.5).decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('ok') is not True:
        return None
    if not data.get('configured'):
        _prune_cloud_images()
    _CLOUD.update(at=now, payload=data)
    return data


def _cloud_image(key):
    key = ''.join(ch for ch in str(key or '') if ch.isalnum())[:16]
    if not key:
        return None
    root = _root()
    for ext in IMAGE_EXTS:
        cached = root / ('cloud-' + key + ext)
        if cached.is_file():
            return cached
    try:
        raw = _cloud_get('/api/payee/image?v=' + key, 6.0)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    ext = _sniff_ext(raw)
    if ext is None or not raw or len(raw) > MAX_IMAGE_BYTES:
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
        target = root / ('cloud-' + key + ext)
        tmp = root / 'cloud.tmp'
        with open(tmp, 'wb') as handle:
            handle.write(raw)
        os.replace(tmp, target)
    except OSError:
        return None
    _prune_cloud_images(keep=target.name)
    return target


def _cloud_view(data):
    base = {'title': str(data.get('title') or ''),
            'contact': str(data.get('contact') or ''),
            'has_contact': bool(str(data.get('contact') or '').strip()),
            'note': str(data.get('note') or ''),
            'updated_at': data.get('updated_at'),
            'notice': str(data.get('notice') or NOTICE),
            'source': 'cloud'}
    if not data.get('configured'):
        return dict(base, configured=False, paused=bool(data.get('paused')),
                    has_image=False, image_url='')
    url = str(data.get('image_url') or '')
    key = url.split('v=', 1)[1] if 'v=' in url else ''
    key = ''.join(ch for ch in key if ch.isalnum())[:16]
    path = _cloud_image(key) if key else None
    if path is None:
        return None
    return dict(base, configured=True, paused=False, has_image=True,
                image_url='/api/billing/payee/image?v=' + key)
