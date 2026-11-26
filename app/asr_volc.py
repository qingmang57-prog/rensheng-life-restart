"""火山 SeedASR（大模型）直连适配器 —— 复用切片工坊同一套凭据。

协议要点照抄切片工坊 backend/app/services/volc_asr_service.py（2026-11-02 实测连通）：
  submit -> query 轮询 -> result.utterances[]
鉴权头：X-Api-Resource-Id / X-Api-Request-Id / X-Api-Sequence / X-Api-Key
        （旧版控制台双头时另加 X-Api-App-Key + X-Api-Access-Key）

🔴 凭据只从环境变量读，绝不写入本仓库任何文件：
     IP_VOLC_ASR_KEY      必填（由本人在服务器 .env 填写）
     IP_VOLC_RESOURCE_ID  可选，默认 volc.seedasr.auc
     IP_VOLC_APP_ID       可选，仅旧版双头鉴权需要

🔴 词级文本口径：火山 word 若自带 text 则原样使用；若只给时间对（工坊版解析即如此），
   退化为「整句一条」而不是按字符猜分——猜分会造成误剪，违反不编造原则。
   上云后须用真实素材实测确认火山 word 是否带 text。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
import secret_store

_SUBMIT_URL = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit'
_QUERY_URL = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/query'
_DEFAULT_RESOURCE_ID = 'volc.seedasr.auc'
_POLL_MAX = 20
_POLL_INTERVAL = 3.0
_HTTP_TIMEOUT = 180.0
_MAX_AUDIO = 8 * 1024 * 1024
# 上传前必须声明真实容器格式：本包音轨统一抽成 16k 单声道 wav，
# 早期版本一律写死 mp3，和实际字节并不一致（2026-11-09 修正）。
_FORMATS = {'.wav': 'wav', '.mp3': 'mp3', '.ogg': 'ogg', '.pcm': 'pcm'}
_DONE = '20000000'
_IN_PROGRESS = ('20000001', '20000002', '')
_NO_SPEECH = '20000003'


class VolcAsrError(Exception):
    pass


class VolcEmptyAudio(VolcAsrError):
    pass


def key_present():
    """BYOK：只认用户自己填的 Key；包里不预置、不代填任何我方凭据。

    来源：进程环境变量 → 包外 .secrets/volc_asr.key → 本机配置；绝不打印内容。
    """
    key, _source = secret_store.read(('IP_VOLC_ASR_KEY',), 'volc_asr.key')
    return bool(key)


def configured():
    if os.getenv('IP_ASR_MODE', '').strip() not in ('volc', 'cloud'):
        return False
    if not key_present():
        return False
    # 云端账号权益：云端明确关掉云端 ASR 时不走这条路，由调用方回落到本地转写。
    try:
        import billing_service
        return billing_service.allow_cloud_asr()
    except Exception:  # noqa: BLE001
        return True


def setup_help():
    """给界面和报错用的指引：客户走官方云端网关，本机直连仅供运维回滚。"""
    return {'console': '',
            'env_file': '包外 .secrets\\volc_asr.key（仅运维回滚模式 IP_KEY_SOURCE=local 使用）',
            'fields': ['IP_ASR_MODE=cloud',
                       'IP_VOLC_ASR_KEY=（仅运维回滚模式需要，客户无需配置）'],
            'service': '云端转写（官方云端网关统一提供）',
            'endpoint': '',
            'limits': 'wav/mp3/ogg；单文件≤512MB、时长≤5小时（本包适配器上限 8MB，60 秒口播远低于此）'}


def _headers(req_id):
    key, _source = secret_store.read(('IP_VOLC_ASR_KEY',), 'volc_asr.key')
    headers = {
        'X-Api-Resource-Id': os.getenv('IP_VOLC_RESOURCE_ID', '').strip() or _DEFAULT_RESOURCE_ID,
        'X-Api-Request-Id': req_id,
        'X-Api-Sequence': '-1',
        'Content-Type': 'application/json',
    }
    app_id = os.getenv('IP_VOLC_APP_ID', '').strip()
    if app_id:
        headers['X-Api-App-Key'] = app_id
        headers['X-Api-Access-Key'] = key
    else:
        headers['X-Api-Key'] = key
    return headers


def _audio_format(audio_path):
    fmt = _FORMATS.get(Path(audio_path).suffix.lower(), '')
    if not fmt:
        raise VolcAsrError('云端转写只认 wav / mp3 / ogg / pcm 音频（收到 %s）。'
                           '请先转成 mp3，或用「一句话」把要点记下来。'
                           % (Path(audio_path).suffix or '无扩展名'))
    return fmt


def _build_payload(b64_audio, fmt):
    return {
        'user': {'uid': 'ip_workbench'},
        'audio': {'data': b64_audio, 'format': fmt},
        'request': {
            'model_name': 'bigmodel',
            'show_utterances': True,
            'enable_punc': True,
            'enable_itn': True,
        },
    }


def _post(url, headers, payload):
    """返回 (headers, data)。HTTPError 也返回其 headers，由调用方按 X-Api-Status-Code 判定。"""
    req = urllib.request.Request(
        url, json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as res:
            raw = res.read(8 * 1024 * 1024)
            return res.headers, _decode(raw)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(8 * 1024 * 1024)
        except Exception:
            raw = b''
        return exc.headers, _decode(raw)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VolcAsrError('火山 ASR 网络异常：%s' % str(exc)[:160]) from None


def _decode(raw):
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode('utf-8'))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _ms(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0
    return int(round(n)) if n > 0 and n == n and n != float('inf') else 0


def _inner(data):
    if not isinstance(data, dict):
        return {}
    result = data.get('result')
    if isinstance(result, dict):
        return result
    wrapped = data.get('data')
    if isinstance(wrapped, dict) and isinstance(wrapped.get('result'), dict):
        return wrapped['result']
    return data


def _words_of(utterance):
    """词级文本：优先用火山自带 text；只有时间对时退化为整句一条，绝不猜分。"""
    text = str(utterance.get('text') or '').strip()
    if not text:
        return []
    raw = utterance.get('words')
    out = []
    if isinstance(raw, list) and raw:
        for w in raw:
            if not isinstance(w, dict):
                continue
            word_text = str(w.get('text') or '').strip()
            start, end = _ms(w.get('start_time')), _ms(w.get('end_time'))
            if word_text and end > start:
                out.append({'text': word_text, 'start_ms': start, 'end_ms': end})
        if out:
            return out
    start, end = _ms(utterance.get('start_time')), _ms(utterance.get('end_time'))
    return [{'text': text, 'start_ms': start, 'end_ms': end}] if end > start else []


def _query(req_id, headers):
    last = ''
    for _ in range(_POLL_MAX):
        time.sleep(_POLL_INTERVAL)
        try:
            response_headers, data = _post(_QUERY_URL, headers, {})
        except VolcAsrError as exc:
            last = str(exc)[:80]
            continue
        status = response_headers.get('X-Api-Status-Code', '')
        last = status
        if status == _DONE:
            return data
        if status in _IN_PROGRESS:
            continue
        if status == _NO_SPEECH:
            raise VolcEmptyAudio('音频中没有检测到有效人声。')
        message = response_headers.get('X-Api-Message', '')
        raise VolcAsrError('火山 query 状态异常 status=%s msg=%s' % (status, message))
    raise VolcAsrError('火山 query 轮询超时（%d 次 × %ss，最后状态=%s）'
                       % (_POLL_MAX, _POLL_INTERVAL, last))


def transcribe(audio_path):
    """转写为个人IP工作台契约：{text, words:[{text,start_ms,end_ms}], cues, method}。"""
    if not key_present():
        raise VolcAsrError('云端转写暂不可用：请改用本地转写，或联系客服处理。')
    raw = Path(audio_path).read_bytes()
    if not raw:
        raise VolcEmptyAudio('音频文件为空。')
    if len(raw) > _MAX_AUDIO:
        raise VolcAsrError('音频超过 8MB 上限（%d 字节），请先分段。' % len(raw))

    fmt = _audio_format(audio_path)
    req_id = str(uuid.uuid4())
    headers = _headers(req_id)
    started = time.monotonic()
    submit_headers, _ = _post(_SUBMIT_URL, headers, _build_payload(base64.b64encode(raw).decode('ascii'), fmt))
    submit_status = submit_headers.get('X-Api-Status-Code', '')
    if submit_status and submit_status not in _IN_PROGRESS and submit_status != _DONE:
        raise VolcAsrError('火山 submit 被拒 status=%s msg=%s'
                           % (submit_status, submit_headers.get('X-Api-Message', '')))

    data = _query(req_id, headers)
    inner = _inner(data)
    utterances = inner.get('utterances')
    words, cues, texts = [], [], []
    if isinstance(utterances, list):
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            text = str(utterance.get('text') or '').strip()
            start, end = _ms(utterance.get('start_time')), _ms(utterance.get('end_time'))
            if text and end > start:
                cues.append({'text': text, 'start_ms': start, 'end_ms': end})
                texts.append(text)
            words.extend(_words_of(utterance))

    if not words:
        fallback = str(inner.get('text') or '').strip()
        if fallback:
            words = [{'text': fallback, 'start_ms': 0, 'end_ms': 1}]
            texts = [fallback]
    if not words:
        raise VolcEmptyAudio('火山未返回可用转写结果。')

    word_text_from_volc = any(str(w.get('text') or '').strip() for u in (utterances or [])
                              if isinstance(u, dict) for w in (u.get('words') or [])
                              if isinstance(w, dict))
    elapsed = round(time.monotonic() - started, 1)
    audio_ms = _ms((inner.get('audio_info') or {}).get('duration'))
    if not audio_ms and words:
        # 火山未回传 audio_info 时用词级时间轴兜底，保证计费日志有可核对的时长。
        audio_ms = max(w['end_ms'] for w in words)
    audio_seconds = round(audio_ms / 1000.0, 1) if audio_ms else 0.0
    # 计费依据是音频时长：把火山回传的时长和本次耗时写进日志，方便对账。
    print('[云端转写] 火山 SeedASR 成功：音频 %ss，调用耗时 %ss，返回 %d 字'
          % (audio_seconds or '?', elapsed, len(''.join(texts))), flush=True)
    return {'text': ''.join(texts), 'words': words, 'cues': cues,
            'method': 'volc_seedasr',
            'precision': 'word' if word_text_from_volc else 'utterance',
            'audio_seconds': audio_seconds, 'cloud_elapsed_s': elapsed}
