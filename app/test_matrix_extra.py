"""B5 车道：矩阵发布链路 + 复盘回流 离线补充验收（服务层，不联网、不真发、不碰用户库）。

与既有测试的分工：
- test_matrix.py / test_feedback.py 走 HTTP 路由层（较慢，覆盖路由/状态码/跨站防护）；
- 本文件直接调 matrix_service / feedback_service / feedback_collect，用临时 SQLite
  （IP_DATA_DIR 不参与），把「账号 → 任务 → 演练 → 本人确认 → 定时排队 → 回登 → 复盘」
  整条状态机串起来，并补上既有用例未覆盖的：定时到点放行、失败重试、needs_action
  后人工重提、已发布不可重提、桩平台 target 全链路拒绝、六类评论分类、快照三段历史、
  跨批次评论去重、洞察→选题立项后「模型推断/待验证」标记不丢。

不导入 app.py（app.py 由 B3 单点集成），也不启动 HTTP 服务；抖音执行器一律用
FakeWorker 记录调用，其余平台必须保持「未接入」。
"""
import copy
import json
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import bench_service
import contracts
import creation_service
import feedback_collect
import feedback_service
import matrix_service
import platform_adapters
from db import Conflict, Store

ROOT = Path(__file__).resolve().parent
# 临时库放系统临时目录：G: 盘上每次 SQLite 提交要数十秒（实测），会让本组用例假性卡住。
# 用例只碰临时目录，不写用户数据目录。
RUNTIME = Path(tempfile.gettempdir()) / 'b5-ip-acceptance'
RUNTIME.mkdir(parents=True, exist_ok=True)

VIDEO_ID = '7511111111111111111'
VIDEO_URL = 'https://www.douyin.com/video/' + VIDEO_ID
TS1 = '2026-10-31T10:00:00.000Z'
TS2 = '2026-10-31T11:00:00.000Z'
TS3 = '2026-10-31T12:00:00.000Z'
ALLOWED_TASK_STATES = {'draft', 'queued', 'running', 'planned', 'awaiting_confirmation',
                       'published', 'failed', 'needs_action', 'rejected', 'cancelled'}
SIX = ('question', 'painpoint', 'objection', 'praise', 'sequel', 'other')


def stamp(hours=0):
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class FakeWorker:
    """记录调用的假抖音执行器：只返回预先设定的结果，绝不真发、绝不联网。"""

    def __init__(self):
        self.calls = []
        self.login_result = {'ok': True, 'logged_in': True, 'message': '抖音创作者中心登录成功。'}
        self.submit_result = {'ok': True, 'prepared': True, 'published': False,
                              'needs_manual_publish': True,
                              'upload_url': 'https://creator.douyin.com/creator-micro/content/upload',
                              'message': '资料已就绪（草稿与表单已填好），未点击发布。'}
        self.fail_titles = ()
        self.needs_action_titles = ()

    def submit_calls(self):
        return [call for call in self.calls if call[0] == 'submit']

    def __call__(self, command, timeout=900):
        self.calls.append(list(command))
        op = command[0]
        title = command[command.index('--title') + 1] if '--title' in command else ''
        if op == 'login':
            return copy.deepcopy(self.login_result)
        if op == 'status':
            return {'ok': True, 'logged_in': True, 'reason': '', 'message': '会话有效。'}
        if op == 'plan':
            return {'ok': True, 'dry_run': True, 'screenshot': 'plan.png', 'title': title,
                    'tags': [], 'message': '演练完成：视频已上传为草稿、表单已填好，未发布。'}
        if op == 'submit':
            if any(part in title for part in self.needs_action_titles):
                return {'ok': False, 'needs_action': True,
                        'message': '发布触发短信验证，需要本人完成。'}
            if any(part in title for part in self.fail_titles):
                return {'ok': False, 'message': '平台拦截：作品审核未通过。'}
            return copy.deepcopy(self.submit_result)
        raise AssertionError('未知执行器命令：' + op)


class SyncJobs:
    """与 app.new_job 同形状的同步任务运行器：job 记录 + 立即执行 work。

    生产环境 runner 在后台线程执行；这里同步执行以便断言最终状态，job 记录字段
    （id/kind/inputs/status/stage/result/error）与 app.py 保持一致。
    """

    def __init__(self, store):
        self.store = store
        self.created = []

    def __call__(self, project_id, kind, inputs, work):
        job = self.store.put('job', {'id': 'job_' + uuid.uuid4().hex[:16],
                                     'project_id': project_id, 'kind': kind,
                                     'status': 'queued', 'stage': '排队', 'inputs': inputs,
                                     'error': None, 'result': None})
        try:
            self.store.patch('job', job['id'], {'status': 'running', 'stage': '处理中'})
            result = work(job['id'])
            attention = bool(isinstance(result, dict) and result.get('requires_action'))
            self.store.patch('job', job['id'], {
                'status': 'needs_action' if attention else 'succeeded',
                'stage': (result or {}).get('stage') if isinstance(result, dict) else '已完成',
                'result': result})
        except Exception as exc:  # 与 app.new_job 一致：失败写进 job.error，不向外炸测试进程
            self.store.patch('job', job['id'], {'status': 'failed', 'error': str(exc)[:1800]})
        saved = self.store.get('job', job['id'])
        self.created.append(saved)
        return saved


def make_raw(collected_at=TS1, metrics=None, comments=None, video_id=VIDEO_ID):
    """真实采集 JSON 形状（= DouyinWorkCollector 输出 / import_raw 消费格式）。"""
    return {'ok': True, 'source_url': 'https://www.douyin.com/video/' + video_id,
            'collected_at': collected_at, 'platform_video_id': video_id,
            'metrics': metrics if metrics is not None else
            {'play': 12000, 'like': 1500, 'comment': 32, 'share': 10, 'collect': 80},
            'metrics_raw': {'statistics': {'play_count': 12000, 'digg_count': 1500,
                                           'comment_count': 32, 'share_count': 10,
                                           'collect_count': 80}},
            'metrics_note': None,
            'comments': comments if comments is not None else [
                {'platform_comment_id': 'pc-b5-1', 'author_name': '粉丝甲',
                 'content': '干货满满，直接照做', 'like_count': 30, 'reply_count': 1,
                 'parent_comment_id': None},
                {'platform_comment_id': 'pc-b5-2', 'author_name': '粉丝乙',
                 'content': '求更新，等续集', 'like_count': None, 'reply_count': 0,
                 'parent_comment_id': None}],
            'comments_error': None}


class MatrixCase(unittest.TestCase):
    """每个用例一套独立临时库 + 独立 job 运行器，互不干扰。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='b5-', dir=str(RUNTIME)))
        self.store = Store(self.tmp / 'data')
        self.jobs = SyncJobs(self.store)
        self.fake = FakeWorker()
        (self.tmp / 'sessions').mkdir()
        (self.tmp / 'artifacts').mkdir()
        matrix_service.init(self.store, self.jobs, self.ctx(ready=True))
        feedback_service.init(self.store, self.jobs)
        # 洞察 → 选题 → 立项 会走到 bench/creation 两个服务：给它们同一套临时库。
        bench_service.init(self.store, self.jobs)
        creation_service.init(self.store, self.jobs, lambda parts: parts)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, ready=True):
        return {'run_worker': self.fake, 'session_dir': self.tmp / 'sessions',
                'default_session': self.tmp / 'sessions' / 'douyin-session.json',
                'progress': self.tmp / 'sessions' / 'progress.json',
                'artifacts_dir': self.tmp / 'artifacts', 'publishing_ready': ready}

    # ---- 夹具 ----

    def make_reviewed(self, title='B5 矩阵验收作品'):
        """造一条已审片项目 + 绑定稿本版本的包装成片（与真实闸门字段一致）。"""
        project = self.store.put('project', {'title': title, 'source_id': 'source_b5',
                                             'tags': [], 'stage': 'draft'})
        version = self.store.put('script_version', {
            'project_id': project['id'],
            'parts': [{'role': r, 'text': r + '段正文'} for r in
                      ('hook', 'content', 'anchor', 'quote', 'ending')]})
        video = self.tmp / ('clip-' + uuid.uuid4().hex[:8] + '.mp4')
        video.write_bytes(b'fixture-video')
        artifact = self.store.put('artifact', {'project_id': project['id'],
                                              'script_version_id': version['id'],
                                              'kind': 'packaged_video', 'path': str(video),
                                              'filename': '成片.mp4',
                                              'info': {'width': 1080, 'height': 1920,
                                                       'duration_ms': 45000}})
        project = self.store.patch('project', project['id'], {
            'stage': 'ready_to_export', 'reviewed_artifact_id': artifact['id']})
        return project, artifact, version

    def make_account(self, platform='douyin', display_name='本人抖音号'):
        return matrix_service.create_account({
            'platform': platform, 'display_name': display_name,
            'session_ref': 'default', 'capabilities': ['video_publish']})

    def create_tasks(self, project, artifact, version, targets, key=None):
        return matrix_service.create_tasks({
            'project_id': project['id'], 'artifact_id': artifact['id'],
            'script_version_id': version['id'], 'targets': targets,
            'idempotency_key': key or ('b5-' + uuid.uuid4().hex)})

    def task_of(self, task_id):
        return self.store.get(contracts.KIND_MATRIX_PUBLISH_TASK, task_id)

    def works(self):
        return self.store.list(contracts.KIND_PLATFORM_WORK, 5000)

    def plan_and_submit(self, project, artifact, version, title='B5 验收标题'):
        """账号已登录的前提下：建档 → 演练 → 本人确认准备资料（不代发），返回 (task_id, account_id)。"""
        account = self.make_account()
        matrix_service.start_login(account['id'])
        batch = self.create_tasks(project, artifact, version, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': title, 'desc': 'B5 离线验收，不真实发布。',
                                 'tags': ['验收']}}])
        task_id = batch['tasks'][0]['id']
        matrix_service.plan_task(task_id)
        matrix_service.submit_task(task_id, {'confirm': True})
        return task_id, account['id']

    def publish_manually(self, task_id, url=VIDEO_URL):
        """本人在平台发布后回填链接：任务进入 published 的唯一出口。"""
        return matrix_service.register_publish_result(task_id, {'url': url})


class MatrixChainTests(MatrixCase):
    """发布链路状态机：账号 → 任务 → 演练 → 本人确认 → 回登。"""

    def test_full_chain_login_plan_confirm_publish_and_feedback(self):
        project, artifact, version = self.make_reviewed()
        account = self.make_account()
        self.assertEqual(account['status'], 'pending')  # 未登录前只能是 pending
        matrix_service.start_login(account['id'])
        self.assertEqual(matrix_service.get_account(account['id'])['status'], 'connected')
        caps = {i['platform']: i for i in matrix_service.platform_capabilities()['items']}
        self.assertEqual(caps['douyin']['status'], 'connected')

        batch = self.create_tasks(project, artifact, version, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': 'B5 验收标题', 'desc': '离线验收', 'tags': ['验收']}}])
        task_id = batch['tasks'][0]['id']
        self.assertEqual(batch['job']['kind'], 'matrix_publish')
        self.assertEqual(batch['tasks'][0]['status'], 'draft')  # 建档绝不自动发布
        self.assertEqual(self.works(), [])

        matrix_service.plan_task(task_id)
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'awaiting_confirmation')
        self.assertEqual(self.fake.submit_calls(), [])  # 演练不点发布

        matrix_service.submit_task(task_id, {'confirm': True})
        prepared = matrix_service.get_task(task_id)
        # 只准备资料：没有点发布、没有平台作品，等本人亲手发布。
        self.assertEqual(prepared['status'], 'awaiting_confirmation')
        self.assertTrue(prepared['awaiting_manual_publish'])
        self.assertEqual(prepared['assist_status'], 'prepared')
        self.assertIsNone(prepared['platform_work_id'])
        self.assertEqual(self.works(), [])
        self.assertEqual(len(self.fake.submit_calls()), 1)

        matrix_service.register_publish_result(task_id, {'url': VIDEO_URL})
        task = matrix_service.get_task(task_id)
        self.assertEqual(task['status'], 'published')
        self.assertEqual(task['platform_url'], VIDEO_URL)
        self.assertTrue(task['platform_work_id'])
        works = self.works()
        self.assertEqual(len(works), 1)
        self.assertEqual(works[0]['platform_video_id'], VIDEO_ID)
        self.assertEqual(works[0]['registration_mode'], 'manual_link')

        # 回登的作品能直接进入复盘：采集 → 快照/评论 → 总览
        collected = feedback_service.collect({'project_id': project['id'], 'raw': make_raw()})
        self.assertEqual(collected['job']['status'], 'succeeded')
        listed = feedback_service.list_works(project['id'])
        self.assertEqual(listed['total'], 1)
        overview = feedback_service.overview(project['id'])
        self.assertEqual(overview['platform_work_count'], 1)
        self.assertEqual(len(overview['latest_snapshots']), 5)  # play/like/comment/share/collect
        self.assertEqual(overview['comments_count'], 2)

    def test_capability_inventory_separates_real_adapter_from_stubs(self):
        items = {i['platform']: i for i in matrix_service.platform_capabilities()['items']}
        self.assertEqual(sorted(items), ['bilibili', 'douyin', 'kuaishou',
                                         'shipinhao', 'xiaohongshu'])
        self.assertTrue(items['douyin']['implemented'])
        self.assertEqual(items['douyin']['status'], 'connected')
        self.assertIsInstance(platform_adapters.get_adapter('douyin'),
                              platform_adapters.DouyinAdapter)
        for name in ('kuaishou', 'xiaohongshu', 'shipinhao', 'bilibili'):
            self.assertFalse(items[name]['implemented'], name)
            self.assertFalse(items[name]['ready'], name)
            self.assertEqual(items[name]['status'], 'unimplemented', name)
            self.assertIn('未接入', items[name]['message'])
            self.assertIsInstance(platform_adapters.get_adapter(name),
                                  platform_adapters.UnimplementedAdapter)
        # 执行器未就绪时抖音降级为待验收（pending），不谎报可用
        matrix_service.init(self.store, self.jobs, self.ctx(ready=False))
        douyin = {i['platform']: i for i in matrix_service.platform_capabilities()['items']}['douyin']
        self.assertEqual(douyin['status'], 'pending')
        self.assertIn('未就绪', douyin['message'])

    def test_scheduled_task_only_reminds_never_auto_runs(self):
        project, artifact, version = self.make_reviewed()
        account = self.make_account()
        matrix_service.start_login(account['id'])
        later = stamp(hours=2)
        batch = self.create_tasks(project, artifact, version, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '定时验收'}, 'scheduled_for': later}])
        task_id = batch['tasks'][0]['id']
        matrix_service.plan_task(task_id)
        queued = matrix_service.submit_task(task_id, {'confirm': True})
        self.assertIn('不会到点代发', queued['message'])
        self.assertEqual(self.fake.submit_calls(), [])  # 到点前执行器零调用
        self.assertEqual(self.works(), [])
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'awaiting_confirmation')
        # 到点：把计划时间改到过去，再由本人确认准备，本人发布后回填链接
        self.store.patch(contracts.KIND_MATRIX_PUBLISH_TASK, task_id,
                         {'scheduled_for': stamp(hours=-1)})
        matrix_service.submit_task(task_id, {'confirm': True})
        self.assertEqual(len(self.fake.submit_calls()), 1)
        self.assertTrue(matrix_service.get_task(task_id)['awaiting_manual_publish'])
        self.assertEqual(self.works(), [])
        self.publish_manually(task_id)
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'published')

    def test_published_task_cannot_be_resubmitted_nor_duplicate_work(self):
        project, artifact, version = self.make_reviewed()
        task_id, _ = self.plan_and_submit(project, artifact, version)
        self.publish_manually(task_id)
        self.assertEqual(len(self.works()), 1)
        with self.assertRaises(Conflict):
            matrix_service.submit_task(task_id, {'confirm': True})
        with self.assertRaises(Conflict):
            self.publish_manually(task_id)
        self.assertEqual(len(self.works()), 1)
        self.assertEqual(len(self.fake.submit_calls()), 1)
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'published')

    def test_failed_prepare_retry_then_publish_counts_attempt(self):
        project, artifact, version = self.make_reviewed()
        self.fake.fail_titles = ('必失败',)
        task_id, _ = self.plan_and_submit(project, artifact, version, title='必失败标题')
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'failed')
        self.assertEqual(self.works(), [])
        self.assertEqual(self.task_of(task_id)['attempt'], 1)
        self.assertIn('审核未通过', self.task_of(task_id)['assist_error'])
        # 本人修好问题后重提：失败 → 重新准备成功，尝试次数累加
        self.fake.fail_titles = ()
        matrix_service.submit_task(task_id, {'confirm': True})
        task = matrix_service.get_task(task_id)
        self.assertEqual(task['status'], 'awaiting_confirmation')
        self.assertTrue(task['awaiting_manual_publish'])
        self.assertEqual(self.task_of(task_id)['attempt'], 2)
        self.publish_manually(task_id)
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'published')
        self.assertEqual(len(self.works()), 1)

    def test_needs_action_waits_for_user_and_never_auto_retries(self):
        project, artifact, version = self.make_reviewed()
        self.fake.needs_action_titles = ('需验证',)
        task_id, _ = self.plan_and_submit(project, artifact, version, title='需验证标题')
        task = matrix_service.get_task(task_id)
        self.assertEqual(task['status'], 'needs_action')  # 对外状态显式暴露
        self.assertTrue(task['result_message'])
        self.assertIn('不会自动重试', task['result_message'])
        self.assertEqual(len(self.fake.submit_calls()), 1)  # 没有后台自动重试
        # 本人处理完平台验证后手工重提
        self.fake.needs_action_titles = ()
        matrix_service.submit_task(task_id, {'confirm': True})
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'awaiting_confirmation')
        self.assertEqual(len(self.fake.submit_calls()), 2)
        self.publish_manually(task_id)
        self.assertEqual(matrix_service.get_task(task_id)['status'], 'published')

    def test_stub_platform_target_is_rejected_and_cannot_plan_or_submit(self):
        project, artifact, version = self.make_reviewed()
        account = self.make_account(platform='kuaishou', display_name='快手号（未接入）')
        batch = self.create_tasks(project, artifact, version, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '快手占位'}}])
        task = batch['tasks'][0]
        self.assertEqual(task['status'], 'rejected')
        self.assertEqual(len(batch['rejected']), 1)
        self.assertIn('未接入', batch['rejected'][0]['reason'])
        self.assertEqual(matrix_service.get_account(account['id'])['status'], 'unimplemented')
        with self.assertRaises(Conflict):
            matrix_service.plan_task(task['id'])
        with self.assertRaises(Conflict):
            matrix_service.submit_task(task['id'], {'confirm': True})
        self.assertEqual(self.works(), [])

    def test_publish_without_prepared_flag_is_never_treated_as_success(self):
        project, artifact, version = self.make_reviewed()
        self.fake.submit_result = {'ok': True, 'published': True,
                                   'url': 'https://www.douyin.com/', 'message': '已发布'}
        task_id, _ = self.plan_and_submit(project, artifact, version)
        task = matrix_service.get_task(task_id)
        self.assertNotEqual(task['status'], 'published')
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(self.works(), [])
        self.assertIn('准备失败', task['result_message'])
        self.assertIn('准备未完成', task['assist_error'])

    def test_exposed_task_states_stay_inside_documented_enum(self):
        project, artifact, version = self.make_reviewed()
        account = self.make_account()
        matrix_service.start_login(account['id'])
        batch = self.create_tasks(project, artifact, version, [{
            'platform_account_id': account['id'],
            'platform_payload': {'title': '状态枚举验收'}}])
        task_id = batch['tasks'][0]['id']
        seen = []
        for step in ('draft', 'plan', 'submit', 'publish-result'):
            if step == 'plan':
                matrix_service.plan_task(task_id)
            elif step == 'submit':
                matrix_service.submit_task(task_id, {'confirm': True})
            elif step == 'publish-result':
                self.publish_manually(task_id)
            listed = matrix_service.list_tasks(project_id=project['id'])
            self.assertEqual(sorted(listed), ['items', 'total'])
            state = listed['items'][0]['status']
            self.assertIn(state, ALLOWED_TASK_STATES)
            seen.append(state)
        self.assertEqual(seen, ['draft', 'awaiting_confirmation', 'awaiting_confirmation',
                                'published'])

    def test_publish_state_machine_rejects_illegal_transitions(self):
        for current, target in (('published', 'draft'), ('published', 'queued'),
                                ('draft', 'published'), ('published', 'running')):
            with self.assertRaises(ValueError, msg=f'{current}->{target}'):
                contracts.check_transition('publish', current, target)
        self.assertEqual(contracts.check_transition('publish', 'draft', 'queued'), 'queued')
        self.assertEqual(contracts.check_transition('publish', 'failed', 'queued'), 'queued')


class FeedbackLoopTests(MatrixCase):
    """复盘回流：快照只增不覆盖、评论去重、六类分类、洞察→选题立项。"""

    def published_work(self, project_title='复盘验收作品'):
        project, artifact, version = self.make_reviewed(project_title)
        task_id, _ = self.plan_and_submit(project, artifact, version)
        self.publish_manually(task_id)
        return project, matrix_service.get_task(task_id)['platform_work_id']

    # ---- 快照 ----

    def test_snapshot_history_is_append_only_across_three_collections(self):
        project, work_id = self.published_work()
        batches = [(TS1, {'play': 100, 'like': 10, 'comment': 1, 'share': 1, 'collect': 1}),
                   (TS2, {'play': 200, 'like': 20, 'comment': 2, 'share': 2, 'collect': 2}),
                   (TS3, {'play': 300, 'like': 30, 'comment': 3, 'share': 3, 'collect': 3})]
        for moment, metrics in batches:
            result = feedback_collect.import_raw(self.store, make_raw(moment, metrics), work_id)
            self.assertEqual(result['snapshots_created'], 5)
        snapshots = feedback_service.list_snapshots(work_id)['items']
        self.assertEqual(len(snapshots), 15)  # 3 次采集 × 5 指标，历史全部保留
        by_key = {}
        for snapshot in snapshots:
            by_key.setdefault(snapshot['metric_key'], []).append(snapshot)
        self.assertEqual(sorted(by_key), ['douyin.collect', 'douyin.comment', 'douyin.like',
                                          'douyin.play', 'douyin.share'])
        for key, rows in by_key.items():
            self.assertEqual([r['collected_at'] for r in rows], [TS1, TS2, TS3], key)
            expected = [batch[1][key.split('.', 1)[1]] for batch in batches]
            self.assertEqual([r['value'] for r in rows], expected, key)  # 无覆盖/无错序
        # 同一采集时刻重放：ID 由 (metric_key, collected_at) 派生，不产生重复记录
        replay = feedback_collect.import_raw(self.store, make_raw(TS3, batches[2][1]), work_id)
        self.assertEqual(replay['snapshots_created'], 5)
        self.assertEqual(len(feedback_service.list_snapshots(work_id)['items']), 15)
        # 总览只取每指标最新一条
        overview = feedback_service.overview(project['id'])
        self.assertEqual(overview['latest_snapshot_at'], TS3)
        self.assertEqual(sorted(s['value'] for s in overview['latest_snapshots']),
                         [3, 3, 3, 30, 300])
        # 未取得的指标（null）不生成快照，不用 0 冒充
        nulls = feedback_collect.import_raw(self.store, make_raw(
            TS3, {'play': None, 'like': None, 'comment': None, 'share': None, 'collect': None}),
            work_id)
        self.assertEqual(nulls['snapshots_created'], 0)

    # ---- 评论 ----

    def test_comment_dedup_across_batches_keeps_original_text(self):
        project, work_id = self.published_work()
        first = feedback_collect.import_raw(self.store, make_raw(TS1), work_id)
        self.assertEqual(first['comments_created'], 2)
        replay = feedback_collect.import_raw(self.store, make_raw(TS1), work_id)
        self.assertEqual(replay['comments_created'], 0)  # 同批重放不重复入库
        extra = make_raw(TS2, comments=[
            {'platform_comment_id': 'pc-b5-1', 'author_name': '粉丝甲', 'content': '改了文案？',
             'like_count': 30, 'reply_count': 1, 'parent_comment_id': None},
            {'platform_comment_id': 'pc-b5-3', 'author_name': '粉丝丙',
             'content': '第三条新评论', 'like_count': 3, 'reply_count': 0,
             'parent_comment_id': 'pc-b5-1'},
            {'platform_comment_id': 'pc-b5-4', 'author_name': '粉丝丁',
             'content': '第四条新评论', 'like_count': None, 'reply_count': 0,
             'parent_comment_id': None}])
        second = feedback_collect.import_raw(self.store, extra, work_id)
        self.assertEqual(second['comments_created'], 2)
        comments = feedback_service.list_comments({'platform_work_id': [work_id],
                                                   'page_size': ['100']})
        self.assertEqual(comments['total'], 4)
        ids = [c['platform_comment_id'] for c in comments['items']]
        self.assertEqual(len(ids), len(set(ids)))
        kept = next(c for c in comments['items'] if c['platform_comment_id'] == 'pc-b5-1')
        self.assertEqual(kept['content'], '干货满满，直接照做')  # 去重保留首次原文，不被覆盖

    def _classify_six(self, work_id):
        # 模型只能引用已入库评论的真实记录 ID（内部 id），不是平台评论 ID。
        items = feedback_service.list_comments({'platform_work_id': [work_id],
                                                'page_size': ['100']})['items']
        by_platform = {c['platform_comment_id']: c['id'] for c in items}
        answer = {'classifications': [
            {'comment_id': by_platform['c-' + cat], 'category': cat, 'note': cat + ' 依据'}
            for cat in SIX]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer) as llm:
            result = feedback_service.classify_comments(work_id)
        return result, llm

    def test_six_categories_persist_markers_and_keep_comment_text(self):
        project, work_id = self.published_work()
        feedback_collect.import_raw(self.store, make_raw(TS1, comments=[
            {'platform_comment_id': 'c-' + cat, 'author_name': '粉丝-' + cat,
             'content': '六类原文-' + cat, 'like_count': 1, 'reply_count': 0,
             'parent_comment_id': None} for cat in SIX]), work_id)
        before = {c['platform_comment_id']: c['content'] for c in
                  feedback_service.list_comments({'platform_work_id': [work_id],
                                                  'page_size': ['100']})['items']}
        result, llm = self._classify_six(work_id)
        self.assertEqual(sorted(result['counts']), sorted(SIX))
        self.assertEqual(set(result['counts'].values()), {1})
        self.assertEqual(len(result['items']), 6)
        payload = llm.call_args[0][1]
        self.assertEqual(len(payload['comments']), 6)  # 只把已采评论交给模型
        after = {c['platform_comment_id']: c['content'] for c in
                 feedback_service.list_comments({'platform_work_id': [work_id],
                                                 'page_size': ['100']})['items']}
        self.assertEqual(before, after)  # 分类不改写评论原文
        for comment in result['items']:
            self.assertIn(comment['category'], SIX)
        feedbacks = [f for f in self.store.list(contracts.KIND_COMMENT_FEEDBACK, 5000)
                     if f.get('platform_work_id') == work_id]
        self.assertEqual(len(feedbacks), 6)
        for record in feedbacks:
            self.assertEqual(record['markers'], ['模型推断', '待验证'])
            self.assertEqual(record['validation'], 'unverified')
            self.assertEqual(record['created_by'], 'model_assisted')
            self.assertEqual(record['feedback_type'], 'none')

    def test_classify_rejects_unknown_category_and_empty_comments(self):
        project, work_id = self.published_work()
        with self.assertRaises(ValueError):
            feedback_service.classify_comments(work_id)  # 还没有评论，不许凭空分类
        feedback_collect.import_raw(self.store, make_raw(TS1), work_id)
        bad = {'classifications': [{'comment_id': 'pc-b5-1', 'category': 'complaint',
                                    'note': '非法类目'}]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=bad):
            with self.assertRaises(ValueError):
                feedback_service.classify_comments(work_id)
        self.assertEqual([f for f in self.store.list(contracts.KIND_COMMENT_FEEDBACK, 5000)
                          if f.get('platform_work_id') == work_id], [])  # 非法分类零入库

    # ---- 洞察 → 选题 ----

    def _insight_answer(self, evidence_ids):
        return {'title': '开头结构互动观察', 'body': '开头先给结论的作品互动更集中，属相关性观察。',
                'insight_type': 'data',
                'findings': [{'claim': '开头直接给结论的作品评论更多', 'evidence_ids': evidence_ids}]}

    def test_insight_to_topic_keeps_model_inference_markers(self):
        project, work_id = self.published_work()
        feedback_collect.import_raw(self.store, make_raw(TS1, comments=[
            {'platform_comment_id': 'c-' + cat, 'author_name': '粉丝-' + cat,
             'content': '六类原文-' + cat, 'like_count': 1, 'reply_count': 0,
             'parent_comment_id': None} for cat in SIX]), work_id)
        self._classify_six(work_id)
        feedbacks = [f for f in self.store.list(contracts.KIND_COMMENT_FEEDBACK, 5000)
                     if f.get('platform_work_id') == work_id]
        snapshots = [s for s in self.store.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                     if s.get('platform_work_id') == work_id]
        evidence = [f['id'] for f in feedbacks[:2]] + [snapshots[0]['id']]
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._insight_answer(evidence)):
            insight = feedback_service.create_insight(project['id'])['item']
        stored = self.store.get(contracts.KIND_REVIEW_INSIGHT, insight['id'])
        self.assertEqual(stored['markers'], ['模型推断', '待验证'])
        self.assertEqual(stored['validation'], 'unverified')
        self.assertEqual(stored['status'], 'candidate')
        self.assertEqual(sorted(stored['evidence_ids']), sorted(evidence))
        self.assertEqual(stored['created_by'], 'model_assisted')
        # 列表视图（GET insights 用的同一函数）也要保留标记与证据
        view = feedback_service.list_insights(project['id'])['items'][0]
        self.assertEqual(view['markers'], ['模型推断', '待验证'])
        self.assertEqual(view['evidence_count'], len(evidence))
        self.assertEqual(view['platform_work_ids'], [work_id])
        self.assertEqual(view['platform_work_urls'], [VIDEO_URL])

        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value={
                    'title': '开头先给结论，评论会更多吗',
                    'angle': '对比三条开头结构不同的作品',
                    'thesis': '假设：开头直接给结论能提高评论率（待验证）'}) as llm:
            started = feedback_service.start_topic(insight['id'])
        self.assertFalse(started['reused'])
        topic = self.store.get(contracts.KIND_TOPIC, started['topic_id'])
        self.assertEqual(topic['markers'], ['模型推断', '待验证'])
        self.assertEqual(topic['validation'], 'unverified')
        self.assertEqual(topic['created_by'], 'model_assisted')
        self.assertEqual(topic['origin'], 'feedback')
        self.assertEqual(topic['review_insight_id'], insight['id'])
        self.assertEqual(sorted(topic['evidence_ids']), sorted(evidence))
        self.assertEqual(topic['source_project_id'], project['id'])  # 来源项目不被误当立项项目
        self.assertEqual((topic.get('data') or {}).get('project_id'), started['project_id'])
        self.assertTrue(started['project_id'].startswith('project_'))
        new_project = self.store.get('project', started['project_id'])
        self.assertEqual(new_project['title'], topic['title'])
        self.assertEqual(new_project['stage'], 'draft')
        # 洞察状态不因开题而变成「已验证」
        self.assertEqual(self.store.get(contracts.KIND_REVIEW_INSIGHT, insight['id'])['status'],
                         'candidate')
        # 同一洞察重复开题：复用，不重复立项、不丢标记
        again = feedback_service.start_topic(insight['id'])
        self.assertTrue(again['reused'])
        self.assertEqual(again['project_id'], started['project_id'])
        topics = [t for t in self.store.list(contracts.KIND_TOPIC, 5000)
                  if t.get('review_insight_id') == insight['id']]
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]['markers'], ['模型推断', '待验证'])

    def test_insight_rejects_verified_claim_and_forged_evidence(self):
        project, work_id = self.published_work()
        feedback_collect.import_raw(self.store, make_raw(TS1), work_id)
        feedbacks = [f for f in self.store.list(contracts.KIND_COMMENT_FEEDBACK, 5000)
                     if f.get('platform_work_id') == work_id]
        self.assertEqual(feedbacks, [])  # 未分类：洞察只能靠快照，下面只测拒绝分支
        snapshots = [s for s in self.store.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                     if s.get('platform_work_id') == work_id]
        forged = self._insight_answer(['metric_snapshot_nope'])
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=forged):
            with self.assertRaises(ValueError):
                feedback_service.create_insight(project['id'])
        banned = self._insight_answer([snapshots[0]['id']])
        banned['body'] = '开头结构导致互动翻倍，因果已验证。'
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=banned):
            with self.assertRaises(ValueError):
                feedback_service.create_insight(project['id'])
        self.assertEqual([i for i in self.store.list(contracts.KIND_REVIEW_INSIGHT, 5000)
                          if i.get('project_id') == project['id']], [])

    def test_collect_skips_stub_platform_work_honestly(self):
        project = self.store.put('project', {'title': '桩平台作品', 'source_id': 'source_b5',
                                             'tags': [], 'stage': 'ready_to_export'})
        account = self.store.put(contracts.KIND_PLATFORM_ACCOUNT,
                                 contracts.create_platform_account(
                                     'kuaishou', '快手号', 'default', ['video_publish']))
        task = contracts.create_matrix_publish_task(
            project['id'], 'artifact_b5', 'script_b5', account['id'], 'idem-b5-kuaishou',
            {'title': '快手占位'})
        work = self.store.put(contracts.KIND_PLATFORM_WORK, contracts.create_platform_work(
            account['id'], 'kuaishou', 'ks-1', title='快手占位',
            source_url='https://www.kuaishou.com/short-video/ks-1',
            publish_task_id=task['id'], published_at=TS1, raw={}))
        self.store.put(contracts.KIND_MATRIX_PUBLISH_TASK,
                       contracts.set_publish_success(task, work['id'],
                                                     'https://www.kuaishou.com/short-video/ks-1'))
        collected = feedback_service.collect({'project_id': project['id']})
        self.assertEqual(collected['job']['status'], 'succeeded')
        job = self.store.get('job', collected['job']['id'])  # _job_view 不带 result，回库读
        entry = job['result']['works'][0]
        self.assertFalse(entry['ok'])
        self.assertTrue(entry['skipped'])
        self.assertIn('未接入', entry['error'])
        self.assertEqual(feedback_service.list_snapshots(work['id'])['total'], 0)


if __name__ == '__main__':
    unittest.main()
