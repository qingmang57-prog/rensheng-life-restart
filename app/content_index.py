# -*- coding: utf-8 -*-
"""统一内容索引/引用层。

方案二：底表保持不动（knowledge / flow / project / checkin ...），
这里只维护一张可检索的聚合视图 content_index，给首页计数、全局搜索、
分类筛选、第二大脑「先查本地」提供同一口径。

分类口径（2026-11-14 定案）：11 类合并为 5 类；原分类保留在
category_legacy 与 alias_text 里，所以旧词「三观」仍然能被搜到。

第二轮（2026-11-16）：索引扩到软件里的重要数据，不再只有认知与成长碎片。
- 知识池计数（summary/list 默认）仍只算 knowledge 与未镜像的 growth_fragment，
  所以「全部 227 条」不会被项目、打卡等记录改数。
- 其余类型 pool='brain'，只参与检索，不参与知识池计数。
- 重建不再阻塞启动：bootstrap() 只建表并起后台线程；索引为空时检索走
  即时 LIKE 兜底（直接扫底表 + 打卡 JSONL），重建完成后自动切回索引。
- 打卡明细只在本机：从 <IP_DATA_DIR>/checkin/*.jsonl 读，不上传、不外发。
"""
import json
import math
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))

TARGET_CATEGORIES = ('认知底座', '人性关系', '赚钱逻辑', '操盘手法', '成功心法', '随手记')

# 旧分类 -> 新分类。包含历史上出现过、但当前数据里不存在的兜底值。
CATEGORY_MAP = {
    '三观': '认知底座',
    '人性关系': '人性关系',
    '赚钱逻辑': '赚钱逻辑',
    '驾驭金钱': '赚钱逻辑',
    '账号观': '赚钱逻辑',
    '操盘手法': '操盘手法',
    '事业': '操盘手法',
    '成功心法': '成功心法',
    '随手记': '随手记',
    '视频积累': '随手记',
    '链接积累': '随手记',
    '成长积累': '随手记',
    '第二大脑': '随手记',
    '个人积累': '随手记',
}

# 每个新分类保留的旧词，用于检索、别名匹配与分类瓦片说明。
CATEGORY_ALIASES = {
    '认知底座': ('三观',),
    '人性关系': (),
    '赚钱逻辑': ('驾驭金钱', '账号观'),
    '操盘手法': ('事业',),
    '成功心法': (),
    '随手记': ('视频积累', '链接积累', '成长积累', '第二大脑', '个人积累'),
}

CATEGORY_NOTES = {
    '认知底座': '看世界、看人、看关系的底层判断',
    '人性关系': '和谁在一起、关系怎么处',
    '赚钱逻辑': '钱怎么来、怎么留，账号怎么定位',
    '操盘手法': '生意怎么跑起来',
    '成功心法': '习惯、判断、走多远',
    '随手记': '随手记下的一句话、链接与灵感',
}

# 参与「知识池」计数的类型；其余进索引只为给第二大脑检索用。
POOL_KINDS = ('knowledge', 'growth_fragment')

# 需要进索引的记录类型。新增类型只要在这里登记，重建就会自动带上。
INDEXED_KINDS = (
    'knowledge', 'growth_fragment',
    'flow', 'growth_goal', 'growth_answer',
    'checkin_profile',
    'project', 'script_version', 'artifact',
    'topic', 'bench_work', 'bench_account', 'bench_comment',
    'source', 'news', 'question',
    'interview_session', 'interview_turn', 'creator_profile',
)

KIND_LABELS = {
    'knowledge': '认知',
    'growth_fragment': '成长碎片',
    'flow': '成长档案',
    'growth_goal': '成长目标',
    'growth_answer': '成长问答',
    'checkin_profile': '打卡档案',
    'checkin_day': '每日打卡',
    'project': '项目',
    'script_version': '定稿脚本',
    'artifact': '成片',
    'topic': '选题',
    'bench_work': '对标作品',
    'bench_account': '对标账号',
    'bench_comment': '对标评论',
    'source': '素材来源',
    'news': '行业热点',
    'question': '问过的问题',
    'interview_session': '深挖访谈',
    'interview_turn': '访谈原话',
    'creator_profile': '创作者画像',
}

VIDEO_ARTIFACT_KINDS = ('clean_video', 'packaged_video', 'final_video', 'cover')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS content_index (
    idx_id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    category_legacy TEXT,
    alias_text TEXT,
    scope TEXT,
    lifecycle TEXT,
    recorded_at TEXT,
    goal_ids TEXT,
    snippet TEXT,
    search_text TEXT,
    revision INTEGER,
    indexed_at TEXT,
    kind_label TEXT,
    body TEXT,
    source_url TEXT,
    meta TEXT,
    pool TEXT
);
CREATE INDEX IF NOT EXISTS content_index_category ON content_index(category);
CREATE INDEX IF NOT EXISTS content_index_source ON content_index(source_kind,source_id);
CREATE TABLE IF NOT EXISTS content_index_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS category_migration_log (
    log_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    old_category TEXT,
    new_category TEXT,
    old_data TEXT,
    new_data TEXT,
    batch TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rolled_back_at TEXT
);
CREATE INDEX IF NOT EXISTS category_migration_batch ON category_migration_log(batch);
'''

COLUMNS = ('idx_id', 'source_kind', 'source_id', 'title', 'category', 'category_legacy',
           'alias_text', 'scope', 'lifecycle', 'recorded_at', 'goal_ids', 'snippet',
           'search_text', 'revision', 'indexed_at', 'kind_label', 'body', 'source_url',
           'meta', 'pool')

# 迁移：老库的 content_index 只有前 15 列，缺的列按需补。
_EXTRA_COLUMNS = {
    'kind_label': 'TEXT',
    'body': 'TEXT',
    'source_url': 'TEXT',
    'meta': 'TEXT',
    'pool': "TEXT DEFAULT 'pool'",
}

_ENSURED = set()
_ENSURE_LOCK = threading.Lock()

_REBUILD_LOCK = threading.Lock()
_REBUILD_STATE = {'running': False, 'thread': None, 'last_error': '', 'started_at': None}

# 指纹缓存：连点几下搜索时不必每次都扫一遍底表。
_FP_CACHE = {}
_FP_TTL = 1.5


def ensure(store):
    key = str(store.path)
    if key in _ENSURED:
        return
    with _ENSURE_LOCK:
        if key in _ENSURED:
            return
        with store.connect() as con:
            con.executescript(SCHEMA)
            have = {row['name'] for row in con.execute('PRAGMA table_info(content_index)')}
            for name, decl in _EXTRA_COLUMNS.items():
                if name not in have:
                    con.execute('ALTER TABLE content_index ADD COLUMN %s %s' % (name, decl))
            # pool 列可能是老库后补的，索引必须在补列之后建
            con.execute('CREATE INDEX IF NOT EXISTS content_index_pool ON content_index(pool)')
            # 图谱按 pool+recorded_at 取最近 800 条：复合索引免掉整池排序（HDD 冷扫实测 2.07s）。
            con.execute('CREATE INDEX IF NOT EXISTS content_index_pool_recorded ON content_index(pool,recorded_at)')
        _ENSURED.add(key)


def normalize_category(value, legacy=''):
    """把一个分类名归一成 5 类之一；同时返回应保留的旧分类名。"""
    value = str(value or '').strip()
    if value in TARGET_CATEGORIES:
        return value, str(legacy or '').strip()
    if value in CATEGORY_MAP:
        return CATEGORY_MAP[value], value
    return '认知底座', value


def aliases_for(category, legacy=''):
    values = [category, str(legacy or '').strip(), *CATEGORY_ALIASES.get(category, ())]
    return tuple(dict.fromkeys(v for v in values if v))


def row_aliases(category, legacy=''):
    """单条记录自己的别名：只含新分类与该条的旧分类，不扩散到整类旧词。"""
    return list(dict.fromkeys(v for v in (category, str(legacy or '').strip()) if v))


def category_matches(item, wanted):
    """筛选兼容：用新分类、旧分类或别名都能命中同一条。"""
    wanted = str(wanted or '').strip()
    if not wanted:
        return True
    if item.get('category') == wanted or item.get('category_legacy') == wanted:
        return True
    return wanted in (item.get('category_aliases') or ())


# ---------------------------------------------------------------- 指纹
def _checkin_fingerprint(store):
    root = Path(store.root) / 'checkin'
    if not root.is_dir():
        return ''
    parts = []
    for path in sorted(root.glob('*.jsonl')):
        try:
            stat = path.stat()
            parts.append('%s:%s:%s' % (path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            continue
    return ','.join(parts)


def _fingerprint(store):
    parts = []
    with store.connect() as con:
        for row in con.execute('SELECT kind,COUNT(*) c,IFNULL(MAX(updated_at),"") u,'
                               'IFNULL(SUM(revision),0) r FROM records GROUP BY kind ORDER BY kind'):
            if row['kind'] in INDEXED_KINDS:
                parts.append('%s:%s:%s:%s' % (row['kind'], row['c'], row['u'], row['r']))
    parts.append('checkin:' + _checkin_fingerprint(store))
    return '|'.join(parts)


def _fingerprint_cached(store):
    key = str(store.path)
    now = time.monotonic()
    hit = _FP_CACHE.get(key)
    if hit and now - hit[0] < _FP_TTL:
        return hit[1]
    value = _fingerprint(store)
    _FP_CACHE[key] = (now, value)
    return value


def _invalidate_fingerprint(store):
    _FP_CACHE.pop(str(store.path), None)


def _meta_get(store, key):
    try:
        with store.connect() as con:
            row = con.execute('SELECT value FROM content_index_meta WHERE key=?', (key,)).fetchone()
            return row['value'] if row else None
    except Exception:
        return None


def _meta_set(store, key, value):
    with store.connect() as con:
        con.execute('INSERT OR REPLACE INTO content_index_meta(key,value) VALUES (?,?)', (key, value))


# ---------------------------------------------------------------- 后台重建
def _rebuild_worker(store):
    started = time.perf_counter()
    try:
        count = rebuild(store)
        ms = int((time.perf_counter() - started) * 1000)
        _REBUILD_STATE['last_error'] = ''
        print('[索引] 后台重建完成：%d 条，用时 %d ms' % (count, ms), flush=True)
    except Exception as exc:  # 重建失败不影响读写：检索自动走 LIKE 兜底
        _REBUILD_STATE['last_error'] = '%s: %s' % (type(exc).__name__, str(exc)[:200])
        print('[索引] 后台重建失败：%s' % _REBUILD_STATE['last_error'], flush=True)
    finally:
        _REBUILD_STATE['running'] = False


def start_rebuild(store):
    """起后台重建线程；已在跑就复用，绝不阻塞调用方。"""
    with _REBUILD_LOCK:
        if _REBUILD_STATE['running']:
            return False
        _REBUILD_STATE['running'] = True
        _REBUILD_STATE['started_at'] = datetime.now(timezone.utc).isoformat()
        thread = threading.Thread(target=_rebuild_worker, args=(store,),
                                  name='content-index-rebuild', daemon=True)
        _REBUILD_STATE['thread'] = thread
    thread.start()
    return True


def bootstrap(store):
    """启动时调用：只建表 + 判断是否需要重建，重建放后台，不占启动时间。"""
    started = time.perf_counter()
    ensure(store)
    current = _fingerprint(store)
    up_to_date = _meta_get(store, 'fingerprint') == current
    started_rebuild = False
    if not up_to_date:
        started_rebuild = start_rebuild(store)
    return {'ok': True, 'schema_ms': int((time.perf_counter() - started) * 1000),
            'up_to_date': up_to_date, 'rebuild_started': started_rebuild}


def ensure_fresh(store, wait=False):
    """读路径入口：索引过期就起后台重建并立刻返回，不再同步重建。

    wait=True 仅供显式重建接口/诊断使用。
    """
    ensure(store)
    current = _fingerprint_cached(store)
    if _meta_get(store, 'fingerprint') == current:
        return current
    if wait:
        rebuild(store)
    else:
        start_rebuild(store)
    return current


def status(store):
    ensure(store)
    current = _fingerprint(store)
    built = _meta_get(store, 'fingerprint')
    counts = {}
    try:
        with store.connect() as con:
            for row in con.execute('SELECT source_kind,COUNT(*) c FROM content_index GROUP BY source_kind'):
                counts[row['source_kind']] = row['c']
    except Exception:
        counts = {}
    return {
        'ok': True,
        'ready': bool(built) and sum(counts.values()) > 0,
        'stale': built != current,
        'rebuilding': _REBUILD_STATE['running'],
        'index_count': sum(counts.values()),
        'by_kind': counts,
        'built_at': _meta_get(store, 'rebuilt_at'),
        'last_ms': _meta_get(store, 'last_ms'),
        'last_error': _REBUILD_STATE['last_error'] or (_meta_get(store, 'last_error') or ''),
        'pool_kinds': list(POOL_KINDS),
        'indexed_kinds': list(INDEXED_KINDS),
    }


# ---------------------------------------------------------------- 行适配
def _clean(text, limit=6000):
    value = ' '.join(str(text or '').split())
    return value[:limit]


def _strip_html(text):
    value = re.sub(r'<[^>]+>', ' ', str(text or ''))
    value = value.replace('&nbsp;', ' ').replace('&mdash;', '—').replace('&amp;', '&')
    return _clean(value)


def _flatten(value, limit=4000):
    """把任意结构的字符串按顺序拼起来，供没有固定字段的类型兜底。"""
    chunks = []

    def walk(node, depth):
        if len(' '.join(chunks)) > limit or depth > 4:
            return
        if isinstance(node, str):
            text = _clean(node, limit)
            if text:
                chunks.append(text)
        elif isinstance(node, dict):
            for key, child in node.items():
                if key in ('raw', 'sync', 'draft', 'parts', 'answers'):
                    continue
                walk(child, depth + 1)
        elif isinstance(node, (list, tuple)):
            for child in node[:30]:
                walk(child, depth + 1)

    walk(value, 0)
    return ' '.join(chunks)[:limit]


def _event_index(rows):
    """与 checkin._event_index 同口径：revise 覆盖当天，void 删除当天。"""
    records = {}
    for row in rows:
        kind = row.get('type')
        if kind in ('checkin', 'revise'):
            records[row.get('date')] = row
        elif kind == 'void':
            records.pop(row.get('date'), None)
    return records


def _read_checkin_events(root, profile_id):
    path = Path(root) / 'checkin' / (str(profile_id) + '.jsonl')
    if not path.is_file():
        return []
    rows = []
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    except (OSError, ValueError):
        return []
    return rows


def _streak(days, end_key):
    present = {d for d, _e in days}
    try:
        cursor = date.fromisoformat(end_key)
    except ValueError:
        return 0
    count = 0
    while cursor.isoformat() in present:
        count += 1
        cursor -= timedelta(days=1)
    return count


def _checkin_rows(store):
    """打卡进索引：一条档案摘要 + 每个已打卡日一条（含文字备注、连续天数、当月累计）。

    数据源只在本机 <IP_DATA_DIR>/checkin/*.jsonl，索引也落在本机数据库。
    """
    rows = []
    try:
        profiles = store.list('checkin_profile', 100)
    except Exception:
        profiles = []
    for profile in profiles:
        events = _read_checkin_events(store.root, profile['id'])
        if not events:
            continue
        records = _event_index(events)
        days = sorted((str(k), v) for k, v in records.items() if k)
        if not days:
            continue
        latest = days[-1][0]
        latest_streak = _streak(days, latest)
        month = latest[:7]
        month_count = len([d for d, _e in days if d[:7] == month])
        name = _clean(profile.get('name') or '未命名主理人', 40)
        created = profile.get('created_at') or ''
        summary_body = ('起始日 %s，应打卡 60 天；已打卡 %d 天；截至最近一次打卡连续 %d 天；'
                        '%s（最近一次打卡所在月）累计 %d 天。备注：%s'
                        % (profile.get('start_date') or '', len(days), latest_streak,
                           month, month_count, _clean(profile.get('note'), 200) or '无'))
        rows.append(_make_row('checkin_profile', profile['id'], {
            'title': '打卡档案 · %s' % name,
            'body': summary_body,
            'category': '', 'category_legacy': '',
            'recorded_at': latest + 'T00:00:00+08:00' if latest else created,
            'source_url': '',
            'meta': {'profile_id': profile['id'], 'checked_days': len(days),
                     'streak': latest_streak, 'month': month, 'month_count': month_count,
                     'start_date': profile.get('start_date')},
        }, profile))
        for key, event in days:
            payload = event.get('payload') or {}
            streak = _streak(days, key)
            in_month = len([d for d, _e in days if d[:7] == key[:7]])
            note = _clean(payload.get('note'), 500)
            video = payload.get('video') or {}
            body = '；'.join(x for x in (
                '打卡日期 %s' % key,
                '连续第 %d 天' % streak,
                '当月累计 %d 天' % in_month,
                '备注：' + note if note else '',
                '发布链接：' + _clean(payload.get('publish_url'), 300) if payload.get('publish_url') else '',
                '成片：' + _clean(video.get('filename'), 120) if video.get('filename') else '',
            ) if x)
            rows.append(_make_row('checkin_day', '%s:%s' % (profile['id'], key), {
                'title': '打卡 %s · 第 %d 天' % (key, streak),
                'body': body,
                'category': '', 'category_legacy': '',
                'recorded_at': str(event.get('at') or key),
                'source_url': '',
                'meta': {'profile_id': profile['id'], 'date': key, 'streak': streak,
                         'month': key[:7], 'month_count': in_month},
            }, {'id': '%s:%s' % (profile['id'], key), 'revision': 0,
                'created_at': event.get('at') or key, 'updated_at': event.get('at') or key}))
    return rows


def _adapt(kind, item, ctx):
    """把一条底表记录变成索引行；不认识/不该进索引的返回 None。"""
    if kind == 'knowledge':
        category, legacy = normalize_category(item.get('category'), item.get('category_legacy'))
        claim = _clean(item.get('claim'), 4000)
        body = _clean(item.get('body'), 6000)
        return {
            'title': _clean(item.get('title'), 200) or '(未命名)',
            'body': '\n'.join(x for x in (claim, body) if x) or _clean(item.get('legacy_id'), 200),
            'category': category, 'category_legacy': legacy,
            'recorded_at': item.get('recorded_at') or item.get('created_at') or '',
            'source_url': item.get('source_url') or '',
            'goal_ids': list(item.get('goal_ids') or []),
            'scope': item.get('scope') or '',
            'lifecycle': item.get('lifecycle') or 'active',
            'meta': {'legacy_id': item.get('legacy_id') or '', 'validation': item.get('validation') or '',
                     'endorsement': item.get('endorsement') or ''},
            'pool': 'pool',
        }
    if kind == 'growth_fragment':
        category, legacy = normalize_category('随手记', '成长碎片')
        text = _clean(item.get('text'), 6000)
        return {
            'title': _clean(item.get('title'), 200) or '成长碎片',
            'body': text or _clean(item.get('source_url'), 500),
            'category': category, 'category_legacy': legacy,
            'recorded_at': item.get('recorded_at') or item.get('created_at') or '',
            'source_url': item.get('source_url') or '',
            'goal_ids': list(item.get('goal_ids') or []),
            'scope': item.get('scope') or '',
            'lifecycle': 'archived' if item.get('archived') else 'active',
            'meta': {'archived': bool(item.get('archived'))},
            'pool': 'pool',
        }
    if kind == 'flow':
        card = item.get('card') or {}
        answers = item.get('answers') or {}
        body = '；'.join(x for x in (
            '三观卡：' + _clean(card.get('worldview'), 800) if card.get('worldview') else '',
            '人生：' + _clean(card.get('life'), 800) if card.get('life') else '',
            '价值观：' + _clean(card.get('values'), 800) if card.get('values') else '',
            '方法：' + _clean(card.get('method'), 200) if card.get('method') else '',
            '问答原文：' + _clean(' '.join(str(v) for v in answers.values() if isinstance(v, str)), 3000)
            if answers else '',
        ) if x)
        return {
            'title': '成长档案 · 重启系统',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('updated_at') or item.get('created_at') or '',
            'source_url': '',
            'meta': {'track': item.get('track') or '', 'stage': item.get('stage') or '',
                     'completed': bool(item.get('completed'))},
            'pool': 'brain',
        }
    if kind == 'growth_goal':
        parent = ctx['goals'].get(item.get('parent_id') or '')
        body = '；'.join(x for x in (
            '目标：' + _clean(item.get('title'), 200),
            '所属：' + _clean(parent, 100) if parent else '',
            '说明：' + _clean(item.get('description'), 1000) if item.get('description') else '',
            '已归档' if item.get('archived') else '',
        ) if x)
        return {
            'title': _clean(item.get('title'), 200) or '成长目标',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'level': item.get('level') or '', 'archived': bool(item.get('archived'))},
            'pool': 'brain',
        }
    if kind == 'project':
        parts = [p.get('text') for p in (item.get('draft') or []) if isinstance(p, dict)]
        body = '；'.join(x for x in (
            '标题：' + _clean(item.get('title'), 200),
            '脚本：' + _clean(' '.join(str(p) for p in parts if p), 5000) if parts else '',
            '标签：' + _clean('、'.join(item.get('tags') or []), 300) if item.get('tags') else '',
            '阶段：' + _clean(item.get('stage'), 60) if item.get('stage') else '',
        ) if x)
        return {
            'title': _clean(item.get('title'), 200) or '未命名项目',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'stage': item.get('stage') or '', 'tags': list(item.get('tags') or [])},
            'pool': 'brain',
        }
    if kind == 'script_version':
        project = ctx['projects'].get(item.get('project_id') or '') or ''
        parts = [p.get('text') for p in (item.get('parts') or []) if isinstance(p, dict)]
        body = '；'.join(x for x in (
            '项目：' + project if project else '',
            '定稿：' + _clean(' '.join(str(p) for p in parts if p), 5000) if parts else '',
            '确认时间：' + _clean(item.get('confirmed_at'), 40) if item.get('confirmed_at') else '',
        ) if x)
        return {
            'title': '定稿脚本 · %s' % (project or '未命名项目'),
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('confirmed_at') or item.get('created_at') or '',
            'source_url': '',
            'meta': {'project_id': item.get('project_id') or ''},
            'pool': 'brain',
        }
    if kind == 'artifact':
        if item.get('kind') not in VIDEO_ARTIFACT_KINDS:
            return None
        info = item.get('info') or {}
        label = {'cover': '封面'}.get(item.get('kind'), '成片')
        seconds = ''
        try:
            if info.get('duration_ms'):
                seconds = '%.1f 秒' % (int(info['duration_ms']) / 1000.0)
        except (TypeError, ValueError):
            seconds = ''
        body = '；'.join(x for x in (
            '%s文件：%s' % (label, _clean(item.get('filename') or Path(str(item.get('path') or '')).name, 200)),
            '时长：' + seconds if seconds else '',
            '尺寸：%sx%s' % (info.get('width'), info.get('height')) if info.get('width') else '',
            '项目：' + (ctx['projects'].get(item.get('project_id') or '') or ''),
        ) if x)
        return {
            'title': _clean(item.get('filename'), 200) or label,
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'artifact_kind': item.get('kind'), 'project_id': item.get('project_id') or ''},
            'pool': 'brain',
        }
    if kind == 'topic':
        body = '；'.join(x for x in (
            '选题：' + _clean(item.get('title'), 300),
            '角度：' + _clean(item.get('angle'), 600) if item.get('angle') else '',
            '观点：' + _clean(item.get('thesis'), 1000) if item.get('thesis') else '',
            '备注：' + _clean(item.get('note'), 1000) if item.get('note') else '',
            '原话依据：' + _clean(item.get('evidence_quote'), 500) if item.get('evidence_quote') else '',
        ) if x)
        return {
            'title': _clean(item.get('title'), 300) or '未命名选题',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'status': item.get('status') or '', 'origin': item.get('origin') or ''},
            'pool': 'brain',
        }
    if kind == 'bench_work':
        raw = item.get('raw') or {}
        body = '；'.join(x for x in (
            '标题：' + _clean(item.get('title'), 300),
            '平台：' + _clean(item.get('platform'), 30) if item.get('platform') else '',
            '原文：' + _clean(raw.get('transcript_text'), 4000) if raw.get('transcript_text') else '',
            '文案：' + _clean(raw.get('caption') or raw.get('desc'), 1000) if (raw.get('caption') or raw.get('desc')) else '',
        ) if x)
        return {
            'title': _clean(item.get('title'), 300) or '对标作品',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('published_at') or item.get('collected_at') or item.get('created_at') or '',
            'source_url': item.get('source_url') or '',
            'meta': {'platform': item.get('platform') or '', 'bench_account_id': item.get('bench_account_id') or ''},
            'pool': 'brain',
        }
    if kind == 'bench_account':
        body = '；'.join(x for x in (
            '账号：' + _clean(item.get('display_name'), 120),
            '平台：' + _clean(item.get('platform'), 30) if item.get('platform') else '',
            '粉丝：%s' % item.get('follower_count') if item.get('follower_count') is not None else '',
            '作品数：%s' % item.get('works_count') if item.get('works_count') is not None else '',
        ) if x)
        return {
            'title': _clean(item.get('display_name'), 120) or '对标账号',
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('collected_at') or item.get('created_at') or '',
            'source_url': item.get('home_url') or '',
            'meta': {'platform': item.get('platform') or ''},
            'pool': 'brain',
        }
    if kind == 'bench_comment':
        return {
            'title': '评论 · %s' % (_clean(item.get('author_name'), 60) or '匿名'),
            'body': _clean(item.get('content'), 2000),
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('collected_at') or item.get('created_at') or '',
            'source_url': item.get('source_url') or '',
            'meta': {'bench_work_id': item.get('bench_work_id') or ''},
            'pool': 'brain',
        }
    if kind == 'source':
        return {
            'title': _clean(item.get('title'), 200) or _clean(item.get('text'), 60) or '素材来源',
            'body': _clean(item.get('text') or item.get('share_text'), 6000),
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': item.get('url') or '',
            'meta': {'source_kind': item.get('kind') or '', 'status': item.get('status') or ''},
            'pool': 'brain',
        }
    if kind == 'news':
        return {
            'title': _strip_html(item.get('title')) or '行业热点',
            'body': _strip_html(item.get('summary')),
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('published') or item.get('fetched_at') or item.get('created_at') or '',
            'source_url': item.get('url') or '',
            'meta': {'source': item.get('source') or ''},
            'pool': 'brain',
        }
    if kind == 'question':
        analysis = item.get('analysis') or {}
        answer = ''
        if isinstance(analysis, dict):
            answer = _clean(analysis.get('answer'), 4000)
            missing = analysis.get('missing_info') or []
            if missing:
                answer += '；资料缺口：' + _clean('、'.join(str(x) for x in missing), 600)
        else:
            answer = _clean(analysis, 4000)
        return {
            'title': '问过：%s' % (_clean(item.get('question'), 120) or '（无标题）'),
            'body': answer,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'history_turns': item.get('history_turns') or 0},
            'pool': 'brain',
        }
    if kind == 'interview_session':
        body = _flatten({k: v for k, v in item.items()
                         if k not in ('id', 'revision', 'created_at', 'updated_at')}, 4000)
        return {
            'title': '深挖访谈 · %s' % (_clean(item.get('topic') or item.get('title'), 80)
                                     or _clean(item.get('created_at'), 10) or '未命名'),
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('closed_at') or item.get('created_at') or '',
            'source_url': '',
            'meta': {'status': item.get('status') or '', 'depth': item.get('depth') or 0},
            'pool': 'brain',
        }
    if kind == 'interview_turn':
        question = _clean(item.get('question') or item.get('ask'), 800)
        answer = _clean(item.get('answer') or item.get('reply') or item.get('text'), 3000)
        return {
            'title': '访谈第 %s 轮 · %s' % (item.get('turn') or item.get('seq') or item.get('layer') or '?',
                                            _clean(question, 60) or '原话'),
            'body': '；'.join(x for x in ('问：' + question if question else '',
                                         '答：' + answer if answer else '') if x),
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'session_id': item.get('session_id') or '',
                     'layer': item.get('layer') or item.get('depth') or 0},
            'pool': 'brain',
        }
    if kind == 'creator_profile':
        body = _flatten({k: v for k, v in item.items()
                         if k not in ('id', 'revision', 'created_at', 'updated_at')}, 4000)
        return {
            'title': '创作者画像 · %s' % (_clean(item.get('dimension') or item.get('kind'), 60) or '综合'),
            'body': body,
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'dimension': item.get('dimension') or item.get('kind') or ''},
            'pool': 'brain',
        }
    if kind == 'growth_answer':
        return {
            'title': '成长问答 · %s' % (_clean(item.get('question_text') or item.get('question'), 80)
                                        or _clean(item.get('qid'), 30) or '未命名'),
            'body': _clean(item.get('answer') or item.get('text'), 4000),
            'category': '', 'category_legacy': '',
            'recorded_at': item.get('created_at') or '',
            'source_url': '',
            'meta': {'qid': item.get('qid') or ''},
            'pool': 'brain',
        }
    return None


def _make_row(kind, source_id, data, raw_item):
    title = data['title']
    body = data.get('body') or ''
    label = KIND_LABELS.get(kind, kind)
    meta = data.get('meta') or {}
    search_text = ' '.join(x for x in (
        title, body, data.get('source_url'), data.get('category'), data.get('category_legacy'),
        label, ' '.join(str(v) for v in meta.values() if isinstance(v, (str, int, float))),
    ) if x).lower()
    return {
        'idx_id': '%s:%s' % (kind, source_id),
        'source_kind': kind,
        'source_id': str(source_id),
        'title': title[:300],
        'category': data.get('category') or '',
        'category_legacy': data.get('category_legacy') or '',
        'alias_text': ' '.join(v for v in (data.get('category'), data.get('category_legacy')) if v),
        'scope': data.get('scope') or '',
        'lifecycle': data.get('lifecycle') or 'active',
        'recorded_at': data.get('recorded_at') or '',
        'goal_ids': json.dumps(list(data.get('goal_ids') or []), ensure_ascii=False),
        'snippet': _clean(body, 200),
        'search_text': search_text[:8000],
        'revision': int(raw_item.get('revision') or 0),
        'indexed_at': datetime.now(timezone.utc).isoformat(),
        'kind_label': label,
        'body': body[:8000],
        'source_url': data.get('source_url') or '',
        'meta': json.dumps(meta, ensure_ascii=False),
        'pool': data.get('pool') or 'brain',
    }


def _context(store):
    projects = {}
    goals = {}
    try:
        for item in store.list('project', 2000):
            projects[item['id']] = item.get('title') or ''
        for item in store.list('growth_goal', 500):
            goals[item['id']] = item.get('title') or ''
    except Exception:
        pass
    return {'projects': projects, 'goals': goals}


def rebuild(store):
    """按底表重建索引；一次短事务，失败不留下半张索引。"""
    ensure(store)
    stamp = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    ctx = _context(store)
    rows = []
    with store.connect() as con:
        for kind in INDEXED_KINDS:
            mirrored = {r['source_id'] for r in rows if r['source_kind'] == 'knowledge'}
            for row in con.execute('SELECT * FROM records WHERE kind=? ORDER BY id', (kind,)):
                item = store.unpack(row)
                # 成长碎片：已经镜像进知识池的不再重复计入；老碎片仍可检索。
                if kind == 'growth_fragment':
                    if item.get('origin_knowledge_id') or mirror_key(item['id']) in mirrored:
                        continue
                adapted = _adapt(kind, item, ctx)
                if adapted:
                    rows.append(_make_row(kind, item['id'], adapted, item))
    rows.extend(_checkin_rows(store))
    fingerprint = _fingerprint(store)
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        try:
            con.execute('DELETE FROM content_index')
            con.executemany('INSERT OR REPLACE INTO content_index (%s) VALUES (%s)'
                            % (','.join(COLUMNS), ','.join(':' + c for c in COLUMNS)), rows)
            con.execute('INSERT OR REPLACE INTO content_index_meta(key,value) VALUES (?,?)',
                        ('fingerprint', fingerprint))
            con.execute('INSERT OR REPLACE INTO content_index_meta(key,value) VALUES (?,?)',
                        ('rebuilt_at', stamp))
            con.execute('INSERT OR REPLACE INTO content_index_meta(key,value) VALUES (?,?)',
                        ('last_ms', str(int((time.perf_counter() - started) * 1000))))
            con.execute('INSERT OR REPLACE INTO content_index_meta(key,value) VALUES (?,?)',
                        ('last_error', ''))
            con.execute('COMMIT')
        except Exception:
            con.execute('ROLLBACK')
            raise
    _invalidate_fingerprint(store)
    return len(rows)


# ---------------------------------------------------------------- 检索
_TERM_RE = re.compile(r'[\u4e00-\u9fff]{2,}|[a-zA-Z]{2,}|\d{3,}')
# 提问里的高频虚词：拆成二元词后只会把结果拉偏，检索时不参与打分。
_STOP_TERMS = {'什么', '怎么', '我该', '该做', '我们', '你们', '他们', '现在', '最近',
               '一下', '帮我', '可以', '能否', '是否', '为什', '么办', '怎样', '多少'}


def _terms(q):
    text = str(q or '').strip().lower()
    if not text:
        return []
    terms = []
    for chunk in _TERM_RE.findall(text):
        if len(chunk) >= 2 and re.fullmatch(r'[\u4e00-\u9fff]+', chunk):
            # 中文按二元切，长句才可能命中短文
            if len(chunk) == 2:
                terms.append(chunk)
            else:
                terms.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        else:
            terms.append(chunk)
    return [t for t in dict.fromkeys(terms) if t not in _STOP_TERMS]


def _score(q, terms, title, search_text, weights=None):
    literal = str(q or '').strip().lower()
    score = 0
    matched = 0
    if literal and literal in search_text:
        score += 12
    for term in terms:
        weight = (weights or {}).get(term, 1.0)
        if term in title:
            score += 4 * weight
            matched += 1
            continue
        hits = search_text.count(term)
        if hits:
            score += min(hits, 3) * weight
            matched += 1
    return score, matched


def _term_weights(rows, terms):
    """按命中条数给词降权：满世界都有的虚词，不该把结果拉偏。"""
    if not rows:
        return {}
    total = len(rows)
    weights = {}
    for term in terms:
        df = sum(1 for text in rows if term in text)
        weights[term] = 1.0 + math.log(1.0 + total / (1.0 + df))
    return weights


def _decorate(row):
    item = dict(row)
    try:
        item['goal_ids'] = json.loads(item.get('goal_ids') or '[]')
    except ValueError:
        item['goal_ids'] = []
    try:
        item['meta'] = json.loads(item.get('meta') or '{}')
    except ValueError:
        item['meta'] = {}
    item['kind_label'] = item.get('kind_label') or KIND_LABELS.get(item.get('source_kind'), '')
    return item


def _active_sql():
    return "AND (lifecycle IS NULL OR lifecycle='' OR lifecycle='active')"


def current_month():
    return datetime.now(TZ).strftime('%Y-%m')


def month_of(value):
    """按北京时间归月；历史时间戳可能是 UTC，不能直接截前 7 位。"""
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime('%Y-%m')
    except (ValueError, TypeError):
        return str(value or '')[:7]


def summary(store, month=None):
    """知识池口径：全部=非示例且 active；本月=记录时间在本月；等待归位=没有关联目标。

    只统计 pool='pool' 的认知/成长碎片，项目、打卡等不改变这里的数字。
    """
    ensure_fresh(store)
    month = str(month or current_month())
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT category,recorded_at,goal_ids FROM content_index "
            "WHERE pool='pool' AND (scope IS NULL OR scope!='example') " + _active_sql())]
    all_count = len(rows)
    month_count = sum(1 for r in rows if month_of(r.get('recorded_at')) == month)
    inbox = sum(1 for r in rows if not json.loads(r.get('goal_ids') or '[]'))
    by_category = {name: 0 for name in TARGET_CATEGORIES}
    for row in rows:
        by_category[row['category']] = by_category.get(row['category'], 0) + 1
    return {'all': all_count, 'month': month_count, 'month_value': month, 'inbox': inbox,
            'by_category': by_category, 'scope': 'default'}


def list_items(store, query):
    ensure_fresh(store)
    scope = (query.get('scope') or [''])[0]
    category = (query.get('category') or [''])[0].strip()
    q = (query.get('q') or [''])[0].strip().lower()
    kind = (query.get('type') or [''])[0].strip()
    pool = (query.get('pool') or [''])[0].strip()
    try:
        limit = min(500, max(1, int((query.get('limit') or ['100'])[0])))
    except ValueError:
        limit = 100
    items = []
    with store.connect() as con:
        for row in con.execute('SELECT * FROM content_index ORDER BY recorded_at DESC, idx_id'):
            item = _decorate(row)
            item.pop('body', None)
            item.pop('search_text', None)
            items.append(item)
    if scope == 'example':
        items = [x for x in items if x.get('scope') == 'example']
    else:
        items = [x for x in items if x.get('scope') != 'example']
    if pool != 'all':
        items = [x for x in items if x.get('pool') == 'pool']
    if category:
        items = [x for x in items if category in (x.get('alias_text') or '').split()]
    if kind:
        items = [x for x in items if x.get('source_kind') == kind]
    if q:
        items = [x for x in items if q in (x.get('search_text') or '')]
    return {'items': items[:limit], 'total': len(items), 'scope': scope or 'default', 'pool': pool or 'pool'}


def _like_pattern(term):
    escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return '%' + escaped + '%'


def _live_rows(store, q, limit):
    """索引还没建好时的兜底：直接扫底表 + 打卡 JSONL，按 LIKE 候选再打分。"""
    terms = _terms(q)
    if not terms:
        return []
    terms = terms[:8]
    conds = ' OR '.join('lower(data) LIKE ? ESCAPE \'\\\'' for _ in terms)
    params = [_like_pattern(t) for t in terms]
    placeholders = ','.join('?' for _ in INDEXED_KINDS)
    ctx = _context(store)
    candidates = []
    try:
        with store.connect() as con:
            rows = con.execute('SELECT * FROM records WHERE kind IN (%s) AND (%s) LIMIT 600'
                               % (placeholders, conds), tuple(INDEXED_KINDS) + tuple(params)).fetchall()
        for row in rows:
            item = store.unpack(row)
            if item.get('scope') == 'example':
                continue
            adapted = _adapt(row['kind'], item, ctx)
            if not adapted:
                continue
            adapted['pool'] = adapted.get('pool') or ('pool' if row['kind'] in POOL_KINDS else 'brain')
            candidates.append(_make_row(row['kind'], item['id'], adapted, item))
    except Exception:
        candidates = []
    candidates.extend(_checkin_rows(store))
    weights = _term_weights([x['search_text'] for x in candidates], terms)
    need = 2 if len(terms) >= 3 else 1
    # 2026-11-25：下面给认知条目补 dormant/used/mainline 时要用这两个，原先没取——
    # 索引没建好时（新装首次启动）只要有一条认知命中就会 NameError，整个搜索 500。
    meta = knowledge_meta(store)
    context = mainline_context(store)
    scored = []
    for indexed in candidates:
        score, matched = _score(q, terms, indexed['title'], indexed['search_text'], weights)
        if score and matched >= need:
            scored.append((score, _decorate(indexed)))
    scored.sort(key=lambda x: (x[0], str(x[1].get('recorded_at') or '')), reverse=True)
    items = []
    for score_value, item in scored[:limit]:
        item.pop('search_text', None)
        item['score'] = round(score_value, 2)
        if item.get('source_kind') == 'knowledge':
            meta_item = meta.get(str(item.get('source_id') or '')) or {}
            item['dormant'] = bool(meta_item.get('dormant'))
            item['used_count'] = int(meta_item.get('used') or 0)
            if meta_item.get('confidence'):
                item['confidence'] = meta_item['confidence']
            _att, rel = attention_weight(meta_item, context)
            item['mainline'] = bool(rel)
        items.append(item)
    return items


def rows_of(store, kinds, limit=300, include_body=True):
    """按来源类型取索引行（新记录在前）；索引未建好时返回空，调用方自己兜底。"""
    ensure_fresh(store)
    kinds = tuple(kinds or ())
    if not kinds:
        return []
    marks = ','.join('?' for _ in kinds)
    try:
        with store.connect() as con:
            rows = con.execute('SELECT * FROM content_index WHERE source_kind IN (%s) '
                               'ORDER BY recorded_at DESC, idx_id DESC LIMIT ?'
                               % marks, kinds + (int(limit),)).fetchall()
    except Exception:
        return []
    out = []
    for row in rows:
        item = _decorate(row)
        item.pop('search_text', None)
        if not include_body:
            item.pop('body', None)
        out.append(item)
    return out


CONFIDENCE = {
    'verified': {'key': 'verified', 'label': '已验证'},
    'lived': {'key': 'lived', 'label': '亲历'},
    'guess': {'key': 'guess', 'label': '只是猜想'},
}


REVISION_MIN_OVERLAP = 0.30


def _bigrams(text):
    chars = re.sub(r'[^\u4e00-\u9fff]', '', str(text or ''))
    return {chars[i:i + 2] for i in range(len(chars) - 1)}


def revision_candidates(store, limit=5):
    """t162：同主题、立场/否定不同 → 新证据可能推翻旧判断；只给候选，不自动改。"""
    try:
        cards = [x for x in store.list('knowledge', 100000)
                 if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        return []
    cards = sorted(cards, key=lambda x: str(x.get('recorded_at') or x.get('created_at') or ''))
    out = []
    for i, old in enumerate(cards):
        if len(out) >= limit:
            break
        old_text = str(old.get('claim') or old.get('title') or '')
        for new in cards[i + 1:]:
            new_text = str(new.get('claim') or new.get('title') or '')
            if not old_text or not new_text or old_text == new_text:
                continue
            left, right = _bigrams(old_text), _bigrams(new_text)
            if not left or not right:
                continue
            overlap = len(left & right) / float(len(left | right))
            if overlap < REVISION_MIN_OVERLAP:
                continue
            neg = ('不', '别', '并非', '不是', '没用', '不值得')
            old_neg = any(w in old_text for w in neg)
            new_neg = any(w in new_text for w in neg)
            out.append({'old_id': str(old.get('id')), 'new_id': str(new.get('id')),
                        'old_title': str(old.get('title') or '')[:60],
                        'new_title': str(new.get('title') or '')[:60],
                        'old_claim': old_text[:160], 'new_claim': new_text[:160],
                        'old_date': str(old.get('recorded_at') or '')[:10],
                        'new_date': str(new.get('recorded_at') or '')[:10],
                        'overlap': round(overlap, 2),
                        'reason': '同一主题，旧判断含否定/新判断立场不同' if old_neg != new_neg
                                  else '同一主题，前后判断不一致'})
            break
    return out


def revise_card(store, old_id, new_id=None, new_title='', new_text=''):
    """客户确认后修订：旧卡标 superseded_by/已修订（原文保留），置信降到「只是猜想」；不删历史。"""
    old = store.get('knowledge', str(old_id or ''))
    if not old:
        raise KeyError('旧认知不存在。')
    new = store.get('knowledge', str(new_id or '')) if new_id else None
    if new is None:
        text = str(new_text or '').strip()
        if len(_flat(text)) < 8:
            raise ValueError('新判断太短，先写一句完整判断。')
        new = store.put('knowledge', {'title': (new_title or text)[:60], 'claim': text, 'body': text,
                                      'category': old.get('category') or '原则', 'legacy_id': '修订',
                                      'boundary': '', 'source_id': None, 'source_url': '',
                                      'source_locator': {}, 'links': [],
                                      'markers': ['本人确认', '修订'], 'endorsement': 'accepted',
                                      'validation': 'unverified', 'lifecycle': 'active',
                                      'content_use': 'available', 'goal_ids': old.get('goal_ids') or [],
                                      'recorded_at': __import__('datetime').datetime.now().isoformat(timespec='seconds')})
    store.patch('knowledge', str(old_id), {'lifecycle': 'superseded',
                                           'superseded_by': str(new.get('id')),
                                           'revised_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
                                           'markers': list(dict.fromkeys((old.get('markers') or []) + ['已修订', '被新判断取代']))})
    return {'ok': True, 'old_id': str(old_id), 'new_id': str(new.get('id')),
            'old': store.get('knowledge', str(old_id)), 'new': new}


def confidence(record):
    """三档置信：客户认可/验证记录→已验证；个人亲历记录→亲历；其余→只是猜想。"""
    record = record or {}
    validation = str(record.get('validation') or 'unverified')
    endorsement = str(record.get('endorsement') or '')
    markers = [str(x) for x in (record.get('markers') or [])]
    if record.get('superseded_by') or str(record.get('lifecycle') or '') == 'superseded':
        item = dict(CONFIDENCE['guess'])
        item['revised'] = True
        return item
    if endorsement == 'accepted' or validation in ('verified', 'validated', 'confirmed'):
        return dict(CONFIDENCE['verified'])
    if validation == 'personal_case' or any('亲历' in m or '亲身' in m for m in markers):
        return dict(CONFIDENCE['lived'])
    return dict(CONFIDENCE['guess'])


def blind_spots(store, limit=3):
    """对照主线/目标找“从没记录/考虑过”的维度；只用真实缺口，不编造。"""
    try:
        goals = [g for g in store.list('growth_goal', 1000) if not g.get('archived')]
        cards = [x for x in store.list('knowledge', 100000)
                 if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        return {'dimensions': [], 'total_cards': 0, 'note': '读取不到目标或认知，先不判断盲点。'}
    pillars = [g for g in goals if g.get('level') == 'pillar']
    if not pillars:
        return {'dimensions': [], 'total_cards': len(cards),
                'note': '还没有五大支柱目标；先立主线，才能对照盲点。'}
    by_id = {g['id']: g for g in goals}

    def descendants(pid):
        out, seen = set(), set()
        for g in goals:
            cur = g.get('parent_id')
            while cur and cur not in seen:
                seen.add(cur)
                if cur == pid:
                    out.add(g['id'])
                    break
                cur = (by_id.get(cur) or {}).get('parent_id')
        return out
    categories = [str(x.get('category') or '') for x in cards]
    missing = []
    for pillar in pillars:
        title = str(pillar.get('title') or '')
        scope = descendants(pillar['id']) | {pillar['id']}
        linked = any(scope.intersection(x.get('goal_ids') or []) for x in cards)
        named = any(title and (title in cat or cat in title) for cat in categories)
        if linked or named:
            continue
        missing.append({'id': pillar['id'], 'title': title, 'knowledge': 0,
                        'reason': '你的 %d 条认知里，没有一条挂在这个支柱下，也没有同名分类。' % len(cards)})
    return {'dimensions': missing[:limit], 'total_cards': len(cards),
            'checked_pillars': len(pillars),
            'note': '只按目标归属与分类名对照；没有记录就说没有，不推断你“其实想过”。'}


def mainline_context(store):
    """当前主线/目标：只取客户自己写的根目标与目标树，供注意力过滤用。"""
    try:
        goals = [g for g in store.list('growth_goal', 1000) if not g.get('archived')]
    except Exception:
        return {}
    root = next((g for g in goals if g.get('level') == 'root'), None)
    if not root:
        return {}
    terms = [str(root.get('title') or ''), str(root.get('thesis') or ''),
             str(root.get('milestone') or '')]
    for goal in goals:
        if goal.get('title'):
            terms.append(str(goal['title']))
    return {'title': str(root.get('title') or '我的主线'),
            'goal_ids': {str(g.get('id')) for g in goals if g.get('id')},
            'terms': [x for x in dict.fromkeys(terms) if x]}


def attention_weight(item, context):
    """与当前主线相关→1.25；无关→0.6（降权不删，t157 权重照常叠加）。"""
    if not context or not item:
        return 1.0, False
    goal_ids = {str(x) for x in (item.get('goal_ids') or [])}
    if goal_ids & context.get('goal_ids', set()):
        return 1.25, True
    category = str(item.get('category') or '')
    title = str(item.get('title') or '')
    for term in context.get('terms') or []:
        if not term:
            continue
        if (category and (term in category or category in term)) or (title and term in title):
            return 1.25, True
    return 0.6, False


def knowledge_meta(store):
    """t157：认知卡的遗忘/使用权重（不删原文，只调排序）。"""
    meta = {}
    try:
        for item in store.list('knowledge', 100000):
            kid = str(item.get('id') or '')
            if not kid:
                continue
            meta[kid] = {'dormant': bool(item.get('dormant')),
                         'used': int(item.get('used_count') or 0),
                         'endorsement': str(item.get('endorsement') or ''),
                         'confidence': confidence(item),
                         'goal_ids': list(item.get('goal_ids') or []),
                         'category': str(item.get('category') or ''),
                         'title': str(item.get('title') or '')}
    except Exception:
        meta = {}
    return meta


def weight_for(source_kind, source_id, meta):
    """沉底×0.35；常用最多 +50%；本人认可 +20%。只影响排序，不改内容。"""
    if source_kind != 'knowledge' or not source_id:
        return 1.0
    item = meta.get(str(source_id)) or {}
    weight = 1.0
    if item.get('dormant'):
        weight *= 0.35
    weight *= 1.0 + min(0.5, 0.1 * max(0, int(item.get('used') or 0)))
    if item.get('endorsement') == 'accepted':
        weight *= 1.2
    return weight


def record_cooccurrence(store, ids):
    """t158：同一次回答里一起被引用的认知对，累计共现次数（只加计数，不改原文）。"""
    pairs = []
    uniq = list(dict.fromkeys(str(x) for x in (ids or []) if x))
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            key = 'cooccur:' + '|'.join(sorted((a, b)))
            try:
                item = store.get('cooccurrence', key)
                count = int(item.get('count') or 0) + 1 if item else 1
                store.put('cooccurrence', {'ids': sorted([a, b]), 'count': count,
                                           'updated_at': __import__('datetime').datetime.now().isoformat(timespec='seconds')}, key)
                pairs.append((a, b, count))
            except Exception:
                continue
    return pairs


def cooccurrence_counts(store):
    """返回 {(a,b): count}，供图谱把共现调用变成边（权重=次数）。"""
    out = {}
    try:
        for item in store.list('cooccurrence', 100000):
            ids = list(item.get('ids') or [])
            if len(ids) == 2:
                out[tuple(sorted(ids))] = int(item.get('count') or 0)
    except Exception:
        out = {}
    return out


def mark_used(store, ids):
    """第二大脑/检索引用后记一次使用（只加计数与时间，便于加权）。"""
    changed = 0
    try:
        for kid in ids:
            kid = str(kid or '')
            if not kid:
                continue
            item = store.get('knowledge', kid)
            if not item:
                continue
            used = int(item.get('used_count') or 0) + 1
            from db import now
            store.patch('knowledge', kid, {'used_count': used, 'last_used_at': now()})
            changed += 1
    except Exception:
        return 0
    return changed


def search(store, q, limit=50, include_body=False, pool=None):
    """先本地检索的入口：返回可解释的命中条目（标题/摘要/来源类型/链路）。

    索引没建好（首次启动后台还在跑）时自动走 LIKE 兜底，不会返回空。
    """
    ensure_fresh(store)
    q = str(q or '').strip()
    terms = _terms(q)
    if not q or not terms:
        return {'q': q, 'items': [], 'total': 0, 'groups': {}, 'source': 'empty'}
    with store.connect() as con:
        indexed = con.execute('SELECT COUNT(*) c FROM content_index').fetchone()['c']
    if not indexed:
        items = _live_rows(store, q, limit)
        meta = knowledge_meta(store)
        context = mainline_context(store)
        for item in items:
            key = item.get('source_id') or item.get('id')
            item['score'] = round(float(item.get('score') or 0) * weight_for(
                item.get('source_kind'), key, meta), 2)
            att, rel = attention_weight(meta.get(str(key) or '') or {}, context)
            item['score'] = round(float(item.get('score') or 0) * att, 2)
            item['mainline'] = bool(rel)
        items.sort(key=lambda x: (float(x.get('score') or 0), str(x.get('recorded_at') or '')), reverse=True)
        groups = {}
        for item in items:
            if item.get('category'):
                groups[item['category']] = groups.get(item['category'], 0) + 1
        return {'q': q, 'items': items[:limit], 'total': len(items), 'groups': groups,
                'source': 'live_like', 'rebuilding': _REBUILD_STATE['running']}
    rows = []
    with store.connect() as con:
        for row in con.execute('SELECT * FROM content_index ' + 'WHERE 1=1 ' + _active_sql()):
            if row['scope'] == 'example':
                continue
            if pool and pool != 'all' and row['pool'] != pool:
                continue
            rows.append(row)
    weights = _term_weights([row['search_text'] for row in rows], terms)
    need = 2 if len(terms) >= 3 else 1
    meta = knowledge_meta(store)
    context = mainline_context(store)
    scored = []
    for row in rows:
        score, matched = _score(q, terms, row['title'], row['search_text'], weights)
        if score and matched >= need:
            score = score * weight_for(row['source_kind'], row['source_id'], meta)
            att, _rel = attention_weight(meta.get(str(row['source_id'] or '')) or {}, context)
            score *= att
            scored.append((score, row))
    scored.sort(key=lambda x: (x[0], str(x[1]['recorded_at'] or '')), reverse=True)
    items = []
    for score_value, row in scored[:limit]:
        item = _decorate(row)
        item.pop('search_text', None)
        if not include_body:
            item.pop('body', None)
        item['score'] = round(score_value, 2)
        if item.get('source_kind') == 'knowledge':
            meta_item = meta.get(str(item.get('source_id') or '')) or {}
            item['dormant'] = bool(meta_item.get('dormant'))
            item['used_count'] = int(meta_item.get('used') or 0)
            if meta_item.get('confidence'):
                item['confidence'] = meta_item['confidence']
            _att, rel = attention_weight(meta_item, context)
            item['mainline'] = bool(rel)
        items.append(item)
    groups = {}
    for item in items:
        if item.get('category'):
            groups[item['category']] = groups.get(item['category'], 0) + 1
    return {'q': q, 'items': items, 'total': len(scored), 'groups': groups,
            'source': 'index', 'rebuilding': _REBUILD_STATE['running']}


def mirror_key(fragment_id):
    return 'knowledge_' + str(fragment_id)


def mirror_fragment(store, fragment):
    """把成长碎片写进知识池（同一处录入、处处可见），返回镜像后的知识条目。"""
    key = mirror_key(fragment['id'])
    old = store.get('knowledge', key)
    category, legacy = normalize_category('随手记', '成长碎片')
    data = {
        'legacy_id': '成长碎片',
        'title': fragment.get('title') or '随手记',
        'category': category,
        'category_legacy': legacy,
        'category_aliases': list(aliases_for(category, legacy)),
        'claim': fragment.get('text') or fragment.get('source_url') or '',
        'body': fragment.get('text') or '',
        'boundary': '',
        'source_id': None,
        'source_url': fragment.get('source_url') or '',
        'source_locator': {},
        'links': [],
        'markers': ['来自成长碎片'],
        'endorsement': fragment.get('endorsement') or 'reference_only',
        'validation': 'unverified',
        'lifecycle': 'archived' if fragment.get('archived') else 'active',
        'content_use': 'available' if fragment.get('endorsement') == 'accepted' else 'review',
        'goal_ids': list(fragment.get('goal_ids') or []),
        'recorded_at': fragment.get('recorded_at') or '',
        'growth_fragment_id': fragment['id'],
        'growth_fragment_revision': fragment.get('revision'),
    }
    if old is None:
        return store.put('knowledge', data, key)
    # 已在认知详情里补充的边界、认同与核验不被碎片覆盖；碎片只负责正文、目标与日期。
    data['boundary'] = old.get('boundary') or ''
    # 手动关联是用户在认知详情里补的，碎片同步时不能被覆盖掉。
    if old.get('related_ids'):
        data['related_ids'] = related_ids(old)
    if old.get('endorsement') == 'accepted':
        data['endorsement'] = 'accepted'
        data['content_use'] = 'available'
    data['validation'] = old.get('validation') or 'unverified'
    return store.put('knowledge', data, key, old.get('revision'))


def drop_fragment_mirror(store, fragment_id):
    """碎片删除时一并删掉镜像；只删由本模块生成的那一条。"""
    key = mirror_key(fragment_id)
    with store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("DELETE FROM records WHERE kind='knowledge' AND id=?", (key,))
        con.execute("DELETE FROM content_index WHERE source_kind='knowledge' AND source_id=?", (key,))
        con.execute('COMMIT')


def _log_id():
    return 'cml_' + uuid.uuid4().hex[:16]


# ---- 卡片关联（2026-11-16） -------------------------------------------------
# 一条关系只存一份：落在 id 字典序较小的一侧；读取时两侧都算，所以反向链接能查到。
RELATED_LIMIT = 50


def related_ids(item):
    """读卡片的关联 id；旧数据没有 related_ids 字段时按空数组处理。"""
    raw = item.get('related_ids') if isinstance(item, dict) else None
    out = []
    if isinstance(raw, list):
        for value in raw:
            key = str(value or '').strip()
            if key and key != item.get('id') and key not in out:
                out.append(key)
    return out[:RELATED_LIMIT]


def _related_owner(left, right):
    return left if left < right else right


def _related_brief(item):
    return {'id': item['id'], 'title': item.get('title') or '',
            'category': item.get('category') or '',
            'category_legacy': item.get('category_legacy') or '',
            'summary': str(item.get('claim') or item.get('body') or '')[:120],
            'revision': item.get('revision')}


def related_view(store, key):
    """返回某张卡的关联：自己存的 + 别人指向它的，都算；失效 id 单独列出。"""
    item = store.get('knowledge', key)
    if item is None:
        raise KeyError(key)
    ids = related_ids(item)
    for other in store.list('knowledge', 10000):
        if other['id'] == key:
            continue
        if key in related_ids(other) and other['id'] not in ids:
            ids.append(other['id'])
    items, missing = [], []
    for rid in ids:
        linked = store.get('knowledge', rid)
        if linked is None:
            missing.append(rid)
        else:
            items.append(_related_brief(linked))
    return {'id': key, 'related_ids': [x['id'] for x in items], 'items': items,
            'count': len(items), 'missing_ids': missing, 'limit': RELATED_LIMIT}


def add_related(store, key, other):
    """加一条关联；同一对卡重复加不会产生重复记录。"""
    key = str(key or '').strip()
    other = str(other or '').strip()
    if not other:
        raise ValueError('缺少要关联的卡片 id。')
    if key == other:
        raise ValueError('不能把卡片关联到自己。')
    if store.get('knowledge', key) is None or store.get('knowledge', other) is None:
        raise KeyError(key if store.get('knowledge', key) is None else other)
    owner = _related_owner(key, other)
    target = other if owner == key else key
    owner_item = store.get('knowledge', owner)
    ids = related_ids(owner_item)
    if target not in ids:
        if len(ids) >= RELATED_LIMIT:
            raise ValueError('单张卡最多关联 %d 张，请先删掉旧关联。' % RELATED_LIMIT)
        store.patch('knowledge', owner, {'related_ids': ids + [target]}, owner_item['revision'])
    # 历史数据若在非归属一侧也存了同一条，删掉，保证只存一份。
    side_item = store.get('knowledge', target)
    if side_item and owner in related_ids(side_item):
        rest = [x for x in related_ids(side_item) if x != owner]
        store.patch('knowledge', target, {'related_ids': rest}, side_item['revision'])
    return related_view(store, key)


def remove_related(store, key, other):
    """删一条关联；两侧都清，返回删除后的关联视图。"""
    key = str(key or '').strip()
    other = str(other or '').strip()
    if store.get('knowledge', key) is None:
        raise KeyError(key)
    for side, drop in ((key, other), (other, key)):
        item = store.get('knowledge', side)
        if item is None:
            continue
        ids = related_ids(item)
        if drop in ids:
            store.patch('knowledge', side, {'related_ids': [x for x in ids if x != drop]},
                        item['revision'])
    return related_view(store, key)


def route(handler, method, bits, query, store):
    """挂在 app.py route 上：/api/content/* 与 /api/knowledge/<id>/related。"""
    if len(bits) >= 4 and bits[:2] == ['api', 'knowledge'] and bits[3] == 'related':
        key = bits[2]
        try:
            if method == 'GET' and len(bits) == 4:
                handler.json(related_view(store, key))
                return True
            if method == 'POST' and len(bits) == 4:
                handler.json(add_related(store, key, (handler.body() or {}).get('related_id')))
                return True
            if method == 'DELETE' and len(bits) == 5:
                handler.json(remove_related(store, key, bits[4]))
                return True
        except KeyError:
            handler.json({'error': '卡片不存在。'}, 404)
            return True
        except ValueError as exc:
            handler.json({'error': str(exc)}, 400)
            return True
        return False
    if len(bits) < 3 or bits[:2] != ['api', 'content']:
        return False
    action = bits[2]
    if action == 'summary' and method == 'GET':
        month = (query.get('month') or [''])[0] or None
        handler.json(summary(store, month))
        return True
    if action == 'list' and method == 'GET':
        handler.json(list_items(store, query))
        return True
    if action == 'search' and method == 'GET':
        q = (query.get('q') or [''])[0]
        try:
            limit = int((query.get('limit') or ['50'])[0])
        except ValueError:
            limit = 50
        handler.json(search(store, q, max(1, min(200, limit))))
        return True
    if action == 'status' and method == 'GET':
        handler.json(status(store))
        return True
    if action == 'rebuild' and method == 'POST':
        started = time.perf_counter()
        count = rebuild(store)
        handler.json({'ok': True, 'count': count,
                      'ms': int((time.perf_counter() - started) * 1000), **status(store)})
        return True
    return False

def similar_cards(store, text, limit=5, threshold=0.62):
    """找与给定文本高度相似的活跃认知卡（t68 去重门槛：结论 distinct 才生成候选）。

    只用本地卡片标题/主张的词集合重合度（不调模型），返回 [(重合度, item)] 降序。
    """
    terms = set(_terms(str(text or '')))
    if not terms:
        return []
    by_kind = {}
    try:
        rows = [x for x in store.list('knowledge', 10000)
                if (x.get('lifecycle') or 'active') == 'active']
    except Exception:
        return []
    result = []
    for item in rows:
        title = str(item.get('title') or '')
        claim = str(item.get('claim') or '')
        # 完全相同/包含：直接算重复（短标题的 Jaccard 会偏低，不能只看词集合）
        probe = (title + ' ' + claim).strip()
        compact = ' '.join(probe.split())
        if compact and (compact in str(text or '') or str(text or '').strip() in compact) and len(compact) >= 12:
            result.append((1.0, item))
            continue
        other = set(_terms(title + ' ' + claim))
        if not other:
            continue
        overlap = len(terms & other) / float(len(terms | other))
        if overlap >= threshold:
            result.append((round(overlap, 3), item))
    result.sort(key=lambda x: x[0], reverse=True)
    return result[:limit]
