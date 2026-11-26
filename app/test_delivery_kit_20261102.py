"""四件套打包 / 交付页回归锁（2026-11-02）。

只验「资料整理」边界：不操作平台、不自动发布、不伪造回执。
D4 定案：客户自己在抖音点发布，自动代发已砍，不得回头做。
"""
import tempfile
import unittest

from db import Store
import release_service as r


class DeliveryKitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = Store(self.temp.name)
        self.s.put('script_version', {'project_id': 'p', 'parts': [{'text': '真实定稿'}]}, 'v')
        self.s.put('project', {
            'title': '标题', 'confirmed_version_id': 'v', 'draft_changed': False,
            'publication_pack': {'script_version_id': 'v', 'selected_title': '发布标题',
                                 'caption': '简介', 'tags': ['创业', '#认知']},
            'reviewed_artifact_id': 'a'}, 'p')
        self.s.put('artifact', {'project_id': 'p', 'script_version_id': 'v',
                                'kind': 'packaged_video', 'cover_id': 'c'}, 'a')
        self.s.put('artifact', {'project_id': 'p', 'script_version_id': 'v', 'kind': 'cover'}, 'c')

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_returns_cover_and_video_urls(self):
        p = r.prepare(self.s, 'p')
        self.assertEqual(p['video_url'], '/api/artifacts/a')
        self.assertEqual(p['cover_id'], 'c')
        self.assertEqual(p['cover_url'], '/api/artifacts/c')

    def test_cross_project_cover_rejected(self):
        self.s.put('artifact', {'project_id': 'other', 'script_version_id': 'v', 'kind': 'cover'}, 'c')
        p = r.prepare(self.s, 'p')
        self.assertIsNone(p['cover_id'])
        self.assertIsNone(p['cover_url'])

    def test_wrong_kind_cover_rejected(self):
        self.s.put('artifact', {'project_id': 'p', 'script_version_id': 'v', 'kind': 'packaged_video'}, 'c')
        p = r.prepare(self.s, 'p')
        self.assertIsNone(p['cover_id'])

    def test_kit_is_superset_of_prepare(self):
        p = r.prepare(self.s, 'p')
        k = r.kit(self.s, 'p')
        for key, value in p.items():
            self.assertEqual(k[key], value, key)

    def test_kit_share_text_and_note(self):
        k = r.kit(self.s, 'p')
        self.assertEqual(k['share_text'], '发布标题\n\n简介\n\n#创业 #认知')
        self.assertIn('不自动代发', k['delivery_note'])

    def test_kit_export_zip_full(self):
        k = r.kit(self.s, 'p')
        self.assertEqual(k['export_zip_url'], '/api/projects/p/export?include_video=1')
        self.assertEqual(k['video']['download_url'], '/api/artifacts/a?download=1')
        self.assertEqual(k['cover']['download_url'], '/api/artifacts/c?download=1')

    def test_kit_without_artifact(self):
        self.s.patch('project', 'p', {'reviewed_artifact_id': None})
        k = r.kit(self.s, 'p')
        self.assertIsNone(k['video'])
        self.assertIsNone(k['cover'])
        self.assertEqual(k['export_zip_url'], '/api/projects/p/export')

    def test_kit_draft_changed_no_zip(self):
        self.s.patch('project', 'p', {'draft_changed': True})
        k = r.kit(self.s, 'p')
        self.assertFalse(k['ready'])
        self.assertIsNone(k['export_zip_url'])
        self.assertIsNone(k['video'])

    def test_kit_has_no_publish_side_effect(self):
        r.kit(self.s, 'p')
        self.assertEqual(self.s.list('matrix_publish_task'), [])
        self.assertEqual(self.s.list('publication_receipt'), [])

    def test_kit_missing_project_still_raises(self):
        with self.assertRaises(KeyError):
            r.kit(self.s, 'nope')


if __name__ == '__main__':
    unittest.main()
