"""半自动发布包自测：全材料 / 缺封面 / 缺标题。

只验证本地目录整理结果，不触碰 app.py、app/web，也不调用平台。
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import Store
import publish_kit


class PublishKitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_data = os.environ.get('IP_DATA_DIR')
        os.environ['IP_DATA_DIR'] = self.temp.name
        self.data = Path(self.temp.name)
        self.pkg = self.data / 'packages' / 'job_full'
        self.pkg.mkdir(parents=True)
        (self.pkg / 'packaged.mp4').write_bytes(b'fake-video-bytes')
        (self.pkg / 'cover.jpg').write_bytes(b'fake-cover-bytes')

        self.store = Store(self.data)
        self.store.put('script_version', {'project_id': 'p', 'parts': [
            {'role': 'hook', 'text': '钩子正文。'},
            {'role': 'ending', 'text': '结尾正文。'}]}, 'v')
        self.store.put('project', {
            'title': '原作品标题', 'confirmed_version_id': 'v', 'draft_changed': False,
            'publication_pack': {'script_version_id': 'v', 'selected_title': '发布用标题',
                                 'caption': '第一段正文。\n\n#旧话题 第二段正文。', 'tags': ['创业', '#认知']},
        }, 'p')
        self.store.put('artifact', {'project_id': 'p', 'script_version_id': 'v', 'kind': 'packaged_video',
                                    'path': str(self.pkg / 'packaged.mp4'), 'filename': '成片.mp4',
                                    'cover_id': 'c', 'info': {'duration_ms': 60000, 'width': 1080, 'height': 1920}},
                        'a')
        self.store.put('artifact', {'project_id': 'p', 'script_version_id': 'v', 'kind': 'cover',
                                    'path': str(self.pkg / 'cover.jpg'), 'filename': '封面.jpg'}, 'c')
        self.store.put('job', {'project_id': 'p', 'kind': 'prepare_video', 'status': 'succeeded',
                               'result': {'artifact': {'id': 'a'}}}, 'job_full')

    def tearDown(self):
        if self.old_data is None:
            os.environ.pop('IP_DATA_DIR', None)
        else:
            os.environ['IP_DATA_DIR'] = self.old_data
        self.temp.cleanup()

    def _build(self):
        return publish_kit.build_publish_kit('job_full')

    def test_full_materials(self):
        result = self._build()
        out = Path(result['out_dir'])
        self.assertEqual(out, self.data / 'publish' / 'job_full')
        self.assertEqual(result['missing'], [])
        self.assertEqual((out / 'title.txt').read_text(encoding='utf-8'), '发布用标题\n')
        self.assertEqual((out / 'caption.txt').read_text(encoding='utf-8'),
                         '第一段正文。\n\n第二段正文。\n')
        # 正文里的 #旧话题 会被收敛到 hashtags.txt，不会留在 caption.txt。
        self.assertEqual((out / 'hashtags.txt').read_text(encoding='utf-8'), '#创业\n#认知\n#旧话题\n')
        self.assertEqual((out / 'copy-all.txt').read_text(encoding='utf-8'),
                         '发布用标题\n\n第一段正文。\n\n第二段正文。\n\n#创业 #认知 #旧话题\n')
        self.assertTrue((out / 'video.mp4').is_file())
        self.assertTrue((out / 'cover.jpg').is_file())
        self.assertTrue((out / '发布指引.md').is_file())
        self.assertTrue((out / 'manifest.json').is_file())
        manifest = result['manifest']
        self.assertEqual(manifest['video']['duration_ms'], 60000)
        self.assertEqual(manifest['video']['width'], 1080)
        self.assertEqual(manifest['source']['video']['artifact_id'], 'a')
        self.assertEqual(manifest['source']['cover']['artifact_id'], 'c')
        for name, digest in manifest['files'].items():
            actual = hashlib.sha256((out / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, name)
        guide = (out / '发布指引.md').read_text(encoding='utf-8')
        for step in ('第 1 步', '第 2 步', '第 3 步', 'creator.douyin.com'):
            self.assertIn(step, guide)

    def test_missing_cover(self):
        self.store.patch('artifact', 'a', {'cover_id': None})
        (self.pkg / 'cover.jpg').unlink()
        result = self._build()
        out = Path(result['out_dir'])
        self.assertEqual(result['missing'], ['cover'])
        self.assertFalse(list(out.glob('cover.*')))
        self.assertTrue(any('第 1 帧' in item for item in result['warnings']))
        guide = (out / '发布指引.md').read_text(encoding='utf-8')
        self.assertIn('本目录没有 cover 文件', guide)
        self.assertIn('"missing": [\n    "cover"\n  ]', (out / 'manifest.json').read_text(encoding='utf-8'))

    def test_missing_title(self):
        pack = self.store.get('project', 'p')['publication_pack']
        pack['selected_title'] = ''
        self.store.patch('project', 'p', {'title': '', 'publication_pack': pack})
        result = self._build()
        out = Path(result['out_dir'])
        self.assertIn('title', result['missing'])
        self.assertEqual((out / 'title.txt').read_text(encoding='utf-8'), '未填标题\n')
        self.assertEqual((out / 'copy-all.txt').read_text(encoding='utf-8'),
                         '未填标题\n\n第一段正文。\n\n第二段正文。\n\n#创业 #认知 #旧话题\n')
        self.assertIn('title', result['manifest']['missing'])


if __name__ == '__main__':
    unittest.main()
