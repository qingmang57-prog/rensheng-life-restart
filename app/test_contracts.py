"""contracts.py 的隔离测试：只测数据契约与状态机，不启动服务、不碰存储层。"""
import json
import unittest

import contracts as c


class CreationTests(unittest.TestCase):
    def test_bench_account_created(self):
        rec = c.create_bench_account(platform='douyin', display_name='参考账号',
                                     home_url='https://www.douyin.com/user/abc',
                                     external_id='abc', follower_count=1200, works_count=None)
        self.assertEqual(rec['kind'] if 'kind' in rec else 'bench_account', 'bench_account')
        self.assertEqual(rec['platform'], 'douyin')
        json.dumps(rec, ensure_ascii=False)

    def test_bench_work_created_with_null_metrics(self):
        rec = c.create_bench_work(bench_account_id='bench_account_x', platform='douyin',
                                  platform_video_id='7300000000000000001', title='标题',
                                  source_url='https://www.douyin.com/video/1',
                                  like_count=None, play_count=None)
        self.assertEqual(rec['platform_video_id'], '7300000000000000001')
        self.assertIsNone(rec['like_count'])

    def test_bench_comment_links_work(self):
        rec = c.create_bench_comment(bench_work_id='bench_work_x', platform='douyin',
                                     platform_comment_id='c1', author_name='路人',
                                     content='说得好', like_count=3)
        self.assertEqual(rec['bench_work_id'], 'bench_work_x')

    def test_topic_from_bench_work(self):
        rec = c.create_topic(title='选题', bench_work_id='bench_work_x')
        self.assertEqual(rec['status'], 'candidate')

    def test_topic_from_knowledge(self):
        rec = c.create_topic(title='选题', knowledge_id='knowledge_x')
        self.assertEqual(rec['knowledge_id'], 'knowledge_x')

    def test_platform_account_created(self):
        rec = c.create_platform_account(platform='douyin', display_name='本人号',
                                        session_ref='session:owner_douyin',
                                        capabilities=['video_publish'], status='connected')
        self.assertEqual(rec['session_ref'], 'session:owner_douyin')
        self.assertIn('video_publish', rec['capabilities'])

    def test_matrix_publish_task_created_as_draft(self):
        rec = c.create_matrix_publish_task(project_id='project_x', artifact_id='artifact_x',
                                           script_version_id='script_x',
                                           platform_account_id='platform_account_x',
                                           idempotency_key='idem-1',
                                           platform_payload={'title': '标题'})
        self.assertEqual(rec['status'], 'draft')
        self.assertIsNone(rec['platform_work_id'])

    def test_platform_work_created(self):
        rec = c.create_platform_work(platform_account_id='platform_account_x',
                                     platform='douyin', platform_video_id='7400000000000000001',
                                     title='标题', source_url='https://www.douyin.com/video/2',
                                     publish_task_id='publish_task_x')
        self.assertEqual(rec['publish_task_id'], 'publish_task_x')

    def test_metric_snapshot_created(self):
        rec = c.create_metric_snapshot(metric_key='douyin.like_count', value=100, unit='count',
                                       collected_at='2026-10-27T08:00:00.000Z',
                                       source='platform_api')
        self.assertEqual(rec['value'], 100)

    def test_comment_feedback_created(self):
        rec = c.create_comment_feedback(bench_comment_id='bench_comment_x',
                                        bench_work_id='bench_work_x', feedback_type='like')
        self.assertEqual(rec['feedback_type'], 'like')

    def test_review_insight_created_with_ref(self):
        rec = c.create_review_insight(title='发现', body='正文', insight_type='data',
                                      bench_work_id='bench_work_x')
        self.assertEqual(rec['insight_type'], 'data')

    def test_digital_human_provider_created(self):
        rec = c.create_digital_human_provider(name='某数字人', provider_key='vendor_a',
                                              capabilities=['avatar_video'])
        self.assertEqual(rec['status'], 'active')

    def test_digital_human_job_created(self):
        rec = c.create_digital_human_job(provider_id='dh_provider_x', script_version_id='script_x',
                                         asset_id=None, request={'text': '稿'},
                                         status='pending', result=None, error=None)
        self.assertEqual(rec['status'], 'pending')

    def test_digital_human_asset_created(self):
        rec = c.create_digital_human_asset(provider_id='dh_provider_x', job_id='dh_job_x',
                                           asset_kind='video', uri='dh://asset/1', duration_ms=5000)
        self.assertEqual(rec['asset_kind'], 'video')


class RequiredFieldTests(unittest.TestCase):
    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            c.validate_record('no_such_kind', {})

    def test_bench_work_missing_required(self):
        with self.assertRaises(ValueError):
            c.create_bench_work(bench_account_id='a', platform='douyin',
                                platform_video_id='1', title='')

    def test_bench_comment_missing_work_link(self):
        with self.assertRaises(ValueError):
            c.create_bench_comment(bench_work_id='', platform='douyin',
                                   platform_comment_id='c1', author_name='x', content='y')

    def test_topic_requires_source_ref(self):
        with self.assertRaises(ValueError):
            c.create_topic(title='无来源选题')

    def test_missing_collected_at_rejected(self):
        with self.assertRaises(ValueError):
            c.validate_record(c.KIND_BENCH_WORK, {
                'bench_account_id': 'a', 'platform': 'douyin',
                'platform_video_id': '1', 'title': 't', 'source_url': ''})

    def test_source_url_key_must_exist(self):
        with self.assertRaises(ValueError):
            c.validate_record(c.KIND_BENCH_WORK, {
                'bench_account_id': 'a', 'platform': 'douyin', 'platform_video_id': '1',
                'title': 't', 'collected_at': c.now_ms()})

    def test_platform_account_missing_capabilities(self):
        with self.assertRaises(ValueError):
            c.create_platform_account(platform='douyin', display_name='x',
                                      session_ref='ref', capabilities=[])


class TypeAndRangeTests(unittest.TestCase):
    def test_negative_metric_rejected(self):
        with self.assertRaises(ValueError):
            c.create_bench_work(bench_account_id='a', platform='douyin',
                                platform_video_id='1', title='t', like_count=-5)

    def test_bool_metric_rejected(self):
        with self.assertRaises(ValueError):
            c.create_bench_comment(bench_work_id='w', platform='douyin',
                                   platform_comment_id='c', author_name='a',
                                   content='b', like_count=True)

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            c.create_bench_account(platform='douyin', display_name='x',
                                   collected_at='2026-10-27 08:00:00')

    def test_bad_enum_rejected(self):
        with self.assertRaises(ValueError):
            c.create_topic(title='t', bench_work_id='w', status='weird')

    def test_numeric_id_rejected(self):
        with self.assertRaises(ValueError):
            c.create_bench_work(bench_account_id='a', platform='douyin',
                                platform_video_id=7300000000000000001, title='t')

    def test_abs_user_path_rejected(self):
        with self.assertRaises(ValueError):
            c.create_platform_account(platform='douyin', display_name='x',
                                      session_ref=r'/tmp/someone/sess.json',
                                      capabilities=[])

    def test_metric_snapshot_value_must_be_number(self):
        with self.assertRaises(ValueError):
            c.create_metric_snapshot(metric_key='k', value='many', unit='count',
                                     collected_at=c.now_ms(), source='s')


class PublishSuccessTests(unittest.TestCase):
    def _task(self):
        return c.create_matrix_publish_task(project_id='p', artifact_id='a',
                                            script_version_id='s',
                                            platform_account_id='pa',
                                            idempotency_key='k',
                                            platform_payload={'title': 't'})

    def test_success_requires_helper(self):
        task = self._task()
        with self.assertRaises(ValueError):
            c.validate_record(c.KIND_MATRIX_PUBLISH_TASK,
                              {**task, 'status': 'published',
                               'platform_work_id': 'pw', 'platform_url': ''})

    def test_cannot_fabricate_published_record(self):
        task = self._task()
        with self.assertRaises(ValueError):
            c.validate_record(c.KIND_MATRIX_PUBLISH_TASK,
                              {**task, 'status': 'published'})

    def test_set_publish_success(self):
        task = self._task()
        done = c.set_publish_success(task, platform_work_id='platform_work_1',
                                     url='https://www.douyin.com/video/3',
                                     raw={'published': True})
        self.assertEqual(done['status'], 'published')
        self.assertEqual(done['platform_work_id'], 'platform_work_1')
        self.assertEqual(done['platform_url'], 'https://www.douyin.com/video/3')
        json.dumps(done, ensure_ascii=False)

    def test_set_publish_success_requires_url(self):
        with self.assertRaises(ValueError):
            c.set_publish_success(self._task(), platform_work_id='pw', url='')

    def test_digital_human_success_requires_result_asset(self):
        job = c.create_digital_human_job(provider_id='p', script_version_id='s',
                                         asset_id=None, request={'text': 'x'})
        with self.assertRaises(ValueError):
            c.validate_record(c.KIND_DIGITAL_HUMAN_JOB,
                              {**job, 'status': 'succeeded', 'result': {}})
        done = c.set_digital_human_success(job, result_asset_id='dh_asset_1')
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(done['result']['result_asset_id'], 'dh_asset_1')


class MetricSnapshotAppendOnlyTests(unittest.TestCase):
    def test_snapshots_at_distinct_times_do_not_overwrite(self):
        s1 = c.create_metric_snapshot(metric_key='douyin.like_count', value=100, unit='count',
                                      collected_at='2026-10-27T08:00:00.000Z',
                                      source='api', platform_work_id='pw')
        s2 = c.create_metric_snapshot(metric_key='douyin.like_count', value=150, unit='count',
                                      collected_at='2026-10-27T09:00:00.000Z',
                                      source='api', platform_work_id='pw')
        self.assertNotEqual(s1['id'], s2['id'])
        self.assertEqual(s1['value'], 100)
        self.assertEqual(s2['value'], 150)
        self.assertEqual(s1['collected_at'], '2026-10-27T08:00:00.000Z')


class StateMachineTests(unittest.TestCase):
    def test_publish_happy_path(self):
        task = c.create_matrix_publish_task(project_id='p', artifact_id='a',
                                            script_version_id='s',
                                            platform_account_id='pa',
                                            idempotency_key='k',
                                            platform_payload={})
        t1 = c.transition(task, 'publish', 'queued')
        t2 = c.transition(t1, 'publish', 'running')
        self.assertEqual(t2['status'], 'running')
        self.assertEqual(task['status'], 'draft')

    def test_publish_illegal_direct_jump(self):
        task = c.create_matrix_publish_task(project_id='p', artifact_id='a',
                                            script_version_id='s',
                                            platform_account_id='pa',
                                            idempotency_key='k',
                                            platform_payload={})
        with self.assertRaises(ValueError):
            c.check_transition('publish', 'draft', 'published')

    def test_terminal_state_is_absorbing(self):
        self.assertFalse(c.can_transition('publish', 'published', 'failed'))
        with self.assertRaises(ValueError):
            c.check_transition('digital_human', 'succeeded', 'running')

    def test_collection_machine(self):
        rec = {'status': 'pending'}
        rec = c.transition(rec, 'collection', 'collecting')
        rec = c.transition(rec, 'collection', 'collected')
        self.assertEqual(rec['status'], 'collected')
        with self.assertRaises(ValueError):
            c.check_transition('collection', 'collected', 'pending')

    def test_collection_retry_after_failure(self):
        self.assertTrue(c.can_transition('collection', 'failed', 'pending'))

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            c.can_transition('publish', 'wat', 'queued')
        with self.assertRaises(ValueError):
            c.can_transition('no_such_machine', 'a', 'b')


if __name__ == '__main__':
    unittest.main()
