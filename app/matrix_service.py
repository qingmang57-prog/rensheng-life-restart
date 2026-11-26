"""多平台矩阵发布底座：第一阶段服务层。

职责（路由由 app.py 挂载到 /api/matrix/*，本模块只做业务）：
- 平台能力状态：已实现平台给真实 ready/message；未实现平台永远「未接入/待验收」。
- 平台账号建档：session_ref 只允许占位标识或相对路径；凭据明文一律拒绝。
- 矩阵发布任务：校验项目/已审片成片/定稿绑定；每 target 独立 matrix_publish_task（draft），
  父 job 汇总；未接入平台 target 直接 rejected，不影响其他平台。
- 幂等：同一 idempotency_key 重复创建返回既有任务集合，不重复建档。
- 两段式发布：plan 演练（不点发布）→ submit 只准备资料（绝不代发）→ 本人在平台发布后
  用 register_publish_result 回填链接，成功只能经 contracts.set_publish_success 并回登
  platform_work；needs_action 不自动解；单 target 失败不影响其他 target。
"""
import re

import contracts
import platform_adapters
import receipt_verify
import collect_schedule
from db import Conflict

STORE = None
_new_job = None
_CTX = None

PLATFORMS = tuple(platform_adapters.ADAPTERS)
MAX_TARGETS = 20
MAX_ATTEMPTS = 3

_ABS_PATH = contracts._ABS_PATH


def init(store, new_job, ctx=None):
    global STORE, _new_job, _CTX
    STORE = store
    _new_job = new_job
    _CTX = dict(ctx or {})


def _require(kind, key):
    item = STORE.get(kind, key)
    if item is None:
        raise KeyError('没有找到该记录。')
    return item


def _adapter_for(account):
    adapter = platform_adapters.get_adapter(account['platform'])
    if not adapter.implemented:
        raise ValueError(f'平台 {account["platform"]} 未接入/待验收：该操作不可用。')
    return adapter


# ---------------------------------------------------------------------------
# 平台能力
# ---------------------------------------------------------------------------

def _capability_status(cap):
    """对外统一状态：connected=已接入/可用，pending=待验收，unimplemented=未接入。"""
    if not cap.get('implemented'):
        return 'unimplemented'
    return 'connected' if cap.get('ready') else 'pending'


def platform_capabilities():
    """固定形状 {items:[{platform, implemented, ready, status, message}]}。

    status 枚举统一：connected=已接入/可用，pending=待验收，unimplemented=未接入；
    implemented/ready 布尔值保留。未接入平台绝不报 ready。"""
    items = []
    for name, adapter in platform_adapters.ADAPTERS.items():
        cap = adapter.capabilities(_CTX)
        items.append({'platform': name, 'implemented': bool(cap.get('implemented')),
                      'ready': bool(cap.get('ready')), 'status': _capability_status(cap),
                      'message': cap.get('message') or ''})
    return {'items': items}


# ---------------------------------------------------------------------------
# 平台账号
# ---------------------------------------------------------------------------

# 对外账号状态即 login_state 语义（单一字段，不做两套）：未接入平台的账号
# 在读取时统一映射为 'unimplemented'（见 _account_view）。
_ACCOUNT_STATUSES = ('connected', 'pending', 'needs_action', 'disconnected')
_ACCOUNT_VIEW_FIELDS = ('id', 'platform', 'display_name', 'session_ref', 'status',
                        'capabilities', 'created_at', 'updated_at')


def _account_view(account):
    platform = account.get('platform') or ''
    adapter = platform_adapters.ADAPTERS.get(platform)
    status = account.get('status') or 'pending'
    if adapter is None or not adapter.implemented:
        status = 'unimplemented'
    view = {field: account.get(field) for field in _ACCOUNT_VIEW_FIELDS}
    view['status'] = status
    return view


# ---------------------------------------------------------------------------
# 平台账号
# ---------------------------------------------------------------------------

_FORBIDDEN_CREDENTIAL_FIELDS = ('cookies', 'password', 'passwd', 'token', 'secret',
                                'credential', 'storage_state', 'auth')


def _check_session_ref(ref):
    """session_ref 只允许相对路径或占位标识；禁止绝对路径与盘符、禁止越界。"""
    if _ABS_PATH.search(ref):
        raise ValueError('session_ref 不能是绝对路径；请使用占位标识（如 default）或相对路径。')
    if ref.startswith(('/', '\\')):
        raise ValueError('session_ref 不能以斜杠开头；请使用相对路径。')
    if any(part in ('..', '') for part in re.split(r'[\\/]+', ref)):
        raise ValueError('session_ref 不能包含空段或 .. 越界片段。')


def create_account(payload):
    """平台账号建档；凭据明文字段一律拒绝入库。"""
    data = payload or {}
    for field in _FORBIDDEN_CREDENTIAL_FIELDS:
        if data.get(field):
            raise ValueError(f'字段 {field} 疑似凭据明文；账号只保存 session_ref 引用，不存凭据。')
    platform = str(data.get('platform') or '').strip()
    if platform not in PLATFORMS:
        raise ValueError(f'platform 必须是 {" / ".join(PLATFORMS)} 之一。')
    display_name = str(data.get('display_name') or '').strip()
    if not display_name:
        raise ValueError('display_name 不能为空。')
    session_ref = str(data.get('session_ref') or '').strip()
    if not session_ref:
        raise ValueError('session_ref 不能为空；占位标识 default 表示默认会话。')
    _check_session_ref(session_ref)
    capabilities = data.get('capabilities')
    if (not isinstance(capabilities, list) or not capabilities
            or not all(isinstance(x, str) and x for x in capabilities)):
        raise ValueError('capabilities 必须是非空字符串数组。')
    status = data.get('status', 'pending')
    if status not in _ACCOUNT_STATUSES:
        raise ValueError(f'账号 status 必须是 {list(_ACCOUNT_STATUSES)} 之一（login_state 语义）。')
    record = contracts.create_platform_account(
        platform, display_name, session_ref, capabilities, status=status)
    return _account_view(STORE.put(contracts.KIND_PLATFORM_ACCOUNT, record))


def list_accounts():
    """固定形状 {items,total}；每条为固定字段的账号视图。"""
    items = [_account_view(a) for a in STORE.list(contracts.KIND_PLATFORM_ACCOUNT, 5000)]
    return {'items': items, 'total': len(items)}


def get_account(account_id):
    return _account_view(_require(contracts.KIND_PLATFORM_ACCOUNT, account_id))


def start_login(account_id):
    """仅已实现适配器可发起登录 job；未实现平台返回明确「未接入/待验收」。

    固定返回 {job_id, status, message}；不提供二维码轮询，不模拟二维码。"""
    account = _require(contracts.KIND_PLATFORM_ACCOUNT, account_id)
    adapter = _adapter_for(account)

    def work(job_id):
        STORE.patch('job', job_id, {'stage': f'等待扫码登录：{account["display_name"]}（{adapter.display_name}）'})
        result = adapter.login(account, _CTX)
        fresh = _require(contracts.KIND_PLATFORM_ACCOUNT, account['id'])
        STORE.patch(contracts.KIND_PLATFORM_ACCOUNT, account['id'], {
            'status': 'connected' if result.get('logged_in') else 'needs_action',
            'raw': result.get('raw') or {}}, fresh['revision'])
        return {'requires_action': not result.get('logged_in'),
                'stage': '已登录，可以发布演练' if result.get('logged_in') else '需要本人扫码登录',
                'result': result}

    job = _new_job(account['id'], 'matrix_login',
                   {'account_id': account['id'], 'platform': account['platform']}, work)
    return {'job_id': job['id'], 'status': job['status'],
            'message': '登录任务已开始，请查看后台任务/平台页面。'}


# ---------------------------------------------------------------------------
# 矩阵发布任务
# ---------------------------------------------------------------------------

# 对外任务状态（以任务列表为准，服务层显式映射；publish_failed 等内部/旧名不得泄露）：
# draft / queued / running / planned / awaiting_confirmation / published / failed
# / needs_action / rejected / cancelled
_TASK_VIEW_FIELDS = ('id', 'project_id', 'artifact_id', 'script_version_id',
                     'platform_account_id', 'platform', 'status', 'scheduled_for',
                     'platform_work_id', 'platform_url', 'result_message',
                     'created_at', 'updated_at')


def _active_plan_task_ids():
    """演练 job 进行中的任务：对外显示 planned。"""
    active = set()
    for job in STORE.list('job', 1000):
        if job.get('kind') == 'matrix_plan' and job.get('status') in ('queued', 'running'):
            task_id = (job.get('inputs') or {}).get('task_id')
            if task_id:
                active.add(task_id)
    return active


def _task_view(task, active_plan_ids=()):
    """存储状态 → 对外状态映射。存储沿用 contracts 发布状态机；rejected/needs_action/
    planned/awaiting_confirmation 为服务层按标志与 job 活动推导的对外状态。"""
    internal = task.get('status')
    if task.get('rejected'):
        external = 'rejected'
    elif task.get('assist_status') == 'failed' and internal in ('draft', 'queued', 'running'):
        external = 'failed'                      # 资料准备失败：对外如实显示，不隐藏在 draft 里
    elif task.get('needs_action') and internal in ('draft', 'queued', 'running'):
        external = 'needs_action'
    elif internal == 'draft':
        if task.get('planned'):
            external = 'awaiting_confirmation'   # 演练已通过，等本人确认提交
        elif task['id'] in active_plan_ids:
            external = 'planned'                 # 演练进行中
        else:
            external = 'draft'
    else:
        external = internal  # queued/running/published/failed/cancelled 同名直通
    view = {field: task.get(field) for field in _TASK_VIEW_FIELDS}
    view['status'] = external
    return view


def _check_str(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f'{name} 必须是非空字符串。')
    return value


def list_tasks(project_id=None):
    """固定形状 {items,total}；每条为固定字段任务视图（状态为对外枚举）。"""
    active = _active_plan_task_ids()
    items = [_task_view(t, active) for t in STORE.list(contracts.KIND_MATRIX_PUBLISH_TASK, 10000)]
    if project_id:
        items = [t for t in items if t.get('project_id') == project_id]
    return {'items': items, 'total': len(items)}


def get_task(task_id):
    task = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
    return {**_task_view(task, _active_plan_task_ids()),
            'platform_payload': task.get('platform_payload') or {},
            # 准备发布资料的真实状态（只准备、不代发）：prepared/failed/needs_action
            'assist_status': task.get('assist_status'),
            'assist_error': task.get('assist_error'),
            'awaiting_manual_publish': bool(task.get('awaiting_manual_publish')),
            'attempt': int(task.get('attempt') or 0),
            # 回执核验可见性（P0-3）：失败也要能被看见，而不是只在后台日志里。
            'receipt_status': task.get('receipt_status'),
            'receipt_error_code': task.get('receipt_error_code'),
            'receipt_error_message': task.get('receipt_error_message'),
            'receipt_attempt_at': task.get('receipt_attempt_at'),
            'receipt_attempts': int(task.get('receipt_attempts') or 0)}


def _load_publish_context(project_id, artifact_id, script_version_id):
    """项目/成片/定稿版本三项校验：归属、成片类型、版本绑定。"""
    project = _require('project', _check_str(project_id, 'project_id'))
    artifact = _require('artifact', _check_str(artifact_id, 'artifact_id'))
    if artifact.get('project_id') != project['id']:
        raise ValueError('artifact 不属于该项目。')
    if artifact.get('kind') != 'packaged_video':
        raise ValueError('待发布产物必须是包装成片（packaged_video）。')
    version = _require('script_version', _check_str(script_version_id, 'script_version_id'))
    if version.get('project_id') != project['id']:
        raise ValueError('script_version 不属于该项目。')
    if artifact.get('script_version_id') != version['id']:
        raise ValueError('稿本版本与成片未绑定：artifact.script_version_id 必须等于 script_version_id。')
    return project, artifact, version


def _check_reviewed(project, artifact_id):
    """已审片铁律：沿用现有 ready_to_export + reviewed_artifact_id 闸门。"""
    if project.get('reviewed_artifact_id') != artifact_id or project.get('stage') != 'ready_to_export':
        raise Conflict('成片尚未审片确认（stage != ready_to_export）；不能进入发布。')


def _find_batch_job(idempotency_key):
    for job in STORE.list('job', 1000):
        if job.get('kind') == 'matrix_publish' \
                and (job.get('inputs') or {}).get('idempotency_key') == idempotency_key:
            return job
    return None


def create_tasks(payload):
    """创建矩阵任务。默认 plan_only 语义：只建档（draft），绝不自动发布。

    同一 idempotency_key 重复创建返回既有任务集合；未接入平台 target 进 rejected，
    不影响其他平台。父 job 只做汇总，不执行任何发布动作。
    """
    data = payload or {}
    project_id = _check_str(data.get('project_id'), 'project_id')
    artifact_id = _check_str(data.get('artifact_id'), 'artifact_id')
    script_version_id = _check_str(data.get('script_version_id'), 'script_version_id')
    idempotency_key = _check_str(data.get('idempotency_key'), 'idempotency_key')
    targets = data.get('targets')
    if not isinstance(targets, list) or not targets:
        raise ValueError('targets 必须是非空数组。')
    if len(targets) > MAX_TARGETS:
        raise ValueError(f'单次最多 {MAX_TARGETS} 个 target。')
    plan_only = data.get('plan_only', True) is not False  # 默认即演练语义，创建永不发布

    # 幂等回放：同键直接返回既有任务集合（含 rejected 记录），不重复建档。
    prefix = idempotency_key + ':'
    existing = [t for t in STORE.list(contracts.KIND_MATRIX_PUBLISH_TASK, 10000)
                if str(t.get('idempotency_key') or '').startswith(prefix)]
    if existing:
        existing.sort(key=lambda t: (str(t.get('created_at') or ''), str(t.get('id') or '')))
        active = _active_plan_task_ids()
        return {'tasks': [_task_view(t, active) for t in existing], 'rejected': [],
                'reused': True, 'job': _find_batch_job(idempotency_key)}

    project, artifact, version = _load_publish_context(project_id, artifact_id, script_version_id)
    _check_reviewed(project, artifact_id)

    tasks = []
    rejected = []
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError('每个 target 必须是对象。')
        account = _require(contracts.KIND_PLATFORM_ACCOUNT,
                           _check_str(target.get('platform_account_id'), 'platform_account_id'))
        platform_payload = target.get('platform_payload')
        if not isinstance(platform_payload, dict):
            raise ValueError('platform_payload 必须是对象；各平台专用字段原样保留。')
        scheduled_for = target.get('scheduled_for')
        if scheduled_for is not None:
            contracts.check_timestamp(scheduled_for, 'scheduled_for')
        adapter = platform_adapters.ADAPTERS.get(account['platform'])
        task = contracts.create_matrix_publish_task(
            project['id'], artifact['id'], version['id'], account['id'],
            f'{idempotency_key}:{index}:{account["id"]}', platform_payload,
            scheduled_for=scheduled_for)
        task.update({'platform': account['platform'], 'attempt': 0, 'max_attempts': MAX_ATTEMPTS,
                     'planned': False, 'plan_raw': None, 'last_raw': None, 'needs_action': False})
        if adapter is None or not adapter.implemented:
            # 未接入平台 target：落库为 rejected 记录（对外状态 rejected），不影响其他平台。
            reason = f'平台 {account["platform"]} 未接入/待验收：target 已拒绝，不影响其他平台。'
            task.update({'rejected': True, 'result_message': reason})
            saved = STORE.put(contracts.KIND_MATRIX_PUBLISH_TASK, task)
            tasks.append(saved)
            rejected.append({'index': index, 'platform_account_id': account['id'],
                             'platform': account['platform'], 'task_id': saved['id'],
                             'reason': reason})
            continue
        task.update({'rejected': False})
        tasks.append(STORE.put(contracts.KIND_MATRIX_PUBLISH_TASK, task))

    task_ids = [t['id'] for t in tasks]

    def work(job_id):
        # 父 job 只汇总：建档本身不执行任何平台动作。
        return {'stage': '矩阵任务已建档（默认不发布，需演练后本人确认）',
                'task_ids': task_ids, 'rejected': rejected, 'plan_only': plan_only}

    job = _new_job(project['id'], 'matrix_publish', {
        'idempotency_key': idempotency_key, 'project_id': project['id'],
        'artifact_id': artifact['id'], 'script_version_id': version['id'],
        'task_ids': task_ids, 'plan_only': plan_only}, work)
    active = _active_plan_task_ids()
    return {'tasks': [_task_view(t, active) for t in tasks], 'rejected': rejected,
            'reused': False, 'job': job}


def plan_task(task_id):
    """发布演练（不点发布）。任务保持 draft；演练通过进入 awaiting_confirmation。"""
    task = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
    if task.get('rejected'):
        raise Conflict('该 target 已被拒绝（平台未接入/待验收），不能演练。')
    if task.get('status') != 'draft':
        raise Conflict(f'只有 draft 状态任务可以演练（当前 {task.get("status")}）。')
    account = _require(contracts.KIND_PLATFORM_ACCOUNT, task['platform_account_id'])
    adapter = _adapter_for(account)
    artifact = _require('artifact', task['artifact_id'])

    def work(job_id):
        STORE.patch('job', job_id, {'stage': f'发布演练（不点发布）：{adapter.display_name}'})
        fresh = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
        # validate_content 在本地先过一遍：缺标题/成片等平台必填项在此阶段失败。
        content = adapter.validate_content(fresh, artifact, account)
        if not content.get('ok'):
            message = '内容校验未通过：' + '；'.join(content.get('issues') or [])
            STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                        {'planned': False, 'result_message': message,
                         'last_raw': content.get('raw')}, fresh['revision'])
            raise ValueError(message)
        result = adapter.publish_plan(fresh, artifact, account, _CTX)
        fresh = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
        if result.get('ok'):
            STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                        {'planned': True, 'plan_raw': result.get('raw'),
                         'result_message': '演练完成，未发布；等待本人确认提交。'}, fresh['revision'])
        else:
            message = '演练未通过：' + str(result.get('message') or '平台未接受演练。')
            STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                        {'planned': False, 'plan_raw': result.get('raw'),
                         'result_message': message}, fresh['revision'])
            raise ValueError(message)
        return {'stage': '演练完成，未点击发布', 'result': result}

    job = _new_job(task['project_id'], 'matrix_plan', {'task_id': task_id}, work)
    return {'job_id': job['id'], 'status': job['status'],
            'message': '演练任务已开始，不点击发布；请查看后台任务进度。'}


def _transition(task, target, **extra):
    """带乐观锁的状态迁移；并发双发在存储层抛 Conflict。"""
    return STORE.put(contracts.KIND_MATRIX_PUBLISH_TASK,
                     contracts.transition(task, 'publish', target, **extra),
                     expected=task['revision'])


def _validatable(task):
    """存储层会用 db.now() 覆盖 created_at（微秒、带时区偏移），与契约毫秒格式不同；
    重新校验前把时间戳规范回契约格式（put 时该字段本就不会入库，无副作用）。"""
    record = dict(task)
    try:
        contracts.check_timestamp(record.get('created_at'), 'created_at')
    except ValueError:
        record['created_at'] = contracts.now_ms()
    return record


def submit_task(task_id, body):
    """准备发布资料：confirm:true + 已审片 + 已演练；只上传草稿并填表，绝不点击发布。

    红线：发布动作必须由本人在平台确认。任务停在 draft 并标记 awaiting_manual_publish，
    本人在平台发布后把链接回填给 register_publish_result，才置为 published。
    """
    data = body or {}
    if data.get('confirm') is not True:
        raise ValueError('准备发布资料必须本人确认：请求体需要 {"confirm": true}。')
    task = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
    if task.get('rejected'):
        raise Conflict('该 target 已被拒绝（平台未接入/待验收），不能提交。')
    if task.get('status') == 'published':
        raise Conflict('任务已发布，不能重复提交。')
    if task.get('status') == 'cancelled':
        raise Conflict('任务已取消，不能提交。')
    if task.get('status') == 'running' and not task.get('needs_action'):
        raise Conflict('任务正在执行中，不能重复提交。')
    if task.get('status') not in ('draft', 'queued', 'failed') and not task.get('needs_action'):
        raise Conflict(f'当前状态 {task.get("status")} 不能提交。')
    project = _require('project', task['project_id'])
    _check_reviewed(project, task['artifact_id'])  # 409：成片未审片
    if not task.get('planned'):
        raise Conflict('请先完成发布演练（POST /plan）；未演练不得准备发布资料。')
    scheduled_for = task.get('scheduled_for')
    if scheduled_for and scheduled_for > contracts.now_ms():
        return {'status': _task_view(task)['status'],
                'message': '定时任务只做提醒，不会到点代发；到点后请本人手动准备并在平台发布。'}
    account = _require(contracts.KIND_PLATFORM_ACCOUNT, task['platform_account_id'])
    adapter = _adapter_for(account)
    artifact = _require('artifact', task['artifact_id'])

    def work(job_id):
        STORE.patch('job', job_id, {
            'stage': f'正在准备发布资料：{adapter.display_name} / {account["display_name"]}（不会代发）'})
        fresh = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
        attempt = int(fresh.get('attempt') or 0) + 1
        STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                    {'attempt': attempt, 'assist_status': 'running', 'assist_error': None},
                    fresh['revision'])
        result = adapter.submit(fresh, artifact, account, _CTX)
        fresh = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
        if result.get('prepared'):
            STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id, {
                'awaiting_manual_publish': True,
                'assist_status': 'prepared',
                'assist_error': None,
                'assist_ready_at': contracts.now_ms(),
                'needs_action': False,
                'result_message': str(result.get('message')
                                      or '发布资料已就绪，请本人到平台发布后回填链接。'),
                'last_raw': result.get('raw')}, fresh['revision'])
            return {'stage': '资料已就绪，未代发；请本人到平台发布后回填链接',
                    'upload_url': result.get('upload_url') or ''}
        if result.get('needs_action'):
            # needs_action 不自动解：任务保持待处理，只等本人处理后手动重提。
            STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                        {'needs_action': True, 'assist_status': 'needs_action',
                         'assist_error': str(result.get('message') or '平台要求本人处理。'),
                         'result_message': str(result.get('message') or '平台要求本人处理。')
                                           + '（任务保持待处理，不会自动重试。）',
                         'last_raw': result.get('raw')}, fresh['revision'])
            return {'requires_action': True,
                    'stage': '平台要求本人处理（如短信验证）；任务保持待处理，不自动重试。',
                    'result': result}
        message = str(result.get('message') or '发布资料准备失败。')
        # 失败必须落库并给出可读原因：assist_status=failed 会让任务列表直接显示 failed。
        STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                    {'assist_status': 'failed', 'assist_error': message,
                     'result_message': '准备失败：' + message,
                     'last_raw': result.get('raw')}, fresh['revision'])
        return {'stage': '准备失败：' + message, 'result': result}

    # new_job 按 (project_id, kind, inputs) 去重：同任务重复准备复用同一 job，防双跑。
    job = _new_job(task['project_id'], 'matrix_submit',
                   {'task_id': task_id, 'confirm': True, 'assist': True,
                    'artifact_revision': artifact['revision']}, work)
    return {'job_id': job['id'], 'status': job['status'],
            'message': '正在准备发布资料（不会代发）；完成后请本人到平台发布并回填链接。'}


def register_publish_result(task_id, body):
    """本人在平台发布后回填作品链接：这是任务进入 published 的唯一人工出口。

    回执核验（P0-3）：链接先经 receipt_verify.parse_post_ref。
    - 通过：解析出平台作品 ID，platform_work.verification='link_parsed'；
    - 失败：不建 platform_work，把失败码/文案/尝试时间落到任务 receipt_* 字段再抛 400，
      用户下次回填能看到上次为什么没核上，不必翻后台任务日志。
    """
    data = body or {}
    url = str(data.get('url') or '').strip()
    task = _require(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)
    if task.get('status') == 'published':
        raise Conflict('该任务已回填过发布结果，不能重复回填。')
    if task.get('status') == 'cancelled':
        raise Conflict('任务已取消，不能回填发布结果。')
    account = _require(contracts.KIND_PLATFORM_ACCOUNT, task['platform_account_id'])
    project = _require('project', task['project_id'])
    checked = receipt_verify.parse_post_ref(account['platform'], url)
    if not checked['ok']:
        _record_receipt_failure(task, checked)
        raise ValueError(checked['error_message'])
    video_id = checked['platform_video_id']
    payload = task.get('platform_payload') or {}
    title = str(payload.get('title') or '').strip() or project['title']
    work_record = contracts.create_platform_work(
        account['id'], account['platform'], video_id, title=title[:160], source_url=url,
        publish_task_id=task_id, published_at=contracts.now_ms(), project_id=task['project_id'],
        registration_mode='manual_link', verification='link_parsed',
        raw={'registered_at': contracts.now_ms(), 'receipt_check': checked})
    saved_work = STORE.put(contracts.KIND_PLATFORM_WORK, work_record)
    # P0-6：发布回填成功后登记首次采集提醒（只提醒，不代发）。
    collect_schedule.upsert_after_publish(STORE, saved_work)
    done = contracts.set_publish_success(
        _validatable(task), saved_work['id'], url, raw={'registered_by': 'manual_link'})
    STORE.put(contracts.KIND_MATRIX_PUBLISH_TASK,
              {**done, 'awaiting_manual_publish': False,
               'receipt_status': 'accepted',
               'receipt_error_code': None, 'receipt_error_message': None,
               'receipt_attempt_at': checked['checked_at'],
               'receipt_attempts': int(task.get('receipt_attempts') or 0)+1},
              expected=task['revision'])
    return {'status': 'published', 'platform_work_id': saved_work['id'], 'url': url,
            'verification': 'link_parsed', 'platform_video_id': video_id}


def _record_receipt_failure(task, checked):
    """回执失败落库：任务上留最近一次失败码/可读原因/尝试时间，供详情接口直接展示。"""
    STORE.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task['id'], {
        'receipt_status': 'failed',
        'receipt_error_code': checked['error_code'],
        'receipt_error_message': checked['error_message'],
        'receipt_attempt_at': checked['checked_at'],
        'receipt_attempts': int(task.get('receipt_attempts') or 0)+1,
    }, task['revision'])
