"""Script-anchored cutting regressions: fuzzy matcher, take scoring, R2 region purge,
real A/V cuts from per-part take selection, director brief."""
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
import anchor_job
import director_pass
import media

WORDS = [{'id': 'a', 'text': '先看', 'start_ms': 100, 'end_ms': 500},
         {'id': 'b', 'text': '嗯', 'start_ms': 600, 'end_ms': 900},
         {'id': 'c', 'text': '再做判断', 'start_ms': 3800, 'end_ms': 4500},
         {'id': 'd', 'text': '条件', 'start_ms': 4600, 'end_ms': 4900}]
CUES = [{'id': 'cue_1', 'start_ms': 100, 'end_ms': 900, 'text': '先看，嗯。'},
        {'id': 'cue_2', 'start_ms': 3800, 'end_ms': 4900, 'text': '再做判断。条件很重要。'}]
PARTS = [{'id': 'hook_01', 'role': 'hook', 'label': '钩子', 'text': '先看全局再做判断。'},
         {'id': 'content_01', 'role': 'content', 'label': '内容', 'text': '再做判断，条件很重要。'},
         {'id': 'anchor_01', 'role': 'anchor', 'label': '钉子', 'text': '完全无关的钉子段。'},
         {'id': 'quote_01', 'role': 'quote', 'label': '金句', 'text': '短。'},
         {'id': 'ending_01', 'role': 'ending', 'label': '结尾', 'text': '总之条件很重要。'}]


class MatcherTests(unittest.TestCase):
    def test_fuzzy_matcher_finds_take_and_flags_missing_parts(self):
        result = anchor_job.find_candidates(WORDS, CUES, PARTS)
        by_id = {p['part_id']: p for p in result}
        self.assertTrue(by_id['hook_01']['takes'], '钩子句应在转写中找到候选')
        take = by_id['hook_01']['takes'][0]
        self.assertTrue({'cue_1', 'cue_2'} & set(take['cue_ids']))
        self.assertGreater(take['completeness'], 0.2)
        self.assertTrue(by_id['content_01']['takes'])
        self.assertEqual(by_id['content_01']['takes'][0]['cue_ids'], ['cue_2'])
        self.assertEqual(by_id['anchor_01']['takes'], [])  # 无关段找不到
        self.assertEqual(by_id['quote_01']['takes'], [])   # 过短段不硬凑

    def test_multi_take_separation(self):
        words = WORDS + [{'id': 'e', 'text': '再做判断', 'start_ms': 40100, 'end_ms': 40900}]
        cues = CUES + [{'id': 'cue_3', 'start_ms': 40100, 'end_ms': 40900, 'text': '再做判断。'}]
        parts = [{'id': 'hook_01', 'role': 'hook', 'label': '钩子', 'text': '再做判断就好。'}]
        result = anchor_job.find_candidates(words, cues, parts)
        self.assertGreaterEqual(len(result[0]['takes']), 2)  # 40s 间隔分为两个 take

    def test_disfluency_signals_and_dense_regions(self):
        words = [{'id': str(i), 'text': t, 'start_ms': i * 400, 'end_ms': i * 400 + 300}
                 for i, t in enumerate(list('你比如就像你比如就像就是说明白一点'))]
        signals = anchor_job.disfluency_signals(words)
        self.assertTrue(signals)
        regions = anchor_job.dense_regions(words, [words[5]['start_ms']])
        self.assertTrue(regions)

    def test_closure_warnings(self):
        cues_end_ok = [{'id': 'x', 'start_ms': 0, 'end_ms': 1000, 'text': '完整收尾。'}]
        self.assertEqual(anchor_job.closure_warnings([], cues_end_ok, {'x'}), [])
        cues_half = [{'id': 'y', 'start_ms': 0, 'end_ms': 1000, 'text': '说到一半的半'}]
        warnings = anchor_job.closure_warnings([], cues_half, {'y'})
        self.assertTrue(any('半句' in w for w in warnings))

    def test_judgment_validation_and_scoring(self):
        parts = anchor_job.find_candidates(WORDS, CUES, PARTS)
        judgments = {('content_01', 0): {'usable': True, 'drop_cue_ids': [], 'performance': 5, 'reason': '沉稳'}}
        out = anchor_job.finalize_parts(parts, judgments)
        by_id = {p['part_id']: p for p in out}
        take = by_id['content_01']['takes'][0]
        self.assertEqual(take['performance'], 5)
        self.assertEqual(by_id['content_01']['recommended'], 0)
        # drop_cue_ids 必须是该 take 的子集；伪造的丢弃列表被忽略
        judgments_bad = {('content_01', 0): {'usable': True, 'drop_cue_ids': ['ghost'], 'performance': None, 'reason': ''}}
        out2 = anchor_job.finalize_parts(anchor_job.find_candidates(WORDS, CUES, PARTS), judgments_bad)
        self.assertEqual(out2[1]['takes'][0]['cue_ids'], ['cue_2'])


class R2RegionPurgeTests(unittest.TestCase):
    def test_repair_inside_restart_dense_region_survives(self):
        # 结巴段：开头即"那谁那谁"重复，后面还有重复；两条互相冲突的删除建议
        # （各自删掉对方的 repair）都落在重启密集区内 → 句级保护让位于段级裁决。
        tokens = list('那谁那谁谁的那谁都那谁那谁都经不住你拿动机去揣测一个人谁都经不住')
        words = [{'id': f'w{i}', 'text': t, 'start_ms': i * 300, 'end_ms': i * 300 + 250} for i, t in enumerate(tokens)]
        n = len(words)
        answer = {'deletions': [
            {'word_ids': [words[3]['id']], 'type': 'repeat', 'repair_word_ids': [words[20]['id']], 'reason': 'r', 'kept_text': '', 'needs_review': False},
            {'word_ids': [words[20]['id']], 'type': 'restart', 'repair_word_ids': [words[3]['id']], 'reason': 'r', 'kept_text': '', 'needs_review': False}]}
        deletions, skipped = app.semantic_plan_valid(words, answer, n * 300)
        self.assertEqual(len(deletions), 2, '重启密集区内句级保护应让位于段级裁决')
        self.assertTrue(all(d.get('region_purge') for d in deletions))
        self.assertEqual(skipped, [])


class DirectorBriefTests(unittest.TestCase):
    def test_rule_fallback_shape_and_item_validation(self):
        brief = director_pass.rule_fallback(PARTS)
        self.assertIn('mood_arc', brief)
        self.assertTrue(brief['visual_plan']['quote_cards'])
        with patch('providers.llm_json', return_value={'visual_plan': {'quote_cards': [
                {'part_id': 'content_01', 'quote': '条件很重要', 'reason': 'r'}], 'keyword_moments': []}}):
            plan = director_pass.generate_item('visual_plan', PARTS)
        self.assertEqual(plan['quote_cards'][0]['quote'], '条件很重要')
        with patch('providers.llm_json', return_value={'visual_plan': {'quote_cards': [
                {'part_id': 'content_01', 'quote': '稿本里不存在的话', 'reason': 'r'}], 'keyword_moments': []}}):
            with self.assertRaises(ValueError):
                director_pass.generate_item('visual_plan', PARTS)


class AnchorAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.fixture = TEST_PATH / 'anchor.mp4'
        if media.binary('ffmpeg') and media.binary('ffprobe'):
            media.run([media.binary('ffmpeg'), '-y', '-f', 'lavfi', '-i', 'color=c=0x29241c:s=320x240:r=20:d=5',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=5',
                       '-af', "volume='if(between(t,1.2,3.2),0,0.3)':eval=frame", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', cls.fixture])

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

    def confirmed_project_with_asset(self):
        p = self.request('/api/projects', 'POST', {'title': '锚定联调', 'source_text': '来源原文'}, expected=201)
        parts = [{'role': r, 'text': t} for r, t in [
            ('hook', '先看全局再做判断。'), ('content', '再做判断，条件很重要。'),
            ('anchor', '钉子内容。'), ('quote', '金句短句。'), ('ending', '总之条件很重要。')]]
        p = self.request('/api/projects/' + p['id'] + '/draft', 'PATCH', {'parts': parts, 'draft_revision': p['draft_revision']})
        p = self.request('/api/projects/' + p['id'] + '/confirm', 'POST', {'draft_revision': p['draft_revision']})
        raw = self.fixture.read_bytes()
        session = self.request('/api/uploads', 'POST', {'project_id': p['id'], 'filename': 'anchor.mp4', 'size': len(raw)}, expected=201)
        url = '/api/uploads/' + session['id']
        self.request(url + '?offset=0', 'PUT', raw)
        asset = self.request(url + '/complete', 'POST', {})
        return p, asset

    def test_full_anchor_chain_cuts_real_media(self):
        if not self.fixture.exists():
            self.skipTest('FFmpeg not installed')
        p, asset = self.confirmed_project_with_asset()
        transcript = {'text': '先看，嗯。再做判断。条件很重要。', 'words': WORDS, 'cues': CUES}
        judgments = {'judgments': [{'part_id': 'content_01', 'take_index': 0, 'usable': True,
                                    'drop_cue_ids': [], 'performance': 4, 'reason': '清楚'}]}
        with patch('providers.available', return_value={'llm': True, 'asr': True}), \
             patch('providers.asr', return_value=transcript), \
             patch('providers.llm_json', return_value=judgments):
            job = self.request('/api/projects/' + p['id'] + '/anchor-align', 'POST', {'asset_id': asset['id']}, expected=202)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'needs_action', result.get('error'))
        plan = result['result']['plan']
        by_id = {x['part_id']: x for x in plan['parts']}
        self.assertTrue(by_id['content_01']['takes'])
        self.assertEqual(by_id['content_01']['takes'][0]['performance'], 4)
        self.assertTrue(any('钉子' in i for i in plan['issues']))
        self.assertEqual(plan['method'], 'anchor_llm_judged')
        details = self.request('/api/projects/' + p['id'])
        self.assertEqual(len(details['anchor_plans']), 1)
        # 选择钩子、内容与结尾段的 take（cue_1+cue_2 全覆盖）；钉子无候选不参与选择
        sel = {'hook_01': 0, 'content_01': 0, 'ending_01': 0}
        job = self.request('/api/projects/' + p['id'] + '/anchor-apply', 'POST',
                           {'asset_id': asset['id'], 'plan_id': plan['id'], 'selection': sel}, expected=202)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        artifact = result['result']['artifact']
        self.assertEqual(artifact['kind'], 'clean_video')
        self.assertEqual(result['result']['warnings'], [])  # cue_2 完整收尾
        # 钩子命中 cue_1+cue_2（多命中分组），全保留只压静音：约 5000-1680=3320ms
        self.assertAlmostEqual(artifact['info']['duration_ms'], 3320, delta=420)
        self.assertTrue(artifact['info']['audio'])
        self.assertFalse(any(s['start_ms'] <= 1400 and s['end_ms'] >= 3000 for s in result['result']['plan']['segments']))
        self.assertTrue(any(s['start_ms'] <= 4600 and s['end_ms'] >= 4900 for s in result['result']['plan']['segments']))

    def test_apply_rejects_bad_selection_and_foreign_plan(self):
        if not self.fixture.exists():
            self.skipTest('FFmpeg not installed')
        p, asset = self.confirmed_project_with_asset()
        other, _ = self.confirmed_project_with_asset()
        plan = app.STORE.put('anchor_plan', {'project_id': p['id'], 'asset_id': asset['id'], 'transcript_id': 't',
            'script_version_id': p['confirmed_version_id'],
            'parts': [{'part_id': 'content_01', 'label': '内容', 'takes': [{'cue_ids': ['cue_2'], 'start_ms': 3800,
                'end_ms': 4900, 'text': '再做判断。条件很重要。', 'completeness': 0.9, 'signals': 0, 'density': 0,
                'take_index': 0, 'score': 0.9, 'performance': None, 'reason': ''}], 'recommended': 0}],
            'improvised': [], 'issues': [], 'method': 'test'})
        self.request('/api/projects/' + other['id'] + '/anchor-apply', 'POST',
                     {'asset_id': asset['id'], 'plan_id': plan['id'], 'selection': {'content_01': 0}}, expected=404)
        self.request('/api/projects/' + p['id'] + '/anchor-apply', 'POST',
                     {'asset_id': asset['id'], 'plan_id': plan['id'], 'selection': {}}, expected=400)
        self.request('/api/projects/' + p['id'] + '/anchor-apply', 'POST',
                     {'asset_id': asset['id'], 'plan_id': plan['id'], 'selection': {'content_01': 9}}, expected=400)
        # 客户端时间戳被忽略：以转写词时间戳回源为准
        job = self.request('/api/projects/' + p['id'] + '/anchor-apply', 'POST',
                           {'asset_id': asset['id'], 'plan_id': plan['id'], 'selection': {'content_01': 0},
                            'segments': [{'start_ms': 0, 'end_ms': 10}]}, expected=202)
        result = self.wait(job['id'])
        self.assertEqual(result['status'], 'failed')

    def test_director_brief_api(self):
        p, asset = self.confirmed_project_with_asset()
        with patch('providers.available', return_value={'llm': False, 'asr': True}):
            details = self.request('/api/projects/' + p['id'])
        self.assertIsNone(details['director_brief'])
        with patch('providers.available', return_value={'llm': True, 'asr': True}), \
             patch('providers.llm_json', side_effect=[
                 {'mood_arc': {'overall': '温情陪伴', 'segments': [
                     {'part_id': r + '_01', 'mood': '温情陪伴', 'intensity': 3, 'reason': 'x'} for r, _ in app.ROLES]}},
                 {'pacing': [{'part_id': 'quote_01', 'pace': '留白', 'pause_before_ms': 600, 'reason': '金句前'}]},
                 {'visual_plan': {'quote_cards': [{'part_id': 'quote_01', 'quote': '金句短句', 'reason': 'r'}],
                                  'keyword_moments': []}}]):
            job = self.request('/api/projects/' + p['id'] + '/director-brief', 'POST', {}, expected=202)
            result = self.wait(job['id'])
        self.assertEqual(result['status'], 'succeeded', result.get('error'))
        brief = result['result']['brief']
        self.assertEqual(brief['mood_arc']['overall'], '温情陪伴')
        self.assertEqual(brief['pacing'][0]['pause_before_ms'], 600)
        self.assertEqual(brief['visual_plan']['quote_cards'][0]['quote'], '金句短句')
        details = self.request('/api/projects/' + p['id'])
        self.assertEqual(details['director_brief']['id'], brief['id'])


if __name__ == '__main__':
    try:
        unittest.main(verbosity=2)
    finally:
        app.POOL.shutdown(wait=True)
