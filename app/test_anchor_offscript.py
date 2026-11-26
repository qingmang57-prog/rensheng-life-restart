"""稿本锚定 P0-2 边界回归：稿外口播突破 30 段全量列出、默认保留、删除须明确选择。

合成转写 + 替身渲染，不依赖真实视频/账号。验证：
- 35 段稿外口播全部进入 plan.improvised（不被截到前 30）；
- apply_plan 默认保留全部稿外（keep_improvised 传全量）；
- 传空列表＝明确删除全部稿外；传子集＝只留子集。
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
(TEST_PATH / 'artifacts').mkdir()
os.environ['IP_DATA_DIR'] = str(TEST_PATH)
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
os.environ['IP_DISABLE_LOCAL_ASR'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import anchor_job
import media

N_OFFSCRIPT = 35  # > 30，专门打穿旧的前 30 段截断


def _transcript():
    cues = [{'id': 'cue_0', 'start_ms': 0, 'end_ms': 1000, 'text': '先看全局再做判断。'}]
    words = [{'id': 'w0', 'text': '先看全局再做判断', 'start_ms': 0, 'end_ms': 1000}]
    for i in range(1, N_OFFSCRIPT + 1):
        s = i * 2000
        cues.append({'id': 'cue_%d' % i, 'start_ms': s, 'end_ms': s + 1000,
                     'text': '这是第%d段与稿本完全无关的即兴口播内容。' % i})
        words.append({'id': 'w%d' % i, 'text': '即兴口播%d' % i, 'start_ms': s, 'end_ms': s + 1000})
    return {'words': words, 'cues': cues, 'text': ''.join(c['text'] for c in cues)}


def _matched_parts(words, cues, parts):
    # 替身模糊匹配：只让 hook 段命中 cue_0，其余 35 句都算稿外。
    return [{'part_id': 'hook_01', 'role': 'hook', 'label': '钩子', 'text': '先看全局再做判断。',
             'takes': [{'cue_ids': ['cue_0'], 'start_ms': 0, 'end_ms': 1000,
                        'text': '先看全局再做判断。', 'density': 0.0, 'completeness': 1.0}]}]


class OffScriptBoundaryTests(unittest.TestCase):
    def setUp(self):
        sv = app.STORE.put('script_version', {'parts': [
            {'id': 'hook_01', 'role': 'hook', 'label': '钩子', 'text': '先看全局再做判断。'}]})
        self.project = app.STORE.put('project', {'title': '稿外边界测试', 'mode': 'script',
            'confirmed_version_id': sv['id'], 'draft_changed': False, 'is_test': True})
        self.asset = app.STORE.put('asset', {'project_id': self.project['id'], 'purpose': 'shooting',
            'script_version_id': sv['id'], 'filename': 's.mp4', 'path': str(TEST_PATH / 's.mp4'),
            'info': {'duration_ms': (N_OFFSCRIPT + 1) * 2000, 'width': 1920, 'height': 1080, 'audio': True}})
        self.transcript = app.STORE.put('transcript', {**_transcript(),
            'asset_id': self.asset['id'], 'project_id': self.project['id']})

    def _build_plan(self):
        with patch('providers.available', return_value={'asr': True, 'llm': False}), \
             patch('anchor_job.find_candidates', side_effect=_matched_parts):
            result = anchor_job.build_plan(app.STORE, self.project, self.asset,
                                           'buildjob', TEST_PATH, lambda s: None)
        return result['plan']

    def test_improvised_lists_all_offscript_no_30_cap(self):
        plan = self._build_plan()
        self.assertEqual(len(plan['improvised']), N_OFFSCRIPT)   # 35 段全量，不截到 30
        ids = {c['cue_id'] for c in plan['improvised']}
        self.assertNotIn('cue_0', ids)                            # 命中的稿本句不算稿外
        self.assertIn('cue_35', ids)                              # 第 35 段仍在列表里

    def _apply(self, plan, keep_ids):
        def fake_cut(path, info, segments, output):
            Path(output).write_bytes(b'fake')
            return [{'start_ms': 0, 'end_ms': 5000, 'output_start_ms': 0, 'output_end_ms': 5000}]
        with patch('media.silence_intervals', return_value=[]), \
             patch('media.snap_edges_to_silence', side_effect=lambda sil, rem: rem), \
             patch('media.silence_plan', return_value={'removed': []}), \
             patch('media.combine_removed', return_value=([{'start_ms': 0, 'end_ms': 5000}], [])), \
             patch('media.render_cut', side_effect=fake_cut), \
             patch('media.probe', return_value={'duration_ms': 5000, 'width': 1920, 'height': 1080, 'audio': True}):
            return anchor_job.apply_plan(app.STORE, self.project, self.asset, plan,
                                         {'hook_01': 0}, keep_ids, 'conservative',
                                         'applyjob', TEST_PATH, lambda s: None)

    def test_default_keep_all_offscript(self):
        plan = self._build_plan()
        all_ids = [c['cue_id'] for c in plan['improvised']]
        result = self._apply(plan, all_ids)
        self.assertEqual(len(result['plan']['keep_improvised']), N_OFFSCRIPT)

    def test_explicit_empty_deletes_all_offscript(self):
        plan = self._build_plan()
        result = self._apply(plan, [])
        self.assertEqual(result['plan']['keep_improvised'], [])

    def test_subset_keeps_only_chosen(self):
        plan = self._build_plan()
        result = self._apply(plan, ['cue_1', 'cue_2', 'cue_99'])  # cue_99 不存在被过滤
        self.assertEqual(sorted(result['plan']['keep_improvised']), ['cue_1', 'cue_2'])

    def test_apply_requires_explicit_take_selection(self):
        plan = self._build_plan()
        with self.assertRaises(ValueError):
            anchor_job.apply_plan(app.STORE, self.project, self.asset, plan,
                                  {}, [c['cue_id'] for c in plan['improvised']], 'conservative',
                                  'applyjob', TEST_PATH, lambda s: None)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        app.POOL.shutdown(wait=True)