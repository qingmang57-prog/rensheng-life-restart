"""多平台矩阵发布底座：第一阶段后端测试。

不联网、不启动真实发布：抖音执行器以记录调用的 fake runner 替换，其余平台必须保持
「未接入/待验收」；伪造成功（无平台作品 ID/URL）必须被系统拒绝。
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_ROOT = Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT/('matrix-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import contracts
import matrix_service
import platform_adapters
from db import Store

ROLES = ('hook', 'content', 'anchor', 'quote', 'ending')

# 后台任务等待窗口：默认 45s，可用环境变量覆盖（G 盘 SQLite 落库慢，10s 会假超时）。
WAIT_DEADLINE = float(os.environ.get('MATRIX_TEST_WAIT_DEADLINE', '45'))


def future_ms(hours=1):
    stamp = datetime.now(timezone.utc) + timedelta(hours=hours)
    return stamp.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class FakeWorker:
    """记录调用的假抖音执行器：按命令与标题分派 canned 结果，绝不真发。"""

    def __init__(self):
        self.calls = []
        self.login_result = {'ok': True, 'logged_in': True, 'message': '抖音创作者中心登录成功。'}
        self.submit_result = {'ok': True, 'prepared': True, 'published': False,
                              'needs_manual_publish': True,
                              'upload_url': 'https://creator.douyin.com/creator-micro/content/upload',
                              'message': '资料已就绪（草稿与表单已填好），未点击发布；请本人到创作者中心确认并发布。'}
        self.fail_titles = ()      # 标题含任一子串则 submit 失败
        self.needs_action_titles = ()

    def __call__(self, command, timeout=900):
        self.calls.append(list(command))
        op = command[0]
        title = ''
        if '--title' in command:
            title = command[command.index('--title') + 1]
        if op == 'login':
            return copy.deepcopy(self.login_result)
        if op == 'status':
            return {'ok': True, 'logged_in': True, 'reason': '', 'message': '会话有效。'}
        if op == 'plan':
            return {'ok': True, 'dry_run': True, 'screenshot': 'x.png', 'title': title,
                    'tags': [], 'message': '演练完成：视频已上传为草稿、表单已填好，未发布。'}
        if op == 'submit':
            if any(part in title for part in self.needs_action_titles):
                return {'ok': False, 'needs_action': True, 'message': '发布触发了短信验证，需要本人完成验证。'}
            if any(part in title for part in self.fail_titles):
                return {'ok': False, 'message': '平台拦截：作品审核未通过。'}
            return copy.deepcopy(self.submit_result)
        raise AssertionError('未知命令：' + op)


FAKE = FakeWorker()
SESSION_DIR = TEST_PATH/'sessions'
ARTIFACTS_DIR = TEST_PATH/'artifacts'
SESSION_DIR.mkdir(exist_ok=True)
ARTIFACTS_DIR.mkdir(exist_ok=True)


def fake_ctx(ready=True):
    return {'run_worker': FAKE, 'session_dir': SESSION_DIR,
            'default_session': SESSION_DIR/'douyin-session.json',
            'progress': SESSION_DIR/'progress.json', 'artifacts_dir': ARTIFACTS_DIR,
            'publishing_ready': ready}


matrix_service.init(app.STORE, app.new_job, fake_ctx())


class AdapterUnitTests(unittest.TestCase):
    """适配器层：纯本地校验与未接入语义，不触网。"""

    def test_unimplemented_adapters_raise_not_implemented(self):
        for name in ('kuaishou', 'xiaohongshu', 'shipinhao', 'bilibili'):
            adapter = platform_adapters.get_adapter(name)
            self.assertFalse(adapter.implemented)
            with self.assertRaises(NotImplementedError):
                adapter.login({}, {})
            cap = adapter.capabilities({})
            self.assertFalse(cap['implemented'])
            self.assertFalse(cap['ready'])
            self.assertIn('未接入', cap['message'])

    def test_douyin_validate_content_requires_title_and_video(self):
        adapter = platform_adapters.get_adapter('douyin')
        artifact = {'kind': 'packaged_video', 'path': str(TEST_PATH/'nope.mp4')}
        task = {'platform_payload': {'title': '有标题'}}
        bad = adapter.validate_content(task, artifact, {})
        self.assertFalse(bad['ok'])
        self.assertTrue(any('文件不存在' in i for i in bad['issues']))
        video = TEST_PATH/'clip.mp4'; video.write_bytes(b'fixture')
        artifact = {'kind': 'packaged_video', 'path': str(video)}
        no_title = adapter.validate_content({'platform_payload': {}}, artifact, {})
        self.assertFalse(no_title['ok'])
        self.assertTrue(any('缺少标题' in i for i in no_title['issues']))
        ok = adapter.validate_content(task, artifact, {})
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['raw']['title'], '有标题')

    def test_douyin_submit_without_prepared_flag_is_not_success(self):
        # 铁律：执行器没明确回报「资料已就绪」，不得判准备成功，更不能判发布成功。
        adapter = platform_adapters.get_adapter('douyin')
        video = TEST_PATH/'clip2.mp4'; video.write_bytes(b'fixture')
        task = {'id': 'publish_task_x', 'platform_payload': {'title': '标题'}}
        artifact = {'kind': 'packaged_video', 'path': str(video)}

        class NoIdWorker:
            def __call__(self, command, timeout=900):
                return {'ok': True, 'published': True,
                        'url': 'https://creator.douyin.com/creator-micro/content/manage',
                        'message': '已发布到抖音。'}
        result = adapter.submit(task, artifact, {}, {**fake_ctx(), 'run_worker': NoIdWorker()})
        self.assertFalse(result['prepared'])
        self.assertFalse(result['published'])
        self.assertFalse(result['needs_action'])
        self.assertIn('准备未完成', result['message'])

    def test_douyin_session_ref_rejects_escape(self):
        adapter = platform_adapters.get_adapter('douyin')
        ctx = fake_ctx()
        with self.assertRaises(ValueError):
            adapter.resolve_session({'session_ref': '../../etc/passwd'}, ctx)
        with self.assertRaises(ValueError):
            adapter.resolve_session({'session_ref': '/tmp/14333/secret.json'}, ctx)
        resolved = adapter.resolve_session({'session_ref': 'accounts/second.json'}, ctx)
        self.assertTrue(str(resolved).startswith(str(SESSION_DIR.resolve())))

    def test_forged_published_record_rejected_by_contracts(self):
        # 伪造 published（无平台作品 ID/URL）在契约层即非法。
        task = contracts.create_matrix_publish_task(
            'project_x', 'artifact_x', 'script_x', 'platform_account_x', 'idem-forged',
            {'title': '标题'})
        with self.assertRaises(ValueError):
            contracts.validate_record(contracts.KIND_MATRIX_PUBLISH_TASK,
                                      {**task, 'status': 'published'})

    def test_restart_keeps_task_state_readable(self):
        # 重启持久化：新 Store 打开同一目录，任务状态完整可读。
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            task = contracts.create_matrix_publish_task(
                'project_x', 'artifact_x', 'script_x', 'platform_account_x', 'idem-persist',
                {'title': '标题'})
            task.update({'platform': 'douyin', 'planned': True})
            saved = store.put(contracts.KIND_MATRIX_PUBLISH_TASK, task)
            reopened = Store(d)
            loaded = reopened.get(contracts.KIND_MATRIX_PUBLISH_TASK, saved['id'])
            self.assertEqual(loaded['status'], 'draft')
            self.assertEqual(loaded['idempotency_key'], 'idem-persist')
            self.assertTrue(loaded['planned'])


class ReceiptVerifyUnitTests(unittest.TestCase):
    """回执核验三态：格式错 / 作品不存在（未解析出 ID）/ 正常，纯本地判定。"""

    def test_receipt_three_states(self):
        import receipt_verify
        missing = receipt_verify.parse_post_ref('douyin', '')
        self.assertFalse(missing['ok'])
        self.assertEqual(missing['error_code'], 'link_missing')
        bad = receipt_verify.parse_post_ref(
            'douyin', 'https://creator.douyin.com/creator-micro/content/manage')
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['error_code'], 'link_invalid')
        self.assertIn('链接格式不对', bad['error_message'])
        ok = receipt_verify.parse_post_ref(
            'douyin', 'https://www.douyin.com/video/7388888888888888888')
        self.assertTrue(ok['ok'])
        self.assertEqual(ok['platform_video_id'], '7388888888888888888')
        self.assertEqual(ok['verification'], 'link_parsed')
        other = receipt_verify.parse_post_ref(
            'xiaohongshu', 'https://www.xiaohongshu.com/explore/abc')
        self.assertEqual(other['error_code'], 'platform_unsupported')


class MatrixAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method='GET', body=None, headers=None, expected=200):
        headers = headers or {}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
            headers = {**headers, 'Content-Type': 'application/json'}
        req = urllib.request.Request(self.base+path, body, headers, method=method)
        try:
            res = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as exc:
            res = exc
        with res:
            data = res.read()
            self.assertEqual(res.status, expected, data[:700])
            return json.loads(data) if 'application/json' in res.headers.get('Content-Type', '') else data

    def poll(self, fetch, done, label, deadline=None):
        """退避轮询直到 done 为真；超时把最后一次真实状态写进日志与失败信息。

        deadline 默认取 MATRIX_TEST_WAIT_DEADLINE（45s）：G 盘 SQLite 慢时 10s 窗口
        会假超时，必须能调；轮询从 20ms 退避到 500ms，避免高频请求抢 GIL 拖慢落库。
        超时时打印实际状态，用于区分「等待窗口太短」与「状态真的没改变」。
        """
        limit = WAIT_DEADLINE if deadline is None else float(deadline)
        start = time.monotonic()
        interval = 0.02
        last = None
        while True:
            last = fetch()
            if done(last):
                elapsed = time.monotonic()-start
                if elapsed > 1.0:
                    print(f'[wait] {label} 用时 {elapsed:.2f}s（deadline {limit:g}s）')
                return last
            if time.monotonic()-start >= limit:
                break
            time.sleep(interval)
            interval = min(interval*1.7, 0.5)
        elapsed = time.monotonic()-start
        state = json.dumps(last, ensure_ascii=False, default=str)
        print(f'[wait-timeout] {label} 等待 {elapsed:.2f}s（deadline {limit:g}s）未满足；'
              f'最后状态：{state[:1200]}')
        self.fail(f'{label} 等待 {elapsed:.2f}s 仍未满足条件（deadline={limit:g}s，可用环境变量 '
                  f'MATRIX_TEST_WAIT_DEADLINE 调整）；最后状态：{state[:1200]}')

    def wait_job(self, job_id, want=None, deadline=None):
        current = self.poll(lambda: self.request('/api/jobs/'+job_id),
                            lambda job: job['status'] not in ('queued', 'running'),
                            '后台任务 '+job_id, deadline)
        if want:
            self.assertEqual(current['status'], want,
                             f'后台任务 {job_id} 结束状态为 {current["status"]}，期望 {want}；'
                             f'error={current.get("error")}')
        return current

    def wait_task(self, task_id, predicate, deadline=None):
        return self.poll(lambda: self.request('/api/matrix/tasks/'+task_id), predicate,
                         '矩阵任务 '+task_id, deadline)

    # ---- 基础 ----

    def create_account(self, platform='douyin', display_name='本人抖音号',
                       session_ref='default', capabilities=('video_publish',)):
        return self.request('/api/matrix/accounts', 'POST', {
            'platform': platform, 'display_name': display_name,
            'session_ref': session_ref, 'capabilities': list(capabilities)}, expected=201)

    def make_reviewed_project(self, title='矩阵测试作品'):
        """真实走 API：建项目 → 补五段稿 → 确认定稿 → 挂成片 → 审片。"""
        project = self.request('/api/projects', 'POST',
                               {'title': title, 'source_text': '真实字幕内容，用于矩阵测试。'},
                               expected=201)
        pid = project['id']
        parts = [{'role': role, 'text': '第'+role+'段正文内容，足够真实。'} for role in ROLES]
        self.request('/api/projects/'+pid+'/draft', 'PATCH',
                     {'parts': parts, 'draft_revision': project['draft_revision']})
        self.request('/api/projects/'+pid+'/confirm', 'POST',
                     {'draft_revision': project['draft_revision']+1})
        project = self.request('/api/projects/'+pid)
        version_id = project['confirmed_version_id']
        video = TEST_PATH/('clip-'+pid+'.mp4'); video.write_bytes(b'fixture-video')
        artifact = app.STORE.put('artifact', {'project_id': pid, 'script_version_id': version_id,
            'kind': 'packaged_video', 'path': str(video), 'filename': '成片.mp4',
            'info': {'width': 1080, 'height': 1920, 'duration_ms': 45000}})
        self.request('/api/projects/'+pid+'/review', 'POST', {'artifact_id': artifact['id']})
        return pid, version_id, artifact['id']

    def create_matrix_tasks(self, pid, version_id, artifact_id, targets, key=None, plan_only=True):
        return self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': artifact_id, 'script_version_id': version_id,
            'targets': targets, 'idempotency_key': key or ('idem-'+uuid.uuid4().hex),
            'plan_only': plan_only}, expected=201)

    # ---- 账号 ----

    def test_create_account_success_and_get(self):
        account = self.create_account()
        self.assertEqual(account['platform'], 'douyin')
        self.assertEqual(account['session_ref'], 'default')
        self.assertEqual(account['status'], 'pending')  # 新建账号未登录 = pending
        # 账号字段固定：id/platform/display_name/session_ref/status/capabilities/created_at/updated_at
        self.assertEqual(sorted(account), sorted(('id', 'platform', 'display_name', 'session_ref',
                                                  'status', 'capabilities', 'created_at', 'updated_at')))
        fetched = self.request('/api/matrix/accounts/'+account['id'])
        self.assertEqual(fetched, account)
        listed = self.request('/api/matrix/accounts')
        self.assertEqual(sorted(listed), ['items', 'total'])  # 固定 {items,total}
        self.assertIn(account['id'], {a['id'] for a in listed['items']})
        for item in listed['items']:
            self.assertEqual(sorted(item), sorted(account))

    def test_create_account_rejects_absolute_session_ref(self):
        for bad in ('/tmp/14333/secret.json', '/abs/path.json',
                    '\\\\server\\share.json', 'a/../../b.json', ''):
            with self.assertRaises(AssertionError, msg=repr(bad)):
                self.request('/api/matrix/accounts', 'POST', {
                    'platform': 'douyin', 'display_name': 'x',
                    'session_ref': bad, 'capabilities': ['video_publish']}, expected=201)
        # 会话目录内的相对路径是合法引用（只用于隔离不同账号的登录态文件）。
        for good in ('a/b.json', 'other-user\\secret.json'):
            self.request('/api/matrix/accounts', 'POST', {
                'platform': 'douyin', 'display_name': 'x', 'session_ref': good,
                'capabilities': ['video_publish']}, expected=201)

    def test_create_account_rejects_credentials_and_bad_fields(self):
        self.request('/api/matrix/accounts', 'POST', {
            'platform': 'douyin', 'display_name': 'x', 'session_ref': 'default',
            'capabilities': ['video_publish'], 'cookies': 'sessionid=steal'}, expected=400)
        self.request('/api/matrix/accounts', 'POST', {
            'platform': 'twitch', 'display_name': 'x', 'session_ref': 'default',
            'capabilities': ['video_publish']}, expected=400)
        self.request('/api/matrix/accounts', 'POST', {
            'platform': 'douyin', 'session_ref': 'default',
            'capabilities': ['video_publish']}, expected=400)
        self.request('/api/matrix/accounts', 'POST', {
            'platform': 'douyin', 'display_name': 'x', 'session_ref': 'default',
            'capabilities': []}, expected=400)
        self.request('/api/matrix/accounts/nope', expected=404)

    def test_login_rejected_for_unimplemented_platform(self):
        account = self.create_account(platform='kuaishou', display_name='快手号')
        result = self.request('/api/matrix/accounts/'+account['id']+'/login', 'POST', {},
                              expected=400)
        self.assertIn('未接入', result['error'])

    def test_login_job_for_douyin_updates_account(self):
        account = self.create_account(display_name='扫码登录测试号')
        result = self.request('/api/matrix/accounts/'+account['id']+'/login', 'POST', {}, expected=202)
        # 固定形状 {job_id, status, message}；不提供二维码。
        self.assertEqual(sorted(result), ['job_id', 'message', 'status'])
        self.assertNotIn('qrcode_data_url', result)
        self.assertEqual(result['status'], 'queued')
        self.assertIn('登录任务已开始', result['message'])
        self.wait_job(result['job_id'], 'succeeded')
        fresh = self.request('/api/matrix/accounts/'+account['id'])
        self.assertEqual(fresh['status'], 'connected')
        self.assertTrue(any(c[0] == 'login' for c in FAKE.calls))

    # ---- capabilities ----

    def test_capabilities_reflect_real_state(self):
        caps = self.request('/api/matrix/capabilities')
        self.assertEqual(sorted(caps), ['items'])  # 固定 {items:[...]}
        by_platform = {item['platform']: item for item in caps['items']}
        self.assertEqual(sorted(by_platform), sorted(('douyin', 'kuaishou', 'xiaohongshu',
                                                      'shipinhao', 'bilibili')))
        for item in caps['items']:
            self.assertEqual(sorted(item), sorted(('platform', 'implemented', 'ready',
                                                   'status', 'message')))
            self.assertIn(item['status'], ('connected', 'pending', 'unimplemented'))
        douyin = by_platform['douyin']
        self.assertTrue(douyin['implemented'])
        for name in ('kuaishou', 'xiaohongshu', 'shipinhao', 'bilibili'):
            self.assertFalse(by_platform[name]['implemented'], name)
            self.assertFalse(by_platform[name]['ready'], name)
            self.assertEqual(by_platform[name]['status'], 'unimplemented')
            self.assertIn('未接入', by_platform[name]['message'])
        # ready 跟随执行器真实可用性；status 同步映射 connected/pending，不得伪造。
        matrix_service.init(app.STORE, app.new_job, fake_ctx(ready=False))
        try:
            item = next(x for x in self.request('/api/matrix/capabilities')['items']
                        if x['platform'] == 'douyin')
            self.assertFalse(item['ready'])
            self.assertEqual(item['status'], 'pending')
        finally:
            matrix_service.init(app.STORE, app.new_job, fake_ctx(ready=True))
        item = next(x for x in self.request('/api/matrix/capabilities')['items']
                    if x['platform'] == 'douyin')
        self.assertTrue(item['ready'])
        self.assertEqual(item['status'], 'connected')

    # ---- 创建任务 ----

    def test_create_tasks_success_creates_draft_tasks_and_parent_job(self):
        pid, version_id, artifact_id = self.make_reviewed_project('创建成功')
        account = self.create_account()
        result = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '矩阵标题一', 'tags': ['认知']}}], key='idem-create-ok')
        self.assertFalse(result['reused'])
        self.assertEqual(result['rejected'], [])
        self.assertEqual(len(result['tasks']), 1)
        task = result['tasks'][0]
        self.assertEqual(task['status'], 'draft')
        self.assertEqual(task['project_id'], pid)
        self.assertEqual(task['artifact_id'], artifact_id)
        self.assertEqual(task['script_version_id'], version_id)
        self.assertEqual(task['platform_account_id'], account['id'])
        self.assertEqual(task['platform'], 'douyin')
        self.assertIsNone(task['scheduled_for'])
        self.assertIsNone(task['platform_work_id'])
        self.assertIsNone(task['platform_url'])
        # 列表视图固定字段；platform_payload 在详情接口给出。
        for field in ('id', 'project_id', 'artifact_id', 'script_version_id',
                      'platform_account_id', 'platform', 'status', 'scheduled_for',
                      'platform_work_id', 'platform_url', 'result_message',
                      'created_at', 'updated_at'):
            self.assertIn(field, task)
        detail = self.request('/api/matrix/tasks/'+task['id'])
        self.assertEqual(detail['platform_payload']['title'], '矩阵标题一')
        job = self.wait_job(result['job']['id'], 'succeeded')
        self.assertEqual(job['result']['task_ids'], [task['id']])
        self.assertTrue(job['result']['plan_only'])
        listed = self.request('/api/matrix/tasks?project_id='+pid)
        self.assertEqual(sorted(listed), ['items', 'total'])
        self.assertEqual(listed['total'], 1)

    def test_create_tasks_rejects_bad_project_artifact_version(self):
        pid, version_id, artifact_id = self.make_reviewed_project('校验拒绝')
        account = self.create_account()
        target = {'platform_account_id': account['id'], 'platform_payload': {'title': '标题'}}
        self.request('/api/matrix/tasks', 'POST', {
            'project_id': 'project_missing', 'artifact_id': artifact_id,
            'script_version_id': version_id, 'targets': [target],
            'idempotency_key': 'idem-bad-project'}, expected=404)
        other = app.STORE.put('artifact', {'project_id': 'project_elsewhere',
            'script_version_id': 'script_elsewhere', 'kind': 'packaged_video',
            'path': str(TEST_PATH/'clip-other.mp4')})
        self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': other['id'], 'script_version_id': version_id,
            'targets': [target], 'idempotency_key': 'idem-wrong-artifact'}, expected=400)
        bad_version = app.STORE.put('script_version', {'project_id': pid,
            'parts': [], 'draft_revision': 1, 'confirmed_at': 'x',
            'source_id': 'source_x', 'source_revision': 1, 'source_snapshot': '',
            'confirmation_scope': 'integration_test'})
        self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': artifact_id,
            'script_version_id': bad_version['id'], 'targets': [target],
            'idempotency_key': 'idem-unbound'}, expected=400)

    def test_create_tasks_rejects_unreviewed_artifact(self):
        project = self.request('/api/projects', 'POST',
                               {'title': '未审片作品', 'source_text': '字幕内容。'}, expected=201)
        pid = project['id']
        parts = [{'role': role, 'text': '正文。'} for role in ROLES]
        self.request('/api/projects/'+pid+'/draft', 'PATCH',
                     {'parts': parts, 'draft_revision': project['draft_revision']})
        self.request('/api/projects/'+pid+'/confirm', 'POST',
                     {'draft_revision': project['draft_revision']+1})
        project = self.request('/api/projects/'+pid)
        video = TEST_PATH/('clip-unreviewed-'+pid+'.mp4'); video.write_bytes(b'x')
        artifact = app.STORE.put('artifact', {'project_id': pid,
            'script_version_id': project['confirmed_version_id'], 'kind': 'packaged_video',
            'path': str(video), 'filename': 'a.mp4', 'info': {}})
        account = self.create_account()
        result = self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': artifact['id'],
            'script_version_id': project['confirmed_version_id'],
            'targets': [{'platform_account_id': account['id'],
                         'platform_payload': {'title': '标题'}}],
            'idempotency_key': 'idem-unreviewed'}, expected=409)
        self.assertIn('审片', result['error'])

    def test_create_tasks_is_idempotent(self):
        pid, version_id, artifact_id = self.make_reviewed_project('幂等创建')
        account = self.create_account()
        target = {'platform_account_id': account['id'], 'platform_payload': {'title': '标题'}}
        first = self.create_matrix_tasks(pid, version_id, artifact_id, [target], key='idem-replay')
        again = self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': artifact_id, 'script_version_id': version_id,
            'targets': [target], 'idempotency_key': 'idem-replay'}, expected=201)
        self.assertTrue(again['reused'])
        self.assertEqual([t['id'] for t in again['tasks']], [t['id'] for t in first['tasks']])
        self.assertEqual(len(self.request('/api/matrix/tasks?project_id='+pid)['items']), 1)

    def test_unimplemented_platform_target_rejected_without_blocking_others(self):
        pid, version_id, artifact_id = self.make_reviewed_project('混合平台')
        douyin = self.create_account(display_name='抖音A')
        kuaishou = self.create_account(platform='kuaishou', display_name='快手A')
        result = self.create_matrix_tasks(pid, version_id, artifact_id, [
            {'platform_account_id': douyin['id'], 'platform_payload': {'title': '抖音标题'}},
            {'platform_account_id': kuaishou['id'], 'platform_payload': {'title': '快手标题'}}],
            key='idem-mixed')
        # 未接入平台 target 落库为 rejected 记录，两个 target 都进任务列表。
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['rejected'][0]['platform_account_id'], kuaishou['id'])
        self.assertIn('未接入', result['rejected'][0]['reason'])
        self.assertTrue(result['rejected'][0]['task_id'])
        views = {v['platform_account_id']: v for v in result['tasks']}
        self.assertEqual(views[douyin['id']]['status'], 'draft')
        self.assertEqual(views[kuaishou['id']]['status'], 'rejected')
        self.assertIn('未接入', views[kuaishou['id']]['result_message'])
        listed = self.request('/api/matrix/tasks?project_id='+pid)
        self.assertEqual(listed['total'], 2)
        # rejected 任务不可演练/提交。
        self.request('/api/matrix/tasks/'+views[kuaishou['id']]['id']+'/plan', 'POST', {},
                     expected=409)
        self.request('/api/matrix/tasks/'+views[kuaishou['id']]['id']+'/submit', 'POST',
                     {'confirm': True}, expected=409)

    # ---- plan / submit 防护 ----

    def test_plan_then_submit_prepares_then_manual_result_writes_platform_work(self):
        pid, version_id, artifact_id = self.make_reviewed_project('成功链路')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '成功标题', 'tags': ['认知']}}], key='idem-success')
        task_id = created['tasks'][0]['id']
        job = self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {}, expected=202)
        self.wait_job(job['job_id'], 'succeeded')
        planned = self.request('/api/matrix/tasks/'+task_id)
        # 演练通过后对外状态为 awaiting_confirmation（未发布）。
        self.assertEqual(planned['status'], 'awaiting_confirmation')
        self.assertIn('未发布', planned['result_message'])
        submit = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                              {'confirm': True}, expected=202)
        self.assertEqual(sorted(submit), ['job_id', 'message', 'status'])
        self.wait_job(submit['job_id'], 'succeeded')
        # 只准备资料：任务仍未发布、没有平台作品，等本人到平台亲手发布。
        prepared = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(prepared['status'], 'awaiting_confirmation')
        self.assertTrue(prepared['awaiting_manual_publish'])
        self.assertEqual(prepared['assist_status'], 'prepared')
        self.assertIsNone(prepared['platform_work_id'])
        self.assertIsNone(prepared['platform_url'])
        self.assertEqual([w for w in app.STORE.list(contracts.KIND_PLATFORM_WORK)
                          if w.get('publish_task_id') == task_id], [])
        # 本人在平台发布后回填作品链接：这是进入 published 的唯一出口。
        url = 'https://www.douyin.com/video/7399999999999999999'
        result = self.request('/api/matrix/tasks/'+task_id+'/publish-result', 'POST',
                              {'url': url}, expected=201)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(result['url'], url)
        done = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(done['status'], 'published')
        self.assertFalse(done['awaiting_manual_publish'])
        self.assertEqual(done['platform_work_id'], result['platform_work_id'])
        self.assertEqual(done['platform_url'], url)
        work = app.STORE.get(contracts.KIND_PLATFORM_WORK, done['platform_work_id'])
        self.assertIsNotNone(work)
        self.assertEqual(work['platform_video_id'], '7399999999999999999')
        self.assertEqual(work['platform_account_id'], account['id'])
        self.assertEqual(work['publish_task_id'], task_id)
        self.assertEqual(work['source_url'], url)
        self.assertEqual(work['registration_mode'], 'manual_link')
        self.assertTrue(work['raw'])  # 回填审计信息保留
        self.assertEqual(work['platform'], 'douyin')
        self.assertEqual(work['project_id'], pid)

    def test_publish_result_rejects_bad_url_and_duplicate(self):
        pid, version_id, artifact_id = self.make_reviewed_project('回填校验')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '标题'}}], key='idem-bad-url')
        task_id = created['tasks'][0]['id']
        self.request('/api/matrix/tasks/'+task_id+'/publish-result', 'POST',
                     {'url': ''}, expected=400)
        failed = self.request('/api/matrix/tasks/'+task_id)
        # 失败落库：任务上能看到失败码、人话原因、尝试时间，而不是只在 400 响应里。
        self.assertEqual(failed['receipt_status'], 'failed')
        self.assertEqual(failed['receipt_error_code'], 'link_missing')
        self.assertIn('缺少作品链接', failed['receipt_error_message'])
        self.assertTrue(failed['receipt_attempt_at'])
        self.assertEqual(failed['receipt_attempts'], 1)
        self.request('/api/matrix/tasks/'+task_id+'/publish-result', 'POST',
                     {'url': 'https://creator.douyin.com/creator-micro/content/manage'},
                     expected=400)
        failed = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(failed['receipt_error_code'], 'link_invalid')
        self.assertEqual(failed['receipt_attempts'], 2)
        self.assertEqual(failed['status'], 'draft')  # 失败不改发布状态，任务仍可重新回填
        url = 'https://www.douyin.com/video/7388888888888888888'
        self.request('/api/matrix/tasks/'+task_id+'/publish-result', 'POST',
                     {'url': url}, expected=201)
        accepted = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(accepted['receipt_status'], 'accepted')
        self.assertIsNone(accepted['receipt_error_code'])
        self.assertEqual(accepted['receipt_attempts'], 3)
        # 重复回填不允许，作品不会重复建档（本类共用模块库，按任务过滤）。
        self.request('/api/matrix/tasks/'+task_id+'/publish-result', 'POST',
                     {'url': url}, expected=409)
        works = [w for w in app.STORE.list(contracts.KIND_PLATFORM_WORK)
                 if w.get('publish_task_id') == task_id]
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]['verification'], 'link_parsed')

    def test_plan_fails_when_title_missing(self):
        pid, version_id, artifact_id = self.make_reviewed_project('缺标题')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'], 'platform_payload': {}}], key='idem-no-title')
        task_id = created['tasks'][0]['id']
        job = self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {}, expected=202)
        current = self.wait_job(job['job_id'], 'failed')
        self.assertIn('内容校验未通过', current['error'])
        task = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(task['status'], 'draft')  # 内容校验失败仍是 draft，未进发布态
        self.assertIn('缺少标题', task['result_message'])
        # 内容校验失败不点发布、没有平台作品回登。
        self.assertEqual(len(app.STORE.list(contracts.KIND_PLATFORM_WORK)), 0)

    def test_submit_requires_confirm_true(self):
        pid, version_id, artifact_id = self.make_reviewed_project('确认闸门')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'], 'platform_payload': {'title': '标题'}}],
            key='idem-confirm')
        task_id = created['tasks'][0]['id']
        self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST', {'confirm': False},
                     expected=400)
        self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST', {}, expected=400)
        task = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(task['status'], 'draft')

    def test_submit_rejected_without_plan(self):
        pid, version_id, artifact_id = self.make_reviewed_project('未演练')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'], 'platform_payload': {'标题': None,
                                                                       'title': '标题'}}],
            key='idem-noplan')
        task_id = created['tasks'][0]['id']
        result = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                              {'confirm': True}, expected=409)
        self.assertIn('演练', result['error'])

    def test_submit_rejected_when_artifact_unreviewed_later(self):
        pid, version_id, artifact_id = self.make_reviewed_project('事后撤审')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'], 'platform_payload': {'title': '标题'}}],
            key='idem-unreview-later')
        task_id = created['tasks'][0]['id']
        job = self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {}, expected=202)
        self.wait_job(job['job_id'], 'succeeded')
        project = app.STORE.get('project', pid)
        app.STORE.patch('project', pid, {'reviewed_artifact_id': None, 'stage': 'review'},
                        project['revision'])
        result = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                              {'confirm': True}, expected=409)
        self.assertIn('审片', result['error'])

    def test_submit_fake_published_without_prepared_not_success(self):
        pid, version_id, artifact_id = self.make_reviewed_project('伪造成功')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '标题'}}],
            key='idem-fake-success')
        task_id = created['tasks'][0]['id']
        self.wait_job(self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {},
                                   expected=202)['job_id'], 'succeeded')

        class ManagePageWorker:
            def __call__(self, command, timeout=900):
                return {'ok': True, 'published': True,
                        'url': 'https://creator.douyin.com/creator-micro/content/manage',
                        'message': '已发布到抖音。'}
        matrix_service.init(app.STORE, app.new_job,
                            {**fake_ctx(), 'run_worker': ManagePageWorker()})
        try:
            submit = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                                  {'confirm': True}, expected=202)
            # 执行器只说 published、没有「资料已就绪」：按失败收口并落库，不算发布。
            self.wait_job(submit['job_id'], 'succeeded')
        finally:
            matrix_service.init(app.STORE, app.new_job, fake_ctx())
        task = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['assist_status'], 'failed')
        self.assertIn('准备失败', task['result_message'])
        self.assertIn('准备未完成', task['assist_error'])
        task_works = [w for w in app.STORE.list(contracts.KIND_PLATFORM_WORK)
                      if w.get('publish_task_id') == task_id]
        self.assertEqual(len(task_works), 0)

    def test_scheduled_task_only_reminds_and_never_runs_worker(self):
        pid, version_id, artifact_id = self.make_reviewed_project('定时排队')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '定时标题'}, 'scheduled_for': future_ms(2)}],
            key='idem-scheduled')
        task_id = created['tasks'][0]['id']
        self.wait_job(self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {},
                                   expected=202)['job_id'], 'succeeded')
        calls_before = len(FAKE.calls)
        result = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                              {'confirm': True}, expected=202)
        self.assertIn('不会到点代发', result['message'])
        task = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(task['status'], 'awaiting_confirmation')
        self.assertFalse(task['awaiting_manual_publish'])
        submit_jobs = [j for j in app.STORE.list('job', 1000)
                       if j.get('kind') == 'matrix_submit'
                       and (j.get('inputs') or {}).get('task_id') == task_id]
        self.assertEqual(len(submit_jobs), 0)
        self.assertEqual(len(FAKE.calls), calls_before)  # 到点前执行器零调用

    def test_single_target_failure_does_not_affect_other(self):
        pid, version_id, artifact_id = self.make_reviewed_project('失败隔离')
        account_a = self.create_account(display_name='账号A')
        account_b = self.create_account(display_name='账号B')
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [
            {'platform_account_id': account_a['id'], 'platform_payload': {'title': '会失败的标题'}},
            {'platform_account_id': account_b['id'], 'platform_payload': {'title': '正常标题'}}],
            key='idem-isolation')
        tasks = {t['platform_account_id']: t for t in created['tasks']}
        FAKE.fail_titles = ('会失败',)
        try:
            for task in tasks.values():
                self.wait_job(self.request('/api/matrix/tasks/'+task['id']+'/plan', 'POST', {},
                                           expected=202)['job_id'], 'succeeded')
            for task in tasks.values():
                self.request('/api/matrix/tasks/'+task['id']+'/submit', 'POST',
                             {'confirm': True}, expected=202)
            failed = self.wait_task(tasks[account_a['id']]['id'],
                                    lambda t: t['status'] == 'failed')
            ok = self.wait_task(tasks[account_b['id']]['id'],
                                lambda t: t['awaiting_manual_publish'] is True)
        finally:
            FAKE.fail_titles = ()
        self.assertIn('审核未通过', failed['result_message'])
        stored = app.STORE.get(contracts.KIND_MATRIX_PUBLISH_TASK, failed['id'])
        self.assertTrue(stored['last_raw'])  # 失败原因与原始返回留存（内部字段）
        self.assertEqual(stored['assist_status'], 'failed')
        self.assertIn('审核未通过', stored['assist_error'])
        self.assertTrue(ok['platform_work_id'] is None)  # 只准备，未发布
        url = 'https://www.douyin.com/video/7399999999999999999'
        self.request('/api/matrix/tasks/'+ok['id']+'/publish-result', 'POST',
                     {'url': url}, expected=201)
        self.assertEqual(self.request('/api/matrix/tasks/'+ok['id'])['platform_url'], url)

    def test_needs_action_not_auto_resolved(self):
        pid, version_id, artifact_id = self.make_reviewed_project('短信验证')
        account = self.create_account()
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [{
            'platform_account_id': account['id'], 'platform_payload': {'title': '触发验证标题'}}],
            key='idem-needs-action')
        task_id = created['tasks'][0]['id']
        FAKE.needs_action_titles = ('触发验证',)
        try:
            self.wait_job(self.request('/api/matrix/tasks/'+task_id+'/plan', 'POST', {},
                                       expected=202)['job_id'], 'succeeded')
            submit = self.request('/api/matrix/tasks/'+task_id+'/submit', 'POST',
                                  {'confirm': True}, expected=202)
            current = self.wait_job(submit['job_id'], 'needs_action')
        finally:
            FAKE.needs_action_titles = ()
        self.assertIn('短信验证', str(current.get('result')))
        task = self.request('/api/matrix/tasks/'+task_id)
        self.assertEqual(task['status'], 'needs_action')
        self.assertNotIn(task['status'], ('published', 'failed'))
        self.assertIn('不会自动重试', task['result_message'])
        # 不自动解：系统不会自发再创建 submit job。
        time.sleep(0.3)
        submit_jobs = [j for j in app.STORE.list('job', 1000)
                       if j.get('kind') == 'matrix_submit'
                       and (j.get('inputs') or {}).get('task_id') == task_id]
        self.assertEqual(len(submit_jobs), 1)

    def test_mixed_batch_integration_contract(self):
        """联调收敛集成验证：混合批次（已实现 + 未实现）走通固定形状与全部闸门。"""
        pid, version_id, artifact_id = self.make_reviewed_project('联调收敛')
        douyin = self.create_account(display_name='联调抖音号')
        kuaishou = self.create_account(platform='kuaishou', display_name='联调快手号')
        created = self.create_matrix_tasks(pid, version_id, artifact_id, [
            {'platform_account_id': douyin['id'], 'platform_payload': {'title': '联调标题'}},
            {'platform_account_id': kuaishou['id'], 'platform_payload': {'title': '联调快手'}}],
            key='idem-integration')
        # 未实现平台 rejected（落库可见），不影响已实现平台。
        self.assertEqual(len(created['tasks']), 2)
        self.assertEqual(len(created['rejected']), 1)
        self.assertEqual(created['rejected'][0]['platform'], 'kuaishou')
        views = {v['platform_account_id']: v for v in created['tasks']}
        douyin_task = views[douyin['id']]
        kuaishou_task = views[kuaishou['id']]
        self.assertEqual(douyin_task['status'], 'draft')
        self.assertEqual(kuaishou_task['status'], 'rejected')
        self.request('/api/matrix/tasks/'+kuaishou_task['id']+'/plan', 'POST', {}, expected=409)
        # 幂等回放：同一批任务，reused:true。
        replay = self.request('/api/matrix/tasks', 'POST', {
            'project_id': pid, 'artifact_id': artifact_id, 'script_version_id': version_id,
            'targets': [{'platform_account_id': douyin['id'],
                         'platform_payload': {'title': '联调标题'}}],
            'idempotency_key': 'idem-integration'}, expected=201)
        self.assertTrue(replay['reused'])
        self.assertEqual(len(replay['tasks']), 2)
        # 已实现任务可演练，通过后进入 awaiting_confirmation。
        plan = self.request('/api/matrix/tasks/'+douyin_task['id']+'/plan', 'POST', {},
                            expected=202)
        self.assertIn('job_id', plan)
        self.wait_job(plan['job_id'], 'succeeded')
        self.assertEqual(self.request('/api/matrix/tasks/'+douyin_task['id'])['status'],
                         'awaiting_confirmation')
        # confirm:false 拒绝（400），状态不被推进。
        self.request('/api/matrix/tasks/'+douyin_task['id']+'/submit', 'POST',
                     {'confirm': False}, expected=400)
        self.assertEqual(self.request('/api/matrix/tasks/'+douyin_task['id'])['status'],
                         'awaiting_confirmation')
        # 伪造成功（执行器只说 published、没有资料已就绪）不算发布：按失败落库。
        class ManagePageWorker:
            def __call__(self, command, timeout=900):
                return {'ok': True, 'published': True,
                        'url': 'https://creator.douyin.com/creator-micro/content/manage',
                        'message': '已发布到抖音。'}
        matrix_service.init(app.STORE, app.new_job,
                            {**fake_ctx(), 'run_worker': ManagePageWorker()})
        try:
            submit = self.request('/api/matrix/tasks/'+douyin_task['id']+'/submit', 'POST',
                                  {'confirm': True}, expected=202)
            self.assertEqual(sorted(submit), ['job_id', 'message', 'status'])
            self.wait_job(submit['job_id'], 'succeeded')
        finally:
            matrix_service.init(app.STORE, app.new_job, fake_ctx())
        final = self.request('/api/matrix/tasks/'+douyin_task['id'])
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['assist_status'], 'failed')
        self.assertIsNone(final['platform_work_id'])
        self.assertIsNone(final['platform_url'])
        works = [w for w in app.STORE.list(contracts.KIND_PLATFORM_WORK)
                 if w.get('publish_task_id') == douyin_task['id']]
        self.assertEqual(len(works), 0)
        # 任务列表固定形状与状态枚举。
        listed = self.request('/api/matrix/tasks?project_id='+pid)
        self.assertEqual(listed['total'], 2)
        allowed = ('draft', 'queued', 'running', 'planned', 'awaiting_confirmation',
                   'published', 'failed', 'needs_action', 'rejected', 'cancelled')
        for item in listed['items']:
            for field in ('id', 'project_id', 'artifact_id', 'script_version_id',
                          'platform_account_id', 'platform', 'status', 'scheduled_for',
                          'platform_work_id', 'platform_url', 'result_message',
                          'created_at', 'updated_at'):
                self.assertIn(field, item)
            self.assertIn(item['status'], allowed)

    def test_matrix_routes_guard_and_shape(self):
        self.request('/api/matrix/tasks', 'GET')  # 空列表合法
        self.request('/api/matrix/tasks/nope', expected=404)
        self.request('/api/matrix/accounts/nope/login', 'POST', {}, expected=404)
        self.request('/api/matrix/capabilities', 'POST', {}, expected=404)
        self.request('/api/matrix/accounts', 'POST', {'url': 'x'}, expected=400)
        # 跨站写入仍被拒绝
        self.request('/api/matrix/accounts', 'POST', {
            'platform': 'douyin', 'display_name': 'x', 'session_ref': 'default',
            'capabilities': ['video_publish']}, headers={'Origin': 'http://evil.example'},
            expected=403)


if __name__ == '__main__':
    unittest.main()
