# -*- coding: utf-8 -*-
"""抖音登录态（cookie）本地读取与注入。

安全边界（硬约束）：
- 只从运行目录 `<data>/douyin_cookie.json` 读取，路径固定，不接受用户传入任意路径；
- 不写源码目录、不进交付包（tools/scan_forbidden.py 已列入排除清单）；
- 不上传任何服务器，只在本机浏览器的 context 里注入。

支持四种写法（都很小，方便用户自己放）：
1. Playwright 列表：[{"name":"sessionid","value":"...","domain":".douyin.com","path":"/"}]
2. {"cookies": [...]}
3. 纯键值对象：{"sessionid": "...", "ttwid": "..."}
4. DevTools 复制的 Cookie 头字符串："sessionid=...; ttwid=..."
"""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))
COOKIE_FILE = DATA / 'douyin_cookie.json'
COOKIE_DOMAIN = '.douyin.com'
LOGIN_COOKIE_NAMES = ('sessionid', 'sessionid_ss', 'sid_tt', 'passport_csrf_token')

LOGIN_HINT = (
    '抖音需要登录态才能稳定读取这条内容。请先登录一次：'
    '双击包内「登录抖音.bat」，用抖音 App 扫码登录；'
    '登录态会自动保存到 %s（只留本机、不进交付包、不上传服务器）。' % COOKIE_FILE
)


def _cookie_list(raw):
    """把四种写法统一成 playwright 可用的 cookie 字典列表。"""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict) and x.get('name')]
    if isinstance(raw, dict):
        if isinstance(raw.get('cookies'), list):
            return [x for x in raw['cookies'] if isinstance(x, dict) and x.get('name')]
        pairs = []
        for name, value in raw.items():
            if isinstance(value, str) and value.strip():
                pairs.append({'name': str(name), 'value': value.strip(),
                              'domain': COOKIE_DOMAIN, 'path': '/'})
        return pairs
    if isinstance(raw, str):
        pairs = []
        for part in raw.split(';'):
            if '=' not in part:
                continue
            name, _, value = part.partition('=')
            name, value = name.strip(), value.strip()
            if name and value:
                pairs.append({'name': name, 'value': value,
                              'domain': COOKIE_DOMAIN, 'path': '/'})
        return pairs
    return []


def load(path=None):
    """返回 (cookies, state)。state: ok / missing / expired / invalid。

    path 只给测试与统一会话层显式指定用；默认仍是固定 COOKIE_FILE，不接受用户传入。
    """
    path = Path(path) if path else COOKIE_FILE
    if not path.is_file():
        return [], {'state': 'missing', 'path': str(path)}
    try:
        text = path.read_text(encoding='utf-8-sig')
    except OSError as exc:
        return [], {'state': 'invalid', 'path': str(path),
                    'reason': '%s: %s' % (type(exc).__name__, exc)}
    try:
        raw = json.loads(text)
    except ValueError:
        raw = text  # 兼容从 DevTools 直接复制的 Cookie 头字符串
    cookies = _cookie_list(raw)
    if not cookies:
        return [], {'state': 'invalid', 'path': str(path),
                    'reason': '文件里没有解析出任何 cookie'}
    names = {c['name'] for c in cookies}
    if not names & set(LOGIN_COOKIE_NAMES):
        return [], {'state': 'invalid', 'path': str(path),
                    'reason': '缺少登录必需字段（%s）' % '/'.join(LOGIN_COOKIE_NAMES)}
    now = time.time()
    expired = [c['name'] for c in cookies
               if isinstance(c.get('expires'), (int, float)) and c['expires'] > 0 and c['expires'] < now]
    if expired and not [c for c in cookies if isinstance(c.get('expires'), (int, float))
                        and c['expires'] > now]:
        return [], {'state': 'expired', 'path': str(path),
                    'reason': '登录态已过期（%s）' % '、'.join(expired)}
    return cookies, {'state': 'ok', 'path': str(path), 'count': len(cookies)}


def attach(context):
    """把本地登录态注入浏览器 context；返回状态字典（不含 cookie 值）。"""
    cookies, state = load()
    if not cookies:
        return state
    try:
        context.add_cookies(cookies)
    except Exception as exc:
        return {'state': 'invalid', 'path': str(COOKIE_FILE),
                'reason': 'cookie 注入失败：%s: %s' % (type(exc).__name__, exc)}
    return state


def save(cookies):
    """登录助手用：把浏览器 cookie 落到运行目录，只保留必要字段。"""
    DATA.mkdir(parents=True, exist_ok=True)
    keep = ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite')
    clean = [{k: c[k] for k in keep if k in c} for c in cookies if c.get('name')]
    tmp = COOKIE_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(COOKIE_FILE)
    return str(COOKIE_FILE), len(clean)


def describe(state):
    """把状态翻成一句人话，供任务失败提示直接使用。"""
    kind = (state or {}).get('state')
    if kind == 'ok':
        return '已使用本机登录态（%d 条 cookie）' % (state.get('count') or 0)
    if kind == 'missing':
        return LOGIN_HINT
    if kind == 'expired':
        return '抖音登录态已过期，请重新登录一次（双击「登录抖音.bat」）。'
    if kind == 'invalid':
        return '抖音登录态文件不可用（%s），请重新登录一次（双击「登录抖音.bat」）。' % (
            state.get('reason') or '格式无法识别')
    return ''
