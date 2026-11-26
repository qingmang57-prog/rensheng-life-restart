# -*- coding: utf-8 -*-
"""公开笔记页解析（匿名，不带登录态）：小红书图文/视频笔记、视频号分享页。

只读页面自己内嵌的公开数据，不签名、不逆向、不读客户的任何登录态。
取不到就抛错或返回空，由调用方写明「卡在哪一步、下一步怎么办」。
"""
import html as html_lib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from db import now

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
TIMEOUT = 15
MAX_IMAGE_BYTES = 30 * 1024 * 1024
TOP_N = 10
IMAGE_SUFFIX = ('.jpg', '.jpeg', '.png', '.webp', '.gif')


def _get(url, agent=UA, referer=None, timeout=TIMEOUT):
    headers = {'User-Agent': agent, 'Accept-Language': 'zh-CN,zh;q=0.9'}
    if referer:
        headers['Referer'] = referer
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode('utf-8', 'replace')


def initial_state(page):
    """取页面里的 window.__INITIAL_STATE__（小红书用它放笔记数据）。没有返回 {}。"""
    match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>', page, re.S)
    if not match:
        return {}
    raw = re.sub(r'(?<=[:,\[])undefined(?=[,\]}])', 'null', match.group(1))
    try:
        state = json.loads(raw)
        return state if isinstance(state, dict) else {}
    except ValueError:
        return {}


def _count(value):
    """「1.2万」→ 12000，「520」→ 520；读不出返回 None（不冒充 0）。"""
    text = str(value or '').strip().replace(',', '')
    if not text:
        return None
    found = re.match(r'^([\d.]+)\s*([万亿]?)$', text)
    if not found:
        return None
    try:
        number = float(found.group(1))
    except ValueError:
        return None
    return int(number * {'万': 10000, '亿': 100000000}.get(found.group(2), 1))


def _comment(item):
    user = item.get('userInfo') or {}
    return {'id': str(item.get('id') or ''),
            'content': str(item.get('content') or '').strip(),
            'author': str(user.get('nickname') or '').strip(),
            'likes': _count(item.get('likeCount')),
            'replies': item.get('subCommentCount'),
            'published_at': None}


def _comments(detail):
    rows = detail.get('comments')
    if isinstance(rows, dict):
        rows = rows.get('list')
    rows = [_comment(item) for item in (rows or []) if isinstance(item, dict) and item.get('content')]
    rows.sort(key=lambda x: (x['likes'] if isinstance(x['likes'], int) else -1), reverse=True)
    result = {'status': 'ok' if rows else 'empty', 'items': rows, 'top': rows[:TOP_N],
              'total_fetched': len(rows), 'source': '笔记页内嵌的公开评论（不登录只给一页）',
              'collected_at': now(), 'note': '按点赞排序，不登录只能读到页面自带的一页'}
    if not rows:
        result['note'] = '未取到：页面没有带出评论（可能没有评论，或需要登录）'
    return result


def xhs_note(url, note_id=''):
    """小红书笔记：标题、正文、作者、图片清单、类型（video/normal）、页面自带的一页评论。"""
    status, page = _get(url, referer='https://www.xiaohongshu.com/')
    if status != 200:
        raise ValueError('笔记页返回 HTTP %s' % status)
    if '当前笔记暂时无法浏览' in page or '笔记不存在' in page or '无法浏览' in page:
        raise ValueError('这条笔记打不开了（可能已删除、仅自己可见，或分享链接已过期）')
    state = initial_state(page)
    detail_map = ((state.get('note') or {}).get('noteDetailMap') or {})
    key = note_id if note_id in detail_map else (next(iter(detail_map), ''))
    note = ((detail_map.get(key) or {}).get('note') or {})
    if not note:
        raise ValueError('笔记页没有带出正文数据（小红书可能要求登录，或链接缺了访问凭据）')
    # 配图地址的取法照页面数据的实际结构：先 urlDefault，没有再从 infoList 里找默认画质（WB_DFT）那张。
    images, seen = [], set()
    for item in note.get('imageList') or []:
        if not isinstance(item, dict):
            continue
        source = item.get('urlDefault') or ''
        if not source:
            source = next((row.get('url') for row in (item.get('infoList') or [])
                           if isinstance(row, dict) and row.get('imageScene') == 'WB_DFT' and row.get('url')), '')
        source = source or item.get('url') or ''
        if source and source not in seen:
            seen.add(source)
            images.append(source)
    interact = note.get('interactInfo') or {}
    user = note.get('user') or {}
    return {
        'type': str(note.get('type') or ''),
        'title': str(note.get('title') or '').strip(),
        'desc': str(note.get('desc') or '').strip(),
        'author': str(user.get('nickname') or '').strip(),
        'author_id': str(user.get('userId') or ''),
        'likes': _count(interact.get('likedCount')),
        'collects': _count(interact.get('collectedCount')),
        'comments_count': _count(interact.get('commentCount')),
        'cover': images[0] if images else None,
        'images': images,
        'comments': _comments(detail_map.get(key) or {}),
    }


def download_image(url, target):
    """存一张笔记配图到本机（小红书图床要求带来源页）。"""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://www.xiaohongshu.com/'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('这张配图太大（超过 30MB）')
    target.write_bytes(data)
    return target


def _meta(page, prop):
    tag = re.search(r'<meta\s[^>]*property="%s"[^>]*>' % re.escape(prop), page, re.I)
    if not tag:
        return ''
    found = re.search(r'content="([^"]*)"', tag.group(0))
    return html_lib.unescape(found.group(1)).strip() if found else ''


def _walk(node, keys, depth=0):
    """在页面内嵌数据里找第一个叫这个名字的字符串字段（深度限制，防异常结构）。"""
    if depth > 6:
        return ''
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in node.values():
            found = _walk(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:20]:
            found = _walk(value, keys, depth + 1)
            if found:
                return found
    return ''


def channels_meta(url):
    """视频号分享页：匿名只能读到标题/文案/作者/封面，读不到视频。"""
    try:
        _, page = _get(url, MOBILE_UA)
    except urllib.error.HTTPError as exc:
        raise ValueError('视频号页面返回 HTTP %s' % exc.code) from None
    state = initial_state(page)
    ssd = re.search(r'window\.__QMTPL_SSR_DATA__\s*=\s*(\{.*?\})\s*</script>', page, re.S)
    if ssd:
        try:
            merged = state
            extra = json.loads(ssd.group(1))
            if isinstance(extra, dict):
                merged = {**extra, **state}
            state = merged
        except ValueError:
            pass
    title = _meta(page, 'og:title') or _walk(state, ('title',))
    found = re.search(r'<title>([^<]*)</title>', page)
    title = title or (html_lib.unescape(found.group(1)).strip() if found else '')
    desc = _meta(page, 'og:description') or _walk(state, ('desc', 'description', 'content'))
    author = _walk(state, ('nickname', 'authorName', 'author_name'))
    return {'title': title, 'desc': '' if desc == title else desc, 'author': author,
            'cover': _meta(page, 'og:image')}
