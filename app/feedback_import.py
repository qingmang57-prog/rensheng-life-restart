"""创作者中心导出表格回采：不依赖平台接口、不需要登录态。

本人从抖音创作者中心「作品数据」导出 xlsx（或 csv），本模块负责：
1. 解析表格（列名容错：播放量/播放次数/播放都认）
2. 落 platform_work（作品档案，保留真实作品 ID）
3. 落 metric_snapshot（每个指标一条快照，source=excel_export）
4. 用规则算出一条复盘结论（review_insight，标「规则计算 · 待验证」，不冒充因果）
5. 失败行不吞：逐行给可读原因，并落一份 feedback_import 报告

口径约定：表格里没有的列一律 null，不补 0；百分比按表格原值（45.6 表示 45.6%）。
"""
from __future__ import annotations

import csv
import os
import re
from datetime import datetime, timedelta, timezone

import contracts

REPORT_KIND = 'feedback_import'

HEADER_ALIASES = {
    'title': ('作品名称', '作品标题', '标题', '视频名称', '视频标题', '内容标题'),
    'url': ('作品链接', '视频链接', '链接', '作品地址', '视频地址', '内容链接'),
    'video_id': ('作品id', '视频id', '作品ID', '视频ID', 'aweme_id', 'item_id'),
    'published_at': ('发布时间', '发布日期', '首次发布时间', '创建时间'),
    'play_count': ('播放量', '播放次数', '播放'),
    'like_count': ('点赞量', '点赞数', '点赞'),
    'comment_count': ('评论量', '评论数', '评论'),
    'share_count': ('分享量', '分享数', '分享', '转发量'),
    'collect_count': ('收藏量', '收藏数', '收藏'),
    'completion_rate': ('完播率', '5s完播率', '完播率(%)'),
    'avg_watch_seconds': ('平均播放时长', '人均播放时长', '平均播放时长(秒)'),
    'duration_seconds': ('视频时长', '视频时长(秒)'),
    'cover_url': ('封面', '封面链接'),
}

METRIC_KEYS = ('play_count', 'like_count', 'comment_count', 'share_count',
               'collect_count', 'completion_rate', 'avg_watch_seconds', 'duration_seconds')

# 计数量（整数）：播放/点赞这些不应该是 8121.0 这种浮点形态
COUNT_FIELDS = ('play_count', 'like_count', 'comment_count', 'share_count', 'collect_count')

# 与 feedback_collect 的采集口径一致：metric_key 带平台前缀（douyin.play），
# 这样导出回采与页面采集的同一指标能被总览/复盘按同一个 key 合并。
METRIC_KEYS_BY_PLATFORM = {
    'play_count': 'play', 'like_count': 'like', 'comment_count': 'comment',
    'share_count': 'share', 'collect_count': 'collect',
    'completion_rate': 'completion_rate', 'avg_watch_seconds': 'avg_watch_seconds',
    'duration_seconds': 'duration_seconds',
}

METRIC_UNITS = {
    'play_count': 'count', 'like_count': 'count', 'comment_count': 'count',
    'share_count': 'count', 'collect_count': 'count',
    'completion_rate': 'percent', 'avg_watch_seconds': 'second', 'duration_seconds': 'second',
}

METRIC_LABELS = {
    'play_count': '播放量', 'like_count': '点赞量', 'comment_count': '评论量',
    'share_count': '分享量', 'collect_count': '收藏量',
    'completion_rate': '完播率', 'avg_watch_seconds': '平均播放时长',
    'duration_seconds': '视频时长',
}

_VIDEO_ID_PATTERNS = (
    re.compile(r'/(?:video|note)/(\d+)'),
    re.compile(r'[?&](?:modal_id|aweme_id|item_id)=(\d+)'),
)


class ImportError_(ValueError):
    """导入前置条件不满足（文件不存在/不是表格/没有表头等）。"""


def _norm(text):
    return re.sub(r'[\s\u3000]+', '', str(text or '')).lower()


def _header_map(header_row):
    """表头 → 字段名；同一字段多个同名列时只认第一个。

    先做完全同名匹配，再做包含匹配（别名从长到短），避免「播放」把
    「平均播放时长」抢成播放量。
    """
    names = [_norm(cell) for cell in header_row]
    mapping = {}

    def taken():
        return set(mapping.values())

    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            target = _norm(alias)
            hit = next((index for index, name in enumerate(names)
                        if name and name == target and index not in taken()), None)
            if hit is not None:
                mapping[field] = hit
                break
    pairs = sorted(((len(_norm(alias)), _norm(alias), field)
                    for field, aliases in HEADER_ALIASES.items() for alias in aliases),
                   key=lambda pair: -pair[0])
    for _length, alias, field in pairs:
        if field in mapping:
            continue
        hit = next((index for index, name in enumerate(names)
                    if name and alias in name and index not in taken()), None)
        if hit is not None:
            mapping[field] = hit
    return mapping


def _looks_like_header(row):
    mapping = _header_map(row)
    return 'title' in mapping and len(mapping) >= 3


def _cell(row, mapping, field):
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ''
    value = row[index]
    return '' if value is None else str(value).strip()


def parse_number(text):
    """把 '1,238' '1.2万' '45.6%' '00:32' 解析成数字；解析不了返回 None（不补 0）。"""
    raw = str(text or '').strip().replace(',', '').replace('，', '')
    if not raw or raw in ('-', '--', '—', 'N/A', 'n/a', 'null', 'None'):
        return None
    if re.fullmatch(r'\d{1,2}:\d{2}(:\d{2})?', raw):
        parts = [int(p) for p in raw.split(':')]
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return float(seconds)
    multiplier = 1.0
    if raw.endswith('亿'):
        multiplier, raw = 1e8, raw[:-1]
    elif raw.endswith('万'):
        multiplier, raw = 1e4, raw[:-1]
    percent = raw.endswith('%')
    raw = raw.rstrip('%').rstrip('秒').rstrip('s').strip()
    try:
        value = float(raw) * multiplier
    except ValueError:
        return None
    return round(value, 4) if percent else round(value, 2)


def parse_video_id(url, explicit=''):
    for pattern in _VIDEO_ID_PATTERNS:
        match = pattern.search(str(url or ''))
        if match:
            return match.group(1)
    digits = re.sub(r'\D', '', str(explicit or ''))
    return digits or ''


def _load_rows(path):
    """返回 (header_row, data_rows, sheet_name)。支持 .xlsx/.xlsm 与 .csv。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.csv':
        with open(path, 'r', encoding='utf-8-sig', newline='') as handle:
            rows = [row for row in csv.reader(handle)]
        return rows, 'csv'
    if ext not in ('.xlsx', '.xlsm'):
        raise ImportError_(f'不支持的表格格式：{ext or "无扩展名"}；请导出 xlsx 或 csv。')
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 便携包内置 openpyxl
        raise ImportError_('当前 Python 缺少 openpyxl，无法读取 xlsx。') from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        return rows, sheet.title
    finally:
        workbook.close()


def parse_export(path):
    """读表格 → {rows, warnings, sheet, header_row}。表头不一定在第一行，自动向下找。"""
    if not os.path.exists(path):
        raise ImportError_(f'找不到文件：{path}')
    rows, sheet = _load_rows(path)
    header_index = None
    for index, row in enumerate(rows[:12]):
        if _looks_like_header(row):
            header_index = index
            break
    if header_index is None:
        raise ImportError_('没找到作品数据表头：至少要有「作品名称/标题」和两项指标列。')
    mapping = _header_map(rows[header_index])
    parsed, warnings = [], []
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(str(cell or '').strip() for cell in row):
            continue
        title = _cell(row, mapping, 'title')
        url = _cell(row, mapping, 'url')
        if not title and not url:
            continue
        if title and re.search(r'^(合计|总计|汇总)$', title.strip()):
            warnings.append(f'第 {offset} 行是汇总行，已跳过。')
            continue
        item = {'excel_row': offset, 'title': title, 'url': url}
        for field in METRIC_KEYS:
            if field in mapping:
                value = parse_number(_cell(row, mapping, field))
                if field in COUNT_FIELDS and value is not None and float(value).is_integer():
                    value = int(value)      # 播放/点赞这类计数量用整数落库，避免 8121.0
                item[field] = value
        item['video_id'] = parse_video_id(url, _cell(row, mapping, 'video_id'))
        item['published_at'] = _cell(row, mapping, 'published_at')
        item['cover_url'] = _cell(row, mapping, 'cover_url')
        parsed.append(item)
    missing = [METRIC_LABELS[key] for key in METRIC_KEYS if key not in mapping]
    if missing:
        warnings.append('表格缺少这些列，对应指标保持 null：' + '、'.join(missing))
    return {'rows': parsed, 'warnings': warnings, 'sheet': sheet,
            'header_row': header_index + 1, 'columns': sorted(mapping)}


def _work_metric_patch(item):
    patch = {}
    for field in ('play_count', 'like_count', 'comment_count', 'share_count', 'collect_count'):
        if field in item and item[field] is not None:
            patch[field] = item[field]
    return patch


def _metric_key(field, platform='douyin'):
    """表格字段 → 快照 metric_key（与页面采集同一口径：douyin.play）。"""
    return f'{platform}.{METRIC_KEYS_BY_PLATFORM[field]}'


def _shift_ms(stamp, milliseconds):
    """给采集时刻加毫秒：同一批导入的多条快照要各有唯一 snapshot_id。

    contracts 的快照 ID 由 (metric_key, collected_at) 派生；同一批里不同作品的同名指标
    必须错开毫秒，否则后一条会覆盖前一条。错开量是同一批次内的毫秒差，不改日期。
    """
    if not milliseconds:
        return stamp
    base = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    moment = base + timedelta(milliseconds=milliseconds)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'


def _create_rule_insight(store, project, imported, batch_id, collected_at):
    """用规则（不含模型）算一条复盘结论；每条结论必须挂 2 条以上可回溯证据。"""
    by_work = {}
    for item in imported:
        if not item.get('snapshots'):
            continue
        by_work[item['work_id']] = item

    def snap(item, field):
        wanted = _metric_key(field)
        for record in item.get('snapshots') or []:
            if record['metric_key'] == wanted:
                return record
        return None

    findings = []
    used = []

    plays = [(item, snap(item, 'play_count')) for item in by_work.values()]
    plays = [(item, record) for item, record in plays if record]
    if len(plays) >= 2:
        plays.sort(key=lambda pair: pair[1]['value'], reverse=True)
        top_item, top_snap = plays[0]
        bottom_item, bottom_snap = plays[-1]
        if top_snap['value'] > 0 and bottom_snap['value'] > 0:
            multiple = round(top_snap['value'] / bottom_snap['value'], 2)
        else:
            multiple = None      # 最低为 0 时倍数没有意义，不编造「相差多少倍」
        claim = (f'播放量最高的是《{top_item["title"]}》（{int(top_snap["value"])} 次），'
                 f'最低的是《{bottom_item["title"]}》（{int(bottom_snap["value"])} 次）')
        if multiple:
            claim += f'，相差约 {multiple} 倍'
        findings.append({'claim': claim + '。', 'evidence_ids': [top_snap['id'], bottom_snap['id']]})
        used.extend([top_snap['id'], bottom_snap['id']])

    completions = [(item, snap(item, 'completion_rate')) for item in by_work.values()]
    completions = [(item, record) for item, record in completions if record]
    if len(completions) >= 2:
        completions.sort(key=lambda pair: pair[1]['value'], reverse=True)
        best_item, best_snap = completions[0]
        worst_item, worst_snap = completions[-1]
        gap = round(best_snap['value'] - worst_snap['value'], 2)
        findings.append({
            'claim': (f'完播率最高的是《{best_item["title"]}》（{best_snap["value"]}%），'
                      f'最低的是《{worst_item["title"]}》（{worst_snap["value"]}%），相差 {gap} 个百分点。'),
            'evidence_ids': [best_snap['id'], worst_snap['id']]})
        used.extend([best_snap['id'], worst_snap['id']])

    if not findings:
        # 只有一条作品时也要出结论：播放与完播同属一个作品，证据仍为 2 条。
        for item in by_work.values():
            play_snap, comp_snap = snap(item, 'play_count'), snap(item, 'completion_rate')
            if play_snap and comp_snap:
                findings.append({
                    'claim': (f'《{item["title"]}》当前播放 {int(play_snap["value"])} 次、'
                              f'完播率 {comp_snap["value"]}%；样本只有 1 条，不足以比较，先记录待验证。'),
                    'evidence_ids': [play_snap['id'], comp_snap['id']]})
                used.extend([play_snap['id'], comp_snap['id']])
                break

    if not findings:
        return None
    evidence_ids = sorted(set(used))
    title = f'导入数据复盘：{len(by_work)} 个作品（{os.path.basename(batch_id)}）'[:160]
    body = ('数据来源：本人从创作者中心导出的作品数据表；'
            f'采集批次 {batch_id}，共 {len(by_work)} 个作品、{len(evidence_ids)} 条指标证据。\n'
            '以下为规则计算（非因果结论），只能当作待验证经验，不能当成平台机制或因果证明。')
    record = contracts.create_review_insight(
        title, body, 'data', created_at=collected_at, project_id=project['id'],
        platform_work_id=None)
    record.update({
        'findings': findings,
        'evidence_ids': evidence_ids,
        'evidence_count': len(evidence_ids),
        'platform_work_ids': sorted(by_work),
        'status': 'candidate',
        'markers': ['规则计算', '待验证'],
        'created_by': 'rule_engine',
        'validation': 'unverified',
        'source': 'excel_export',
    })
    return store.put(contracts.KIND_REVIEW_INSIGHT, record)


def import_export(store, project_id, path, collected_at=None):
    """导入一份导出表格。返回报告（同时落一条 feedback_import 记录）。"""
    project = store.get('project', project_id)
    if project is None:
        raise ImportError_(f'项目不存在：{project_id}')
    parsed = parse_export(path)
    rows = parsed['rows']
    if not rows:
        raise ImportError_('表格里没有作品数据行。')

    collected_at = collected_at or contracts.now_ms()
    batch_id = f'{os.path.basename(path)}#{datetime.now().strftime("%Y%m%d%H%M%S")}'
    existing = {}
    for record in store.list(contracts.KIND_PLATFORM_WORK, 100000):
        existing[(record.get('platform'), record.get('platform_video_id'))] = record

    errors, imported = [], []
    works_created = works_updated = snapshots_created = 0
    for item in rows:
        try:
            if not item['video_id']:
                raise ValueError('没有作品链接/作品ID，无法确认是哪条作品；请导出带「作品链接」的表格。')
            key = ('douyin', item['video_id'])
            work = existing.get(key)
            if work and work.get('project_id') and work['project_id'] != project['id']:
                raise ValueError(f'该作品已归属其他项目（{work["project_id"]}），不能重复导入。')
            if work:
                work = store.put(contracts.KIND_PLATFORM_WORK,
                                 {**work, 'project_id': project['id'],
                                  'title': item['title'] or work.get('title') or '',
                                  **_work_metric_patch(item),
                                  'raw': {**(work.get('raw') or {}),
                                          'excel_import': {'file': batch_id, 'row': item['excel_row']}}})
                works_updated += 1
            else:
                work = contracts.create_platform_work(
                    None, 'douyin', item['video_id'],
                    title=(item['title'] or f'抖音作品 {item["video_id"]}')[:160],
                    source_url=item['url'], project_id=project['id'],
                    registration_mode='excel_import',
                    raw={'excel_import': {'file': batch_id, 'row': item['excel_row']},
                         'published_at_text': item.get('published_at') or '',
                         'cover_url': item.get('cover_url') or ''},
                    **_work_metric_patch(item))
                work = store.put(contracts.KIND_PLATFORM_WORK, work)
                existing[key] = work
                works_created += 1
            item['work_id'] = work['id']
            item['snapshots'] = []
            # 每个作品错开 1ms，保证同批同名指标的 snapshot_id 不互相覆盖。
            stamp = _shift_ms(collected_at, len(imported))
            for metric_key in METRIC_KEYS:
                if item.get(metric_key) is None:
                    continue
                record = contracts.create_metric_snapshot(
                    _metric_key(metric_key), item[metric_key], METRIC_UNITS[metric_key],
                    collected_at=stamp, source='excel_export',
                    platform_work_id=work['id'],
                    dimensions={'batch': batch_id, 'work_title': item['title']},
                    raw={'excel_row': item['excel_row'], 'sheet': parsed['sheet'],
                         'platform': 'douyin', 'field': metric_key})
                saved = store.put(contracts.KIND_METRIC_SNAPSHOT, record)
                item['snapshots'].append(saved)
                snapshots_created += 1
            imported.append(item)
        except Exception as exc:  # 单行失败不影响其他行，但必须写清原因
            errors.append({'row': item.get('excel_row'), 'title': item.get('title') or '',
                           'reason': str(exc)})

    insight = _create_rule_insight(store, project, imported, batch_id, collected_at)
    report = {
        'id': contracts.new_id('feedback_import'),
        'project_id': project['id'],
        'file': os.path.abspath(path),
        'file_name': os.path.basename(path),
        'batch': batch_id,
        'sheet': parsed['sheet'],
        'header_row': parsed['header_row'],
        'columns': parsed['columns'],
        'rows_total': len(rows),
        'rows_ok': len(imported),
        'rows_failed': len(errors),
        'works_created': works_created,
        'works_updated': works_updated,
        'snapshots_created': snapshots_created,
        'insight_id': insight['id'] if insight else None,
        'errors': errors,
        'warnings': parsed['warnings'],
        'status': 'ok' if not errors else ('partial' if imported else 'failed'),
        'created_at': collected_at,
    }
    store.put(REPORT_KIND, report)
    return {'report': report, 'insight': insight}


def import_export_cli(argv):
    """命令行：python feedback_import.py <项目ID> <表格路径>（用于服务器外单独核验）。"""
    import json
    import sys

    import db
    if len(argv) < 3:
        print('用法：python feedback_import.py <项目ID> <xlsx|csv 路径>')
        return 2
    root = os.environ.get('IP_DATA_DIR') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '.runtime')
    result = import_export(db.Store(root), argv[1], argv[2])
    print(json.dumps(result['report'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    import sys

    sys.exit(import_export_cli(sys.argv))
