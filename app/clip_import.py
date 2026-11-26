"""按路径登记工作台之外手工剪好的成片（external clip import）。

仅做路径引用：GB 级成片不复制、不移动，登记条目里保存绝对路径与 ffprobe 实证。
写入前必须通过 ffprobe 校验（容器/时长/分辨率/音轨），校验不过直接拒绝，不落库。
自身只新增 artifact(kind=imported_video)、clip_review、clip_package 三类记录，
不改动既有项目、净剪、包装、审片、交付链路的任何语义。
"""
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import media
from db import Conflict, now

ARTIFACT_KIND = 'imported_video'
REVIEW_KIND = 'clip_review'
PACKAGE_KIND = 'clip_package'
REVIEW_DECISIONS = ('accepted', 'rejected')
DELIVERY_CONTAINERS = {'mp4', 'mkv', 'webm'}
CONTAINER_ALIASES = {
    'mov': 'mp4', 'mp4': 'mp4', 'm4a': 'mp4', '3gp': 'mp4', '3g2': 'mp4', 'mj2': 'mp4',
    'matroska': 'mkv', 'webm': 'webm', 'flv': 'flv', 'mpegts': 'ts', 'avi': 'avi',
}


def _container(format_name):
    for token in str(format_name or '').split(','):
        token = token.strip().lower()
        if token in CONTAINER_ALIASES:
            return CONTAINER_ALIASES[token]
    return str(format_name or 'unknown').split(',')[0][:40] or 'unknown'


def _fps(stream):
    for key in ('avg_frame_rate', 'r_frame_rate'):
        value = str(stream.get(key) or '')
        match = re.fullmatch(r'(\d+)/(\d+)', value)
        if match and int(match.group(2)):
            rate = int(match.group(1)) / int(match.group(2))
            if rate > 0:
                return round(rate, 3)
    return None


def inspect(path):
    """ffprobe 校验并返回成片实证；损坏/不完整文件在 media.run 阶段即失败。"""
    exe = media.binary('ffprobe')
    if not exe:
        raise ValueError('未找到 ffprobe，请配置 IP_FFPROBE 或便携包 tools/ffmpeg。')
    try:
        run = media.run([exe, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], 60)
    except media.MediaProcessError as exc:
        raise ValueError('ffprobe 无法解析这个文件，可能未封装完成或已损坏：' + str(exc)[-500:])
    try:
        obj = json.loads(run.stdout or '{}')
    except ValueError:
        raise ValueError('ffprobe 输出不是合法 JSON，无法登记。')
    streams = obj.get('streams') or []
    videos = [s for s in streams if s.get('codec_type') == 'video'
              and not (s.get('disposition') or {}).get('attached_pic')]
    if not videos:
        raise ValueError('文件里没有视频轨，不能登记为成片。')
    video = videos[0]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    fmt = obj.get('format') or {}
    duration = float(fmt.get('duration') or video.get('duration') or 0)
    if duration <= 0:
        raise ValueError('读不出成片时长（文件可能未封装完成），登记失败。')
    if duration > 1800:
        raise ValueError('登记接口仅支持 0–30 分钟的成片。')
    width, height = int(video.get('width') or 0), int(video.get('height') or 0)
    if width <= 0 or height <= 0:
        raise ValueError('读不出视频分辨率，登记失败。')
    if not audios:
        raise ValueError('成片没有音轨，不能登记为交付用成片。')
    container = _container(fmt.get('format_name'))
    if container not in DELIVERY_CONTAINERS:
        raise ValueError('容器 %s 不在交付白名单（mp4/mkv/webm）内。' % container)
    return {
        'container': container,
        'format_name': str(fmt.get('format_name') or '')[:80],
        'duration_ms': round(duration * 1000),
        'width': width,
        'height': height,
        'resolution': '%dx%d' % (width, height),
        'fps': _fps(video),
        'video_codec': video.get('codec_name'),
        'audio_codec': audios[0].get('codec_name'),
        'audio_channels': audios[0].get('channels'),
        'size': int(fmt.get('size') or 0),
    }


def resolve_path(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('请提供成片文件的完整路径。')
    if len(raw) > 4096:
        raise ValueError('路径过长。')
    text = raw.strip().strip('"')
    if text.startswith('~'):
        raise ValueError('请提供绝对路径，不要使用 ~ 或环境变量。')
    path = Path(os.path.abspath(text))
    if not path.is_absolute():
        raise ValueError('请提供绝对路径（盘符开头，指向具体的成片文件）。')
    if path.is_dir():
        raise ValueError('给的是目录，请提供具体的成片文件路径。')
    if not path.is_file():
        raise ValueError('文件不存在或不可读：' + str(path))
    return path


def fingerprint(path):
    """路径 + 大小 + 修改时间 + 前 1MiB 内容的快速指纹；不读取整个 GB 级文件。"""
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(str(path).lower().encode('utf-8'))
    digest.update(('%d:%d' % (stat.st_size, stat.st_mtime_ns)).encode('ascii'))
    with path.open('rb') as handle:
        digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()


def _existing(store, digest):
    for item in store.list('artifact', 10000):
        if item.get('kind') == ARTIFACT_KIND and item.get('source_fingerprint') == digest:
            return item
    return None


def _artifact_view(item):
    info = item.get('info') or {}
    return {**item, 'checks': {
        'container': info.get('container'),
        'duration_ms': info.get('duration_ms'),
        'resolution': info.get('resolution'),
        'audio': bool(info.get('audio_codec')),
        'ffprobe_verified': bool(item.get('ffprobe_verified')),
        'file_exists': Path(str(item.get('path') or '')).is_file(),
    }}


def register(store, payload):
    """把一个既有成片登记为 artifact；重复登记同一文件返回原记录，不重复写入。"""
    if not isinstance(payload, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    project_id = str(payload.get('project_id') or '').strip() or None
    project = None
    if project_id:
        project = store.get('project', project_id)
        if project is None:
            raise ValueError('项目不存在，无法登记成片：' + project_id)
        expected = payload.get('project_revision')
        if expected is not None and int(expected) != project['revision']:
            raise Conflict('项目已在另一页面更新（版本 %s），请刷新后重试。' % project['revision'])
    path = resolve_path(payload.get('path'))
    info = inspect(path)
    digest = fingerprint(path)
    artifact_id = str(payload.get('artifact_id') or '').strip() or None
    expected = payload.get('expected_revision')
    if artifact_id:
        current = store.get('artifact', artifact_id)
        if current is None or current.get('kind') != ARTIFACT_KIND:
            raise KeyError('成片登记记录不存在。')
        if expected is None:
            raise ValueError('更新已登记成片需要提供 expected_revision（版本保护）。')
        if project_id and current.get('project_id') != project_id:
            raise Conflict('这个成片已登记到其他项目（%s），不能静默改挂。' % current.get('project_id'))
    else:
        if expected is not None:
            raise ValueError('expected_revision 只在更新已登记成片时使用。')
        prior = _existing(store, digest)
        if prior is not None:
            if project_id and prior.get('project_id') != project_id:
                raise Conflict('这个成片已登记到其他项目（%s），不能静默改挂。' % prior.get('project_id'))
            return {'artifact': _artifact_view(prior), 'duplicate': True,
                    'checks': _artifact_view(prior)['checks'],
                    'note': '该文件已按同一路径登记过，未重复写入；如需更新元数据请带 artifact_id 与 expected_revision。'}
    if expected is not None:
        expected = int(expected)
    changes = {
        'kind': ARTIFACT_KIND,
        'origin': 'external_path',
        'path': str(path),
        'filename': path.name,
        'project_id': project_id,
        'project_revision_at_import': project['revision'] if project else None,
        'info': info,
        'resolution': info['resolution'],
        'duration_ms': info['duration_ms'],
        'audio': bool(info['audio_codec']),
        'source_fingerprint': digest,
        'ffprobe_verified': True,
        'registered_at': now(),
        'review_status': 'pending',
        'reviewed_artifact_id': None,
        'external': True,
        'note': str(payload.get('note') or '')[:2000],
        'script_version_id': None,
    }
    saved = store.put('artifact', changes, key=artifact_id, expected=expected)
    view = _artifact_view(saved)
    return {'artifact': view, 'duplicate': False, 'checks': view['checks'],
            'note': '已按路径登记，未复制文件；成片仍留在原路径。'}


def list_imported(store, project_id=None):
    items = [x for x in store.list('artifact', 10000) if x.get('kind') == ARTIFACT_KIND]
    if project_id:
        items = [x for x in items if x.get('project_id') == project_id]
    return [_artifact_view(x) for x in items]


def describe(store, artifact_id):
    item = store.get('artifact', artifact_id)
    if item is None or item.get('kind') != ARTIFACT_KIND:
        raise KeyError('成片登记记录不存在。')
    view = _artifact_view(item)
    path = Path(item.get('path') or '')
    view['stale'] = not path.is_file()
    if path.is_file():
        stat = path.stat()
        view['file'] = {'exists': True, 'size': stat.st_size, 'mtime': stat.st_mtime}
        view['fingerprint_unchanged'] = fingerprint(path) == item.get('source_fingerprint')
    else:
        view['file'] = {'exists': False, 'size': None, 'mtime': None}
        view['fingerprint_unchanged'] = False
    return view


def review(store, artifact_id, payload):
    """审片：通过前重新 ffprobe 校验；乐观锁保护，防止旧页面覆盖新状态。"""
    if not isinstance(payload, dict):
        raise ValueError('请求体必须是 JSON 对象。')
    item = store.get('artifact', artifact_id)
    if item is None or item.get('kind') != ARTIFACT_KIND:
        raise KeyError('成片登记记录不存在。')
    decision = str(payload.get('decision') or 'accepted').strip()
    if decision not in REVIEW_DECISIONS:
        raise ValueError('审片结论只能是 accepted 或 rejected。')
    info = None
    if decision == 'accepted':
        path = Path(item.get('path') or '')
        if not path.is_file():
            raise ValueError('成片文件已不在原路径，不能审片通过：' + str(path))
        info = inspect(path)
        if fingerprint(path) != item.get('source_fingerprint'):
            raise Conflict('原路径文件已变化（可能被重新渲染或替换），请重新登记后再审片。')
    expected = payload.get('expected_revision')
    if expected is not None:
        expected = int(expected)
    record = store.put('clip_review', {
        'artifact_id': artifact_id, 'project_id': item.get('project_id'),
        'decision': decision, 'scope': 'external_clip',
        'checks': _artifact_view(item)['checks'] if info is None else {
            'container': info['container'], 'duration_ms': info['duration_ms'],
            'resolution': info['resolution'], 'audio': bool(info['audio_codec']),
            'ffprobe_verified': True, 'file_exists': True},
        'note': str(payload.get('note') or '')[:2000],
        'reviewed_at': now(),
    })
    updated = store.patch('artifact', artifact_id, {
        'review_status': decision,
        'reviewed_at': now(),
        'reviewed_artifact_id': record['id'],
        'review_note': record['note'],
    }, expected if expected is not None else item['revision'])
    view = _artifact_view(updated)
    view['review'] = record
    return view


def export_pack(store, artifact_id, package_dir):
    """审片通过后把成片打成 ZIP（视频用 STORED，不做二次压缩/转码）。"""
    item = store.get('artifact', artifact_id)
    if item is None or item.get('kind') != ARTIFACT_KIND:
        raise KeyError('成片登记记录不存在。')
    if item.get('review_status') != 'accepted':
        raise Conflict('请先完成审片（accepted）再导出交付包。')
    path = Path(item.get('path') or '')
    if not path.is_file():
        raise ValueError('成片文件已不在原路径，无法导出：' + str(path))
    if fingerprint(path) != item.get('source_fingerprint'):
        raise Conflict('原路径文件已变化，请重新登记并审片后再导出。')
    package_dir = Path(package_dir)
    package_dir.mkdir(parents=True, exist_ok=True)
    target = package_dir / ('clip-%s-r%d.zip' % (artifact_id, item['revision']))
    if target.is_file():
        return target, item
    temporary = package_dir / ('.clip-%s.tmp' % artifact_id)
    info = inspect(path)
    manifest = {
        'artifact_id': artifact_id, 'kind': ARTIFACT_KIND, 'source_path': str(path),
        'filename': path.name, 'info': info, 'review_status': item.get('review_status'),
        'review_id': item.get('reviewed_artifact_id'), 'note': item.get('note', ''),
        'exported_at': now(), 'delivery_scope': 'local_single_owner',
    }
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.write(path, 'video' + path.suffix.lower(), compress_type=zipfile.ZIP_STORED)
        archive.writestr('clip-info.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('README.txt',
                         '这是工作台之外手工剪好的成片，按原路径登记后审片通过。\n'
                         '内容未转码、未加水印，视频文件与原路径逐字节一致（ZIP 内 STORED）。\n'
                         '尚未发送到任何社交平台。\n')
    temporary.replace(target)
    return target, item


def route(handler, method, bits, query, store, data_dir):
    """clip_import 的路由注册入口；返回 True 表示已响应，由 app.py 调用。"""
    if bits[:3] == ['api', 'artifacts', 'import-path'] and len(bits) == 3 and method == 'POST':
        result = register(store, handler.body())
        handler.json(result, 200 if result.get('duplicate') else 201)
        return True
    if bits[:3] == ['api', 'artifacts', 'imported'] and len(bits) == 3 and method == 'GET':
        project_id = (query.get('project_id') or [''])[0].strip() or None
        handler.json({'items': list_imported(store, project_id)})
        return True
    if bits[:3] == ['api', 'artifacts', 'imported'] and len(bits) >= 4:
        artifact_id = bits[3]
        if len(bits) == 4 and method == 'GET':
            handler.json(describe(store, artifact_id))
            return True
        if len(bits) == 5 and bits[4] == 'review' and method == 'POST':
            handler.json(review(store, artifact_id, handler.body()))
            return True
        if len(bits) == 5 and bits[4] == 'export' and method == 'GET':
            target, item = export_pack(store, artifact_id, Path(data_dir) / 'packages')
            handler.send_file(target, download=True)
            return True
    return False
