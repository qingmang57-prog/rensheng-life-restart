"""本人已发布作品的数据回流采集：指标快照 + 本人作品评论。

原则（对齐《链路扩展契约_2026-10-27》与 bench_collect 的已验收经验）：
- 一切字段以真实取得为准，未知为 null，禁止伪造；每条记录保留 raw 原始返回。
- 不手工签名、不采 Cookie：复用 bench_collect 的 patchright 子进程模式，
  打开本人作品页滚动，只读取页面自身已签名接口响应。
- 评论接口路径沿用 bench_collect 已核实的 /aweme/v1/web/comment/list/；
  指标取自页面自身发出的作品详情响应（aweme/detail，已签名，可能有风控），
  未取得一律 null 并如实注明，不回退假数据。
- 风控/登录/未加载都按 bench_collect 五类语义真实分类；needs_action 不自动绕过。
- 指标快照只新增不覆盖（snapshot_id 由 metric_key+collected_at 派生）；
  评论按 (platform_work_id, platform_comment_id) 去重。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_collect
import contracts
import video_intake

# 复用 bench_collect 已核实的常量与分类（勿在别处散写路径）。
COMMENT_API_PATH = bench_collect.COMMENT_API_PATH          # /aweme/v1/web/comment/list/
COMMENT_MARKER = bench_collect.COMMENT_MARKER
AWEME_DETAIL_MARKER = 'aweme/v1/web/aweme/detail/'          # 作品详情（页面自身请求，已签名）
classify_api_response = bench_collect.classify_api_response
BrowserUnavailable = bench_collect.BrowserUnavailable
CollectionFailed = bench_collect.CollectionFailed

PLATFORM = 'douyin'
MAX_COMMENTS_DEFAULT = 50
JOB_TIMEOUT = 300

# 作品详情 statistics → 快照 metric_key 映射；映射不了的字段原样留在 raw 下。
METRIC_FIELDS = {'play_count': 'play', 'digg_count': 'like', 'comment_count': 'comment',
                 'share_count': 'share', 'collect_count': 'collect'}

_VIDEO_PATH = re.compile(r'^/video/(\d+)/?$')
_DOUYIN_HOSTS = ('douyin.com', 'www.douyin.com')


def parse_video_url(url):
    """校验 https 抖音作品页 URL，返回 (规范作品页 URL, platform_video_id)；否则 ValueError。

    支持两种本人常用入口：
    - https://www.douyin.com/video/<ID>
    - https://www.douyin.com/<任意页>?modal_id=<ID>（精选/推荐页弹出的作品）
    统一规范成 /video/<ID>，采集器只打开这一个地址，避免弹层页结构差异。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError('请提供抖音作品页 HTTPS 链接。')
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('请提供抖音作品页 HTTPS 链接（不是 http 或带账号密码的地址）。')
    if parsed.hostname not in _DOUYIN_HOSTS:
        raise ValueError('仅支持 douyin.com 的抖音作品链接。')
    match = _VIDEO_PATH.match(parsed.path)
    video_id = match.group(1) if match else (parse_qs(parsed.query).get('modal_id') or [''])[0]
    if not video_id.isdigit():
        raise ValueError('链接里没有作品编号：请用 https://www.douyin.com/video/<ID> '
                         '或带 ?modal_id=<ID> 的作品链接。')
    return 'https://www.douyin.com/video/' + video_id, video_id


# ---------------------------------------------------------------------------
# 采集结果规范化为入库记录（contracts 实体 + 追加/去重）
# ---------------------------------------------------------------------------

def _now_ms():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_metrics(detail_item):
    """从作品详情条目提取指标；逐项未知为 null，不编造。detail_item 为 None 时全 null。"""
    metrics = {target: None for target in METRIC_FIELDS.values()}
    if not isinstance(detail_item, dict):
        return metrics
    stats = detail_item.get('statistics')
    if not isinstance(stats, dict):
        return metrics
    for raw_key, target in METRIC_FIELDS.items():
        metrics[target] = bench_collect._metric(stats.get(raw_key))
    return metrics


def import_metrics(store, work, metrics, raw, collected_at):
    """指标快照只新增不覆盖；未取得（null）的指标不生成快照，禁止填 0 冒充。"""
    if not isinstance(metrics, dict):
        raise ValueError('采集 JSON 的 metrics 必须是对象。')
    created = []
    for target in METRIC_FIELDS.values():
        # 展示串（如 '1.2万'）与非法值一律视为未取得：不生成快照，不用 0 冒充。
        value = bench_collect._metric(metrics.get(target))
        if value is None:
            continue
        snapshot = contracts.create_metric_snapshot(
            f'{work["platform"]}.{target}', value, 'count', collected_at,
            source=f'{PLATFORM}_web_detail', platform_work_id=work['id'], raw=raw or {})
        created.append(store.put(contracts.KIND_METRIC_SNAPSHOT, snapshot))
    return created


def upsert_comment(store, work, raw_comment, source_url, collected_at):
    """同一作品同一平台评论 ID 重复入库不产生重复记录；原文原样必填。"""
    if not isinstance(raw_comment, dict):
        raise ValueError('评论条目必须是对象。')
    comment_id = str(raw_comment.get('platform_comment_id') or '').strip()
    author = str(raw_comment.get('author_name') or '').strip()
    content = str(raw_comment.get('content') or '')
    if not comment_id:
        raise ValueError('评论缺少平台评论编号，不能入库。')
    if not author or not content:
        raise ValueError('评论缺少作者或正文，原文原样是必填项。')
    existing = next((x for x in store.list(contracts.KIND_PLATFORM_COMMENT, 50000)
                     if x.get('platform_work_id') == work['id']
                     and x.get('platform_comment_id') == comment_id), None)
    if existing is not None:
        return existing, False
    parent = raw_comment.get('parent_comment_id')
    record = contracts.create_platform_comment(
        work['id'], PLATFORM, comment_id, author, content,
        source_url=str(source_url or '')[:800], collected_at=collected_at,
        parent_comment_id=str(parent).strip() if parent else None,
        like_count=bench_collect._metric(raw_comment.get('like_count')),
        reply_count=bench_collect._metric(raw_comment.get('reply_count')),
        raw=raw_comment.get('raw') if isinstance(raw_comment.get('raw'), dict) else raw_comment)
    return store.put(contracts.KIND_PLATFORM_COMMENT, record), True


def validate_raw(raw):
    """校验真实采集 JSON 的来源 URL、采集时间与结构；返回 (collected_at, video_url, video_id)。"""
    if not isinstance(raw, dict):
        raise ValueError('采集 JSON 必须是对象。')
    video_url, video_id = parse_video_url(raw.get('source_url'))
    collected_at = raw.get('collected_at')
    contracts.check_timestamp(collected_at, 'collected_at')
    if raw.get('comments') is not None and not isinstance(raw.get('comments'), list):
        raise ValueError('采集 JSON 的 comments 必须是数组。')
    return collected_at, video_url, video_id


def import_raw(store, raw, platform_work_id):
    """把本人作品的真实采集 JSON 落入 metric_snapshot / platform_comment；增量去重。

    采集 JSON 的平台视频编号必须与目标 platform_work 一致，否则拒绝入库。
    返回导入统计。
    """
    collected_at, video_url, video_id = validate_raw(raw)
    work = store.get(contracts.KIND_PLATFORM_WORK, platform_work_id)
    if work is None:
        raise ValueError('目标 platform_work 不存在。')
    if str(work.get('platform_video_id') or '') != video_id:
        raise ValueError('采集 JSON 的作品编号与目标 platform_work 不一致，未入库。')
    snapshots_created = len(import_metrics(
        store, work, raw.get('metrics') or {}, raw.get('metrics_raw'), collected_at))
    comments_created = 0
    for raw_comment in raw.get('comments') or []:
        _, created = upsert_comment(store, work, raw_comment, video_url, collected_at)
        comments_created += 1 if created else 0
    return {'snapshots_created': snapshots_created, 'comments_created': comments_created,
            'collected_at': collected_at, 'platform_work_id': work['id']}


class RawFeedbackImporter:
    """允许采集 job 或测试消费真实采集 JSON（格式 = DouyinWorkCollector 输出）。"""

    def __init__(self, store):
        self.store = store

    def import_raw(self, raw, platform_work_id):
        return import_raw(self.store, raw, platform_work_id)


# ---------------------------------------------------------------------------
# 抖音作品页采集器：browser-env 子进程 + 页面内已签名接口响应
# ---------------------------------------------------------------------------

class DouyinWorkCollector:
    """复用 video_intake.browser_python() 的 patchright 子进程模式采集本人作品页。"""

    def collect(self, video_url, max_comments=MAX_COMMENTS_DEFAULT, timeout=JOB_TIMEOUT):
        video_url, _ = parse_video_url(video_url)
        python = video_intake.browser_python()
        script = Path(__file__).resolve()
        if not python.is_file():
            raise BrowserUnavailable(
                '浏览器采集环境未安装：需要 .runtime/browser-env 与 patchright；'
                '未执行任何假采集。')
        command = [str(python), str(script), 'fetch-work', '--url', video_url,
                   '--max-comments', str(max_comments)]
        # 服务进程可能继承只读的系统 TEMP（例如系统缓存盘上的 Temp 目录），patchright 建临时目录会 EPERM；
        # 子进程统一指向包内可写的 .runtime/tmp。
        env = dict(os.environ)
        # 子进程 stdout 走管道时 Windows 默认按控制台代码页(GBK)编码，
        # 评论里的 emoji 会抛 UnicodeEncodeError；统一强制 UTF-8。
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        tmp_dir = script.parent / '.runtime' / 'tmp'
        try:
            tmp_dir.mkdir(parents=True, exist_ok=True)
            env['TEMP'] = env['TMP'] = str(tmp_dir)
        except OSError:
            pass
        try:
            proc = subprocess.run(command, capture_output=True, timeout=timeout,
                                  cwd=str(script.parent), env=env,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise CollectionFailed('采集超时；未入库任何数据，可稍后重试。') from None
        if proc.returncode != 0:
            detail = (proc.stdout or proc.stderr).decode('utf-8', 'replace').strip()[:600]
            message = detail or '无输出'
            needs_action, classification = False, None
            try:
                payload = json.loads(detail)
                needs_action = bool(payload.get('requires_action'))
                classification = payload.get('classification')
                message = str(payload.get('error') or message)
            except ValueError:
                pass
            raise CollectionFailed('采集子进程失败：' + message,
                                   needs_action=needs_action, classification=classification)
        try:
            payload = json.loads(proc.stdout.decode('utf-8', 'replace'))
        except ValueError:
            raise CollectionFailed('采集子进程返回格式无效。') from None
        if not payload.get('ok'):
            raise CollectionFailed(str(payload.get('error', '采集未完成')),
                                   needs_action=bool(payload.get('requires_action')),
                                   classification=payload.get('classification'))
        validate_raw(payload)
        return payload


# ---------------------------------------------------------------------------
# 子进程入口：python feedback_collect.py fetch-work --url <作品页>（browser-env python）
# 低风险路线同 bench_collect：不手工拼未签名接口，只读页面自身已签名响应。
# ---------------------------------------------------------------------------

# 评论接口实测每页 5 条；12 轮滚动约覆盖 50 条，够“登记后一次采满”。
WORK_COMMENT_SCROLL_ROUNDS = 12
CAPTURE_MAX_JSON = bench_collect.CAPTURE_MAX_JSON
UA = bench_collect.UA
# 抖音游客翻到一定页数会出现“登录后可查看更多评论”的覆盖层：
# 这是平台要求本人登录，不是抓取成功，必须如实回报 needs_action。
LOGIN_GATE_JS = """() => {
  const el = document.querySelector('.video-comment-cover');
  return !!(el && /登录/.test(el.textContent || ''));
}"""

_CLASSIFY_MESSAGE = bench_collect._CLASSIFY_MESSAGE


def _fail(message, requires_action=False, classification=None):
    print(json.dumps({'ok': False, 'error': message[:800], 'requires_action': requires_action,
                      'classification': classification}, ensure_ascii=False))
    sys.exit(1)


def _split_work_hits(hits):
    """把捕获的响应按评论/详情接口分组；同一 URL 只保留第一条（去重）。"""
    grouped = {'comment': [], 'detail': []}
    seen = set()
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get('url') or '')
        for name, marker in (('comment', COMMENT_MARKER), ('detail', AWEME_DETAIL_MARKER)):
            if marker in url and (name, url) not in seen:
                seen.add((name, url))
                grouped[name].append(hit)
    return grouped


def _build_work_result(comment_hits, detail_hits, max_comments):
    """从捕获响应构建 (comments, comments_error, metrics, metrics_raw, metrics_note, comments_skipped)。

    评论/指标各自独立判断：未加载/被风控如实记录，不互相否定，也不回退假数据。
    空正文（纯表情/图片）或空作者的评论无法分类，跳过并单独计数，不伪造内容。
    """
    comments = []
    comments_skipped = 0
    comments_error = None
    if not comment_hits:
        comments_error = 'comments_not_loaded'
    else:
        blocked = None
        ok_hits = []
        for hit in comment_hits:
            classification = classify_api_response(hit.get('status'), hit.get('json'))
            if classification == bench_collect.OK:
                ok_hits.append(hit)
            elif classification in (bench_collect.AUTH_REQUIRED, bench_collect.RISK_CONTROL):
                blocked = classification
        if blocked:
            comments_error = blocked
        else:
            for item in bench_collect.extract_comment_items(ok_hits, max_comments):
                comment = bench_collect._parse_comment_item(item)
                if not comment or not comment['platform_comment_id']:
                    continue
                if not (comment.get('author_name') and comment.get('content')):
                    comments_skipped += 1
                    continue
                comments.append(comment)

    detail_item = None
    metrics_note = None
    if not detail_hits:
        metrics_note = 'metrics_not_loaded'
    else:
        blocked = None
        for hit in detail_hits:
            classification = classify_api_response(hit.get('status'), hit.get('json'))
            if classification == bench_collect.OK and isinstance(hit.get('json'), dict):
                candidate = hit['json'].get('aweme_detail')
                if isinstance(candidate, dict):
                    detail_item = candidate
                    break
            elif classification in (bench_collect.AUTH_REQUIRED, bench_collect.RISK_CONTROL):
                blocked = classification
        if detail_item is None and metrics_note is None:
            metrics_note = 'metrics_blocked:' + str(blocked) if blocked else 'metrics_not_loaded'
    return (comments, comments_error, parse_metrics(detail_item), detail_item, metrics_note,
            comments_skipped)


def _comment_total(hits):
    """平台返回的评论总数（接口 total 字段）；未取得为 None。"""
    total = None
    for hit in hits or []:
        data = hit.get('json')
        if isinstance(data, dict) and isinstance(data.get('total'), int):
            total = max(total or 0, data['total'])
    return total


def _capture_into(hits):
    """注册 response 捕获：只读页面自身发出的已签名接口响应（200 + 目标路径）。"""
    def on_response(resp):
        try:
            if resp.status != 200:
                return
            url = resp.url
            if not any(m in url for m in (COMMENT_MARKER, AWEME_DETAIL_MARKER)):
                return
            data = resp.json()
            raw = json.dumps(data, ensure_ascii=False)
            if len(raw) > CAPTURE_MAX_JSON:
                return
            hits.append({'url': url[:400], 'status': resp.status, 'json': data})
        except Exception:
            pass
    return on_response


def cmd_fetch_work(args):
    """真实浏览器采集：打开本人作品页滚动，读取页面自身已签名评论/详情响应。"""
    from patchright.sync_api import sync_playwright
    import browser_fetch
    max_comments = max(args.max_comments, 0)
    # 弹层页（?modal_id=）与 /video/<ID> 统一成作品页再打开：弹层页可能不加载评论区。
    args.url, _ = parse_video_url(args.url)
    hits = []
    login_gated = False
    with sync_playwright() as p:
        launch_options = {'headless': True,
                          'args': ['--no-sandbox', '--disable-blink-features=AutomationControlled']}
        executable = browser_fetch.browser_executable()
        if executable:
            launch_options['executable_path'] = executable
        browser = p.chromium.launch(**launch_options)
        try:
            context = browser.new_context(user_agent=UA, viewport={'width': 1280, 'height': 800},
                                          locale='zh-CN')
            page = context.new_page()
            page.on('response', _capture_into(hits))
            try:
                page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                _fail(f'作品页打开失败（网络或页面错误）：{type(exc).__name__}: {exc}'[:700])
            page.wait_for_timeout(max(args.wait_ms, 2500))
            # 复用 bench_collect 已实测的评论区滚动：滚 window 只出首屏 5 条，
            # 滚评论区自己的可滚祖先才会触发页面自己请求下一页。
            bench_collect._scroll_comments(page, WORK_COMMENT_SCROLL_ROUNDS)
            try:
                login_gated = bool(page.evaluate(LOGIN_GATE_JS))
            except Exception:
                login_gated = False
            final_url = page.url
        finally:
            browser.close()

    grouped = _split_work_hits(hits)
    comments, comments_error, metrics, metrics_raw, metrics_note, comments_skipped = _build_work_result(
        grouped['comment'], grouped['detail'], max_comments)
    _, video_id = parse_video_url(args.url)
    platform_total = _comment_total(grouped['comment'])
    gated = login_gated and bool(comments)
    # 数量对不上也算“未取全”：不把“只拿到一部分”说成采集完成。
    short = bool(platform_total) and (len(comments) + comments_skipped) < platform_total
    requires_action = (comments_error in (bench_collect.AUTH_REQUIRED, bench_collect.RISK_CONTROL)
                       or gated or short)
    payload = {'ok': True, 'source_url': args.url, 'page_url': final_url or args.url,
               'collected_at': _now_ms(), 'platform_video_id': video_id,
               'comments_total': platform_total, 'comments_gated': gated,
               'comments_partial': short, 'comments_skipped': comments_skipped,
               'metrics': metrics, 'metrics_raw': metrics_raw, 'metrics_note': metrics_note,
               'comments': comments, 'comments_error': comments_error}
    if gated or short:
        payload['requires_action'] = True
        got = len(comments) + comments_skipped
        if gated:
            reason = '平台要求登录后才能查看更多评论'
        else:
            reason = '本次未取全评论（差额可能在登录限制或未展开的回复里）'
        payload['error'] = (f'{reason}：本次取得 {got} 条'
                            + (f'，平台显示共 {platform_total} 条' if platform_total else '')
                            + '，未登录、未绕过验证。请在抖音登录后重试。')
    elif requires_action:
        payload['requires_action'] = True
        payload['error'] = '评论响应被平台拒绝（' + comments_error + '）：' \
            + _CLASSIFY_MESSAGE.get(comments_error, '需本人处理')
    print(json.dumps(payload, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='Feedback collection worker (browser-env python).')
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch-work', help='采集抖音作品页指标与公开评论')
    fetch.add_argument('--url', required=True)
    fetch.add_argument('--max-comments', type=int, default=MAX_COMMENTS_DEFAULT)
    fetch.add_argument('--wait-ms', type=int, default=3000)
    args = parser.parse_args()
    if args.command == 'fetch-work':
        cmd_fetch_work(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # 子进程任何异常都要以 JSON 失败返回，不能假成功
        _fail(f'{type(exc).__name__}: {exc}')
