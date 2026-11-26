"""按路径登记既有成片接口的隔离回归。

只用 ffmpeg 合成的短素材与临时库（IP_DATA_DIR 指向 .test-runtime 下的随机目录），
不读不写用户真实成片、业务库和 .runtime。真实 4K 成片的实测另见证据文件。
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

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
import clip_import


class ClipImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.folder = Path(tempfile.mkdtemp(dir=TEST_ROOT))
        cls.fixture = cls.folder / '外部剪辑成片.mp4'
        media.run([media.binary('ffmpeg'), '-y', '-v', 'error',
                   '-f', 'lavfi', '-i', 'color=c=0x29241c:s=320x240:r=20:d=2',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=2',
                   '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', cls.fixture])
        cls.silent = cls.folder / '无音轨.mp4'
        media.run([media.binary('ffmpeg'), '-y', '-v', 'error',
                   '-f', 'lavfi', '-i', 'color=c=black:s=160x120:r=10:d=1',
                   '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', cls.silent])
        cls.broken = cls.folder / '损坏成片.mp4'
        cls.broken.write_bytes(b'not a real mp4 container')

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, path, method='GET', body=None, expected=200):
        headers = {}
        if body is not None:
            body = json.dumps(body, ensure_ascii=False).encode()
            headers = {'Content-Type': 'application/json'}
        req = urllib.request.Request(self.base + path, body, headers, method=method)
        try:
            res = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            res = exc
        with res:
            data = res.read()
            self.assertEqual(res.status, expected, data[:700])
            if 'application/json' in res.headers.get('Content-Type', ''):
                return json.loads(data)
            return data

    def new_clip(self, name, content=None):
        target = Path(tempfile.mkdtemp(dir=TEST_ROOT)) / name
        if content is None:
            shutil.copyfile(self.fixture, target)
        else:
            target.write_bytes(content)
        return target

    def new_project(self, title='外部成片联调项目'):
        return app.STORE.put('project', {'title': title, 'source_id': 'src_test'})

    def test_import_registers_with_ffprobe_checks(self):
        clip = self.new_clip('已验证成片.mp4')
        result = self.request('/api/artifacts/import-path', 'POST',
                              {'path': str(clip), 'note': '手工剪好的成片'}, expected=201)
        artifact = result['artifact']
        self.assertFalse(result['duplicate'])
        self.assertEqual(artifact['kind'], clip_import.ARTIFACT_KIND)
        self.assertEqual(artifact['origin'], 'external_path')
        self.assertEqual(Path(artifact['path']), clip)
        self.assertEqual(artifact['checks']['container'], 'mp4')
        self.assertAlmostEqual(artifact['checks']['duration_ms'], 2000, delta=300)
        self.assertEqual(artifact['checks']['resolution'], '320x240')
        self.assertTrue(artifact['checks']['audio'])
        self.assertTrue(artifact['checks']['ffprobe_verified'])
        self.assertTrue(artifact['checks']['file_exists'])
        self.assertEqual(artifact['review_status'], 'pending')
        # 只做路径引用：原文件仍在，大小不变。
        self.assertTrue(clip.is_file())
        listed = self.request('/api/artifacts/imported')
        self.assertIn(artifact['id'], [x['id'] for x in listed['items']])
        detail = self.request('/api/artifacts/imported/' + artifact['id'])
        self.assertTrue(detail['file']['exists'])
        self.assertTrue(detail['fingerprint_unchanged'])
        # 既有下载路由可直接播放登记后的成片，无需新增播放接口。
        raw = self.request('/api/artifacts/' + artifact['id'])
        self.assertEqual(raw, clip.read_bytes())

    def test_duplicate_import_is_idempotent(self):
        clip = self.new_clip('重复登记.mp4')
        first = self.request('/api/artifacts/import-path', 'POST', {'path': str(clip)}, expected=201)
        before = len(self.request('/api/artifacts/imported')['items'])
        again = self.request('/api/artifacts/import-path', 'POST', {'path': str(clip)}, expected=200)
        after = len(self.request('/api/artifacts/imported')['items'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['artifact']['id'], first['artifact']['id'])
        self.assertEqual(before, after)

    def test_register_rejects_bad_paths_and_files(self):
        cases = {
            '相对路径': {'path': '成片.mp4'},
            '文件不存在': {'path': str(self.folder / '不存在.mp4')},
            '目录': {'path': str(self.folder)},
            '空路径': {'path': ''},
            '无音轨': {'path': str(self.silent)},
            '损坏容器': {'path': str(self.broken)},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                self.request('/api/artifacts/import-path', 'POST', payload, expected=400)

    def test_project_binding_uses_version_protection(self):
        project = self.new_project()
        clip = self.new_clip('挂项目成片.mp4')
        with self.subTest(case='项目版本过期'):
            self.request('/api/artifacts/import-path', 'POST',
                         {'path': str(clip), 'project_id': project['id'],
                          'project_revision': project['revision'] + 5}, expected=409)
        ok = self.request('/api/artifacts/import-path', 'POST',
                          {'path': str(clip), 'project_id': project['id'],
                           'project_revision': project['revision']}, expected=201)
        self.assertEqual(ok['artifact']['project_id'], project['id'])
        other = self.new_project('另一个项目')
        with self.subTest(case='不能静默改挂项目'):
            self.request('/api/artifacts/import-path', 'POST',
                         {'path': str(clip), 'project_id': other['id']}, expected=409)

    def test_reregister_requires_revision(self):
        clip = self.new_clip('重登记成片.mp4')
        first = self.request('/api/artifacts/import-path', 'POST', {'path': str(clip)}, expected=201)
        artifact = first['artifact']
        self.request('/api/artifacts/import-path', 'POST',
                     {'path': str(clip), 'artifact_id': artifact['id'], 'note': '只更新备注'}, expected=400)
        updated = self.request('/api/artifacts/import-path', 'POST',
                               {'path': str(clip), 'artifact_id': artifact['id'],
                                'expected_revision': artifact['revision'], 'note': '只更新备注'}, expected=201)
        self.assertEqual(updated['artifact']['note'], '只更新备注')
        self.request('/api/artifacts/import-path', 'POST',
                     {'path': str(clip), 'artifact_id': artifact['id'],
                      'expected_revision': artifact['revision'], 'note': '旧版本写入'}, expected=409)

    def test_review_validates_and_locks(self):
        clip = self.new_clip('审片成片.mp4')
        artifact = self.request('/api/artifacts/import-path', 'POST',
                                {'path': str(clip)}, expected=201)['artifact']
        self.request('/api/artifacts/imported/%s/review' % artifact['id'], 'POST',
                     {'decision': 'looks_good'}, expected=400)
        self.request('/api/artifacts/imported/%s/review' % artifact['id'], 'POST',
                     {'decision': 'accepted', 'expected_revision': artifact['revision'] - 1}, expected=409)
        reviewed = self.request('/api/artifacts/imported/%s/review' % artifact['id'], 'POST',
                                {'decision': 'accepted', 'note': '本人已看', 'expected_revision': artifact['revision']},
                                expected=200)
        self.assertEqual(reviewed['review_status'], 'accepted')
        self.assertEqual(reviewed['review']['scope'], 'external_clip')
        self.assertEqual(reviewed['review']['checks']['resolution'], '320x240')
        self.assertTrue(app.STORE.get('clip_review', reviewed['review']['id']))
        # 先落盘后改文件：再审片应拒绝，避免用旧校验通过新内容。
        replaced = self.new_clip('被替换成片.mp4')
        other = self.request('/api/artifacts/import-path', 'POST',
                             {'path': str(replaced)}, expected=201)['artifact']
        with replaced.open('ab') as handle:
            handle.write(b'\x00' * 64)
        self.request('/api/artifacts/imported/%s/review' % other['id'], 'POST',
                     {'decision': 'accepted'}, expected=409)

    def test_export_requires_review_and_packs_original_bytes(self):
        clip = self.new_clip('交付成片.mp4')
        artifact = self.request('/api/artifacts/import-path', 'POST',
                                {'path': str(clip)}, expected=201)['artifact']
        self.request('/api/artifacts/imported/%s/export' % artifact['id'], 'GET', expected=409)
        reviewed = self.request('/api/artifacts/imported/%s/review' % artifact['id'], 'POST',
                                {'decision': 'accepted', 'expected_revision': artifact['revision']}, expected=200)
        raw = self.request('/api/artifacts/imported/%s/export' % artifact['id'], 'GET')
        self.assertEqual(raw[:2], b'PK')
        archive_path = Path(tempfile.mkdtemp(dir=TEST_ROOT)) / 'delivery.zip'
        archive_path.write_bytes(raw)
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn('video.mp4', names)
            self.assertIn('clip-info.json', names)
            self.assertEqual(archive.read('video.mp4'), clip.read_bytes())
            manifest = json.loads(archive.read('clip-info.json').decode('utf-8'))
            self.assertEqual(manifest['artifact_id'], artifact['id'])
            self.assertEqual(manifest['info']['resolution'], '320x240')
            self.assertEqual(manifest['review_id'], reviewed['review']['id'])


if __name__ == '__main__':
    unittest.main()
