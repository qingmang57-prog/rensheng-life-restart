"""内容指纹缓存：同一段音频只转一次。

2026-11-16 用户要求「同一文件不重复转」。指纹只取内容（大小 + 头/中/尾三段），
不含修改时间，所以换名拷进来的同一段音频也能命中；音频一旦被剪辑或替换，
大小或采样段就会变，自动失效。说话人开关、模型文件变了由 cache_tag() 带入，
避免拿旧设置下的结果冒充新设置。
"""
import hashlib
import json
import os
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
CACHE_DIR=ROOT/'.runtime/asr-cache/results'
KEEP_MAX=int(os.getenv('IP_ASR_CACHE_MAX','60'))
SAMPLE=1<<20  # 1 MB


def fingerprint(audio_path,tag=''):
    """文件内容指纹；读失败返回空串，调用方据此跳过缓存，不影响转写。"""
    path=Path(audio_path)
    try:
        size=path.stat().st_size
        digest=hashlib.sha256()
        digest.update(('%d|%s'%(size,tag)).encode('utf-8'))
        with path.open('rb') as handle:
            digest.update(handle.read(SAMPLE))
            if size>SAMPLE*2:
                handle.seek(size//2-SAMPLE//2)
                digest.update(handle.read(SAMPLE))
            if size>SAMPLE:
                handle.seek(size-SAMPLE)
                digest.update(handle.read(SAMPLE))
        return digest.hexdigest()[:32]
    except OSError:
        return ''


def _entry_path(key):
    return CACHE_DIR/(key+'.json')


def get(audio_path,tag=''):
    """命中返回缓存的结果副本（带 cache_hit 标记与原始时间），否则 None。"""
    key=fingerprint(audio_path,tag)
    if not key:return None
    path=_entry_path(key)
    try:
        entry=json.loads(path.read_text(encoding='utf-8'))
        result=entry.get('result')
        if not isinstance(result,dict):return None
        out=dict(result)
        out['cache_hit']=True
        out['cache_key']=key
        out['cache_created_at']=entry.get('created_at')
        out['cache_source']=entry.get('source')
        return out
    except (OSError,ValueError):
        return None


def put(audio_path,tag,result):
    """写入缓存；失败只当没缓存，绝不打断转写。返回缓存键或空串。"""
    key=fingerprint(audio_path,tag)
    if not key or not isinstance(result,dict):return ''
    payload={k:v for k,v in result.items() if not k.startswith('cache_')}
    entry={'key':key,'source':Path(audio_path).name,'tag':tag,
           'created_at':time.strftime('%Y-%m-%d %H:%M:%S'),'result':payload}
    try:
        CACHE_DIR.mkdir(parents=True,exist_ok=True)
        tmp=_entry_path(key).with_suffix('.json.tmp')
        tmp.write_text(json.dumps(entry,ensure_ascii=False),encoding='utf-8')
        os.replace(tmp,_entry_path(key))
        _prune()
        return key
    except OSError:
        return ''


def _prune():
    try:
        files=sorted(CACHE_DIR.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    except OSError:
        return
    for old in files[max(0,KEEP_MAX):]:
        try:old.unlink()
        except OSError:pass


def stats():
    try:
        files=list(CACHE_DIR.glob('*.json'))
    except OSError:
        return {'entries':0,'bytes':0,'dir':str(CACHE_DIR)}
    return {'entries':len(files),'bytes':sum(p.stat().st_size for p in files),'dir':str(CACHE_DIR)}
