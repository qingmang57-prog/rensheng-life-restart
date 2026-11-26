# -*- coding: utf-8 -*-
"""60 天打卡与结算：主理人履约担保的本地记账件。

数据形态
- 档案（姓名/起始日/口径）存 sqlite 的 ``checkin_profile``，纳入现有备份；
- 打卡明细存 ``<IP_DATA_DIR>/checkin/<profile_id>.jsonl``，只追加、带哈希链；
- 结算单导出到 ``<IP_DATA_DIR>/checkin/exports/``，仅作临时产物，不参与备份。

时区
- 一律按 Asia/Shanghai（固定 UTC+8，中国自 1991 年起无夏令时）判定自然日；
- 当天 23:59:59 仍可改可删，跨过本地零点即锁死。

口径（2026-11-03 待用户最终确认，先按默认实现）
- 漏卡 1-6 次：每次扣 penalty_per_miss（默认 50 元），余额退还；
- 第 7 次及以后：forfeit_mode=capped（默认，按次扣到担保金为止）或 full（担保金全不退）。
"""
import csv
import hashlib
import io
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import checkin_cloud

TZ = timezone(timedelta(hours=8))
PERIOD_DAYS = 60
GENESIS = '0' * 64
SCHEMA_VERSION = 1
PROFILE_PREFIX = 'checkin_profile_'
PROFILE_ID_RE = re.compile(r'^checkin_profile_[a-f0-9]{8,32}$')
WEEKDAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
VIDEO_SUFFIXES = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v'}

DEFAULT_SETTINGS = {
    'deposit': 599,
    'penalty_per_miss': 50,
    'forfeit_after_misses': 6,
    'forfeit_mode': 'capped',        # capped=按次扣到担保金为止（默认）；full=超过允许次数后担保金全不退
}

_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础工具
def now_local():
    return datetime.now(TZ)


def _local(value=None):
    """把外部传入的时间统一换算到 Asia/Shanghai，避免拿 UTC 日界当本地日界。"""
    value = value or now_local()
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def today_key(now=None):
    return _local(now).date().isoformat()


def parse_date(value):
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError('日期格式应为 YYYY-MM-DD。') from None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def sha256_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def is_valid_profile_id(profile_id):
    return bool(PROFILE_ID_RE.match(str(profile_id or '')))


def checkin_root(data_dir):
    root = Path(data_dir) / 'checkin'
    root.mkdir(parents=True, exist_ok=True)
    return root


def log_path(data_dir, profile_id):
    if not is_valid_profile_id(profile_id):
        raise ValueError('档案编号无效。')
    return checkin_root(data_dir) / (profile_id + '.jsonl')


def device_id(data_dir):
    """本机标识：只用于给未来的云端同步留字段，不含任何硬件指纹。"""
    path = checkin_root(data_dir) / 'device.json'
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('device_id'):
                return str(data['device_id'])
        except (OSError, ValueError):
            pass
    value = 'dev_' + hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    path.write_text(json.dumps({'device_id': value, 'created_at': now_local().isoformat(timespec='seconds')},
                               ensure_ascii=False, indent=2), encoding='utf-8')
    return value


# ---------------------------------------------------------------- 日志与哈希链
def load_events(data_dir, profile_id):
    path = log_path(data_dir, profile_id)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            # 半行/坏行（断电、外部编辑）跳过：坏一行不该让整个打卡页 500。
            continue
    return rows


def _append_event(data_dir, profile_id, event_type, date_key, payload, actor='owner'):
    with _LOCK:
        return _append_event_locked(data_dir, profile_id, event_type, date_key, payload, actor)


def _append_event_locked(data_dir, profile_id, event_type, date_key, payload, actor='owner'):
    """已持有 _LOCK 时调用；给「读-判-写」整段加锁的调用方复用，避免重复 append。"""
    rows = load_events(data_dir, profile_id)
    prev = rows[-1]['hash'] if rows else GENESIS
    core = {
        'v': SCHEMA_VERSION,
        'seq': len(rows) + 1,
        'profile_id': profile_id,
        'type': event_type,
        'date': date_key,
        'at': now_local().isoformat(timespec='seconds'),
        'actor': actor,
        'payload': payload or {},
    }
    digest = sha256_text(prev + canonical(core))
    record = dict(core, prev=prev, hash=digest,
                  # 云端同步预留字段：不参与哈希，后续同步也不破坏链
                  sync={'state': 'local', 'remote_seq': None, 'synced_at': None,
                        'device_id': device_id(data_dir)})
    path = log_path(data_dir, profile_id)
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return record


def verify_chain(rows):
    """逐条重算哈希并核对 seq/prev；返回可展示的校验结果。"""
    prev = GENESIS
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            return {'ok': False, 'records': len(rows), 'broken_at': index,
                    'reason': '第 %d 条不是 JSON 对象。' % index}
        if row.get('seq') != index:
            return {'ok': False, 'records': len(rows), 'broken_at': index,
                    'reason': '第 %d 条序号不连续（实际 %s）。' % (index, row.get('seq'))}
        if row.get('prev') != prev:
            return {'ok': False, 'records': len(rows), 'broken_at': index,
                    'reason': '第 %d 条前序哈希对不上，记录可能被改动或插入了新行。' % index}
        core = {k: row.get(k) for k in ('v', 'seq', 'profile_id', 'type', 'date', 'at', 'actor', 'payload')}
        expected = sha256_text(prev + canonical(core))
        if row.get('hash') != expected:
            return {'ok': False, 'records': len(rows), 'broken_at': index,
                    'reason': '第 %d 条内容哈希不匹配，记录可能被改动。' % index}
        prev = row['hash']
    return {'ok': True, 'records': len(rows), 'broken_at': None, 'reason': '',
            'last_hash': prev if rows else GENESIS}


# ---------------------------------------------------------------- 档案
def _clean_settings(raw):
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for key in DEFAULT_SETTINGS:
            if key not in raw:
                continue
            value = raw[key]
            if key == 'forfeit_mode':
                if value not in ('full', 'capped'):
                    raise ValueError('漏超 6 次的口径只能选 full 或 capped。')
                settings[key] = value
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise ValueError('金额与次数必须是整数。') from None
                limits = {'deposit': (1, 100000), 'penalty_per_miss': (0, 10000),
                          'forfeit_after_misses': (0, PERIOD_DAYS - 1)}
                low, high = limits[key]
                if not low <= number <= high:
                    raise ValueError('%s 取值范围应为 %d-%d。' % (key, low, high))
                settings[key] = number
    return settings


def list_profiles(store):
    items = [x for x in store.list('checkin_profile', 100) if x.get('id')]
    return sorted(items, key=lambda x: str(x.get('created_at') or ''))


def _profile_of(store, profile_id):
    if not is_valid_profile_id(profile_id):
        raise KeyError(profile_id)
    profile = store.get('checkin_profile', profile_id)
    if not profile:
        raise KeyError(profile_id)
    return profile


def _profile_brief(profile):
    start = parse_date(profile['start_date'])
    end = start + timedelta(days=PERIOD_DAYS - 1)
    return {'id': profile['id'], 'name': profile.get('name') or '未命名主理人',
            'start_date': start.isoformat(), 'end_date': end.isoformat(),
            'note': profile.get('note') or '', 'settings': dict(profile.get('settings') or DEFAULT_SETTINGS)}


def create_profile(store, data_dir, name, start_date, settings=None, note=''):
    name = str(name or '').strip()
    if not name:
        raise ValueError('请填写主理人姓名。')
    if len(name) > 40:
        raise ValueError('姓名最多 40 个字。')
    start = parse_date(start_date)
    clean = _clean_settings(settings)
    profile = store.put('checkin_profile', {
        'name': name, 'start_date': start.isoformat(), 'note': str(note or '')[:200],
        'settings': clean, 'origin': 'local',
    })
    if not is_valid_profile_id(profile['id']):
        raise ValueError('档案编号异常，请重试。')
    _append_event(data_dir, profile['id'], 'profile_created', start.isoformat(),
                  {'name': name, 'start_date': start.isoformat(), 'settings': clean})
    return profile


def update_profile(store, data_dir, profile_id, changes):
    profile = _profile_of(store, profile_id)
    allowed = {}
    if 'name' in changes:
        name = str(changes.get('name') or '').strip()
        if not name or len(name) > 40:
            raise ValueError('姓名应为 1-40 个字。')
        allowed['name'] = name
    if 'note' in changes:
        allowed['note'] = str(changes.get('note') or '')[:200]
    if 'settings' in changes:
        allowed['settings'] = _clean_settings(changes.get('settings'))
    new_start = None
    if 'start_date' in changes:
        new_start = parse_date(changes.get('start_date'))
        if new_start.isoformat() != profile['start_date']:
            events = load_events(data_dir, profile_id)
            if any(e.get('type') in ('checkin', 'revise') for e in events):
                raise ValueError('已经打过卡，不能再改起始日；如需重来请新建档案。')
            allowed['start_date'] = new_start.isoformat()
    if not allowed:
        return profile
    before = {k: profile.get(k) for k in allowed}
    updated = store.patch('checkin_profile', profile_id, allowed)
    _append_event(data_dir, profile_id, 'profile_update',
                  updated.get('start_date'), {'before': before, 'after': allowed})
    return updated


# ---------------------------------------------------------------- 派生状态
def _event_index(rows):
    records = {}
    for row in rows:
        if row.get('type') == 'checkin':
            records[row.get('date')] = row
        elif row.get('type') == 'revise' and row.get('date') in records:
            records[row.get('date')] = row
        elif row.get('type') == 'void':
            records.pop(row.get('date'), None)
    return records


def period_dates(start_date):
    start = parse_date(start_date) if isinstance(start_date, str) else start_date
    return [start + timedelta(days=i) for i in range(PERIOD_DAYS)]


def build_days(profile, rows, today=None):
    today = today or today_key()
    today_date = parse_date(today)
    start = parse_date(profile['start_date'])
    records = _event_index(rows)
    days = []
    for date in period_dates(start):
        key = date.isoformat()
        if key in records:
            state = 'checked'
        elif date > today_date:
            state = 'future'
        elif date == today_date:
            state = 'today'
        else:
            state = 'missed'
        event = records.get(key) or {}
        payload = event.get('payload') or {}
        days.append({
            'index': (date - start).days + 1,
            'date': key,
            'weekday': WEEKDAYS[date.weekday()],
            'state': state,
            'recorded_at': event.get('at'),
            'seq': event.get('seq'),
            'text': payload.get('text') or '',
            'card_id': payload.get('card_id') or '',
            'video': payload.get('video'),
            'publish_url': payload.get('publish_url') or '',
            'note': payload.get('note') or '',
        })
    return days


def settle(misses, settings):
    settings = _clean_settings(settings)
    deposit = settings['deposit']
    penalty = settings['penalty_per_miss']
    threshold = settings['forfeit_after_misses']
    mode = settings['forfeit_mode']
    if misses > threshold and mode == 'full':
        deduction = deposit
        forfeited = True
    else:
        deduction = min(deposit, max(0, misses) * penalty)
        forfeited = False
    return {
        'deposit': deposit,
        'penalty_per_miss': penalty,
        'forfeit_after_misses': threshold,
        'forfeit_mode': mode,
        'misses': misses,
        'deduction': deduction,
        'refund': deposit - deduction,
        'deposit_forfeited': forfeited,
    }


def _stats(days, settings, today):
    missed = [d['date'] for d in days if d['state'] == 'missed']
    checked = len([d for d in days if d['state'] == 'checked'])
    future = [d for d in days if d['state'] == 'future']
    today_date = parse_date(today)
    end = parse_date(days[-1]['date']) if days else today_date
    period_done = today_date > end
    money = settle(len(missed), settings)
    return {
        'total': PERIOD_DAYS,
        'checked': checked,
        'missed': len(missed),
        'missed_dates': missed,
        'remaining_days': len(future) + (1 if any(d['state'] == 'today' for d in days) else 0),
        'remaining_misses': max(0, money['forfeit_after_misses'] - len(missed)),
        'period_done': period_done,
        'today_checked': any(d['date'] == today and d['state'] == 'checked' for d in days),
        **money,
    }


def reconcile(data_dir, profile, now=None):
    """补写历史漏卡标记（只追加、幂等）。派生状态不依赖它，它只让账本自洽。"""
    profile_id = profile['id']
    rows = load_events(data_dir, profile_id)
    today_date = _local(now).date()
    start = parse_date(profile['start_date'])
    records = _event_index(rows)
    marked = {row.get('date') for row in rows if row.get('type') == 'miss'}
    created = []
    for date in period_dates(start):
        key = date.isoformat()
        if date >= today_date or key in records or key in marked:
            continue
        created.append(_append_event(data_dir, profile_id, 'miss', key,
                                     {'reason': '跨日后自动补记漏卡', 'auto': True}))
    return created


def reconcile_all(store, data_dir, now=None):
    summary = []
    for profile in list_profiles(store):
        try:
            summary.append({'profile_id': profile['id'], 'created': len(reconcile(data_dir, profile, now=now))})
        except (ValueError, KeyError):
            continue
    return summary


def state(store, data_dir, profile_id=None, now=None):
    now = _local(now)
    profiles = [_profile_brief(p) for p in list_profiles(store)]
    if not profiles:
        return {'ok': True, 'today': now.date().isoformat(),
                'local_time': now.isoformat(timespec='seconds'),
                'profiles': [], 'profile': None, 'days': [],
                'stats': None, 'settings': dict(DEFAULT_SETTINGS), 'chain': None}
    chosen = None
    if profile_id:
        chosen = next((p for p in profiles if p['id'] == profile_id), None)
        if not chosen:
            raise KeyError(profile_id)
    chosen = chosen or profiles[-1]
    profile = _profile_of(store, chosen['id'])
    rows = load_events(data_dir, chosen['id'])
    chain = verify_chain(rows)
    days = build_days(profile, rows, now.date().isoformat())
    settings = _clean_settings(profile.get('settings'))
    stats = _stats(days, settings, now.date().isoformat())
    return {
        'ok': True,
        'today': now.date().isoformat(),
        'local_time': now.isoformat(timespec='seconds'),
        'profiles': profiles,
        'profile': {**_profile_brief(profile),
                    'created_at': profile.get('created_at'), 'updated_at': profile.get('updated_at')},
        'days': days,
        'stats': stats,
        'settings': settings,
        'chain': chain,
    }


# ---------------------------------------------------------------- 写入打卡
def _video_payload(store, data):
    artifact_id = str(data.get('artifact_id') or '').strip()
    manual = str(data.get('video_path') or '').strip()
    if artifact_id:
        artifact = store.get('artifact', artifact_id)
        if not artifact:
            raise ValueError('选中的成片记录不存在了，请重新选择。')
        path = Path(str(artifact.get('path') or ''))
        if not path.is_file():
            raise ValueError('这条成片文件已经不在了，请重新选择或改为手动填路径。')
        return {'kind': 'artifact', 'artifact_id': artifact_id, 'path': str(path),
                'filename': artifact.get('filename') or path.name,
                'size': path.stat().st_size, 'mtime': int(path.stat().st_mtime)}
    if not manual:
        raise ValueError('请选择一条成片记录，或填入成片文件的完整路径。')
    path = Path(manual)
    if not path.is_file():
        raise ValueError('找不到这个文件：%s' % manual)
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError('成片应为 mp4/mov/mkv/avi/webm/m4v 视频文件。')
    return {'kind': 'path', 'artifact_id': None, 'path': str(path), 'filename': path.name,
            'size': path.stat().st_size, 'mtime': int(path.stat().st_mtime)}


def _clean_optional(data):
    url = str(data.get('publish_url') or '').strip()
    note = str(data.get('note') or '').strip()
    if url and not re.match(r'^https?://', url):
        raise ValueError('发布链接应以 http:// 或 https:// 开头。')
    if len(url) > 500:
        raise ValueError('发布链接过长。')
    if len(note) > 500:
        raise ValueError('备注最多 500 字。')
    return url, note


def _checkin_card_fields(text, profile_id, date_key, now):
    import content_index
    title = (text.splitlines()[0].strip() if text.splitlines() else '')[:40] or '打卡随手记'
    category, legacy = content_index.normalize_category('随手记')
    return {
        'legacy_id': '打卡随手记',
        'title': title,
        'category': category,
        'category_legacy': legacy,
        'category_aliases': content_index.row_aliases(category, legacy),
        'claim': text,
        'boundary': '来自每日打卡，随手记下的一句话。',
        'body': text,
        'source_id': None,
        'source_locator': {'kind': 'checkin', 'profile_id': profile_id, 'date': date_key},
        'source_type': 'checkin',
        'source_label': '打卡 · %s' % date_key,
        'source_date': date_key,
        'source_profile_id': profile_id,
        'links': [],
        'markers': [],
        'endorsement': 'reference_only',
        'validation': 'unverified',
        'lifecycle': 'active',
        'content_use': 'available',
        'recorded_at': now.isoformat(timespec='seconds'),
    }


def submit_checkin(store, data_dir, profile_id, data, now=None):
    now = _local(now)
    profile = _profile_of(store, profile_id)
    text = str(data.get('text') or '').strip()
    if not text:
        raise ValueError('先写一句再打卡。')
    if len(text) > 4000:
        raise ValueError('一次最多记 4000 字。')
    today = now.date()
    start = parse_date(profile['start_date'])
    end = start + timedelta(days=PERIOD_DAYS - 1)
    if today < start:
        raise ValueError('打卡周期还没开始（起始日 %s）。' % profile['start_date'])
    if today > end:
        raise ValueError('60 天周期已于 %s 结束，不能再打卡。' % end.isoformat())
    # 读-判-写必须同一把锁：两个并发打卡都看不到今天已有记录，就会写两条。
    with _LOCK:
        rows = load_events(data_dir, profile_id)
        records = _event_index(rows)
        today_key_str = today.isoformat()
        previous = records.get(today_key_str) or {}
        previous_payload = previous.get('payload') or {}
        video = None
        if str(data.get('artifact_id') or '').strip() or str(data.get('video_path') or '').strip():
            video = _video_payload(store, data)
        url, note = _clean_optional(data)
        fields = _checkin_card_fields(text, profile_id, today_key_str, now)
        card_id = str(previous_payload.get('card_id') or '')
        card = store.get('knowledge', card_id) if card_id else None
        if card:
            # 现有 PATCH /api/knowledge 只允许改背书/状态，不改正文；
            # 这里在同一 store 记录上直接更新正文，等价于编辑卡片。
            changes = {k: fields[k] for k in ('title', 'category', 'category_legacy', 'category_aliases',
                                              'claim', 'boundary', 'body', 'source_locator', 'source_type',
                                              'source_label', 'source_date', 'source_profile_id', 'recorded_at')}
            store.patch('knowledge', card_id, changes)
        else:
            card = store.put('knowledge', fields)
            card_id = card['id']
        event_type = 'revise' if today_key_str in records else 'checkin'
        _append_event_locked(data_dir, profile_id, event_type, today_key_str,
                             {'text': text, 'card_id': card_id, 'video': video,
                              'publish_url': url, 'note': note})
    # 索引重建约 1 秒，放到后台，不拖慢打卡提交；图谱随后可见。
    threading.Thread(target=_refresh_index, args=(store,), name='checkin-index-refresh', daemon=True).start()
    return state(store, data_dir, profile_id, now=now)


def _refresh_index(store):
    try:
        import content_index
        content_index.rebuild(store)
    except Exception:
        pass


def void_checkin(store, data_dir, profile_id, date_key=None, now=None):
    now = _local(now)
    profile = _profile_of(store, profile_id)
    target = str(date_key or now.date().isoformat())
    if target != now.date().isoformat():
        raise ValueError('只能删除今天的打卡；历史日期已锁定。')
    rows = load_events(data_dir, profile_id)
    if target not in _event_index(rows):
        raise ValueError('今天还没有打卡记录。')
    _append_event(data_dir, profile_id, 'void', target, {'reason': '本人当天删除'})
    return state(store, data_dir, profile_id, now=now)


def outputs(store, limit=60):
    items = []
    for artifact in store.list('artifact', 300):
        path = Path(str(artifact.get('path') or ''))
        if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
            continue
        items.append({
            'id': artifact.get('id'), 'kind': artifact.get('kind'),
            'filename': artifact.get('filename') or path.name, 'path': str(path),
            'project_id': artifact.get('project_id'),
            'created_at': artifact.get('created_at'),
            'size': path.stat().st_size,
        })
    items.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    return items[:limit]


# ---------------------------------------------------------------- 结算单导出
def _state_for(store, data_dir, profile_id, now=None):
    payload = state(store, data_dir, profile_id, now=now)
    if not payload.get('profile'):
        raise KeyError(profile_id)
    return payload


def _amount_text(value):
    return str(int(value))


def csv_text(payload):
    profile = payload['profile']
    stats = payload['stats']
    chain = payload['chain']
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(['60 天打卡记录'])
    writer.writerow(['主理人', profile['name']])
    writer.writerow(['周期', '%s 至 %s' % (profile['start_date'], profile['end_date'])])
    writer.writerow(['生成时间', payload['local_time']])
    writer.writerow(['应打卡天数', stats['total']])
    writer.writerow(['已打卡', stats['checked']])
    writer.writerow(['漏卡次数', stats['missed']])
    writer.writerow(['漏卡日期', '、'.join(stats['missed_dates']) or '无'])
    writer.writerow(['周期是否结束', '是' if stats['period_done'] else '否'])
    writer.writerow(['哈希链校验', '通过（%d 条）' % chain['records'] if chain['ok']
                     else '失败：' + str(chain.get('reason') or '')])
    writer.writerow(['末条哈希', chain.get('last_hash') or ''])
    writer.writerow([])
    writer.writerow(['序号', '日期', '星期', '状态', '今天记的句子', '成片文件', '发布链接', '备注', '打卡时间', '记录序号', '知识卡ID'])
    for day in payload['days']:
        video = day.get('video') or {}
        writer.writerow([
            day['index'], day['date'], day['weekday'],
            {'checked': '已打卡', 'missed': '漏卡', 'future': '未到', 'today': '今日待打卡'}[day['state']],
            day.get('text') or '', video.get('filename') or '',
            day.get('publish_url') or '', day.get('note') or '',
            (day.get('recorded_at') or '').replace('T', ' '), day.get('seq') or '',
            day.get('card_id') or '',
        ])
    writer.writerow([])
    writer.writerow(['说明', '本记录由工作台本机生成；打卡明细为只追加 JSONL + SHA-256 哈希链，导出时逐条校验。'])
    return buffer.getvalue()


def html_text(payload):
    profile = payload['profile']
    stats = payload['stats']
    chain = payload['chain']

    def esc(value):
        text = str(value if value is not None else '')
        return (text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;'))

    rows = []
    for day in payload['days']:
        state_text = {'checked': '已打卡', 'missed': '漏卡', 'future': '未到', 'today': '今日待打卡'}[day['state']]
        rows.append(
            '<tr class="st-%s"><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                day['state'], esc(day['date']), esc(day['weekday']), esc(state_text),
                esc(day.get('text') or '—'),
                esc(day.get('note') or day.get('publish_url') or ''),
            ))
    half = (len(rows) + 1) // 2
    left = ''.join(rows[:half])
    right = ''.join(rows[half:])
    missed_text = '、'.join(stats['missed_dates']) or '无'
    chain_text = ('通过（%d 条记录，末条哈希 %s）' % (chain['records'], chain.get('last_hash') or '')
                  if chain['ok'] else '未通过：%s' % (chain.get('reason') or '未知原因'))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>60 天打卡记录 · {esc(profile['name'])}</title>
<link rel="stylesheet" href="/checkin-print.css?v=20261103">
<script src="/checkin-print.js?v=20261103" defer></script>
</head>
<body>
<main class="doc">
  <header class="doc-head">
    <div>
      <p class="doc-eyebrow">60 天打卡 · 打卡记录</p>
      <h1>{esc(profile['name'])}</h1>
      <p class="doc-meta">周期 {esc(profile['start_date'])} 至 {esc(profile['end_date'])} · 生成于 {esc(payload['local_time'])}</p>
    </div>
    <button class="doc-print" type="button" data-print="1">打印 / 另存为 PDF</button>
  </header>

  <section class="doc-cards">
    <div class="doc-card"><span>已打卡</span><strong>{stats['checked']} / {stats['total']}</strong></div>
    <div class="doc-card"><span>漏卡</span><strong>{stats['missed']} 次</strong></div>
  </section>

  <section class="doc-rules">
    <p><b>规则</b>每天 1 条作品，满 60 条完成本周期。</p>
    <p><b>漏卡日期</b>{esc(missed_text)}</p>
  </section>

  <section class="doc-tablewrap">
    <table class="doc-table">
      <thead><tr class="doc-tablehead"><th>日期</th><th>星期</th><th>状态</th><th>今天记的句子</th><th>备注 / 链接</th></tr></thead>
      <tbody>{left}</tbody>
    </table>
    <table class="doc-table">
      <thead><tr class="doc-tablehead"><th>日期</th><th>星期</th><th>状态</th><th>今天记的句子</th><th>备注 / 链接</th></tr></thead>
      <tbody>{right}</tbody>
    </table>
  </section>

  <section class="doc-foot">
    <div>
      <p><b>账本校验</b>{esc(chain_text)}</p>
      <p><b>凭据说明</b>本记录由本机工作台生成；打卡记录为只追加 JSONL + SHA-256 哈希链，导出时逐条校验。</p>
    </div>
  </section>
</main>
</body>
</html>
"""


def export_files(store, data_dir, profile_id, now=None):
    payload = _state_for(store, data_dir, profile_id, now=now)
    folder = checkin_root(data_dir) / 'exports'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _local(now).strftime('%Y%m%d-%H%M%S')
    base = '打卡记录_%s_%s_%s' % (re.sub(r'[\\/:*?"<>|\s]+', '_', payload['profile']['name'])[:20],
                              payload['profile']['start_date'], stamp)
    csv_file = folder / (base + '.csv')
    html_file = folder / (base + '.html')
    csv_file.write_text(csv_text(payload), encoding='utf-8-sig', newline='')
    html_file.write_text(html_text(payload), encoding='utf-8')
    return payload, csv_file, html_file


# ---------------------------------------------------------------- HTTP 路由
def route(handler, method, bits, query, store, data_dir):
    """挂在 app.py 的 route 里；只处理 /api/checkin/*，命中返回 True。"""
    if not bits or bits[:2] != ['api', 'checkin']:
        return False

    def q(name, default=''):
        return (query.get(name) or [default])[0]

    path = '/' + '/'.join(bits)
    if path == '/api/checkin/profiles' and method == 'GET':
        handler.json({'ok': True, 'items': [_profile_brief(p) for p in list_profiles(store)]})
        return True
    if path == '/api/checkin/profiles' and method == 'POST':
        data = handler.body()
        profile = create_profile(store, data_dir, data.get('name'), data.get('start_date'),
                                 data.get('settings'), data.get('note'))
        handler.json({'ok': True, 'profile': _profile_brief(profile),
                      'state': state(store, data_dir, profile['id'])}, 201)
        return True
    if len(bits) == 4 and bits[:3] == ['api', 'checkin', 'profiles'] and method == 'PATCH':
        data = handler.body()
        profile = update_profile(store, data_dir, bits[3], data)
        handler.json({'ok': True, 'profile': _profile_brief(profile),
                      'state': state(store, data_dir, profile['id'])})
        return True
    if path == '/api/checkin/state' and method == 'GET':
        handler.json(state(store, data_dir, q('profile_id') or None))
        return True
    if path == '/api/checkin/outputs' and method == 'GET':
        handler.json({'ok': True, 'items': outputs(store, int(q('limit', '60') or 60))})
        return True
    if path == '/api/checkin/checkin' and method == 'POST':
        data = handler.body()
        handler.json(submit_checkin(store, data_dir, data.get('profile_id'), data))
        # 2026-11-14 只上报日期：本地已落盘，异步发送、失败静默，不影响打卡结果。
        checkin_cloud.queue(store, today_key(), handler)
        return True
    if path == '/api/checkin/void' and method == 'POST':
        data = handler.body()
        handler.json(void_checkin(store, data_dir, data.get('profile_id'), data.get('date')))
        return True
    if path == '/api/checkin/settlement' and method == 'GET':
        profile_id = q('profile_id')
        if not profile_id:
            raise ValueError('缺少 profile_id。')
        fmt = (q('format', 'csv') or 'csv').lower()
        payload, csv_file, html_file = export_files(store, data_dir, profile_id)
        if fmt == 'html':
            handler.send_file(html_file, download=bool(q('download')))
            return True
        handler.send_file(csv_file, download=True, filename=csv_file.name)
        return True
    raise ValueError('接口不存在。')
