"""Douyin publisher worker. Runs under .runtime/browser-env python (patchright).

Patterns learned from dreammis/social-auto-upload (MIT, https://github.com/dreammis/social-auto-upload):
QR login on creator.douyin.com with storage_state session file; direct set_input_files on the
upload page; wait for the publish-page URL; title/description/tags via the real form; native
pointer-event clicks for anti-bot components; recommended-cover fallback when the platform
requires a cover. It never clicks 发布: the owner confirms and publishes on the platform.

Only the owner's own account via their own QR scan. No cookie harvesting, no challenge solving.
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

UPLOAD_URL = 'https://creator.douyin.com/creator-micro/content/upload'
CREATOR_HOME = 'https://creator.douyin.com/'
PUBLISH_URLS = (
    'https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page',
    'https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page',
)
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
QRCODE_SELECTORS = (
    'div#animate_qrcode_container img[src^="data:image"]',
    'div[class*="animate_qrcode_container"] img[src^="data:image"]',
    'div[class*="scan_qrcode_login_content"] img[src^="data:image"]',
    'img[aria-label="二维码"]',
)
UPLOAD_INPUT_SELECTORS = (
    'input.upload-btn-input',
    'div[class^="container"] input[accept]',
    'div[class^="container"] input[type="file"]',
    'div[class^="container"] input',
)


def out(payload):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0 if payload.get('ok') else 1)


def fail(message, **extra):
    out({'ok': False, 'message': message[:1200], **extra})


def write_progress(progress, payload):
    if not progress:
        return
    try:
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text(json.dumps({'at': time.time(), **payload}, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass


def launch(playwright, headless=True):
    return playwright.chromium.launch(headless=headless, args=['--no-sandbox', '--disable-blink-features=AutomationControlled'])


def new_context(browser, session):
    kwargs = {'viewport': {'width': 1280, 'height': 800}, 'locale': 'zh-CN', 'user_agent': UA,
              'permissions': ['geolocation']}
    if session and Path(session).is_file():
        kwargs['storage_state'] = session
    return browser.new_context(**kwargs)


def clear_overlays(page):
    try:
        page.evaluate("() => { document.querySelectorAll('.shepherd-element,.shepherd-modal-overlay-container,[class*=\"mention-wrapper\"]').forEach(e => e.remove()); }")
    except Exception:
        pass


def has_session_cookie(session):
    try:
        data = json.loads(Path(session).read_text(encoding='utf-8'))
        return any(c.get('name') == 'sessionid' and c.get('value') for c in data.get('cookies', []))
    except (OSError, ValueError):
        return False


def cmd_status(args):
    from patchright.sync_api import sync_playwright
    if not Path(args.session).is_file():
        return out({'ok': True, 'logged_in': False, 'reason': 'no_session', 'message': '还没有抖音账号会话。'})
    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            context = new_context(browser, args.session)
            page = context.new_page()
            page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=90000)
            page.wait_for_timeout(3500)
            on_login = page.get_by_text('手机号登录').count() or page.get_by_text('扫码登录').count()
            valid = 'content/upload' in page.url and not on_login
            return out({'ok': True, 'logged_in': bool(valid), 'reason': '' if valid else 'cookie_invalid',
                        'message': '会话有效。' if valid else '会话已失效，需要重新扫码登录。', 'url': page.url})
        finally:
            browser.close()


def extract_qrcode(page):
    for selector in QRCODE_SELECTORS:
        try:
            element = page.locator(selector).first
            element.wait_for(state='attached', timeout=8000)
            src = element.get_attribute('src')
            if src:
                return src
        except Exception:
            continue
    return ''


def logged_in_now(page):
    # Only the sessionid cookie proves login. The page URL alone is not evidence:
    # creator.douyin.com can land on a non-login URL without a session.
    for cookie in page.context.cookies():
        if cookie.get('name') == 'sessionid' and cookie.get('value'):
            return True
    return False


def cmd_login(args):
    from patchright.sync_api import sync_playwright
    deadline = time.time() + args.timeout
    write_progress(args.progress, {'phase': 'opening', 'qrcode_data_url': '', 'message': '正在打开登录页…'})
    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            context = new_context(browser, None)
            page = context.new_page()
            page.goto(CREATOR_HOME, wait_until='domcontentloaded', timeout=90000)
            try:
                page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
            if not page.get_by_text('扫码登录', exact=True).first.count():
                tab = page.get_by_text('登录').first
                if tab.count():
                    tab.click()
                    page.wait_for_timeout(1500)
            page.get_by_text('扫码登录', exact=True).first.wait_for(state='attached', timeout=60000)
            qrcode = extract_qrcode(page)
            if not qrcode:
                write_progress(args.progress, {'phase': 'failed', 'qrcode_data_url': '', 'message': '没有定位到登录二维码，请重试。'})
                return fail('没有定位到登录二维码，请重试。')
            write_progress(args.progress, {'phase': 'waiting_scan', 'qrcode_data_url': qrcode,
                                           'message': '请用抖音 App 扫码登录创作者中心。'})
            while time.time() < deadline:
                if logged_in_now(page):
                    page.wait_for_timeout(2000)
                    context.storage_state(path=args.session)
                    if not has_session_cookie(args.session):
                        return fail('扫码流程结束，但会话中缺少 sessionid，请重试。')
                    write_progress(args.progress, {'phase': 'done', 'qrcode_data_url': '', 'message': '登录成功。'})
                    return out({'ok': True, 'logged_in': True, 'message': '抖音创作者中心登录成功。'})
                expired = page.get_by_text('二维码失效', exact=True).first
                if expired.count() and expired.is_visible():
                    try:
                        expired.click()
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    qrcode = extract_qrcode(page)
                    if qrcode:
                        write_progress(args.progress, {'phase': 'waiting_scan', 'qrcode_data_url': qrcode,
                                                       'message': '二维码已刷新，请重新扫码。'})
                time.sleep(2)
            write_progress(args.progress, {'phase': 'expired', 'qrcode_data_url': '',
                                           'message': '二维码已过期，请重新点击“用抖音 APP 扫码登录”。'})
            return fail('等待扫码超时，没有完成登录。')
        finally:
            browser.close()


def find_upload_input(page):
    for selector in UPLOAD_INPUT_SELECTORS:
        candidate = page.locator(selector).first
        if candidate.count():
            return candidate
    return None


def wait_publish_page(page, deadline):
    while time.time() < deadline:
        for url in PUBLISH_URLS:
            try:
                page.wait_for_url(url, timeout=2000)
                return url
            except Exception:
                pass
        time.sleep(1)
    raise RuntimeError('没有进入发布表单页，可能上传失败或被平台拦截。')


def wait_upload_done(page, deadline):
    while time.time() < deadline:
        try:
            if page.locator('[class^="long-card"] div:has-text("重新上传")').count():
                return
        except Exception:
            pass
        try:
            if page.locator('div.progress-div > div:has-text("上传失败")').count():
                raise RuntimeError('视频上传失败，请重试或更换文件。')
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('等待视频上传完成超时。')


def fill_form(page, title, desc, tags):
    title_input = page.locator('input[placeholder*="填写作品标题"]').first
    title_input.wait_for(state='visible', timeout=120000)
    title_input.fill(title[:30])
    editor = page.locator('div.zone-container[contenteditable="true"]').first
    editor.wait_for(state='visible', timeout=60000)
    editor.click()
    page.keyboard.press('Control+KeyA')
    page.keyboard.press('Delete')
    if desc and desc.strip():
        page.keyboard.type(desc.strip()[:1000])
    for tag in (tags or [])[:10]:
        page.keyboard.type(' #' + str(tag).strip().lstrip('#'))
        page.keyboard.press('Space')
    page.keyboard.press('Escape')


def apply_ai_declaration(page):
    for text in ('内容由AI生成', '由AI生成'):
        option = page.get_by_text(text, exact=True).first
        if option.count() and option.is_visible():
            try:
                option.click()
                time.sleep(1)
                return
            except Exception:
                continue


def handle_cover(page):
    if not page.get_by_text('请设置封面后再发布').first.is_visible():
        return False
    recommend = page.locator('[class^="recommendCover-"]').first
    if not recommend.count():
        return False
    recommend.click()
    time.sleep(1)
    confirm = page.get_by_role('button', name='确定')
    if confirm.count() and confirm.is_visible():
        confirm.click()
        time.sleep(1)
    return True


def upload_video(args, dry_run):
    from patchright.sync_api import sync_playwright
    if not has_session_cookie(args.session):
        return fail('抖音账号会话缺失或失效，请先扫码登录。')
    if not Path(args.video).is_file():
        return fail('待发布视频不存在。')
    with sync_playwright() as p:
        browser = launch(p, headless=True)
        try:
            context = new_context(browser, args.session)
            page = context.new_page()
            write_progress(args.progress, {'phase': 'open_upload', 'message': '打开创作者中心上传页。'})
            page.goto(UPLOAD_URL, wait_until='domcontentloaded', timeout=90000)
            try:
                page.wait_for_url(UPLOAD_URL, timeout=90000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            if page.get_by_text('扫码登录').count() or page.get_by_text('手机号登录').count():
                return fail('会话已失效，打开的是登录页。请先扫码登录。')
            upload_input = find_upload_input(page)
            if upload_input is None:
                return fail('没有找到上传入口，页面结构可能已变化。')
            upload_input.wait_for(state='attached', timeout=60000)
            write_progress(args.progress, {'phase': 'uploading', 'message': '正在上传视频文件。'})
            upload_input.set_input_files(args.video)
            wait_publish_page(page, time.time() + 300)
            page.wait_for_timeout(1000)
            write_progress(args.progress, {'phase': 'filling', 'message': '填写标题、文案与话题。'})
            fill_form(page, args.title, args.desc or '', json.loads(args.tags_json or '[]'))
            wait_upload_done(page, time.time() + 300)
            apply_ai_declaration(page)
            handle_cover(page)
            page.wait_for_timeout(1000)
            clear_overlays(page)
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            context.storage_state(path=args.session)
            write_progress(args.progress, {'phase': 'prepared',
                                           'message': '资料已就绪，未点击发布；请本人到创作者中心确认并发布。'})
            return out({'ok': True, 'published': False, 'prepared': True,
                        'needs_manual_publish': True, 'dry_run': bool(dry_run),
                        'screenshot': args.screenshot or '', 'title': args.title,
                        'tags': json.loads(args.tags_json or '[]'), 'upload_url': UPLOAD_URL,
                        'message': ('演练完成：视频已上传为草稿、表单已填好，未发布。'
                                    if dry_run else
                                    '资料已就绪（草稿与表单已填好），未点击发布。'
                                    '请本人到创作者中心确认并发布，发布后把作品链接回填到工作台。')})
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    parser_status = sub.add_parser('status')
    parser_status.add_argument('--session', required=True)
    parser_status.set_defaults(func=cmd_status)

    parser_login = sub.add_parser('login')
    parser_login.add_argument('--session', required=True)
    parser_login.add_argument('--progress', default='')
    parser_login.add_argument('--timeout', type=int, default=280)
    parser_login.set_defaults(func=cmd_login)

    for command in ('plan', 'submit'):
        parser_cmd = sub.add_parser(command)
        parser_cmd.add_argument('--session', required=True)
        parser_cmd.add_argument('--video', required=True)
        parser_cmd.add_argument('--title', required=True)
        parser_cmd.add_argument('--desc', default='')
        parser_cmd.add_argument('--tags-json', default='[]')
        parser_cmd.add_argument('--screenshot', default='')
        parser_cmd.add_argument('--progress', default='')
        parser_cmd.set_defaults(func=(lambda args, dry=command == 'plan': upload_video(args, dry)))

    args = parser.parse_args()
    args.progress = Path(args.progress) if getattr(args, 'progress', '') else None
    args.func(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        out({'ok': False, 'message': f'{type(exc).__name__}: {exc}'[:1200]})
