"""视频链接解析与解剖包：抖音/快手等链接 → 统一识别 → 复用既有视频通道。

分工（不重复实现下载器）：
- 链接归一：本模块只做重定向展开与视频 id 提取；
- 页面/评论/指标：复用 bench_collect.DouyinBenchCollector（patchright 子进程）；
- 媒体下载：复用 video_intake.download_media；
- 音频抽取：复用 media.extract_audio；
- 转写：复用 providers.asr（本地 FunASR → 云端火山，auto 自动切换）；
- 结构/点评/复用清单：只基于已取得的逐字稿，逐句可回源；取不到就写"未取到"。

纪律：
- 每个字段带来源与抓取时间；未知为 None，reason 写清"未取到：原因"，禁止编造数字或评论。
- 评论只存公开内容（昵称、正文、点赞数、时间），不采集手机号等隐私字段。
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import bench_collect
import content_capture
import hot_comments
import media
import media_fetch
import page_notes
import prompt_store
import providers
import long_text
import video_intake
from db import now, uid

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')

VERSION = '1.0'
EXPAND_TIMEOUT = 20
MAX_COMMENTS = 30
TOP_N = 10
# t205：真转写短于这个字数只加「短稿」标注，不当成“没人说话”丢掉。
SHORT_TRANSCRIPT_CHARS = 60
# t205：内存不足是「此刻不可用」——job 内最多等这么久（秒）再重试一次转写；0 表示不等。
ASR_WAIT_SECONDS = int(os.environ.get('IP_WASH_ASR_WAIT') or 120)
# 2026-11-25：抖音之外走统一「取媒体」通道的平台；其余平台仍然如实说没做。
OTHER_PLATFORMS = ('bilibili', 'xiaohongshu', 'wechat_channels')
# 图文笔记最多识别这么多张配图（小红书一条笔记最多 18 张）。
MAX_NOTE_IMAGES = 18
MISSING_ENGINE = ('取视频的组件不在（tools/yt-dlp/yt-dlp.exe）。重新解压完整安装包即可恢复；'
                  '也可以把视频保存到电脑后，直接拖进输入框。')
CHANNELS_HINT = ('视频号只能在微信里看，电脑端取不到视频。要口播逐字稿：在微信里打开这条视频 → '
                 '右上角「…」→ 保存到手机，再把视频拖进输入框，上传后可以直接转写。')
# 实测（2026-11-25）：不登录访问小红书笔记页，页面不带正文数据。所以取不到时不能只报「失败」，
# 要给走得通的路：文字直接粘贴；图片拖进来走本机文字识别（这两条能力现成）。
XHS_FALLBACK = ('小红书不登录时经常不给内容。可以这样做：文字直接粘贴进输入框；'
                '图片保存后拖进输入框，会自动识别图片里的文字。')

PLATFORMS = (
    ('douyin', ('douyin.com', 'iesdouyin.com')),
    ('kuaishou', ('kuaishou.com', 'chenzhongtech.com', 'gifshow.com')),
    ('bilibili', ('bilibili.com', 'b23.tv')),
    ('xiaohongshu', ('xiaohongshu.com', 'xhslink.com')),
    ('wechat_channels', ('channels.weixin.qq.com', 'finder.video.qq.com')),
    ('xigua', ('ixigua.com', 'xigua.com')),
)
SHORT_HOSTS = ('v.douyin.com', 'v.kuaishou.com', 'b23.tv', 'xhslink.com')

# 平台能不能自动解析，由能力表说清楚，不在代码里静默早退。
PLATFORM_NOTES = {
    'douyin': '抖音：浏览器通道可取文案、互动数、评论与播放地址；口播需下载后转写。',
    'kuaishou': '快手：当前没有实现快手解析器（现有浏览器采集只覆盖抖音），'
                '只能打开链接人工核对，或粘贴文案/字幕。',
    'bilibili': 'B站：取音频后本机转写；热评按点赞取前 20 条（不登录只给一页）。',
    'xiaohongshu': '小红书：视频笔记取视频后转写；图文笔记能读到页面时取正文并对配图做本机文字识别'
                   '（不登录时经常读不到，读不到会提示改用粘贴或拖图片）。',
    'wechat_channels': '视频号：电脑端取不到视频，只能读到标题和文案；口播请保存视频后上传。',
    'xigua': '西瓜视频：当前没有实现解析器，未接入。',
    'unknown': '这个域名不在已支持清单里；可以把它当普通网页尝试读正文，或直接粘贴原文。',
}


class VideoLinkError(RuntimeError):
    def __init__(self, message, code='video_failed', requires_action=False):
        super().__init__(message)
        self.code = code
        self.requires_action = requires_action


def _host(url):
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def platform_of(url):
    host = _host(url)
    for name, domains in PLATFORMS:
        if any(host == d or host.endswith('.' + d) for d in domains):
            return name
    return 'unknown'


def video_id_of(url):
    for pattern in (r'/(?:share/)?video/(\d{6,})', r'/(?:note|slides)/(\d{6,})',
                    r'[?&]modal_id=(\d{6,})', r'[?&]vid=(\d{6,})',
                    r'/video/(BV[0-9A-Za-z]{10})', r'/(\d{15,})'):
        match = re.search(pattern, url or '')
        if match:
            return match.group(1)
    return ''


def expand(url, timeout=EXPAND_TIMEOUT):
    """展开短链重定向，拿到真实地址；把"失效"和"被拦"分开报。"""
    url = str(url or '').strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return {'ok': False, 'reason': '这看起来不是完整链接，请以 http:// 或 https:// 开头。'}
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,*/*'})
    try:
        with urllib.request.build_opener().open(request, timeout=timeout) as response:
            response.read(256)  # 只取少量字节，避免把整页拖下来
            return {'ok': True, 'url': response.geturl() or url, 'status': response.status}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            reason = '链接已失效（对方返回 404）。'
        elif exc.code in (401, 403, 412):
            reason = '链接被对方拦住（HTTP %s），本机匿名访问读不到；可在浏览器打开后复制真实地址。' % exc.code
        elif exc.code in (429, 444):
            reason = '被对方风控限制（HTTP %s），稍后再试或换网络。' % exc.code
        else:
            reason = '对方返回 HTTP %s。' % exc.code
        return {'ok': False, 'reason': reason, 'status': exc.code, 'url': url}
    except urllib.error.URLError as exc:
        return {'ok': False, 'reason': '连不上这个链接（%s）。' % str(getattr(exc, 'reason', exc))[:120], 'url': url}
    except Exception as exc:
        return {'ok': False, 'reason': '链接展开失败（%s）。' % str(exc)[:120], 'url': url}


def resolve(url):
    """归一：短链 → 真实地址 → 平台 + 视频 id。"""
    raw = str(url or '').strip()
    expanded = expand(raw)
    final = expanded.get('url') or raw
    platform = platform_of(final) if expanded.get('ok') else platform_of(raw)
    if platform == 'unknown' and expanded.get('ok'):
        platform = platform_of(raw)
    if expanded.get('ok'):
        # 有些平台用 200 跳转到错误页/下载引导页，这类要直接说失效，不能当成功。
        broken = None
        if '/404' in final or 'error_code=' in final:
            broken = '链接指向平台的失效页（作品可能已删除、设为私密或地区限制）。'
        elif 'support.weixin.qq.com/update' in final:
            broken = '这个链接跳到了微信下载页：视频号只能在微信里打开，电脑端读不到。' + CHANNELS_HINT
        if broken:
            expanded['ok'] = False
            expanded['reason'] = broken
    video_id = video_id_of(final) or video_id_of(raw)
    if expanded.get('ok') and not video_id and _host(raw) in SHORT_HOSTS:
        expanded['ok'] = False
        expanded['reason'] = '短链没有指向一条有效视频（可能已失效、被删除或跳转到了首页）。'
    canonical = 'https://www.douyin.com/video/' + video_id if platform == 'douyin' and video_id else final
    return {
        'ok': expanded.get('ok', False),
        'reason': expanded.get('reason', ''),
        'status': expanded.get('status'),
        'input_url': raw,
        'final_url': final,
        'canonical_url': canonical,
        'platform': platform,
        'video_id': video_id,
        'short_link': bool(_host(raw) in SHORT_HOSTS or final != raw),
    }


def _stamp():
    return now()


def _field(value, source, note=''):
    return {'value': value, 'source': source, 'collected_at': _stamp(), 'note': note}


def _missing(reason):
    return {'value': None, 'source': None, 'collected_at': _stamp(), 'note': '未取到：' + reason}


def _ordered_media(raw_video):
    play = raw_video.get('play_addr') or {}
    urls = [u for u in (play.get('url_list') or []) if isinstance(u, str)]
    for rate in raw_video.get('bit_rate') or []:
        urls.extend(u for u in ((rate.get('play_addr') or {}).get('url_list') or []) if isinstance(u, str))
    ordered = [u for u in urls if '/playwm/' not in u] + [u for u in urls if '/playwm/' in u]
    return ordered


def _cover(raw_video):
    cover = raw_video.get('cover') or {}
    for key in ('url_list',):
        urls = cover.get(key) or []
        if urls:
            return urls[0]
    origin = raw_video.get('origin_cover') or {}
    urls = origin.get('url_list') or []
    return urls[0] if urls else None


def _comment_row(item):
    user = item.get('user') or {}
    created = item.get('create_time')
    published = None
    if isinstance(created, (int, float)) and created > 0:
        try:
            published = datetime.fromtimestamp(created, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            published = None
    return {
        'id': str(item.get('cid') or ''),
        'content': str(item.get('text') or ''),
        'author': str(user.get('nickname') or ''),
        'likes': item.get('digg_count'),
        'replies': item.get('reply_comment_total'),
        'published_at': published,
        'author_reply': True if (user.get('is_author') or item.get('is_author')) else None,
        'is_pinned': bool(item.get('stick_position')),
    }


def _stats(raw):
    statistics = raw.get('statistics') or {}
    keys = (('play', 'play_count'), ('like', 'digg_count'), ('collect', 'collect_count'),
            ('share', 'share_count'), ('comment', 'comment_count'))
    out = {}
    for name, key in keys:
        value = statistics.get(key)
        if value not in (None, ''):
            field = _field(value, '页面自身 aweme/detail 响应')
            field['note'] = '页面值·抓取时刻 ' + field['collected_at']
            out[name] = field
        else:
            out[name] = _missing('页面没有这个字段')
    return out


def _build_from_detail(raw, info, collection_note=''):
    video = raw.get('video') or {}
    author = raw.get('author') or {}
    published = raw.get('create_time')
    published_at = None
    if isinstance(published, (int, float)) and published > 0:
        try:
            published_at = datetime.fromtimestamp(published, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            published_at = None
    comments = []
    for item in raw.get('comment_list') or []:
        if isinstance(item, dict):
            comments.append(_comment_row(item))
    comments.sort(key=lambda x: (x['likes'] if isinstance(x['likes'], (int, float)) else -1), reverse=True)
    media_urls = _ordered_media(video)
    package = {
        'version': VERSION,
        'platform': info['platform'],
        'platform_note': PLATFORM_NOTES.get(info['platform'], ''),
        'video_id': _field(str(raw.get('aweme_id') or info['video_id']),
                           '页面内嵌 JSON（aweme/detail）' if raw else 'URL'),
        'url': {'input': info['input_url'], 'final': info['final_url'], 'canonical': info['canonical_url'],
                'source': '重定向展开', 'collected_at': _stamp()},
        'title': _field(raw.get('desc') or '', '页面内嵌 JSON（aweme/detail）')
        if raw.get('desc') else _missing('页面没有标题字段'),
        'author': _field(str(author.get('nickname') or ''), '页面内嵌 JSON（aweme/detail）')
        if author.get('nickname') else _missing('页面没有作者字段'),
        'author_id': _field(str(author.get('sec_uid') or ''), '页面内嵌 JSON（aweme/detail）')
        if author.get('sec_uid') else _missing('页面没有作者标识'),
        'published_at': _field(published_at, '页面内嵌 JSON（aweme/detail）')
        if published_at else _missing('页面没有发布时间'),
        'duration_ms': _field(video.get('duration'), '页面内嵌 JSON（aweme/detail）')
        if isinstance(video.get('duration'), (int, float)) else _missing('页面没有时长'),
        'cover': _field(_cover(video), '页面内嵌 JSON（aweme/detail）') if _cover(video) else _missing('页面没有封面地址'),
        'stats': _stats(raw),
        'comments': {
            'status': 'ok' if comments else 'empty',
            'items': comments,
            'top': comments[:TOP_N],
            'total_fetched': len(comments),
            'source': '页面自身请求的评论接口（浏览器会话）',
            'collected_at': _stamp(),
            'note': ('按点赞数降序，最多 %d 条。' % MAX_COMMENTS) if comments else '未取到：评论区没有返回公开评论（可能折叠、关闭或需登录）',
        },
        'media': {
            'url': media_urls[0] if media_urls else None,
            'count': len(media_urls),
            'source': '页面内嵌 JSON（aweme/detail）' if media_urls else None,
            'note': '' if media_urls else '未取到：页面没有播放地址',
        },
        'collection_note': collection_note,
    }
    return package


def _transcript(package, info, data_dir, progress):
    """下载 → 抽音频 → 转写；每一步失败都写清原因，不静默。"""
    note = []
    url = (package.get('media') or {}).get('url')
    if not url:
        package['transcript'] = {'status': 'missing', 'text': '', 'chars': 0,
                                 'reason': '未取到：页面没有播放地址，无法下载', 'route': None}
        return package
    if not media.binary('ffmpeg'):
        package['transcript'] = {'status': 'missing', 'text': '', 'chars': 0,
                                 'reason': '未取到：没有找到 ffmpeg，无法抽音频（可配置 IP_FFMPEG 或放好 tools/ffmpeg）',
                                 'route': None}
        return package
    key = info['video_id'] or uid('vid')
    target = Path(data_dir) / 'artifacts' / ('video-%s.mp4' % key)
    audio = Path(data_dir) / 'artifacts' / ('video-%s.mp3' % key)
    try:
        progress('下载视频…')
        video_intake.download_media(url, target)
    except Exception as exc:
        package['transcript'] = {'status': 'download_failed', 'text': '', 'chars': 0,
                                 'reason': '下载失败：' + str(exc)[:300], 'route': None}
        return package
    return _asr_into(package, target, audio, progress)


def _asr_into(package, source_path, audio, progress):
    """本机媒体文件 → 抽音频 → 转写。下载由调用方负责（抖音用页面播放地址，其他平台用 media_fetch）。"""
    try:
        progress('抽取音频…')
        if audio.exists():
            # 上一回失败留下的半截音频不能当成品，先清掉再抽
            try:
                audio.unlink()
            except OSError:
                pass
        media.extract_audio(source_path, audio)
    except Exception as exc:
        package['transcript'] = {'status': 'audio_failed', 'text': '', 'chars': 0,
                                 'reason': '抽音频失败：' + str(exc)[:300], 'route': None}
        return package
    # t205：内存不足是「此刻不可用」，不是「没配置」——job 里等一会儿（有上限）再试一次，
    # 避免用户连点两条视频时第二条直接失败；仍需带真实原因失败时如实报。
    asr_ready = providers.available()['asr']
    waited = 0
    while not asr_ready and waited < ASR_WAIT_SECONDS:
        progress('等待上一条转写结束、内存释放…')
        time.sleep(10)
        waited += 10
        asr_ready = providers.available()['asr']
    if not asr_ready:
        # t205：转写能力「此刻」不可用有两种完全不同的原因——没配置 vs 上一条刚跑完内存还没释放。
        # 只报“没配置”会让用户以为永久不可用，所以把 preflight 的真实原因带上。
        detail = ''
        try:
            import local_asr  # noqa: PLC0415  仅在失败时取一次明细
            probs = local_asr.preflight().get('problems') or []
            detail = '；'.join(str(p) for p in probs)[:200]
        except Exception:  # noqa: BLE001
            detail = ''
        hint = ('；等上一条转写结束、内存释放后再点一次即可' if ('内存' in detail) else
                '；可用「粘贴原文」兜底，或稍后重试')
        package['transcript'] = {'status': 'asr_missing', 'text': '', 'chars': 0,
                                 'reason': ('未取到：本机转写此刻不可用（%s）%s' % (detail or '云端语音识别也未配置', hint)),
                                 'detail': detail, 'route': None}
        return package
    try:
        progress('本地/云端转写中…')
        result = providers.asr(audio, stage=progress)
    except Exception as exc:
        package['transcript'] = {'status': 'asr_failed', 'text': '', 'chars': 0,
                                 'reason': '转写失败：' + str(exc)[:400], 'route': None}
        return package
    text = str(result.get('text') or '').strip()
    # t205：短口述不丢、不冒充「没人说话」。真转写出几个字就照实给几个字并标注短稿，
    # 用户需要更多文字时可以再走 OCR / 手动补，而不是被一句“几乎没人说话”挡回去。
    short = bool(text) and len(text) < SHORT_TRANSCRIPT_CHARS
    package['transcript'] = {
        'status': 'ok' if text else 'empty', 'text': text, 'chars': len(text),
        'short': short,
        'note': ('短稿：这条只转出 %d 字口播（可能是短口述，也可能大半是音乐）；需要更多文字可以再走 OCR 或手动补。'
                 % len(text)) if short else '',
        'route': result.get('asr_route'), 'route_note': result.get('asr_route_note'),
        'cues': result.get('cues') or [], 'words': result.get('words') or [],
        'source': '视频音频转写（%s）' % (result.get('asr_route_note') or result.get('asr_route') or ''),
        'collected_at': _stamp(),
        'reason': '' if text else '未取到：音频转写结果为空',
    }
    return package



def _base_package(info):
    """抖音以外平台的解剖包骨架：字段与抖音同形，没取到的一律保留「未取到」。"""
    return {
        'version': VERSION,
        'platform': info['platform'],
        'platform_note': PLATFORM_NOTES.get(info['platform'], ''),
        'video_id': _field(str(info['video_id'] or ''), 'URL'),
        'url': {'input': info['input_url'], 'final': info['final_url'], 'canonical': info['canonical_url'],
                'source': '重定向展开', 'collected_at': _stamp()},
        'title': _missing('公开页没有标题'),
        'author': _missing('公开页没有作者'),
        'author_id': _missing('公开页没有作者标识'),
        'published_at': _missing('公开页没有发布时间'),
        'duration_ms': _missing('公开页没有时长'),
        'cover': _missing('公开页没有封面'),
        'stats': {name: _missing('公开页没有这个字段')
                  for name in ('play', 'like', 'collect', 'share', 'comment')},
        'comments': {'status': 'missing', 'items': [], 'top': [], 'total_fetched': 0,
                     'source': None, 'collected_at': _stamp(), 'note': '未取到：还没有请求热评'},
        'media': {'url': None, 'count': 0, 'source': None, 'note': ''},
        'collection_note': '',
    }


def _apply_meta(package, meta, stats_map, source):
    """把公开元数据填进解剖包；没有的字段保留「未取到」，不补默认值。"""
    for key in ('title', 'author', 'author_id', 'published_at', 'duration_ms', 'cover'):
        value = meta.get(key)
        if value not in (None, ''):
            package[key] = _field(value, source)
    for name, source_key in stats_map:
        value = (meta.get('stats') or {}).get(source_key)
        if value not in (None, ''):
            package['stats'][name] = _field(value, source)


def _bilibili(info, progress):
    package = _base_package(info)
    progress('读取标题与热评…')
    view = {}
    try:
        view = hot_comments.bilibili_view(info['video_id'])
    except Exception as exc:  # noqa: BLE001  元数据读不到不影响转写
        package['collection_note'] = '标题和互动数没读到（%s）；不影响转写。' % str(exc)[:160]
    if view:
        _apply_meta(package, view,
                    (('play', 'play'), ('like', 'like'), ('collect', 'collect'),
                     ('share', 'share'), ('comment', 'comment')),
                    'B站公开接口（web-interface/view）')
    package['comments'] = hot_comments.bilibili(view.get('aid') or info['video_id'])
    return package


def _xiaohongshu(info, data_dir, progress):
    package = _base_package(info)
    progress('读取笔记正文与热评…')
    note = {}
    try:
        note = page_notes.xhs_note(info['final_url'] or info['input_url'], info['video_id'])
    except Exception as exc:  # noqa: BLE001  页面读不到就交给取视频通道
        package['collection_note'] = '笔记页没有读到正文（%s）；改用取视频通道。' % str(exc)[:160]
    if note:
        likes, collects = note.get('likes'), note.get('collects')
        _apply_meta(package, {'title': note.get('title'), 'author': note.get('author'),
                              'author_id': note.get('author_id'), 'cover': note.get('cover'),
                              'stats': {'like': likes, 'collect': collects,
                                        'comment': note.get('comments_count')}},
                    (('like', 'like'), ('collect', 'collect'), ('comment', 'comment')),
                    '小红书笔记页内嵌数据')
        package['comments'] = note['comments']
        if note.get('type') != 'video' and note.get('images'):
            return _xhs_images(package, note, info, data_dir, progress)
    return package


def _xhs_images(package, note, info, data_dir, progress):
    """图文笔记：正文 + 配图文字识别（本机 RapidOCR，不联网识别）。"""
    images = [u for u in (note.get('images') or []) if u][:MAX_NOTE_IMAGES]
    folder = Path(data_dir) / 'artifacts' / ('xhs-' + (info['video_id'] or uid('note')))
    blocks, failed = [], 0
    # 逐行去重：笔记的封面和内页常把正文原样印在图上，同一段话只留一次。
    seen = {line.strip() for line in str(note.get('desc') or '').splitlines() if line.strip()}
    for index, image in enumerate(images, 1):
        progress('识别图片文字（第 %d/%d 张）…' % (index, len(images)))
        target = folder / ('%02d.jpg' % index)
        try:
            page_notes.download_image(image, target)
            lines = content_capture.ocr_image(target, folder)
        except Exception:  # noqa: BLE001  单张失败不拖累整条，数出来写进说明
            failed += 1
            continue
        fresh = []
        for line in lines or []:
            line = str(line).strip()
            if line and line not in seen:
                seen.add(line)
                fresh.append(line)
        if fresh:
            blocks.append('\n'.join(fresh))
    desc = str(note.get('desc') or '').strip()
    text = '\n\n'.join(([desc] if desc else []) + blocks)
    unread = ('，其中 %d 张没读到' % failed) if failed else ''
    package['media'] = {
        'url': None, 'count': len(images), 'source': '笔记页内嵌数据',
        'note': '图文笔记没有口播：正文 %d 字，配图 %d 张%s' % (len(desc), len(images), unread)}
    package['transcript'] = {
        'status': 'ok' if text else 'empty', 'text': text, 'chars': len(text),
        'note': ('图文笔记：这里是笔记正文加配图里识别出的文字（本机识别，可能有错字）' + unread)
                if text else '',
        'route': 'image_ocr' if blocks else None, 'route_note': '图文笔记（正文 + 图片文字识别，本机完成）',
        'cues': [], 'words': [], 'source': '图文笔记正文与配图文字识别（本机，未联网识别）',
        'collected_at': _stamp(), 'reason': '' if text else '未取到：正文和配图里都没有读到文字'}
    return package


def _wechat_channels(info):
    package = _base_package(info)
    try:
        meta = page_notes.channels_meta(info['final_url'] or info['input_url'])
    except Exception as exc:  # noqa: BLE001
        meta = {}
        package['collection_note'] = '视频号页面没有读到（%s）' % str(exc)[:160]
    if meta:
        _apply_meta(package, meta, (), '视频号分享页（匿名）')
    desc = str(meta.get('desc') or '').strip()
    package['media'] = {'url': None, 'count': 0, 'source': None, 'note': '视频号：电脑端取不到视频'}
    package['transcript'] = {
        'status': 'partial' if desc else 'missing', 'text': desc, 'chars': len(desc),
        'note': ('这里是视频号的文案，不是口播逐字稿。' + CHANNELS_HINT) if desc else '',
        'reason': '' if desc else '未取到：' + CHANNELS_HINT,
        'route': None, 'route_note': '视频号分享页文案', 'cues': [], 'words': [],
        'source': '视频号分享页（匿名）', 'collected_at': _stamp()}
    return package


def _build_other(info, data_dir, progress):
    """抖音以外的平台：B站 / 小红书取音频后转写，视频号只给文案。每一步失败都写清原因。"""
    platform = info['platform']
    if platform == 'wechat_channels':
        return _wechat_channels(info)
    if not media_fetch.available():
        raise VideoLinkError(MISSING_ENGINE, code='engine_missing')
    if platform == 'bilibili':
        package = _bilibili(info, progress)
    else:
        package = _xiaohongshu(info, data_dir, progress)
        if package.get('transcript'):
            return package
    key = info['video_id'] or uid('vid')
    stem = Path(data_dir) / 'artifacts' / ('video-%s' % key)
    try:
        stale = Path(data_dir) / 'artifacts' / ('video-%s.mp3' % key)
        if stale.exists():
            # 上回抽取失败会留下半截 mp3，比下载的 m4a 新，重试时会被误当成输入，先清掉
            try:
                stale.unlink()
            except OSError:
                pass
        path, meta = media_fetch.fetch_audio(info['final_url'] or info['input_url'], stem, progress)
    except media_fetch.MediaFetchError as exc:
        reason = '未取到：' + str(exc)
        if platform == 'xiaohongshu':
            reason += ' ' + XHS_FALLBACK
        package['transcript'] = {'status': 'download_failed', 'text': '', 'chars': 0,
                                 'reason': reason, 'route': None}
        return package
    if meta:
        _apply_meta(package, meta,
                    (('play', 'view'), ('like', 'like'), ('comment', 'comment')),
                    '平台页面数据（取媒体引擎）')
    return _asr_into(package, path, Path(data_dir) / 'artifacts' / ('video-%s.mp3' % key), progress)


def _save_package(store, data_dir, package, progress):
    progress('保存解剖包…')
    package['package_id'] = uid('vpk')
    package['created_at'] = now()
    target = Path(data_dir) / 'packages' / 'video-anatomy'
    target.mkdir(parents=True, exist_ok=True)
    markdown = _markdown(package)
    path = target / ('%s.md' % package['package_id'])
    path.write_text(markdown, encoding='utf-8')
    package['markdown_path'] = str(path)
    package['markdown'] = markdown
    try:
        store.put('video_package', package)
    except Exception:
        pass
    return package


def _analysis(package, progress):
    transcript = (package.get('transcript') or {})
    text = transcript.get('text') or ''
    if not text:
        package['interpretation'] = {'status': 'missing',
                                     'reason': '未取到：没有口播逐字稿，无法拆结构做点评',
                                     'segments': [], 'pattern': '', 'good': [], 'bad': [], 'reuse': []}
        return package
    if not providers.available()['llm']:
        package['interpretation'] = {'status': 'llm_missing',
                                     'reason': '未取到：生成模型还没连接，结构和点评暂不生成',
                                     'segments': [], 'pattern': '', 'good': [], 'bad': [], 'reuse': []}
        return package
    progress('拆结构、写点评…')
    cues = transcript.get('cues') or []
    system = prompt_store.get('video_link.analysis')
    def _payload(seg):
        rows = cues[:700]
        if seg != text:
            # 分段时只带落在这一段里的 cue，时间轴仍是全片时间。
            rows = [c for c in rows if str(c.get('text') or '') in seg]
        return {
            'transcript': seg,
            'cues': [{'id': c.get('id'), 'start_ms': c.get('start_ms'), 'end_ms': c.get('end_ms'),
                      'text': c.get('text')} for c in rows],
            'meta': {'title': (package.get('title') or {}).get('value'),
                     'author': (package.get('author') or {}).get('value'),
                     'duration_ms': (package.get('duration_ms') or {}).get('value')},
        }
    def _call(payload, tag):
        return providers.llm_json(system, payload, tag=tag)
    def _stage(index, total, merging):
        progress('正在合并 %d 段拆解结果' % total if merging
                 else '字幕较长，正在拆解第 %d/%d 段' % (index, total))
    def _merge(results):
        try:
            merged = long_text.merge_json(
                '输出与输入相同的 JSON：{segments:[...],pattern,good:[...],bad:[...],reuse:[...]}；'
                'segments 按原顺序合并，重复的引文只留一条，good/bad 各最多 3 条，reuse 最多 5 条，'
                '不新增原文没有的判断。', results, 'video_link_analysis')
            if isinstance(merged, dict) and isinstance(merged.get('segments'), list):
                return merged
        except Exception:
            pass
        merged = {'segments': [], 'pattern': '', 'good': [], 'bad': [], 'reuse': []}
        seen = set()
        for part in results:
            if not isinstance(part, dict):
                continue
            for row in part.get('segments') or []:
                if not isinstance(row, dict):
                    continue
                quote = str(row.get('quote') or '')
                if not quote or quote in seen:
                    continue
                seen.add(quote)
                merged['segments'].append(row)
            for key, cap in (('good', 3), ('bad', 3), ('reuse', 5)):
                for value in part.get(key) or []:
                    value = str(value or '').strip()
                    if value and value not in merged[key] and len(merged[key]) < cap:
                        merged[key].append(value)
            merged['pattern'] = merged['pattern'] or str(part.get('pattern') or '')
        return merged
    try:
        result = long_text.run(text, _payload, _call, 'video_link_analysis',
                               shrink=long_text.fit_cues, progress=_stage, merge=_merge)
    except Exception as exc:
        package['interpretation'] = {'status': 'failed', 'reason': '模型没返回可用结果：' + str(exc)[:200],
                                     'segments': [], 'pattern': '', 'good': [], 'bad': [], 'reuse': []}
        return package
    segments = []
    for item in result.get('segments') or []:
        if not isinstance(item, dict):
            continue
        quote = str(item.get('quote') or '').strip()
        if not quote or quote not in text:
            continue  # 引文回不了源就不要，宁可少一段
        segments.append({'start_s': item.get('start_s'), 'end_s': item.get('end_s'),
                         'what': str(item.get('what') or '')[:120],
                         'quote': quote[:300], 'role': str(item.get('role') or '')[:20],
                         'evidence': 'transcript'})
    def lines(key, cap=6):
        rows = []
        for item in result.get(key) or []:
            value = str(item or '').strip()
            if value:
                rows.append(value[:300])
            if len(rows) >= cap:
                break
        return rows
    if segments and any(s['start_s'] is None for s in segments):
        # 云端转写没有逐句时间戳时，按"引文在全文中的位置"估时间轴，并明确标注是估算。
        duration_s = ((package.get('duration_ms') or {}).get('value') or 0) / 1000.0
        total = max(1, len(text))
        cursor = 0
        for row in segments:
            found = text.find(row['quote'], cursor)
            if found < 0:
                found = text.find(row['quote'])
            found = max(0, found)
            cursor = found + len(row['quote'])
            row['start_s'] = round(duration_s * found / total, 1) if duration_s else None
            row['end_s'] = round(duration_s * cursor / total, 1) if duration_s else None
            row['estimated'] = True
    package['interpretation'] = {
        'status': 'ok', 'reason': '',
        'segments': segments, 'pattern': str(result.get('pattern') or '')[:300],
        'good': lines('good', 3), 'bad': lines('bad', 3), 'reuse': lines('reuse', 5),
        'source': '模型基于逐字稿解读', 'collected_at': _stamp(),
        'note': '点评与结构均为模型解读，每条引文已回源；没有观看画面，不评论镜头与配乐。',
    }
    return package


def _markdown(package):
    def value(field):
        value = (field or {}).get('value')
        return '（未取到）' if value in (None, '') else str(value)

    def note(field):
        return (field or {}).get('note') or ''

    title = value(package.get('title'))
    lines = ['# 视频解剖包 · %s' % title, '',
             '- 平台：%s' % package.get('platform'),
             '- 视频 id：%s' % value(package.get('video_id')),
             '- 作者：%s' % value(package.get('author')),
             '- 发布时间：%s' % value(package.get('published_at')),
             '- 时长：%s' % ((str(value(package.get('duration_ms'))) +
                             '（约 %d 秒）' % (int(package['duration_ms']['value']) // 1000))
                            if (package.get('duration_ms') or {}).get('value') else '（未取到）'),
             '- 链接：%s' % (package.get('url') or {}).get('canonical'),
             '- 抓取时间：%s' % _stamp(), '']
    stats = package.get('stats') or {}
    lines.append('## 互动数据')
    lines.append('| 指标 | 值 | 来源 | 备注 |')
    lines.append('|---|---|---|---|')
    for name in ('play', 'like', 'collect', 'share', 'comment'):
        field = stats.get(name) or {}
        lines.append('| %s | %s | %s | %s |' % (
            name, value(field), field.get('source') or '—', note(field)))
    lines.append('')
    comments = package.get('comments') or {}
    lines.append('## 评论（%s）' % ('按点赞降序' if comments.get('items') else '未取到'))
    if comments.get('items'):
        lines.append('| 点赞 | 作者 | 内容 | 时间 | 作者回复 |')
        lines.append('|---|---|---|---|---|')
        for row in comments['items']:
            lines.append('| %s | %s | %s | %s | %s |' % (
                row.get('likes'), row.get('author'), str(row.get('content')).replace('|', '｜')[:120],
                row.get('published_at') or '—', '是' if row.get('author_reply') else '—'))
    else:
        note = comments.get('note') or '评论区没有返回公开评论'
        lines.append('- %s' % (note if note.startswith('未取到') else '未取到：' + note))
    lines.append('')
    transcript = package.get('transcript') or {}
    lines.append('## 口播逐字稿')
    if transcript.get('text'):
        lines.append('来源：%s；字数：%s' % (transcript.get('source'), transcript.get('chars')))
        lines.append('')
        lines.append(transcript['text'])
    else:
        lines.append('- 未取到：%s' % transcript.get('reason'))
    lines.append('')
    interp = package.get('interpretation') or {}
    lines.append('## 结构拆解')
    if interp.get('segments'):
        lines.append('| 时间 | 这段在干什么 | 原话 | 作用 |')
        lines.append('|---|---|---|---|')
        for row in interp['segments']:
            span = ('约 %ss–%ss' % (row.get('start_s'), row.get('end_s'))) if row.get('estimated') \
                else ('%ss–%ss' % (row.get('start_s'), row.get('end_s')))
            lines.append('| %s | %s | %s | %s |' % (
                span, row.get('what'), str(row.get('quote')).replace('|', '｜'), row.get('role')))
        lines.append('')
        lines.append('整体套路：%s' % (interp.get('pattern') or '未命名'))
    else:
        lines.append('- 未取到：%s' % interp.get('reason'))
    lines.append('')
    lines.append('## 好坏点评')
    for label, key in (('好在哪', 'good'), ('差在哪', 'bad')):
        lines.append('### ' + label)
        rows = interp.get(key) or []
        lines.extend(['- ' + x for x in rows] or ['- 未取到'])
    lines.append('')
    lines.append('## 可复用清单')
    lines.extend(['- ' + x for x in (interp.get('reuse') or [])] or ['- 未取到'])
    lines.append('')
    lines.append('> 纪律：所有数字与评论均来自页面公开数据，未取到的字段保留行并写明原因，不编造。')
    return '\n'.join(lines)


def build(store, data_dir, url, max_comments=MAX_COMMENTS, with_analysis=True, progress=None):
    progress = progress or (lambda *_: None)
    progress('解析链接…')
    info = resolve(url)
    if not info['ok']:
        raise VideoLinkError(info['reason'], code='link_failed')
    platform = info['platform']
    if platform not in ('douyin',) + OTHER_PLATFORMS:
        raise VideoLinkError(PLATFORM_NOTES.get(platform, PLATFORM_NOTES['unknown']),
                             code='platform_unsupported')
    if platform != 'douyin':
        package = _build_other(info, data_dir, progress)
        if with_analysis:
            package = _analysis(package, progress)
        return _save_package(store, data_dir, package, progress)
    if not info['video_id']:
        raise VideoLinkError('这个链接没有指向单条视频（可能是主页或合集），请粘贴具体那一条视频的链接。',
                             code='no_video_id')
    progress('打开视频页，抓文案、互动数与评论（约 1–2 分钟）…')
    package = None
    collection_note = ''
    def collect_once(wait_ms):
        raw = bench_collect.DouyinBenchCollector().collect_video(
            info['canonical_url'], max_comments=max_comments, wait_ms=wait_ms)
        detail = (raw.get('works') or [{}])[0].get('raw') or {}
        result = _build_from_detail(detail, info)
        for item in (raw.get('works') or [{}])[0].get('comments') or []:
            row = _comment_row(item.get('raw') or item)
            if row['id'] and row['id'] not in {x['id'] for x in result['comments']['items']}:
                result['comments']['items'].append(row)
        result['comments']['items'].sort(
            key=lambda x: (x['likes'] if isinstance(x['likes'], (int, float)) else -1), reverse=True)
        result['comments']['items'] = result['comments']['items'][:max_comments]
        result['comments']['top'] = result['comments']['items'][:TOP_N]
        result['comments']['total_fetched'] = len(result['comments']['items'])
        page_total = (result['stats'].get('comment') or {}).get('value')
        partial = isinstance(page_total, (int, float)) and page_total > len(result['comments']['items'])
        result['comments']['status'] = 'partial' if partial else ('ok' if result['comments']['items'] else 'empty')
        if not result['comments']['items']:
            result['comments']['note'] = '未取到：评论区没有返回公开评论（可能折叠、关闭或需登录）'
        elif partial:
            result['comments']['note'] = ('未完整取到：本次取得 %d 条，页面评论数为 %d（可能需登录）；已按点赞降序。'
                                          % (len(result['comments']['items']), page_total))
        result['session'] = raw.get('session') or {}
        return result
    try:
        package = collect_once(4000)
        if with_analysis and not package['comments']['items']:
            # 评论区是否加载有随机性（同一条视频实测 14/0/12 条），最多补抓两次；
            # 重复抓到的互动数要如实比较，页面本身在变就不假装一致。
            first_stats = {k: (v.get('value') if isinstance(v, dict) else v)
                           for k, v in package['stats'].items()}
            for attempt, wait in ((2, 8000), (3, 10000)):
                try:
                    package = collect_once(wait)
                except bench_collect.CollectionFailed as exc:
                    package['stability'] = {'repeat': attempt - 1, 'changed': [],
                                            'note': '补抓评论失败（%s），评论按上一次结果，可能偏少。' % str(exc)[:120]}
                    break
                second_stats = {k: (v.get('value') if isinstance(v, dict) else v)
                                for k, v in package['stats'].items()}
                changed = [k for k in first_stats if first_stats[k] != second_stats[k]]
                package['stability'] = {
                    'repeat': attempt,
                    'changed': changed,
                    'note': ('两次抓取的四项互动数有不一致：%s；页面本身在变（计数会滚动更新），以最后一次为准。'
                             % '、'.join(changed)) if changed else '两次抓取互动数一致。',
                }
                if package['comments']['items']:
                    break
    except bench_collect.BrowserUnavailable as exc:
        collection_note = '浏览器采集环境不可用：' + str(exc)[:300]
    except bench_collect.CollectionFailed as exc:
        collection_note = '浏览器采集未完成：' + str(exc)[:400]
        if exc.classification in ('auth_required', 'risk_control'):
            collection_note += '（需要本人登录/验证，本软件不绕过）'
    except Exception as exc:
        collection_note = '浏览器采集异常：' + str(exc)[:300]
    if package is None:
        # 兜底：公开页面 HTML 里能读到多少算多少，评论仍标未取到。
        try:
            try:
                public = video_intake.resolve_share(info['canonical_url'])
            except ValueError:
                public = video_intake.public_resolve(info['canonical_url'])
            meta = public.get('metadata') or {}
            fake = {'aweme_id': info['video_id'], 'desc': meta.get('title') or '',
                    'author': {'nickname': meta.get('author') or ''},
                    'create_time': None, 'video': {'duration': meta.get('duration_ms')},
                    'statistics': meta.get('metrics') or {}}
            package = _build_from_detail(fake, info, collection_note)
            package['transcript'] = {'status': 'missing', 'text': '', 'chars': 0,
                                     'reason': '未取到：' + (public.get('message') or collection_note or '公开页面没有字幕')}
            package['interpretation'] = {'status': 'missing', 'reason': '未取到：没有口播逐字稿',
                                         'segments': [], 'pattern': '', 'good': [], 'bad': [], 'reuse': []}
        except Exception as exc:
            raise VideoLinkError(collection_note or ('解析失败：' + str(exc)[:200]), code='collect_failed') from None
    package['collection_note'] = collection_note
    package = _transcript(package, info, data_dir, progress)
    if with_analysis:
        package = _analysis(package, progress)
    return _save_package(store, data_dir, package, progress)


# ---------------------------------------------------------------------------
# 后台任务：给洗稿入口和讲稿页共用，前端轮询进度。
# ---------------------------------------------------------------------------
JOBS = {}
JOB_LOCK = threading.Lock()
STEPS = (
    ('link', '解析链接'),
    ('collect', '抓资料与评论'),
    ('transcript', '下载并转写口播'),
    ('analysis', '拆结构、写点评'),
    ('save', '保存解剖包'),
)


def start(store, data_dir, url, with_analysis=True):
    job_id = uid('vjob')
    steps = [{'key': key,
              'label': (label + '（洗稿只用逐字稿，跳过）') if key == 'analysis' and not with_analysis else label,
              'status': 'pending'} for key, label in STEPS]
    with JOB_LOCK:
        JOBS[job_id] = {'id': job_id, 'status': 'running', 'stage': '准备中',
                        'steps': steps, 'result': None, 'error': None,
                        'code': None, 'requires_action': False, 'started_at': now()}

    def mark(key, status):
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            for step in job['steps']:
                if step['key'] == key:
                    step['status'] = status
            job['stage'] = next((s['label'] for s in job['steps'] if s['status'] == 'running'), job['stage'])

    def progress(message):
        # 用进度文案推进分步状态：用户能看到"现在卡在下载还是转写"。
        text = str(message)[:120]
        order = ['link', 'collect', 'transcript', 'analysis', 'save']
        keyword = None
        for kw in ('解析链接', '打开视频页', '下载视频', '抽取音频', '转写中', '拆结构', '保存解剖包'):
            if kw in text:
                keyword = 'link' if kw == '解析链接' else (
                    'collect' if kw == '打开视频页' else (
                        'transcript' if kw in ('下载视频', '抽取音频', '转写中') else (
                            'analysis' if kw == '拆结构' else 'save')))
                break
        with JOB_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            job['stage'] = text
            if keyword:
                target = order.index(keyword)
                for step in job['steps']:
                    idx = order.index(step['key'])
                    if idx < target and step['status'] in ('pending', 'running'):
                        step['status'] = 'done'
                    elif idx == target and step['status'] == 'pending':
                        step['status'] = 'running'

    # 2026-11-14：新线程不继承 threading.local；起线程前快照登录账号与权益，
    # 否则下载后的转写读不到 allow_cloud_asr（会误走云端）。
    try:
        import billing_service as _billing
        billing_context = _billing.capture_context()
    except Exception:  # noqa: BLE001
        _billing = None
        billing_context = {}

    def work():
        if _billing is not None:
            _billing.apply_context(billing_context)
        mark('link', 'running')
        try:
            package = build(store, data_dir, url, with_analysis=with_analysis, progress=progress)
            for step in steps:
                if step['key'] == 'analysis' and not with_analysis:
                    mark('analysis', 'skipped')
                else:
                    mark(step['key'], 'done')
            with JOB_LOCK:
                JOBS[job_id].update({'status': 'done', 'stage': '完成', 'result': package})
        except VideoLinkError as exc:
            with JOB_LOCK:
                for step in JOBS[job_id]['steps']:
                    if step['status'] == 'running':
                        step['status'] = 'failed'
                JOBS[job_id].update({'status': 'failed', 'stage': '失败', 'error': str(exc),
                                     'code': exc.code, 'requires_action': exc.requires_action})
        except Exception as exc:
            with JOB_LOCK:
                for step in JOBS[job_id]['steps']:
                    if step['status'] == 'running':
                        step['status'] = 'failed'
                JOBS[job_id].update({'status': 'failed', 'stage': '失败', 'error': str(exc)[:400],
                                     'code': 'video_failed'})

    threading.Thread(target=work, daemon=True, name='ip-video-' + job_id).start()
    return {'job_id': job_id, 'steps': steps, 'platform': platform_of(url),
            'video_id': video_id_of(url), 'message': '这是视频，正在解析（下载→转写，约 1–2 分钟）'}


def job_status(job_id):
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise KeyError('这个解析任务已经不在了，请重新贴一次链接。')
    return job


def selftest(network_url='', expired_url=''):
    """认链接自检：纯代码用例（短链/长链/不支持的域名/坏输入）+ 可选联网用例。

    只验证识别与归一，不下载视频；联网用例用于验证短链展开和失效链报错文案。
    """
    cases = []

    def check(name, ok, detail):
        cases.append({'case': name, 'ok': bool(ok), 'detail': detail})

    long_url = 'https://www.douyin.com/video/7565812345678901234?previous_page=app_code_link'
    check('长链识别', platform_of(long_url) == 'douyin' and video_id_of(long_url) == '7565812345678901234',
          {'platform': platform_of(long_url), 'video_id': video_id_of(long_url)})
    short_url = 'https://v.douyin.com/iMNPg7mD/'
    check('短链识别（不展开）', platform_of(short_url) == 'douyin' and video_id_of(short_url) == '',
          {'platform': platform_of(short_url), 'video_id': video_id_of(short_url)})
    bili = 'https://www.bilibili.com/video/BV1xx411c7XX'
    check('B站 BV 号', platform_of(bili) == 'bilibili' and video_id_of(bili) == 'BV1xx411c7XX',
          {'platform': platform_of(bili), 'video_id': video_id_of(bili)})
    check('不支持的域名', platform_of('https://example.com/video/123456') == 'unknown',
          platform_of('https://example.com/video/123456'))
    bad = expand('这不是链接')
    check('坏输入不崩', bad['ok'] is False and 'http' in bad['reason'], bad['reason'])
    if network_url:
        r = resolve(network_url)
        check('联网：短链展开', r['ok'] and r['video_id'], {'final_url': r['final_url'][:120],
              'video_id': r['video_id'], 'reason': r['reason']})
    if expired_url:
        r = resolve(expired_url)
        check('联网：失效链给原因', r['ok'] is False and bool(r['reason']),
              {'reason': r['reason'], 'status': r['status']})
    return {'ok': all(c['ok'] for c in cases), 'cases': cases,
            'note': '纯代码用例不访问网络；联网用例只展开链接，不下载视频。'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='视频链接识别自检')
    parser.add_argument('--url', default='', help='联网验证用的真实短链/长链')
    parser.add_argument('--expired', default='', help='联网验证用的失效链接')
    args = parser.parse_args()
    print(json.dumps(selftest(args.url, args.expired), ensure_ascii=False, indent=2))
