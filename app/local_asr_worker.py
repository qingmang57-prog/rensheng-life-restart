"""Offline ASR worker: reuse workshop code/models in a supervised child process.

Two modes:
- --serve: resident worker. Loads the model once, then answers JSON lines on
  stdin/stdout until it is told to shut down (or the parent kills it).
- one-shot (--audio/--output): kept for diagnostics; exits after one file.

All human-readable logs go to stderr so stdout only carries protocol JSON.
"""
import argparse
import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
import types

PROTO = sys.stdout


def _emit(payload):
    PROTO.write(json.dumps(payload, ensure_ascii=False) + '\n')
    PROTO.flush()


def _resolve_device(requested, torch):
    """auto: use CUDA when the bundled torch really has it, else CPU.

    A missing driver/GPU must degrade to CPU, never crash the transcription.
    """
    if requested == 'auto':
        return ('cuda' if torch.cuda.is_available() else 'cpu'), 'auto'
    if requested == 'cuda' and not torch.cuda.is_available():
        return 'cpu', 'cuda_unavailable'
    return requested, 'explicit'


def _gpu_name(torch, device):
    if device != 'cuda':
        return ''
    try:
        return torch.cuda.get_device_name(0)
    except Exception:
        return ''


def _build_transcript(module, model, raw, audio, output, ffmpeg, device,
                      diarize, model_seconds):
    """Run one file and return the transcript payload.

    The model object is reused; only the captured token buffer is reset.
    """
    package = sys.modules['app']
    package.config.FFMPEG_PATH = ffmpeg or package.config.FFMPEG_PATH
    package.config.OUTPUT_DIR = output.parent / 'asr-scratch'
    raw.clear()
    started = time.monotonic()
    rows = module.transcribe_media(str(audio), hotwords='')
    cues = [{'id': 'cue_%d' % (i + 1), 'start_ms': round(r['start'] * 1000),
             'end_ms': round(r['end'] * 1000), 'text': r['text']}
            for i, r in enumerate(rows)]
    words = []
    # Use model tokens aligned one-to-one with timestamps; never estimate by
    # character count.
    segments = module._collect_segments(raw)
    if not segments or any(not ts or len(text.split()) != len(ts)
                           for text, ts, _ in segments):
        raise ValueError('原始模型时间戳无法逐词对齐，停止处理，未使用估算时间轴。')
    for text, ts, spk in segments:
        tokens = text.split()
        if not ts or len(tokens) != len(ts):
            continue
        for token, (start, end) in zip(tokens, ts):
            if start >= 0 and end > start:
                words.append({'id': 'word_%d' % (len(words) + 1), 'text': token,
                              'start_ms': round(start), 'end_ms': round(end),
                              'speaker': spk})
    if not words:
        raise ValueError('模型未提供可用的真实词级时间戳。')
    result = {
        'text': '\n'.join(c['text'] for c in cues), 'cues': cues, 'words': words,
        'method': 'workshop_funasr_local',
        'device': device,
        'diarize': bool(diarize),
        'model_seconds': round(model_seconds, 2),
        'elapsed_seconds': round(time.monotonic() - started, 2),
        'note': '复用分段工坊本地模型和全文断句；机器转写待核对，可能漏掉语气词。',
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temp.replace(output)
    return result


def _prepare_model(workshop, device, diarize, ffmpeg, keep_model):
    # Do not import workshop config.py: no settings, keys, caches or original
    # output writes.
    package = types.ModuleType('app')
    package.__path__ = []
    package.config = types.SimpleNamespace(
        LOCAL_ASR_MODEL_DIR=workshop / 'models/hub',
        LOCAL_ASR_DEVICE=device,
        LOCAL_ASR_KEEP_MODEL=keep_model,
        LOCAL_ASR_HOTWORDS_FILE=workshop / 'backend/hotwords.txt',
        FFMPEG_PATH=ffmpeg or '',
        OUTPUT_DIR=workshop / 'backend/out',
        ASR_PUNCT_ENABLED=True)
    package.asr_punct = types.SimpleNamespace()
    sys.modules['app'] = package

    print('ASR worker: importing torch', flush=True)
    import torch
    resolved, why = _resolve_device(device, torch)
    package.config.LOCAL_ASR_DEVICE = resolved
    print('ASR worker: torch runtime loaded (device=%s, reason=%s)'
          % (resolved, why), flush=True)
    _emit({'type': 'stage', 'text': '语音运行库已加载（%s），正在加载解析组件'
                                    % resolved.upper()})
    if resolved == 'cpu':
        torch.set_num_threads(4)
        torch.set_num_interop_threads(2)

    spec = importlib.util.spec_from_file_location(
        'workbench_workshop_asr', workshop / 'backend/app/local_asr.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print('ASR worker: workshop parser loaded', flush=True)
    _emit({'type': 'stage', 'text': '解析组件已加载，正在注册模型组件'})
    if not diarize:
        # The workshop module treats an existing spk model as "do diarization".
        # Point it at a name that cannot exist, so CAM++ is not loaded at all.
        module._SPK_MODEL = '__ip_diarization_disabled__'
        print('ASR worker: speaker diarization disabled', flush=True)

    # funasr 默认会把整包 400+ 模块全部预导入（含 torch.distributed、sklearn、
    # tensorboardX），慢盘上会卡几分钟。只注册本机实际要用的组件。
    print('ASR worker: components importing', flush=True)
    for name in ('funasr.frontends.wav_frontend', 'funasr.tokenizer.char_tokenizer',
                 'funasr.models.specaug.specaug', 'funasr.models.sanm.encoder',
                 'funasr.models.paraformer.cif_predictor',
                 'funasr.models.paraformer.decoder',
                 'funasr.models.bicif_paraformer.cif_predictor',
                 'funasr.models.fsmn_vad_streaming.encoder',
                 'funasr.models.seaco_paraformer.model',
                 'funasr.models.fsmn_vad_streaming.model',
                 'funasr.models.campplus.model', 'funasr.models.ct_transformer.model'):
        importlib.import_module(name)
    print('ASR worker: components registered', flush=True)
    print('ASR worker: model loading', flush=True)
    _emit({'type': 'stage', 'text': '正在加载语音模型（首次冷启动较慢）'})
    started = time.monotonic()
    model = module._load_model()
    model_seconds = time.monotonic() - started
    print('ASR worker: model loaded in %.2fs' % model_seconds, flush=True)
    return package, module, model, resolved, why, model_seconds


def main():
    import faulthandler
    faulthandler.enable()
    # 2026-11-03：卡死定位证据——从第一行起每 60 秒把线程栈写进运行日志。
    try:
        faulthandler.dump_traceback_later(60, repeat=True)
    except Exception:
        pass
    os.environ['IP_FUNASR_SKIP_EAGER'] = '1'

    parser = argparse.ArgumentParser()
    parser.add_argument('--workshop', required=True)
    parser.add_argument('--audio')
    parser.add_argument('--output')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--warmup', action='store_true')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--device', choices=['cpu', 'cuda', 'auto'], default='auto')
    parser.add_argument('--diarize', choices=['0', '1'], default='0')
    args = parser.parse_args()
    if not args.serve and not args.warmup and not (args.audio and args.output and args.ffmpeg):
        parser.error('transcription needs --audio --output --ffmpeg')

    # Keep all library prints off the protocol channel.
    sys.stdout = sys.stderr
    PROTO = sys.__stdout__
    globals()['PROTO'] = PROTO
    print('ASR worker: starting (serve=%s, device=%s, diarize=%s)'
          % (args.serve, args.device, args.diarize), flush=True)

    workshop = Path(args.workshop).resolve()
    out = Path(args.output).resolve() if args.output else None

    # The ASR subprocess has no network route, including implicit downloads.
    def offline(*a, **kw):
        raise RuntimeError('Local transcription worker is offline')

    socket.socket.connect = offline
    socket.create_connection = offline

    # torchaudio 依赖 VC++ OpenMP；便携包已随 sklearn 携带 vcomp140/msvcp140，
    # Windows 必须显式加入 DLL 搜索目录，干净机器不能只指望系统已装运行库。
    dll_handles = []
    if os.name == 'nt':
        runtime_dlls = Path(sys.prefix) / 'Lib/site-packages/sklearn/.libs'
        if runtime_dlls.is_dir():
            dll_handles.append(os.add_dll_directory(str(runtime_dlls)))

    logging.basicConfig(level=logging.INFO)
    _emit({'type': 'stage', 'text': '正在加载语音模型'})
    package, module, model, device, device_reason, model_seconds = _prepare_model(
        workshop, args.device, args.diarize == '1', args.ffmpeg,
        keep_model=args.serve)

    import torch
    info = {'type': 'ready', 'ok': True, 'device': device,
            'device_reason': device_reason, 'gpu': _gpu_name(torch, device),
            'torch': getattr(torch, '__version__', ''),
            'cuda': bool(torch.cuda.is_available()),
            'diarize': args.diarize == '1',
            'model_seconds': round(model_seconds, 2), 'pid': os.getpid()}

    if args.warmup:
        module.release_model()
        _emit({'ok': True, 'warmup_seconds': round(time.monotonic() - model_seconds, 2),
               'device': device})
        os._exit(0)

    if not args.serve:
        raw = []
        original = model.generate

        def capture(*a, **kw):
            result = original(*a, **kw)
            raw.extend(result)
            return result

        model.generate = capture
        print('ASR worker: transcribing', flush=True)
        result = _build_transcript(module, model, raw, args.audio, out, args.ffmpeg,
                                   device, args.diarize == '1', model_seconds)
        module.release_model()
        _emit({'ok': True, 'cues': len(result['cues']), 'words': len(result['words']),
               'seconds': result['elapsed_seconds']})
        os._exit(0)

    # Resident mode: wrap generate once and reset the buffer per request.
    raw = []
    original = model.generate

    def capture(*a, **kw):
        result = original(*a, **kw)
        raw.extend(result)
        return result

    model.generate = capture
    _emit(info)
    print('ASR worker: ready for requests', flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _emit({'type': 'error', 'message': 'worker: invalid JSON request'})
            continue
        cmd = str(request.get('cmd') or '')
        if cmd == 'shutdown':
            _emit({'type': 'done', 'ok': True, 'stopping': True})
            module.release_model()
            PROTO.flush()
            os._exit(0)
        if cmd == 'ping':
            _emit({'type': 'done', 'ok': True, 'pong': True, 'device': device,
                   'model_seconds': round(model_seconds, 2), 'pid': os.getpid()})
            continue
        if cmd == 'warmup':
            _emit({'type': 'done', 'ok': True, 'warmup_seconds': 0.0,
                   'device': device, 'model_seconds': round(model_seconds, 2)})
            continue
        if cmd != 'transcribe':
            _emit({'type': 'error', 'message': 'worker: unknown command %r' % cmd})
            continue

        audio = Path(str(request.get('audio') or '')).resolve()
        output = Path(str(request.get('output') or '')).resolve()
        if not audio.is_file() or not output:
            _emit({'type': 'error', 'message': 'worker: audio/output missing'})
            continue
        stop = threading.Event()

        def heartbeat():
            started = time.monotonic()
            while not stop.wait(5):
                _emit({'type': 'stage',
                       'text': '本地识别中（%s，已用 %d 秒）'
                               % (device.upper(), int(time.monotonic() - started))})

        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        try:
            result = _build_transcript(module, model, raw, audio, output,
                                       request.get('ffmpeg') or args.ffmpeg,
                                       device, args.diarize == '1', model_seconds)
            _emit({'type': 'done', 'ok': True, 'result_path': str(output),
                   'cues': len(result['cues']), 'words': len(result['words']),
                   'elapsed_seconds': result['elapsed_seconds'], 'device': device})
        except Exception as exc:
            logging.exception('ASR worker transcription failed')
            _emit({'type': 'error', 'message': str(exc)[:600],
                   'trace': traceback.format_exc()[-1500:]})
        finally:
            stop.set()


if __name__ == '__main__':
    main()
