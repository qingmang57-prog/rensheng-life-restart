"""直剪自动链回归（试用反馈 B 方案 P0-3 / P0-4）。

用合成素材与替身渲染验证边界，不依赖真实视频/语音识别/真实账号：
- 230ms 时长差（31065 vs 30835）→ 标需复核、任务成功，而非直报失败；
- 时长差超出可播放容差 → 任务失败且中间文件保留；
- 词级部分为空 → 句级回退保留整句并报数，不静默丢句；
- 素材已更换 → 旧任务结果不挂到当前成片；
- 语音识别不可用 → 仍产出净剪版并如实说明。
"""
import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent / '.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT / uuid.uuid4().hex
TEST_PATH.mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
os.environ['IP_DISABLE_LOCAL_ASR'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import media
import packaging_job


def _mapping(segments):
    out, cursor = [], 0
    for s in segments:
        span = s['end_ms'] - s['start_ms']
        out.append({'start_ms': s['start_ms'], 'end_ms': s['end_ms'],
                    'output_start_ms': cursor, 'output_end_ms': cursor + span})
        cursor += span
    return out


class DirectPipelineTests(unittest.TestCase):
    def setUp(self):
        self.project = app.direct_create({'title': '直剪边界测试', 'is_test': True})
        self.asset = app.STORE.put('asset', {
            'project_id': self.project['id'], 'purpose': 'direct', 'filename': 'synthetic.mp4',
            'path': str(TEST_PATH / 'synthetic.mp4'), 'material_version': 1,
            'info': {'duration_ms': 40000, 'width': 1920, 'height': 1080, 'audio': True}})
        self.project = app.STORE.patch('project', self.project['id'],
            {'current_asset_id': self.asset['id'], 'material_version': 1},
            expected=self.project['revision'])

    def _run(self, probe_ms, segments=None, transcript=None, asr=False,
             capture_cues=None, stale=False):
        """同步跑一次直剪链；替身渲染只控时长，不做真实剪切/烧字幕。"""
        segments = segments if segments is not None else [{'start_ms': 0, 'end_ms': 30835}]
        mapping = _mapping(segments)
        if transcript is not None:
            app.STORE.put('transcript', {**transcript, 'asset_id': self.asset['id'],
                                         'project_id': self.project['id']})
        if stale:
            other = app.STORE.put('asset', {'project_id': self.project['id'], 'purpose': 'direct',
                'filename': 'new.mp4', 'path': str(TEST_PATH / 'new.mp4'), 'material_version': 2,
                'info': self.asset['info']})
            self.project = app.STORE.patch('project', self.project['id'],
                {'current_asset_id': other['id'], 'material_version': 2},
                expected=self.project['revision'])

        def fake_cut(path, info, segs, clean_path):
            Path(clean_path).write_bytes(b'fake-clean')
            return mapping

        def fake_caption(project, clean, cues, overlays, note, job_id, style=None, attach=True):
            if capture_cues is not None:
                capture_cues.extend(cues)
            return app.STORE.put('artifact', {'project_id': project['id'], 'kind': 'packaged_video',
                'asset_id': self.asset['id'], 'script_version_id': None,
                'path': clean['path'], 'info': clean['info'], 'caption_cues': cues})

        probe = {'duration_ms': probe_ms, 'width': 1920, 'height': 1080, 'audio': True,
                 'codec': 'h264', 'size': 10}
        with patch('app.POOL.submit', side_effect=lambda fn: fn()), \
             patch('media.silence_plan', return_value={'segments': segments, 'mode': 'conservative'}), \
             patch('media.render_cut', side_effect=fake_cut), \
             patch('media.probe', return_value=probe), \
             patch('media.extract_audio', return_value=None), \
             patch('providers.available', return_value={'asr': asr, 'llm': False}), \
             patch('app.render_caption_artifact', side_effect=fake_caption):
            job = app.direct_pipeline_job(self.project, self.asset, 'conservative')
        return app.STORE.get('job', job['id'])

    # ---- P0-4：时长差异分级 -------------------------------------------------
    def test_230ms_diff_marks_needs_review_and_succeeds(self):
        # 反馈原案：成片 31065ms vs 保留计划 30835ms，差 230ms。
        job = self._run(probe_ms=31065, segments=[{'start_ms': 0, 'end_ms': 30835}])
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        art = job['result']['artifact']
        self.assertTrue(art['needs_review'])
        self.assertEqual(art['duration_diff_ms'], 230)
        self.assertIn('需人工核对音画', job['result']['note'])

    def test_exact_duration_no_review_flag(self):
        job = self._run(probe_ms=30835, segments=[{'start_ms': 0, 'end_ms': 30835}])
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertFalse(job['result']['artifact']['needs_review'])
        self.assertEqual(job['result']['artifact']['duration_diff_ms'], 0)

    def test_small_diff_over_200_within_tolerance_succeeds(self):
        # 250ms：超过 200ms 复核线，但远小于 max(2000, 5%) 容差 → 成功+需复核。
        job = self._run(probe_ms=30250, segments=[{'start_ms': 0, 'end_ms': 30000}])
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertTrue(job['result']['artifact']['needs_review'])
        self.assertEqual(job['result']['artifact']['duration_diff_ms'], 250)

    def test_large_diff_fails_and_preserves_intermediate(self):
        # 差 10000ms > max(2000, 1500) → 判失败，但中间净剪文件保留可核对。
        job = self._run(probe_ms=20000, segments=[{'start_ms': 0, 'end_ms': 30000}])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('超出可播放容差', job['error'])
        self.assertIn('中间文件保留', job['error'])
        clean = TEST_PATH / 'packages' / job['id'] / 'clean.mp4'
        self.assertTrue(clean.is_file(), '失败时中间文件必须保留')

    # ---- P0-3：词级部分为空 → 句级回退 -------------------------------------
    def test_partial_empty_words_keeps_sentence_and_reports(self):
        transcript = {
            'text': '先看全局。再做判断。',
            'cues': [{'id': 'c1', 'start_ms': 0, 'end_ms': 900, 'text': '先看全局。'},
                     {'id': 'c2', 'start_ms': 3800, 'end_ms': 4900, 'text': '再做判断。'}],
            'words': [{'id': 'w1', 'text': '先看', 'start_ms': 0, 'end_ms': 500},
                      {'id': 'w2', 'text': '', 'start_ms': 500, 'end_ms': 900},
                      {'id': 'w3', 'text': '再做判断', 'start_ms': None, 'end_ms': None}]}
        captured = []
        job = self._run(probe_ms=30835, transcript=transcript, asr=True, capture_cues=captured)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(job['result']['artifact']['kind'], 'packaged_video')
        texts = ''.join(c['text'] for c in captured)
        self.assertIn('再做判断', texts)               # 词级全缺的句子靠句级时间保下来
        self.assertIn('词级结果缺失', job['result']['note'])

    # ---- 素材版本锚：旧结果不冒充当前 --------------------------------------
    def test_stale_asset_result_not_attached_to_current(self):
        job = self._run(probe_ms=30835, stale=True)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertFalse(job['result']['attached'])
        self.assertIn('没有设为当前成片', job['result']['note'])
        current = app.STORE.get('project', self.project['id'])
        self.assertNotEqual(current.get('latest_artifact_id'), job['result']['artifact']['id'])

    def test_current_asset_result_attached(self):
        job = self._run(probe_ms=30835)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertTrue(job['result']['attached'])
        current = app.STORE.get('project', self.project['id'])
        self.assertEqual(current.get('latest_artifact_id'), job['result']['artifact']['id'])
        self.assertEqual(current.get('stage'), 'review')

    # ---- 语音识别不可用：诚实降级 ------------------------------------------
    def test_asr_unavailable_produces_clean_with_honest_note(self):
        job = self._run(probe_ms=30835, asr=False)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(job['result']['artifact']['kind'], 'clean_video')
        self.assertIn('语音识别当前不可用', job['result']['note'])
        self.assertIn('没有字幕', job['result']['note'])

    # ---- 入口守卫 -----------------------------------------------------------
    def test_rejects_non_direct_project(self):
        proj = app.STORE.patch('project', self.project['id'], {'mode': 'script'},
                               expected=self.project['revision'])
        with self.assertRaises(app.Conflict):
            app.direct_pipeline_job(proj, self.asset)

    def test_rejects_foreign_asset(self):
        foreign = app.STORE.put('asset', {'project_id': 'other', 'purpose': 'direct',
            'filename': 'x.mp4', 'path': 'x', 'material_version': 1, 'info': self.asset['info']})
        with self.assertRaises(app.Conflict):
            app.direct_pipeline_job(self.project, foreign)

    def test_rejects_invalid_silence_mode(self):
        with self.assertRaises(ValueError):
            app.direct_pipeline_job(self.project, self.asset, 'ultra')


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        app.POOL.shutdown(wait=True)