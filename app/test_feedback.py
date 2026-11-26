"""发布数据与评论回流→复盘：第一阶段后端测试。全部使用真实形状的 fixture 数据，
不联网、不启动真实采集；缺 LLM/空证据/伪造 ID/伪造因果必须被明确拒绝，
浏览器采集不可用时验证其真实失败而非假成功。"""
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
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent/'.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT/('feedback-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import contracts
import feedback_collect
import feedback_service
from db import Store

VIDEO_ID = '7500000000000000001'
VIDEO_URL = 'https://www.douyin.com/video/' + VIDEO_ID
COLLECTED_AT_1 = '2026-10-27T10:00:00.000Z'
COLLECTED_AT_2 = '2026-10-27T11:00:00.000Z'

# 真实采集 JSON 形状（= DouyinWorkCollector 输出 / RawFeedbackImporter 消费格式）。
FIXTURE_RAW = {
    'ok': True,
    'source_url': VIDEO_URL,
    'collected_at': COLLECTED_AT_1,
    'platform_video_id': VIDEO_ID,
    'metrics': {'play': 12000, 'like': 1500, 'comment': '1.2万', 'share': 10, 'collect': None},
    'metrics_raw': {'aweme_detail': {'aweme_id': VIDEO_ID,
                                     'statistics': {'play_count': 12000, 'digg_count': 1500,
                                                    'comment_count': 32, 'share_count': 10,
                                                    'collect_count': 80}}},
    'metrics_note': None,
    'comments': [
        {'platform_comment_id': 'pc-2001', 'author_name': '粉丝甲',
         'content': '干货满满，直接照做', 'like_count': 30, 'reply_count': 1,
         'parent_comment_id': None},
        {'platform_comment_id': 'pc-2002', 'author_name': '粉丝乙',
         'content': '求更新，等续集', 'like_count': None, 'reply_count': 0,
         'parent_comment_id': None}],
    'comments_error': None}

NO_COMMENT_RAW = {
    'source_url': 'https://www.douyin.com/video/7500000000000000002',
    'collected_at': COLLECTED_AT_1,
    'platform_video_id': '7500000000000000002',
    'metrics': {'play': None, 'like': 3, 'comment': 1, 'share': None, 'collect': None},
    'metrics_raw': None,
    'metrics_note': 'metrics_not_loaded',
    'comments': [],
    'comments_error': 'comments_not_loaded'}


def fixture_at(collected_at, raw=FIXTURE_RAW):
    clone = copy.deepcopy(raw)
    clone['collected_at'] = collected_at
    return clone


def build_published_work(store, project_id, video_id, key):
    """经 contracts 最小链路造 published 任务 + platform_work（不经发布执行器）。"""
    account = store.put(contracts.KIND_PLATFORM_ACCOUNT, contracts.create_platform_account(
        'douyin', '本人抖音号-'+key, 'default', ['video_publish'], status='connected'))
    url = 'https://www.douyin.com/video/' + video_id
    task = contracts.create_matrix_publish_task(
        project_id, 'artifact_'+key, 'script_'+key, account['id'], 'idem-fb-'+key,
        {'title': '标题-'+key})
    work = store.put(contracts.KIND_PLATFORM_WORK, contracts.create_platform_work(
        account['id'], 'douyin', video_id, title='标题-'+key, source_url=url,
        publish_task_id=task['id'], published_at=COLLECTED_AT_1,
        raw={'receipt': {'url': url}}))
    done = contracts.set_publish_success(task, work['id'], url, raw={'receipt': {'url': url}})
    store.put(contracts.KIND_MATRIX_PUBLISH_TASK, done)
    return work


def make_project(store, title='复盘测试项目'):
    return store.put('project', {'title': title, 'source_id': 'source_fb',
                                 'tags': [], 'stage': 'ready_to_export'})


class FeedbackUnitTests(unittest.TestCase):
    """纯函数/导入器级校验，不启动服务。"""

    def test_parse_video_url_valid_and_invalid(self):
        url, vid = feedback_collect.parse_video_url(VIDEO_URL)
        self.assertEqual(vid, VIDEO_ID)
        self.assertEqual(url, VIDEO_URL)
        for bad in ('http://www.douyin.com/video/123', 'https://www.douyin.com/user/abc',
                    'https://example.com/video/123', '', None, 'not-a-url'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                feedback_collect.parse_video_url(bad)

    def test_metric_null_rules(self):
        metrics = feedback_collect.parse_metrics(
            {'statistics': {'play_count': 12000, 'digg_count': '1.2万', 'comment_count': True,
                            'share_count': -1, 'collect_count': 80}})
        self.assertEqual(metrics['play'], 12000)
        self.assertEqual(metrics['collect'], 80)
        for target in ('like', 'comment', 'share'):
            self.assertIsNone(metrics[target], target)
        empty = feedback_collect.parse_metrics(None)
        self.assertTrue(all(value is None for value in empty.values()))

    def test_platform_comment_contract(self):
        rec = contracts.create_platform_comment('pw_1', 'douyin', 'pc-1', '粉丝', '内容',
                                                source_url=VIDEO_URL, like_count=None)
        self.assertEqual(rec['platform_work_id'], 'pw_1')
        self.assertIsNone(rec['like_count'])
        with self.assertRaises(ValueError):
            contracts.create_platform_comment('pw_1', 'douyin', 'pc-1', '', '内容',
                                              source_url=VIDEO_URL)
        with self.assertRaises(ValueError):
            contracts.create_platform_comment('pw_1', 'douyin', 'pc-1', '粉丝', '',
                                              source_url=VIDEO_URL)
        with self.assertRaises(ValueError):
            contracts.create_platform_comment('pw_1', 'douyin', 'pc-1', '粉丝', '内容',
                                              source_url=VIDEO_URL, like_count=True)

    def test_comment_feedback_dual_link_contract(self):
        # 旧 bench_comment 链路保持不变。
        old = contracts.create_comment_feedback('bench_comment_x', 'bench_work_x', 'like')
        self.assertEqual(old['bench_comment_id'], 'bench_comment_x')
        # 新 platform_comment 链路：bench_* 显式 null，platform_comment_id 必填其一。
        new = contracts.create_comment_feedback(None, None, 'pin', platform_comment_id='pc_1',
                                                platform_work_id='pw_1')
        self.assertIsNone(new['bench_comment_id'])
        self.assertEqual(new['platform_comment_id'], 'pc_1')
        with self.assertRaises(ValueError):
            contracts.create_comment_feedback(None, None, 'pin')  # 两条链路都没有评论引用

    def test_topic_source_compatibility(self):
        # 旧来源 bench_work / knowledge 兼容不变；新来源 review_insight 合法；无来源仍拒绝。
        by_bench = contracts.create_topic('旧bench选题', bench_work_id='bench_work_x')
        self.assertEqual(by_bench['bench_work_id'], 'bench_work_x')
        by_knowledge = contracts.create_topic('旧认知选题', knowledge_id='knowledge_x')
        self.assertEqual(by_knowledge['knowledge_id'], 'knowledge_x')
        by_insight = contracts.create_topic('复盘洞察选题', review_insight_id='review_insight_x')
        self.assertEqual(by_insight['review_insight_id'], 'review_insight_x')
        self.assertIsNone(by_insight['bench_work_id'])
        self.assertIsNone(by_insight['knowledge_id'])
        with self.assertRaises(ValueError):
            contracts.create_topic('无来源选题')

    def test_snapshot_import_appends_history_no_overwrite(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            work = build_published_work(store, 'project_u', VIDEO_ID, 'unit-history')
            first = feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_1), work['id'])
            second = feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_2), work['id'])
            self.assertGreater(first['snapshots_created'], 0)
            self.assertGreater(second['snapshots_created'], 0)
            snapshots = [s for s in store.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                         if s.get('platform_work_id') == work['id']]
            keys = {(s['metric_key'], s['collected_at']) for s in snapshots}
            for target in ('play', 'like', 'share'):
                self.assertIn((f'douyin.{target}', COLLECTED_AT_1), keys)
                self.assertIn((f'douyin.{target}', COLLECTED_AT_2), keys)
            # 只新增不覆盖：两个时间点各自存在，旧值不被新值替换。
            play = [s for s in snapshots if s['metric_key'] == 'douyin.play']
            self.assertEqual({s['value'] for s in play}, {12000})

    def test_snapshot_null_not_saved_and_raw_preserved(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            work = build_published_work(store, 'project_u', VIDEO_ID, 'unit-null')
            feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_1), work['id'])
            snapshots = {s['metric_key']: s for s in store.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                         if s.get('platform_work_id') == work['id']
                         and s.get('collected_at') == COLLECTED_AT_1}
            # 'comment' 是 '1.2万' 展示串 → null → 不生成快照，不用 0 冒充。
            self.assertNotIn('douyin.comment', snapshots)
            self.assertNotIn('douyin.collect', snapshots)
            self.assertTrue(snapshots['douyin.play']['raw'])  # 原始返回保留

    def test_import_rejects_mismatched_work_and_bad_raw(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            work = build_published_work(store, 'project_u', VIDEO_ID, 'unit-match')
            other = build_published_work(store, 'project_u', '7600000000000000099', 'unit-other')
            with self.assertRaises(ValueError):
                feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_1), other['id'])
            bad_url = copy.deepcopy(FIXTURE_RAW)
            bad_url['source_url'] = 'http://www.douyin.com/video/' + VIDEO_ID
            with self.assertRaises(ValueError):
                feedback_collect.import_raw(store, bad_url, work['id'])
            bad_ts = copy.deepcopy(FIXTURE_RAW)
            bad_ts['collected_at'] = '2026-10-27'
            with self.assertRaises(ValueError):
                feedback_collect.import_raw(store, bad_ts, work['id'])

    def test_comment_import_dedupe_and_requirements(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            work = build_published_work(store, 'project_u', VIDEO_ID, 'unit-dedupe')
            first = feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_1), work['id'])
            again = feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_2), work['id'])
            self.assertEqual(first['comments_created'], 2)
            self.assertEqual(again['comments_created'], 0)  # 同平台评论 ID 去重
            comments = [c for c in store.list(contracts.KIND_PLATFORM_COMMENT, 50000)
                        if c.get('platform_work_id') == work['id']]
            self.assertEqual(len(comments), 2)
            bad = copy.deepcopy(FIXTURE_RAW)
            bad['comments'] = [{'platform_comment_id': 'pc-x', 'author_name': '',
                                'content': '缺作者'}]
            with self.assertRaises(ValueError):
                feedback_collect.import_raw(store, bad, work['id'])

    def test_restart_keeps_state_readable(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            project = make_project(store)
            work = build_published_work(store, project['id'], VIDEO_ID, 'restart')
            feedback_collect.import_raw(store, fixture_at(COLLECTED_AT_1), work['id'])
            reopened = Store(d)
            snapshots = [s for s in reopened.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                         if s.get('platform_work_id') == work['id']]
            comments = [c for c in reopened.list(contracts.KIND_PLATFORM_COMMENT, 50000)
                        if c.get('platform_work_id') == work['id']]
            loaded_work = reopened.get(contracts.KIND_PLATFORM_WORK, work['id'])
            self.assertGreaterEqual(len(snapshots), 3)
            self.assertEqual(len(comments), 2)
            self.assertEqual(loaded_work['platform_video_id'], VIDEO_ID)
            self.assertTrue(loaded_work['raw'])


class FeedbackAPITests(unittest.TestCase):
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

    def wait_job(self, job_id, want=None, deadline=None):
        """退避轮询等待任务结束；deadline 默认 45s（FEEDBACK_TEST_WAIT_DEADLINE 可配）。

        G 盘 SQLite 在整机并发时会超过 10s，原先固定 10s 窗口会假超时；超时打印实际状态。
        """
        limit = float(os.environ.get('FEEDBACK_TEST_WAIT_DEADLINE', '45')) if deadline is None else float(deadline)
        start = time.monotonic()
        interval = 0.02
        current = None
        while True:
            current = self.request('/api/jobs/'+job_id)
            if current['status'] not in ('queued', 'running'):
                elapsed = time.monotonic()-start
                if elapsed > 1.0:
                    print(f'[wait] 后台任务 {job_id} 用时 {elapsed:.2f}s（deadline {limit:g}s）')
                if want:
                    self.assertEqual(current['status'], want, current.get('error'))
                return current
            if time.monotonic()-start >= limit:
                break
            time.sleep(interval)
            interval = min(interval*1.7, 0.5)
        state = json.dumps(current, ensure_ascii=False)
        print(f'[wait-timeout] 后台任务 {job_id} 等待 {time.monotonic()-start:.2f}s 未结束；'
              f'最后状态：{state[:1000]}')
        self.fail(f'后台任务 {job_id} 等待 {limit:g}s 仍未结束（FEEDBACK_TEST_WAIT_DEADLINE 可调）；'
                  f'最后状态：{state[:1000]}')

    # ---- 准备 ----

    def make_project_with_work(self, key=None, video_id=VIDEO_ID, raw=FIXTURE_RAW):
        key = key or uuid.uuid4().hex[:8]
        project = make_project(app.STORE, '复盘项目-'+key)
        work = build_published_work(app.STORE, project['id'], video_id, key)
        if raw is not None:
            feedback_collect.import_raw(app.STORE, fixture_at(COLLECTED_AT_1, raw), work['id'])
        return project, work

    # ---- 总览 / 作品列表 ----

    def test_overview_empty_state_real_empty_lists(self):
        project = make_project(app.STORE)
        result = self.request('/api/feedback/overview?project_id='+project['id'])
        self.assertEqual(sorted(result), sorted(('project', 'platform_works',
                                                 'platform_work_count', 'latest_snapshots',
                                                 'latest_snapshot_at', 'comments_count',
                                                 'insights_count')))
        self.assertEqual(result['project']['id'], project['id'])
        self.assertEqual(result['platform_works'], [])  # 真实空列表
        self.assertEqual(result['platform_work_count'], 0)
        self.assertEqual(result['latest_snapshots'], [])
        self.assertIsNone(result['latest_snapshot_at'])
        self.assertEqual(result['comments_count'], 0)
        self.assertEqual(result['insights_count'], 0)

    def test_overview_unknown_project_404(self):
        self.request('/api/feedback/overview?project_id=project_nope', expected=404)
        self.request('/api/feedback/overview', expected=400)

    def test_overview_with_data(self):
        project, work = self.make_project_with_work('overview')
        result = self.request('/api/feedback/overview?project_id='+project['id'])
        self.assertEqual([w['id'] for w in result['platform_works']], [work['id']])
        self.assertEqual(result['platform_work_count'], 1)
        self.assertEqual(len(result['latest_snapshots']), 3)  # play/like/share
        self.assertEqual(result['latest_snapshot_at'], COLLECTED_AT_1)
        self.assertEqual(result['comments_count'], 2)
        self.assertEqual(result['insights_count'], 0)

    def test_works_list_includes_url_published_raw(self):
        project, work = self.make_project_with_work('works')
        result = self.request('/api/feedback/works?project_id='+project['id'])
        self.assertEqual(sorted(result), ['items', 'total'])
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['id'], work['id'])
        self.assertEqual(item['platform_url'], VIDEO_URL)
        self.assertEqual(item['source_url'], VIDEO_URL)
        self.assertEqual(item['published_at'], COLLECTED_AT_1)
        self.assertTrue(item['raw'])
        self.assertEqual(item['platform_video_id'], VIDEO_ID)

    def test_works_unknown_project_404(self):
        self.request('/api/feedback/works?project_id=project_nope', expected=404)

    # ---- 快照 / 评论 ----

    def test_snapshots_ascending_and_shape(self):
        project, work = self.make_project_with_work('snaps')
        feedback_collect.import_raw(app.STORE, fixture_at(COLLECTED_AT_2), work['id'])
        result = self.request('/api/feedback/snapshots?platform_work_id='+work['id'])
        self.assertEqual(sorted(result), ['items', 'total'])
        self.assertEqual(result['total'], 6)
        stamps = [s['collected_at'] for s in result['items']]
        self.assertEqual(stamps, sorted(stamps))  # 按 collected_at 升序
        for snapshot in result['items']:
            self.assertEqual(snapshot['platform_work_id'], work['id'])
            self.assertGreaterEqual(snapshot['value'], 0)
            self.assertTrue(snapshot['raw'])

    def test_snapshots_unknown_work_404(self):
        self.request('/api/feedback/snapshots?platform_work_id=pw_nope', expected=404)

    def test_comments_pagination_and_shape(self):
        project, work = self.make_project_with_work('paging')
        extra = fixture_at(COLLECTED_AT_1)
        extra['comments'] = [{'platform_comment_id': 'pc-extra-%d' % i,
                              'author_name': '粉丝%d' % i, 'content': '评论%d' % i,
                              'like_count': i, 'reply_count': 0, 'parent_comment_id': None}
                             for i in range(23)]
        feedback_collect.import_raw(app.STORE, extra, work['id'])
        page1 = self.request('/api/feedback/comments?platform_work_id='+work['id']+'&page=1&page_size=10')
        self.assertEqual(sorted(page1), ['items', 'page', 'page_size', 'total'])
        self.assertEqual(page1['total'], 25)
        self.assertEqual(page1['page'], 1)
        self.assertEqual(page1['page_size'], 10)
        self.assertEqual(len(page1['items']), 10)
        page3 = self.request('/api/feedback/comments?platform_work_id='+work['id']+'&page=3&page_size=10')
        self.assertEqual(len(page3['items']), 5)
        ids = {c['id'] for c in page1['items']} & {c['id'] for c in page3['items']}
        self.assertEqual(ids, set())  # 分页不重叠不遗漏
        # 固定含 category 字段：未分类为 null。
        for item in page1['items'] + page3['items']:
            self.assertIn('category', item)
            self.assertIsNone(item['category'])

    def test_comments_empty_and_unknown_work(self):
        project, work = self.make_project_with_work('nocomment', video_id='7500000000000000002',
                                                    raw=NO_COMMENT_RAW)
        result = self.request('/api/feedback/comments?platform_work_id='+work['id'])
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['items'], [])
        self.request('/api/feedback/comments?platform_work_id=pw_nope', expected=404)

    # ---- 采集 job ----

    def test_collect_without_published_works_explicit_message(self):
        project = make_project(app.STORE)
        result = self.request('/api/feedback/collect', 'POST',
                              {'project_id': project['id']}, expected=202)
        self.assertIsNone(result['job'])
        self.assertEqual(result['works'], [])
        self.assertIn('还没有已发布', result['message'])
        self.assertIn('未发布', result['message'])

    def test_collect_returns_fixed_shape(self):
        project, work = self.make_project_with_work('collect-shape', raw=None)
        result = self.request('/api/feedback/collect', 'POST',
                              {'project_id': project['id'], 'raw': fixture_at(COLLECTED_AT_1)},
                              expected=202)
        # 固定形状 {job:{id,status,stage,error}, works:[...], message}
        self.assertEqual(sorted(result), ['job', 'message', 'works'])
        self.assertEqual(sorted(result['job']), ['error', 'id', 'stage', 'status'])
        self.assertIn(result['job']['status'], ('queued', 'running', 'succeeded'))
        self.assertTrue(result['job']['stage'])
        self.assertEqual(len(result['works']), 1)
        summary = result['works'][0]
        self.assertEqual(summary['platform_work_id'], work['id'])
        self.assertEqual(summary['platform_url'], VIDEO_URL)
        self.assertEqual(summary['platform_video_id'], VIDEO_ID)
        self.assertIn('已开始', result['message'])
        done = self.wait_job(result['job']['id'], 'succeeded')
        self.assertGreaterEqual(done['result']['works_ok'], 1)

    def test_collect_with_raw_imports_and_job_succeeds(self):
        project, work = self.make_project_with_work('collect-raw', raw=None)
        result = self.request('/api/feedback/collect', 'POST',
                              {'project_id': project['id'], 'raw': fixture_at(COLLECTED_AT_1)},
                              expected=202)
        job = self.wait_job(result['job']['id'], 'succeeded')
        entry = job['result']['works'][0]
        self.assertTrue(entry['ok'])
        self.assertGreaterEqual(entry['snapshots_created'], 3)
        self.assertEqual(entry['comments_created'], 2)
        listed = self.request('/api/feedback/comments?platform_work_id='+work['id'])
        self.assertEqual(listed['total'], 2)

    def test_collect_is_idempotent_while_active(self):
        project, work = self.make_project_with_work('collect-idem', raw=None)
        real_import = feedback_collect.import_raw
        calls = {'n': 0}

        def slow_import(store, raw, work_id):
            calls['n'] += 1
            time.sleep(0.8)
            return real_import(store, raw, work_id)
        with patch.object(feedback_collect, 'import_raw', side_effect=slow_import):
            first = self.request('/api/feedback/collect', 'POST',
                                 {'project_id': project['id'], 'raw': fixture_at(COLLECTED_AT_1)},
                                 expected=202)
            second = self.request('/api/feedback/collect', 'POST',
                                  {'project_id': project['id'], 'raw': fixture_at(COLLECTED_AT_1)},
                                  expected=202)
            # 复用同一 job（幂等）
            self.assertEqual(first['job']['id'], second['job']['id'])
            current = self.wait_job(first['job']['id'], 'succeeded')
        self.assertGreaterEqual(calls['n'], 1)
        self.assertGreaterEqual(current['result']['works_ok'], 1)

    def test_collect_single_work_failure_isolated(self):
        key = uuid.uuid4().hex[:8]
        project = make_project(app.STORE, '隔离项目-'+key)
        work_a = build_published_work(app.STORE, project['id'], VIDEO_ID, key+'a')
        work_b = build_published_work(app.STORE, project['id'],
                                      '7600000000000000099', key+'b')
        # raw 只匹配 work_a：同一 job 内 work_b 如实记录失败，不影响 work_a。
        result = self.request('/api/feedback/collect', 'POST',
                              {'project_id': project['id'], 'raw': fixture_at(COLLECTED_AT_1)},
                              expected=202)
        self.assertEqual(len(result['works']), 2)
        job = self.wait_job(result['job']['id'], 'succeeded')  # 单作品失败不拖垮整个 job
        entries = {w['platform_work_id']: w for w in job['result']['works']}
        self.assertTrue(entries[work_a['id']]['ok'])
        self.assertGreaterEqual(entries[work_a['id']]['snapshots_created'], 3)
        self.assertFalse(entries[work_b['id']]['ok'])
        self.assertIn('不一致', entries[work_b['id']]['error'])
        self.assertEqual(job['result']['works_ok'], 1)
        self.assertEqual(job['result']['works_failed'], 1)
        listed = self.request('/api/feedback/comments?platform_work_id='+work_b['id'])
        self.assertEqual(listed['total'], 0)  # 失败作品没有假数据

    def test_collect_fails_honestly_without_browser(self):
        project, work = self.make_project_with_work('no-browser', raw=None)
        missing = TEST_PATH/'no-such-browser-env'/'python.exe'
        with patch('feedback_collect.video_intake.browser_python', return_value=missing):
            result = self.request('/api/feedback/collect', 'POST',
                                  {'project_id': project['id']}, expected=202)
            current = self.wait_job(result['job']['id'], 'failed')
        self.assertIn('浏览器采集环境未安装', current['error'])
        listed = self.request('/api/feedback/snapshots?platform_work_id='+work['id'])
        self.assertEqual(listed['total'], 0)  # 不假成功、不补造数据

    # ---- 评论分类 ----

    def test_classify_requires_llm(self):
        project, work = self.make_project_with_work('cls-nollm')
        with patch('providers.available', return_value={'llm': False}):
            result = self.request('/api/feedback/classify-comments', 'POST',
                                  {'platform_work_id': work['id']}, expected=409)
        self.assertIn('尚未配置', result['error'])
        self.assertEqual(len(app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)), 0)

    def test_classify_rejects_empty_comments(self):
        project, work = self.make_project_with_work('cls-empty', video_id='7500000000000000002',
                                                    raw=NO_COMMENT_RAW)
        with patch('providers.available', return_value={'llm': True}):
            result = self.request('/api/feedback/classify-comments', 'POST',
                                  {'platform_work_id': work['id']}, expected=400)
        self.assertIn('没有已采评论', result['error'])

    def test_classify_rejects_forged_comment_id(self):
        project, work = self.make_project_with_work('cls-forged')
        answer = {'classifications': [{'comment_id': 'platform_comment_nope',
                                       'category': 'praise', 'note': '伪造引用'}]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            result = self.request('/api/feedback/classify-comments', 'POST',
                                  {'platform_work_id': work['id']}, expected=400)
        self.assertIn('不存在的评论ID', result['error'])
        self.assertEqual(len(app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)), 0)

    def test_classify_rejects_invalid_category(self):
        project, work = self.make_project_with_work('cls-badcat')
        comments = self.request('/api/feedback/comments?platform_work_id='+work['id'])['items']
        answer = {'classifications': [{'comment_id': comments[0]['id'],
                                       'category': 'label', 'note': '用了旧字段名'}]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            result = self.request('/api/feedback/classify-comments', 'POST',
                                  {'platform_work_id': work['id']}, expected=400)
        self.assertIn('category 无效', result['error'])

    def test_classify_success_returns_counts_and_persists_category(self):
        project, work = self.make_project_with_work('cls-ok')
        comments = self.request('/api/feedback/comments?platform_work_id='+work['id'])['items']
        self.assertEqual(len(comments), 2)
        answer = {'classifications': [
            {'comment_id': comments[0]['id'], 'category': 'praise', 'note': '高认同'},
            {'comment_id': comments[1]['id'], 'category': 'sequel', 'note': '催更'}]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer) as llm:
            result = self.request('/api/feedback/classify-comments', 'POST',
                                  {'platform_work_id': work['id']}, expected=201)
        # 固定形状 {items, counts, message}；counts 六类固定键。
        self.assertEqual(sorted(result), ['counts', 'items', 'message'])
        self.assertEqual(result['counts'], {'question': 0, 'painpoint': 0, 'objection': 0,
                                            'praise': 1, 'sequel': 1, 'other': 0})
        payload = llm.call_args[0][1]
        self.assertEqual({c['id'] for c in payload['comments']},
                         {comments[0]['id'], comments[1]['id']})
        # items 是更新后的评论对象：固定使用 category 字段，不用 label/classification。
        real_ids = {c['id'] for c in comments}
        categories = {comments[0]['id']: 'praise', comments[1]['id']: 'sequel'}
        for item in result['items']:
            self.assertIn(item['id'], real_ids)
            self.assertEqual(item['category'], categories[item['id']])
            self.assertNotIn('label', item)
            self.assertNotIn('classification', item)
        # 落库验证：评论对象与反馈记录都带真实评论 ID 证据。
        feedbacks = app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)
        self.assertEqual(len(feedbacks), 2)
        for feedback in feedbacks:
            self.assertIn(feedback['platform_comment_id'], real_ids)
            self.assertEqual(feedback['markers'], ['模型推断', '待验证'])
            self.assertEqual(feedback['validation'], 'unverified')
            self.assertIsNone(feedback['bench_comment_id'])  # 旧链路字段不被污染
        listed = self.request('/api/feedback/comments?platform_work_id='+work['id'])
        for item in listed['items']:
            self.assertEqual(item['category'], categories[item['id']])

    # ---- 复盘洞察 ----

    def _classified_project(self, key):
        """有快照 + 已分类评论的项目，返回 (project, work, feedback_ids)。"""
        project, work = self.make_project_with_work(key)
        comments = [c for c in app.STORE.list(contracts.KIND_PLATFORM_COMMENT, 50000)
                    if c.get('platform_work_id') == work['id']]
        feedbacks = []
        for index, comment in enumerate(comments):
            record = contracts.create_comment_feedback(
                None, None, 'pin' if index == 0 else 'reply', note='依据真实评论',
                platform_comment_id=comment['id'], platform_work_id=work['id'])
            feedbacks.append(app.STORE.put(contracts.KIND_COMMENT_FEEDBACK, record))
        return project, work, [f['id'] for f in feedbacks]

    def _good_insight_answer(self, evidence_ids):
        return {'title': '开头结构互动观察', 'body': '点赞评论集中在开头钩子相关反馈，'
                '与开头结构可能相关（相关性观察，未经对照验证）。',
                'insight_type': 'data',
                'findings': [{'claim': '互动反馈集中在开头结构', 'evidence_ids': evidence_ids}]}

    def test_insight_requires_llm(self):
        project, work, fb_ids = self._classified_project('ins-nollm')
        with patch('providers.available', return_value={'llm': False}):
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=409)
        self.assertIn('尚未配置', result['error'])

    def test_insight_rejects_empty_evidence(self):
        project = make_project(app.STORE)
        work = build_published_work(app.STORE, project['id'], VIDEO_ID, 'ins-empty')
        with patch('providers.available', return_value={'llm': True}):
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=400)
        self.assertIn('没有任何指标快照或已分类评论', result['error'])

    def test_insight_rejects_forged_evidence_id(self):
        project, work, fb_ids = self._classified_project('ins-forged')
        answer = self._good_insight_answer(['metric_snapshot_nope'])
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=400)
        self.assertIn('不存在的证据ID', result['error'])
        leftovers = [i for i in app.STORE.list(contracts.KIND_REVIEW_INSIGHT, 5000)
                     if i.get('project_id') == project['id']]
        self.assertEqual(leftovers, [])

    def test_insight_rejects_verified_causality_claim(self):
        project, work, fb_ids = self._classified_project('ins-causal')
        answer = self._good_insight_answer(fb_ids)
        answer['body'] = '开头结构导致互动翻倍，因果已验证。'
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=400)
        self.assertIn('已被验证', result['error'])
        leftovers = [i for i in app.STORE.list(contracts.KIND_REVIEW_INSIGHT, 5000)
                     if i.get('project_id') == project['id']]
        self.assertEqual(leftovers, [])

    def test_insight_success_saves_evidence_chain_and_markers(self):
        project, work, fb_ids = self._classified_project('ins-ok')
        snapshots = [s for s in app.STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                     if s.get('platform_work_id') == work['id']]
        evidence_ids = fb_ids + [snapshots[0]['id']]
        answer = self._good_insight_answer(evidence_ids)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer) as llm:
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=201)
        # 固定形状 {item, message}；item 已落库。
        self.assertEqual(sorted(result), ['item', 'message'])
        self.assertIn('待验证', result['message'])
        item = result['item']
        self.assertEqual(item['project_id'], project['id'])
        self.assertEqual(item['platform_work_id'], work['id'])
        self.assertEqual(item['markers'], ['模型推断', '待验证'])
        self.assertEqual(item['validation'], 'unverified')
        self.assertEqual(item['created_by'], 'model_assisted')
        self.assertEqual(item['status'], 'candidate')  # 候选待验证，本人确认后才 confirmed
        self.assertEqual(sorted(item['evidence_ids']), sorted(evidence_ids))
        self.assertEqual(item['evidence_count'], len(evidence_ids))
        self.assertEqual(item['platform_work_ids'], [work['id']])
        self.assertEqual(item['platform_work_urls'], [VIDEO_URL])
        self.assertEqual(item['findings'][0]['evidence_ids'], evidence_ids)
        pool = llm.call_args[0][1]['evidence_pool']
        self.assertTrue(set(evidence_ids) <= set(pool))
        # 已落库：GET insights 立刻可查，且证据 ID 全部真实存在。
        listed = self.request('/api/feedback/insights?project_id='+project['id'])
        self.assertEqual(listed['total'], 1)
        got = listed['items'][0]
        self.assertEqual(got['id'], item['id'])
        known = {s['id'] for s in app.STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)}
        known |= {f['id'] for f in app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)}
        self.assertTrue(set(got['evidence_ids']) <= known)
        # 总览 insights_count 联动
        overview = self.request('/api/feedback/overview?project_id='+project['id'])
        self.assertEqual(overview['insights_count'], 1)

    def test_insights_list_fixed_fields_and_status(self):
        project, work, fb_ids = self._classified_project('ins-list')
        answer = self._good_insight_answer(fb_ids)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            self.request('/api/feedback/insight', 'POST',
                         {'project_id': project['id']}, expected=201)
        result = self.request('/api/feedback/insights?project_id='+project['id'])
        self.assertEqual(sorted(result), ['items', 'total'])
        self.assertEqual(result['total'], 1)
        item = result['items'][0]
        self.assertEqual(item['title'], '开头结构互动观察')
        # 固定字段：evidence_count/evidence_ids/platform_work_ids/platform_work_urls/status。
        for field in ('evidence_count', 'evidence_ids', 'platform_work_ids',
                      'platform_work_urls', 'status'):
            self.assertIn(field, item)
        self.assertEqual(item['status'], 'candidate')
        self.assertIn(item['status'], ('candidate', 'confirmed'))
        self.assertEqual(item['evidence_count'], len(item['evidence_ids']))
        self.assertEqual(item['platform_work_urls'], [VIDEO_URL])
        self.request('/api/feedback/insights?project_id=project_nope', expected=404)

    # ---- 复盘洞察 → 选题立项 ----

    def _topic_answer(self):
        return {'title': '开头结构改良对照实验', 'angle': '把复盘观察变成下一条可验证内容',
                'thesis': '沿用同结构做一条对照内容，观察互动是否改善（待验证假设）'}

    def _make_insight(self, key, mutate=None):
        """经 insight API 造一条 candidate 洞察，可变异后重写（伪造证据/去项目/改状态等）。"""
        project, work, fb_ids = self._classified_project(key)
        snapshots = [s for s in app.STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                     if s.get('platform_work_id') == work['id']]
        evidence_ids = fb_ids + [snapshots[0]['id']]
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._good_insight_answer(evidence_ids)):
            result = self.request('/api/feedback/insight', 'POST',
                                  {'project_id': project['id']}, expected=201)
        insight = result['item']
        if mutate is not None:
            changed = mutate(dict(insight))
            if changed is not None:
                app.STORE.put(contracts.KIND_REVIEW_INSIGHT, changed)
                insight = app.STORE.get(contracts.KIND_REVIEW_INSIGHT, insight['id'])
        return project, work, insight

    def test_start_topic_success_creates_topic_and_project(self):
        project, work, insight = self._make_insight('topic-ok')
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()) as llm:
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=201)
        # 固定形状 {topic_id, project_id, reused}
        self.assertEqual(sorted(result), ['project_id', 'reused', 'topic_id'])
        self.assertFalse(result['reused'])
        # 模型输入只包含真实证据
        payload = llm.call_args[0][1]
        self.assertEqual(payload['insight']['id'], insight['id'])
        known = {s['id'] for s in app.STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)}
        known |= {f['id'] for f in app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)}
        self.assertTrue({e['id'] for e in payload['evidence']} <= known)
        # topic 关联字段完整（project_id 为立项回写的新内容项目，source_project_id 溯源洞察来源项目）
        topic = app.STORE.get(contracts.KIND_TOPIC, result['topic_id'])
        self.assertIsNotNone(topic)
        self.assertEqual(topic['review_insight_id'], insight['id'])
        self.assertEqual(topic['project_id'], result['project_id'])
        self.assertNotEqual(topic['project_id'], project['id'])
        self.assertEqual(topic['source_project_id'], project['id'])
        self.assertEqual(topic['platform_work_ids'], [work['id']])
        self.assertEqual(sorted(topic['evidence_ids']), sorted(insight['evidence_ids']))
        self.assertEqual(topic['markers'], ['模型推断', '待验证'])
        self.assertEqual(topic['validation'], 'unverified')
        self.assertEqual(topic['origin'], 'feedback')
        self.assertEqual(topic['status'], 'candidate')
        self.assertEqual((topic.get('data') or {}).get('project_id'), result['project_id'])
        # 已立项：项目存在且回链 topic
        got = self.request('/api/projects/'+result['project_id'])
        self.assertEqual(got['topic_id'], topic['id'])
        self.assertEqual(got['title'], topic['title'])

    def test_start_topic_idempotent_reuse_and_no_overwrite(self):
        project, work, insight = self._make_insight('topic-idem')
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            first = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                 'POST', {}, expected=201)
            revision_after_first = self.request('/api/projects/'+first['project_id'])['revision']
            second = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=201)
        self.assertFalse(first['reused'])
        self.assertTrue(second['reused'])
        self.assertEqual(second['topic_id'], first['topic_id'])
        self.assertEqual(second['project_id'], first['project_id'])
        # 不重复立项、不覆盖已有项目
        topics = [t for t in app.STORE.list(contracts.KIND_TOPIC, 5000)
                  if t.get('review_insight_id') == insight['id']]
        self.assertEqual(len(topics), 1)
        after = self.request('/api/projects/'+first['project_id'])
        self.assertEqual(after['revision'], revision_after_first)
        self.assertEqual(after['title'], topics[0]['title'])

    def test_start_topic_different_insights_create_different_topics(self):
        project_a, work_a, insight_a = self._make_insight('topic-a')
        project_b, work_b, insight_b = self._make_insight('topic-b')
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            result_a = self.request('/api/feedback/insights/'+insight_a['id']+'/start-topic',
                                    'POST', {}, expected=201)
            answer_b = dict(self._topic_answer(), title='另一条复盘选题')
            with patch('providers.llm_json', return_value=answer_b):
                result_b = self.request('/api/feedback/insights/'+insight_b['id']+'/start-topic',
                                        'POST', {}, expected=201)
        self.assertNotEqual(result_a['topic_id'], result_b['topic_id'])
        self.assertNotEqual(result_a['project_id'], result_b['project_id'])
        topic_a = app.STORE.get(contracts.KIND_TOPIC, result_a['topic_id'])
        topic_b = app.STORE.get(contracts.KIND_TOPIC, result_b['topic_id'])
        self.assertEqual(topic_a['review_insight_id'], insight_a['id'])
        self.assertEqual(topic_b['review_insight_id'], insight_b['id'])

    def test_start_topic_rejects_forged_evidence_id(self):
        def mutate(insight):
            insight['evidence_ids'] = ['metric_snapshot_nope']
            return insight
        project, work, insight = self._make_insight('topic-forged', mutate=mutate)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=400)
        self.assertIn('不存在的证据ID', result['error'])
        topics = [t for t in app.STORE.list(contracts.KIND_TOPIC, 5000)
                  if t.get('review_insight_id') == insight['id']]
        self.assertEqual(topics, [])

    def test_start_topic_rejects_missing_or_unknown_project(self):
        def no_project(insight):
            insight['project_id'] = None
            return insight
        project, work, insight = self._make_insight('topic-noproj', mutate=no_project)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=400)
        self.assertIn('未关联项目', result['error'])
        # 指向不存在的项目：404
        def ghost_project(insight):
            insight['project_id'] = 'project_nope'
            return insight
        project2, work2, insight2 = self._make_insight('topic-ghost', mutate=ghost_project)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            self.request('/api/feedback/insights/'+insight2['id']+'/start-topic',
                         'POST', {}, expected=404)

    def test_start_topic_unknown_insight_404(self):
        self.request('/api/feedback/insights/review_insight_nope/start-topic',
                     'POST', {}, expected=404)

    def test_start_topic_rejects_illegal_status_but_accepts_confirmed(self):
        def bad_status(insight):
            insight['status'] = 'verified'
            return insight
        project, work, insight = self._make_insight('topic-badstatus', mutate=bad_status)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=400)
        self.assertIn('candidate', result['error'])
        # confirmed 状态允许生成选题
        def confirmed(insight):
            insight['status'] = 'confirmed'
            return insight
        project2, work2, insight2 = self._make_insight('topic-confirmed', mutate=confirmed)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            ok = self.request('/api/feedback/insights/'+insight2['id']+'/start-topic',
                              'POST', {}, expected=201)
        self.assertFalse(ok['reused'])
        self.assertTrue(ok['project_id'])

    def test_start_topic_rejects_no_evidence(self):
        def no_evidence(insight):
            insight['evidence_ids'] = []
            insight['findings'] = []
            return insight
        project, work, insight = self._make_insight('topic-noevidence', mutate=no_evidence)
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._topic_answer()):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=400)
        self.assertIn('没有证据链', result['error'])

    def test_start_topic_requires_llm(self):
        project, work, insight = self._make_insight('topic-nollm')
        with patch('providers.available', return_value={'llm': False}):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=409)
        self.assertIn('尚未配置', result['error'])
        topics = [t for t in app.STORE.list(contracts.KIND_TOPIC, 5000)
                  if t.get('review_insight_id') == insight['id']]
        self.assertEqual(topics, [])

    def test_start_topic_rejects_verified_claim(self):
        project, work, insight = self._make_insight('topic-claim')
        bad_answer = {'title': '已被数据验证的爆款结构', 'angle': '直接照抄',
                      'thesis': '这个结构必然有效，已被数据验证'}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=bad_answer):
            result = self.request('/api/feedback/insights/'+insight['id']+'/start-topic',
                                  'POST', {}, expected=400)
        self.assertIn('已被数据验证', result['error'])
        topics = [t for t in app.STORE.list(contracts.KIND_TOPIC, 5000)
                  if t.get('review_insight_id') == insight['id']]
        self.assertEqual(topics, [])

    # ---- 全链路集成 ----

    def test_full_flow_integration(self):
        """真实形状 fixture → collect 幂等 → classify counts/category 落库
        → insight 同步落库且证据 ID 全部真实 → overview 汇总数量正确。"""
        key = uuid.uuid4().hex[:8]
        project = make_project(app.STORE, '回流联调-'+key)
        work = build_published_work(app.STORE, project['id'], VIDEO_ID, 'flow'+key)
        real_import = feedback_collect.import_raw

        def slow_import(store, raw, work_id):
            time.sleep(0.8)
            return real_import(store, raw, work_id)

        # 1) collect（raw 真实采集 JSON）：重复提交复用同一 job（幂等）。
        with patch.object(feedback_collect, 'import_raw', side_effect=slow_import):
            first = self.request('/api/feedback/collect', 'POST',
                                 {'project_id': project['id'],
                                  'raw': fixture_at(COLLECTED_AT_1)}, expected=202)
            second = self.request('/api/feedback/collect', 'POST',
                                  {'project_id': project['id'],
                                  'raw': fixture_at(COLLECTED_AT_1)}, expected=202)
            self.assertEqual(first['job']['id'], second['job']['id'])
            self.assertEqual(first['works'][0]['platform_work_id'], work['id'])
            job = self.wait_job(first['job']['id'], 'succeeded')
        self.assertEqual(job['result']['works_ok'], 1)
        comments = self.request('/api/feedback/comments?platform_work_id='+work['id'])['items']
        self.assertEqual(len(comments), 2)
        self.assertTrue(all(c['category'] is None for c in comments))

        # 2) classify：counts 六类固定键，category 写回评论对象并落库。
        answer = {'classifications': [
            {'comment_id': comments[0]['id'], 'category': 'question', 'note': '提问型评论'},
            {'comment_id': comments[1]['id'], 'category': 'sequel', 'note': '催更'}]}
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=answer):
            classified = self.request('/api/feedback/classify-comments', 'POST',
                                      {'platform_work_id': work['id']}, expected=201)
        self.assertEqual(classified['counts'], {'question': 1, 'painpoint': 0, 'objection': 0,
                                                'praise': 0, 'sequel': 1, 'other': 0})
        persisted = self.request('/api/feedback/comments?platform_work_id='+work['id'])['items']
        self.assertEqual({c['id']: c['category'] for c in persisted},
                         {comments[0]['id']: 'question', comments[1]['id']: 'sequel'})
        feedbacks = [f for f in app.STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)
                     if f.get('platform_work_id') == work['id']]
        fb_ids = [f['id'] for f in feedbacks]
        self.assertEqual(len(fb_ids), 2)

        # 3) insight：同步落库，证据 ID 全部真实，GET insights 立即可查。
        snapshots = [s for s in app.STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)
                     if s.get('platform_work_id') == work['id']]
        evidence_ids = fb_ids + [snapshots[0]['id']]
        with patch('providers.available', return_value={'llm': True}), \
                patch('providers.llm_json', return_value=self._good_insight_answer(evidence_ids)):
            insight = self.request('/api/feedback/insight', 'POST',
                                   {'project_id': project['id']}, expected=201)
        item = insight['item']
        known = {s['id'] for s in snapshots} | set(fb_ids)
        self.assertTrue(set(item['evidence_ids']) <= known)  # 证据 ID 全部真实
        self.assertEqual(item['status'], 'candidate')
        self.assertEqual(item['evidence_count'], len(evidence_ids))
        listed = self.request('/api/feedback/insights?project_id='+project['id'])
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['items'][0]['id'], item['id'])

        # 4) overview 汇总数量正确。
        overview = self.request('/api/feedback/overview?project_id='+project['id'])
        self.assertEqual(overview['platform_work_count'], 1)
        self.assertEqual(len(overview['platform_works']), 1)
        self.assertEqual(overview['comments_count'], 2)
        self.assertEqual(overview['insights_count'], 1)
        self.assertEqual(overview['latest_snapshot_at'], COLLECTED_AT_1)
        self.assertEqual(len(overview['latest_snapshots']), 3)  # play/like/share

    # ---- 路由 ----

    def test_feedback_routes_guard_and_shape(self):
        project = make_project(app.STORE)
        self.request('/api/feedback/works?project_id='+project['id'])  # 空列表合法
        self.request('/api/feedback/overview', expected=400)
        self.request('/api/feedback/comments', expected=404)  # 缺 platform_work_id
        self.request('/api/feedback/insight', 'POST', {}, expected=400)
        self.request('/api/feedback/classify-comments', 'POST', {}, expected=404)
        self.request('/api/feedback/overview', 'POST', {}, expected=404)
        self.request('/api/feedback/nope?project_id='+project['id'], expected=404)
        # 跨站写入仍被拒绝
        self.request('/api/feedback/collect', 'POST', {'project_id': project['id']},
                     headers={'Origin': 'http://evil.example'}, expected=403)


if __name__ == '__main__':
    unittest.main()
