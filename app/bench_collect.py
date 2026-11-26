"""对标账号采集：Collector 接口、抖音主页采集器、真实采集 JSON 导入器。

原则（对齐《链路扩展契约_2026-10-27》）：
- 一切字段以真实取得为准，未知为 null，禁止伪造；每条记录保留 raw 原始返回。
- 抖音采集复用 browser_fetch/video_intake 的 patchright 模式：真实 chromium 子进程
  （.runtime/browser-env），在页面内经页面自身会话调用公开接口，不采集 Cookie、
  不实现签名逆向、不解校验。
- 接口路径常量集中在下方「已核实」区块（2026-10-27 读一手源码核实）。
- RawBenchImporter 允许采集 job 消费真实采集 JSON（须通过来源 URL / 采集时间 /
  账号一致性校验），测试全部使用 fixture。
- 宣称纪律：接口路径已按 GitHub 核实，但真实账号采集仍未验收，不得宣称打通。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contracts
import douyin_session
import video_intake

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

# ---------------------------------------------------------------------------
# 平台接口常量（2026-10-27 已读一手源码核实，勿在别处散写路径）
# 来源：
# - MediaCrawler media_platform/douyin/client.py（自定义许可，仅作行为参照，未搬代码）
# - Evil0ctal/Douyin_TikTok_Download_API src/dtk/platforms/douyin/endpoints.py（Apache 2.0）
# 已知风险（核实结论 + 2026-10-27 真实验收，非全量实测）：
# - 手工调用未签名 /aweme/v1/web/aweme/post/ 会触发 risk_control（真实验收已证实）；
#   滚动页面时页面自己会请求带签名接口并返回 200。因此 collector 不手工拼接口，
#   只打开页面滚动、读取页面自身已签名网络响应（profile/other、aweme/post、comment/list）。
# - 游客身份作者作品列表第一页可能缺最新几条；第二页起可能返回 17 字节空壳
#   {"status_code":0}——这是拒绝不是完成，必须按 AUTH_REQUIRED 处理，不能当"没有作品"。
# - 评论游客身份通常可翻页（滚动时 cursor 递增）；风控表现为 HTTP 403/429/444 或 Argus/验证码。
# - 作品详情接口有 uifid/签名风控；本 collector 只取作品列表与评论，不做签名逆向。
USER_POST_API_PATH = '/aweme/v1/web/aweme/post/'
# GET 参数：sec_user_id、count、max_cursor、locate_query=false、
#           publish_video_strategy_type=2；响应：aweme_list / max_cursor / has_more。
COMMENT_API_PATH = '/aweme/v1/web/comment/list/'
# GET 参数：aweme_id、cursor、count、item_type=0；响应：comments / cursor / has_more / total。
COMMENT_REPLY_API_PATH = '/aweme/v1/web/comment/list/reply/'
# GET 参数：comment_id、item_id、cursor、count、item_type=0（二级评论，当前 collector 不拉取）。

# ---- 响应分类（五类语义，详见 classify_api_response）----
EMPTY_END = 'empty_end'          # has_more=0 / 空列表：正常结束
AUTH_REQUIRED = 'auth_required'  # 空壳/游客拒绝/需登录：needs_action，不当普通重试
RISK_CONTROL = 'risk_control'    # 403/429/444/验证码/Argus：needs_action，不绕过
SIGNATURE_BROKEN = 'signature_broken'  # 响应结构异常/签名失效：接口可能已变化
NOT_FOUND = 'not_found'          # 404：对象不存在
OK = 'ok'                        # 取得有效数据

PLATFORM = 'douyin'
MAX_WORKS_DEFAULT = 20
MAX_COMMENTS_DEFAULT = 30
JOB_TIMEOUT = 300


def classify_api_response(http_status, payload):
    """把平台接口响应分为五类语义。payload 为解析后的 JSON（或原始文本/None）。

    空壳 {"status_code":0}（约 17 字节）是游客拒绝信号 → AUTH_REQUIRED；
    has_more=0 的空列表是正常翻页结束 → EMPTY_END，二者不得混淆。
    """
    if http_status == 404:
        return NOT_FOUND
    if http_status is not None and http_status >= 400:
        return RISK_CONTROL  # 403/429/444 及一切 4xx/5xx 风控或服务端拒绝
    if payload is None:
        return SIGNATURE_BROKEN
    if isinstance(payload, bytes):
        payload = payload.decode('utf-8', 'replace')
    if isinstance(payload, str):
        low = payload.lower()
        if 'argus' in low or 'captcha' in low or '验证' in payload:
            return RISK_CONTROL
        return SIGNATURE_BROKEN
    if not isinstance(payload, dict):
        return SIGNATURE_BROKEN
    data_keys = set(payload.keys()) & {'aweme_list', 'comments'}
    status_code = payload.get('status_code')
    if not data_keys:
        message = str(payload.get('status_msg') or payload.get('message') or '')
        if status_code not in (0, '0', None):
            if '签名' in message or 'signature' in message.lower():
                return SIGNATURE_BROKEN
            return AUTH_REQUIRED
        if len(payload) <= 2:
            return AUTH_REQUIRED  # {"status_code":0} 空壳：游客拒绝，不是"没有作品"
        return SIGNATURE_BROKEN
    data = payload.get('aweme_list') if 'aweme_list' in payload else payload.get('comments')
    if not isinstance(data, list):
        return SIGNATURE_BROKEN
    if not data and payload.get('has_more') in (0, '0', False):
        return EMPTY_END
    if data:
        return OK
    return SIGNATURE_BROKEN


class BrowserUnavailable(RuntimeError):
    """浏览器采集环境（patchright / browser-env）未安装。"""


class CollectionFailed(RuntimeError):
    """采集失败；needs_action=True 表示平台要求登录/验证等本人处理，不自动绕过；
    classification 为五类响应分类（见 classify_api_response）。"""

    def __init__(self, message, needs_action=False, classification=None):
        super().__init__(message)
        self.needs_action = needs_action
        self.classification = classification


class Collector:
    """平台采集器统一接口（对齐契约第 4 节适配器思想，当前仅采集侧子集）。"""

    def collect_account(self, home_url, max_works=MAX_WORKS_DEFAULT,
                        max_comments=MAX_COMMENTS_DEFAULT):
        """返回真实采集 JSON（RawBenchImporter 的消费格式），不直接写库。"""
        raise NotImplementedError

    def collect_video(self, video_url, max_comments=MAX_COMMENTS_DEFAULT):
        """采集单条视频的文案、指标与公开评论（同样返回 raw JSON，不写库）。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 采集结果规范化为入库记录（contracts 实体 + 增量去重）
# ---------------------------------------------------------------------------

def _now_ms():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _metric(value):
    """互动指标：整数原样；数字字符串转整数；其他（含小数、'1.2万' 展示串）一律 null。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r'\d+', value):
        return int(value)
    return None


def _iso_ms(epoch):
    """平台 create_time（秒）→ ISO8601 毫秒字符串；无效返回 None，不猜测。"""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        return None


def _valid_source_url(url):
    if not isinstance(url, str) or not url.startswith('https://'):
        return False
    host = (url.split('/', 3)[2] if url.count('/') >= 3 else '').lower()
    return host == 'douyin.com' or host.endswith('.douyin.com')


def upsert_account(store, raw_account, home_url, collected_at):
    """按 (platform, external_id) 复用已有 bench_account；重复导入不产生重复记录。"""
    if not isinstance(raw_account, dict):
        raise ValueError('采集 JSON 的 account 必须是对象。')
    external_id = str(raw_account.get('external_id') or '').strip()
    if not external_id:
        raise ValueError('采集 JSON 的 account.external_id 缺失；账号标识必须来自平台。')
    display_name = str(raw_account.get('display_name') or '').strip() or external_id
    existing = next((x for x in store.list(contracts.KIND_BENCH_ACCOUNT, 5000)
                     if x.get('platform') == PLATFORM and x.get('external_id') == external_id), None)
    fields = {'follower_count': _metric(raw_account.get('follower_count')),
              'following_count': _metric(raw_account.get('following_count')),
              'works_count': _metric(raw_account.get('works_count'))}
    if existing is None:
        return store.put(contracts.KIND_BENCH_ACCOUNT, contracts.create_bench_account(
            PLATFORM, display_name, home_url=home_url, external_id=external_id,
            collected_at=collected_at, raw=raw_account, **fields)), True
    changes = {'collected_at': collected_at, 'raw': raw_account, **fields}
    if display_name != existing.get('display_name'):
        changes['display_name'] = display_name
    return store.patch(contracts.KIND_BENCH_ACCOUNT, existing['id'], changes), False


def upsert_work(store, account, raw_work, collected_at):
    """同一账号同一平台作品 ID 重复入库不产生重复记录；重跑保留历史（不覆盖旧指标）。"""
    if not isinstance(raw_work, dict):
        raise ValueError('作品条目必须是对象。')
    video_id = str(raw_work.get('platform_video_id') or '').strip()
    if not video_id:
        raise ValueError('作品缺少平台视频编号，不能入库。')
    existing = next((x for x in store.list(contracts.KIND_BENCH_WORK, 10000)
                     if x.get('bench_account_id') == account['id']
                     and x.get('platform_video_id') == video_id), None)
    fields = {'like_count': _metric(raw_work.get('like_count')),
              'comment_count': _metric(raw_work.get('comment_count')),
              'share_count': _metric(raw_work.get('share_count')),
              'collect_count': _metric(raw_work.get('collect_count')),
              'play_count': _metric(raw_work.get('play_count')),
              'duration_ms': _metric(raw_work.get('duration_ms'))}
    if existing is not None:
        return existing, False
    record = contracts.create_bench_work(
        account['id'], PLATFORM, video_id,
        title=str(raw_work.get('title') or '')[:500],
        source_url=str(raw_work.get('source_url') or '')[:800],
        collected_at=collected_at, published_at=_iso_ms(raw_work.get('published_at')),
        raw=raw_work.get('raw') if isinstance(raw_work.get('raw'), dict) else raw_work, **fields)
    return store.put(contracts.KIND_BENCH_WORK, record), True


def upsert_comment(store, work, raw_comment, source_url, collected_at):
    """同一作品同一平台评论 ID 重复入库不产生重复记录。"""
    if not isinstance(raw_comment, dict):
        raise ValueError('评论条目必须是对象。')
    comment_id = str(raw_comment.get('platform_comment_id') or '').strip()
    author = str(raw_comment.get('author_name') or '').strip()
    content = str(raw_comment.get('content') or '')
    if not comment_id:
        raise ValueError('评论缺少平台评论编号，不能入库。')
    if not author or not content:
        raise ValueError('评论缺少作者或正文，原文原样是必填项。')
    existing = next((x for x in store.list(contracts.KIND_BENCH_COMMENT, 20000)
                     if x.get('bench_work_id') == work['id']
                     and x.get('platform_comment_id') == comment_id), None)
    if existing is not None:
        return existing, False
    parent = raw_comment.get('parent_comment_id')
    record = contracts.create_bench_comment(
        work['id'], PLATFORM, comment_id, author, content,
        source_url=str(source_url or '')[:800], collected_at=collected_at,
        parent_comment_id=str(parent).strip() if parent else None,
        like_count=_metric(raw_comment.get('like_count')),
        reply_count=_metric(raw_comment.get('reply_count')),
        raw=raw_comment.get('raw') if isinstance(raw_comment.get('raw'), dict) else raw_comment)
    return store.put(contracts.KIND_BENCH_COMMENT, record), True


def validate_raw(raw):
    """校验真实采集 JSON 的来源 URL 与采集时间；返回 (collected_at, home_url)。"""
    if not isinstance(raw, dict):
        raise ValueError('采集 JSON 必须是对象。')
    source_url = raw.get('source_url')
    if not _valid_source_url(source_url):
        raise ValueError('采集 JSON 的 source_url 必须是抖音 HTTPS 地址，用于来源校验。')
    collected_at = raw.get('collected_at')
    contracts.check_timestamp(collected_at, 'collected_at')
    works = raw.get('works')
    if not isinstance(works, list):
        raise ValueError('采集 JSON 的 works 必须是数组。')
    return collected_at, source_url


def import_raw(store, raw, account_id=None):
    """把真实采集 JSON 落入 bench_* 实体；增量去重。返回导入统计。"""
    collected_at, home_url = validate_raw(raw)
    account, account_created = upsert_account(store, raw.get('account') or {}, home_url, collected_at)
    if account_id and account['id'] != account_id:
        raise ValueError('采集 JSON 的账号与目标 bench_account 不一致，未入库。')
    works_created = 0
    comments_created = 0
    works_total = 0
    for raw_work in raw['works']:
        work, created = upsert_work(store, account, raw_work, collected_at)
        works_total += 1
        works_created += 1 if created else 0
        comments = raw_work.get('comments') or []
        if not isinstance(comments, list):
            raise ValueError('作品评论必须是数组。')
        for raw_comment in comments:
            _, created = upsert_comment(store, work, raw_comment, work.get('source_url') or '', collected_at)
            comments_created += 1 if created else 0
    return {'account': account, 'account_created': account_created,
            'works_total': works_total, 'works_created': works_created,
            'comments_created': comments_created, 'collected_at': collected_at}


class RawBenchImporter:
    """允许采集 job 或测试消费真实采集 JSON（格式 = DouyinBenchCollector 输出）。"""

    def __init__(self, store):
        self.store = store

    def import_raw(self, raw, account_id=None):
        return import_raw(self.store, raw, account_id=account_id)


# ---------------------------------------------------------------------------
# 抖音主页采集器：browser-env 子进程 + 页面内公开接口响应
# ---------------------------------------------------------------------------

def _extract_sec_uid(url):
    match = re.search(r'/user/([^/?#]+)', url or '')
    return match.group(1) if match else ''


class DouyinBenchCollector(Collector):
    """复用 video_intake.browser_python() 的 patchright 子进程模式采集抖音主页。"""

    def _run(self, extra_args, timeout):
        """跑采集子进程并解析结果。

        子进程失败时 stdout 里同样是 JSON（_fail 的约定），先按 JSON 取 error 与
        requires_action：平台要求登录/验证必须如实透传，不能退化成一条普通报错。
        """
        python = video_intake.browser_python()
        script = Path(__file__).resolve()
        if not python.is_file():
            raise BrowserUnavailable(
                '浏览器采集环境未安装：需要 .runtime/browser-env 与 patchright；'
                '未执行任何假采集。')
        command = [str(python), str(script)] + list(extra_args)
        # 服务进程可能继承只读或不可写的系统 TEMP（历史坑：mkdtemp EPERM），
        # patchright 建临时目录前先把 TEMP/TMP 指到包内运行目录。
        scratch = douyin_session.DATA / 'tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        child_env = dict(os.environ)
        child_env['TEMP'] = child_env['TMP'] = str(scratch)
        # 子进程 stdout 走管道时按系统 GBK 编码，采集 JSON 里的 emoji/生僻字会直接崩；
        # 固定 UTF-8，父进程也按 utf-8 解码（start.bat 之外的启动方式同样成立）。
        child_env['PYTHONIOENCODING'] = 'utf-8'
        try:
            proc = subprocess.run(command, capture_output=True, timeout=timeout,
                                  cwd=str(script.parent), env=child_env,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except subprocess.TimeoutExpired:
            raise CollectionFailed('采集超时；未入库任何数据，可稍后重试。') from None
        stdout = (proc.stdout or b'').decode('utf-8', 'replace').strip()
        payload = None
        try:
            payload = json.loads(stdout) if stdout.startswith('{') else None
        except ValueError:
            payload = None
        if proc.returncode != 0 or (isinstance(payload, dict) and not payload.get('ok')):
            if isinstance(payload, dict):
                raise CollectionFailed(
                    str(payload.get('error') or '采集未完成')[:800],
                    needs_action=bool(payload.get('requires_action')),
                    classification=payload.get('classification'))
            detail = (stdout or (proc.stderr or b'').decode('utf-8', 'replace').strip())[:600]
            raise CollectionFailed('采集子进程失败：' + (detail or '无输出'))
        if payload is None:
            raise CollectionFailed('采集子进程返回格式无效。')
        validate_raw(payload)
        return payload

    def collect_account(self, home_url, max_works=MAX_WORKS_DEFAULT,
                        max_comments=MAX_COMMENTS_DEFAULT, timeout=JOB_TIMEOUT):
        if not _valid_source_url(home_url):
            raise ValueError('主页地址必须是抖音 HTTPS 链接。')
        return self._run(['fetch', '--url', home_url,
                          '--max-works', str(max_works),
                          '--max-comments', str(max_comments)], timeout)

    def collect_video(self, video_url, max_comments=MAX_COMMENTS_DEFAULT, timeout=180, wait_ms=4000):
        if not _valid_source_url(video_url):
            raise ValueError('视频地址必须是抖音 HTTPS 链接。')
        return self._run(['fetch-video', '--url', video_url,
                          '--max-comments', str(max_comments), '--wait-ms', str(wait_ms)], timeout)


# ---------------------------------------------------------------------------
# 子进程入口：在 browser-env python 下执行（python bench_collect.py fetch ...）
#
# 低风险路线（真实验收后的修正）：不手工拼未签名接口（实测会触发风控），
# 只打开页面滚动，读取页面自身发出的已签名接口响应。不采集 Cookie。
# ---------------------------------------------------------------------------

PROFILE_MARKER = 'user/profile/other'    # 主页滚动时页面自动请求，响应含 user 资料
POST_MARKER = 'aweme/v1/web/aweme/post/'  # 作品列表（已签名）
COMMENT_MARKER = 'aweme/v1/web/comment/list/'  # 一级评论（已签名，滚动翻页 cursor 递增）

HOME_SCROLL_ROUNDS = 5       # 主页滚动轮数上限
COMMENT_SCROLL_ROUNDS = 8    # 每个作品页滚动轮数上限（count=5/页时约可覆盖 30+ 评论）
COMMENT_PAGE_LIMIT = 10      # 最多打开多少个作品页拉评论（控制总耗时 < JOB_TIMEOUT）
CAPTURE_MAX_JSON = 3 * 1024 * 1024

_CLASSIFY_MESSAGE = {
    AUTH_REQUIRED: '平台以游客身份拒绝（空壳响应/需登录），未绕过校验',
    RISK_CONTROL: '触发平台风控（403/429/444 或验证码），未绕过校验',
    SIGNATURE_BROKEN: '接口响应结构异常（可能签名/结构已变化），待核实',
    NOT_FOUND: '平台返回 404，对象不存在',
}


def _fail(message, requires_action=False, classification=None):
    print(json.dumps({'ok': False, 'error': message[:800], 'requires_action': requires_action,
                      'classification': classification}, ensure_ascii=False))
    sys.exit(1)


def _parse_comment_item(item):
    if not isinstance(item, dict):
        return None
    user = item.get('user') or {}
    reply_id = str(item.get('reply_id') or '')
    return {'platform_comment_id': str(item.get('cid') or ''),
            'author_name': str(user.get('nickname') or '')[:200],
            'content': str(item.get('text') or ''),
            'like_count': item.get('digg_count'),
            'reply_count': item.get('reply_comment_total'),
            'parent_comment_id': None if reply_id in ('', '0') else reply_id,
            'raw': item}


def _parse_work_item(item):
    if not isinstance(item, dict):
        return None
    aweme_id = str(item.get('aweme_id') or '')
    if not aweme_id:
        return None
    stats = item.get('statistics') or {}
    video = item.get('video') or {}
    return {'platform_video_id': aweme_id,
            'title': str(item.get('desc') or '')[:500],
            'source_url': 'https://www.douyin.com/video/' + aweme_id,
            'published_at': item.get('create_time'),
            'duration_ms': video.get('duration'),
            'like_count': stats.get('digg_count'),
            'comment_count': stats.get('comment_count'),
            'share_count': stats.get('share_count'),
            'collect_count': stats.get('collect_count'),
            'play_count': stats.get('play_count'),
            'raw': item}


# ---- 纯解析函数（可测）：从捕获的页面响应中提取数据 ----

def split_hits(hits):
    """把捕获的响应按接口类型分组；同一 URL 只保留第一条（去重）。"""
    grouped = {'profile': [], 'post': [], 'comment': []}
    seen = set()
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        url = str(hit.get('url') or '')
        for name, marker in (('profile', PROFILE_MARKER), ('post', POST_MARKER),
                             ('comment', COMMENT_MARKER)):
            if marker in url and (name, url) not in seen:
                seen.add((name, url))
                grouped[name].append(hit)
    return grouped


def extract_profile_fields(profile_hits):
    """从 profile/other 响应提取账号资料；未取得一律 None，不编造。"""
    fields = {'display_name': None, 'follower_count': None,
              'following_count': None, 'works_count': None}
    for hit in reversed(list(profile_hits or [])):
        payload = hit.get('json')
        if not isinstance(payload, dict):
            continue
        user = payload.get('user')
        if not isinstance(user, dict):
            continue
        if fields['display_name'] is None:
            name = str(user.get('nickname') or '').strip()
            if name:
                fields['display_name'] = name[:200]
        for key, target in (('follower_count', 'follower_count'),
                            ('following_count', 'following_count'),
                            ('aweme_count', 'works_count')):
            if fields[target] is None:
                fields[target] = _metric(user.get(key))
    return fields


def extract_work_items(post_hits):
    """合并多条 aweme/post 响应的 aweme_list，按 aweme_id 去重保序。"""
    items = []
    seen = set()
    for hit in post_hits or []:
        payload = hit.get('json')
        if not isinstance(payload, dict):
            continue
        block = payload.get('aweme_list')
        if not isinstance(block, list):
            continue
        for item in block:
            if not isinstance(item, dict):
                continue
            key = str(item.get('aweme_id') or '')
            if key and key not in seen:
                seen.add(key)
                items.append(item)
    return items


def extract_comment_items(comment_hits, max_comments):
    """合并多条评论响应（含 cursor>0 翻页）的 comments，按 cid 去重，截断到上限。"""
    items = []
    seen = set()
    for hit in comment_hits or []:
        payload = hit.get('json')
        if not isinstance(payload, dict):
            continue
        block = payload.get('comments')
        if not isinstance(block, list):
            continue
        for item in block:
            cid = str(item.get('cid') or '') if isinstance(item, dict) else ''
            if cid and cid not in seen:
                seen.add(cid)
                items.append(item)
                if len(items) >= max(1, max_comments):
                    return items
    return items


def build_home_result(post_hits, profile_hits):
    """从主页捕获响应构建 (work_items, profile_fields)。

    页面未发出目标请求：真实 needs_action（AUTH_REQUIRED 语义），不回退假数据；
    空壳/风控按分类如实上报；EMPTY_END 且零作品如实提示需登录态复核。
    """
    if not post_hits:
        raise CollectionFailed(
            '页面未自动加载作品列表接口响应（未捕获 aweme/post）；'
            '游客态可能受限或页面结构变化，需人工复核或登录态，不回退假数据。',
            needs_action=True, classification=AUTH_REQUIRED)
    ok_hits = []
    empty_end = False
    for hit in post_hits:
        classification = classify_api_response(hit.get('status'), hit.get('json'))
        if classification == OK:
            ok_hits.append(hit)
        elif classification == EMPTY_END:
            empty_end = True
        elif classification in (AUTH_REQUIRED, RISK_CONTROL):
            raise CollectionFailed(
                f'作品列表响应被平台拒绝（{classification}）：{_CLASSIFY_MESSAGE[classification]}。',
                needs_action=True, classification=classification)
    items = extract_work_items(ok_hits)
    if not items:
        if empty_end:
            raise CollectionFailed(
                '作品列表正常结束但为空（EMPTY_END）；游客态第一页可能缺最新几条，'
                '需登录态复核，不能当作“账号没有作品”。',
                needs_action=True, classification=EMPTY_END)
        raise CollectionFailed(
            '捕获到作品列表响应但无法解析出作品（SIGNATURE_BROKEN）；接口结构可能已变化。',
            needs_action=False, classification=SIGNATURE_BROKEN)
    return items, extract_profile_fields(profile_hits)


def _capture_into(hits, markers=None):
    """注册 response 捕获：只读页面自身发出的已签名接口响应（200 + 目标路径）。"""
    wanted = markers or (PROFILE_MARKER, POST_MARKER, COMMENT_MARKER)

    def on_response(resp):
        try:
            if resp.status != 200:
                return
            url = resp.url
            if not any(m in url for m in wanted):
                return
            data = resp.json()
            raw = json.dumps(data, ensure_ascii=False)
            if len(raw) > CAPTURE_MAX_JSON:
                return
            hits.append({'url': url[:400], 'status': resp.status, 'json': data})
        except Exception:
            pass
    return on_response


def _scroll(page, rounds):
    for _ in range(rounds):
        try:
            page.mouse.wheel(0, 2400)
        except Exception:
            pass
        page.wait_for_timeout(1500)


def cmd_fetch(args):
    """真实浏览器采集：打开主页/作品页滚动，读取页面自身已签名接口响应。"""
    from patchright.sync_api import sync_playwright
    import browser_fetch
    max_works = max(args.max_works, 1)
    max_comments = max(args.max_comments, 0)
    hits = []
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
            session = douyin_session.attach(context)
            page = context.new_page()
            page.on('response', _capture_into(hits))
            try:
                page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                _fail(f'主页打开失败（网络或页面错误）：{type(exc).__name__}: {exc}'[:700])
            page.wait_for_timeout(max(args.wait_ms, 2000))
            _scroll(page, HOME_SCROLL_ROUNDS)
            final_url = page.url
            home = split_hits(hits)
            try:
                items, profile_fields = build_home_result(home['post'], home['profile'])
            except CollectionFailed as exc:
                _fail(str(exc), requires_action=exc.needs_action,
                      classification=exc.classification)
            works_raw = []
            seen = set()
            for item in items:
                parsed = _parse_work_item(item)
                if parsed and parsed['platform_video_id'] not in seen:
                    seen.add(parsed['platform_video_id'])
                    parsed['comments'] = []
                    works_raw.append(parsed)
            if not works_raw:
                _fail('作品列表响应中没有可解析的作品条目（SIGNATURE_BROKEN）；接口结构可能已变化。',
                      requires_action=False, classification=SIGNATURE_BROKEN)
            out_works = works_raw[:max_works]
            sec_uid = _extract_sec_uid(args.url) or _extract_sec_uid(final_url)
            first_author = (items[0].get('author') or {}) if items else {}
            author_name = first_author.get('nickname') if isinstance(first_author, dict) else ''
            nickname = profile_fields['display_name'] or str(author_name or '')[:200]

            # 评论：逐作品打开视频页，捕获页面自身发出的 comment/list 响应（含翻页）
            if max_comments > 0 and out_works:
                for parsed in out_works[:COMMENT_PAGE_LIMIT]:
                    before = len(hits)
                    video_page = context.new_page()
                    try:
                        video_page.on('response', _capture_into(hits))
                        try:
                            video_page.goto(parsed['source_url'],
                                            wait_until='domcontentloaded', timeout=60000)
                        except Exception:
                            pass
                        video_page.wait_for_timeout(2500)
                        _scroll(video_page, COMMENT_SCROLL_ROUNDS)
                        grouped = split_hits(hits[before:])
                        if not grouped['comment']:
                            # 页面未自动加载评论：如实记录，不否定已采作品
                            parsed['comments_error'] = 'comments_not_loaded'
                            continue
                        blocked = None
                        for hit in grouped['comment']:
                            classification = classify_api_response(hit.get('status'),
                                                                 hit.get('json'))
                            if classification in (AUTH_REQUIRED, RISK_CONTROL):
                                blocked = classification
                                break
                        if blocked:
                            parsed['comments_error'] = blocked
                            continue
                        for item in extract_comment_items(grouped['comment'], max_comments):
                            comment = _parse_comment_item(item)
                            if comment and comment['platform_comment_id']:
                                parsed['comments'].append(comment)
                    finally:
                        video_page.close()
        finally:
            browser.close()

    print(json.dumps({'ok': True, 'source_url': final_url or args.url,
                      'collected_at': _now_ms(),
                      'account': {'platform': PLATFORM, 'external_id': sec_uid,
                                  'display_name': nickname or sec_uid,
                                  'follower_count': profile_fields['follower_count'],
                                  'following_count': profile_fields['following_count'],
                                  'works_count': profile_fields['works_count']},
                      'works': out_works}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 单条视频采集：贴一条链接 → 文案 + 指标 + 公开评论（输出与主页采集同构）
#
# 与主页采集的差别：视频页自己会请求 aweme/detail（已签名），响应里的
# desc / statistics / author 就是这条视频的真实资料；页面同时自动拉
# comment/list。两条都只读页面自身发出的响应，不做签名逆向、不取 Cookie。
# 结构化数据完全没拿到时如实失败：不拿评论反推文案，不编造标题。
# ---------------------------------------------------------------------------

DETAIL_MARKER = 'aweme/v1/web/aweme/detail/'
VIDEO_SCROLL_ROUNDS = 16   # 单条视频评论：实测 8 轮常只到 12 条，16 轮才能逼近 top30
_VIDEO_ID_RE = re.compile(r'/(?:video|note|slides)/(\d{6,})')
_MODAL_ID_RE = re.compile(r'[?&]modal_id=(\d{6,})')

# 视频页评论区在独立滚动容器里（实测：滚 window 不触发翻页，滚这个容器才会）。
# 优先按评论区往上找可滚祖先，找不到再退回路由滚动容器。
_COMMENT_SCROLL_JS = """() => {
  let el = document.querySelector('[data-e2e="comment-list"]') ||
           document.querySelector('.comment-mainContent');
  let scroller = null;
  while (el && el !== document.documentElement) {
    if (el.scrollHeight > el.clientHeight + 20) { scroller = el; break; }
    el = el.parentElement;
  }
  if (!scroller) scroller = document.querySelector('[class*="route-scroll"]');
  if (!scroller) return 'none';
  const before = scroller.scrollTop;
  scroller.scrollTop = before + Math.round(scroller.clientHeight * 0.85);
  return scroller.scrollTop > before ? 'scrolled' : 'stuck';
}"""


def extract_video_id(url):
    """从视频页地址或分享链接里取平台作品 ID；取不到返回空串（调用方不得猜测）。"""
    for pattern in (_VIDEO_ID_RE, _MODAL_ID_RE):
        match = pattern.search(url or '')
        if match:
            return match.group(1)
    return ''


def clean_title_tag(text):
    """页面 <title> 形如「文案 - 抖音」，只去掉平台后缀，其余原样保留。"""
    return re.sub(r'\s*[-–—]\s*抖音\s*$', '', str(text or '').strip()).strip()


def pick_detail(hits):
    """取最后一个非空 aweme_detail；页面可能多次请求，后者通常更完整。"""
    detail = None
    for hit in hits or []:
        if DETAIL_MARKER not in str(hit.get('url') or ''):
            continue
        block = hit.get('json')
        if not isinstance(block, dict):
            continue
        item = block.get('aweme_detail')
        if isinstance(item, dict) and item.get('aweme_id'):
            detail = item
    return detail


def video_comment_items(hits, detail, max_comments):
    """视频页评论：先取页面翻页响应，再补 detail 自带的 comment_list，按 cid 去重。"""
    page_hits = [h for h in (hits or [])
                 if COMMENT_MARKER in str(h.get('url') or '')
                 and 'reply' not in str(h.get('url') or '')]
    items = extract_comment_items(page_hits, max_comments)
    if isinstance(detail, dict) and len(items) < max_comments:
        inline = detail.get('comment_list')
        if isinstance(inline, list):
            seen = {str(x.get('cid') or '') for x in items if isinstance(x, dict)}
            for item in inline:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get('cid') or '')
                if cid and cid not in seen:
                    seen.add(cid)
                    items.append(item)
                    if len(items) >= max(1, max_comments):
                        break
    return items


def pick_filter_detail(hits):
    """页面把作品过滤为不可见时，平台会在 detail 响应里给 filter_detail。

    aweme_detail 为 null 不代表"接口没被调用"，这里把平台自己的原因码取出来，
    避免把"作品被删除/设为仅自己可见"误报成"需要登录"。
    """
    for hit in reversed(hits or []):
        if DETAIL_MARKER not in str(hit.get('url') or ''):
            continue
        block = hit.get('json')
        if not isinstance(block, dict):
            continue
        info = block.get('filter_detail')
        if isinstance(info, dict) and (info.get('filter_reason') or info.get('detail_msg')):
            return info
    return None


def describe_filter(info):
    """把平台过滤原因翻成用户能懂的一句话。"""
    if not isinstance(info, dict):
        return ''
    reason = str(info.get('filter_reason') or '')
    detail = str(info.get('detail_msg') or '').strip()
    if reason.startswith('status_self_see') or '仅自己可见' in detail or '自己的作品' in detail:
        return ('这条作品被作者设为「仅自己可见」（平台原因码 %s）。'
                '匿名和其他账号都读不到，只有作者本人账号登录后能看到。' % reason)
    if reason.startswith('status_deleted') or '删除' in detail or '不见' in detail:
        return '这条作品已被删除或下架（平台原因码 %s），任何账号都读不到。' % reason
    return ('平台拒绝展示这条作品（原因码 %s）：%s' % (reason, detail)) if reason else detail


def _scroll_comments(page, rounds):
    """滚视频页评论区，触发页面自己请求下一页 comment/list。

    实测：滚 window 只加载首屏 5 条；滚评论区的可滚祖先每次多 5 条。
    容器找不到或滚不动时立即停下，不空耗等待。
    """
    for _ in range(max(0, rounds)):
        try:
            state = page.evaluate(_COMMENT_SCROLL_JS)
        except Exception:
            return
        if state in ('none', 'stuck'):
            return
        page.wait_for_timeout(1500)


def _page_text_hint(page):
    """detail 未捕获时的诊断文案：页面公开渲染的原文（用于报错说明，不用于入库）。"""
    for selector in ('[data-e2e="video-desc"]', '[data-e2e="detail-video-desc"]', 'h1'):
        try:
            element = page.query_selector(selector)
        except Exception:
            continue
        if not element:
            continue
        try:
            text = (element.inner_text() or '').strip()
        except Exception:
            continue
        if text:
            return text
    return clean_title_tag(page.title() or '')


def build_video_raw(detail, hits, video_id, max_comments):
    """把视频页捕获内容整理成 import_raw 可消费的 raw（与主页采集同构）。"""
    if not isinstance(detail, dict) or not detail.get('aweme_id'):
        raise CollectionFailed(
            '没有取得这条视频的结构化资料（页面未发出或未捕获 aweme/detail 响应）；'
            '未编造标题与指标，请本人确认登录态或稍后重试。',
            needs_action=True, classification=AUTH_REQUIRED)
    if str(detail.get('aweme_id')) != str(video_id):
        raise CollectionFailed(
            f'页面加载到的作品（{detail.get("aweme_id")}）与链接里的作品（{video_id}）不一致；'
            '未入库任何数据，请重试或直接打开原链接确认。',
            needs_action=True, classification=NOT_FOUND)
    work = _parse_work_item(detail)
    if not work:
        raise CollectionFailed('作品条目解析失败（缺少 aweme_id）；未入库。',
                               needs_action=False, classification=SIGNATURE_BROKEN)
    author = detail.get('author') if isinstance(detail.get('author'), dict) else {}
    sec_uid = str(author.get('sec_uid') or '').strip()
    if not sec_uid:
        raise CollectionFailed('没有取得作者标识（sec_uid）；不能把视频挂到不存在的账号下。',
                               needs_action=True, classification=AUTH_REQUIRED)
    comments = []
    for item in video_comment_items(hits, detail, max_comments):
        parsed = _parse_comment_item(item)
        if parsed and parsed['platform_comment_id']:
            comments.append(parsed)
    work['comments'] = comments
    account = {'platform': PLATFORM, 'external_id': sec_uid,
               'display_name': str(author.get('nickname') or '').strip()[:200] or sec_uid,
               'follower_count': _metric(author.get('follower_count')),
               'following_count': _metric(author.get('following_count')),
               'works_count': _metric(author.get('aweme_count'))}
    return account, work


def cmd_fetch_video(args):
    """打开一条视频页、滚动，读取页面自身已签名的 detail 与 comment 响应。"""
    from patchright.sync_api import sync_playwright
    import browser_fetch
    max_comments = max(args.max_comments, 0)
    hits = []
    final_url = args.url
    session = {'state': 'missing'}
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
            session = douyin_session.attach(context)
            page = context.new_page()
            page.on('response', _capture_into(hits, (DETAIL_MARKER, COMMENT_MARKER)))
            try:
                page.goto(args.url, wait_until='domcontentloaded', timeout=60000)
            except Exception as exc:
                _fail(f'视频页打开失败（网络或页面错误）：{type(exc).__name__}: {exc}'[:700])
            page.wait_for_timeout(max(args.wait_ms, 2500))
            final_url = page.url or args.url
            hint = _page_text_hint(page)
            _scroll(page, 2)                          # 让详情与首屏评论先加载
            _scroll_comments(page, VIDEO_SCROLL_ROUNDS)
            final_url = page.url or final_url
        finally:
            browser.close()
    video_id = extract_video_id(final_url) or extract_video_id(args.url)
    if not video_id:
        _fail('这个链接没有指向单条视频（看起来是主页或合集）；请粘贴具体那一条视频的链接。',
              requires_action=True, classification=NOT_FOUND)
    detail = pick_detail(hits)
    if not isinstance(detail, dict):
        filtered = pick_filter_detail(hits)
        if filtered:
            _fail('没有取得这条视频的结构化资料。' + describe_filter(filtered),
                  requires_action=True, classification=NOT_FOUND)
    try:
        account, work = build_video_raw(detail, hits, video_id, max_comments)
    except CollectionFailed as exc:
        extra = f'（页面上读到文案「{hint[:40]}…」）' if hint else ''
        tail = '' if session.get('state') == 'ok' else '；' + douyin_session.describe(session)
        _fail(str(exc) + extra + tail, requires_action=exc.needs_action,
              classification=exc.classification)
    print(json.dumps({'ok': True, 'source_url': final_url or args.url, 'collected_at': _now_ms(),
                      'account': account, 'works': [work], 'mode': 'single_video',
                      'session': {'state': session.get('state'), 'count': session.get('count', 0)}},
                     ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description='Benchmark collection worker (browser-env python).')
    sub = parser.add_subparsers(dest='command', required=True)
    fetch = sub.add_parser('fetch', help='采集抖音主页作品与公开评论')
    fetch.add_argument('--url', required=True)
    fetch.add_argument('--max-works', type=int, default=MAX_WORKS_DEFAULT)
    fetch.add_argument('--max-comments', type=int, default=MAX_COMMENTS_DEFAULT)
    fetch.add_argument('--wait-ms', type=int, default=5000)
    video = sub.add_parser('fetch-video', help='采集单条抖音视频的文案、指标与公开评论')
    video.add_argument('--url', required=True)
    video.add_argument('--max-comments', type=int, default=MAX_COMMENTS_DEFAULT)
    video.add_argument('--wait-ms', type=int, default=4000)
    args = parser.parse_args()
    if args.command == 'fetch':
        cmd_fetch(args)
    elif args.command == 'fetch-video':
        cmd_fetch_video(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # 子进程任何异常都要以 JSON 失败返回，不能假成功
        _fail(f'{type(exc).__name__}: {exc}')
