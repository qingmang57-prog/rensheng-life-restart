# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy"]
# ///
"""analyze_beats.py — 节拍表自动化（P0.5：BGM → beats+hits，喂 beats.js）
librosa 不在托管环境时退化到 numpy/scipy 谱通量检拍（本实现即退化版，零新增依赖）。
产物 JSON：{"fps": 0, "beats": [[t, isDown], ...], "hits": [[t, strength], ...]}
用法：python analyze_beats.py <音频> <输出.json> [--bpm 120]
验收：对 20 首 BGM 抽听卡点（施工 1.5 回归项）。"""
from pathlib import Path
import argparse, json, subprocess
import numpy as np
from scipy.io import wavfile
from scipy import signal

SR = 22050
HOP = 512

def decode(p):
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(p),
        '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-'])
    return np.frombuffer(raw, np.float32).astype(np.float64)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('audio'); ap.add_argument('out')
    ap.add_argument('--bpm', type=float, default=None)
    a = ap.parse_args()
    y = decode(a.audio)
    n = len(y)
    # STFT 幅值
    f, t, Z = signal.stft(y, SR, nperseg=1024, noverlap=1024 - HOP)
    mag = np.abs(Z)
    # 谱通量（onset strength）
    flux = np.maximum(0, np.diff(mag, axis=1)).sum(axis=0)
    flux = flux / (flux.max() + 1e-9)
    times = t[1:]
    # 峰值拣选（最小间隔 0.25s）
    peaks, props = signal.find_peaks(flux, height=.18, distance=int(.25 * SR / HOP))
    # 估计 BPM：峰值间隔直方图
    if a.bpm is None and len(peaks) > 4:
        d = np.diff(times[peaks])
        hist, edges = np.histogram(d, bins=60, range=(.25, 1.0))
        best = edges[np.argmax(hist)]
        bpm = 60 / best if best > 0 else 120
        bpm = round(bpm * 2) / 2
    else:
        bpm = a.bpm or 120
    beat = 60 / bpm
    # 吸附到拍网格：取最强 onset 附近 8s 窗估相位
    w = peaks[(times[peaks] > 5) & (times[peaks] < 20)]
    anchors = times[w] if len(w) else times[peaks][:8]
    phases = [(anchors % beat)]
    phase = float(np.median(phases[0])) if len(phases[0]) else 0
    beats = []
    tb = phase
    dur = n / SR
    while tb < dur:
        i = np.argmin(np.abs(times - tb))
        is_down = (round((tb - phase) / beat) % 4 == 0)
        beats.append([round(float(times[i]), 3), 1 if is_down else 0])
        tb += beat
    # hits = 强 onset（前 1/16 能量峰值）
    strong = [times[p] for p in peaks if flux[p] > .55]
    hits = [[round(float(x), 3), round(float(flux[np.argmin(np.abs(times - x))]), 2)] for x in strong[:64]]
    json.dump({"profile": "dreamweaver.beats/1", "sr": SR, "bpm": bpm,
               "beat": round(beat, 4), "count": len(beats), "beats": beats, "hits": hits},
              open(a.out, 'w', encoding='utf-8'), ensure_ascii=False)
    print(json.dumps({"bpm": bpm, "beats": len(beats), "hits": len(hits), "out": a.out}))

if __name__ == '__main__':
    main()
