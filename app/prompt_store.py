"""提示词中心：云端下发 + 本地缓存 + 内置默认兜底。

优先级：云端最新版 > 本地缓存 > 本文件内置默认（断网、401、5xx、缺条目都不会让功能不可用）。
模板里的 {{name}} 由调用方按固定顺序填充；服务端只存文本、不渲染。
内置默认文本由客户端源码原样抽取生成；改措辞走云端中控，不要手改本文件。
"""
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

REFRESH_SECONDS = 60
HTTP_TIMEOUT = 30
CACHE_NAME = 'prompts_cache.json'

# 2026-11-17（A2 空壳）：33 条内置模板全部掏空为短占位。
# 模板正文只在登录后由云端下发（prompts_cache.json 缓存 60 秒级刷新），
# 本机源码不再携带可用的核心提示词；断网 / 未登录时核心生成不可用，查看与导出照旧。
PLACEHOLDER = '【需登录云端获取本功能模板】登录后自动下发；已保存的内容仍可查看和导出。'
DEFAULT_KEYS = (
    'interview.ask',
    'interview.extract',
    'brain.coach',
    'brain.soul',
    'brain.answer',
    'create.topics',
    'create.script',
    'video.analyze_write',
    'reference.rewrite',
    'reference.knowledge',
    'subtitle.keywords',
    'edit.semantic_cut',
    'wash.stage1',
    'wash.stage2',
    'wash.rules',
    'bench.topic',
    'bench.text_topic',
    'bench.angle',
    'style.analyze',
    'feedback.classify',
    'feedback.insight',
    'feedback.topic',
    'director.mood_arc',
    'director.pacing',
    'director.visual_plan',
    'anchor.judge',
    'video_link.analysis',
    'compliance.review',
    'packaging.config',
    'packaging.translation',
    'eva.scene_titles',
    'eva.slots',
    'flow.anchors',
    'avatar.portrait',
)
DEFAULTS = {key: PLACEHOLDER for key in DEFAULT_KEYS}

class PromptUnavailable(Exception):
    """云端模板没到位（未登录 / 断网且无缓存）：核心生成停摆，查看与导出不受影响。"""
    code = 'prompt_login_required'

NAMES = {
    "interview.ask": "深挖访谈·下一问",
    "interview.extract": "深挖访谈·收口提炼",
    "brain.answer": "知识库·回答",
    "create.topics": "创作台·选题生成",
    "create.script": "创作台·讲稿生成",
    "video.analyze_write": "对标视频·拆解并创作",
    "reference.rewrite": "参考稿·重构讲稿",
    "reference.knowledge": "参考资料·提炼认知",
    "subtitle.keywords": "包装·字幕关键词强调",
    "edit.semantic_cut": "剪辑·语义删口误",
    "wash.stage1": "洗稿·拆题与三角度",
    "wash.stage2": "洗稿·成稿",
    "wash.rules": "洗稿·共用写作约束",
    "bench.topic": "对标库·开选题",
    "bench.text_topic": "转写正文·开选题",
    "bench.angle": "对标库·五角度",
    "style.analyze": "账号风格卡·分析",
    "feedback.classify": "评论·分类",
    "feedback.insight": "复盘·洞察",
    "feedback.topic": "洞察·转选题",
    "director.mood_arc": "导演·情绪弧线",
    "director.pacing": "导演·节奏",
    "director.visual_plan": "导演·视觉预埋",
    "anchor.judge": "锚定·take 判定",
    "video_link.analysis": "对标链接·拆解点评",
    "compliance.review": "合规·模型复核",
    "packaging.config": "横屏包装·配置",
    "packaging.translation": "横屏包装·英译",
    "eva.scene_titles": "竖屏包装·每镜标题",
    "eva.slots": "竖屏包装·槽位填字",
    "flow.anchors": "建档·三观三句",
    "avatar.portrait": "头像·生成提示词",
}

_cache = {}
_meta = {'etag': '', 'fetched_at': 0.0, 'origin': 'builtin', 'last_error': '', 'count': 0}
_used = {}
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


def _load_cache(store=None):
    try:
        data = json.loads(cache_path(store).read_text(encoding='utf-8'))
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key, value in (data.get('items') or {}).items():
        if key in DEFAULTS and isinstance(value, dict) and isinstance(value.get('text'), str) and value['text'].strip():
            _cache[key] = {'text': value['text'], 'version': int(value.get('version') or 0)}
    _meta['etag'] = str(data.get('etag') or '')
    _meta['fetched_at'] = float(data.get('fetched_at') or 0)
    if _cache:
        _meta['origin'] = 'cache'


def _save_cache():
    payload = {'etag': _meta['etag'], 'fetched_at': _meta['fetched_at'],
               'items': dict(_cache)}
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    except Exception as exc:
        log.warning('提示词缓存写入失败：%s', exc)


def _cloud_token():
    """取本机最近一次登录的云 token；没有登录态返回空串（走内置默认）。"""
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
    """带随包 CA 的 opener：本机系统代理开启时，默认 SSL 上下文握手会卡死（实测）。"""
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
        _meta['origin'] = 'cache' if _cache else 'builtin'
        return False
    url = auth_service.cloud_base().rstrip('/') + '/api/prompts/bundle'
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
            return True
        _meta['last_error'] = 'unauthorized' if exc.code in (401, 403) else 'http_%s' % exc.code
        log.debug('提示词下发失败：%s', _meta['last_error'])
        return False
    except Exception as exc:
        _meta['last_error'] = 'network'
        log.debug('提示词下发失败：%s', exc)
        return False
    try:
        data = json.loads(raw)
    except Exception:
        _meta['last_error'] = 'bad_json'
        return False
    fresh = {}
    for key, value in (data.get('items') or {}).items():
        if key in DEFAULTS and isinstance(value, dict) and isinstance(value.get('text'), str) and value['text'].strip():
            fresh[key] = {'text': value['text'], 'version': int(value.get('version') or 0)}
    if not fresh:
        _meta['last_error'] = 'empty'
        return False
    _cache.clear()
    _cache.update(fresh)
    _meta.update({'etag': etag or str(data.get('etag') or ''), 'fetched_at': time.time(),
                  'origin': 'cloud', 'last_error': '', 'count': len(fresh)})
    _save_cache()
    return True


def refresh(force=False):
    if not force and _meta['fetched_at'] and time.time() - _meta['fetched_at'] < REFRESH_SECONDS:
        return
    with _lock:
        _fetch()


def _loop():
    while not _stop.wait(REFRESH_SECONDS):
        try:
            refresh(force=True)
        except Exception as exc:  # 后台刷新永远不能抛出
            log.debug('提示词后台刷新异常：%s', exc)


def start(store=None):
    """启动后台刷新（幂等）。没有登录态时只读缓存；无缓存=核心生成不可用。"""
    global _store, _thread
    if store is not None:
        _store = store
    _load_cache()
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name='prompt-refresh', daemon=True)
    _thread.start()
    threading.Thread(target=lambda: refresh(force=True), name='prompt-refresh-once',
                     daemon=True).start()


def cloud_ready():
    """本机是否已拿到云端模板（有任意一条缓存即视为已就绪）。"""
    return bool(_cache)


def available(key):
    """该模板当前是否有可用正文（云端缓存命中）。"""
    entry = _cache.get(key)
    return bool(entry and str(entry.get('text') or '').strip())


def get(key, values=None, **kw):
    """取模板并填占位符。

    2026-11-17（A2 空壳）：内置 DEFAULTS 已掏空，只有云端下发的缓存才是可用正文。
    没有缓存时抛 PromptUnavailable（调用方/HTTP 层据此提示「请先登录」，不是把占位当提示词发出去）。
    诊断模式 IP_PROMPT_PLACEHOLDER=1 时仍返回占位串，便于离线查界面。
    """
    entry = _cache.get(key)
    text = entry['text'] if entry else ''
    _used[key] = entry['version'] if entry else 0
    if not (text and text.strip()):
        name = NAMES.get(key, key)
        if os.getenv('IP_PROMPT_PLACEHOLDER', '').strip() == '1':
            return PLACEHOLDER + '（' + name + '）'
        raise PromptUnavailable(
            '需登录云端获取「%s」功能模板后再使用；已保存的内容仍可查看和导出。' % name)
    merged = dict(values or {})
    merged.update(kw)
    for name, value in merged.items():
        text = text.replace('{{%s}}' % name, '' if value is None else str(value))
    return text


def version(key):
    entry = _cache.get(key)
    return entry['version'] if entry else 0


def status():
    """诊断用：当前来源、云端版本、最后错误。"""
    return {'origin': _meta['origin'], 'etag': _meta['etag'],
            'fetched_at': _meta['fetched_at'], 'last_error': _meta['last_error'],
            'cloud_items': len(_cache), 'builtin_items': 0,  # 2026-11-17 空壳：内置模板已掏空
            'placeholder': PLACEHOLDER,
            'versions': {k: v['version'] for k, v in _cache.items()}}
