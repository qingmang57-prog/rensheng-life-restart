# -*- coding: utf-8 -*-
"""统一平台会话层（P0-1）：把两处抖音登录态收敛成一个对外状态契约。

现有两套会话都复用，不新造第二套：
- douyin_cookie.json：内容采集/打开详情页用的 cookie 文件（douyin_session.py 负责解析）；
- publish/douyin-session.json：发布执行器（Playwright storage_state）用的会话文件。

对外只给固定契约：
{status: ok|missing|expired|need_rescan, account_name, last_checked_at, expires_at, reason}
外加 sources 明细（两处会话各自状态）。只读本地文件判定，不发网络请求；
任何返回与异常文案都不包含 cookie 值。
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

import douyin_session

DATA = Path(os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent/'.runtime')))
COOKIE_FILE = douyin_session.COOKIE_FILE
PUBLISH_SESSION = DATA/'publish'/'douyin-session.json'

STATUS_OK = 'ok'
STATUS_MISSING = 'missing'
STATUS_EXPIRED = 'expired'
STATUS_NEED_RESCAN = 'need_rescan'

# 严重程度：任一来源要到这个状态，整体就按它报（need_rescan 最需要人处理）。
_SEVERITY = (STATUS_OK, STATUS_EXPIRED, STATUS_NEED_RESCAN, STATUS_MISSING)


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec='seconds')
    except (TypeError, ValueError, OSError):
        return None


def _mtime(path):
    try:
        return _iso(Path(path).stat().st_mtime)
    except OSError:
        return None


def _cookie_source(path):
    """采集用 cookie 文件：复用 douyin_session.load，invalid 统一映射为 need_rescan。"""
    cookies, state = douyin_session.load(path)
    kind = state.get('state')
    status = {'ok': STATUS_OK, 'missing': STATUS_MISSING,
              'expired': STATUS_EXPIRED}.get(kind, STATUS_NEED_RESCAN)
    expires = [c['expires'] for c in cookies
               if isinstance(c.get('expires'), (int, float)) and c['expires'] > 0]
    reason = '' if status == STATUS_OK else (state.get('reason') or {
        STATUS_MISSING: '还没有采集用 cookie 文件（douyin_cookie.json）',
        STATUS_EXPIRED: '采集用登录态已过期',
        STATUS_NEED_RESCAN: '采集用登录态文件不可用',
    }.get(status, ''))
    return {'source': 'douyin_cookie.json', 'status': status,
            'last_checked_at': _mtime(path),
            'expires_at': _iso(min(expires)) if expires else None,
            'reason': reason}


def _publish_source(path):
    """发布用 storage_state：只认 sessionid；文件坏了要求重扫，不猜。"""
    path = Path(path)
    if not path.is_file():
        return {'source': 'douyin-session.json', 'status': STATUS_MISSING,
                'last_checked_at': None, 'expires_at': None, 'reason': '还没有发布会话文件'}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {'source': 'douyin-session.json', 'status': STATUS_NEED_RESCAN,
                'last_checked_at': _mtime(path), 'expires_at': None,
                'reason': '会话文件无法解析，请重新扫码登录'}
    cookies = data.get('cookies') if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return {'source': 'douyin-session.json', 'status': STATUS_NEED_RESCAN,
                'last_checked_at': _mtime(path), 'expires_at': None,
                'reason': '会话文件缺少 cookies 列表，请重新扫码登录'}
    ids = [c for c in cookies if isinstance(c, dict)
           and c.get('name') == 'sessionid' and c.get('value')]
    if not ids:
        return {'source': 'douyin-session.json', 'status': STATUS_NEED_RESCAN,
                'last_checked_at': _mtime(path), 'expires_at': None,
                'reason': '会话里没有 sessionid，请重新扫码登录'}
    expires = [c['expires'] for c in ids if isinstance(c.get('expires'), (int, float)) and c['expires'] > 0]
    if expires and max(expires) < time.time():
        return {'source': 'douyin-session.json', 'status': STATUS_EXPIRED,
                'last_checked_at': _mtime(path), 'expires_at': _iso(max(expires)),
                'reason': '发布会话已过期，请重新扫码登录'}
    return {'source': 'douyin-session.json', 'status': STATUS_OK,
            'last_checked_at': _mtime(path),
            'expires_at': _iso(max(expires)) if expires else None, 'reason': ''}


def state(platform='douyin', account_name='', cookie_file=None, session_file=None):
    """返回统一会话状态；platform 只支持抖音（其余平台仍是待验收占位）。"""
    platform = str(platform or 'douyin')
    if platform != 'douyin':
        return {'platform': platform, 'status': STATUS_MISSING, 'account_name': account_name,
                'last_checked_at': None, 'expires_at': None,
                'reason': '该平台暂未接入会话层（待验收），请先在平台后台手动登录。',
                'sources': []}
    sources = [_cookie_source(cookie_file or COOKIE_FILE),
               _publish_source(session_file or PUBLISH_SESSION)]
    worst = max((s['status'] for s in sources), key=_SEVERITY.index)
    checked = [s['last_checked_at'] for s in sources if s['last_checked_at']]
    expiring = [s['expires_at'] for s in sources if s['expires_at']]
    problems = [f'{s["source"]}：{s["reason"]}' for s in sources
                if s['status'] != STATUS_OK and s['reason']]
    return {'platform': platform, 'status': worst, 'account_name': account_name,
            'last_checked_at': max(checked) if checked else None,
            'expires_at': min(expiring) if expiring else None,
            'reason': '；'.join(problems), 'sources': sources}


def describe(result):
    """状态翻成一句给用户看的话（不含任何凭据）。"""
    status = (result or {}).get('status')
    if status == STATUS_OK:
        return '会话可用。'
    if status == STATUS_EXPIRED:
        return '登录态已过期，请重新扫码登录一次。'
    if status == STATUS_NEED_RESCAN:
        return '登录态文件不可用，请重新扫码登录一次：%s' % (result.get('reason') or '格式无法识别')
    return '还没有登录态，请先扫码登录一次。'


def save(cookies, session_file=None, origins=None):
    """保存发布会话（storage_state）；只留必要字段，写入走临时文件再替换。"""
    path = Path(session_file or PUBLISH_SESSION)
    keep = ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite')
    clean = [{k: c[k] for k in keep if k in c} for c in cookies or [] if c.get('name')]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps({'cookies': clean, 'origins': list(origins or [])},
                              ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    return {'path': str(path), 'count': len(clean)}


def clear(session_file=None, remove_cookie_file=False, cookie_file=None):
    """清除会话文件；cookie 文件默认不动，必须显式要求才删。"""
    removed = []
    for path, enabled in ((Path(session_file or PUBLISH_SESSION), True),
                          (Path(cookie_file or COOKIE_FILE), bool(remove_cookie_file))):
        if enabled and path.is_file():
            path.unlink()
            removed.append(str(path))
    return {'removed': removed}
