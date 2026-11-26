# -*- coding: utf-8 -*-
"""抖音登录助手：打开有界面浏览器，扫码登录后把登录态存到运行目录。

安全边界：
- 只在打开的一个浏览器窗口里等用户自己扫码/输密码，不破解、不接收外部上传；
- 登录态只写 `<data>/douyin_cookie.json`，不进源码目录、不进交付包、不上传服务器；
- 终端和日志只打印cookie 条数与路径，不打印任何 cookie 值。
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
sys.path.insert(0, str(APP))

os.environ.setdefault('IP_DATA_DIR', str(APP / '.runtime'))
scratch = Path(os.environ['IP_DATA_DIR']) / 'tmp'
scratch.mkdir(parents=True, exist_ok=True)
os.environ['TEMP'] = os.environ['TMP'] = str(scratch)

import browser_fetch  # noqa: E402
import douyin_session  # noqa: E402
from patchright.sync_api import sync_playwright  # noqa: E402

UA = browser_fetch.UA
WAIT_SECONDS = 300
POLL_SECONDS = 3


def main():
    print('[登录] 正在打开抖音登录页，请用抖音 App 扫码登录（%d 秒内完成）。' % WAIT_SECONDS, flush=True)
    print('[登录] 登录态将保存到：%s' % douyin_session.COOKIE_FILE, flush=True)
    with sync_playwright() as p:
        options = {'headless': False,
                   'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled']}
        exe = browser_fetch.browser_executable()
        if exe:
            options['executable_path'] = exe
        browser = p.chromium.launch(**options)
        try:
            context = browser.new_context(user_agent=UA, viewport={'width': 1280, 'height': 900},
                                          locale='zh-CN')
            page = context.new_page()
            try:
                page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                print('[登录] 打开抖音页面失败：%s: %s' % (type(exc).__name__, exc), flush=True)
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                cookies = context.cookies()
                if any(c.get('name') == 'sessionid' and c.get('value') for c in cookies):
                    path, count = douyin_session.save(cookies)
                    print('[登录] 成功：已保存 %d 条 cookie 到 %s' % (count, path), flush=True)
                    print('[登录] 可以直接关闭这个浏览器窗口了。', flush=True)
                    time.sleep(1)
                    return 0
                time.sleep(POLL_SECONDS)
            print('[登录] 超时：没有检测到登录成功（未拿到 sessionid）。请重试并确认扫码后停留几秒。', flush=True)
            return 1
        finally:
            try:
                browser.close()
            except Exception:
                pass


if __name__ == '__main__':
    sys.exit(main())
