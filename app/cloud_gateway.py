# -*- coding: utf-8 -*-
"""云端代理客户端：大模型 / 云端转写统一走 api.jidixh.com，本机不再持有上游 Key。

- 凭据：只从本地会话表取「当前登录账号」的云端令牌（与 auth_service 同一张表，
  不新增登录、不落新文件）；令牌只进内存，不打印、不写日志。
- 开关：IP_KEY_SOURCE=proxy（默认）走代理；IP_KEY_SOURCE=local 回滚到旧的
  「读本机 Key 直连」路径。两种模式都不会静默互相顶替。
- 计量、限流、配额都在服务端；这里只把错误翻成中文，不伪造结果。
"""
import base64
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import secret_box

DEFAULT_BASE = 'https://api.jidixh.com'
LLM_TIMEOUT = float(os.getenv('IP_PROXY_LLM_TIMEOUT', '180') or 180)
ASR_TIMEOUT = float(os.getenv('IP_PROXY_ASR_TIMEOUT', '300') or 300)
ASR_MAX_BYTES = int(os.getenv('IP_PROXY_ASR_MAX_BYTES', '') or 8 * 1024 * 1024)
# 云端钱包（2026-11-18 积分真源）：短超时、内存缓存 TTL ≤ 15s、关单重试 1/2/4s。
WALLET_TIMEOUT = float(os.getenv('IP_WALLET_TIMEOUT', '8') or 8)
WALLET_CACHE_TTL = 15.0
WALLET_RETRY_DELAYS = (1.0, 2.0, 4.0)
# 云端连不上时记住失败，短期内不再撞超时（客户机弱网/断网时 bootstrap 不被拖死）。
WALLET_NEG_TTL = float(os.getenv('IP_WALLET_NEG_TTL', '60') or 60)


class ProxyError(RuntimeError):
    """云端代理失败：message 已是可展示的中文，code/status 供调用方判断是否重试。

    details 携带云端错误体的附加字段（如 INSUFFICIENT_POINTS 的 required/available/balance），
    只作展示与判定，不含任何凭据。
    """

    def __init__(self, message, code='', status=0, details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})


def base():
    return (os.getenv('IP_API_BASE') or DEFAULT_BASE).rstrip('/')


def key_source():
    """proxy（默认：云端代理）/ local（回滚：读本机 Key 直连）。"""
    raw = (os.getenv('IP_KEY_SOURCE') or os.getenv('IP_LLM_MODE') or 'proxy').strip().lower()
    return 'local' if raw in ('local', 'direct', 'file', 'byok') else 'proxy'


def _db_path():
    root = os.getenv('IP_DATA_DIR')
    folder = Path(root) if root else Path(__file__).resolve().parent / '.runtime'
    return folder / 'workbench.sqlite3'


def cloud_token():
    """当前登录账号的云端令牌；没有登录返回空串。只读，不缓存到磁盘。"""
    path = _db_path()
    if not path.is_file():
        return ''
    try:
        con = sqlite3.connect(str(path), timeout=5)
        try:
            row = con.execute(
                'SELECT cloud_token FROM auth_cloud_sessions '
                'WHERE expires_at>? ORDER BY last_seen_at DESC LIMIT 1',
                (time.time(),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ''
    return secret_box.unseal(row[0]) if row else ''


def available():
    return key_source() == 'proxy' and bool(cloud_token())


def describe():
    """给状态接口用：只报模式和是否就绪，不含任何凭据。"""
    return {'mode': key_source(), 'ready': available(),
            'base': base(), 'logged_in': bool(cloud_token())}


def _call(path, payload, timeout, method='POST'):
    token = cloud_token()
    if not token:
        raise ProxyError('尚未登录云端账号，云端服务不可用；请先登录。', 'NOT_LOGGED_IN', 401)
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(
        base() + path, body,
        {'Content-Type': 'application/json',
         'Authorization': 'Bearer ' + token,
         'User-Agent': 'RenshengRestartDesktop/1.0'},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        data = {}
        try:
            data = json.loads(exc.read(1024 * 1024).decode('utf-8', 'replace'))
        except Exception:  # noqa: BLE001
            data = {}
        message = str((data or {}).get('error') or (data or {}).get('message') or '').strip()
        code = str((data or {}).get('code') or '')
        if exc.code == 401:
            raise ProxyError(message or '云端登录已失效，请重新登录后再试。',
                             code or 'UNAUTHORIZED', 401) from None
        if exc.code == 429:
            raise ProxyError(message or '请求过于频繁，请稍后再试。',
                             code or 'RATE_LIMITED', 429) from None
        raise ProxyError(message or '云端服务返回异常（%s），请稍后再试。' % exc.code,
                         code or 'HTTP_%d' % exc.code, exc.code,
                         details=data if isinstance(data, dict) else None) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProxyError('连不上云端服务，请检查网络后再试；本地功能不受影响。',
                         'UNREACHABLE', 0) from None
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ProxyError('云端服务返回的内容看不懂，请稍后再试。', 'BAD_RESPONSE', 502) from None
    if not isinstance(data, dict):
        raise ProxyError('云端服务返回的内容看不懂，请稍后再试。', 'BAD_RESPONSE', 502)
    return data


def llm_chat(messages, model=None, json_mode=False, max_tokens=None, timeout=None, job_ref=None):
    """代理对话补全。返回 {content, usage, model, cost_cny}；失败抛 ProxyError。

    job_ref：可选冻结号；留空时自动取本线程当前第二大脑问答的冻结号
    （billing_route.start_brain_ask 登记），供服务端按 jobRef 记用量与超时清扫分流。
    """
    if not isinstance(messages, list) or not messages:
        raise ProxyError('对话内容为空，未发起请求。', 'INVALID_INPUT', 400)
    payload = {'messages': messages}
    if model:
        payload['model'] = str(model)
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    if max_tokens:
        payload['max_tokens'] = int(max_tokens)
    ref = str(job_ref or current_ask_job() or '')[:64]
    if ref:
        payload['jobRef'] = ref
    data = _call('/api/llm/chat', payload, timeout or LLM_TIMEOUT)
    content = str(data.get('content') or '')
    if not content.strip():
        raise ProxyError('云端模型没有返回内容，请稍后再试。', 'EMPTY_CONTENT', 502)
    return {'content': content, 'usage': data.get('usage') or {},
            'model': str(data.get('model') or ''), 'cost_cny': data.get('cost_cny')}


def asr_transcribe(audio_bytes, fmt='mp3', duration_sec=None, timeout=None):
    """代理云端转写。返回 {text, cues, seconds}；失败抛 ProxyError。"""
    if not audio_bytes:
        raise ProxyError('音频内容为空，未发起请求。', 'INVALID_INPUT', 400)
    if len(audio_bytes) > ASR_MAX_BYTES:
        raise ProxyError('音频超过 8MB，请先分段再用云端转写。', 'TOO_LARGE', 400)
    payload = {'audio_base64': base64.b64encode(audio_bytes).decode('ascii'),
               'format': (fmt or 'mp3').lower()}
    if duration_sec:
        payload['duration_sec'] = float(duration_sec)
    data = _call('/api/asr/transcribe', payload, timeout or ASR_TIMEOUT)
    return {'text': str(data.get('text') or ''),
            'cues': data.get('cues') or [],
            'words': data.get('words') or [],
            'seconds': float(data.get('seconds') or 0)}


# ---------------------------------------------------------------- 云端钱包
# 2026-11-18 积分真源（设计稿 §2/§3）：登录账号（cloud-*）第二大脑积分的唯一
# 真源是云端 auth_points.balance；后台 /admin/wallet/grant|debit 即时生效。
# 这里只做用户侧 /api/brain/credit/{balance,hold,settle,release} 的客户端：
#   - 余额内存缓存 TTL ≤ 15s；hold/settle/release 响应立即覆写缓存；
#   - 登录/登出由 auth_service 调 wallet_cache_clear() 清空；不落盘；
#   - 关单（settle/release）失败重试 3 次（1/2/4s），仍失败写待对账日志，
#     启动时按 jobRef 幂等重放；服务端 15 分钟清扫兜底，不丢答案、不重复扣。
# 本地账本（billing.sqlite3）不替云端账号的第二大脑记账；封面/形象等本地商品
# 仍走 billing_service，与本节无关。

_WALLET_CACHE = {}
_WALLET_LOCK = threading.Lock()
_WALLET_REFRESHING = set()   # 正在后台刷新的账号，避免重复起线程
_WALLET_FAIL = {}            # 账号 -> (失败时刻, code, message)，负缓存
_ASK_JOB = threading.local()
# 服务端已关单的码：不再重试、不进待对账（幂等语义，见设计稿 §2.3/§2.4）。
_WALLET_CLOSED_CODES = ('HOLD_NOT_FOUND', 'HOLD_RELEASED', 'HOLD_SETTLED')


def set_ask_job(job_ref):
    """登记本线程当前的第二大脑冻结号（start_brain_ask 成功后调用）。"""
    _ASK_JOB.job_ref = str(job_ref or '')


def clear_ask_job():
    _ASK_JOB.job_ref = ''


def current_ask_job():
    return getattr(_ASK_JOB, 'job_ref', '')


def _wallet_account():
    """当前会话的云端账号 id（缓存键）；未登录返回空串。令牌不进缓存、不落盘。"""
    path = _db_path()
    if not path.is_file():
        return ''
    try:
        con = sqlite3.connect(str(path), timeout=5)
        try:
            row = con.execute(
                'SELECT cloud_id FROM auth_cloud_sessions '
                'WHERE expires_at>? ORDER BY last_seen_at DESC LIMIT 1',
                (time.time(),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ''
    return str(row[0]) if row else ''


def wallet_cache_clear():
    """登录/登出/切号时清空余额缓存（auth_service 调用）。"""
    with _WALLET_LOCK:
        _WALLET_CACHE.clear()
        _WALLET_FAIL.clear()
        _WALLET_REFRESHING.clear()


def _cache_get():
    with _WALLET_LOCK:
        snap = _WALLET_CACHE.get(_wallet_account())
        return dict(snap) if snap else {}


def _cache_store(data):
    """任何 balance/hold/settle/release 响应都立即覆写缓存（强制刷新口径）。"""
    key = _wallet_account()
    if not key or not isinstance(data, dict):
        return
    with _WALLET_LOCK:
        prev = _WALLET_CACHE.get(key) or {}
        _WALLET_CACHE[key] = {
            'balance': data.get('balance'),
            'held': data.get('held'),
            'available': data.get('available'),
            # hold/settle/release 响应不带报价：沿用上一次 balance 回显的值。
            'brain_ask_points': data.get('brain_ask_points', prev.get('brain_ask_points')),
            'fetched_at': time.time(),
            'stale': False,
        }


def mark_stale():
    """钱包操作断云时把最近一次内存余额标 stale（设计稿 §3.4），供展示降级。"""
    with _WALLET_LOCK:
        snap = _WALLET_CACHE.get(_wallet_account())
        if snap:
            snap['stale'] = True


def _wallet_refresh_async(timeout):
    """缓存过期时后台悄悄刷新；同一账号同时只起一个线程。"""
    key = _wallet_account()
    if not key:
        return
    with _WALLET_LOCK:
        if key in _WALLET_REFRESHING:
            return
        _WALLET_REFRESHING.add(key)

    def run():
        try:
            data = _call('/api/brain/credit/balance', None, timeout or WALLET_TIMEOUT, method='GET')
            _cache_store(data)
            with _WALLET_LOCK:
                _WALLET_FAIL.pop(key, None)
        except ProxyError as exc:
            mark_stale()
            with _WALLET_LOCK:
                _WALLET_FAIL[key] = (time.time(), exc.code, str(exc))
        finally:
            with _WALLET_LOCK:
                _WALLET_REFRESHING.discard(key)

    threading.Thread(target=run, daemon=True).start()


def credit_balance(force=False, timeout=None):
    """读云端余额（展示路径，永不抛异常）。

    返回 {balance, held, available, brain_ask_points, stale, fetched_at}。
    缓存过期先回旧值、后台悄悄刷新（翻页不等云端）；force=True 同步直读
    （问答结束后的强制刷新口径）；无缓存且云端刚连不上时，负缓存窗口内
    直接回离线占位，不反复撞超时。
    """
    cached = _cache_get()
    now = time.time()
    if not force and cached:
        if now - float(cached.get('fetched_at') or 0) < WALLET_CACHE_TTL:
            return cached
        _wallet_refresh_async(timeout)
        return cached
    key = _wallet_account()
    if not force and key:
        with _WALLET_LOCK:
            fail = _WALLET_FAIL.get(key)
        if fail and now - fail[0] < WALLET_NEG_TTL:
            return {'balance': None, 'held': None, 'available': None,
                    'brain_ask_points': None, 'stale': True, 'fetched_at': None,
                    'error': fail[1], 'message': fail[2]}
    try:
        data = _call('/api/brain/credit/balance', None, timeout or WALLET_TIMEOUT, method='GET')
    except ProxyError as exc:
        with _WALLET_LOCK:
            if key:
                _WALLET_FAIL[key] = (time.time(), exc.code, str(exc))
        if cached:
            cached['stale'] = True
            return cached
        return {'balance': None, 'held': None, 'available': None,
                'brain_ask_points': None, 'stale': True, 'fetched_at': None,
                'error': exc.code, 'message': str(exc)}
    _cache_store(data)
    with _WALLET_LOCK:
        _WALLET_FAIL.pop(key, None)
    return _cache_get()


def credit_hold(job_ref, action='brain_ask', qty=1):
    """冻结一次问答积分（服务端报价，客户端不传金额）。失败抛 ProxyError。"""
    data = _call('/api/brain/credit/hold',
                 {'jobRef': str(job_ref), 'action': str(action), 'qty': int(qty)},
                 WALLET_TIMEOUT)
    _cache_store(data)
    return data


def credit_settle(job_ref):
    """结算：balance 只在 settle 时真正减少。失败抛 ProxyError。"""
    data = _call('/api/brain/credit/settle', {'jobRef': str(job_ref)}, WALLET_TIMEOUT)
    _cache_store(data)
    return data


def credit_release(job_ref, reason=''):
    """释放：全额解冻，balance 不动。失败抛 ProxyError。"""
    payload = {'jobRef': str(job_ref)}
    if reason:
        payload['reason'] = str(reason)[:200]
    data = _call('/api/brain/credit/release', payload, WALLET_TIMEOUT)
    _cache_store(data)
    return data


def _pending_path():
    root = os.getenv('IP_DATA_DIR')
    folder = Path(root) if root else Path(__file__).resolve().parent / '.runtime'
    return folder / 'wallet_pending.jsonl'


def _pending_append(entry):
    try:
        path = _pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass  # 待对账日志写不进也不丢答案：服务端 15 分钟清扫兜底


def wallet_close(job_ref, ok=True, reason=''):
    """关单（成功 settle / 失败 release）：重试 3 次，穷尽写待对账；永不抛异常。

    服务端已关单（HOLD_NOT_FOUND/HOLD_RELEASED/HOLD_SETTLED）视为完成，不重试。
    """
    last = None
    for attempt in range(len(WALLET_RETRY_DELAYS) + 1):
        try:
            if ok:
                return credit_settle(job_ref)
            return credit_release(job_ref, reason or '生成失败')
        except ProxyError as exc:
            last = exc
            if exc.code in _WALLET_CLOSED_CODES:
                return None
            if attempt < len(WALLET_RETRY_DELAYS):
                time.sleep(WALLET_RETRY_DELAYS[attempt])
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < len(WALLET_RETRY_DELAYS):
                time.sleep(WALLET_RETRY_DELAYS[attempt])
    _pending_append({'jobRef': str(job_ref), 'ok': bool(ok), 'reason': str(reason or ''),
                     'at': time.time(), 'code': str(getattr(last, 'code', '') or '')})
    return None


def wallet_replay_pending():
    """启动时把待对账关单按 jobRef 幂等重放一次；成功/已关单即销账。

    未登录时保留不动（等下次登录后的启动再重放）。返回 {replayed, left}。
    """
    path = _pending_path()
    if not path.is_file():
        return {'replayed': 0, 'left': 0}
    try:
        lines = [ln for ln in path.read_text(encoding='utf-8').splitlines() if ln.strip()]
    except OSError:
        return {'replayed': 0, 'left': 0}
    if not lines:
        return {'replayed': 0, 'left': 0}
    if not cloud_token():
        return {'replayed': 0, 'left': len(lines)}
    replayed, left = 0, []
    for ln in lines:
        try:
            entry = json.loads(ln)
            job_ref = str(entry.get('jobRef') or '')
            if not job_ref:
                replayed += 1
                continue
            if entry.get('ok'):
                credit_settle(job_ref)
            else:
                credit_release(job_ref, str(entry.get('reason') or ''))
            replayed += 1
        except ProxyError as exc:
            if exc.code in _WALLET_CLOSED_CODES:
                replayed += 1  # 服务端已关单（含超时清扫），销账
            else:
                left.append(ln)
        except Exception:  # noqa: BLE001
            left.append(ln)
    try:
        if left:
            path.write_text('\n'.join(left) + '\n', encoding='utf-8')
        else:
            path.unlink()
    except OSError:
        pass
    return {'replayed': replayed, 'left': len(left)}
