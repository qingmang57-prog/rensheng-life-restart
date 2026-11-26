# -*- coding: utf-8 -*-
"""AIGC 标识回归防线（2026-11-16）。

背景：`_iter_boxes()` 曾漏掉 `pos += size`，导致 read()/has_mark()/stamp() 死循环，
stamp() 还会把临时文件无限写大。三个断言用子进程限时跑，回归时会直接失败而不是挂住。

跑法：python -m unittest test_aigc_mark -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TIMEOUT = 10


def make_mp4(path):
    """最小可解析 MP4：ftyp + moov + mdat，box 头都是规范的 8 字节。"""
    ftyp_body = b'isom' + (0).to_bytes(4, 'big') + b'isommp42'
    data = (8 + len(ftyp_body)).to_bytes(4, 'big') + b'ftyp' + ftyp_body
    data += (8).to_bytes(4, 'big') + b'moov'
    data += (72).to_bytes(4, 'big') + b'mdat' + b'\x00' * 64
    path.write_bytes(data)
    return path


def run_child(code):
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                          timeout=TIMEOUT, cwd=str(HERE))


class AigcMarkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def child(self, body):
        code = ('import sys;sys.path.insert(0,%r);import aigc_mark,json;'
                'out=%s;print(json.dumps(out,ensure_ascii=False))') % (str(HERE), body)
        return run_child(code)

    def test_read_terminates(self):
        path = make_mp4(self.dir / 'plain.mp4')
        proc = self.child('aigc_mark.read(%r)' % str(path))
        self.assertEqual(proc.returncode, 0, 'read() 未正常结束：%s' % proc.stderr[-400:])
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(info['present'])

    def test_stamp_writes_once_and_terminates(self):
        path = make_mp4(self.dir / 'clip.mp4')
        before = path.stat().st_size
        proc = self.child('aigc_mark.stamp(%r,"QA厂商","proj_qa")' % str(path))
        self.assertEqual(proc.returncode, 0, 'stamp() 未正常结束：%s' % proc.stderr[-400:])
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(info['present'])
        self.assertEqual(info['fields']['ProduceID'], 'proj_qa')
        self.assertFalse(Path(str(path) + '.aigc.tmp').exists())
        self.assertLess(path.stat().st_size - before, 64 * 1024, 'stamp() 写出的文件异常膨胀')

    def test_stamp_replaces_instead_of_growing(self):
        path = make_mp4(self.dir / 'again.mp4')
        self.child('aigc_mark.stamp(%r,"QA厂商","proj_one")' % str(path))
        once = path.stat().st_size
        proc = self.child('aigc_mark.stamp(%r,"QA厂商","proj_two")' % str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        info = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(info['fields']['ProduceID'], 'proj_two')
        self.assertEqual(path.stat().st_size, once, '重复写入不应叠加 box')

    def test_corrupt_box_raises_instead_of_looping(self):
        path = self.dir / 'broken.mp4'
        path.write_bytes((4).to_bytes(4, 'big') + b'ftyp' + b'\x00' * 32)
        proc = self.child('aigc_mark.read(%r)' % str(path))
        self.assertNotEqual(proc.returncode, 0, '损坏结构应抛错，而不是静默返回')
        self.assertIn('结构损坏', proc.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
