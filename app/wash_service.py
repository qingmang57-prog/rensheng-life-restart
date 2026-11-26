"""链接洗稿：直接 HTTP 读正文；被拦/读不到时明确返回原因，由前端走“粘贴原文”。"""
import gzip
import html
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

import prompt_store
import providers
import long_text
import material
import resolver_status
import style_profile
import video_intake
import video_link
from db import uid, now

# 2026-11-05: 可选依赖改为按需导入。trafilatura 会连带 courlan/lxml 等一整棵依赖树，
# 放在模块顶层会让每次启动都从磁盘（尤其网络盘）逐个打开数百个文件。
# 只有真正抽正文时才导入，行为不变：没装/导入失败则退回内置解析器。
_TRAFILATURA = None
_TRAFILATURA_TRIED = False


def _trafilatura_module():
    global _TRAFILATURA, _TRAFILATURA_TRIED
    if not _TRAFILATURA_TRIED:
        _TRAFILATURA_TRIED = True
        try:
            import trafilatura as mod
        except Exception:
            mod = None
        _TRAFILATURA = mod
    return _TRAFILATURA

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 60000
SKIP_TAGS = ('script', 'style', 'noscript', 'template', 'nav', 'footer', 'aside')
BREAK_TAGS = ('p', 'br', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4',
              'h5', 'h6', 'li', 'blockquote', 'tr')
# 视频平台的“正文”其实是口播稿，HTML 里抓不到；不再早退，交给视频解析通道。
VIDEO_HOSTS = ('douyin.com', 'iesdouyin.com', 'kuaishou.com', 'chenzhongtech.com',
               'channels.weixin.qq.com', 'finder.video.qq.com', 'xhslink.com',
               'xiaohongshu.com', 'bilibili.com', 'b23.tv', 'ixigua.com', 'xigua.com')
VIDEO_HINT = ('这是视频链接，页面里没有正文，口播稿在视频里。'
              '正在转到视频解析通道（下载→转写）；也可以直接粘贴口播稿。')

# 2026-11-25（t201）：模型网关对「单条 content」有硬上限（实测 20000 字，超了直接 500）。
# long_text 只在正文 >15000 字时才分段、才校验 payload 长度；正文不长但素材多的时候会漏过去，
# 于是 6460 字公众号 + 个人素材就撞成裸 500。这里在调用前先按真实 payload 估一次长度，
# 超限抛结构化的 TooLongError（前端落到既有的 wash.blocked 分栏，不再显示成 500）。
# 注意：providers.py 里的上限数值不动（全局上下文/计费策略归产品拍板），本卡只做预检。
# 2026-11-25（t205）：t201 的判定太硬——6460 字正文 + 13000 字档案被直接挡掉，而它其实可以
# 「先缩素材、再按额度分段」跑完。现在预检只兜「真装不下」和「模板本身超限」两种情况，
# 其余交给 long_text 的分段循环 + 本文件的 _shrink_payload（正文不截、不让用户手裁）。
PROMPT_CHAR_LIMIT = long_text.PAYLOAD_LIMIT      # 19000，与 long_text 分段的预算一致，留同样的余量
PROMPT_CHAR_MARGIN = 200                         # 估算时给 JSON 键名/转义留的余量


class TooLongError(ValueError):
    """正文＋素材超过模型单次请求上限：给用户看得懂的显式失败，而不是 500。"""

    code = 'wash_too_long'

    def __init__(self, message, chars=0, limit=0, payload_chars=0, system_chars=0,
                 suggest_chars=0, cause='payload'):
        super().__init__(message)
        self.chars = int(chars or 0)
        self.limit = int(limit or 0)
        self.payload_chars = int(payload_chars or 0)
        self.system_chars = int(system_chars or 0)
        self.suggest_chars = int(suggest_chars or 0)
        self.cause = cause or 'payload'

    def as_dict(self):
        return {'error': str(self), 'code': self.code,
                'too_long': {'chars': self.chars, 'limit': self.limit,
                             'payload_chars': self.payload_chars,
                             'system_chars': self.system_chars,
                             'suggest_chars': self.suggest_chars, 'cause': self.cause}}


class PromptConfigError(TooLongError):
    """模板本身超限：属配置问题，不能归咎成用户正文太长（t205）。"""

    code = 'wash_prompt_config'

    def as_dict(self):
        data = super().as_dict()
        data['config_error'] = True
        return data


def _slim_payload(payload, keep_text=None):
    """返回「最省形态」的 payload 副本：只缩旁路信息，正文一个字都不动。

    缩的顺序（先省最不重要的）：
    1. 素材 material_ids/materials —— 上下文参考，缺了照样能改写；
    2. style_samples —— 风格样本，属于锦上添花；
    3. knowledge[].body —— 保留 claim/title（主张层），只清正文层。
    keep_text 给了就用它替换正文（估算下限时用前 SEGMENT_MIN 字）。
    """
    lean = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    if keep_text is not None:
        src = lean.get('source')
        if isinstance(src, dict) and 'text' in src:
            src['text'] = keep_text
    personal = lean.get('personal')
    if isinstance(personal, dict):
        personal['materials'] = []
        personal['material_ids'] = []
        personal['style_samples'] = []
        for item in personal.get('knowledge') or []:
            if isinstance(item, dict):
                item['body'] = ''
    return lean


def _shrink_payload(payload, limit=None):
    """long_text 的 shrink 回调：装不下就按「素材 → 风格样本 → 知识正文」逐层让位。"""
    limit = PROMPT_CHAR_LIMIT if limit is None else limit
    if long_text.payload_size(payload) <= limit:
        return payload
    personal = payload.get('personal')
    if not isinstance(personal, dict):
        return payload
    while personal.get('materials') and long_text.payload_size(payload) > limit:
        personal['materials'] = (personal.get('materials') or [])[:-1]
        ids = personal.get('material_ids') or []
        personal['material_ids'] = ids[:len(personal['materials'])]
    if personal.get('style_samples') and long_text.payload_size(payload) > limit:
        personal['style_samples'] = []
    knowledge = personal.get('knowledge') or []
    index = len(knowledge) - 1
    while long_text.payload_size(payload) > limit and index >= 0:
        item = knowledge[index]
        if isinstance(item, dict):
            item['body'] = ''
        index -= 1
    return payload


def _precheck_prompt(system, payload, text):
    """送模型前的预算判定（t205）：先缩旁路、再分段，只有真装不下才报错。

    与 t201 的区别：t201 看到「正文＋素材超 19000」就直接 413，把本来可以分段/缩素材的
    情况也挡掉了（6460 字正文 + 13000 字档案就是被这么挡的）。现在只判定下限：
    模板本身超限 → PromptConfigError（配置问题，不归咎正文）；
    把素材/样本/知识正文全省掉、正文压到最小分段后仍超限 → 才是真装不下（413 保留）。
    """
    text = text or ''
    system_chars = len(system or '')
    if system_chars > PROMPT_CHAR_LIMIT:
        raise PromptConfigError(
            '模板超限：这次要用的改写模板本身就有 %d 字，超过模型单次上限（约 %d 字），'
            '这属于模板配置问题，不是你的正文太长；本次没有送去改写。请在云端把这条模板精简后重试。'
            % (system_chars, PROMPT_CHAR_LIMIT),
            chars=len(text), limit=PROMPT_CHAR_LIMIT,
            payload_chars=long_text.payload_size(payload), system_chars=system_chars,
            suggest_chars=0, cause='system')
    floor_text = text[:long_text.SEGMENT_MIN]
    floor = _slim_payload(payload, keep_text=floor_text)
    floor_chars = long_text.payload_size(floor)
    if floor_chars <= PROMPT_CHAR_LIMIT:
        return          # 能装下：交给 long_text 缩旁路/分段，正文不截
    payload_chars = long_text.payload_size(payload)
    fixed = max(0, floor_chars - len(floor_text))
    raise TooLongError(
        '装不下：把「个人档案 → 素材」和风格样本全部省略、正文压到 %d 字以后，仍要 %d 字，'
        '超过模型单次上限（约 %d 字），这一次没有送去改写（原文没有被改动）。'
        '先精简「个人档案」里的主张与经历，或把正文删到 %d 字以内，再点「读取并拆方向」。'
        % (len(floor_text), floor_chars, PROMPT_CHAR_LIMIT, max(60, PROMPT_CHAR_LIMIT - fixed - PROMPT_CHAR_MARGIN)),
        chars=len(text), limit=PROMPT_CHAR_LIMIT, payload_chars=payload_chars,
        system_chars=system_chars,
        suggest_chars=max(60, PROMPT_CHAR_LIMIT - fixed - PROMPT_CHAR_MARGIN), cause='fixed')


# 正文质量判定阈值（针对“栏目页/列表页被当成文章”的假成功）。取值来自 15 条真实链接的
# 实测分布：栏目页短行占比 0.98、单行最长 32 字、链接文字占比远高于正文；正常文章
# 短行占比 ≤0.79（公众号分句多）、单行最长 ≥96 字。
MIN_LINES = 5             # 非空段落少于 5 段：像列表/摘要，不是文章
MAX_LINE_NO_PUNCT = 600   # 单段超过 600 字且结尾没有句末标点：像正文被拼成一坨
ANCHOR_DENSITY = 0.5      # 链接文字占页面正文 ≥50%：导航/标题链接为主
ANCHOR_MIN = 20           # 同时至少 20 个链接，排除正常的“相关阅读”少量链接
SHORT_RATIO = 0.85        # ≤25 字的短行占全部段落 ≥85%：一排排标题
SHORT_LINE = 25
SHORT_MIN_LINES = 20      # 短行判定要求至少 20 段，避免短文误伤
THIN_CHARS = 900          # 抽到不足 900 字，且页面呈栏目结构：算“薄”
THIN_DENSITY = 0.3
THIN_SHORT = 0.6
SENT_END = '。！？…；;.!?"”’」』）)'
TITLE_MATCH = 0.4         # <title> 与 <h1> 相似度低于 0.4 视为“与页面主标题不符”



class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.parts = []
        self.skip = 0
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag in BREAK_TAGS:
            self.parts.append('\n')
        if tag == 'title':
            self.in_title = True

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in BREAK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in SKIP_TAGS and self.skip:
            self.skip -= 1
        elif tag in BREAK_TAGS:
            self.parts.append('\n')
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip and data.strip():
            self.parts.append(data.strip())

    def text(self):
        raw = ''.join(self.parts)
        raw = re.sub(r'[ \t\u3000]+', ' ', raw)
        raw = re.sub(r'\n\s*\n+', '\n\n', raw)
        return raw.strip()


def _decode(raw, headers):
    charset = ''
    try:
        charset = headers.get_content_charset() or ''
    except Exception:
        charset = ''
    if not charset:
        match = re.search(br'<meta[^>]+charset=["\']?([a-zA-Z0-9_-]+)', raw[:4096], re.I)
        if match:
            charset = match.group(1).decode('ascii', 'ignore')
    for candidate in (charset, 'utf-8', 'gb18030'):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, 'ignore')
        except (LookupError, ValueError):
            continue
    return raw.decode('utf-8', 'ignore')


def _clean_text(text):
    text = str(text or '')
    text = re.sub(r'[ \t\u3000]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


def _extract_trafilatura(source, url):
    trafilatura = _trafilatura_module()
    if not trafilatura:
        return ''
    try:
        text = trafilatura.extract(source, url=url, include_comments=False,
                                   include_tables=False, favor_recall=True) or ''
    except Exception:
        return ''
    return _clean_text(text)


def _meta(source):
    """页面标题三元组：<title> / og:title / <h1>。取不到 <title> 时按顺序兜底。"""
    def pick_title():
        match = re.search(r'<title[^>]*>(.*?)</title>', source, re.S | re.I)
        return _plain(match.group(1)) if match else ''

    def pick_og():
        for tag in re.findall(r'<meta\b[^>]*>', source, re.I):
            if 'og:title' in tag.lower():
                match = re.search(r'content=["\'](.*?)["\']', tag, re.S | re.I)
                if match:
                    return _plain(match.group(1))
        return ''

    def pick_h1():
        match = re.search(r'<h1[^>]*>(.*?)</h1>', source, re.S | re.I)
        return _plain(match.group(1)) if match else ''

    return {'title': pick_title(), 'og': pick_og(), 'h1': pick_h1()}


def _plain(value):
    text = re.sub(r'<[^>]+>', '', html.unescape(str(value or '')))
    return re.sub(r'[ \t\r\n\u3000]+', ' ', text).strip()


def _headline(meta, host):
    """可读标题：<title> → og:title → h1 → 域名 + 日期，绝不留空。"""
    for key in ('title', 'og', 'h1'):
        if meta.get(key):
            return meta[key][:200]
    return '%s · %s' % (host, now()[:10])


def _similar(a, b):
    from difflib import SequenceMatcher
    norm = lambda s: re.sub(r'[^\w\u4e00-\u9fa5]', '', str(s or '')).lower()
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def _anchor_stats(source):
    """页面链接文字量/链接数。只认“闭合且短”的 <a>，避开未闭合锚点把整页算成链接。"""
    chars = 0
    count = 0
    for match in re.finditer(r'<a\b[^>]*>(.*?)</a>', source, re.S | re.I):
        inner = match.group(1)
        if '<a' in inner.lower():
            continue
        plain = _plain(inner)
        if not plain or len(plain) > 300:
            continue
        chars += len(plain)
        count += 1
    return chars, count


def _quality(text, source, meta):
    """判“像不像一篇文章”。返回 (reasons, metrics)；有 reasons 就必须降级到粘贴原文。"""
    lines = [x.strip() for x in text.split('\n') if x.strip()]
    anchor_chars, anchors = _anchor_stats(source)
    # 链接占比 = 链接文字 /（链接文字 + 正文），避免“整页都是导航”时被除数放大
    density = anchor_chars / max(anchor_chars + len(text), 1)
    short = sum(1 for x in lines if len(x) <= SHORT_LINE)
    ratio = short / len(lines) if lines else 0.0
    max_line = max((len(x) for x in lines), default=0)
    reasons = []
    if len(lines) < MIN_LINES:
        reasons.append('只有 %d 段' % len(lines))
    if any(len(x) > MAX_LINE_NO_PUNCT and x[-1] not in SENT_END for x in lines):
        reasons.append('有超过 %d 字却无句末标点的长段' % MAX_LINE_NO_PUNCT)
    if density >= ANCHOR_DENSITY and anchors >= ANCHOR_MIN:
        reasons.append('链接文字占正文 %d%%（%d 个链接）' % (round(density * 100), anchors))
    if len(lines) >= SHORT_MIN_LINES and ratio >= SHORT_RATIO:
        reasons.append('%d 段中 %d 段是 ≤%d 字的短标题' % (len(lines), short, SHORT_LINE))
    if len(text) < THIN_CHARS and (density >= THIN_DENSITY or ratio >= THIN_SHORT):
        reasons.append('只抽到 %d 字且页面是栏目/列表结构' % len(text))
    headline = meta.get('title') or meta.get('og') or meta.get('h1')
    if not headline:
        reasons.append('页面取不到标题')
    elif meta.get('title') and meta.get('h1') and _similar(meta['title'], meta['h1']) < TITLE_MATCH:
        reasons.append('页面标题与主标题不一致')
    metrics = {'chars': len(text), 'lines': len(lines), 'short_ratio': round(ratio, 2),
               'anchor_density': round(density, 2), 'anchors': anchors, 'max_line': max_line}
    return reasons, metrics


MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
PARSE_MIN_CHARS = 120    # 少于这个字数视为没抽到正文
# 自测抓取：只挑公开、静态、长期存在的文章；可用 IP_RESOLVER_PROBE_URL 覆盖。
DEFAULT_PROBE_URLS = (
    'https://www.ruanyifeng.com/blog/2020/01/weekly-issue-90.html',
    'https://mp.weixin.qq.com/s/uswdg2HdFvHMt7YvFJurUg',
)
# 抓到这些字样说明站点根本没给正文，按真实原因回话，不笼统说“失败”。
BLOCK_HINTS = (
    (('该内容已被发布者删除', '此内容因违规无法查看', '该文章已被删除', '内容已删除', '内容无法查看'),
     '这篇公众号文章已经被删除或收回，读不到了；换一篇，或把正文粘贴进来。'),
    (('链接已过期', '该链接已过期'),
     '这个链接已经失效（公众号返回了错误页）；请重新复制完整链接，或粘贴原文。'),
    # t205：微信对不完整/设限的链接会回一个 200 的「参数错误」页，别笼统说成“已失效”。
    (('参数错误',),
     '公众号返回“参数错误”：这条分享链接不完整或已经失效（常见于链接被截断、只复制了一半，或文章被设置成不可分享）。'
     '请在微信里重新打开这篇文章再复制完整链接，或把正文粘贴进来。'),
    (('环境异常', '完成验证', '安全验证', '访问过于频繁'),
     '微信要求先完成人机验证，直接读取拿不到正文；用手机微信打开后把正文粘贴进来。'),
    (('请在微信客户端打开',),
     '这个链接只能在微信里打开；请在微信里打开后把正文粘贴进来。'),
    (('验证码', '登录后查看', '请先登录', '请登录'),
     '这个页面要求登录或人工验证，直接读不到正文；登录后复制正文粘贴进来。'),
)


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(str(url or '')).hostname or '').lower()
    except ValueError:
        return ''


def _mobile_variant(url):
    """桌面版是脚本挑战、移动版是服务端直出的站点，换成移动域名再试。"""
    host = _host_of(url)
    if host == 'www.toutiao.com' or (host.endswith('.toutiao.com') and host != 'm.toutiao.com'):
        parts = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(('https', 'm.toutiao.com', parts.path, parts.query, ''))
    return url


def _decompress(raw, headers):
    encoding = (headers.get('Content-Encoding') or '').lower()
    if 'gzip' in encoding:
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    if 'deflate' in encoding:
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                return zlib.decompress(raw, wbits)
            except Exception:
                continue
        return raw
    return raw


def _request_page(url, user_agent, timeout):
    """直连取页面。失败时带 _kind，供上层决定要不要换移动端再试。"""
    host = _host_of(url)
    request = urllib.request.Request(url, headers={
        'User-Agent': user_agent,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
    })
    try:
        with urllib.request.build_opener().open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            headers = response.headers
            final_url = response.geturl()
            content_type = (headers.get_content_type() or '').lower()
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            if host.endswith('zhihu.com'):
                reason = '知乎的正文要登录才能看，直接读取被拒（403）；请在知乎打开后把正文粘贴进来。'
            elif code == 401:
                reason = '这个页面需要登录才能看（401）；可以把正文粘贴进来继续。'
            else:
                reason = '对方站点拒绝了直接读取（403），多数是反爬或需要登录；可以把正文粘贴进来继续。'
            return {'ok': False, '_kind': 'blocked', 'reason': reason}
        if code in (404, 410):
            return {'ok': False, '_kind': 'gone', 'reason': '这个链接已经失效（HTTP %d）。可以粘贴原文继续。' % code}
        if code == 429:
            return {'ok': False, '_kind': 'blocked', 'reason': '对方站点限制了访问频率（429），稍后再试，或直接粘贴原文。'}
        return {'ok': False, '_kind': 'http', 'reason': '对方站点返回 HTTP %d。可以粘贴原文继续。' % code}
    except (TimeoutError, socket.timeout):
        return {'ok': False, '_kind': 'timeout',
                'reason': '读取超时（%d 秒没有响应）。网络慢或被限速，可以稍后重试或粘贴原文。' % int(timeout)}
    except urllib.error.URLError as exc:
        inner = getattr(exc, 'reason', exc)
        if isinstance(inner, (TimeoutError, socket.timeout)):
            return {'ok': False, '_kind': 'timeout',
                    'reason': '读取超时（%d 秒没有响应）。网络慢或被限速，可以稍后重试或粘贴原文。' % int(timeout)}
        return {'ok': False, '_kind': 'network',
                'reason': '连不上这个链接（%s）。检查网络后重试，或粘贴原文继续。' % str(inner)[:120]}
    except Exception as exc:
        return {'ok': False, '_kind': 'network', 'reason': '读取失败（%s）。可以粘贴原文继续。' % str(exc)[:120]}
    if len(raw) > MAX_BYTES:
        return {'ok': False, '_kind': 'size', 'reason': '这个页面太大了，没有直接读取。可以粘贴原文继续。'}
    if content_type and content_type not in ('text/html', 'application/xhtml+xml', 'text/plain'):
        return {'ok': False, '_kind': 'type', 'reason': '这个链接不是网页正文（%s），请直接粘贴原文。' % content_type}
    return {'ok': True, 'raw': _decompress(raw, headers), 'headers': headers,
            'final_url': final_url, 'content_type': content_type}


def _short_page_reason(source, host):
    for markers, reason in BLOCK_HINTS:
        if any(m in source for m in markers):
            return reason
    if 'weixin.qq.com' in (host or ''):
        return '公众号没有返回正文（文章可能已删除、链接不完整，或需要验证）；请把正文粘贴进来。'
    if re.search(r'<script[^>]*>', source, re.I) and len(source) > 1500:
        return '这个页面的正文是脚本渲染的，直接读取拿不到；可以粘贴原文继续。'
    return '这个页面没有可读正文（可能是首页/导航页，或正文由脚本渲染）；可以粘贴原文继续。'


def _article_from_page(page):
    """把原始页面整理成 {ok,title,text,chars,...}；含标题、正文质量判定。"""
    if not page.get('ok'):
        return page
    final_url = page['final_url']
    host = _host_of(final_url)
    content_type = page['content_type']
    headers = page['headers']
    text_source = _decode(page['raw'], headers)
    if content_type == 'text/plain':
        meta = {'title': '', 'og': '', 'h1': ''}
        text, engine = text_source, 'plain'
    else:
        meta = _meta(text_source)
        builtin = _TextParser()
        try:
            builtin.feed(text_source)
            builtin.close()
            builtin_text = builtin.text()
        except Exception:
            builtin_text = ''
        trafi_text = _extract_trafilatura(text_source, final_url)
        # 2026-11-16：trafilatura 对公众号常只抽到几百字（实测 565 字，内置解析 2398 字），
        # 丢掉一半以上正文就退回内置解析器，否则好文章会被误判成“栏目/列表页”。
        if len(trafi_text) >= PARSE_MIN_CHARS and len(trafi_text) * 2 >= len(builtin_text):
            text, engine = trafi_text, 'trafilatura'
        else:
            text, engine = builtin_text, 'builtin'
    title = _headline(meta, host)
    text = _clean_text(text)[:MAX_TEXT]
    if len(text) < PARSE_MIN_CHARS:
        return {'ok': False, '_kind': 'empty', 'title': title, 'chars': len(text),
                'engine': engine, 'reason': _short_page_reason(text_source, host)}
    reasons, metrics = _quality(text, text_source, meta)
    if reasons:
        return {'ok': False, 'kind': 'list_page', 'title': title, 'chars': len(text),
                'engine': engine, 'quality': metrics,
                'reason': '这条更像是栏目/列表页，不是一篇文章（%s）。'
                          '直接拿它写稿会把别人的标题当成你的素材，所以先停下来；'
                          '把你要的那篇文章原文粘贴进来就能继续。' % '；'.join(reasons)}
    return {'ok': True, 'title': title, 'text': text, 'chars': len(text),
            'url': final_url, 'reason': '', 'engine': engine, 'quality': metrics}


DOUYIN_HOSTS = ('douyin.com',)
DOUYIN_BROWSER_TIMEOUT = 180   # 浏览器兜底的硬上限；实测公开作品 20–110 秒


def _douyin_id(text):
    m = re.search(r'/(?:share/)?(?:video|note|slides)/(\d{8,30})', str(text or ''))
    if m:
        return m.group(1)
    m = re.search(r'(?:modal_id|vid|aweme_id)=(\d{8,30})', str(text or ''))
    return m.group(1) if m else ''


# t196（2026-11-24）：链接分类。短链必须先解析落地地址再判类型，不能凭域名猜。
DOUYIN_SHORT_HOSTS = ('v.douyin.com', 'v.iesdouyin.com')
DOUYIN_USER_RE = re.compile(r'/(?:share/)?user/([A-Za-z0-9_\-\.=]+)')
DOUYIN_ITEM_RE = re.compile(r'/(?:share/)?(video|note|slides)/(\d{8,30})')
HOMEPAGE_PATHS = ('', '/', '/index.html', '/jingxuan', '/discover')


def _resolve_final(url, timeout=12):
    """跟跳转拿落地地址（不下载正文、不取 cookie）；失败就返回原地址。"""
    request = urllib.request.Request(url, headers={
        'User-Agent': MOBILE_UA, 'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9'})
    try:
        with urllib.request.build_opener().open(request, timeout=timeout) as response:
            return response.geturl() or url
    except urllib.error.HTTPError as exc:
        return exc.geturl() or url
    except Exception:
        return url


def _classify_douyin(url, final_url=None):
    """返回 (kind, item_id)；kind ∈ video/note/account/live/dead_link/unknown。"""
    target = str(final_url or url)
    # ?modal_id= / aweme_id= 是「主页里弹出的那条作品」，落在 /user/ 路径上也是作品。
    modal = re.search(r'[?&](?:modal_id|aweme_id|vid)=(\d{8,30})', target)
    if modal:
        return 'video', modal.group(1)
    item = DOUYIN_ITEM_RE.search(target)
    if item:
        return ('note' if item.group(1) in ('note', 'slides') else 'video'), item.group(2)
    if '/live/' in target or 'webcast' in target:
        return 'live', ''
    user = DOUYIN_USER_RE.search(target)
    if user:
        return 'account', user.group(1)
    parsed = urllib.parse.urlsplit(target)
    if parsed.path in HOMEPAGE_PATHS and (parsed.hostname or '').endswith('douyin.com'):
        # 短链点过去落在抖音首页：链接过期或只在 App 内可用，不是「这条视频没有正文」。
        return 'dead_link', ''
    return 'unknown', ''


def classify_link(url, timeout=12):
    """给前端一个「这条链接到底该进哪个框」的判定：作品/图文/账号主页/直播/文章/网页。"""
    url = str(url or '').strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return {'ok': False, 'kind': 'invalid', 'input_url': url,
                'reason': '这看起来不是完整链接，请以 http:// 或 https:// 开头。'}
    host = (parsed.hostname or '').lower()
    result = {'ok': True, 'input_url': url, 'final_url': url, 'platform': '',
              'kind': '', 'video_id': '', 'user_id': '', 'reason': '', 'resolved': False}
    if any(host == h or host.endswith('.' + h) for h in DOUYIN_HOSTS):
        result['platform'] = 'douyin'
        kind, item_id = _classify_douyin(url)
        if kind in ('unknown',):
            final = _resolve_final(url, timeout)
            if final and final != url:
                result['final_url'] = final
                result['resolved'] = True
            kind, item_id = _classify_douyin(url, final)
        if kind == 'dead_link':
            result['ok'] = False
            result['reason'] = ('这条短链已经打不开作品页了（实测跳回抖音首页 %s）；'
                                '请在抖音里重新复制“分享链接”，或直接把文案粘贴进来。'
                                % (result['final_url'] or 'https://www.douyin.com/'))
        result['kind'] = kind
        result['video_id'] = item_id if kind in ('video', 'note') else ''
        result['user_id'] = item_id if kind == 'account' else ''
        return result
    if host == 'mp.weixin.qq.com' or host.endswith('.mp.weixin.qq.com'):
        result['platform'] = 'wechat'
        result['kind'] = 'article'
        return result
    result['platform'] = 'web'
    result['kind'] = 'webpage'
    return result


def _optional_wash_rules():
    """t196：共用写作约束是「可选风格约束」，不是使用许可。

    云端只下发了 wash.stage1/stage2 主模板、还没下发 wash.rules 时，改写照样能做，
    不能因为缺这条约束就抛「需登录获取「洗稿·共用写作约束」」——那句话会被前台当正文读失败。
    """
    try:
        return prompt_store.get('wash.rules')
    except Exception:
        return ''


def _douyin_fail(reason, **extra):
    return {'ok': False, 'kind': 'douyin', 'title': '', 'text': '', 'chars': 0,
            'reason': reason, 'engine': 'douyin', **extra}


# t205：抖音的「正文」只有两种来源——创作者写的描述（通常一两句宣发文案）和口播逐字稿。
# 描述不能当正文用，逐字稿也不能在「读链接」这个同步请求里现做（下载+转写要几十秒到几分钟）。
# 所以这里只做判断：有原生字幕就用；只有描述就交给既有的异步视频 job
# （video_link.start → 下载到 IP_DATA_DIR/artifacts → media.extract_audio → providers.asr）。
def _douyin_video_handoff(desc, canonical, aweme_id, media_url=''):
    """描述当不了正文：返回 kind=video，让前端走 /api/wash/video/start 的异步通道。"""
    reason = ('这条抖音只拿到一句作品描述（%s），口播内容在视频里。'
              '已经按视频通道排队：下载 → 真实语音转写 → 直接接着洗稿（约 1–2 分钟，期间可以离开这一页）。'
              % (desc[:40] + '…' if len(desc) > 40 else desc))
    if not media_url:
        reason = ('这条抖音只拿到一句作品描述（%s），也没拿到播放地址。'
                  '可以重试一次；仍不行就在抖音里重新复制分享链接，或把文案粘贴进来。'
                  % (desc[:40] + '…' if len(desc) > 40 else desc))
    return {'ok': False, 'kind': 'video', 'platform': 'douyin', 'video_id': aweme_id,
            'input_url': canonical, 'title': desc[:60], 'text': '', 'chars': 0,
            'reason': reason, 'engine': 'douyin-feed', 'media_url': media_url,
            'needs_transcript': True}


def _douyin_ok(desc, author, canonical, engine, **extra):
    """ok=True 只用于「readto 正文就是真的文字」的情况：原生字幕或图文笔记的正文。"""
    transcript = str(extra.get('transcript') or '')
    note = ('已取到视频描述和原生字幕；下面用的是字幕文本。' if transcript
            else '图文作品没有口播，下面用的是作品正文。')
    result = {'ok': True, 'kind': 'douyin', 'text': transcript or desc,
              'chars': len(transcript or desc), 'title': desc[:60], 'url': canonical,
              'reason': '', 'author': author, 'engine': engine, 'note': note,
              'text_source': 'transcript' if transcript else 'desc'}
    for key in ('media_url', 'captions', 'transcript', 'method'):
        if extra.get(key) is not None:
            result[key] = extra[key]
    return result



def _douyin_browser_reason(detail):
    text = str(detail or '').strip()
    # video_intake 会在失败信息后面追一句“需要登录态”的通用提示；先按主因归类，别把已删除说成要登录。
    head = text.split('抖音需要登录态')[0].strip().rstrip('。.;；')
    if '未提供' in text or '未取得' in text or '没有返回' in text:
        return ('抖音公开页面没有返回这条视频的可读资料（视频可能已删除、仅粉丝可见或地区限制）；'
                '换一条链接，或直接把文案粘贴进来。')
    if '登录' in text:
        return ('抖音这条视频需要登录态才能稳定读取%s；'
                '可以在包目录双击「登录抖音.bat」扫码登录后重试，或直接把文案粘贴进来。'
                % ('（%s）' % head[:160] if head else ''))
    return '抖音解析未完成：%s' % (head or text)[:200]


def _fetch_douyin(url, timeout=15):
    """抖音作品：跟短链 302 到作品页，取视频描述当正文。

    实测直连会被 __ac_nonce 访问校验拦住，“描述”在页面内嵌 JSON 里而不是 HTML 标签，
    所以最后兜底走本机浏览器（真实 chromium 过校验 + 读页面自己的公开详情接口）。
    拿不到描述就明确说卡在哪，绝不把只有标题的结果当成功。
    """
    final_url, aweme_id = url, _douyin_id(url)
    if not aweme_id:
        page = _request_page(url, MOBILE_UA, timeout)
        final_url = page.get('final_url') or url
        aweme_id = _douyin_id(final_url)
    # t196：先按落地地址分类再谈成败——账号主页/直播/失效短链各有各的去处，
    # 不能都报成「链接里没有找到视频编号」。
    kind, kind_id = _classify_douyin(url, final_url)
    if kind == 'account':
        return _douyin_fail('这是抖音账号主页（%s），不是单条作品；'
                            '要看某条作品的文案，请在抖音里分享那条作品再粘进来。'
                            '账号主页可以到「发布与复盘 → 从别人的作品找依据 → 同类账号库」保存并采集作品榜。'
                            % (final_url or url), kind='douyin_account', final_url=final_url)
    if 'webcast' in final_url or '/live/' in final_url:
        return _douyin_fail('这是直播间链接，不是视频作品页；链接解析只读视频作品的文案。')
    if not aweme_id:
        if kind == 'dead_link':
            return _douyin_fail('这条短链已经打不开作品页了（实测跳回抖音首页 %s）；'
                                '请在抖音里重新复制“分享链接”，或直接把文案粘贴进来。'
                                % (final_url or 'https://www.douyin.com/'),
                                kind='douyin_dead_link', final_url=final_url)
        detail = str(page.get('reason') or '').strip()
        return _douyin_fail('链接里没有找到作品编号（短链可能已失效）%s；请重新复制分享链接，或粘贴文案。'
                            % ('：' + detail if detail else ''), final_url=final_url)
    canonical = 'https://www.douyin.com/video/' + aweme_id
    # 0) 2026-11-17 t67：移动 Feed 免签名快路径（~1 秒，带无水印流/原生字幕）。
    try:
        fast = video_intake.resolve_share(canonical)
        fast_meta = fast.get('metadata') or {}
        fast_desc = str(fast_meta.get('title') or '').strip()
        if fast_desc:
            transcript = str(fast.get('transcript') or '').strip()
            extra = {'media_url': fast.get('media_url'), 'captions': fast.get('captions'),
                     'transcript': transcript, 'method': fast.get('method')}
            if transcript or kind == 'note':
                # 原生字幕 = 创作者自己写的口播文本；图文笔记本来就没有口播，描述即正文。
                return _douyin_ok(fast_desc, str(fast_meta.get('author') or ''), canonical,
                                  'douyin-feed', **extra)
            # t205：只有描述时不能拿它当正文——交给异步视频通道做真实转写（见 _douyin_video_handoff）。
            return _douyin_video_handoff(fast_desc, canonical, aweme_id, fast.get('media_url'))
    except Exception:
        pass
    # 1) 直连公开页：能读到就省掉浏览器（多数情况会撞访问校验，0.2 秒内就返回）。
    try:
        quick = video_intake.public_resolve(canonical)
    except ValueError as exc:
        quick = {'status': 'blocked', 'metadata': {}, 'message': str(exc)}
    meta = quick.get('metadata') or {}
    desc = str(meta.get('title') or '').strip()
    if desc and quick.get('status') in ('ready', 'partial'):
        media_url = quick.get('media_url') or (quick.get('media') or {}).get('url')
        transcript = str(quick.get('transcript') or '').strip()
        if transcript or kind == 'note':
            return _douyin_ok(desc, str(meta.get('author') or ''), canonical, 'douyin-page',
                              media_url=media_url, transcript=transcript)
        return _douyin_video_handoff(desc, canonical, aweme_id, media_url)
    # 2) 浏览器兜底：代价 20–110 秒；公开作品不需要登录态，登录后更稳。
    try:
        slow = video_intake.browser_resolve(canonical, timeout=DOUYIN_BROWSER_TIMEOUT)
    except ValueError as exc:
        return _douyin_fail(_douyin_browser_reason(str(exc)))
    except Exception as exc:
        return _douyin_fail('抖音浏览器解析出错（%s）；可以重试，或直接粘贴文案。' % str(exc)[:120])
    meta = slow.get('metadata') or {}
    desc = str(meta.get('title') or '').strip()
    if len(desc) < 5:
        return _douyin_fail('抖音页面结构可能变了：浏览器打开了作品页，却没读到视频描述；可以粘贴文案继续。')
    media_url = (slow.get('media') or {}).get('url') or slow.get('media_url')
    transcript = str(slow.get('transcript') or '').strip()
    if transcript or kind == 'note':
        return _douyin_ok(desc, str(meta.get('author') or ''), canonical, 'douyin-browser',
                          media_url=media_url, transcript=transcript)
    return _douyin_video_handoff(desc, canonical, aweme_id, media_url)


def fetch_link(url, timeout=15):
    """读网页正文；失败返回 ok=False + 人话原因，不抛异常。

    2026-11-16：桌面直连没拿到正文时，再用移动端 UA（头条类换 m. 域名）重试一次——
    头条桌面版是脚本挑战（0 字），移动版是服务端直出（实测 3390 字）。
    抖音在视频通道之前单独接：先取视频描述，取不到再明说卡在哪。
    每次真实结果都写进 resolver_status，设置页「链接解析」只按实测结果报状态。
    """
    url = str(url or '').strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return {'ok': False, 'reason': '这看起来不是完整链接，请以 http:// 或 https:// 开头。'}
    host = (parsed.hostname or '').lower()
    if any(host == h or host.endswith('.' + h) for h in DOUYIN_HOSTS):
        started = time.monotonic()
        result = _fetch_douyin(url, timeout)
        elapsed = int((time.monotonic() - started) * 1000)
        result['elapsed_ms'] = elapsed
        resolver_status.record(result, url, elapsed)
        return result
    if any(host == h or host.endswith('.' + h) for h in VIDEO_HOSTS):
        # 不再提示“不支持”：交给 video_link 的既有视频通道（下载→转写→整理）。
        platform = video_link.platform_of(url)
        return {'ok': False, 'kind': 'video', 'platform': platform,
                'video_id': video_link.video_id_of(url),
                'input_url': url, 'reason': VIDEO_HINT}
    started = time.monotonic()
    result = _article_from_page(_request_page(url, UA, timeout))
    if not result.get('ok') and result.get('_kind') in ('empty', 'blocked'):
        second = _article_from_page(_request_page(_mobile_variant(url), MOBILE_UA, timeout))
        if second.get('ok'):
            second['_via'] = 'mobile'
            result = second
    elapsed = int((time.monotonic() - started) * 1000)
    result['elapsed_ms'] = elapsed
    # ccE 联动：失败分类键要先摘掉再交给 resolver_status.record（载荷与今日完全一致），
    # 但摘掉的值要随返回体带出去，路由侧才能按 FETCH_<kind> 记故障。
    _fk = result.pop('_kind', None)
    resolver_status.record(result, url, elapsed)
    if _fk:
        result['fetch_kind'] = str(_fk)
    return result


def probe_resolver(timeout=12):
    """真实解析一次抖音作品（不是只测公开文章）：拿得到标题/视频流才算解析能力可用；
    抖音快路径失败时再按老口径测一篇公开文章，并把两边原因都记在状态里。"""
    douyin_target = (os.getenv('IP_RESOLVER_PROBE_DOUYIN') or '').strip() or \
        'https://www.douyin.com/jingxuan?modal_id=7693550505402840741'
    started = time.monotonic()
    try:
        fast = video_intake.resolve_share(douyin_target)
        fast_meta = fast.get('metadata') or {}
        fast_title = str(fast_meta.get('title') or '').strip()
        fast_ok = bool(fast_title) and bool(fast.get('media_url'))
        result = {'ok': fast_ok, 'title': fast_title[:80], 'text': fast_title, 'chars': len(fast_title),
                  'reason': '' if fast_ok else '抖音快路径没返回标题或视频流'}
    except Exception as exc:
        result = {'ok': False, 'reason': '抖音快路径自测失败（%s）' % str(exc)[:120]}
    resolver_status.record(result, douyin_target, int((time.monotonic() - started) * 1000), source='selftest-douyin')
    if result.get('ok'):
        return resolver_status.snapshot()
    custom = (os.getenv('IP_RESOLVER_PROBE_URL') or '').strip()
    targets = ([custom] if custom else []) + list(DEFAULT_PROBE_URLS)
    for target in targets:
        started = time.monotonic()
        try:
            result = _article_from_page(_request_page(target, UA, timeout))
        except Exception as exc:
            result = {'ok': False, 'reason': '自测出错（%s）' % str(exc)[:120]}
        elapsed = int((time.monotonic() - started) * 1000)
        resolver_status.record(result, target, elapsed, source='selftest')
        if result.get('ok'):
            break
    return resolver_status.snapshot()


CORE_LEGACY_MAX = 5          # legacy_id 编号 01–05 视为根基条目
CORE_CATEGORIES = ('三观', '账号观')
MAX_KNOWLEDGE = 20           # 进提示词的条目上限
ITEM_BODY_CHARS = 400        # 单条正文最多带多少字
TOTAL_BODY_BUDGET = 6000     # 全部正文总预算

def _missing_profile_rule(personal):
    if not personal.get('missing_profile'):
        return ''
    return ('本人还没建档（personal.knowledge 里没有三观条目）：按“普通人第一人称口语”的通用口吻来写——'
            '短句、具体场景、少形容词、不喊口号、不模仿任何博主；不要假设他的三观和经历，'
            '凡需要本人视角的地方一律写【待补：此处需要你的真实经历】，不许编。')


def _baseline_rule(personal):
    if not personal.get('style_samples'):
        return ''
    return '口吻以 personal.style_samples 里的本人原稿为准：句式长短、用词习惯、标点都要向它靠。'


BASE_DIR = Path(__file__).resolve().parent
BASELINE_CONFIG = BASE_DIR / 'voice_baseline.json'
BASELINE_DIR = BASE_DIR / 'voice_samples'
BASELINE_TOTAL = 2400


def _baseline_samples():
    """口吻基准稿：文件清单写在 voice_baseline.json，原稿放 voice_samples/ 目录。

    只在配置里列出的文件会被读取；没有配置就返回空，不猜、不编基准稿。
    """
    try:
        cfg = json.loads(BASELINE_CONFIG.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    samples, budget = [], BASELINE_TOTAL
    per = int(cfg.get('max_chars_per_sample') or 1200)
    for name in cfg.get('files') or []:
        path = BASELINE_DIR / str(name)
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        text = ''.join(text.split())
        text = text[:min(per, budget)]
        if not text:
            continue
        budget -= len(text)
        samples.append({'file': str(name), 'text': text})
        if budget <= 0:
            break
    return samples


def _legacy_number(item):
    match = re.search(r'-(\d+)\s*$', str(item.get('legacy_id') or ''))
    return int(match.group(1)) if match else 0


def _is_core(item):
    """核心标识规则：根基类目（三观/账号观）里编号 01–05 的条目必进提示词。

    这样即便按时间倒序取条，最根基的几条也不会被后来的条目挤出上下文。
    """
    return (item.get('category') in CORE_CATEGORIES
            and 1 <= _legacy_number(item) <= CORE_LEGACY_MAX)


def _body_plain(item):
    return ''.join(str(item.get('body') or '').replace('\r', '\n').split())


def personal_context(store, limit=MAX_KNOWLEDGE, query=''):
    profile = store.get('profile', 'owner') or {}
    items = [x for x in store.list('knowledge', 10000)
             if x.get('scope') != 'example'
             and x.get('content_use') == 'available'
             and x.get('lifecycle') == 'active'
             and x.get('endorsement') not in ('rejected', 'reference_only')]
    # store.list 已按 updated_at 倒序：先放核心条目，再按时间补齐。
    core = sorted([x for x in items if _is_core(x)],
                  key=lambda x: (0 if x.get('category') == '三观' else 1, _legacy_number(x)))
    picked, seen = [], set()
    for item in core + [x for x in items if not _is_core(x)]:
        if item['id'] in seen:
            continue
        seen.add(item['id'])
        picked.append(item)
        if len(picked) >= limit:
            break
    knowledge, budget = [], TOTAL_BODY_BUDGET
    for item in picked:
        body = _body_plain(item)
        body = body[:ITEM_BODY_CHARS] if len(body) > ITEM_BODY_CHARS else body
        body = body[:budget] if budget > 0 else ''
        budget -= len(body)
        knowledge.append({'id': item['id'], 'title': item.get('title'), 'claim': item.get('claim'),
                          'body': body, 'category': item.get('category'),
                          'boundary': str(item.get('boundary') or '')[:600],
                          'legacy_id': item.get('legacy_id')})
    used = [x for x in knowledge if x['claim']]
    return {
        'name': profile.get('name', ''),
        'audience': profile.get('audience', ''),
        'style': profile.get('style', ''),
        'redlines': profile.get('redlines', ''),
        # 档案压缩块：行业、经历、痛点、看完做什么、变现方式、更新频率、片长、禁忌等
        'profile_block': style_profile.profile_block(profile),
        'knowledge': used,
        'knowledge_ids': [x['id'] for x in used],
        'missing_profile': not any(x.get('category') == '三观' for x in used),
        'core_included': [x.get('legacy_id') or '' for x in used if _is_core(x)],
        'style_samples': _baseline_samples(),
    }


def _require_llm():
    if not providers.available()['llm']:
        raise providers.ProviderMissing('生成模型还没连接。可以到「个人档案 → 服务连接」检查后再试。')


def _with_materials(store, personal, query):
    """两层装配：主张层（personal.knowledge 三观卡）+ 素材层（相关的 12 题原话/经历）。"""
    ctx = material.context(store, query, 5)
    personal['materials'] = ctx['items']
    personal['material_ids'] = ctx['ids']
    personal['material_counts'] = ctx['counts']
    return personal


def stage1(store, data):
    _require_llm()
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    text = str(data.get('text') or '').strip()
    if len(text) < 60:
        raise ValueError('参考正文太短了，至少粘贴一段完整内容。')
    url = str(data.get('url') or '')[:2000]
    source_title = str(data.get('source_title') or '')[:200]
    track = str(data.get('track') or '')
    track_label = {'selfmedia': '做自己的自媒体', 'clip': '做切片'}.get(track, '')
    personal = _with_materials(store, personal_context(store), text[:4000])
    # 2026-11-16 缓存：档案块（逐字稳定）提到 messages[0]，与 create.script 共用同一段前缀；
    # 只是换了位置，送进模型的字段内容不变。
    cache_head = str(personal.get('profile_block') or '')
    personal_payload = {k: v for k, v in personal.items() if k != 'profile_block'}
    system = prompt_store.get('wash.stage1',
                              wash_rules=_optional_wash_rules(),
                              missing_profile=_missing_profile_rule(personal),
                              baseline=_baseline_rule(personal),
                              track_suffix='本次赛道：' + track_label + '。' if track_label else '')
    def _payload(seg):
        return {'source': {'title': source_title, 'url': url, 'text': seg},
                'personal': personal_payload}
    def _call(payload, tag):
        value = providers.llm_json(system, payload, stable_prefix=cache_head, tag=tag)
        # t205：把「格式不对」当成这次调用失败，才能被 long_text 的同次重试兜住。
        # 原来在 run() 返回之后才校验，重试永远轮不到它（实测 6460 字偶发 400 就是这个）。
        if not _valid(value):
            raise ValueError('模型这次没按约定格式回（选题/结构/三个角度缺项）')
        return value
    def _valid(value):
        # t205：schema 完整性一起查（label/angle 都要有），否则后置校验仍会绕过重试。
        if not isinstance(value, dict):
            return False
        if not str(value.get('topic') or '').strip() or not str(value.get('structure') or '').strip():
            return False
        angles = value.get('angles')
        if not isinstance(angles, list) or len(angles) != 3:
            return False
        for item in angles:
            if not isinstance(item, dict):
                return False
            if not str(item.get('label') or '').strip() or not str(item.get('angle') or '').strip():
                return False
        return True
    def _merge(results):
        # t205：合并失败时不再「静默只取第一段」——那等于把一段的分析冒充整篇长文。
        # 先让模型做归纳合并；模型也不成，就用全段结果做确定性合并（题目取首段、骨架去重拼接、
        # 角度按出现顺序取前三个且互不相同），并标明 merged_by，让上层/用户知道这是机械合并。
        try:
            merged = long_text.merge_json(
                '输出与输入相同的 JSON：{topic,structure,angles:[{label,angle}]}；topic 不超过 30 字，'
                'structure 先用一句话说明骨架再列 3 个短句，angles 必须正好 3 个且两两不同；'
                '重复的合并，不新增分段里没有的判断。', results, 'wash_stage1')
            if _valid(merged):
                merged['merged_by'] = 'model'
                return merged
        except Exception:
            pass
        usable = [part for part in results if _valid(part)]
        if not usable:
            raise ValueError('分段结果都不合约定格式（选题/结构/三个角度缺项），本次未保存；'
                             '原始正文没有被改动，可以直接重试一次。')
        topic = str(usable[0].get('topic') or '').strip()[:160]
        seen_struct, structs = set(), []
        for part in usable:
            for line in str(part.get('structure') or '').splitlines():
                line = line.strip()
                if line and line not in seen_struct:
                    seen_struct.add(line)
                    structs.append(line)
        angles, seen_label = [], set()
        for part in usable:
            for item in part.get('angles') or []:
                label = str(item.get('label') or '').strip()[:40]
                angle = str(item.get('angle') or '').strip()[:500]
                if label and angle and label not in seen_label:
                    seen_label.add(label)
                    angles.append({'label': label, 'angle': angle})
                if len(angles) == 3:
                    break
            if len(angles) == 3:
                break
        if len(angles) != 3:
            raise ValueError('分段结果里凑不出三个互不相同的角度，本次未保存；'
                             '原始正文没有被改动，可以直接重试一次。')
        return {'topic': topic, 'structure': '\n'.join(structs)[:1000], 'angles': angles,
                'merged_by': 'local', 'segments': len(usable)}
    # t201：正文不长、但素材多的时候网关会以「单条 content 太长」回 500；送模型前先预检。
    _precheck_prompt(system, _payload(text), text)
    result = long_text.run(text, _payload, _call, 'wash_stage1', shrink=_shrink_payload, merge=_merge)
    topic = str(result.get('topic') or '').strip()[:160]
    structure = str(result.get('structure') or '').strip()[:1000]
    angles = result.get('angles')
    if not topic or not structure or not isinstance(angles, list) or len(angles) != 3:
        raise ValueError('模型没有返回约定格式的选题、结构和三个角度，本次未保存。')
    clean = []
    for item in angles:
        if not isinstance(item, dict):
            raise ValueError('角度格式无效，本次未保存。')
        label = str(item.get('label') or '').strip()[:40]
        angle = str(item.get('angle') or '').strip()[:500]
        if not label or not angle:
            raise ValueError('角度缺少说明，本次未保存。')
        clean.append({'label': label, 'angle': angle})
    plan = store.put('wash_plan', {
        'topic': topic, 'structure': structure, 'angles': clean,
        'merged_by': result.get('merged_by') or 'model',
        'source_text': text[:MAX_TEXT], 'source_url': url, 'source_title': source_title,
        'track': track, 'status': 'planned', 'project_id': None, 'chosen_angle': None,
        'knowledge_ids': personal.get('knowledge_ids') or [],
        'material_ids': personal.get('material_ids') or [],
    })
    return {'plan_id': plan['id'], 'topic': topic, 'structure': structure,
            'angles': clean, 'source_title': source_title, 'source_url': url,
            'chars': len(text), 'track': track, 'created_at': plan['created_at'],
            'merged_by': plan.get('merged_by') or 'model'}


def _parts_valid(parts):
    if not isinstance(parts, list) or not 3 <= len(parts) <= 5:
        raise ValueError('生成稿缺少开头、主体或结尾，本次未保存。')
    labels = {'hook': '开头', 'content': '主体', 'anchor': '让人记住的那一句',
              'quote': '值得被转发的一句话', 'ending': '结尾'}
    order = ['hook', 'content', 'anchor', 'quote', 'ending']
    result, seen = [], set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('生成稿段落格式无效。')
        role = str(part.get('role') or '')
        if role not in labels or role in seen:
            raise ValueError('生成稿段落角色无效或重复。')
        text = str(part.get('text') or '').strip()
        if role in ('hook', 'content', 'ending') and not text:
            raise ValueError('生成稿缺少开头、主体或结尾，本次未保存。')
        if len(text) > 12000:
            raise ValueError('生成稿单段过长，本次未保存。')
        seen.add(role)
        result.append({'id': role + '_01', 'role': role, 'label': labels[role], 'text': text})
    return sorted(result, key=lambda x: order.index(x['role']))


def stage2(store, data):
    _require_llm()
    if not isinstance(data, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    plan_id = str(data.get('plan_id') or '')
    plan = store.get('wash_plan', plan_id)
    if not plan:
        raise KeyError('这次拆题记录已经不在了，请重新贴一次链接。')
    angles = plan.get('angles') or []
    try:
        index = int(data.get('index'))
    except (TypeError, ValueError):
        raise ValueError('请选择一个方向。') from None
    if index < 0 or index >= len(angles):
        raise ValueError('这个方向不在可选范围内。')
    angle = angles[index]
    personal = _with_materials(store, personal_context(store),
                               (str(plan.get('topic') or '') + str(plan.get('structure') or '')
                                + str(angle.get('angle') or '') + str(plan.get('source_text') or '')[:2000]))
    track = plan.get('track') or ''
    track_rule = {'selfmedia': '按“做自己的自媒体”的口播稿来写：第一人称，讲清楚观点。',
                  'clip': '按“做切片”的解说稿来写：开头更快，三句内交代看点。'}.get(track, '')
    # 2026-11-16 缓存：同 stage1，档案块提到 messages[0] 作共享前缀。
    cache_head = str(personal.get('profile_block') or '')
    personal_payload = {k: v for k, v in personal.items() if k != 'profile_block'}
    system = prompt_store.get('wash.stage2',
                              track_rule=track_rule,
                              wash_rules=_optional_wash_rules(),
                              missing_profile=_missing_profile_rule(personal),
                              baseline=_baseline_rule(personal))
    def _payload(seg):
        return {'topic': plan.get('topic'), 'structure': plan.get('structure'),
                'angle': angle, 'personal': personal_payload,
                'source': {'title': plan.get('source_title'), 'url': plan.get('source_url'),
                           'text': seg}}
    def _call(payload, tag):
        value = providers.llm_json(system, payload, stable_prefix=cache_head, tag=tag)
        # t205：格式不对 = 这次调用失败，才轮得到 long_text 的同次重试（同 stage1 的理由）。
        try:
            _parts_valid((value or {}).get('parts') if isinstance(value, dict) else None)
        except ValueError as exc:
            raise ValueError('模型这次没按约定格式回（%s）' % str(exc)[:80]) from None
        return value
    def _merge(results):
        try:
            merged = long_text.merge_json(
                '仅返回 {"parts":[{"role","text"}]}，按 hook/content/ending 顺序给出；'
                '把多段讲稿合成一份完整口播稿，重复的合并，不新增原文没有的判断。',
                results, 'wash_stage2')
            if isinstance(merged, dict):
                _parts_valid(merged.get('parts'))
                return merged
        except Exception:
            pass
        # t205：合并失败不再「静默取第一段」（那会把开头一半的分析当整篇）。用全段做确定性拼接：
        # 开头取第一段、主体按顺序拼接、结尾取最后一段，anchor/quote 取首个出现的。
        good = []
        for part in results:
            try:
                good.append(_parts_valid((part or {}).get('parts')))
            except (AttributeError, ValueError):
                continue
        if not good:
            raise ValueError('分段生成的稿子都不合约定格式（缺开头/主体/结尾），本次未保存；'
                             '原始正文没有被改动，可以直接重试一次。')
        def _pick(role, first=True):
            seq = good if first else list(reversed(good))
            for seg in seq:
                for item in seg:
                    if item['role'] == role and item['text'].strip():
                        return item['text']
            return ''
        hook = _pick('hook')
        ending = _pick('ending', first=False)
        body = '\n\n'.join(item['text'].strip() for seg in good for item in seg
                           if item['role'] == 'content' and item['text'].strip())
        if not hook or not ending or not body:
            raise ValueError('分段生成的稿子拼不出完整的开头/主体/结尾，本次未保存；'
                             '原始正文没有被改动，可以直接重试一次。')
        merged_parts = [{'role': 'hook', 'text': hook}, {'role': 'content', 'text': body},
                        {'role': 'ending', 'text': ending}]
        anchor = _pick('anchor')
        quote = _pick('quote')
        if anchor:
            merged_parts.insert(2, {'role': 'anchor', 'text': anchor})
        if quote:
            merged_parts.insert(len(merged_parts) - 1, {'role': 'quote', 'text': quote})
        return {'parts': merged_parts, 'merged_by': 'local', 'segments': len(good)}
    # t201：同 stage1，送模型前先预检（生成整稿这一步素材更多，同样会撞网关上限）。
    _precheck_prompt(system, _payload(str(plan.get('source_text') or '')), str(plan.get('source_text') or ''))
    result = long_text.run(plan.get('source_text') or '', _payload, _call, 'wash_stage2',
                           shrink=_shrink_payload, merge=_merge)
    parts = _parts_valid(result.get('parts'))
    title = str(plan.get('topic') or '链接洗稿')[:160]
    source = store.put('source', {
        'title': title, 'url': plan.get('source_url') or '', 'share_text': '',
        'text': plan.get('source_text') or '', 'kind': 'link' if plan.get('source_url') else 'text',
        'status': 'ready',
    })
    total = sum(len(p['text']) for p in parts)
    project = store.put('project', {
        'title': title, 'source_id': source['id'], 'draft': parts, 'draft_revision': 1,
        'is_test': False, 'draft_changed': True, 'confirmed_version_id': None,
        'stage': 'draft', 'knowledge_ids': personal.get('knowledge_ids') or [],
        'tags': ['链接洗稿'], 'director': [], 'title_origin': 'automatic',
        'selected_topic': {'id': 'wash_' + plan_id, 'title': plan.get('topic'),
                           'angle': angle.get('angle'), 'thesis': plan.get('structure'),
                           'validation': 'unverified'},
        'creation_brief': {'track': track, 'source_url': plan.get('source_url'),
                           'source_title': plan.get('source_title'),
                           'wash_plan_id': plan_id},
        'origin_note': '由链接洗稿生成：' + plan_id,
    })
    store.patch('wash_plan', plan_id, {'status': 'generated', 'project_id': project['id'],
                                       'chosen_angle': angle, 'script_chars': total})
    return {'project_id': project['id'], 'plan_id': plan_id, 'chars': total,
            'angle': angle, 'track': track}


def history(store, limit=10):
    items = []
    for plan in store.list('wash_plan', 50)[:limit]:
        angles = plan.get('angles') or []
        chosen = plan.get('chosen_angle') or {}
        items.append({
            'id': plan['id'], 'topic': plan.get('topic'), 'structure': plan.get('structure'),
            'angles': [a.get('label') for a in angles], 'track': plan.get('track'),
            'project_id': plan.get('project_id'), 'status': plan.get('status'),
            'source_title': plan.get('source_title'), 'source_url': plan.get('source_url'),
            'chosen': chosen.get('label') if isinstance(chosen, dict) else '',
            'created_at': plan.get('created_at'), 'updated_at': plan.get('updated_at'),
        })
    return {'items': items, 'total': len(items)}


def get_plan(store, plan_id):
    plan = store.get('wash_plan', plan_id)
    if not plan:
        raise KeyError('这次拆题记录已经不在了，请重新贴一次链接。')
    return {'plan_id': plan['id'], 'topic': plan.get('topic'), 'structure': plan.get('structure'),
            'angles': plan.get('angles') or [], 'source_title': plan.get('source_title'),
            'source_url': plan.get('source_url'), 'track': plan.get('track'),
            'project_id': plan.get('project_id'), 'status': plan.get('status')}
