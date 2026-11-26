"""发布回执核验（P0-3）：把「本人回填的作品链接」解析成作品 ID 与核验等级；可选「轻核验」。

解析只做离线可证的判断（2026-11-16 起允许链接带 ?query，canonical_url 仍去参数归一）：
- self_reported：本人自述，链接未解析（历史回执/非抖音平台）
- link_parsed  ：链接符合平台作品页规则，已解析出作品 ID
- verified     ：轻核验打开作品公开页，标题/作者比对通过
- unverified   ：轻核验结论为「作品不存在/标题对不上」（比对不通过）

轻核验（可选开关，任务书 §4 P0-3）：
- 用已有登录态（没有就游客态）打开作品公开页，只读页面自身已签名的
  aweme/detail 响应与页面文案，取标题/作者做比对；
- 失败只做一次，不重试轰炸；被平台拦截（auth_required/risk_control）、
  签名异常或浏览器环境缺失时如实回报（ok=None），由调用方保持原核验等级，
  不得降级成「比对不通过」——skip 不是比对不通过；
- 全程只读公开页面：不点击、不发布、不把 cookie/密钥值写进日志或结果
  （登录态只以 douyin_session.describe 的摘要形式出现）。

解析失败统一返回 {ok: False, error_code, error_message}，error_message 直接给用户看；
调用方（release_service.register）对抖音解析失败仍保存 URL，附 unparsed_post_url 警告。
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import contracts

# 作品页链接规则：只认 HTTPS + 平台域名 + 作品页路径，创作后台/账号主页一律不算。
# 2026-11-16 放宽：允许 ?query（App 分享链接常带 previous_page=app_code_link 等参数），
# 解析出的 canonical_url 仍然只留净链接（去 query、去 fragment）。
_REF_RULES = {
    'douyin': (
        re.compile(r'^https://([a-z0-9-]+\.)?douyin\.com/(?:video|note)/(\d{5,25})/?(?:\?.*)?$'),
    ),
}

PLATFORM_NAMES = {'douyin': '抖音'}

# 全局固定错误码：前端可据此换文案，后端日志也能按码统计。
ERROR_LINK_MISSING = 'link_missing'
ERROR_LINK_INVALID = 'link_invalid'
ERROR_PLATFORM_UNSUPPORTED = 'platform_unsupported'

# --- 轻核验 ------------------------------------------------------------------

# 与 bench_collect 同源常量（采集线已验证过，勿散写第二份值）：
DETAIL_MARKER = 'aweme/v1/web/aweme/detail/'  # 作品详情页加载时浏览器自动发出的接口路径
LIGHT_CHECK_DEFAULT_TIMEOUT = 75  # 单次轻核验总预算（秒）；超时算「环境不可用」，不重试

# 页面文案信号（作品被删/私密/不可见，以及风控验证页）。
BODY_NOT_FOUND_KEYS = ('作品不存在', '找不到该作品', '已删除', '内容无法查看',
                       '暂时无法查看', '走丢了', '不存在或不可见')
BODY_BLOCK_KEYS = ('安全验证', '滑动验证', '验证中心', '异常访问', '请完成验证')

# 轻核验结果码（写进 platform_work.last_error_code 的取值域）：
WORK_NOT_FOUND = 'work_not_found'
TITLE_MISMATCH = 'title_mismatch'
INCONCLUSIVE = 'inconclusive'


def _result(ok, platform, video_id=None, url=None, verification=None,
            error_code=None, error_message=None):
    return {'ok': ok, 'platform': platform, 'platform_video_id': video_id,
            'canonical_url': url, 'verification': verification,
            'error_code': error_code, 'error_message': error_message,
            'checked_at': contracts.now_ms()}


def parse_post_ref(platform, url):
    """解析作品链接，返回三态：link_missing / link_invalid / link_parsed（正常）。"""
    url = str(url or '').strip()
    if not url:
        return _result(False, platform, error_code=ERROR_LINK_MISSING,
                       error_message='缺少作品链接：请粘贴发布后的作品页链接。')
    rules = _REF_RULES.get(platform)
    if not rules:
        return _result(False, platform, error_code=ERROR_PLATFORM_UNSUPPORTED,
                       error_message='该平台暂不支持链接核验：仍是待验收占位，链接不会写入作品表。')
    for rule in rules:
        match = rule.match(url)
        if match:
            return _result(True, platform, match.group(2),
                           'https://www.douyin.com/%s/%s' % ('video', match.group(2)),
                           'link_parsed')
    name = PLATFORM_NAMES.get(platform, platform)
    return _result(False, platform, error_code=ERROR_LINK_INVALID,
                   error_message=f'链接格式不对：请填{name}作品页链接（形如 '
                                 f'https://www.douyin.com/video/数字），不要填创作后台或账号主页。')


def _normalize_title(text):
    """标题归一：去话题标签、空白与标点符号（保留中英文与数字），转小写。"""
    t = str(text or '').lower()
    t = re.sub(r'#\S+', ' ', t)
    t = re.sub(r'[\s\u3000]+', '', t)
    t = re.sub(r'[^\w\u4e00-\u9fff]+', '', t)
    return t


def _titles_match(expected, actual):
    """标题比对：归一后互相包含，或 difflib 相似度 ≥0.6（容错截断/省略号）。"""
    a, b = _normalize_title(expected), _normalize_title(actual)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def light_verify(platform, url, expected_title=None, timeout=None):
    """轻核验：单次尝试，打开作品公开页比对标题/作者。绝不重试。

    返回 {ok, verification, error_code, error_message, work_title, work_author,
          login_state, classification, page_url, checked_at}：
    - ok=True  verification='verified'（比对通过）
    - ok=False verification='unverified'（作品不存在/标题对不上——这是比对结论）
    - ok=None  verification=None（被拦/环境不可用/不确定——调用方保持原等级并留痕）
    """
    checked_at = contracts.now_ms()
    timeout = int(timeout or LIGHT_CHECK_DEFAULT_TIMEOUT)
    base = {'platform': platform, 'url': url, 'mode': 'browser_light_check',
            'checked_at': checked_at}
    parsed = parse_post_ref(platform, url)
    target = parsed['canonical_url'] or url
    base['canonical_url'] = target
    if not parsed['ok']:
        return {**base, 'ok': None, 'verification': None,
                'error_code': parsed['error_code'],
                'error_message': '链接未解析，轻核验未执行：%s' % parsed['error_message']}
    proc = _run_check_work(target, timeout)
    if proc is None:  # 浏览器环境缺失
        return {**base, 'ok': None, 'verification': None,
                'error_code': 'browser_env_missing',
                'error_message': '轻核验未执行：视频解析所需的浏览器运行环境没有随包安装（缺少 .runtime/browser-env）。'}
    stdout = proc.stdout.decode('utf-8', 'replace')
    payload = None
    try:
        payload = json.loads(stdout[stdout.index('{'):])
    except Exception:
        payload = None
    if payload is None:
        stderr = proc.stderr.decode('utf-8', 'replace')[-400:]
        return {**base, 'ok': None, 'verification': None,
                'error_code': 'inconclusive',
                'error_message': '轻核验无有效输出，无法下结论（%s）%s' % (
                    _proc_exit_text(proc), ('：' + stderr) if stderr.strip() else '')}
    login_state = payload.get('login_state') or ''
    if payload.get('ok') is not True:
        return {**base, 'ok': None, 'verification': None, 'login_state': login_state,
                'error_code': 'inconclusive',
                'error_message': '轻核验未能完成：%s' % (payload.get('error') or '未知原因')}
    classification = payload.get('classification') or INCONCLUSIVE
    work_title = payload.get('work_title') or ''
    work_author = payload.get('work_author') or ''
    page_url = payload.get('page_url') or ''
    base.update({'login_state': login_state, 'classification': classification,
                 'work_title': work_title, 'work_author': work_author,
                 'page_url': page_url})
    if classification == 'found':
        matched = _titles_match(expected_title, work_title) if expected_title else None
        if matched is True:
            return {**base, 'ok': True, 'verification': 'verified',
                    'error_code': None, 'error_message': None}
        if matched is False:
            return {**base, 'ok': False, 'verification': 'unverified',
                    'error_code': TITLE_MISMATCH,
                    'error_message': '轻核验不通过：作品存在，但页面标题与登记标题对不上。'}
        return {**base, 'ok': None, 'verification': None,
                'error_code': None,
                'error_message': '作品存在；未提供登记标题，只回报存在性，不升级核验等级。'}
    if classification == WORK_NOT_FOUND:
        return {**base, 'ok': False, 'verification': 'unverified',
                'error_code': WORK_NOT_FOUND,
                'error_message': '轻核验不通过：作品不存在或已不可见。'}
    # auth_required / risk_control / signature_broken / inconclusive：如实回报，不下比对结论。
    texts = {
        'auth_required': '被平台拦下（游客态被拒/需要登录），轻核验未完成，不下比对结论。',
        'risk_control': '触发平台安全验证/风控，轻核验未完成，不下比对结论。',
        'signature_broken': '作品详情接口结构异常（可能已改版），轻核验未完成，不下比对结论。',
    }
    return {**base, 'ok': None, 'verification': None,
            'error_code': classification if classification != INCONCLUSIVE else INCONCLUSIVE,
            'error_message': texts.get(classification, '页面信号不足以判断，轻核验未完成，不下比对结论。')}


def _proc_exit_text(proc):
    return '退出码 %s' % proc.returncode


def _run_check_work(url, timeout):
    """起子进程执行 check-work；返回 CompletedProcess，浏览器环境缺失返回 None。"""
    import video_intake
    py = video_intake.browser_python()
    if not Path(py).is_file():
        return None
    script_dir = Path(__file__).resolve().parent
    tmp_dir = script_dir / '.runtime' / 'tmp'
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
                'TEMP': str(tmp_dir), 'TMP': str(tmp_dir)})
    started = time.time()
    proc = subprocess.run(
        [str(py), str(Path(__file__).resolve()), 'check-work', '--url', url],
        cwd=str(script_dir), env=env, timeout=max(timeout, 10),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    _ = started
    return proc


def _fail(message, **extra):
    """子进程出口：环境/崩溃类失败，输出 JSON 后以非零码退出。"""
    print(json.dumps({'ok': False, 'error': str(message)[:700], **extra},
                     ensure_ascii=False))
    raise SystemExit(1)


def _classify_detail_payload(payload):
    """对 aweme/detail 响应做分类：found / work_not_found / auth 系分支，与 bench_collect 语义对齐。

    2026-11-16 真机核实：作品不存在时接口回 200 + aweme_detail 只回显 aweme_id 的空壳
    （desc/author 全空），因此「found」必须看 desc 或 author.nickname，不能看 aweme_id；
    空壳 {"status_code":0}（无 aweme_detail）是游客拒绝信号 → auth_required，不是「作品不存在」；
    status_code 非零且带签名字样 → signature_broken。
    """
    if not isinstance(payload, dict):
        return 'signature_broken', None, None
    detail = payload.get('aweme_detail')
    if isinstance(detail, dict):
        author = (detail.get('author') or {}).get('nickname') or ''
        desc = (detail.get('desc') or '').strip()
        if desc or author:
            return 'found', desc, author
        if detail.get('aweme_id'):
            # 只有回显 ID 的空壳：作品不可见（不存在/已删/私密）——比对不通过，不是被拦截。
            return WORK_NOT_FOUND, None, None
    status_code = payload.get('status_code')
    if status_code not in (0, '0', None):
        msg = str(payload.get('status_msg') or payload.get('message') or '')
        if '签名' in msg or 'signature' in msg.lower():
            return 'signature_broken', None, None
        return 'auth_required', None, None
    return 'auth_required', None, None  # 空壳：游客拒绝


def cmd_check_work(args):
    """子进程入口：打开作品公开页，读已签名 detail 响应与页面文案，输出 JSON。

    分类：found / work_not_found / auth_required / risk_control / signature_broken /
    inconclusive。只读，不滚动采集、不点击、不保存 cookie 值。
    """
    from patchright.sync_api import sync_playwright
    import browser_fetch
    import douyin_session
    import bench_collect
    hits = []
    outcome = {'ok': True, 'found': False, 'classification': INCONCLUSIVE,
               'work_title': '', 'work_author': '', 'page_url': '',
               'login_state': '', 'detail_hits': 0, 'elapsed_ms': 0}
    started = time.time()
    with sync_playwright() as p:
        launch_options = {'headless': True,
                          'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled']}
        executable = browser_fetch.browser_executable()
        if executable:
            launch_options['executable_path'] = executable
        browser = p.chromium.launch(**launch_options)
        try:
            context = browser.new_context(user_agent=bench_collect.UA,
                                          viewport={'width': 1280, 'height': 800},
                                          locale='zh-CN')
            try:
                state = douyin_session.attach(context)
                outcome['login_state'] = douyin_session.describe(state)
            except Exception:
                outcome['login_state'] = '登录态读取失败（按游客态继续）'
            page = context.new_page()
            page.on('response', bench_collect._capture_into(hits, markers=(DETAIL_MARKER,)))
            try:
                page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                _fail('作品页打开失败（网络或页面错误）：%s: %s' % (type(exc).__name__, str(exc)[:200]))
            page.wait_for_timeout(max(getattr(args, 'wait_ms', 0) or 0, 2500))
            # 轮询至多 12s 等 detail 响应（页面首帧后接口通常立即返回）。
            deadline = time.time() + 12
            while time.time() < deadline and not hits:
                page.wait_for_timeout(500)
            outcome['page_url'] = page.url
            outcome['detail_hits'] = len(hits)
            if hits:
                found_cls = (None, None, None)  # 哨兵：只有真 found 才置位，不可隐式成立
                for hit in hits:
                    cls, title, author = _classify_detail_payload(hit.get('json'))
                    if cls == 'found':
                        found_cls = (cls, title, author)
                        break
                    outcome['classification'] = cls
                if found_cls[0] == 'found':
                    outcome.update({'found': True, 'classification': 'found',
                                    'work_title': found_cls[1], 'work_author': found_cls[2]})
            if not outcome['found']:
                try:
                    body_text = (page.inner_text('body', timeout=8000) or '')[:20000]
                except Exception:
                    body_text = ''
                if any(k in body_text for k in BODY_NOT_FOUND_KEYS):
                    outcome['classification'] = WORK_NOT_FOUND
                elif any(k in body_text for k in BODY_BLOCK_KEYS):
                    outcome['classification'] = 'risk_control'
            outcome['elapsed_ms'] = int((time.time() - started) * 1000)
            print(json.dumps(outcome, ensure_ascii=False))
        finally:
            browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='发布回执核验（解析 + 可选轻核验）')
    sub = parser.add_subparsers(dest='cmd')
    check = sub.add_parser('check-work', help='轻核验子进程：打开作品公开页做只读检查')
    check.add_argument('--url', required=True)
    check.add_argument('--wait-ms', type=int, default=2500)
    args = parser.parse_args(argv)
    if args.cmd == 'check-work':
        cmd_check_work(args)
        return 0
    parser.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
