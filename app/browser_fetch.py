"""Browser-based douyin page fetch. Runs under .runtime/browser-env python (patchright).

The browser executes the platform challenge and computes signatures itself, so no signature
maintenance is needed (TikTokDownloader stopped maintaining signature code for compliance).
Pattern learned from dreammis/social-auto-upload (MIT): real chromium, normal user agent,
read the page's embedded JSON, and reuse the page's own session for the public detail API
(the same endpoint yt-dlp's douyin extractor uses). No cookie harvesting, no challenge solving.
"""
import argparse
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import douyin_session

# Crash-trace log: survives even if the process dies before printing (empty stdout cases).
_LOG = Path(__file__).resolve().parent / 'browser_fetch.log'

def _log(msg):
    try:
        with open(_LOG, 'a', encoding='utf-8') as f:
            f.write(msg.rstrip('\n') + '\n')
    except Exception:
        pass

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_BLOCK = 12 * 1024 * 1024

API_PROBE = """
async (awemeId) => {
    const params = new URLSearchParams({
        aweme_id: awemeId, aid: '1128', version_name: '23.5.0', device_platform: 'webapp',
        os_version: '10', channel: 'channel_pc_web', pc_client_type: '1', browser_language: 'zh-CN',
        browser_platform: 'Win32', browser_name: 'Chrome', browser_version: '131.0.0.0',
        engine_name: 'Blink', engine_version: '131.0.0.0', os_name: 'Windows', cpu_core_num: '8',
        device_memory: '8', platform: 'PC', downlink: '10', effective_type: '4g', round_trip_time: '50'
    });
    try {
        const res = await fetch('/aweme/v1/web/aweme/detail/?' + params, {credentials: 'include'});
        const text = await res.text();
        return {ok: true, status: res.status, url: res.url, text: text.slice(0, 3000000)};
    } catch (e) {
        return {ok: false, error: String(e)};
    }
}
"""


def collect(page, wait_ms, aweme_id):
    captured = []

    def on_response(resp):
        if resp.status == 200 and 'aweme/detail' in resp.url:
            try:
                captured.append({'url': resp.url[:400], 'json': resp.json()})
            except Exception:
                pass

    page.on('response', on_response)
    page.wait_for_timeout(wait_ms)
    probe = None
    if not aweme_id:
        # Short share links redirect to /video/{id}; derive the id for the detail API probe.
        m = re.search(r'/video/(\d+)', page.url or '')
        if m:
            aweme_id = m.group(1)
            _log(f'aweme derived from page url: {aweme_id}')
    if aweme_id:
        try:
            probe = page.evaluate(API_PROBE, aweme_id)
        except Exception as exc:
            probe = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:300]}
    data = page.evaluate("""() => {
        const out = {render_data: '', router: '', initial_state: '', title: document.title, url: location.href};
        const rd = document.getElementById('RENDER_DATA');
        if (rd && rd.textContent) out.render_data = rd.textContent;
        try { const v = window._ROUTER_DATA; if (v !== undefined) out.router = JSON.stringify(v); } catch (e) {}
        try { const v = window.__INITIAL_STATE__; if (v !== undefined) out.initial_state = JSON.stringify(v); } catch (e) {}
        return out;
    }""")
    data['api_probe'] = probe
    data['api_responses'] = captured
    return data


BROWSER_HINT = ('本机没有可用的浏览器：视频类链接（抖音等）需要 Chrome 或 Edge 才能解析页面。'
                '请安装其中一个后重试；也可以改用「手工粘贴文案/字幕」，不影响其他功能。')


def _registry_browser(names):
    """Read App Paths so non-standard install dirs are still found (read-only)."""
    if os.name != 'nt':
        return None
    import winreg
    for name in names:
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\' + name) as key:
                    value = winreg.QueryValueEx(key, '')[0]
                if value and Path(value).is_file():
                    return value
            except OSError:
                continue
    return None


def _package_chromium():
    """随包 chromium：tools/chromium/chrome-win64/chrome.exe（买家机器不装 Chrome/Edge 也能用）。"""
    root = Path(__file__).resolve().parent.parent / 'tools' / 'chromium'
    for pattern in ('chrome-win64/chrome.exe', 'chrome-win/chrome.exe', '*/chrome.exe'):
        for exe in sorted(root.glob(pattern)):
            if exe.is_file():
                return str(exe)
    return None


def _bundled_browser():
    """patchright/playwright 缓存里的 chromium（如果这台机器以前下载过）。"""
    roots = [Path(os.getenv('LOCALAPPDATA', '')) / 'ms-playwright',
             Path(os.getenv('USERPROFILE', '')) / '.cache/ms-playwright']
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ('*/chrome-win/chrome.exe', '*/chrome-win64/chrome.exe', '*/chrome-linux/chrome'):
            found.extend(p for p in root.glob(pattern) if p.is_file())
    return str(sorted(found)[-1]) if found else None


def browser_executable():
    """候选顺序：环境变量 → 包内 tools/chromium → 系统 Chrome → 系统 Edge → 本机浏览器缓存。
    包内 chromium 排在系统浏览器之前，保证交付到买家机器上行为一致。
    IP_BROWSER_EXECUTABLE remains the explicit override."""
    explicit = os.getenv('IP_BROWSER_EXECUTABLE', '').strip()
    if explicit:
        return explicit if Path(explicit).is_file() else None
    bundled = _package_chromium()
    if bundled:
        return bundled
    program_files = Path(os.getenv('PROGRAMFILES', r'C:\Program Files'))
    program_x86 = Path(os.getenv('PROGRAMFILES(X86)', r'C:\Program Files (x86)'))
    local = Path(os.getenv('LOCALAPPDATA', r'C:\Users\Public\AppData\Local'))
    for path in (
        program_files / 'Google/Chrome/Application/chrome.exe',
        program_x86 / 'Google/Chrome/Application/chrome.exe',
        local / 'Google/Chrome/Application/chrome.exe',
    ):
        if path.is_file():
            return str(path)
    chrome = shutil.which('chrome') or _registry_browser(('chrome.exe',))
    if chrome:
        return chrome
    for path in (
        program_files / 'Microsoft/Edge/Application/msedge.exe',
        program_x86 / 'Microsoft/Edge/Application/msedge.exe',
    ):
        if path.is_file():
            return str(path)
    return (shutil.which('msedge') or _registry_browser(('msedge.exe',))
            or _bundled_browser())


def browser_status():
    exe = browser_executable()
    return {'available': bool(exe), 'executable': exe or '',
            'message': '' if exe else BROWSER_HINT}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--aweme-id', default='')
    parser.add_argument('--wait-ms', type=int, default=7000)
    args = parser.parse_args()
    _log(f'start url={args.url} aweme={args.aweme_id} pid={os.getpid()} temp={os.environ.get("TEMP","")}')

    from patchright.sync_api import sync_playwright
    with sync_playwright() as p:
        launch_options = {'headless': True, 'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled']}
        executable = browser_executable()
        _log('browser executable=%s' % (executable or 'playwright-default'))
        if executable:
            launch_options['executable_path'] = executable
        elif not Path(p.chromium.executable_path).is_file():
            # 不静默失败：没有系统浏览器、也没有 patchright 缓存浏览器时直接说清原因。
            raise RuntimeError(BROWSER_HINT)
        # 2026-11-17 t67：会话复用——持久化 profile（cookie/缓存/存储）跨次复用，避免每次新起环境；
        # stealth 三件套沿用：真实 chromium、运行时 UA、禁用 AutomationControlled 特征。
        profile = Path(os.environ.get('IP_BROWSER_PROFILE')
                       or (Path(os.environ.get('IP_DATA_DIR') or (Path(__file__).resolve().parent / '.runtime'))
                           / 'browser-profile'))
        profile.mkdir(parents=True, exist_ok=True)
        context_args = dict(user_agent=UA, viewport={'width': 1280, 'height': 800}, locale='zh-CN')
        if executable:
            context_args['executable_path'] = executable
        launch_options = {'headless': True,
                          'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled',
                                   '--lang=zh-CN']}
        browser = None
        context = None
        try:
            context = p.chromium.launch_persistent_context(str(profile), **context_args, **launch_options)
            _log('persistent context profile=%s' % profile)
        except Exception as exc:
            # 持久化上下文不可用（浏览器版本差异）时退回一次性实例，不静默失败。
            _log('persistent context failed: %s; fallback to launch()' % exc)
            if executable:
                launch_options['executable_path'] = executable
            browser = p.chromium.launch(**launch_options)
            context = browser.new_context(user_agent=UA, viewport={'width': 1280, 'height': 800}, locale='zh-CN')
        try:
            session = douyin_session.attach(context)
            _log('session state=%s' % session.get('state'))
            page = context.pages[0] if getattr(context, 'pages', None) else context.new_page()
            page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
            data = collect(page, args.wait_ms, args.aweme_id)
        finally:
            try:
                context.close()
            except Exception:
                pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
    _log('main done, printing payload')
    for key in ('render_data', 'router', 'initial_state'):
        if data[key] and len(data[key]) > MAX_BLOCK:
            data[key] = data[key][:MAX_BLOCK]
    probe = data.get('api_probe') or {}
    if isinstance(probe, dict) and probe.get('text') and len(probe['text']) > MAX_BLOCK:
        probe['text'] = probe['text'][:MAX_BLOCK]
        data['api_probe'] = probe
    serialized = []
    for item in (data.get('api_responses') or [])[:3]:
        try:
            raw = json.dumps(item['json'], ensure_ascii=False)
            serialized.append({'url': item['url'], 'json_text': raw[:MAX_BLOCK]})
        except (TypeError, ValueError):
            pass
    data['api_responses'] = serialized
    print(json.dumps({'ok': True, **data}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        _log('FAILED: ' + traceback.format_exc()[:2000])
        print(json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}, ensure_ascii=False))
        sys.exit(1)
