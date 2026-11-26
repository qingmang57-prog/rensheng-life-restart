# -*- coding: utf-8 -*-
"""热评：只取公开评论（昵称、正文、点赞数），不带任何登录态。

2026-11-25 本机匿名实测（BV1GJ411x7h7）：
- /x/v2/reply/main  mode=3 = 按热度（点赞 78 万、18 万、20 万…），mode=2 = 按最新；
- 不带 Cookie 一次给 20 条、可翻页；带了浏览器设备标识反而只给 3 条——所以这里不带；
- 不需要签名；接口可能「成功但没数据」（风控），不能当成「没有评论」。

纪律：取不到就写明原因，不编造评论；不采集手机号等隐私字段。
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from db import now

API = 'https://api.bilibili.com'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_ITEMS = 20
TOP_N = 10
TIMEOUT = 12
HEADERS = {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/', 'Accept': 'application/json'}


def _get_json(path, params):
    url = '%s%s?%s' % (API, path, urllib.parse.urlencode(params))
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode('utf-8', 'replace'))


def _row(item):
    content = item.get('content') or {}
    member = item.get('member') or item.get('author') or {}
    likes = item.get('like')
    if likes is None:
        likes = content.get('like')
    created = content.get('dateline') or item.get('ctime')
    published = None
    if isinstance(created, (int, float)) and created > 0:
        published = datetime.fromtimestamp(created, timezone.utc).isoformat()
    return {
        'id': str(item.get('rpid') or item.get('oid') or ''),
        'content': str(content.get('message') or '').strip(),
        'author': str(member.get('uname') or member.get('name') or '').strip(),
        'likes': likes if isinstance(likes, (int, float)) else None,
        'replies': item.get('rcount'),
        'published_at': published,
        'is_pinned': bool(item.get('is_up_top') or item.get('top')),
    }


def _empty(note):
    return {'status': 'missing', 'items': [], 'top': [], 'total_fetched': 0,
            'source': None, 'collected_at': now(), 'note': note}


def bilibili_view(bvid):
    """视频公开信息：aid（热评接口要数字 id）、标题、作者、时长、互动数。"""
    body = _get_json('/x/web-interface/view', {'bvid': str(bvid or '')})
    if body.get('code') != 0 or not isinstance(body.get('data'), dict):
        raise ValueError('B站公开信息接口没有返回这条视频（%s）' % str(body.get('message') or '')[:80])
    data = body['data']
    stat = data.get('stat') or {}
    owner = data.get('owner') or {}
    duration = data.get('duration')
    return {
        'aid': data.get('aid'),
        'title': str(data.get('title') or '').strip(),
        'desc': str(data.get('desc') or '').strip(),
        'author': str(owner.get('name') or '').strip(),
        'author_id': str(owner.get('mid') or ''),
        'duration_ms': int(duration * 1000) if isinstance(duration, (int, float)) else None,
        'published_at': (datetime.fromtimestamp(data['pubdate'], timezone.utc).isoformat()
                         if isinstance(data.get('pubdate'), (int, float)) and data['pubdate'] > 0 else None),
        'cover': data.get('pic'),
        'stats': {'play': stat.get('view'), 'like': stat.get('like'), 'collect': stat.get('favorite'),
                  'share': stat.get('share'), 'comment': stat.get('reply')},
    }


def bilibili(aid, max_items=MAX_ITEMS):
    """按热度取一页热评。aid 传数字 id；接口也接受 BV 号，所以拿不到数字时原样传。"""
    result = _empty('未取到：还没有请求热评')
    try:
        body = _get_json('/x/v2/reply/main', {'type': 1, 'oid': aid, 'mode': 3,
                                              'ps': max_items, 'pn': 1})
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            result['note'] = '未取到：B站限制了本机的评论请求（412），过几分钟再试'
        else:
            result['note'] = '未取到：热评接口返回 HTTP %s' % exc.code
        return result
    except Exception as exc:  # noqa: BLE001  网络类错误统一转成人话
        result['note'] = '未取到：连不上热评接口（%s）' % str(exc)[:120]
        return result
    code = body.get('code')
    data = body.get('data')
    if code != 0 or not isinstance(data, dict):
        result['note'] = '未取到：热评接口没有给数据（%s %s）' % (code, str(body.get('message') or '')[:40])
        return result
    if data.get('v_voucher'):
        # 「成功」但没有评论、只给校验单据 = 风控，不能当成「没有评论」。
        result['note'] = '未取到：B站把这次评论请求当成机器访问拦下了，过几分钟再试'
        return result
    replies = data.get('top_replies') or data.get('replies') or []
    total = int((data.get('cursor') or {}).get('all_count') or 0)
    items = [_row(item) for item in replies if isinstance(item, dict) and item.get('content')]
    items.sort(key=lambda x: (x['likes'] if isinstance(x['likes'], (int, float)) else -1), reverse=True)
    result.update({'items': items, 'top': items[:TOP_N], 'total_fetched': len(items),
                   'source': 'B站公开评论接口（匿名，按热度）'})
    if not items:
        result['note'] = ('未取到：B站把这次评论请求拦下了（页面显示有 %d 条评论）' % total
                          if total else '未取到：这条视频还没有评论')
    else:
        result['status'] = 'partial' if total > len(items) else 'ok'
        result['note'] = ('按热度取前 %d 条（共 %d 条；不登录只能取一页）' % (len(items), total)
                          if total > len(items) else '按热度排序，共 %d 条' % len(items))
    return result
