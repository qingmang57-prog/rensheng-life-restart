# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy", "soundfile", "pyloudnorm"]
# ///
"""织梦包装引擎 · 混音器（响度标准与闪避结构移植自 385712 open-beta mix.py，MIT）
改造点：音效生成器接我们自己的 synth.py；事件表来自我们场景的 runtime-events.json。"""
from pathlib import Path
import argparse, json, subprocess, sys
import numpy as np
import soundfile as sf
import pyloudnorm as pyln
from scipy.ndimage import uniform_filter1d
sys.dont_write_bytecode = True

SR = 48000
# —— 响度目标（照抄验证值）——
VOICE_LUFS, MUSIC_LUFS, NATIVE_LUFS, PUNCT_LUFS = -16, -23, -25, -28
BUS_VOICE_LUFS, BUS_NOVOICE_LUFS = -17, -21
DUCK_DB, LIMIT = -11, 0.65

def decode(p):
    return np.frombuffer(subprocess.check_output(
        ['ffmpeg', '-v', 'error', '-i', str(p), '-ar', str(SR), '-ac', '2',
         '-f', 'f32le', '-']), np.float32).reshape(-1, 2).astype(np.float64)

def norm(y, db):
    v = pyln.Meter(SR).integrated_loudness(y)
    return y * 10 ** ((db - v) / 20) if np.isfinite(v) else y

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--instance', type=Path, required=True)
    ap.add_argument('--silent-video', type=Path, required=True)
    a = ap.parse_args()
    s = json.loads((a.instance / 'settings.json').read_text(encoding='utf-8'))
    n = round(s['duration'] * SR)
    voice = np.zeros((n, 2)); music = np.zeros((n, 2))
    native = np.zeros((n, 2))
    revents = a.instance / 'runtime-events.json'
    if revents.exists():
        import importlib.util
        from scipy.signal import resample_poly
        spec = importlib.util.spec_from_file_location('dw_synth', Path(__file__).resolve().parent / 'synth.py')
        A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
        A.init(s['duration'])
        events = json.loads(revents.read_text(encoding='utf-8'))
        dry = A.render_sfx(events.get('sfx', []))          # 44.1k N x 2
        if dry.shape[0]:
            native = resample_poly(A.reverb(dry, .9, .1), 160, 147, axis=0)[:n]
            native = norm(native, NATIVE_LUFS)
    for role in ['voice', 'music']:
        asset = s['media'].get(role, {'mode': 'none'})
        if asset.get('mode') == 'file':
            track = decode(a.instance / asset['file'])[:n]
            if len(track) != n:
                raise ValueError(role + ' shorter than output clock')
            if role == 'voice':
                voice = norm(track, VOICE_LUFS)
            else:
                music = norm(track, MUSIC_LUFS)
    if np.any(voice):  # 人声能量门限闪避（照抄结构）
        mono = np.sqrt(np.mean(voice * voice, axis=1))
        energy = np.sqrt(np.maximum(0, uniform_filter1d(mono * mono, size=round(.04 * SR))))
        gate = (energy > .007).astype(float)
        gate = uniform_filter1d(gate, size=round(.25 * SR))
        music *= 10 ** (-DUCK_DB * np.clip(gate * 2, 0, 1) / 20)[:, None]
    full = voice + music + native
    fade = min(round(.6 * SR), n)
    full[-fade:] *= np.linspace(1, 0, fade)[:, None] ** 1.3
    full = norm(full, BUS_VOICE_LUFS if np.any(voice) else BUS_NOVOICE_LUFS)
    audio = a.instance / 'audio-output'
    audio.mkdir(exist_ok=True)
    for stale in ('prelimit.wav', 'mix.wav'):          # 末端派生产物，重跑自清理
        p = audio / stale
        if p.exists(): p.unlink()
    sf.write(audio / 'native-sfx.wav', native, SR, subtype='FLOAT')
    raw = audio / 'prelimit.wav'
    sf.write(raw, full, SR, subtype='FLOAT')
    # alimiter：limit=0.65 attack=5 release=100（照抄）
    subprocess.run(['ffmpeg', '-v', 'error', '-n', '-i', str(raw),
                    '-af', f'aresample=192000,alimiter=limit={LIMIT}:level=false:latency=true:attack=5:release=100,aresample=48000',
                    '-c:a', 'pcm_s24le', str(audio / 'mix.wav')], check=True)
    final = a.silent_video.with_name(a.silent_video.stem + '-with-audio.mp4')
    subprocess.run(['ffmpeg', '-v', 'error', '-n', '-i', str(a.silent_video),
                    '-i', str(audio / 'mix.wav'), '-map', '0:v:0', '-map', '1:a:0',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k', '-ar', '48000',
                    '-movflags', '+faststart', str(final)], check=True)
    raw.unlink()
    (audio / 'mix-report.json').write_text(json.dumps({
        'nativePeak': round(float(np.abs(native).max()), 4) if native.shape[0] else 0,
        'voice': s['media'].get('voice', {'mode': 'none'}).get('mode'),
        'music': s['media'].get('music', {'mode': 'none'}).get('mode'),
        'busTarget': BUS_VOICE_LUFS if np.any(voice) else BUS_NOVOICE_LUFS,
        'video': final.name}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'video': str(final)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
