# -*- coding: utf-8 -*-
"""打卡云端上报（2026-11-14）：只报日期，失败静默。

边界（与用户定案一致）：
- 只上送一个北京时间日期（YYYY-MM-DD），不带备注、发布链接、成片路径、
  知识库或成长档案的任何内容；
- 本地打卡先落盘，再异步上报；未登录 / 断网 / 云端 5xx 一律静默跳过，
  绝不影响打卡本身，也不弹错误；
- 上报失败在 checkin_report_log 留标记（只存日期、云账号 id、错误摘要），
  下次启动补报最近 RETRY_DAYS 天；
- 幂等：云端返回 already=true 当成功，本地清除标记。
"""
import re
import threading
import time

import auth_service

RETRY_DAYS = 7
TIMEOUT = 30
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

_SCHEMA_READY = set()
_SCHEMA_LOCK = threading.Lock()


def _ensure_schema(store):
    key = str(getattr(store, 'path', ''))
    if key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        with store.connect() as con:
            con.execute('''
                CREATE TABLE IF NOT EXISTS checkin_report_log (
                    date_key TEXT NOT NULL,
                    cloud_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (date_key, cloud_id)
                )''')
        _SCHEMA_READY.add(key)


def _current_row(store, handler=None):
    """当前请求的云账号；启动补报时取最近活跃的未过期会话。没登录返回 None。"""
    try:
        auth_service._ensure_schema(store)
    except Exception:
        return None
    if handler is not None:
        # 请求里没带有效登录态就是未登录，绝不回落到别的账号。
        return auth_service._session_row(store, auth_service._request_token(handler))
    with store.connect() as con:
        return con.execute(
            'SELECT * FROM auth_cloud_sessions WHERE expires_at>?'
            ' ORDER BY last_seen_at DESC LIMIT 1', (time.time(),)).fetchone()


def queue(store, date_key, handler=None):
    """本地打卡成功后调用：先登记待上报，再起后台线程发送。返回是否登记成功。"""
    date_key = str(date_key or '')
    if not _DATE_RE.match(date_key):
        return False
    try:
        row = _current_row(store, handler)
        if not row:
            return False  # 未登录：什么都不留，静默跳过
        cloud_id = int(row['cloud_id'] or 0)
        _ensure_schema(store)
        now = time.time()
        with store.connect() as con:
            con.execute(
                'INSERT INTO checkin_report_log (date_key, cloud_id, status, attempts,'
                ' last_error, created_at, updated_at) VALUES (?,?,?,0,\'\',?,?)'
                ' ON CONFLICT(date_key, cloud_id) DO UPDATE SET updated_at=excluded.updated_at',
                (date_key, cloud_id, 'pending', now, now))
        threading.Thread(target=_send_pending, args=(store, auth_service.plain_token(row['cloud_token']), cloud_id),
                         daemon=True, name='checkin-report').start()
        return True
    except Exception:
        return False  # 上报永远不能影响打卡


def _post(cloud_token, date_key):
    """真正的一次上报：请求体只有 date 一个字段。"""
    data, _token = auth_service._cloud('POST', '/api/checkin', {'date': date_key},
                                       cloud_token=cloud_token, timeout=TIMEOUT)
    return data


def _clear(store, cloud_id, date_key):
    with store.connect() as con:
        con.execute('DELETE FROM checkin_report_log WHERE cloud_id=? AND date_key=?',
                    (cloud_id, date_key))


def _mark_failed(store, cloud_id, date_key, reason):
    with store.connect() as con:
        con.execute('UPDATE checkin_report_log SET status=?, attempts=attempts+1,'
                    ' last_error=?, updated_at=? WHERE cloud_id=? AND date_key=?',
                    ('pending', str(reason or '')[:200], time.time(), cloud_id, date_key))


def _send_pending(store, cloud_token, cloud_id):
    """补报该账号最近 RETRY_DAYS 天的待上报日期；单条失败不阻断其他条。"""
    cutoff = time.strftime('%Y-%m-%d', time.localtime(time.time() - (RETRY_DAYS - 1) * 86400))
    try:
        with store.connect() as con:
            rows = con.execute(
                'SELECT date_key FROM checkin_report_log WHERE cloud_id=? AND date_key>=?'
                ' ORDER BY date_key', (cloud_id, cutoff)).fetchall()
    except Exception:
        return
    for row in rows:
        date_key = row['date_key']
        try:
            data = _post(cloud_token, date_key)
        except auth_service.CloudAuthError:
            return  # 登录失效：保留标记，等下次登录后的启动再补
        except Exception as exc:  # 断网、5xx、解析失败一律静默留标记
            _mark_failed(store, cloud_id, date_key, exc)
            continue
        try:
            _clear(store, cloud_id, date_key)
        except Exception:
            pass


def warmup(store):
    """启动时补报：不占启动路径，后台线程跑；没登录或无标记时不发任何请求。"""
    try:
        row = _current_row(store, None)
        if not row:
            return
        _ensure_schema(store)
        cloud_id = int(row['cloud_id'] or 0)
        with store.connect() as con:
            pending = con.execute(
                'SELECT 1 FROM checkin_report_log WHERE cloud_id=? LIMIT 1', (cloud_id,)).fetchone()
        if not pending:
            return
        threading.Thread(target=_send_pending, args=(store, auth_service.plain_token(row['cloud_token']), cloud_id),
                         daemon=True, name='checkin-report-warm').start()
    except Exception:
        pass
