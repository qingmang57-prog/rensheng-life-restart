"""链路数据契约：采集、发布、指标、数字人各链路实体的 kind、创建函数、通用校验与状态机。

纯标准库，不写文件、不联网。所有实体都是可 JSON 序列化的 dict，
配合 db.Store(kind,id,data JSON,revision) 泛型存储直接使用，不需要改存储层。

约定：
- 所有 ID（id/project_id/platform_video_id 等）必须是字符串；未知为 null，不用负数冒充。
- 互动指标可 null，非 null 时必须是 >= 0 的数字。
- 采集与发布对象必须带 collected_at 或 created_at，格式为 ISO8601 毫秒字符串。
- 来源 URL 字段必须存在，允许显式空串 ''，不允许缺 key。
"""
import json
import re
import uuid
from datetime import datetime, timezone


# ---- kind 常量 ----
KIND_BENCH_ACCOUNT = 'bench_account'
KIND_BENCH_WORK = 'bench_work'
KIND_BENCH_COMMENT = 'bench_comment'
KIND_PLATFORM_COMMENT = 'platform_comment'
KIND_TOPIC = 'topic'
KIND_PLATFORM_ACCOUNT = 'platform_account'
KIND_MATRIX_PUBLISH_TASK = 'matrix_publish_task'
KIND_PLATFORM_WORK = 'platform_work'
KIND_METRIC_SNAPSHOT = 'metric_snapshot'
KIND_COMMENT_FEEDBACK = 'comment_feedback'
KIND_REVIEW_INSIGHT = 'review_insight'
KIND_DIGITAL_HUMAN_PROVIDER = 'digital_human_provider'
KIND_DIGITAL_HUMAN_JOB = 'digital_human_job'
KIND_DIGITAL_HUMAN_ASSET = 'digital_human_asset'

KNOWN_KINDS = frozenset((
    KIND_BENCH_ACCOUNT, KIND_BENCH_WORK, KIND_BENCH_COMMENT, KIND_PLATFORM_COMMENT,
    KIND_TOPIC,
    KIND_PLATFORM_ACCOUNT, KIND_MATRIX_PUBLISH_TASK, KIND_PLATFORM_WORK,
    KIND_METRIC_SNAPSHOT, KIND_COMMENT_FEEDBACK, KIND_REVIEW_INSIGHT,
    KIND_DIGITAL_HUMAN_PROVIDER, KIND_DIGITAL_HUMAN_JOB, KIND_DIGITAL_HUMAN_ASSET,
))

# ---- 时间戳 ----
_TS_MS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}(Z|[+-]\d{2}:?\d{2})$')
# Windows 盘符绝对路径（如用户目录下的完整路径）；URL 里的盘符字符前面紧邻字母，不会误伤。
_ABS_PATH = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]')


def now_ms():
    """ISO8601 毫秒字符串（UTC，Z 结尾）。"""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def new_id(prefix):
    return prefix + '_' + uuid.uuid4().hex[:16]


def check_timestamp(value, name):
    if not isinstance(value, str) or not _TS_MS.match(value):
        raise ValueError(f'{name} 必须是 ISO8601 毫秒字符串（如 2026-10-27T08:00:00.000Z）。')
    return value


def check_id(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} 必须是非空字符串。')
    return value


def check_metric(value, name):
    """互动指标：可 null；非 null 必须是 >= 0 的数字，禁止用负数冒充未知。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} 必须是数字或 null；未知请用 null，不能用其他类型冒充。')
    if value < 0:
        raise ValueError(f'{name} 不能为负数；未知请用 null，不要用负数冒充。')
    return value


def check_json_dict(value, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} 必须是对象。')
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError(f'{name} 必须可 JSON 序列化。') from None
    return value


def check_no_abs_user_path(record):
    """platform_account 等实体不得硬编码绝对用户路径（会话引用用 opaque 标识）。"""
    def scan(value):
        if isinstance(value, str):
            if _ABS_PATH.search(value):
                raise ValueError('数据中不能硬编码绝对用户路径；请使用 session_ref 等间接引用。')
        elif isinstance(value, dict):
            for v in value.values():
                scan(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                scan(v)
    scan(record)
    return record


# ---- 字段模式：[(字段名, 校验类型, 是否必填)] ----
# 类型：id / str / url（必填但允许 ''）/ ts / ts_or_null / metric / dict / list / enum:<集合名>
SCHEMAS = {
    KIND_BENCH_ACCOUNT: [
        ('platform', 'str', True), ('display_name', 'str', True),
        ('home_url', 'url', True), ('external_id', 'str', True),
        ('collected_at', 'ts', True),
        ('follower_count', 'metric', False), ('following_count', 'metric', False),
        ('works_count', 'metric', False), ('raw', 'dict', False),
    ],
    KIND_BENCH_WORK: [
        ('bench_account_id', 'id', True), ('platform', 'str', True),
        ('platform_video_id', 'id', True), ('title', 'str', True),
        ('source_url', 'url', True), ('collected_at', 'ts', True),
        ('published_at', 'ts_or_null', False),
        ('like_count', 'metric', False), ('comment_count', 'metric', False),
        ('share_count', 'metric', False), ('collect_count', 'metric', False),
        ('play_count', 'metric', False), ('duration_ms', 'metric', False),
        ('raw', 'dict', False),
    ],
    KIND_BENCH_COMMENT: [
        ('bench_work_id', 'id', True), ('platform', 'str', True),
        ('platform_comment_id', 'id', True), ('author_name', 'str', True),
        ('content', 'str', True), ('source_url', 'url', True),
        ('collected_at', 'ts', True),
        ('parent_comment_id', 'id', False),
        ('like_count', 'metric', False), ('reply_count', 'metric', False),
        ('raw', 'dict', False),
    ],
    KIND_PLATFORM_COMMENT: [
        ('platform_work_id', 'id', True), ('platform', 'str', True),
        ('platform_comment_id', 'id', True), ('author_name', 'str', True),
        ('content', 'str', True), ('source_url', 'url', True),
        ('collected_at', 'ts', True),
        ('parent_comment_id', 'id_or_null', False),
        ('like_count', 'metric', False), ('reply_count', 'metric', False),
        ('category', 'str_or_null', False),  # 评论分类（question/painpoint/objection/praise/sequel/other），未分类为 null
        ('raw', 'dict', False),
    ],
    KIND_TOPIC: [
        ('title', 'str', True), ('status', 'enum:topic_status', True),
        ('bench_work_id', 'id', False), ('knowledge_id', 'id', False),
        ('creation_job_id', 'id', False), ('review_insight_id', 'id_or_null', False),
        ('note', 'str', False), ('share_to', 'str', False), ('content_type', 'str', False), ('basis', 'str', False), ('created_at', 'ts', True),
    ],
    KIND_PLATFORM_ACCOUNT: [
        ('platform', 'str', True), ('display_name', 'str', True),
        ('session_ref', 'str', True), ('status', 'enum:platform_account_status', True),
        ('capabilities', 'list', True), ('created_at', 'ts', True),
        ('platform_user_id', 'str', False),
    ],
    KIND_MATRIX_PUBLISH_TASK: [
        ('project_id', 'id', True), ('artifact_id', 'id', True),
        ('script_version_id', 'id', True), ('platform_account_id', 'id', True),
        ('idempotency_key', 'str', True), ('platform_payload', 'dict', True),
        ('status', 'enum:publish_status', True),
        ('scheduled_for', 'ts_or_null', False),
        ('platform_work_id', 'id_or_null', False),
        ('platform_url', 'url_or_null', False),
        ('result_message', 'str_or_null', False),
        ('created_at', 'ts', True),
    ],
    KIND_PLATFORM_WORK: [
        # 手动登记（本人自己发的作品）没有工作台账号记录，platform_account_id 允许 null；
        # 代发链路的 platform_work 仍由 matrix_service 写入真实账号 ID。
        ('platform_account_id', 'id_or_null', True), ('platform', 'str', True),
        ('platform_video_id', 'id', True), ('title', 'str', True),
        ('source_url', 'url', True), ('created_at', 'ts', True),
        ('publish_task_id', 'id', False), ('published_at', 'ts_or_null', False),
        ('project_id', 'id_or_null', False),
        ('registration_mode', 'str_or_null', False),  # manual=本人手动登记；空=矩阵代发回登
        # P0-3 回执核验：链接解析等级与最近一次失败原因（旧行不写这些字段，保持原样）。
        ('verification', 'enum:receipt_verification', False),
        ('last_error_code', 'str_or_null', False),
        ('last_error_message', 'str_or_null', False),
        ('last_attempt_at', 'ts_or_null', False),
        ('like_count', 'metric', False), ('comment_count', 'metric', False),
        ('share_count', 'metric', False), ('collect_count', 'metric', False),
        ('play_count', 'metric', False), ('raw', 'dict', False),
    ],
    KIND_METRIC_SNAPSHOT: [
        ('metric_key', 'str', True), ('value', 'number', True), ('unit', 'str', True),
        ('collected_at', 'ts', True), ('source', 'str', True),
        ('platform_work_id', 'id_or_null', False),
        ('dimensions', 'dict', False), ('raw', 'dict', False),
    ],
    KIND_COMMENT_FEEDBACK: [
        # bench_* 关联与 platform_* 关联二选一：旧 bench_comment 链路（bench_comment_id 非空）
        # 保持不变；本人作品评论链路用 platform_comment_id 引用 platform_comment.id。
        # 两个 id 字段保持 required（键必须存在），平台链路显式置 null。
        ('bench_comment_id', 'id_or_null', True), ('bench_work_id', 'id_or_null', True),
        ('feedback_type', 'enum:feedback_type', True),
        ('note', 'str', False), ('created_at', 'ts', True),
        ('platform_comment_id', 'id_or_null', False), ('platform_work_id', 'id_or_null', False),
    ],
    KIND_REVIEW_INSIGHT: [
        ('title', 'str', True), ('body', 'str', True),
        ('insight_type', 'enum:insight_type', True),
        ('project_id', 'id_or_null', False), ('bench_work_id', 'id_or_null', False),
        ('platform_work_id', 'id_or_null', False), ('knowledge_id', 'id_or_null', False),
        ('created_at', 'ts', True),
    ],
    KIND_DIGITAL_HUMAN_PROVIDER: [
        ('name', 'str', True), ('provider_key', 'str', True),
        ('status', 'enum:provider_status', True), ('capabilities', 'list', True),
        ('session_ref', 'str', False), ('config', 'dict', False),
        ('created_at', 'ts', True),
    ],
    KIND_DIGITAL_HUMAN_JOB: [
        ('provider_id', 'id', True), ('script_version_id', 'id', True),
        ('asset_id', 'id_or_null', True),
        ('status', 'enum:digital_human_status', True),
        ('request', 'dict', True), ('result', 'dict_or_null', True),
        ('error', 'str_or_null', True), ('created_at', 'ts', True),
    ],
    KIND_DIGITAL_HUMAN_ASSET: [
        ('provider_id', 'id', True), ('job_id', 'id', True),
        ('asset_kind', 'enum:asset_kind', True), ('uri', 'str', True),
        ('duration_ms', 'metric', False), ('meta', 'dict', False),
        ('created_at', 'ts', True),
    ],
}

ENUMS = {
    'topic_status': ('candidate', 'approved', 'rejected', 'archived'),
    'platform_account_status': ('connected', 'pending', 'needs_action', 'disconnected'),
    'publish_status': ('draft', 'queued', 'running', 'published', 'failed', 'cancelled'),
    'feedback_type': ('like', 'reply', 'pin', 'hide', 'report', 'none'),
    'insight_type': ('data', 'content', 'workflow'),
    'provider_status': ('active', 'disabled'),
    'digital_human_status': ('pending', 'queued', 'running', 'succeeded', 'failed', 'cancelled'),
    'asset_kind': ('video', 'audio', 'image', 'model', 'other'),
    # 回执核验等级：self_reported=本人自述未解析；link_parsed=链接已解析出作品 ID；
    # verified=轻核验打开平台页面比对通过（2026-11-16 随轻核验启用）；unverified=比对不通过。
    'receipt_verification': ('self_reported', 'link_parsed', 'verified', 'unverified'),
}

# 跨字段规则：这些 kind 至少要有其中一个字段非空。
REQUIRE_ONE_OF = {
    # topic 来源：旧链路 bench_work/knowledge/creation_job 兼容，新链路 review_insight（复盘洞察生成选题）。
    KIND_TOPIC: ('bench_work_id', 'knowledge_id', 'creation_job_id', 'review_insight_id'),
    KIND_COMMENT_FEEDBACK: ('bench_comment_id', 'platform_comment_id'),
    KIND_REVIEW_INSIGHT: ('project_id', 'bench_work_id', 'platform_work_id', 'knowledge_id'),
}

NO_ABS_PATH_KINDS = frozenset((KIND_PLATFORM_ACCOUNT, KIND_DIGITAL_HUMAN_PROVIDER))


def _check_field(kind, name, ftype, value):
    if ftype == 'id':
        return check_id(value, name)
    if ftype == 'id_or_null':
        return None if value is None else check_id(value, name)
    if ftype == 'str':
        if not isinstance(value, str) or not value:
            raise ValueError(f'{kind}.{name} 必须是非空字符串。')
        return value
    if ftype == 'str_or_null':
        if value is not None and not isinstance(value, str):
            raise ValueError(f'{kind}.{name} 必须是字符串或 null。')
        return value
    if ftype == 'url':
        # 来源 URL 必填或明确为空：key 必须存在，值允许 ''。
        if not isinstance(value, str):
            raise ValueError(f'{kind}.{name} 必须是字符串；未知来源请显式置空串。')
        return value
    if ftype == 'url_or_null':
        if value is not None and not isinstance(value, str):
            raise ValueError(f'{kind}.{name} 必须是字符串或 null。')
        return value
    if ftype == 'ts':
        return check_timestamp(value, name)
    if ftype == 'ts_or_null':
        return None if value is None else check_timestamp(value, name)
    if ftype == 'metric':
        return check_metric(value, name)
    if ftype == 'number':
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{kind}.{name} 必须是数字。')
        return value
    if ftype == 'dict':
        return check_json_dict(value, name)
    if ftype == 'dict_or_null':
        return None if value is None else check_json_dict(value, name)
    if ftype == 'list':
        if not isinstance(value, list) or not value:
            raise ValueError(f'{kind}.{name} 必须是非空数组。')
        return value
    if ftype.startswith('enum:'):
        allowed = ENUMS[ftype.split(':', 1)[1]]
        if value not in allowed:
            raise ValueError(f'{kind}.{name} 状态无效：{value!r}；合法值为 {sorted(allowed)}。')
        return value
    raise ValueError(f'{kind}.{name} 使用了未知的校验类型 {ftype!r}。')


def validate_record(kind, data):
    """校验并返回清理后的记录副本；未知 kind 或必填缺失/类型越界抛 ValueError。"""
    if kind not in KNOWN_KINDS:
        raise ValueError(f'未知的数据 kind：{kind!r}。')
    if not isinstance(data, dict):
        raise ValueError(f'{kind} 记录必须是对象。')
    schema = SCHEMAS[kind]
    clean = {}
    for name, ftype, required in schema:
        if name not in data:
            if required:
                raise ValueError(f'{kind} 缺少必填字段：{name}。')
            continue
        value = data[name]
        if value is None:
            if required and ftype not in ('id_or_null', 'str_or_null', 'dict_or_null', 'ts_or_null', 'url_or_null'):
                raise ValueError(f'{kind}.{name} 为必填，不能为 null。')
            clean[name] = None
            continue
        if not required and value == '':
            clean[name] = None
            continue
        clean[name] = _check_field(kind, name, ftype, value)
    if 'id' in data:
        clean['id'] = check_id(data['id'], 'id')
    for field in REQUIRE_ONE_OF.get(kind, ()):
        if data.get(field):
            break
    else:
        if kind in REQUIRE_ONE_OF:
            options = '、'.join(REQUIRE_ONE_OF[kind])
            raise ValueError(f'{kind} 必须至少保存一个来源引用：{options}。')
    if kind == KIND_MATRIX_PUBLISH_TASK and clean.get('status') == 'published':
        if not clean.get('platform_work_id') or not clean.get('platform_url'):
            raise ValueError('matrix_publish_task 的 published 状态必须带 platform_work_id 与 platform_url；'
                             '成功只能经 set_publish_success 设置。')
    if kind == KIND_DIGITAL_HUMAN_JOB and clean.get('status') == 'succeeded':
        result = clean.get('result')
        if not isinstance(result, dict) or not result.get('result_asset_id'):
            raise ValueError('digital_human_job 成功结果的 result 必须包含 result_asset_id。')
    if kind in NO_ABS_PATH_KINDS:
        check_no_abs_user_path(clean)
    return clean


# ---- 状态机 ----
TRANSITIONS = {
    'collection': {
        'pending': {'collecting', 'cancelled'},
        'collecting': {'collected', 'failed'},
        'collected': set(),
        'failed': {'pending'},
        'cancelled': {'pending'},
    },
    'publish': {
        'draft': {'queued', 'cancelled'},
        'queued': {'running', 'cancelled', 'failed'},
        'running': {'published', 'failed', 'cancelled'},
        'published': set(),
        'failed': {'queued'},
        'cancelled': {'queued'},
    },
    'digital_human': {
        'pending': {'queued', 'cancelled'},
        'queued': {'running', 'cancelled'},
        'running': {'succeeded', 'failed', 'cancelled'},
        'succeeded': set(),
        'failed': {'queued'},
        'cancelled': {'queued'},
    },
}


def states_of(machine):
    if machine not in TRANSITIONS:
        raise ValueError(f'未知状态机：{machine!r}；支持 {sorted(TRANSITIONS)}。')
    return frozenset(TRANSITIONS[machine])


def can_transition(machine, current, target):
    if machine not in TRANSITIONS:
        raise ValueError(f'未知状态机：{machine!r}；支持 {sorted(TRANSITIONS)}。')
    if current not in TRANSITIONS[machine]:
        raise ValueError(f'{machine} 状态机不存在状态 {current!r}。')
    if target not in TRANSITIONS[machine]:
        raise ValueError(f'{machine} 状态机不存在状态 {target!r}。')
    return target in TRANSITIONS[machine][current]


def check_transition(machine, current, target):
    """校验一次状态迁移；未知状态或非法迁移抛 ValueError。"""
    if not can_transition(machine, current, target):
        raise ValueError(f'非法状态迁移：{machine} 不允许 {current} -> {target}。')
    return target


def transition(record, machine, target, **extra):
    """返回状态迁移后的新记录副本，不改入参。"""
    current = record.get('status')
    if not isinstance(current, str):
        raise ValueError('记录缺少字符串 status 字段，无法迁移状态。')
    check_transition(machine, current, target)
    return {**record, 'status': target, **extra}


# ---- 创建函数 ----
def create_bench_account(platform, display_name, home_url='', external_id='',
                         collected_at=None, raw=None, **metrics):
    return validate_record(KIND_BENCH_ACCOUNT, {
        'id': new_id('bench_account'), 'platform': platform, 'display_name': display_name,
        'home_url': home_url, 'external_id': external_id,
        'collected_at': collected_at or now_ms(), 'raw': raw or {},
        **metrics,
    })


def create_bench_work(bench_account_id, platform, platform_video_id, title,
                      source_url='', collected_at=None, published_at=None, raw=None, **metrics):
    return validate_record(KIND_BENCH_WORK, {
        'id': new_id('bench_work'), 'bench_account_id': bench_account_id, 'platform': platform,
        'platform_video_id': platform_video_id, 'title': title, 'source_url': source_url,
        'collected_at': collected_at or now_ms(), 'published_at': published_at, 'raw': raw or {},
        **metrics,
    })


def create_bench_comment(bench_work_id, platform, platform_comment_id, author_name, content,
                         source_url='', collected_at=None, parent_comment_id=None,
                         raw=None, **metrics):
    return validate_record(KIND_BENCH_COMMENT, {
        'id': new_id('bench_comment'), 'bench_work_id': bench_work_id, 'platform': platform,
        'platform_comment_id': platform_comment_id, 'author_name': author_name,
        'content': content, 'source_url': source_url,
        'collected_at': collected_at or now_ms(), 'parent_comment_id': parent_comment_id,
        'raw': raw or {}, **metrics,
    })


def create_topic(title, bench_work_id=None, knowledge_id=None, status='candidate',
                 note='', created_at=None, creation_job_id=None, review_insight_id=None):
    return validate_record(KIND_TOPIC, {
        'id': new_id('topic'), 'title': title, 'status': status,
        'bench_work_id': bench_work_id, 'knowledge_id': knowledge_id,
        'creation_job_id': creation_job_id, 'review_insight_id': review_insight_id,
        'note': note, 'created_at': created_at or now_ms(),
    })


def create_platform_account(platform, display_name, session_ref, capabilities,
                            status='pending', platform_user_id=None, created_at=None):
    return validate_record(KIND_PLATFORM_ACCOUNT, {
        'id': new_id('platform_account'), 'platform': platform, 'display_name': display_name,
        'session_ref': session_ref, 'status': status, 'capabilities': list(capabilities),
        'platform_user_id': platform_user_id, 'created_at': created_at or now_ms(),
    })


def create_matrix_publish_task(project_id, artifact_id, script_version_id, platform_account_id,
                               idempotency_key, platform_payload,
                               scheduled_for=None, created_at=None):
    """发布任务一律从 draft 起步；published 只能经 set_publish_success 设置。"""
    return validate_record(KIND_MATRIX_PUBLISH_TASK, {
        'id': new_id('publish_task'), 'project_id': project_id, 'artifact_id': artifact_id,
        'script_version_id': script_version_id, 'platform_account_id': platform_account_id,
        'idempotency_key': idempotency_key, 'platform_payload': platform_payload,
        'status': 'draft', 'scheduled_for': scheduled_for,
        'platform_work_id': None, 'platform_url': None, 'result_message': None,
        'created_at': created_at or now_ms(),
    })


def set_publish_success(task, platform_work_id, url, raw=None):
    """发布成功的唯一入口：校验并返回成功态任务副本，不改入参。"""
    check_id(platform_work_id, 'platform_work_id')
    if not isinstance(url, str) or not url:
        raise ValueError('发布成功必须带平台作品 URL。')
    result = {**task,
              'status': 'published',
              'platform_work_id': platform_work_id,
              'platform_url': url,
              'result_message': '已发布到平台。'}
    if raw is not None:
        result['raw'] = raw
    return validate_record(KIND_MATRIX_PUBLISH_TASK, result)


def create_platform_work(platform_account_id, platform, platform_video_id, title,
                         source_url='', publish_task_id=None, published_at=None,
                         created_at=None, raw=None, project_id=None,
                         registration_mode=None, verification=None, **metrics):
    return validate_record(KIND_PLATFORM_WORK, {
        'id': new_id('platform_work'), 'platform_account_id': platform_account_id,
        'platform': platform, 'platform_video_id': platform_video_id, 'title': title,
        'source_url': source_url, 'publish_task_id': publish_task_id,
        'published_at': published_at, 'created_at': created_at or now_ms(),
        'project_id': project_id, 'registration_mode': registration_mode,
        'verification': verification,
        'raw': raw or {}, **metrics,
    })


def snapshot_id(metric_key, collected_at):
    """快照 ID 由 (metric_key, collected_at) 派生：不同时间点必然是新记录，不覆盖历史。"""
    safe = re.sub(r'[^0-9A-Za-z_]+', '_', metric_key)
    stamp = re.sub(r'[^0-9A-Za-z]+', '', collected_at)
    return f'metric_{safe}_{stamp}'


def create_metric_snapshot(metric_key, value, unit, collected_at, source,
                           platform_work_id=None, dimensions=None, raw=None):
    return validate_record(KIND_METRIC_SNAPSHOT, {
        'id': snapshot_id(metric_key, collected_at), 'metric_key': metric_key,
        'value': value, 'unit': unit, 'collected_at': collected_at, 'source': source,
        'platform_work_id': platform_work_id, 'dimensions': dimensions or {}, 'raw': raw or {},
    })


def create_platform_comment(platform_work_id, platform, platform_comment_id, author_name, content,
                            source_url='', collected_at=None, parent_comment_id=None,
                            raw=None, **metrics):
    """本人已发布作品的评论（platform_comment）：原文原样，未取得指标为 null。"""
    return validate_record(KIND_PLATFORM_COMMENT, {
        'id': new_id('platform_comment'), 'platform_work_id': platform_work_id,
        'platform': platform, 'platform_comment_id': platform_comment_id,
        'author_name': author_name, 'content': content, 'source_url': source_url,
        'collected_at': collected_at or now_ms(), 'parent_comment_id': parent_comment_id,
        'raw': raw or {}, **metrics,
    })


def create_comment_feedback(bench_comment_id, bench_work_id, feedback_type,
                            note='', created_at=None, platform_comment_id=None,
                            platform_work_id=None):
    """评论反馈记录：bench_comment 链路（bench_comment_id 非空）保持不变；
    本人作品评论链路置 bench_* 为 null，用 platform_comment_id 引用 platform_comment.id。
    两条链路至少引用一条真实评论（REQUIRE_ONE_OF）。"""
    return validate_record(KIND_COMMENT_FEEDBACK, {
        'id': new_id('comment_feedback'), 'bench_comment_id': bench_comment_id,
        'bench_work_id': bench_work_id, 'feedback_type': feedback_type,
        'note': note, 'created_at': created_at or now_ms(),
        'platform_comment_id': platform_comment_id, 'platform_work_id': platform_work_id,
    })


def create_review_insight(title, body, insight_type='data', created_at=None, **refs):
    return validate_record(KIND_REVIEW_INSIGHT, {
        'id': new_id('review_insight'), 'title': title, 'body': body,
        'insight_type': insight_type, 'created_at': created_at or now_ms(),
        'project_id': None, 'bench_work_id': None, 'platform_work_id': None, 'knowledge_id': None,
        **refs,
    })


def create_digital_human_provider(name, provider_key, capabilities,
                                  status='active', session_ref=None, config=None, created_at=None):
    return validate_record(KIND_DIGITAL_HUMAN_PROVIDER, {
        'id': new_id('dh_provider'), 'name': name, 'provider_key': provider_key,
        'status': status, 'capabilities': list(capabilities),
        'session_ref': session_ref, 'config': config or {}, 'created_at': created_at or now_ms(),
    })


def create_digital_human_job(provider_id, script_version_id, asset_id, request,
                             status='pending', result=None, error=None, created_at=None):
    return validate_record(KIND_DIGITAL_HUMAN_JOB, {
        'id': new_id('dh_job'), 'provider_id': provider_id,
        'script_version_id': script_version_id, 'asset_id': asset_id,
        'status': status, 'request': request, 'result': result, 'error': error,
        'created_at': created_at or now_ms(),
    })


def set_digital_human_success(job, result_asset_id, result=None):
    """数字人任务成功出口：成功结果的 result 必须带 result_asset_id。"""
    check_id(result_asset_id, 'result_asset_id')
    merged = dict(result or {})
    merged['result_asset_id'] = result_asset_id
    return validate_record(KIND_DIGITAL_HUMAN_JOB,
                           {**job, 'status': 'succeeded', 'result': merged, 'error': None})


def create_digital_human_asset(provider_id, job_id, asset_kind, uri,
                               duration_ms=None, meta=None, created_at=None):
    return validate_record(KIND_DIGITAL_HUMAN_ASSET, {
        'id': new_id('dh_asset'), 'provider_id': provider_id, 'job_id': job_id,
        'asset_kind': asset_kind, 'uri': uri, 'duration_ms': duration_ms,
        'meta': meta or {}, 'created_at': created_at or now_ms(),
    })
