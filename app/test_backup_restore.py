"""备份/导出/恢复回归用例（合成小数据，几秒跑完）。

运行：tools\\python\\python.exe -m unittest test_backup_restore -v
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_restore
from db import Store


class BackupRestoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='ip-backup-test-'))
        self.data = self.root / 'source-runtime'
        self.data.mkdir()
        self.store = Store(self.data)
        for folder in ('uploads', 'artifacts', 'avatars'):
            (self.data / folder).mkdir()
        (self.data / 'uploads' / 'upload_a.mp4').write_bytes(b'video-bytes' * 1000)
        (self.data / 'artifacts' / 'artifact_a.png').write_bytes(b'png-bytes' * 100)
        (self.data / 'avatars' / 'owner.png').write_bytes(b'avatar-bytes' * 10)
        self.store.put('project', {'title': '演示项目', 'stage': 'writing'})
        self.asset = self.store.put('asset', {'project_id': 'p1', 'path': str(self.data / 'uploads' / 'upload_a.mp4'),
                                              'filename': 'upload_a.mp4'})
        self.artifact = self.store.put('artifact', {'project_id': 'p1', 'kind': 'clean_video',
                                                    'path': str(self.data / 'artifacts' / 'artifact_a.png'),
                                                    'subtitle_path': str(self.data / 'artifacts' / 'missing.srt')})
        self.store.put('cover_template_license', {'template_id': '04-magazine', 'granted': True})
        self.store.put('knowledge', {'title': '认知', 'claim': '主张'})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _zip_copy(self, source, target, manifest_change=None, corrupt=None):
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as new:
            for info in original.infolist():
                raw = original.read(info.filename)
                if info.filename == 'manifest.json' and manifest_change:
                    payload = json.loads(raw)
                    manifest_change(payload)
                    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                elif info.filename == corrupt:
                    raw = raw[:-1] + bytes([raw[-1] ^ 0xFF])
                new.writestr(info, raw)

    def test_export_restore_roundtrip(self):
        exported = backup_restore.export_full(self.data)
        self.assertTrue(Path(exported['path']).is_file())
        target = self.root / 'restored'
        result = backup_restore.restore_full(exported['path'], target)
        self.assertEqual(result['tables']['total'], 5)
        self.assertEqual(result['tables']['overall'], exported['tables']['overall'])
        self.assertEqual(result['files'], exported['files'])
        self.assertGreater(result['path_rewrites'], 0)
        restored_db = sqlite3.connect(str(target / 'workbench.sqlite3'))
        path = restored_db.execute("SELECT data FROM records WHERE kind='asset'").fetchone()[0]
        restored_db.close()
        restored_asset = json.loads(path)['path']
        self.assertTrue(Path(restored_asset).is_file())
        self.assertTrue(str(Path(restored_asset)).startswith(str(target)))
        self.assertEqual((target / 'uploads' / 'upload_a.mp4').read_bytes(), b'video-bytes' * 1000)
        self.assertEqual(backup_restore.status(self.data)['last_export'], exported['filename'])

    def test_corrupted_file_is_rejected_and_target_untouched(self):
        exported = backup_restore.export_full(self.data)
        broken = self.root / 'broken.zip'
        self._zip_copy(exported['path'], broken, corrupt='README.txt')
        target = self.root / 'should-not-exist'
        with self.assertRaises(ValueError) as ctx:
            backup_restore.restore_full(broken, target)
        self.assertIn('校验失败', str(ctx.exception))
        self.assertFalse(target.exists())

    def test_lower_schema_version_is_rejected_with_reason(self):
        exported = backup_restore.export_full(self.data)
        old = self.root / 'old-schema.zip'
        self._zip_copy(exported['path'], old,
                       manifest_change=lambda m: m.update(schema_version=int(m['schema_version']) - 1))
        target = self.root / 'schema-target'
        with self.assertRaises(ValueError) as ctx:
            backup_restore.restore_full(old, target)
        message = str(ctx.exception)
        self.assertIn('低 1 个版本', message)
        self.assertFalse(target.exists())

    def test_restore_accepts_live_db_with_module_tables(self):
        """真实数据目录会在同一库里建 content_index 等派生表，不能因此拒绝同版本备份。"""
        con = sqlite3.connect(str(self.data / 'workbench.sqlite3'))
        con.execute('CREATE TABLE content_index (id TEXT PRIMARY KEY, body TEXT)')
        con.commit()
        con.close()
        exported = backup_restore.export_full(self.data)
        target = self.root / 'restored-with-module-tables'
        result = backup_restore.restore_full(exported['path'], target)
        restored = sqlite3.connect(str(target / 'workbench.sqlite3'))
        names = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        restored.close()
        self.assertIn('content_index', names)
        self.assertGreater(result['tables']['total'], 0)

    def test_incompatible_core_schema_is_still_rejected(self):
        other = self.root / 'other-runtime'
        other.mkdir()
        Store(other)
        con = sqlite3.connect(str(other / 'workbench.sqlite3'))
        con.execute('ALTER TABLE records ADD COLUMN evil TEXT')
        con.commit()
        con.close()
        exported = backup_restore.export_full(other)
        with self.assertRaises(ValueError) as ctx:
            backup_restore.restore_in_place(exported['path'], data_dir=self.data)
        self.assertIn('核心表字段不同', str(ctx.exception))

    def test_full_restore_keeps_current_media_in_pre_restore_backup(self):
        """全量恢复只按备份替换媒体；备份包里没有的当前素材要留在 pre-restore/media。"""
        other = self.root / 'other-runtime'
        other.mkdir()
        Store(other)
        exported = backup_restore.export_full(other)
        result = backup_restore.restore_in_place(exported['path'], data_dir=self.data)
        kept = Path(result['pre_restore_media']) / 'uploads' / 'upload_a.mp4'
        self.assertTrue(kept.is_file())
        self.assertEqual(kept.read_bytes(), b'video-bytes' * 1000)
        self.assertFalse((self.data / 'uploads' / 'upload_a.mp4').exists())

    def test_auto_backup_24h_rule_and_retention(self):
        folder = self.data / 'backups'
        folder.mkdir()
        manual = folder / 'workbench-manual-20261101.db'
        manual.write_bytes(b'manual')
        base = time.time()
        first = backup_restore.maybe_auto_backup(self.data, now_ts=base)
        self.assertTrue(first['created'])
        second = backup_restore.maybe_auto_backup(self.data, now_ts=base + 3600)
        self.assertFalse(second['created'])
        self.assertIn('next_due_hours', second)
        for index in range(8):
            fake = folder / ('auto-2020010{0}.sqlite3'.format(index + 1))
            fake.write_bytes(b'fake')
            os.utime(fake, (base - 86400 * (20 - index), base - 86400 * (20 - index)))
        third = backup_restore.maybe_auto_backup(self.data, now_ts=base + 25 * 3600)
        self.assertTrue(third['created'])
        autos = sorted(folder.glob('auto-*.sqlite3'))
        self.assertEqual(len(autos), 7)
        self.assertTrue(manual.is_file())
        self.assertTrue(all(p.name.startswith('auto-') for p in autos))


if __name__ == '__main__':
    unittest.main(verbosity=2)
