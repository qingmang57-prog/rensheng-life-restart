"""Regression tests for dynamic style routing and version-bound packaging."""
import copy
import tempfile
import unittest
from pathlib import Path
from db import Store, Conflict
import smart_packaging as sp


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.project = self.store.put('project', {'title': '创业', 'confirmed_version_id': 'v1', 'draft_changed': False})
        self.clean = self.store.put('artifact', {'project_id': self.project['id'], 'script_version_id': 'v1',
            'kind': 'clean_video', 'asset_id': 'a1', 'path': 'source.mp4', 'info': {'duration_ms': 12000, 'width': 1920, 'height': 1080},
            'mapping': [{'start_ms': 0, 'end_ms': 12000, 'output_start_ms': 0, 'output_end_ms': 12000}]})
        self.transcript = self.store.put('transcript', {'asset_id': 'a1', 'words': [], 'cues': [
            {'start_ms': 0, 'end_ms': 3500, 'text': '那年创业失败，我很孤独难过。'},
            {'start_ms': 3600, 'end_ms': 7000, 'text': '失眠疲惫，允许自己慢慢休息。'},
            {'start_ms': 7200, 'end_ms': 11500, 'text': '没关系，温柔地陪你放下。'}]})

    def tearDown(self): self.temp.cleanup()

    def test_same_topic_can_have_different_styles(self):
        emotion = sp.ensure_plan(self.store, self.project, self.clean)
        self.assertEqual(emotion['selected']['id'], 'night-ledger-warm')
        self.store.patch('transcript', self.transcript['id'], {'cues': [
            {'start_ms': 0, 'end_ms': 11000, 'text': '创业如何管理成本？首先分析客户数据，其次比较现金流，第三复盘利润比例。'}]})
        practical = sp.ensure_plan(self.store, self.project, self.clean)
        self.assertNotEqual(emotion['selected']['id'], practical['selected']['id'])
        self.assertNotEqual(emotion['fingerprint'], practical['fingerprint'])

    def test_title_does_not_force_emotion(self):
        cues = [{'text': '首先计算成本，其次比较数据，第三检查百分之二十的比例。'}]
        ranked, analysis = sp.match_styles(cues, 12000, {'title': '孤独与情感', 'persona': {}})
        self.assertNotEqual(ranked[0]['id'], 'night-ledger-warm')
        self.assertIn('分析与对照', analysis['signals'])

    def test_ambiguous_content_requests_preview(self):
        ranked, analysis = sp.match_styles([{'text': '今天和大家分享这件事情。'}], 10000, {'persona': {}})
        self.assertEqual(analysis['confidence'], '建议试看')
        self.assertEqual(ranked[0]['id'], 'modern-editorial')

    def test_persona_is_a_soft_preference(self):
        calm, _ = sp.match_styles([{'text': '今天分享一点有趣的经历。'}], 10000, {'persona': {'style': '克制简洁'}})
        self.assertGreater(len(calm), 3)
        self.assertEqual(len({x['id'] for x in calm}), 11)

    def test_plan_is_reused_and_manual_choice_stays_local(self):
        plan = sp.ensure_plan(self.store, self.project, self.clean)
        self.assertEqual(plan['id'], sp.ensure_plan(self.store, self.project, self.clean)['id'])
        selected = sp.ensure_plan(self.store, self.project, self.clean, 'modern-editorial')
        self.assertEqual(selected['selection'], 'manual')
        self.assertEqual(selected['id'], sp.ensure_plan(self.store, self.project, self.clean)['id'])
        self.assertIsNone(self.store.get('profile', 'owner'))
        with self.assertRaises(Conflict): sp.validate_plan(self.store, self.project, plan)

    def test_unknown_style_rejected(self):
        with self.assertRaises(ValueError): sp.ensure_plan(self.store, self.project, self.clean, '../foo')

    def test_cross_project_and_draft_rejected(self):
        other = {**self.project, 'id': 'other'}
        with self.assertRaises(ValueError): sp.ensure_plan(self.store, other, self.clean)
        with self.assertRaises(Conflict): sp.ensure_plan(self.store, {**self.project, 'draft_changed': True}, self.clean)
        with self.assertRaises(Conflict): sp.ensure_plan(self.store, {**self.project, 'confirmed_version_id': 'v2'}, self.clean)

    def test_changed_profile_invalidates_preview_binding(self):
        plan = sp.ensure_plan(self.store, self.project, self.clean)
        self.store.put('profile', {'style': '活泼幽默'}, 'owner')
        with self.assertRaises(Conflict): sp.validate_plan(self.store, self.project, plan)

    def test_new_clean_invalidates_old_plan(self):
        plan = sp.ensure_plan(self.store, self.project, self.clean)
        self.store.put('artifact', {**self.clean, 'id': 'new-clean'})
        with self.assertRaises(Conflict): sp.validate_plan(self.store, self.project, plan)

    def test_cut_words_do_not_restore_deleted_text(self):
        transcript = {'words': [
            {'start_ms': 0, 'end_ms': 300, 'text': '我'},
            {'start_ms': 350, 'end_ms': 600, 'text': '嗯'},
            {'start_ms': 650, 'end_ms': 1000, 'text': '认为'}],
            'cues': [{'start_ms': 0, 'end_ms': 1000, 'text': '我嗯认为'}]}
        mapping = [{'start_ms': 0, 'end_ms': 320, 'output_start_ms': 0},
                   {'start_ms': 630, 'end_ms': 1100, 'output_start_ms': 320}]
        cues = sp.cut_cues(transcript, mapping, 790)
        self.assertEqual(''.join(c['text'] for c in cues), '我认为')
        self.assertLessEqual(cues[-1]['end_ms'], 790)

    def test_partial_cue_without_words_is_rejected(self):
        with self.assertRaises(ValueError):
            sp.cut_cues({'cues': [{'start_ms': 0, 'end_ms': 1000, 'text': '不能恢复删掉的半句'}]},
                        [{'start_ms': 400, 'end_ms': 1000, 'output_start_ms': 0}], 600)

    def test_params_keep_theme_and_full_clock(self):
        plan = {**sp.ensure_plan(self.store, self.project, self.clean), 'layout': 'landscape'}
        params = sp.make_params(plan, 'source.mp4', 12)
        timeline = params['timeline']
        self.assertEqual(timeline['mood'], 'warm')
        self.assertEqual(timeline['family'], 'night-ledger')
        self.assertEqual(timeline['segments'][0]['t0'], 0)
        self.assertEqual(timeline['segments'][-1]['t1'], 12)
        self.assertEqual(timeline['presenter']['framing'], 'none')
        self.assertEqual(timeline['music'], '')
        for segment in timeline['segments']:
            self.assertTrue(all(c in ''.join(c['text'] for c in plan['cues']) for c in segment['props'].get('text', '')))
        # Real engine contracts, without media binding.
        import importlib.util
        spec = importlib.util.spec_from_file_location('kit_adapter', sp.KIT / 'engine/adapter.py')
        adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
        adapter.check_shape(params); adapter.check_segments(params, sp.catalog()); adapter.check_narration_map(params)

    def test_steps_keep_prefix_and_action_together(self):
        plan = {**sp.ensure_plan(self.store, self.project, self.clean), 'layout': 'portrait'}
        plan['cues'] = [{'start_ms': i * 2000, 'end_ms': i * 2000 + 1900, 'text': text}
                        for i, text in enumerate(['首先，认真学习新的方法。', '其次，记录习惯的改变。', '第三，复盘行动和目标。'])]
        params = sp.make_params(plan, 'source.mp4', 6)
        props = params['timeline']['segments'][0]['props']
        self.assertEqual(props['steps'], ['首先，认真学习新的方法', '其次，记录习惯的改变', '第三，复盘行动和目标'])

    def test_emotional_card_emphasizes_one_short_clause(self):
        plan = {**sp.ensure_plan(self.store, self.project, self.clean), 'layout': 'landscape'}
        params = sp.make_params(plan, 'source.mp4', 12)
        for segment in params['timeline']['segments']:
            self.assertLessEqual(len(segment['props']['text']), 18)

    def test_preview_caps_cues_without_changing_source_plan(self):
        plan = {**sp.ensure_plan(self.store, self.project, self.clean), 'layout': 'portrait'}
        before = copy.deepcopy(plan)
        params = sp.make_params(plan, 'source.mp4', 8)
        self.assertLessEqual(max(c['end'] for c in params['timeline']['captionsData']), 8)
        self.assertEqual(plan, before)

    def test_corrected_captions_are_used_and_invalidate_fingerprint(self):
        plan = sp.ensure_plan(self.store, self.project, self.clean)
        cues = copy.deepcopy(plan['cues']); cues[0]['text'] = '那年创业失败，我很失落。'
        self.store.put('packaging_captions', {'base_cue_digest': sp.digest(plan['cues']), 'cues': cues}, self.clean['id'])
        revised = sp.ensure_plan(self.store, self.project, self.clean, plan['selected']['id'])
        self.assertEqual(revised['cues'][0]['text'], cues[0]['text'])
        self.assertNotEqual(revised['fingerprint'], plan['fingerprint'])
        with self.assertRaises(Conflict): sp.validate_plan(self.store, self.project, plan)


if __name__ == '__main__': unittest.main(verbosity=2)
