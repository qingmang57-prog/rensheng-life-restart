"""Reuse the existing video resolver and local ASR; keep manuscripts on disk."""
import json
from pathlib import Path
import re
import time

from db import Conflict, now, uid
from growth_service import LOCK

ACTIVE = ('queued', 'running')


def source_url(value):
    match = re.search(r'https?://[^\s<>"\u3000]+', str(value or ''))
    return match.group(0).rstrip('。，,；;！!）)】}') if match else str(value or '').strip()


def validate_video_url(value):
    import video_intake
    info = video_intake.share_info(value)
    if not info.get('url') or info['platform'] != 'douyin':
        raise ValueError('目前支持抖音视频链接转文字稿。此链接暂不能直取视频；可先使用内容工厂的本地音视频转写。')
    return info['url']


def owned_directory(store, key):
    if not isinstance(key, str) or not re.fullmatch(r'growth_fragment_(?:[0-9a-f]{16}|[ak]_[0-9a-f]{20})', key):
        raise ValueError('碎片编号格式无效。')
    root = store.root.resolve()
    base = (root / 'growth-transcripts').resolve()
    target = (base / key).resolve()
    if not base.is_relative_to(root) or target.parent != base or target == base:
        raise ValueError('文字稿目录不在本工作台范围内。')
    if (root / 'growth-transcripts').is_symlink() or (base / key).is_symlink():
        raise ValueError('文字稿目录不能是外部链接。')
    return target


def transcript_file(store, item, extension='txt'):
    job_id = item.get('transcript_job_id', '')
    if not re.fullmatch(r'job_[0-9a-f]{16}', job_id):
        raise ValueError('这条碎片还没有生成本地文字稿。')
    directory = owned_directory(store, item['id'])
    target = (directory / job_id / ('transcript.' + ('md' if extension == 'md' else 'txt'))).resolve()
    if not target.is_relative_to(directory) or not target.is_file():
        raise ValueError('没有找到本地原稿文件，请重新转写。')
    return target


def decorate(store, item):
    result = dict(item)
    job = store.get('job', item.get('transcription_job_id', ''))
    if job:
        result['transcription'] = {k: job.get(k) for k in ('id', 'status', 'stage', 'error', 'finished_at')}
    result['transcript_available'] = False
    if item.get('transcript_job_id'):
        try:
            result['transcript_local_path'] = str(transcript_file(store, item))
            result['transcript_available'] = True
        except ValueError:
            pass
    return result


def start(store, key, revision, submit_job):
    with LOCK:
        item = store.get('growth_fragment', key)
        if not item:
            raise KeyError(key)
        if item.get('archived'):
            raise ValueError('请先恢复这条碎片，再转写视频。')
        prior = store.get('job', item.get('transcription_job_id', ''))
        if prior and prior.get('status') in ACTIVE:
            return decorate(store, item)
        if type(revision) is not int or revision != item['revision']:
            raise Conflict('碎片已更新，请刷新后再转写。')
        url = validate_video_url(item.get('source_url', ''))
        job = submit_job(key, 'growth_transcribe', {'fragment_id': key, 'revision': revision, 'url': url},
                         lambda job_id: run(store, item, job_id))
        updated = store.patch('growth_fragment', key, {'transcription_job_id': job['id']}, revision)
        return decorate(store, updated)


def run(store, snapshot, job_id):
    import local_asr
    import media
    import video_intake

    def progress(message):
        store.patch('job', job_id, {'stage': str(message)[:200]})

    # start() holds the same lock until the job reference is committed.
    with LOCK:
        current = store.get('growth_fragment', snapshot['id'])
        if not current or current.get('transcription_job_id') != job_id:
            raise Conflict('碎片已变化，没有启动转写。')
    progress('检查本机识别环境')
    ready = local_asr.preflight()
    if not ready.get('ok'):
        raise ValueError('本机暂不能转写：' + '；'.join(ready.get('problems') or ['环境尚未就绪']) + '。释放内存或准备好本地模型后可重试；不会自动调用付费服务。')
    progress('读取视频播放地址')
    # 2026-11-24（t174，吸收客户端已验证修复）：免登录快路径优先——resolve_share() 是纯 HTTP
    # 的抖音 Feed 解析，不需要登录；只有它拿不到才回退浏览器解析（browser_resolve 需要登录态）。
    share_url = snapshot['source_url']
    fast_error = ''
    resolved = None
    try:
        resolved = video_intake.resolve_share(share_url)
    except Exception as exc:                      # 快路径失败：记下真实原因，继续回退
        fast_error = str(exc).strip()[:200] or exc.__class__.__name__
        resolved = None
    media_url = (resolved or {}).get('media_url')
    if not media_url:
        try:
            resolved = video_intake.browser_resolve(share_url)
            media_url = (resolved or {}).get('media_url')
        except Exception as exc:                  # 回退也失败：把两条真实原因一起还回去
            raise ValueError(
                '拿不到这条视频的播放地址。免登录解析失败：%s；浏览器解析失败：%s。'
                '可以换一条新鲜分享链接，或直接粘贴文案/上传视频。'
                % (fast_error or '未返回可下载地址', str(exc).strip()[:200] or exc.__class__.__name__))
    if not media_url:
        raise ValueError(
            '平台没有返回可下载的视频地址，尚未生成文字稿。免登录解析结果：%s。'
            '可以换一条新鲜分享链接，或直接粘贴文案/上传视频。' % (fast_error or '未返回可下载地址'))
    directory = owned_directory(store, snapshot['id']) / job_id
    directory.mkdir(parents=True, exist_ok=False)
    video, audio = directory / 'video.mp4', directory / 'audio.wav'
    try:
        progress('下载视频到本机')
        video_intake.download_media(media_url, video)
        progress('提取视频音轨')
        media.extract_audio(video, audio)
        progress('本机转写口播内容')
        # The resolver's browser may still be releasing memory. Respect the existing
        # ASR floor and wait once, bounded; never lower it or switch to a cloud route.
        deadline = time.monotonic() + 15
        while True:
            readiness = local_asr.preflight()
            if readiness.get('ok'):
                break
            if not readiness.get('memory_low') or time.monotonic() >= deadline:
                raise ValueError('本机暂不能转写：' + '；'.join(readiness.get('problems') or ['环境尚未就绪']) + '。请准备好本机环境后重试。')
            progress('等待本机内存可用，再继续识别')
            time.sleep(.5)
        try:
            result = local_asr.transcribe(audio, stage=progress)
        except Exception as exc:
            reason = str(exc).split(getattr(local_asr, 'CLOUD_HINT', '\0'))[0].strip()
            raise ValueError('本机转写未完成：' + reason[:500] + ' 请处理后重试；没有调用付费转写。') from None
        manuscript = str(result.get('text') or '').strip()
        if not manuscript:
            raise ValueError('未识别到口播文字，尚未生成文字稿。')
        if len(manuscript) > 120000:
            raise ValueError('转写超过 12 万字，请分段处理；没有截断或覆盖碎片。')
        title = str((resolved.get('metadata') or {}).get('title') or snapshot['title'])[:160]
        txt = directory / 'transcript.txt'
        md = directory / 'transcript.md'
        txt.write_text(manuscript, encoding='utf-8-sig')
        md.write_text('# ' + title + '\n\n来源：' + snapshot['source_url'] + '\n\n' + manuscript + '\n', encoding='utf-8-sig')
        (directory / 'transcript.json').write_text(json.dumps({'source_url': snapshot['source_url'], 'title': title,
            'method': 'local_asr', 'created_at': now(), 'cues': result.get('cues') or [], 'words': result.get('words') or []}, ensure_ascii=False, indent=2), encoding='utf-8')
        with LOCK:
            fresh = store.get('growth_fragment', snapshot['id'])
            if not fresh or fresh.get('transcription_job_id') != job_id or fresh.get('source_url') != snapshot.get('source_url'):
                raise Conflict('来源链接已变更，转写文件保存在本机，但没有写进新的碎片。')
            changes = {'transcript_text': manuscript, 'transcript_source_url': snapshot['source_url'],
                       'transcript_job_id': job_id, 'transcript_created_at': now()}
            # User notes, tags, endorsement and recorded date stay authoritative.
            if not fresh.get('text'):
                changes['text'] = manuscript[:40000]
            from urllib.parse import urlsplit
            if fresh['title'] in ('', urlsplit(snapshot['source_url']).hostname):
                changes['title'] = title
            store.patch('growth_fragment', fresh['id'], changes, fresh['revision'])
        return {'fragment_id': snapshot['id'], 'chars': len(manuscript), 'local_path': str(txt), 'route': 'local', 'stage': '文字稿已保存在本机'}
    finally:
        # Only this task's generated media; the source video and user uploads are untouched.
        for generated in (video, audio):
            if generated.resolve().parent == directory.resolve() and generated.is_file():
                try:
                    generated.unlink()
                except OSError:
                    pass  # A local file lock must not turn a saved manuscript into a failed task.


def delete(store, key, revision, confirm=False):
    if confirm is not True:
        raise ValueError('请确认删除这条碎片和它生成的本地文字稿。')
    if type(revision) is not int or revision < 1:
        raise ValueError('请刷新记录后再删除。')
    with LOCK, store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute("SELECT * FROM records WHERE kind='growth_fragment' AND id=?", (key,)).fetchone()
        if not row:
            raise KeyError(key)
        item = store.unpack(row)
        if item['revision'] != revision:
            raise Conflict('碎片已更新，没有删除；请刷新核对。')
        job_row = con.execute("SELECT * FROM records WHERE kind='job' AND id=?", (item.get('transcription_job_id', ''),)).fetchone()
        if job_row and store.unpack(job_row).get('status') in ACTIVE:
            raise Conflict('正在转写，请等任务结束后再删除。')
        directory = owned_directory(store, key)
        root = store.root.resolve()
        backup = (root / 'backups/growth-deleted' / uid('deleted')).resolve()
        if not backup.is_relative_to(root):
            raise ValueError('删除备份路径不在本工作台范围内。')
        backup.mkdir(parents=True, exist_ok=False)
        (backup / 'fragment.json').write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding='utf-8')
        moved = False
        try:
            if directory.is_dir():
                directory.rename(backup / 'transcripts')
                moved = True
            origin = item.get('origin_answer_id')
            if origin:
                answer_row = con.execute("SELECT * FROM records WHERE kind='growth_answer' AND id=?", (origin,)).fetchone()
                if answer_row:
                    answer = store.unpack(answer_row)
                    if answer.get('fragment_id') == key and answer.get('status') == 'answered':
                        value = {k: v for k, v in answer.items()
                                 if k not in ('id', 'revision', 'created_at', 'updated_at')}
                        value.update(projection_status='pending', fragment_id='')
                        con.execute("UPDATE records SET data=?, revision=revision+1, updated_at=? "
                                    "WHERE kind='growth_answer' AND id=? AND revision=?",
                                    (json.dumps(value, ensure_ascii=False), now(), origin, answer['revision']))
            con.execute("DELETE FROM records WHERE kind='growth_fragment' AND id=? AND revision=?", (key, revision))
            # 2026-11-14：碎片镜像到知识池的那一条一起删，避免删完还在「随手记」里出现。
            con.execute("DELETE FROM records WHERE kind='knowledge' AND id=?", ('knowledge_' + key,))
            con.commit()
        except Exception:
            con.rollback()
            if moved:
                (backup / 'transcripts').rename(directory)
            raise
        return {'deleted': True, 'backup_path': str(backup)}
