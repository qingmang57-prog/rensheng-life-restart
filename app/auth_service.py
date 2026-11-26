# -*- coding: utf-8 -*-
"""云端账号接入（2026-11-14 重写）：注册 / 登录 / 登出 / 邮箱验证码 / 找回 / 多设备会话。

边界：
- 账号主数据只有一份，在云端 https://api.jidixh.com（服务端 SQLite）。
  本机不再建账号、不再存密码哈希、不再发邮件，买家包里不需要任何邮箱凭据。
- 本机只缓存「会话」：本机会话 token（浏览器 Cookie ip_auth）→ 云 token，
  存在工作台自己的 workbench.sqlite3（auth_cloud_sessions），
  不共用切片工坊 member.db，也不依赖常驻会员中心。
- 桌面端没有 .jidixh.com 域，云端的 Cookie 拿不到；登录/注册时从响应
  Set-Cookie 里取 ip_auth，之后用 Authorization: Bearer <token> 调云端。
- 登录态长期保持：本机会话与云 token 都是 30 天；每隔 1 小时向云端复核一次
  /api/auth/me。云端 401 立即清掉本机会话；网络不通则沿用缓存放行，
  保证断网时已登录的人还能进本地工作台，只是云端功能不可用。
- 老的本地账号表（auth_accounts / auth_tokens / auth_email_codes）原样保留，
  不删除、不迁移、不再读写，避免动到已有用户数据。
- POST /api/auth/* 在 trial_gate 只读态放行（见 trial_gate.EXEMPT_PREFIXES）。
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import http.client
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from http.cookies import SimpleCookie

import install_id
import secret_box

SESSION_TTL_DAYS = 30
COOKIE_NAME = 'ip_auth'
# 2026-11-21 差网络修复：默认超时 10→30（越南客户实测握手就要 ~30 秒），
# 配合 _cloud 内连接复用 + 自动重试 + 60 秒总预算，见 _cloud 文档串。
CLOUD_TIMEOUT = 30
VERIFY_INTERVAL = 3600
RETRY_MAX = 3
RETRY_BACKOFF = 1.5
TOTAL_BUDGET = 60
DEFAULT_BASE = 'https://api.jidixh.com'

_SCHEMA_READY = set()
_SCHEMA_LOCK = threading.Lock()


class CloudUnreachable(ValueError):
    """网络层失败：连不上、超时、DNS 解析不了。"""


class CloudError(ValueError):
    """云端返回的业务错误；文案直接用云端的中文 error。"""

    def __init__(self, message, status=400, code=''):
        super().__init__(message)
        self.status = status
        self.code = code


class CloudAuthError(CloudError):
    """401：云 token 失效，需要重新登录。"""


# ---------------------------------------------------------------- 本机表

def _ensure_schema(store):
    key = str(getattr(store, 'path', ''))
    if key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with store.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS auth_cloud_sessions (
                    token TEXT PRIMARY KEY,
                    cloud_token TEXT NOT NULL,
                    cloud_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    nickname TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    last_verified_at REAL NOT NULL DEFAULT 0,
                    user_agent TEXT NOT NULL DEFAULT '',
                    entitlements TEXT NOT NULL DEFAULT '',
                    vip_level INTEGER NOT NULL DEFAULT 0,
                    vip_expires_at REAL,
                    trial_started_at REAL,
                    trial_ends_at REAL,
                    membership_state TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS auth_cloud_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            ''')
            columns = [row['name'] for row in
                       con.execute('PRAGMA table_info(auth_cloud_sessions)').fetchall()]
            if 'entitlements' not in columns:  # 老库补列
                con.execute("ALTER TABLE auth_cloud_sessions ADD COLUMN"
                            " entitlements TEXT NOT NULL DEFAULT ''")
            # 会员/试用落本地：断网时仍能显示会员标识与试用剩余（2026-11-15）
            for name, ddl in (('vip_level', 'INTEGER NOT NULL DEFAULT 0'),
                              ('vip_expires_at', 'REAL'),
                              ('trial_started_at', 'REAL'),
                              ('trial_ends_at', 'REAL'),
                              ('membership_state', "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    con.execute('ALTER TABLE auth_cloud_sessions ADD COLUMN %s %s'
                                % (name, ddl))
            # 2026-11-16：云端令牌改用 DPAPI 加密落库。老明文行就地加密迁移，
            # 用户不需要重新登录；解不开的留在原样，等下次登录自然覆盖。
            for row in con.execute(
                    "SELECT token, cloud_token FROM auth_cloud_sessions"
                    " WHERE cloud_token<>'' AND cloud_token NOT LIKE 'enc1:%'").fetchall():
                sealed = secret_box.seal(row['cloud_token'])
                if sealed:
                    con.execute('UPDATE auth_cloud_sessions SET cloud_token=? WHERE token=?',
                                (sealed, row['token']))
        _SCHEMA_READY.add(key)


def _state_get(store, key, default=''):
    with store.connect() as con:
        row = con.execute('SELECT value FROM auth_cloud_state WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _state_set(store, key, value):
    with store.connect() as con:
        con.execute('INSERT INTO auth_cloud_state (key, value) VALUES (?,?)'
                    ' ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


# ---------------------------------------------------------------- 云端调用

def cloud_base():
    return (os.getenv('IP_API_BASE') or DEFAULT_BASE).rstrip('/')


def _cookie_token(headers):
    """从 Set-Cookie 里取 ip_auth（桌面端拿不到域 Cookie，只能读头）。"""
    try:
        raw_list = headers.get_all('Set-Cookie') or []
    except Exception:
        raw_list = []
    for raw in raw_list:
        first = str(raw).split(';', 1)[0].strip()
        name, sep, value = first.partition('=')
        if sep and name.strip() == COOKIE_NAME and value.strip():
            return value.strip()
    return ''


def _error_payload(raw):
    """云端错误体 → (人话, 业务错误码)。云端用 error/code（如 ACCOUNT_EXISTS）。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return '', ''
    if isinstance(data, dict):
        message = str(data.get('error') or data.get('message') or '')
        code = str(data.get('code') or '')
        return message, code
    return '', ''


# ---------------------------------------------------------------- 云调用基础（2026-11-21 差网络修复）
# 背景：越南客户实测单次建连 ~30 秒；旧实现 urllib 每次调用新开 TCP+TLS 握手、
# 单次尝试零重试、auth/me 窗口仅 4 秒 → 客户等 ~30 秒得到「连不上」。
# 新实现（纯标准库，零新依赖）：
# - keep-alive：线程本地连接池，同进程后续调用复用连接（省握手，好网络更快）。
# - 超时：默认 30 秒（覆盖 TCP+TLS+响应头）；auth/me 不再缩短窗口。
# - 重试：仅连接阶段失败（连不上/握手/响应头未收到）自动重试 ≤RETRY_MAX 次、
#   每次退避 RETRY_BACKOFF 秒，全程 TOTAL_BUDGET 总预算封顶；
#   服务器已应答（收到任何 HTTP 状态码）绝不重试，按原语义报错。
# - 可见：每次重试写入 retry_state() 并通知 set_retry_listener 监听器
#   （app 启动时接控制台打印）；最终不可达时错误文案带重试次数与耗时。

_RETRY_LOCK = threading.Lock()
_RETRY_LISTENER = None
_RETRY_STATE = {'path': '', 'attempt': 0, 'max': RETRY_MAX, 'at': 0.0}
_LOCAL = threading.local()


def set_retry_listener(fn):
    """注册重试通知回调 fn(note)；传 None 注销。"""
    global _RETRY_LISTENER
    _RETRY_LISTENER = fn


def retry_state():
    """读最近一次重试记录（path / attempt / max / 时间戳），供界面或日志查询。"""
    with _RETRY_LOCK:
        return dict(_RETRY_STATE)


def _note_retry(path, attempt):
    note = '网络较慢，正在自动重试(%d/%d)…' % (attempt, RETRY_MAX)
    with _RETRY_LOCK:
        _RETRY_STATE.update({'path': path, 'attempt': attempt,
                             'max': RETRY_MAX, 'at': time.time()})
    listener = _RETRY_LISTENER
    if listener:
        try:
            listener(note)
        except Exception:
            pass


def _pool_conn(base, timeout):
    """取本线程可复用的云端连接；base 或 timeout 变化时重建。"""
    parts = urlsplit(base)
    host = parts.hostname or ''
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    key = (parts.scheme, host, port)
    conn = getattr(_LOCAL, 'conn', None)
    if conn is not None and getattr(_LOCAL, 'conn_key', None) == key \
            and getattr(_LOCAL, 'conn_timeout', None) == timeout:
        return conn
    _drop_conn()
    if parts.scheme == 'https':
        conn = http.client.HTTPSConnection(
            host, port=port, timeout=timeout,
            context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port=port, timeout=timeout)
    _LOCAL.conn = conn
    _LOCAL.conn_key = key
    _LOCAL.conn_timeout = timeout
    return conn


def _drop_conn():
    conn = getattr(_LOCAL, 'conn', None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _LOCAL.conn = None
    _LOCAL.conn_key = None
    _LOCAL.conn_timeout = None


def _cloud(method, path, payload=None, cloud_token='', user_agent='', timeout=None):
    """调云端 API。成功返回 (json_dict, 新 token)；失败抛 CloudError / CloudUnreachable。

    2026-11-21 差网络修复：默认超时 30 秒；连接阶段失败自动重试 ≤3 次
    （退避 1.5 秒，全程 60 秒总预算封顶）；连接复用省握手；
    服务器已应答的失败（任何 HTTP 状态码）不重试，原样抛出。
    """
    if timeout is None:
        timeout = CLOUD_TIMEOUT
    parts = urlsplit(cloud_base() + path)
    base = (parts.scheme or 'https') + '://' + parts.netloc
    target = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Accept': 'application/json',
               'Content-Type': 'application/json',
               'User-Agent': (user_agent or 'RenshengRestartDesktop/1.0')[:200]}
    if cloud_token:
        headers['Authorization'] = 'Bearer ' + cloud_token
    deadline = time.monotonic() + TOTAL_BUDGET
    attempts = 0
    last_err = None
    result = None
    hops = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts += 1
        try:
            conn = _pool_conn(base, min(timeout, max(1, int(remaining))))
            conn.request(method, target, body=body, headers=headers)
            response = conn.getresponse()
        except (http.client.HTTPException, ConnectionError,
                TimeoutError, socket.timeout, OSError) as exc:
            # 连接阶段失败：可重试
            last_err = exc
            _drop_conn()
            if attempts >= RETRY_MAX:
                break
            if time.monotonic() + RETRY_BACKOFF >= deadline:
                break
            _note_retry(path, attempts + 1)
            time.sleep(RETRY_BACKOFF)
            continue
        try:
            raw = response.read()
        except (http.client.HTTPException, ConnectionError,
                TimeoutError, socket.timeout, OSError):
            # 服务器已应答（响应头已收到）但正文中断：不重试，如实报错
            _drop_conn()
            raise CloudUnreachable('账号服务器连接中断，请检查网络后再试。')
        status = response.code
        if 300 <= status < 400 and response.headers.get('Location') and hops < 1:
            hops += 1
            target = urlsplit(response.headers.get('Location')).path or target
            continue
        result = (status, response.headers, raw)
        break
    if result is None:
        waited = max(0, int(time.monotonic() - (deadline - TOTAL_BUDGET)))
        if last_err is not None:
            raise CloudUnreachable(
                '连不上账号服务器（已自动重试 %d 次，约 %d 秒），请检查网络后再试。'
                % (attempts, waited))
        raise CloudUnreachable('连不上账号服务器，请检查网络后再试。')
    status, resp_headers, raw = result
    if status >= 400:
        text = raw.decode('utf-8', 'replace')
        message, cloud_code = _error_payload(text)
        message = message or '云端账号服务返回异常（%s）。' % status
        code = cloud_code or str(status)
        if status == 401:
            raise CloudAuthError(message, 401, code)
        raise CloudError(message, status, code)
    token = _cookie_token(resp_headers)
    try:
        data = json.loads(raw.decode('utf-8', 'replace')) if raw else {}
    except ValueError:
        raise CloudError('账号服务器返回的内容看不懂，请稍后再试。', 502, 'bad_response')
    if not isinstance(data, dict):
        data = {}
    return data, token


def _public_user(user):
    user = user or {}
    username = str(user.get('username') or '')
    public = {
        'id': user.get('id'),
        'username': username,
        'nickname': str(user.get('nickname') or username),
        'email': str(user.get('email') or ''),
        'created_at': user.get('created_at'),
        'last_login_at': user.get('last_login_at'),
    }
    public.update(_membership_of(user))
    return public


def _membership_of(user):
    """从云端 user 提取会员/试用快照（缺字段按老账号处理，不臆造试用）。"""
    user = user or {}
    try:
        vip = int(user.get('vip_level') or 0)
    except (TypeError, ValueError):
        vip = 0
    state = str(user.get('membership_state') or '')
    trial_ends = user.get('trial_ends_at')
    if not state:  # 老云端未下发状态：按到期时间推断（vip_level 已废弃，不再作为判定条件）
        now = time.time()
        if user.get('vip_expires_at') and float(user['vip_expires_at']) > now:
            state = 'active'
        elif trial_ends:
            state = 'trial' if float(trial_ends) > now else 'expired'
        else:
            state = 'legacy'
    return {'vip_level': vip, 'vip_expires_at': user.get('vip_expires_at'),
            'trial_started_at': user.get('trial_started_at'),
            'trial_ends_at': trial_ends, 'membership_state': state}


# ---------------------------------------------------------------- 云端权益

ENTITLEMENT_KEYS = ('exports_per_month', 'max_height', 'expires_at',
                    'allow_cloud_asr', 'whitelist',
                    'asr_unlimited', 'all_style_packs', 'vip_service')


def _normalize_entitlements(raw):
    """云端权益原样归一：没下发的键就不出现，客户端不擅自补默认值。

    字段口径（与云端约定死）：
      exports_per_month  int|None  每月可导出次数，None=不限
      max_height         int|None  成片最高清晰度（像素高），None=不限
      expires_at         int|str   权益到期时间
      allow_cloud_asr    bool      是否允许走云端转写
      whitelist          bool      白名单：跳过额度与清晰度限制
      asr_unlimited      bool      会员专属：云端转写不限量（2026-11-16）
      all_style_packs    bool      会员专属：全部风格与包装套件开放
      vip_service        bool      会员专属：新功能优先体验 + 专属客服
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ('exports_per_month', 'max_height'):
        if key not in raw:
            continue
        value = raw.get(key)
        if value is None or value == '':
            out[key] = None
            continue
        try:
            out[key] = max(0, int(value))
        except (TypeError, ValueError):
            continue
    if 'expires_at' in raw:
        out['expires_at'] = raw.get('expires_at')
    for key in ('allow_cloud_asr', 'whitelist',
                'asr_unlimited', 'all_style_packs', 'vip_service'):
        if key in raw:
            out[key] = bool(raw.get(key))
    if out.get('whitelist'):
        out['exports_per_month'] = None
        out['max_height'] = None
        out['asr_unlimited'] = True
        out['all_style_packs'] = True
        out['vip_service'] = True
    return out


def _entitlements_from(data):
    """兼容两种下发位置：顶层 entitlements 或 user.entitlements。"""
    if not isinstance(data, dict):
        return {}
    ent = _normalize_entitlements(data.get('entitlements'))
    if ent:
        return ent
    user = data.get('user')
    if isinstance(user, dict):
        return _normalize_entitlements(user.get('entitlements'))
    return {}


def _row_entitlements(row):
    try:
        return _normalize_entitlements(json.loads(row['entitlements'] or '{}'))
    except (KeyError, TypeError, ValueError):
        return {}


def current_entitlements(store, handler=None, token=''):
    """当前登录账号的云端权益；没登录或云端没下发时返回 {}。"""
    token = token or (_request_token(handler) if handler is not None else '')
    _ensure_schema(store)
    row = _session_row(store, token)
    return _row_entitlements(row) if row else {}


# ---------------------------------------------------------------- 本机会话

def _HTTPCookie(token, max_age):
    return ('Set-Cookie',
            '%s=%s; HttpOnly; SameSite=Lax; Path=/; Max-Age=%d' % (COOKIE_NAME, token, max_age))


def _request_token(handler):
    raw = handler.headers.get('Cookie', '') if handler is not None else ''
    if not raw:
        return ''
    try:
        cookie = SimpleCookie()
        cookie.load(raw)
    except Exception:
        return ''
    item = cookie.get(COOKIE_NAME)
    return item.value if item else ''


def _clear_wallet_cache():
    """2026-11-18 积分真源：登录/登出/会话失效时清空云端钱包内存余额缓存，
    避免切号后看到上一个账号的余额。失败静默，不影响账号动作本身。"""
    try:
        import cloud_gateway
        cloud_gateway.wallet_cache_clear()
    except Exception:  # noqa: BLE001
        pass


def _save_session(store, cloud_token, user, user_agent, entitlements=None):
    local_token = secrets.token_urlsafe(32)
    now = time.time()
    public = _public_user(user)
    ent_json = json.dumps(entitlements or {}, ensure_ascii=False) if entitlements else ''
    with store.connect() as con:
        con.execute(
            'INSERT INTO auth_cloud_sessions (token, cloud_token, cloud_id, username, nickname,'
            ' email, created_at, expires_at, last_seen_at, last_verified_at, user_agent,'
            ' entitlements, vip_level, vip_expires_at, trial_started_at, trial_ends_at,'
            ' membership_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (local_token, secret_box.seal(cloud_token) or cloud_token, int(public['id'] or 0), public['username'],
             public['nickname'], public['email'], now, now + SESSION_TTL_DAYS * 86400,
             now, now, (user_agent or '')[:200], ent_json,
             int(public.get('vip_level') or 0), public.get('vip_expires_at'),
             public.get('trial_started_at'), public.get('trial_ends_at'),
             str(public.get('membership_state') or '')))
        con.execute('DELETE FROM auth_cloud_sessions WHERE expires_at<?', (now - 7 * 86400,))
    _state_set(store, 'seen', '1')
    _clear_wallet_cache()  # 新会话：旧账号的云端余额缓存立即作废
    _spawn_refresh_shell(store, user_agent)  # 2026-11-17 B7：登录后后台换激活凭据
    return local_token


def plain_token(value):
    """把库里的 cloud_token 还原成明文；兼容未迁移的旧明文值。只在本进程内使用。"""
    return secret_box.unseal(value)


def _session_row(store, token):
    if not token:
        return None
    with store.connect() as con:
        row = con.execute(
            'SELECT * FROM auth_cloud_sessions WHERE token=? AND expires_at>?',
            (token, time.time())).fetchone()
    if not row:
        return None
    data = dict(row)
    data['cloud_token'] = secret_box.unseal(data.get('cloud_token'))
    return data


def _drop_session(store, token):
    if not token:
        return
    with store.connect() as con:
        con.execute('DELETE FROM auth_cloud_sessions WHERE token=?', (token,))
    _clear_wallet_cache()  # 登出/401 失效：云端余额缓存一并清空


def _drop_by_email(store, email):
    key = str(email or '').strip().lower()
    if not key:
        return
    with store.connect() as con:
        con.execute('DELETE FROM auth_cloud_sessions WHERE lower(email)=?', (key,))


def _row_user(row, offline=False):
    keys = row.keys()
    return {'id': row['cloud_id'], 'username': row['username'], 'nickname': row['nickname'],
            'email': row['email'], 'created_at': None, 'last_login_at': None,
            'offline': bool(offline), 'entitlements': _row_entitlements(row),
            'vip_level': int(row['vip_level'] or 0) if 'vip_level' in keys else 0,
            'vip_expires_at': row['vip_expires_at'] if 'vip_expires_at' in keys else None,
            'trial_started_at': row['trial_started_at'] if 'trial_started_at' in keys else None,
            'trial_ends_at': row['trial_ends_at'] if 'trial_ends_at' in keys else None,
            'membership_state': (row['membership_state'] if 'membership_state' in keys
                                 else '') or 'legacy'}


def _touch(store, token, verified=False):
    now = time.time()
    with store.connect() as con:
        if verified:
            con.execute('UPDATE auth_cloud_sessions SET last_seen_at=?, last_verified_at=?'
                        ' WHERE token=?', (now, now, token))
        else:
            con.execute('UPDATE auth_cloud_sessions SET last_seen_at=? WHERE token=?',
                        (now, token))


def _verify_row(store, row):
    """向云端复核会话。返回 (user, offline)。401 抛 CloudAuthError。"""
    # 2026-11-21：不再用 4 秒短窗口（越南客户实测握手就要 ~30 秒，短窗口必超时）；
    # 默认 30 秒 + 连接失败自动重试 + 60 秒总预算，由 _cloud 统一兜底。
    data, _ = _cloud('GET', '/api/auth/me', None, row['cloud_token'])
    user = data.get('user') or {}
    public = _public_user(user)
    ent = _entitlements_from(data)
    ent_json = json.dumps(ent, ensure_ascii=False) if ent else (row['entitlements'] or '')
    with store.connect() as con:
        con.execute('UPDATE auth_cloud_sessions SET username=?, nickname=?, email=?,'
                    ' cloud_id=?, last_seen_at=?, last_verified_at=?, entitlements=?,'
                    ' vip_level=?, vip_expires_at=?, trial_started_at=?, trial_ends_at=?,'
                    ' membership_state=? WHERE token=?',
                    (public['username'], public['nickname'], public['email'],
                     int(public['id'] or row['cloud_id']), time.time(), time.time(),
                     ent_json, int(public.get('vip_level') or 0), public.get('vip_expires_at'),
                     public.get('trial_started_at'), public.get('trial_ends_at'),
                     str(public.get('membership_state') or ''), row['token']))
    public['offline'] = False
    public['id'] = public['id'] or row['cloud_id']
    public['entitlements'] = ent or _row_entitlements(row)
    save_grace(store, data)  # 2026-11-17 B3：/api/auth/me 的 72h 宽限字段落盘
    return public, False


def current_account(store, handler=None, token='', verify=None):
    """当前登录账号。

    verify=None：距上次复核超过 1 小时才问云端（普通请求走这条路，省往返）；
    verify=True：强制问一次云端——启动时 /api/auth/me 用它，保证后台改了
    权益（导出次数/清晰度）下次打开就生效；断网时沿用缓存并标记 offline。
    """
    token = token or (_request_token(handler) if handler is not None else '')
    _ensure_schema(store)
    row = _session_row(store, token)
    if not row:
        _CURRENT.membership = None
        return None
    if verify is None:
        stale = time.time() - float(row['last_verified_at'] or 0) > VERIFY_INTERVAL
    else:
        stale = bool(verify)
    if stale:
        try:
            user, _ = _verify_row(store, row)
        except CloudAuthError:
            _drop_session(store, token)
            _CURRENT.membership = None
            return None
        except (CloudUnreachable, CloudError):
            user = _row_user(row, offline=True)
    else:
        _touch(store, token)
        user = _row_user(row)
    _CURRENT.membership = _membership_snapshot(user) if user else None
    return user


class _CurrentScope(threading.local):
    """每个请求线程一份会员快照。

    原先用进程级 dict，两个账号并发（局域网双人、来回切号）时后到请求会覆盖前一个，
    另一人拿到别人的会员态与权益。threading.local 让每个服务线程各存一份。
    后台线程没有请求上下文时读到 None，按本地兜底，这是安全的默认值。
    """

    def __init__(self):
        self.membership = None


_CURRENT = _CurrentScope()


def _membership_snapshot(user):
    user = user or {}
    state = str(user.get('membership_state') or 'legacy')
    return {'state': state, 'vip_level': int(user.get('vip_level') or 0),
            'vip_expires_at': user.get('vip_expires_at'),
            'trial_started_at': user.get('trial_started_at'),
            'trial_ends_at': user.get('trial_ends_at')}


def current_membership():
    """给 license 的轻量读取：不触发 IO，返回最近一次解析的会员快照或 None。"""
    return _CURRENT.membership


# ---------------------------------------------------------------- 后台会话复核
# /api/auth/me 原先每次页面加载同步问云端（30s 超时×3 重试、60s 总预算，差网实测整页卡死）；
# 改为后台定时复核，请求路径只读本地会话行。硬闸语义不变：last_verified_at 只在云端真成功时
# 推进，offline_state 的 72h 宽限与 shell_gate 照旧读落盘时间戳，失败保留旧结果不冒充就绪。
REFRESH_INTERVAL = 60.0
_REFRESH = {'lock': threading.Lock(), 'ok': None, 'at': 0.0, 'started': False}
_REFRESH_WAKE = threading.Event()


def session_refresh_state():
    """最近一次后台复核结果（ok=None 尚未复核过；False=上次失败，旧会话数据保留）。"""
    with _REFRESH['lock']:
        return {'ok': _REFRESH['ok'], 'at': _REFRESH['at']}


def request_session_refresh():
    """请求路径非阻塞地催一次后台复核；上次尝试不足 15s 不催，避免断网时后台热循环。"""
    with _REFRESH['lock']:
        recent = time.time() - _REFRESH['at'] < 15.0
    if not recent:
        _REFRESH_WAKE.set()


def start_session_refresher(store, interval=REFRESH_INTERVAL):
    """启动预热＋周期复核（单实例守护线程）。401 与请求路径同语义：立即吊销本机会话。"""
    with _REFRESH['lock']:
        if _REFRESH['started']:
            return
        _REFRESH['started'] = True

    def _loop():
        while True:
            row = None
            try:
                row = _latest_session(store)
            except Exception:
                pass
            if row and row.get('cloud_token'):
                try:
                    _verify_row(store, row)
                    outcome = True
                except CloudAuthError:
                    try:
                        _drop_session(store, row['token'])
                    except Exception:
                        pass
                    outcome = None
                except Exception:
                    outcome = False  # 断网/云端故障：保留上次成功落盘的会话与宽限时间戳
                with _REFRESH['lock']:
                    _REFRESH['ok'] = outcome
                    _REFRESH['at'] = time.time()
            _REFRESH_WAKE.wait(interval)
            _REFRESH_WAKE.clear()

    threading.Thread(target=_loop, daemon=True, name='auth-session-refresh').start()


# ---------------------------------------------------------------- 账号动作

def request_email_code(email, purpose, user_agent):
    purpose = str(purpose or 'register')
    if purpose not in ('register', 'reset'):
        raise ValueError('验证码用途不对。')
    data, _ = _cloud('POST', '/api/auth/email-code',
                     {'email': str(email or '').strip(), 'purpose': purpose}, '',
                     user_agent)
    return {'sent': True, 'ttl': data.get('ttl'), 'resend_after': data.get('resend_after')}


def _login_for_token(username, password, user_agent):
    data, token = _cloud('POST', '/api/auth/login',
                         {'username': username, 'password': password}, '', user_agent)
    if not token:
        raise CloudError('登录成功但没拿到登录凭据，请再试一次。', 502, 'no_token')
    return data, token


def _me_snapshot(token, user, ent):
    """登录/注册后立刻问一次 /api/auth/me 补权益。

    云端 login / register 只回用户资料、不带 entitlements；不补这一步，新会话会
    先按本地 free_limits 兜底，直到页面再调 /api/auth/me 才纠正。云端不可达时
    保留登录响应里的值，不阻断登录。
    """
    if not token:
        return user, ent
    try:
        data, _ = _cloud('GET', '/api/auth/me', None, token)
    except (CloudError, CloudUnreachable):
        return user, ent
    return (data.get('user') or user), (_entitlements_from(data) or ent)


def register(store, username, password, email, code, user_agent, agreed=False):
    # 2026-11-17（PIPL 告知同意）：注册必须先勾选《服务协议》《隐私政策》。
    # 前端把勾选结果放在 agreed 里传上来；不是布尔 true 一律本地拒绝——
    # 不发验证码、不转发云端、不落会话。与 member_pay 下单同一口径，
    # 直接打接口（脚本、重放、改前端）也绕不过去。
    if agreed is not True:
        raise CloudError('请先勾选同意《服务协议》与《隐私政策》，再创建账号。',
                         400, 'AGREEMENT_REQUIRED')
    _ensure_schema(store)
    payload = {'username': str(username or '').strip(),
               'password': str(password or ''),
               'email': str(email or '').strip(),
               'code': str(code or '').strip()}
    # 本机安装号（2026-11-16）：随机串，只用于「同一台电脑不重复领试用」。
    # 取不到就留空，云端按未知设备处理，照旧给试用，不阻断注册。
    device_id = install_id.install_id(os.path.dirname(getattr(store, 'path', '') or ''))
    if device_id:
        payload['device_id'] = device_id
    data, token = _cloud('POST', '/api/auth/register', payload, '', user_agent)
    ent = _entitlements_from(data)
    user = data.get('user') or {}
    if not token:
        # 少数网关会吞掉 Set-Cookie；注册成功后补一次登录，保证本机能落到会话。
        data, token = _login_for_token(user.get('username') or username, password, user_agent)
        user = data.get('user') or user
        ent = _entitlements_from(data) or ent
    user, ent = _me_snapshot(token, user, ent)
    local_token = _save_session(store, token, user, user_agent, ent)
    return _public_user(user), local_token


def login(store, username, password, user_agent):
    _ensure_schema(store)
    data, token = _login_for_token(str(username or '').strip(), str(password or ''), user_agent)
    user, ent = _me_snapshot(token, data.get('user') or {}, _entitlements_from(data))
    local_token = _save_session(store, token, user, user_agent, ent)
    return _public_user(user), local_token


def logout(store, handler):
    token = _request_token(handler)
    row = _session_row(store, token)
    if row:
        try:
            _cloud('POST', '/api/auth/logout', {}, row['cloud_token'])
        except (CloudError, CloudUnreachable):
            pass  # 云端不可达也要把本机退掉，不能卡在门里
        _drop_session(store, token)


def list_sessions(store, handler):
    token = _request_token(handler)
    row = _session_row(store, token)
    if not row:
        raise ValueError('请先登录。')
    try:
        data, _ = _cloud('GET', '/api/auth/sessions', None, row['cloud_token'])
    except CloudAuthError:
        _drop_session(store, token)
        raise ValueError('登录已过期，请重新登录。')
    return data.get('items') or []


def revoke_session(store, handler, session_id):
    token = _request_token(handler)
    row = _session_row(store, token)
    if not row:
        raise ValueError('请先登录。')
    payload = {}
    if session_id not in (None, ''):
        try:
            payload['id'] = int(session_id)
        except (TypeError, ValueError):
            raise ValueError('设备编号不对。')
    try:
        data, _ = _cloud('POST', '/api/auth/sessions/revoke', payload, row['cloud_token'])
    except CloudAuthError:
        _drop_session(store, token)
        raise ValueError('登录已过期，请重新登录。')
    if 'id' not in payload:
        _drop_session(store, token)  # 撤销的就是当前设备
    return data


def reset_password(store, email, code, password):
    email = str(email or '').strip()
    data, _ = _cloud('POST', '/api/auth/password/reset',
                     {'email': email, 'code': str(code or '').strip(),
                      'password': str(password or '')})
    _drop_by_email(store, email)  # 云端已吊销全部 token，本机缓存一并清掉
    return data


# ---------------------------------------------------------------- 路由

def _state(store, handler):
    # 页面加载不再同步等云端（原 verify=True 差网实测卡 30s×3 重试）：只读本地会话，
    # 后台复核器负责刷新权益；offline 按最近一次后台复核是否失败标记。
    account = current_account(store, handler, verify=False)
    request_session_refresh()
    return {
        'authenticated': bool(account),
        'has_users': bool(_state_get(store, 'seen')) or _has_legacy_users(store),
        'email_configured': True,
        'cloud': cloud_base(),
        'offline': bool(account) and session_refresh_state()['ok'] is False,
        'user': _public_user(account) if account else None,
        'entitlements': (account or {}).get('entitlements') or {},
        'membership': current_membership(),
    }


def _has_legacy_users(store):
    """老版本在本机注册过账号的人，默认落在登录页而不是注册页。"""
    try:
        with store.connect() as con:
            return bool(con.execute('SELECT 1 FROM auth_accounts LIMIT 1').fetchone())
    except Exception:
        return False


def _reply(handler, payload, status=200, headers=None):
    """写响应并返回 True。app.route 用返回值判断是否已处理；handler.json 返回 None。"""
    handler.json(payload, status, headers or [])
    return True


_REDEEM_ERRORS = {
    '404': ('兑换码不存在，请核对后重试。', 'redeem_code_invalid'),
    '403': ('这个兑换码已作废，请联系管理员换一个。', 'redeem_code_void'),
    '410': ('兑换码已过期，请联系管理员换一个。', 'redeem_code_expired'),
    '409': ('这个兑换码已经兑换过了。', 'redeem_code_used'),
    '401': ('登录状态已失效，请重新登录后再兑换。', 'unauthenticated'),
    '429': ('操作太频繁，请稍后再试。', 'redeem_rate_limited'),
}


def normalize_redeem_code(raw):
    """兑换码统一成「大写、无横线」再送云端；展示层可以带 4-4-4-4。"""
    return ''.join(ch for ch in str(raw or '').upper()
                   if ch.isascii() and ch.isalnum())


def _save_row_entitlements(store, token, ent):
    with store.connect() as con:
        con.execute('UPDATE auth_cloud_sessions SET entitlements=?, last_verified_at=?'
                    ' WHERE token=?',
                    (json.dumps(ent or {}, ensure_ascii=False), time.time(), token))


def _redeem_entitlements(payload):
    ent = payload.get('entitlements')
    if isinstance(ent, dict):
        return _normalize_entitlements(ent)
    keys = ('exports_per_month', 'max_height', 'expires_at',
            'allow_cloud_asr', 'whitelist',
            'asr_unlimited', 'all_style_packs', 'vip_service')
    if any(k in payload for k in keys):
        return _normalize_entitlements({k: payload.get(k) for k in keys if k in payload})
    return {}


def redeem(store, handler):
    """云端核销兑换码。业务失败一律 200 + ok=false，前端只读中文 message。"""
    try:
        data = handler.body(limit=64 * 1024)
    except Exception:  # noqa: BLE001
        return {'ok': False, 'code': 'bad_request', 'message': '请求格式不对，请重试。'}
    code = normalize_redeem_code((data or {}).get('code'))
    if not code:
        return {'ok': False, 'code': 'redeem_code_invalid', 'message': '请输入兑换码。'}
    token = _request_token(handler)
    row = _session_row(store, token)
    if not row:
        return {'ok': False, 'code': 'unauthenticated',
                'message': '登录状态已失效，请重新登录后再兑换。'}
    try:
        payload, _new = _cloud(
            'POST', '/api/redeem', {'code': code},
            cloud_token=row['cloud_token'],
            user_agent=handler.headers.get('User-Agent', ''))
    except CloudUnreachable:
        return {'ok': False, 'code': 'cloud_unreachable',
                'message': '连不上服务器，请检查网络后再试。'}
    except CloudAuthError:
        return {'ok': False, 'code': 'unauthenticated',
                'message': '登录状态已失效，请重新登录后再兑换。'}
    except CloudError as exc:
        if str(exc.code).upper() == 'CODE_ALREADY_REDEEMED':
            return {'ok': False, 'code': 'redeem_code_used',
                    'message': '这个兑换码已经兑换过了。'}
        # 云端账号锁（2026-11-16）：直接显示云端给的剩余分钟，别盖成「操作太频繁」
        if str(exc.status) == '429' and str(exc.code).upper() == 'REDEEM_LOCKED':
            return {'ok': False, 'code': 'redeem_locked',
                    'message': str(exc) or '兑换尝试次数过多，请稍后再试。'}
        message, biz = _REDEEM_ERRORS.get(str(exc.status),
                                          ('兑换没有成功，请稍后再试，或联系管理员。',
                                           'redeem_failed'))
        return {'ok': False, 'code': biz, 'message': message}

    ent = _redeem_entitlements(payload)
    if ent:
        _save_row_entitlements(store, token, ent)
    try:
        credits = int(payload.get('credits') or payload.get('credit_amount')
                      or payload.get('balance_credits') or 0)
    except (TypeError, ValueError):
        credits = 0
    message = str(payload.get('message') or '').strip()
    if not message:
        message = '兑换成功，权益已更新。'
    return {'ok': True, 'code': 'redeemed', 'credits': credits,
            'entitlements': ent, 'message': message}


def route(handler, method, bits, query, store, data_dir):
    """挂载登录与兑换接口：/api/auth/*、/api/redeem。"""
    if method == 'POST' and bits == ['api', 'redeem']:
        _ensure_schema(store)
        return _reply(handler, redeem(store, handler))
    if bits[:2] != ['api', 'auth']:
        return False
    _ensure_schema(store)
    tail = bits[2:]
    ua = handler.headers.get('User-Agent', '')

    if method == 'GET' and tail == ['me']:
        return _reply(handler, _state(store, handler))

    if method == 'POST' and tail == ['email-code']:
        data = handler.body(limit=64 * 1024)
        result = request_email_code(data.get('email'), data.get('purpose', 'register'), ua)
        return _reply(handler, result)

    if method == 'POST' and tail == ['register']:
        data = handler.body(limit=64 * 1024)
        account, token = register(store, data.get('username'), data.get('password'),
                                  data.get('email'), data.get('code'), ua,
                                  agreed=data.get('agreed'))
        return _reply(handler, {'authenticated': True, 'has_users': True, 'user': account},
                      201, [_HTTPCookie(token, SESSION_TTL_DAYS * 86400)])

    if method == 'POST' and tail == ['login']:
        data = handler.body(limit=64 * 1024)
        account, token = login(store, data.get('username'), data.get('password'), ua)
        return _reply(handler, {'authenticated': True, 'has_users': True, 'user': account},
                      200, [_HTTPCookie(token, SESSION_TTL_DAYS * 86400)])

    if method == 'POST' and tail == ['logout']:
        logout(store, handler)
        return _reply(handler, {'authenticated': False, 'user': None}, 200,
                      [_HTTPCookie('', 0)])

    if method == 'GET' and tail == ['sessions']:
        return _reply(handler, {'items': list_sessions(store, handler)})

    if method == 'POST' and tail == ['sessions', 'revoke']:
        data = handler.body(limit=16 * 1024)
        result = revoke_session(store, handler, data.get('id'))
        return _reply(handler, result)

    # 兼容老前端路径 /api/auth/sessions/<id>/revoke
    if method == 'POST' and len(tail) == 3 and tail[0] == 'sessions' and tail[2] == 'revoke':
        result = revoke_session(store, handler, tail[1])
        return _reply(handler, result)

    if method == 'POST' and tail in (['password', 'reset'], ['password', 'reset-by-email']):
        data = handler.body(limit=64 * 1024)
        reset_password(store, data.get('email'), data.get('code'), data.get('password'))
        return _reply(handler, {'reset': True})

    raise ValueError('未知登录接口。')

# ---------------------------------------------------------------- 客户端空壳（2026-11-17）
# B7：首启用 install_id 换激活凭据（每账号 N=3 台，超配额由云端 403 决定是否拦核心生成）；
# B3：离线超过 72 小时（读 /api/auth/me 的 last_verified_at / grace_expires_at）核心生成停摆。
# 两者都只影响「核心生成」，查看与导出永远放行。凭据用 DPAPI 加密后落盘，不写日志。
SHELL_ACTIVATION_FILE = 'shell_activation.json'
SHELL_GRACE_FILE = 'shell_grace.json'
SHELL_GRACE_HOURS = 72


def _shell_dir(store):
    root = os.path.dirname(getattr(store, 'path', '') or '')
    return root or os.getenv('IP_DATA_DIR') or str(Path(__file__).resolve().parent / '.runtime')


def _shell_path(store, name):
    return os.path.join(_shell_dir(store), name)


def _shell_load(store, name):
    try:
        with open(_shell_path(store, name), encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _shell_write(store, name, data):
    try:
        path = _shell_path(store, name)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def _latest_session(store):
    """最近一次仍然有效的本机会话行（不触发网络）。"""
    try:
        with store.connect() as con:
            row = con.execute('SELECT * FROM auth_cloud_sessions WHERE expires_at>? '
                              'ORDER BY last_seen_at DESC LIMIT 1', (time.time(),)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    data = dict(row)
    data['cloud_token'] = secret_box.unseal(data.get('cloud_token'))
    return data


def save_grace(store, payload):
    """把 /api/auth/me 的离线宽限字段落盘（B3）：server_time + grace_expires_at + 本地收到时刻。"""
    payload = payload or {}
    if not payload:
        return
    data = {'saved_at': time.time(),
            'server_time': payload.get('server_time'),
            'grace_expires_at': payload.get('grace_expires_at'),
            'last_verified_at': payload.get('last_verified_at'),
            'hours': payload.get('offline_grace_hours') or SHELL_GRACE_HOURS}
    if data['grace_expires_at'] or data['server_time']:
        _shell_write(store, SHELL_GRACE_FILE, data)


def offline_state(store):
    """离线是否已超过宽限：优先用云端 grace_expires_at（带服务器时钟偏移），否则退回本地 last_verified_at+72h。"""
    grace = _shell_load(store, SHELL_GRACE_FILE)
    now = time.time()
    expires = grace.get('grace_expires_at')
    if expires:
        try:
            server = float(grace.get('server_time') or 0)
            saved = float(grace.get('saved_at') or 0)
            offset = (server - saved) if server and saved else 0.0
            if now + offset > float(expires):
                return {'stale': True, 'hours': int(grace.get('hours') or SHELL_GRACE_HOURS),
                        'message': '离线超过 %d 小时，核心生成已停用；联网校验一次登录态即恢复。'
                                   '已有内容仍可查看和导出。' % int(grace.get('hours') or SHELL_GRACE_HOURS)}
            return {'stale': False, 'hours': int(grace.get('hours') or SHELL_GRACE_HOURS)}
        except (TypeError, ValueError):
            pass
    row = _latest_session(store)
    if row:
        try:
            last = float(row.get('last_verified_at') or 0)
            if last and now - last > SHELL_GRACE_HOURS * 3600:
                return {'stale': True, 'hours': SHELL_GRACE_HOURS,
                        'message': '离线超过 %d 小时，核心生成已停用；联网校验一次登录态即恢复。'
                                   '已有内容仍可查看和导出。' % SHELL_GRACE_HOURS}
        except (TypeError, ValueError):
            pass
    return {'stale': False, 'hours': SHELL_GRACE_HOURS}


def ensure_activation(store, user_agent=''):
    """B7：用 install_id 向云端换激活凭据。成功/超配额/未登录都会落盘，供 HTTP 层判断。"""
    row = _latest_session(store)
    state = {'checked_at': time.time()}
    if not row:
        state.update({'state': 'login_required',
                      'message': '需登录云端账号后激活本机；已保存的内容仍可查看和导出。'})
        _shell_write(store, SHELL_ACTIVATION_FILE, state)
        return state
    device = install_id.install_id(os.path.dirname(getattr(store, 'path', '') or ''))
    if not device:
        state.update({'state': 'unknown', 'message': '本机安装号读取失败，暂不拦核心功能。'})
        _shell_write(store, SHELL_ACTIVATION_FILE, state)
        return state
    try:
        data, _token = _cloud('POST', '/api/activation/activate',
                              {'install_id': device,
                               'label': (os.environ.get('COMPUTERNAME') or '')[:60]},
                              row.get('cloud_token') or '', user_agent or '')
        state.update({'state': 'active', 'used': data.get('used'), 'quota': data.get('quota'),
                      'credential': secret_box.seal(str(data.get('credential') or '')) or '',
                      'message': ''})
    except CloudError as exc:
        code = str(getattr(exc, 'code', '') or '')
        if code in ('ACTIVATION_QUOTA', 'ACTIVATION_REVOKED') or int(getattr(exc, 'status', 0) or 0) == 403:
            state.update({'state': 'blocked', 'code': code, 'message': str(exc)})
        elif int(getattr(exc, 'status', 0) or 0) == 401:
            state.update({'state': 'login_required',
                          'message': '登录已失效，请重新登录后激活本机；已保存的内容仍可查看和导出。'})
        else:
            state.update({'state': 'unknown', 'message': str(exc)[:200]})
    except (CloudUnreachable, OSError):
        state.update({'state': 'unknown', 'message': '连不上云端，激活状态待校验；暂不因此拦核心功能。'})
    except Exception as exc:  # 激活永不能把软件弄崩
        state.update({'state': 'unknown', 'message': str(exc)[:200]})
    _shell_write(store, SHELL_ACTIVATION_FILE, state)
    return state


def activation_state(store):
    data = _shell_load(store, SHELL_ACTIVATION_FILE)
    if data:
        return data
    # 没有落盘记录时按「有没有本机会话」现场判：无会话=未登录（核心生成拦，查看放行）；
    # 有会话但还没换到凭据=unknown（断网/首次启动），不因此拦核心功能，交给 B3 宽限管。
    if not _latest_session(store):
        return {'state': 'login_required',
                'message': '需登录云端账号后激活本机；已保存的内容仍可查看和导出。'}
    return {'state': 'unknown', 'message': ''}


def shell_status(store):
    return {'activation': activation_state(store), 'offline': offline_state(store)}


def has_session(store):
    """本机是否有有效登录会话（不发网络请求）。空壳闸据此判断「未登录」。"""
    return _latest_session(store) is not None


def refresh_shell(store, user_agent=''):
    """启动/登录后：先复核会话（顺带落 B3 宽限），再换激活凭据。失败静默。"""
    row = _latest_session(store)
    if row and row.get('cloud_token'):
        try:
            _verify_row(store, row)
        except (CloudError, CloudUnreachable, OSError):
            pass
        except Exception:
            pass
    return ensure_activation(store, user_agent)


def _spawn_refresh_shell(store, user_agent=''):
    try:
        threading.Thread(target=lambda: refresh_shell(store, user_agent), daemon=True).start()
    except Exception:
        pass
