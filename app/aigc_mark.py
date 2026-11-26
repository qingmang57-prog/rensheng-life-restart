# -*- coding: utf-8 -*-
"""AIGC 隐式标识（元数据层）：往 MP4 里写可核验的 AI 生成合成标识。

依据：
- GB 45438-2025《网络安全技术 人工智能生成合成内容标识方法》附录 E：隐式标识要素
  （生成合成标签 Label、服务提供者 ContentProducer、内容制作编号 ProduceID、
  传播服务提供者 ContentPropagator、传播编号 PropagateID、保留字段 ReservedCode1/2）。
- TC260《人工智能生成合成内容标识方法 文件元数据隐式标识 视频文件》(V1.0-202508)
  6.2 XMP 方案：RDF 增加命名空间 TC260（URI http://www.tc260.org.cn/ns/AIGC/1.0/），
  键为 AIGC，值为上面字段组成的 JSON 字符串。
- XMP 在 ISO BMFF(MP4) 里的标准载体：顶层 `uuid` box，UUID 为
  BE7ACFCB-97A9-42E8-9C71-999491E3AFAC（Adobe XMP Part 3 规定）。

本模块只做两件事：stamp() 写入 / read() 读出。写入不重编码视频，只在文件尾部
增删一个顶层 box，因此对播放与画质无影响；重复写入会替换旧标识，不会叠加。
"""
import json
import os
import re
from pathlib import Path

XMP_UUID = bytes.fromhex('be7acfcb97a942e89c71999491e3afac')
TC260_NS = 'http://www.tc260.org.cn/ns/AIGC/1.0/'
AIGC_RE = re.compile(r'<TC260:AIGC>(.*?)</TC260:AIGC>', re.S)

XMP_TEMPLATE = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
    ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
    '  <rdf:Description rdf:about="" xmlns:TC260="{ns}">\n'
    '   <TC260:AIGC>{payload}</TC260:AIGC>\n'
    '  </rdf:Description>\n'
    ' </rdf:RDF>\n'
    '</x:xmpmeta>\n'
    '<?xpacket end="w"?>'
)


def _xml_escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def build_fields(producer, produce_id, label='1'):
    """按 GB 45438-2025 附录 E 组装标识字段。label=1 表示属于 AI 生成合成内容。"""
    return {
        'Label': str(label),
        'ContentProducer': str(producer or ''),
        'ProduceID': str(produce_id or ''),
        'ReservedCode1': '',
        'ContentPropagator': '',
        'PropagateID': '',
        'ReservedCode2': '',
    }


def build_packet(fields):
    payload = _xml_escape(json.dumps(fields, ensure_ascii=False, separators=(',', ':')))
    return XMP_TEMPLATE.format(ns=TC260_NS, payload=payload).encode('utf-8')


def _uuid_box(payload):
    body = XMP_UUID + payload
    return (8 + len(body)).to_bytes(4, 'big') + b'uuid' + body


def _iter_boxes(fh, total):
    pos = 0
    while pos + 8 <= total:
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            break
        size = int.from_bytes(head[:4], 'big')
        typ = head[4:8]
        header = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                break
            size = int.from_bytes(ext, 'big')
            header = 16
        elif size == 0:
            size = total - pos
        if size < header or pos + size > total:
            raise ValueError('MP4 box 结构损坏（pos=%d，size=%d，总长=%d）' % (pos, size, total))
        yield pos, size, typ
        pos += size  # 必须前进，否则同一个 box 会被无限重复


def _copy_range(src, dst, start, size, chunk=1024 * 1024):
    src.seek(start)
    left = size
    while left > 0:
        buf = src.read(min(chunk, left))
        if not buf:
            raise IOError('读取文件中断')
        dst.write(buf)
        left -= len(buf)


def has_mark(path):
    return read(path).get('present', False)


def read(path):
    """读出我们写入的标识。返回 {present, offset, fields, payload_len}。"""
    path = Path(path)
    info = {'present': False, 'path': str(path)}
    if not path.is_file():
        return info
    total = path.stat().st_size
    with open(path, 'rb') as fh:
        for pos, size, typ in _iter_boxes(fh, total):
            if typ != b'uuid' or size < 24:
                continue
            fh.seek(pos + 8)
            if fh.read(16) != XMP_UUID:
                continue
            payload = fh.read(size - 24)
            m = AIGC_RE.search(payload.decode('utf-8', 'replace'))
            if not m:
                continue
            try:
                fields = json.loads(m.group(1))
            except ValueError:
                fields = {'raw': m.group(1)}
            info.update({'present': True, 'offset': pos, 'payload_len': len(payload),
                         'fields': fields})
            return info
    return info


def stamp(path, producer, produce_id='', label='1'):
    """把隐式标识写进 MP4；已有本模块标识则替换。返回 read() 的结果。"""
    path = Path(path)
    if not path.is_file():
        raise IOError('待标识的文件不存在：%s' % path)
    box = _uuid_box(build_packet(build_fields(producer, produce_id, label)))
    tmp = path.with_name(path.name + '.aigc.tmp')
    total = path.stat().st_size
    try:
        with open(path, 'rb') as src, open(tmp, 'wb') as dst:
            for pos, size, typ in _iter_boxes(src, total):
                if typ == b'uuid' and size >= 24:
                    src.seek(pos + 8)
                    if src.read(16) == XMP_UUID:
                        continue  # 旧的 XMP box 丢掉，后面写入新的
                _copy_range(src, dst, pos, size)
            dst.write(box)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return read(path)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'read':
        print(json.dumps(read(sys.argv[2]), ensure_ascii=False, indent=2))
