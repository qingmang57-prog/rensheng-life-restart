"""方法论资产（题库/词库）云端下发 + 本地加密缓存。

与 prompt_store 同一套模式，但只存「核心资产」：
  优先级：云端最新（登录态 /api/assets/bundle）> 本地 DPAPI 密封缓存 > 无（占位「登录后可用」）
**绝不在本地留全文兜底**：没登录、没缓存、缓存超 TTL 一律返回 None，由调用方降级为引导/停摆。

- 云端分层：active 全量、trial/legacy 子集、expired 403（服务端判定）。
- 缓存：secret_box.seal（DPAPI，enc1: 前缀）落盘；写失败不落明文。
- 时效：沿用 B3 语义——缓存超过 GRACE_SECONDS（72h）且拉取失败，视为过期，停止供给。
"""
import hashlib
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import secret_box

log = logging.getLogger(__name__)

REFRESH_SECONDS = 60
HTTP_TIMEOUT = 30
CACHE_NAME = 'assets_cache.json'
GRACE_SECONDS = 72 * 3600

_cache = {}
_meta = {'etag': '', 'fetched_at': 0.0, 'origin': 'none', 'last_error': '', 'count': 0,
         'membership_state': '', 'server_time': 0.0}
_store = None
_thread = None
_lock = threading.Lock()
_stop = threading.Event()


def cache_path(store=None):
    store = store or _store
    if store is None:
        root = os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent / '.runtime'))
        return Path(root) / CACHE_NAME
    return Path(store.root) / CACHE_NAME


def _discard_cache(reason):
    """密文解不开/结构不对：删掉重拉，绝不回退明文。"""
    path = cache_path()
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
    log.debug('资产缓存已丢弃：%s', reason)


def _load_cache(store=None):
    path = cache_path(store)
    try:
        raw = path.read_text(encoding='utf-8').strip()
    except Exception:
        return
    if not raw:
        return
    if not raw.startswith('enc1:'):
        _discard_cache('明文缓存')
        return
    try:
        data = json.loads(secret_box.unseal(raw))
    except Exception:
        _discard_cache('解密失败')
        return
    if not isinstance(data, dict):
        _discard_cache('结构不是对象')
        return
    items = data.get('items') or {}
    for key, value in items.items():
        if isinstance(value, dict) and value.get('value') is not None:
            _cache[key] = {'value': value['value'], 'version': int(value.get('version') or 0)}
    _meta['etag'] = str(data.get('etag') or '')
    _meta['fetched_at'] = float(data.get('fetched_at') or 0)
    _meta['membership_state'] = str(data.get('membership_state') or '')
    if _cache:
        _meta['origin'] = 'cache'


def _save_cache():
    payload = {'etag': _meta['etag'], 'fetched_at': _meta['fetched_at'],
               'membership_state': _meta['membership_state'], 'items': dict(_cache)}
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sealed = secret_box.seal(json.dumps(payload, ensure_ascii=False))
        if not sealed:
            log.warning('资产缓存加密失败，本次不落盘（不写明文）')
            return
        tmp = path.with_suffix('.tmp')
        tmp.write_text(sealed, encoding='utf-8')
        tmp.replace(path)
    except Exception as exc:
        log.warning('资产缓存写入失败：%s', exc)


def _cloud_token():
    if _store is None:
        return ''
    try:
        import auth_service
        with _store.connect() as con:
            row = con.execute(
                "SELECT cloud_token FROM auth_cloud_sessions WHERE cloud_token<>'' "
                "AND expires_at>? ORDER BY last_seen_at DESC LIMIT 1", (time.time(),)).fetchone()
    except Exception:
        return ''
    if not row:
        return ''
    try:
        return auth_service.plain_token(row['cloud_token'])
    except Exception:
        return ''


def _opener():
    try:
        import pip._vendor.certifi as certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _fetch():
    import auth_service
    token = _cloud_token()
    if not token:
        _meta['origin'] = 'cache' if _cache else 'need_login'
        _meta['last_error'] = 'need_login'
        return False
    url = auth_service.cloud_base().rstrip('/') + '/api/assets/bundle'
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    if _meta['etag']:
        request.add_header('If-None-Match', _meta['etag'])
    try:
        with _opener().open(request, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8')
            etag = resp.headers.get('ETag') or ''
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            _meta['fetched_at'] = time.time()
            _meta['last_error'] = ''
            _meta['origin'] = 'cloud'
            return True
        _meta['last_error'] = ('membership_expired' if exc.code == 403 else
                               'unauthorized' if exc.code == 401 else 'http_%s' % exc.code)
        log.debug('资产下发失败：%s', _meta['last_error'])
        return False
    except Exception as exc:
        _meta['last_error'] = 'network'
        log.debug('资产下发网络失败：%s', exc)
        return False
    try:
        data = json.loads(raw)
    except Exception:
        _meta['last_error'] = 'bad_json'
        return False
    items = data.get('items') or {}
    if not isinstance(items, dict):
        _meta['last_error'] = 'bad_items'
        return False
    with _lock:
        _cache.clear()
        for key, value in items.items():
            if isinstance(value, dict) and value.get('value') is not None:
                _cache[key] = {'value': value['value'], 'version': int(value.get('version') or 0)}
        _meta['etag'] = etag
        _meta['fetched_at'] = time.time()
        _meta['origin'] = 'cloud'
        _meta['count'] = len(_cache)
        _meta['last_error'] = ''
        _meta['membership_state'] = str(data.get('membership_state') or '')
    _save_cache()
    return True


def refresh(force=False):
    if not force and _meta['fetched_at'] and time.time() - _meta['fetched_at'] < REFRESH_SECONDS:
        return bool(_cache)
    return _fetch()


def _ensure():
    if not _meta['fetched_at'] and not _cache:
        _load_cache()
    if not _meta['fetched_at'] or time.time() - _meta['fetched_at'] > REFRESH_SECONDS:
        refresh()


def _loop():
    while not _stop.wait(REFRESH_SECONDS):
        try:
            refresh(force=True)
        except Exception:
            pass


def start(store=None):
    """由 app 启动时调用一次；随后后台每 REFRESH_SECONDS 拉一次。"""
    global _store, _thread
    if store is not None:
        _store = store
    _load_cache()
    refresh(force=True)
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name='asset-store', daemon=True)
        _thread.start()
    return status()


def stop():
    _stop.set()


def stale():
    """缓存超过宽限期且最近一次拉取失败 → 停止供给（查看导出不受影响）。"""
    age = time.time() - float(_meta['fetched_at'] or 0)
    return bool(_cache) and age > GRACE_SECONDS and _meta['origin'] != 'cloud'


def get(key):
    """返回资产值（dict）或 None；None = 未登录/未下发/已过期，调用方必须降级。"""
    try:
        _ensure()
    except Exception:
        pass
    if stale():
        return None
    item = _cache.get(key)
    return item['value'] if item else None


def available(key):
    return get(key) is not None


def version(key):
    try:
        _ensure()
    except Exception:
        pass
    item = _cache.get(key)
    return item['version'] if item else 0


def cloud_ready():
    return _meta['origin'] == 'cloud'


def status():
    age = time.time() - float(_meta['fetched_at'] or 0) if _meta['fetched_at'] else None
    return {'origin': _meta['origin'], 'count': len(_cache), 'keys': sorted(_cache),
            'fetched_at': _meta['fetched_at'] or None,
            'age_seconds': int(age) if age is not None else None,
            'membership_state': _meta['membership_state'],
            'last_error': _meta['last_error'], 'stale': stale(),
            'grace_seconds': GRACE_SECONDS,
            'need_login': not _meta['fetched_at'] and not _cache}


# ===== t31 云适配层（2026-11-18）：URL 型媒体资产消费 =====
# 清单（JSON）走 bundle items key='packaging_assets'（DPAPI 缓存语义不变）；
# 媒体正文经清单内 url 下载 + sha256 强校验，落明文媒体目录（按 sha256 命名）。
# 失败一律降级返回 None 并留痕，绝不抛出阻断渲染（调用方跳过该资产即可）。
ASSETS_BUNDLE_KEY = 'packaging_assets'
MEDIA_DIR_NAME = 'packaging-assets'
MEDIA_MAX_BYTES = 512 * 1024 * 1024  # 单件上限 512MB（B-ROLL mov 级）
_last_media_error = ''
_EXT_BY_CTYPE = {'video/mp4': '.mp4', 'video/quicktime': '.mov', 'image/png': '.png',
                 'image/jpeg': '.jpg', 'audio/mpeg': '.mp3', 'audio/wav': '.wav',
                 'audio/x-wav': '.wav', 'font/otf': '.otf', 'font/ttf': '.ttf',
                 'application/x-font-otf': '.otf', 'application/x-font-ttf': '.ttf',
                 'application/zip': '.zip', 'application/json': '.json'}


def packaging_manifest():
    """云端 URL 型资产清单（dict）或 None；None=未登录/未下发/过期，调用方降级。"""
    value = get(ASSETS_BUNDLE_KEY)
    if not isinstance(value, dict) or not isinstance(value.get('assets'), list):
        return None
    return value


def _find_asset(asset_id, manifest=None):
    m = manifest if isinstance(manifest, dict) else packaging_manifest()
    if not m:
        return None
    for it in m.get('assets') or []:
        if isinstance(it, dict) and it.get('id') == asset_id:
            return it
    return None


def pick_media_for_moods(moods, kind=None, manifest=None):
    """按情绪+kind 过滤清单条目（moods=[] 条目=全情绪通用恒过）。
    返回条目 list（未物化，需经 ensure_media 落地）；无清单返回 []。"""
    m = manifest if isinstance(manifest, dict) else packaging_manifest()
    if not m:
        return []
    want = {str(mo) for mo in (moods or [])}
    out = []
    for it in m.get('assets') or []:
        if not isinstance(it, dict):
            continue
        if kind and it.get('kind') != kind:
            continue
        tags = set(it.get('moods') or [])
        if want and tags and not (want & tags):
            continue
        out.append(it)
    return out


def media_dir(store=None):
    """媒体缓存目录：IP_DATA_DIR/packaging-assets/（明文，sha256 命名，不进 DPAPI）。"""
    store = store or _store
    if store is None:
        root = os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent / '.runtime'))
    else:
        root = str(store.root)
    return Path(root) / MEDIA_DIR_NAME


def _media_ext(entry, url):
    ext = os.path.splitext(url or '')[1].lower()
    if ext in ('.mp4', '.mov', '.png', '.jpg', '.jpeg', '.mp3', '.wav', '.otf', '.ttf', '.zip', '.json'):
        return '.jpg' if ext == '.jpeg' else ext
    ctype = str(entry.get('content_type') or '').lower()
    for key, val in _EXT_BY_CTYPE.items():
        if ctype == key:
            return val
    for key, val in _EXT_BY_CTYPE.items():
        if ctype and ctype.startswith(key.split('/')[0] + '/'):
            return val
    return '.bin'


def _locate_entry_file(entry, role=None, manifest=None):
    """清单条目→下载 URL + 相对路径。优先级：条目 url 直用（云端态绝对 URL）→
    manifest.url_base + files[role].path / file（相对路径拼接）。返回 (url, rel)。"""
    url = entry.get('url')
    files = entry.get('files') or []
    pick = None
    if role:
        pick = next((f for f in files if f.get('role') == role), None)
        if pick is None:
            return None, None
    if pick is None and files:
        pick = files[0]
    rel = (pick or {}).get('path') or entry.get('file') or ''
    if (role or not url) and rel:
        base = (manifest if isinstance(manifest, dict) else packaging_manifest() or {}).get('url_base')
        if base:
            url = base.rstrip('/') + '/' + str(rel).lstrip('/').replace('\\', '/')
    return url, rel


def _cached_media_path(entry, rel=''):
    url, rel = _locate_entry_file(entry) if not rel else (None, rel)
    ext = _media_ext(entry, url or rel)
    digest = str(entry.get('sha256') or '')
    return media_dir() / ('%s-v%s%s' % (entry.get('id'), int(entry.get('version') or 0), ext)), digest


def ensure_media(asset_id, role=None, manifest=None, force=False, allow_dev_http=False):
    """按清单条目取媒体：缓存命中即返；否则下载→sha256 校验→原子落盘。
    role=多件资产取件（如 'mp4-loopable'/'static-png'/'png-seq-30fps'）；缺省主件。
    allow_dev_http=True 仅放行 http://127.0.0.1|localhost 回环（离线 mock/自检专用；
    生产调用不传该参，公网明文 http 一律拒绝）。返回本地路径 str 或 None
    （失败留痕 _last_media_error，不抛不阻断）。"""
    global _last_media_error
    entry = _find_asset(asset_id, manifest)
    if not entry:
        _last_media_error = 'asset_not_in_manifest:%s' % asset_id
        return None
    url, rel = _locate_entry_file(entry, role, manifest)
    if not url or not rel:
        _last_media_error = 'no_url:%s:%s' % (asset_id, role or 'main')
        return None
    ext = _media_ext(entry, url or rel)
    digest = str(entry.get('sha256') or '')
    safe_id = str(asset_id).replace('/', '__')
    path = media_dir() / ('%s-v%s%s' % (safe_id, int(entry.get('version') or 0), ext))
    if not force and digest and path.exists():
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                _last_media_error = ''
                return str(path)
        except Exception:
            pass  # 读失败按未缓存走下载
    lower = url.lower()
    loopback = False
    if lower.startswith('http://'):
        try:
            from urllib.parse import urlparse
            loopback = (urlparse(url).hostname or '') in ('127.0.0.1', 'localhost')  # 动态端口回环（离线 mock）
        except Exception:
            loopback = False
    if not (lower.startswith('https://') or (allow_dev_http and loopback)):
        _last_media_error = 'scheme_not_https:%s' % asset_id
        return None
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'ip-workbench-asset-store/1.1'})
        with _opener().open(request, timeout=HTTP_TIMEOUT * 10) as resp:
            blob = resp.read(MEDIA_MAX_BYTES + 1)
    except Exception as exc:
        _last_media_error = 'download_failed:%s:%s' % (asset_id, exc)
        return None
    if len(blob) > MEDIA_MAX_BYTES:
        _last_media_error = 'too_large:%s' % asset_id
        return None
    if digest:
        actual = hashlib.sha256(blob).hexdigest()
        if actual != digest:
            _last_media_error = 'sha256_mismatch:%s' % asset_id
            return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.part')
        tmp.write_bytes(blob)
        tmp.replace(path)
    except Exception as exc:
        _last_media_error = 'cache_write_failed:%s:%s' % (asset_id, exc)
        return None
    _last_media_error = ''
    return str(path)


def media_status():
    """媒体缓存诊断：目录、件数、字节、最近一次媒体错误留痕。"""
    d = media_dir()
    files = list(d.glob('*')) if d.exists() else []
    return {'dir': str(d), 'files': len(files),
            'bytes': sum(f.stat().st_size for f in files if f.is_file()),
            'last_error': _last_media_error}
