"""B1 lite packaging regressions: cue mapping, grounded config validation, chain wiring."""
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(__file__).resolve().parent / '.test-runtime'
TEST_ROOT.mkdir(exist_ok=True)
TEST_PATH = TEST_ROOT / uuid.uuid4().hex
TEST_PATH.mkdir()
os.environ.setdefault('IP_DATA_DIR', str(TEST_PATH))
os.environ['IP_SKIP_LOCAL_CONFIG'] = '1'
os.environ['IP_DISABLE_LOCAL_ASR'] = '1'
for name in ('IP_LLM_URL', 'IP_LLM_MODEL', 'IP_ASR_URL', 'IP_RESOLVER_URL'):
    os.environ.pop(name, None)
import app
import media
import packaging_job

CUES = [{'id': 'cue_1', 'start_ms': 100, 'end_ms': 900, 'text': '先看，嗯。'},
        {'id': 'cue_2', 'start_ms': 3800, 'end_ms': 4900, 'text': '再做判断。条件很重要。'}]
MAPPING = [{'start_ms': 0, 'end_ms': 5000, 'output_start_ms': 0, 'output_end_ms': 5000}]


class MappingTests(unittest.TestCase):
    def test_map_cues_projects_source_time_to_cut_timeline(self):
        out, stats = packaging_job.map_cues(CUES, MAPPING)
        self.assertEqual(stats['mode'], 'sentence')
        self.assertEqual(out[0]['start_ms'], 100)
        self.assertEqual(out[1]['end_ms'], 4900)
        shifted, _ = packaging_job.map_cues(CUES, [{'start_ms': 1000, 'end_ms': 6000,
                                                 'output_start_ms': 0, 'output_end_ms': 5000}])
        self.assertEqual(len(shifted), 1)              # cue_1 在剪切中已移除
        self.assertEqual(shifted[0]['start_ms'], 2800)
        self.assertAlmostEqual(shifted[0]['end_ms'], 3900)

    def test_map_cues_drops_mostly_removed_cues(self):
        out, _ = packaging_job.map_cues(CUES, [{'start_ms': 0, 'end_ms': 1000, 'output_start_ms': 0, 'output_end_ms': 1000}])
        self.assertEqual(len(out), 1)

    def test_map_cues_partial_empty_words_falls_back_to_sentence(self):
        # 2026-11-10（试用反馈 P0-3）：词级部分为空（缺时间戳/空文本）时，
        # 整句不能静默消失；句级有内容的句子按句级时间保留并报数。
        cues = [{'id': 'c1', 'start_ms': 100, 'end_ms': 900, 'text': '先看全局。'},
                {'id': 'c2', 'start_ms': 3800, 'end_ms': 4900, 'text': '再做判断。'}]
        words = [{'id': 'w1', 'text': '先看', 'start_ms': 100, 'end_ms': 500},
                 {'id': 'w2', 'text': '', 'start_ms': 500, 'end_ms': 900},        # 空文本词
                 {'id': 'w3', 'text': '再做判断', 'start_ms': None, 'end_ms': None}]  # 缺时间戳
        out, stats = packaging_job.map_cues(cues, MAPPING, words)
        self.assertEqual(stats['mode'], 'words')
        self.assertGreaterEqual(stats['fallback_cues'], 1)   # 至少一句靠句级时间保下来
        texts = ''.join(c['text'] for c in out)
        self.assertIn('再做判断', texts)                      # 词级全缺的句子仍在字幕里
        self.assertTrue(all(c['start_ms'] < c['end_ms'] for c in out))

    def test_adapt_transcript_builds_packaging_schema(self):
        transcript = {'cues': CUES, 'words': []}
        segments, cues = packaging_job.adapt_transcript(transcript, MAPPING, 5000)
        self.assertEqual(segments['duration'], 5.0)
        self.assertEqual(len(segments['segments']), 2)
        self.assertEqual(segments['segments'][1]['text'], '再做判断。条件很重要。')


class ConfigValidationTests(unittest.TestCase):
    def _segments(self):
        return {'duration': 5.0, 'segments': [
            {'start': 0.1, 'end': 0.9, 'text': '先看，嗯。'},
            {'start': 3.8, 'end': 4.9, 'text': '再做判断。条件很重要。'}]}

    def _brief(self):
        return {'mood_arc': {'overall': '温情陪伴'}, 'visual_plan': {'quote_cards': []}}

    def test_config_cards_must_quote_verbatim(self):
        good = {'title': 'T', 'tagline': '', 'cards': [
            {'start': 3.8, 'end': 4.9, 'tag': '判断', 'lines': ['再做判断'], 'english': 'X', 'kind': 'quote'}],
            'chapters': [{'start': 0, 'end': 5, 'label': '01'}]}
        with patch('providers.llm_json', side_effect=[good, ['A', 'B']]):
            config, translations, bgm = packaging_job.generate_config(
                {'title': 'T'}, {'path': 'x.mp4', 'info': {'width': 1920, 'height': 1080}},
                self._segments(), self._brief())
        self.assertEqual(config['cards'][0]['lines'], ['再做判断'])
        self.assertEqual(translations, ['A', 'B'])
        self.assertEqual(bgm['bpm'], 90)   # 温情陪伴 → 90/520
        fabricated = {'title': 'T', 'cards': [
            {'start': 0, 'end': 2, 'tag': '假', 'lines': ['稿本里不存在'], 'english': '', 'kind': 'quote'}]}
        with patch('providers.llm_json', side_effect=[fabricated, ['A', 'B']]):
            with self.assertRaises(ValueError):
                packaging_job.generate_config({'title': 'T'}, {'path': 'x', 'info': {}},
                                              self._segments(), None)
        bad_en = {'title': 'T', 'cards': [{'start': 0, 'end': 1, 'tag': 't', 'lines': ['先看'], 'english': '', 'kind': 'quote'}]}
        with patch('providers.llm_json', side_effect=[bad_en, ['only-one'], ['only-one']]):  # 第二次调用为重试
            with self.assertRaises(ValueError):
                packaging_job.generate_config({'title': 'T'}, {'path': 'x', 'info': {}},
                                              self._segments(), None)

    def test_overlapping_cards_are_deduped(self):
        overlap = {'title': 'T', 'cards': [
            {'start': 0, 'end': 3, 'tag': '甲', 'lines': ['先看'], 'english': '', 'kind': 'quote'},
            {'start': 1, 'end': 4, 'tag': '乙', 'lines': ['嗯'], 'english': '', 'kind': 'quote'},
            {'start': 4, 'end': 5, 'tag': '丙', 'lines': ['条件很重要'], 'english': '', 'kind': 'quote'}]}
        with patch('providers.llm_json', side_effect=[overlap, ['A', 'B']]):
            config, _, _ = packaging_job.generate_config(
                {'title': 'T'}, {'path': 'x', 'info': {'width': 1920, 'height': 1080}},
                self._segments(), None)
        tags = [c['tag'] for c in config['cards']]
        self.assertEqual(tags, ['甲', '丙'])   # 与甲重叠的乙被剔除，丙保留


class WordCueRebuildTests(unittest.TestCase):
    WORD_MAP = [{'start_ms': 0, 'end_ms': 520, 'output_start_ms': 0, 'output_end_ms': 520},
                {'start_ms': 660, 'end_ms': 1100, 'output_start_ms': 520, 'output_end_ms': 960},
                {'start_ms': 1220, 'end_ms': 1700, 'output_start_ms': 960, 'output_end_ms': 1440}]

    def test_deleted_words_are_removed_when_text_is_rebuilt(self):
        words = [{'id': 'w1', 'text': '白了', 'start_ms': 100, 'end_ms': 300},
                 {'id': 'w2', 'text': '就是', 'start_ms': 320, 'end_ms': 500},
                 {'id': 'w3', 'text': '呃', 'start_ms': 520, 'end_ms': 640},      # 剪掉的语气词
                 {'id': 'w4', 'text': '压力呀', 'start_ms': 700, 'end_ms': 1100},
                 {'id': 'w5', 'text': '压', 'start_ms': 1120, 'end_ms': 1200},     # 剪掉的重复词
                 {'id': 'w6', 'text': '压力呀', 'start_ms': 1220, 'end_ms': 1600}]
        cues, _ = packaging_job.map_cues([{'start_ms': 100, 'end_ms': 1600, 'text': '白了就是呃压力呀压压力呀'}],
                                      self.WORD_MAP, words)
        text = ''.join(c['text'] for c in cues)
        self.assertEqual(text, '白了就是压力呀压力呀')      # 已删的“呃”“压”不再写回字幕
        self.assertTrue(all(c.get('id') for c in cues))   # 与 ASS/SRT/剪映导出共用同一份带 id 的结果

    def test_midpoint_law_drops_word_that_only_overlaps_half(self):
        # 词中点正好落在剪掉的空档里：旧“重叠≥50%”会保留，新判据必须丢弃。
        mapping = [{'start_ms': 0, 'end_ms': 500, 'output_start_ms': 0},
                   {'start_ms': 520, 'end_ms': 1000, 'output_start_ms': 510}]
        words = [{'id': 'a', 'text': '压', 'start_ms': 0, 'end_ms': 1000}]
        cues, _ = packaging_job.map_cues([], mapping, words)
        self.assertEqual(cues, [])

    def test_regrouping_caps_each_cue_and_never_splits_words(self):
        words = [{'id': 'w%d' % i, 'text': '字', 'start_ms': i * 100, 'end_ms': i * 100 + 100}
                 for i in range(120)]
        mapping = [{'start_ms': 0, 'end_ms': 12000, 'output_start_ms': 0, 'output_end_ms': 12000}]
        cues, _ = packaging_job.map_cues([], mapping, words)
        self.assertTrue(len(cues) > 1)
        for cue in cues:
            self.assertLessEqual(cue['end_ms'] - cue['start_ms'], 5000)
            self.assertLessEqual(len(cue['text']), 84)
        self.assertEqual(''.join(c['text'] for c in cues), '字' * 120)   # 只在词边界切，不丢词


class PackageLiteAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.fixture = TEST_PATH / 'package.mp4'
        if media.binary('ffmpeg') and media.binary('ffprobe'):
            media.run([media.binary('ffmpeg'), '-y', '-f', 'lavfi', '-i', 'color=c=0x29241c:s=320x240:r=20:d=5',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=5',
                       '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', cls.fixture])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method='GET', body=None, expected=200):
        headers = {}
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, body, headers, method=method)
        try:
            res = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as exc:
            res = exc
        with res:
            data = res.read()
            self.assertEqual(res.status, expected, data[:700])
            return json.loads(data) if 'application/json' in res.headers.get('Content-Type', '') else data

    def wait(self, job_id, limit=60):
        deadline = time.time() + limit
        while time.time() < deadline:
            job = self.request('/api/jobs/' + job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(.1)
        self.fail('job ' + job_id + ' timed out')

    def clean_artifact(self):
        p = self.request('/api/projects', 'POST', {'title': '包装联调', 'source_text': '来源'}, expected=201)
        parts = [{'role': r, 'text': r} for r, _ in app.ROLES]
        p = self.request('/api/projects/' + p['id'] + '/draft', 'PATCH', {'parts': parts, 'draft_revision': p['draft_revision']})
        p = self.request('/api/projects/' + p['id'] + '/confirm', 'POST', {'draft_revision': p['draft_revision']})
        raw = self.fixture.read_bytes()
        session = self.request('/api/uploads', 'POST', {'project_id': p['id'], 'filename': 'p.mp4', 'size': len(raw)}, expected=201)
        url = '/api/uploads/' + session['id']
        self.request(url + '?offset=0', 'PUT', raw)
        asset = self.request(url + '/complete', 'POST', {})
        job = self.request('/api/projects/' + p['id'] + '/cut', 'POST',
                           {'asset_id': asset['id'], 'mode': 'conservative',
                            'segments': [{'start_ms': 0, 'end_ms': 5000}]}, expected=202)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        app.STORE.put('transcript', {'asset_id': asset['id'], 'project_id': p['id'],
                                     'text': '先看，嗯。再做判断。条件很重要。', 'words': [], 'cues': CUES})
        return p, asset, result['result']['artifact']

    def _fake_run(self):
        def _run(cmd, stage_name, cwd=None):
            cmd = [str(x) for x in cmd]
            if any('burn_compose.py' in c for c in cmd):
                wd = Path(cmd[cmd.index('--workdir') + 1])
                (wd / 'packaged_output.mp4').write_bytes(b'fake')
            return type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
        return _run

    def test_package_lite_registers_artifact(self):
        if not self.fixture.exists():
            self.skipTest('FFmpeg not installed')
        p, asset, clean = self.clean_artifact()
        config = {'title': '包装联调', 'tagline': '', 'cards': [
            {'start': 0, 'end': 2, 'tag': '看', 'lines': ['先看'], 'english': 'Look', 'kind': 'quote'}]}
        with patch('providers.available', return_value={'llm': True, 'asr': True}), \
             patch('providers.llm_json', side_effect=[config, ['A', 'B']]), \
             patch('packaging_job._run', side_effect=self._fake_run()), \
             patch('media.probe', return_value={'duration_ms': 5000, 'width': 1920, 'height': 1080,
                                                'audio': True, 'codec': 'h264', 'size': 4}):
            job = self.request('/api/projects/' + p['id'] + '/package-lite', 'POST',
                               {'artifact_id': clean['id']}, expected=202)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        item = result['result']['artifact']
        self.assertEqual(item['kind'], 'packaged_video')
        self.assertEqual(item['packaging_method'], 'b1_lite_learning_pack')
        self.assertEqual(item['clean_artifact_id'], clean['id'])
        details = self.request('/api/projects/' + p['id'])
        self.assertTrue(any(a['id'] == item['id'] for a in details['artifacts']))

    def test_package_lite_rejects_wrong_artifact(self):
        if not self.fixture.exists():
            self.skipTest('FFmpeg not installed')
        p, asset, clean = self.clean_artifact()
        other, _, other_clean = self.clean_artifact()
        self.request('/api/projects/' + p['id'] + '/package-lite', 'POST',
                     {'artifact_id': other_clean['id']}, expected=404)
        packaged = app.STORE.put('artifact', {'project_id': p['id'], 'kind': 'packaged_video',
            'script_version_id': p['confirmed_version_id'], 'path': clean['path'], 'info': clean['info']})
        self.request('/api/projects/' + p['id'] + '/package-lite', 'POST',
                     {'artifact_id': packaged['id']}, expected=400)


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        app.POOL.shutdown(wait=True)
