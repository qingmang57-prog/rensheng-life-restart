"""对标账号库→作品/热评→选题池：第一阶段后端测试。全部使用真实形状的 fixture 数据，
不联网、不启动真实采集；浏览器采集不可用时验证其真实失败而非假成功。"""
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
TEST_PATH = TEST_ROOT/('bench-'+uuid.uuid4().hex)
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
for name in ('IP_LLM_URL','IP_LLM_MODEL','IP_ASR_URL','IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import bench_collect
import bench_service
import contracts
from db import Store

FIXTURE_URL = 'https://www.douyin.com/user/MS4wLjABAAAAFIXTURESECUID'
FIXTURE_EXTERNAL_ID = 'MS4wLjABAAAAFIXTURESECUID'
# 真实采集 JSON 形状（= DouyinBenchCollector 输出 / RawBenchImporter 消费格式）。
FIXTURE_RAW = {
    'source_url': FIXTURE_URL,
    'collected_at': '2026-10-27T08:00:00.000Z',
    'account': {'platform': 'douyin', 'external_id': FIXTURE_EXTERNAL_ID,
                'display_name': '极地测试号', 'follower_count': 12345,
                'following_count': 88, 'works_count': 42},
    'works': [
        {'platform_video_id': '7300000000000000001', 'title': '第一条作品标题',
         'source_url': 'https://www.douyin.com/video/7300000000000000001',
         'published_at': 1780000000, 'duration_ms': 45000,
         'like_count': 1500, 'comment_count': 32, 'share_count': 10,
         'collect_count': 80, 'play_count': None,
         'comments': [
             {'platform_comment_id': 'c-1001', 'author_name': '读者甲',
              'content': '这个角度第一次见', 'like_count': 99, 'reply_count': 2,
              'parent_comment_id': None},
             {'platform_comment_id': 'c-1002', 'author_name': '读者乙',
              'content': '收藏了，求续集', 'like_count': 45, 'reply_count': 0,
              'parent_comment_id': None}]},
        {'platform_video_id': '7300000000000000002', 'title': '第二条作品标题',
         'source_url': 'https://www.douyin.com/video/7300000000000000002',
         'published_at': 1780100000, 'duration_ms': 30000,
         'like_count': None, 'comment_count': 5, 'share_count': None,
         'collect_count': None, 'play_count': None, 'comments': []}]}

NO_COMMENT_RAW = {
    'source_url': 'https://www.douyin.com/user/MS4wLjABAAAANOCOMMENT',
    'collected_at': '2026-10-27T09:00:00.000Z',
    'account': {'platform': 'douyin', 'external_id': 'MS4wLjABAAAANOCOMMENT',
                'display_name': '无评论测试号'},
    'works': [{'platform_video_id': '7400000000000000009', 'title': '无评论作品',
               'source_url': 'https://www.douyin.com/video/7400000000000000009',
               'published_at': 1780200000, 'like_count': 3, 'comments': []}]}


class BenchUnitTests(unittest.TestCase):
    """纯函数/导入器级校验，不启动服务。"""

    def test_home_url_validation(self):
        home, ext = bench_service.parse_home_url(FIXTURE_URL)
        self.assertEqual(ext, FIXTURE_EXTERNAL_ID)
        self.assertEqual(home, FIXTURE_URL)
        for bad in ('http://www.douyin.com/user/abc',
                    'https://www.douyin.com/video/12345678901',
                    'https://example.com/user/abc',
                    'https://www.douyin.com/',
                    '', None, 'not-a-url'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                bench_service.parse_home_url(bad)

    def test_metric_null_rules(self):
        self.assertEqual(bench_collect._metric(1500), 1500)
        self.assertEqual(bench_collect._metric('1500'), 1500)
        self.assertEqual(bench_collect._metric(0), 0)
        for unknown in (None, '', '1.2万', True, -5, 1.5, 'abc'):
            self.assertIsNone(bench_collect._metric(unknown), repr(unknown))

    def test_published_at_conversion_and_invalid(self):
        iso = bench_collect._iso_ms(1780000000)
        self.assertRegex(iso, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
        for bad in (None, 0, -1, '1780000000', True):
            self.assertIsNone(bench_collect._iso_ms(bad), repr(bad))

    def test_import_rejects_http_source_url(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            raw = copy.deepcopy(FIXTURE_RAW)
            raw['source_url'] = 'http://www.douyin.com/user/abc'
            with self.assertRaises(ValueError):
                bench_collect.import_raw(store, raw)

    def test_import_rejects_missing_or_bad_collected_at(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            raw = copy.deepcopy(FIXTURE_RAW)
            raw.pop('collected_at')
            with self.assertRaises(ValueError):
                bench_collect.import_raw(store, raw)
            raw = copy.deepcopy(FIXTURE_RAW)
            raw['collected_at'] = '2026-10-27'
            with self.assertRaises(ValueError):
                bench_collect.import_raw(store, raw)

    def test_import_rejects_non_https_and_mismatched_account(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            raw = copy.deepcopy(FIXTURE_RAW)
            raw['source_url'] = 'https://attacker.example/user/x'
            with self.assertRaises(ValueError):
                bench_collect.import_raw(store, raw)
            raw = copy.deepcopy(FIXTURE_RAW)
            result = bench_collect.import_raw(store, raw)
            other = store.put('bench_account', contracts.create_bench_account(
                'douyin', '别的账号', home_url='https://www.douyin.com/user/OTHER',
                external_id='OTHER'))
            with self.assertRaises(ValueError):
                bench_collect.import_raw(store, raw, account_id=other['id'])
            self.assertIsNotNone(result['account'])

    def test_import_fixture_profile_works_comments(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            result = bench_collect.import_raw(store, copy.deepcopy(FIXTURE_RAW))
            self.assertTrue(result['account_created'])
            self.assertEqual(result['works_total'], 2)
            self.assertEqual(result['works_created'], 2)
            self.assertEqual(result['comments_created'], 2)
            account = result['account']
            self.assertEqual(account['display_name'], '极地测试号')
            self.assertEqual(account['follower_count'], 12345)
            works = store.list('bench_work')
            self.assertEqual(len(works), 2)
            first = next(w for w in works if w['platform_video_id'] == '7300000000000000001')
            self.assertEqual(first['like_count'], 1500)
            self.assertEqual(first['duration_ms'], 45000)
            self.assertIsNotNone(first['published_at'])
            self.assertEqual(first['raw']['platform_video_id'], '7300000000000000001')
            second = next(w for w in works if w['platform_video_id'] == '7300000000000000002')
            self.assertIsNone(second['like_count'])
            self.assertIsNone(second['share_count'])
            self.assertIsNone(second['play_count'])
            comments = store.list('bench_comment')
            self.assertEqual(len(comments), 2)
            self.assertTrue(all(c['bench_work_id'] == first['id'] for c in comments))
            self.assertIn('第一次见', comments[0]['content'] + comments[1]['content'])

    def test_reimport_is_deduped_and_history_kept(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as d:
            store = Store(d)
            first = bench_collect.import_raw(store, copy.deepcopy(FIXTURE_RAW))
            second = bench_collect.import_raw(store, copy.deepcopy(FIXTURE_RAW))
            self.assertTrue(first['account_created'])
            self.assertFalse(second['account_created'])
            self.assertEqual(second['works_created'], 0)
            self.assertEqual(second['comments_created'], 0)
            self.assertEqual(len(store.list('bench_work')), 2)
            self.assertEqual(len(store.list('bench_comment')), 2)
            self.assertEqual(len(store.list('bench_account')), 1)

    # ---- 已核实接口：响应五类分类与字段映射（GitHub 一手源码，2026-10-27）----

    def test_classify_empty_shell_is_auth_required(self):
        # 17 字节空壳 {"status_code":0}：游客拒绝，不是"没有作品"
        shell = json.loads('{"status_code":0}')
        self.assertEqual(bench_collect.classify_api_response(200, shell),
                         bench_collect.AUTH_REQUIRED)
        self.assertEqual(bench_collect.classify_api_response(200, {'status_code': 0, 'log_pb': {}}),
                         bench_collect.AUTH_REQUIRED)

    def test_classify_has_more_zero_is_empty_end(self):
        end_post = {'aweme_list': [], 'max_cursor': 40, 'has_more': 0, 'status_code': 0}
        self.assertEqual(bench_collect.classify_api_response(200, end_post),
                         bench_collect.EMPTY_END)
        end_comment = {'comments': [], 'cursor': 30, 'has_more': 0, 'total': 3, 'status_code': 0}
        self.assertEqual(bench_collect.classify_api_response(200, end_comment),
                         bench_collect.EMPTY_END)

    def test_classify_risk_control(self):
        for status in (403, 429, 444, 500):
            self.assertEqual(bench_collect.classify_api_response(status, None),
                             bench_collect.RISK_CONTROL, status)
        self.assertEqual(bench_collect.classify_api_response(
            200, '<html>Argus risk control</html>'), bench_collect.RISK_CONTROL)
        self.assertEqual(bench_collect.classify_api_response(
            200, '{"captcha": "verify"}'), bench_collect.RISK_CONTROL)

    def test_classify_not_found_and_signature_broken_and_ok(self):
        self.assertEqual(bench_collect.classify_api_response(404, None),
                         bench_collect.NOT_FOUND)
        self.assertEqual(bench_collect.classify_api_response(
            200, {'status_code': 8, 'status_msg': '签名错误'}), bench_collect.SIGNATURE_BROKEN)
        self.assertEqual(bench_collect.classify_api_response(200, None),
                         bench_collect.SIGNATURE_BROKEN)
        ok_post = {'aweme_list': [{'aweme_id': '1'}], 'has_more': 1, 'status_code': 0}
        self.assertEqual(bench_collect.classify_api_response(200, ok_post), bench_collect.OK)
        ok_comment = {'comments': [{'cid': '1'}], 'has_more': 0, 'total': 1, 'status_code': 0}
        self.assertEqual(bench_collect.classify_api_response(200, ok_comment), bench_collect.OK)

    def test_verified_endpoint_constants_and_comment_mapping(self):
        # 已核实路径（MediaCrawler douyin client.py / Evil0ctal dtk endpoints.py）
        self.assertEqual(bench_collect.USER_POST_API_PATH, '/aweme/v1/web/aweme/post/')
        self.assertEqual(bench_collect.COMMENT_API_PATH, '/aweme/v1/web/comment/list/')
        self.assertEqual(bench_collect.COMMENT_REPLY_API_PATH,
                         '/aweme/v1/web/comment/list/reply/')
        item = {'cid': '73000000000000000010001', 'text': '说得对', 'digg_count': 66,
                'reply_comment_total': 4, 'reply_id': '0',
                'user': {'nickname': '评论者丙'}}
        parsed = bench_collect._parse_comment_item(item)
        self.assertEqual(parsed['platform_comment_id'], '73000000000000000010001')
        self.assertEqual(parsed['author_name'], '评论者丙')
        self.assertEqual(parsed['content'], '说得对')
        self.assertEqual(parsed['like_count'], 66)
        self.assertEqual(parsed['reply_count'], 4)
        self.assertIsNone(parsed['parent_comment_id'])  # reply_id '0' → 顶层评论
        item['reply_id'] = '73000000000000000010000'
        parsed = bench_collect._parse_comment_item(item)
        self.assertEqual(parsed['parent_comment_id'], '73000000000000000010000')

    # ---- 低风险路线：页面已签名响应的纯解析（不联网）----

    def _hit(self, url, payload):
        return {'url': url, 'status': 200, 'json': payload}

    def test_split_hits_groups_and_dedups(self):
        post_url = 'https://www.douyin.com/aweme/v1/web/aweme/post/?max_cursor=0'
        hits = [self._hit('https://www.douyin.com/aweme/v1/web/user/profile/other/?x=1',
                          {'user': {}}),
                self._hit(post_url, {'aweme_list': []}),
                self._hit(post_url, {'aweme_list': []}),  # 同 URL 去重
                self._hit('https://www.douyin.com/aweme/v1/web/comment/list/?cursor=0',
                          {'comments': []}),
                self._hit('https://www.douyin.com/other/api', {'x': 1})]
        grouped = bench_collect.split_hits(hits)
        self.assertEqual(len(grouped['profile']), 1)
        self.assertEqual(len(grouped['post']), 1)
        self.assertEqual(len(grouped['comment']), 1)
        self.assertEqual(bench_collect.split_hits([]),
                         {'profile': [], 'post': [], 'comment': []})

    def test_extract_profile_fields(self):
        hits = [
            self._hit('u1', {'user': {'nickname': '旧名字', 'follower_count': 10}}),
            self._hit('u2', {'user': {'nickname': '真名', 'follower_count': 12345,
                                      'following_count': 88, 'aweme_count': 42}}),
        ]
        fields = bench_collect.extract_profile_fields(hits)
        self.assertEqual(fields['display_name'], '真名')  # 取最新一条
        self.assertEqual(fields['follower_count'], 12345)
        self.assertEqual(fields['following_count'], 88)
        self.assertEqual(fields['works_count'], 42)
        empty = bench_collect.extract_profile_fields([self._hit('u3', {'user': {}})])
        self.assertEqual(empty, {'display_name': None, 'follower_count': None,
                                 'following_count': None, 'works_count': None})
        self.assertEqual(bench_collect.extract_profile_fields(None)['display_name'], None)

    def test_extract_work_items_dedup_and_order(self):
        item1 = {'aweme_id': '7686488847220355171', 'desc': '第一条'}
        item2 = {'aweme_id': '7686488847220355172', 'desc': '第二条'}
        hits = [self._hit('p1', {'aweme_list': [item1], 'has_more': 1}),
                self._hit('p2', {'aweme_list': [item1, item2], 'has_more': 1}),
                self._hit('bad', 'not-a-dict-payload'),
                self._hit('nokey', {'status_code': 0})]
        items = bench_collect.extract_work_items(hits)
        self.assertEqual([x['aweme_id'] for x in items],
                         ['7686488847220355171', '7686488847220355172'])

    def test_extract_comment_items_cursor_merge(self):
        def comment(cid, text):
            return {'cid': cid, 'text': text, 'digg_count': 1,
                    'user': {'nickname': 'u'}}
        hits = [self._hit('c0?cursor=0&count=5',
                          {'comments': [comment('1', '一'), comment('2', '二')],
                           'cursor': 5, 'has_more': 1}),
                self._hit('c5?cursor=5&count=5',
                          {'comments': [comment('2', '二重复'), comment('3', '三')],
                           'cursor': 10, 'has_more': 0})]
        items = bench_collect.extract_comment_items(hits, 30)
        self.assertEqual([x['cid'] for x in items], ['1', '2', '3'])
        self.assertEqual(items[1]['text'], '二')  # 先去重者优先
        capped = bench_collect.extract_comment_items(hits, 2)
        self.assertEqual(len(capped), 2)

    def test_build_home_result_no_hits_is_real_failure(self):
        with self.assertRaises(bench_collect.CollectionFailed) as ctx:
            bench_collect.build_home_result([], [])
        self.assertTrue(ctx.exception.needs_action)
        self.assertEqual(ctx.exception.classification, bench_collect.AUTH_REQUIRED)
        self.assertIn('未自动加载', str(ctx.exception))

    def test_build_home_result_shell_and_empty_end(self):
        shell = [self._hit('p', {'status_code': 0})]
        with self.assertRaises(bench_collect.CollectionFailed) as ctx:
            bench_collect.build_home_result(shell, [])
        self.assertEqual(ctx.exception.classification, bench_collect.AUTH_REQUIRED)
        end = [self._hit('p', {'aweme_list': [], 'has_more': 0, 'status_code': 0})]
        with self.assertRaises(bench_collect.CollectionFailed) as ctx:
            bench_collect.build_home_result(end, [])
        self.assertEqual(ctx.exception.classification, bench_collect.EMPTY_END)
        self.assertTrue(ctx.exception.needs_action)

    def test_build_home_result_ok_uses_profile(self):
        post = [self._hit('p1', {'aweme_list': [{'aweme_id': '7686488847220355171'}],
                                  'has_more': 1, 'status_code': 0})]
        profile = [self._hit('u', {'user': {'nickname': 'YuE悦', 'follower_count': 100}})]
        items, fields = bench_collect.build_home_result(post, profile)
        self.assertEqual(len(items), 1)
        self.assertEqual(fields['display_name'], 'YuE悦')
        self.assertEqual(fields['follower_count'], 100)


class BenchAPITests(unittest.TestCase):
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

    def create_account(self, url=FIXTURE_URL):
        try:
            return self.request('/api/bench/accounts', 'POST', {'url': url}, expected=201)
        except AssertionError:
            # 同一 URL 在本运行内已被其他用例建过：复用 200
            return self.request('/api/bench/accounts', 'POST', {'url': url}, expected=200)

    def import_fixture(self, account_id, raw=FIXTURE_RAW):
        job = self.request('/api/bench/accounts/'+account_id+'/collect', 'POST',
                           {'raw': copy.deepcopy(raw)}, expected=202)
        return self.wait_job(job['id'], 'succeeded')

    def wait_job(self, job_id, want=None, deadline=10):
        end = time.time()+deadline
        while time.time() < end:
            current = self.request('/api/jobs/'+job_id)
            if current['status'] not in ('queued', 'running'):
                if want:
                    self.assertEqual(current['status'], want, current.get('error'))
                return current
            time.sleep(0.05)
        self.fail('任务没有在限定时间内结束：'+job_id)

    # ---- 账号 ----

    def test_create_account_rejects_invalid_urls(self):
        for bad in ('http://www.douyin.com/user/abc',
                    'https://www.douyin.com/video/12345678901',
                    'https://example.com/user/abc', ''):
            self.request('/api/bench/accounts', 'POST', {'url': bad}, expected=400)
        self.request('/api/bench/accounts', 'POST', {}, expected=400)

    def test_create_account_and_reuse(self):
        unique_url = 'https://www.douyin.com/user/MS4wLjABAAAAUNIQUE0001'
        account = self.create_account(unique_url)
        self.assertFalse(account['reused'])
        self.assertEqual(account['platform'], 'douyin')
        self.assertEqual(account['external_id'], 'MS4wLjABAAAAUNIQUE0001')
        self.assertEqual(account['home_url'], unique_url)
        self.assertIsNone(account.get('follower_count'))
        again = self.request('/api/bench/accounts', 'POST', {'url': unique_url}, expected=200)
        self.assertTrue(again['reused'])
        self.assertEqual(again['id'], account['id'])
        items = self.request('/api/bench/accounts')['items']
        self.assertEqual(len([x for x in items if x['id'] == account['id']]), 1)

    def test_route_guards(self):
        # 跨站写入被拒绝
        self.request('/api/bench/accounts', 'POST', {'url': FIXTURE_URL},
                     headers={'Origin': 'http://evil.example'}, expected=403)
        # 错误方法/路径 404
        self.request('/api/bench/topics/from-evidence', 'GET', expected=404)
        self.request('/api/bench/accounts/xxx/collect', 'GET', expected=404)
        # 坏 JSON 400
        req = urllib.request.Request(self.base+'/api/bench/accounts', b'{not-json',
                                     {'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        self.assertEqual(ctx.exception.code, 400)

    # ---- 采集 job ----

    def test_collect_job_fails_honestly_without_browser(self):
        account = self.create_account()
        missing = TEST_PATH/'no-such-browser-env'/'python.exe'
        with patch('bench_collect.video_intake.browser_python', return_value=missing):
            job = self.request('/api/bench/accounts/'+account['id']+'/collect', 'POST', {}, expected=202)
            current = self.wait_job(job['id'], 'failed')
        self.assertIn('浏览器采集环境未安装', current['error'])
        self.assertEqual(self.request('/api/bench/works?account_id='+account['id'])['total'], 0)

    def test_collect_job_is_idempotent(self):
        account = self.create_account(url='https://www.douyin.com/user/MS4wLjABAAAAIDEMPOTENT')
        extra = copy.deepcopy(FIXTURE_RAW)
        extra['source_url'] = 'https://www.douyin.com/user/MS4wLjABAAAAIDEMPOTENT'
        extra['account'] = {'platform': 'douyin', 'external_id': 'MS4wLjABAAAAIDEMPOTENT',
                            'display_name': '幂等测试号'}
        extra['works'] = extra['works'][:1] + [{
            'platform_video_id': '7300000000000000003', 'title': '幂等测试新增作品',
            'source_url': 'https://www.douyin.com/video/7300000000000000003',
            'published_at': 1780300000, 'like_count': 7, 'comments': []}]
        def slow_collect(home_url, **kw):
            time.sleep(1.0)
            return copy.deepcopy(extra)
        with patch.object(bench_collect.DouyinBenchCollector, 'collect_account', side_effect=slow_collect):
            first = self.request('/api/bench/accounts/'+account['id']+'/collect', 'POST', {}, expected=202)
            second = self.request('/api/bench/accounts/'+account['id']+'/collect', 'POST', {}, expected=202)
            self.assertEqual(first['id'], second['id'])
            current = self.wait_job(first['id'], 'succeeded', deadline=15)
        self.assertEqual(current['result']['works_created'], 2)
        works = self.request('/api/bench/works?account_id='+account['id']+'&page_size=50')['items']
        self.assertEqual(len(works), 2)
        self.assertIn('7300000000000000003', {w['platform_video_id'] for w in works})

    def test_collect_job_rejects_mismatched_raw_account(self):
        account = self.create_account(url='https://www.douyin.com/user/SOMEOTHERSECUID')
        job = self.request('/api/bench/accounts/'+account['id']+'/collect', 'POST',
                           {'raw': copy.deepcopy(FIXTURE_RAW)}, expected=202)
        current = self.wait_job(job['id'], 'failed')
        self.assertIn('不一致', current['error'])

    def test_collect_job_imports_raw_fixture(self):
        account = self.create_account()
        current = self.import_fixture(account['id'])
        self.assertEqual(current['result']['works_created'], 2)
        self.assertEqual(current['result']['comments_created'], 2)
        fresh = next(x for x in self.request('/api/bench/accounts')['items']
                     if x['id'] == account['id'])
        self.assertEqual(fresh['display_name'], '极地测试号')
        self.assertEqual(fresh['follower_count'], 12345)
        self.assertEqual(fresh['works_count'], 42)

    # ---- 作品 / 评论列表 ----

    def test_works_list_metrics_null_and_pagination(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        works = self.request('/api/bench/works?account_id='+account['id']+'&page=1&page_size=1')
        self.assertEqual(works['total'], 2)
        self.assertEqual(len(works['items']), 1)
        page2 = self.request('/api/bench/works?account_id='+account['id']+'&page=2&page_size=1')
        self.assertEqual(len(page2['items']), 1)
        self.assertNotEqual(works['items'][0]['id'], page2['items'][0]['id'])
        all_ids = {works['items'][0]['platform_video_id'], page2['items'][0]['platform_video_id']}
        self.assertEqual(all_ids, {'7300000000000000001', '7300000000000000002'})
        second = page2['items'][0] if page2['items'][0]['like_count'] is None else works['items'][0]
        self.assertIsNone(second['like_count'])
        self.assertIsNone(second['play_count'])
        self.assertIsNone(second['share_count'])
        first = works['items'][0] if works['items'][0]['like_count'] == 1500 else page2['items'][0]
        self.assertEqual(first['collect_count'], 80)
        self.assertRegex(first['published_at'], r'^\d{4}-\d{2}-\d{2}T')
        self.assertEqual(self.request('/api/bench/works?account_id='+account['id'])['total'], 2)
        self.request('/api/bench/works?account_id=nope', expected=404)

    def test_comments_list(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        works = self.request('/api/bench/works?account_id='+account['id']+'&page_size=50')['items']
        first = next(w for w in works if w['platform_video_id'] == '7300000000000000001')
        result = self.request('/api/bench/comments?work_id='+first['id']+'&page=1&page_size=10')
        self.assertEqual(result['total'], 2)
        texts = ''.join(c['content'] for c in result['items'])
        self.assertIn('第一次见', texts)
        self.assertIn('求续集', texts)
        self.assertTrue(all(c['bench_work_id'] == first['id'] for c in result['items']))
        self.assertTrue(all(c['like_count'] is not None for c in result['items']))
        self.request('/api/bench/comments?page=1', expected=400)

    def test_reimport_dedup_via_api(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        current = self.import_fixture(account['id'])
        self.assertEqual(current['result']['works_created'], 0)
        self.assertEqual(current['result']['comments_created'], 0)
        self.assertEqual(self.request('/api/bench/works?account_id='+account['id'])['total'], 2)

    # ---- 选题 ----

    def _imported_work(self, account_id, video_id):
        works = self.request('/api/bench/works?account_id='+account_id+'&page_size=50')['items']
        return next(w for w in works if w['platform_video_id'] == video_id)

    def test_topics_from_evidence_success_with_sources(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        work = self._imported_work(account['id'], '7300000000000000001')
        comments = self.request('/api/bench/comments?work_id='+work['id']+'&page_size=50')['items']
        answer = {'topics': [
            {'title': '选题一：换个角度谈第一条', 'angle': '评论高频词切入', 'thesis': '观众对新角度敏感',
             'evidence_work_ids': [work['id']],
             'evidence_comment_ids': [comments[0]['id'], comments[1]['id']]},
            {'title': '选题二：续集承诺', 'angle': '从求评论切入', 'thesis': '系列化内容有需求',
             'evidence_work_ids': [work['id']], 'evidence_comment_ids': [comments[1]['id']]}]}
        with patch('providers.available', return_value={'llm': True}), \
             patch('providers.llm_json', return_value=answer) as llm:
            result = self.request('/api/bench/topics/from-evidence',
                                  'POST', {'work_ids': [work['id']], 'max_topics': 8}, expected=201)
        payload = llm.call_args[0][1]
        self.assertIn('works', payload)
        self.assertEqual(len(payload['comments']), 2)
        topics = result['items']
        self.assertEqual(len(topics), 2)
        comment_ids = {c['id'] for c in comments}
        for topic in topics:
            self.assertIn('模型推断', topic['markers'])
            self.assertIn('待验证', topic['markers'])
            self.assertEqual(topic['validation'], 'unverified')
            self.assertEqual(topic['status'], 'candidate')
            self.assertEqual(topic['origin'], 'bench')
            self.assertTrue(set(topic['bench_work_ids']) <= {work['id']})
            self.assertTrue(set(topic['bench_comment_ids']) <= comment_ids)
            self.assertTrue(topic['bench_work_ids'])
        listed = self.request('/api/bench/topics')['items']
        self.assertEqual(len([t for t in listed if t['title'].startswith('选题')]), 2)

    def test_topics_reject_without_llm(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        work = self._imported_work(account['id'], '7300000000000000001')
        before = len(self.request('/api/bench/topics')['items'])
        with patch('providers.available', return_value={'llm': False}):
            self.request('/api/bench/topics/from-evidence',
                         'POST', {'work_ids': [work['id']]}, expected=409)
        self.assertEqual(len(self.request('/api/bench/topics')['items']), before)

    def test_topics_reject_when_no_comments(self):
        account = self.create_account(url='https://www.douyin.com/user/MS4wLjABAAAANOCOMMENT')
        self.import_fixture(account['id'], NO_COMMENT_RAW)
        work = self._imported_work(account['id'], '7400000000000000009')
        before = len(self.request('/api/bench/topics')['items'])
        with patch('providers.available', return_value={'llm': True}):
            self.request('/api/bench/topics/from-evidence',
                         'POST', {'work_ids': [work['id']]}, expected=400)
        self.assertEqual(len(self.request('/api/bench/topics')['items']), before)

    def test_topics_reject_unknown_or_empty_evidence(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        work = self._imported_work(account['id'], '7300000000000000001')
        before = len(self.request('/api/bench/topics')['items'])
        bad_cases = [
            {'topics': [{'title': '无来源选题', 'evidence_work_ids': [],
                         'evidence_comment_ids': []}]},
            {'topics': [{'title': '伪造来源选题', 'evidence_work_ids': ['bench_work_bogus'],
                         'evidence_comment_ids': []}]},
            {'topics': [{'title': '伪造评论来源', 'evidence_work_ids': [work['id']],
                         'evidence_comment_ids': ['bench_comment_bogus']}]},
            {'topics': [{'title': '冒充已发布', 'published': True,
                         'evidence_work_ids': [work['id']], 'evidence_comment_ids': []}]},
            {'topics': 'not-a-list'},
            {'topics': []},
        ]
        for bad in bad_cases:
            with patch('providers.available', return_value={'llm': True}), \
                 patch('providers.llm_json', return_value=bad):
                self.request('/api/bench/topics/from-evidence',
                             'POST', {'work_ids': [work['id']]}, expected=400)
        self.assertEqual(len(self.request('/api/bench/topics')['items']), before)

    def test_topics_reject_bad_request_shape(self):
        for bad in ({}, {'work_ids': []}, {'work_ids': 'x'}, {'work_ids': ['a'], 'max_topics': 99}):
            self.request('/api/bench/topics/from-evidence', 'POST', bad, expected=400)
        self.request('/api/bench/topics/from-evidence', 'POST',
                     {'work_ids': ['bench_work_missing']}, expected=404)

    # ---- 立项 ----

    def _make_topic(self):
        account = self.create_account()
        self.import_fixture(account['id'])
        work = self._imported_work(account['id'], '7300000000000000001')
        comments = self.request('/api/bench/comments?work_id='+work['id']+'&page_size=50')['items']
        answer = {'topics': [{'title': '立项测试选题',
                              'evidence_work_ids': [work['id']],
                              'evidence_comment_ids': [comments[0]['id']]}]}
        with patch('providers.available', return_value={'llm': True}), \
             patch('providers.llm_json', return_value=answer):
            result = self.request('/api/bench/topics/from-evidence',
                                  'POST', {'work_ids': [work['id']]}, expected=201)
        return result['items'][0]

    def test_start_project_links_and_is_idempotent(self):
        topic = self._make_topic()
        before = len(self.request('/api/bootstrap')['projects'])
        result = self.request('/api/bench/topics/'+topic['id']+'/start-project', 'POST', {}, expected=200)
        self.assertFalse(result['reused'])
        project = self.request('/api/projects/'+result['project_id'])
        self.assertEqual(project['topic_id'], topic['id'])
        self.assertEqual(project['title'], topic['title'])
        self.assertEqual(project['stage'], 'draft')
        self.assertEqual(len(project['draft']), 5)
        self.assertTrue(all(not p['text'] for p in project['draft']))
        again = self.request('/api/bench/topics/'+topic['id']+'/start-project', 'POST', {}, expected=200)
        self.assertTrue(again['reused'])
        self.assertEqual(again['project_id'], result['project_id'])
        after = len(self.request('/api/bootstrap')['projects'])
        self.assertEqual(after, before+1)
        listed = self.request('/api/bench/topics')
        linked = next(t for t in listed['items'] if t['id'] == topic['id'])
        self.assertIn('total', listed)
        self.assertEqual(linked['data']['project_id'], result['project_id'])
        self.assertEqual(linked['project_id'], result['project_id'])

    def test_start_project_does_not_overwrite_draft(self):
        topic = self._make_topic()
        result = self.request('/api/bench/topics/'+topic['id']+'/start-project', 'POST', {}, expected=200)
        project = self.request('/api/projects/'+result['project_id'])
        self.assertTrue(all(not p['text'] for p in project['draft']))
        self.assertIsNone(project['confirmed_version_id'])
        detail = self.request('/api/projects/'+result['project_id'])
        # 选题池立项的项目没有外部视频来源，下一步是直接完善五段稿。
        self.assertEqual(detail['next_action']['step'], 'script')
        self.request('/api/bench/topics/bench_topic_missing/start-project', 'POST', {}, expected=404)

    def test_accounts_carry_collect_status(self):
        fresh = self.create_account(url='https://www.douyin.com/user/MS4wLjABAAAAFRESHSTATUS')
        items = self.request('/api/bench/accounts')['items']
        self.assertTrue(items)
        for account in items:
            self.assertIn('collect_status', account)
            self.assertIn('collect_error', account)
            self.assertIn('status', account)  # 生命周期字段保留
        fresh_item = next(x for x in items if x['id'] == fresh['id'])
        self.assertEqual(fresh_item['collect_status'], 'none')
        self.assertIsNone(fresh_item['collect_error'])
        failed = next((x for x in items if x['collect_status'] == 'failed'), None)
        if failed is not None:
            self.assertTrue(failed['collect_error'])
        succeeded = next((x for x in items if x['collect_status'] == 'succeeded'), None)
        if succeeded is not None:
            self.assertIsNone(succeeded['collect_error'])

    def test_topics_list_pagination_shape(self):
        result = self.request('/api/bench/topics?page=1&page_size=2')
        for key in ('items', 'total', 'page', 'page_size'):
            self.assertIn(key, result)
        self.assertEqual(result['page'], 1)
        self.assertLessEqual(len(result['items']), 2)


if __name__ == '__main__':
    unittest.main()
