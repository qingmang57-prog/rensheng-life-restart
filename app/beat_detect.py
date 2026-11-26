"""节拍检测（纯标准库）：给 BGM 卡点用。

为什么不用源项目的 beat_engine：那个是 librosa + numpy 实现，目标包 tools/python
里没有 librosa，直接搬会在买家机器上 ImportError。这里改成：
  ffmpeg 解码成 8kHz 单声道 → 10ms 帧能量 → 起音包络 → 自相关求速度 → 相位对齐。

输出：{"beats": [...秒], "bpm": float, "confidence": float}
无节拍/静音/解码失败 → beats 为空，调用方退化为"不吸附节拍"，绝不阻断出片。
"""
from __future__ import annotations

import array
import math
import subprocess
import threading
from pathlib import Path

SAMPLE_RATE = 8000
HOP = 0.010                      # 10ms 一帧
FRAME = int(SAMPLE_RATE * HOP)   # 80 采样
MIN_BPM, MAX_BPM = 50.0, 190.0

_cache: dict = {}
_lock = threading.Lock()


def _decode(path: Path, ffmpeg: str, timeout: int = 120) -> array.array:
    kw = {"capture_output": True, "timeout": timeout}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"],
        **kw)
    if result.returncode or not result.stdout:
        return array.array("h")
    pcm = array.array("h")
    raw = result.stdout
    pcm.frombytes(raw[: len(raw) - (len(raw) % 2)])
    return pcm


def _envelope(pcm: array.array) -> list[float]:
    """每 10ms 的能量（平均绝对值）与起音（正向差分）。"""
    rms = []
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        chunk = pcm[start:start + FRAME]
        total = 0
        for value in chunk:
            total += value if value >= 0 else -value
        rms.append(total / FRAME)
    onset = [0.0]
    for i in range(1, len(rms)):
        diff = rms[i] - rms[i - 1]
        onset.append(diff if diff > 0 else 0.0)
    # 3 帧滑动平均，削掉单帧毛刺
    smooth = []
    for i in range(len(onset)):
        lo, hi = max(0, i - 1), min(len(onset), i + 2)
        smooth.append(sum(onset[lo:hi]) / (hi - lo))
    return smooth


def _tempo(env: list[float]) -> tuple[int, float]:
    """自相关找主周期（帧数）与相位置信度；再做倍频校正，避免把 100 BPM 测成 50。"""
    n = len(env)
    if n < 60:
        return 0, 0.0
    mean = sum(env) / n
    centered = [x - mean for x in env]
    lag_min = max(2, int(round(60.0 / MAX_BPM / HOP)))
    lag_max = min(n // 2, int(round(60.0 / MIN_BPM / HOP)))
    scores = {}
    best_lag, best_score = 0, 0.0
    for lag in range(lag_min, lag_max + 1):
        total = 0.0
        for i in range(0, n - lag):
            total += centered[i] * centered[i + lag]
        score = total / (n - lag)
        scores[lag] = score
        if score > best_score:
            best_score, best_lag = score, lag
    if best_lag <= 0:
        return 0, 0.0
    # 倍频校正：半拍/三分之一拍也强，说明真实速度是更快的那个
    changed = True
    while changed:
        changed = False
        for divisor in (2, 3):
            cand = int(round(best_lag / divisor))
            if cand >= lag_min and scores.get(cand, 0.0) >= 0.82 * best_score:
                best_lag, best_score = cand, scores[cand]
                changed = True
                break
    energy = sum(x * x for x in centered) / n
    confidence = best_score / energy if energy > 0 else 0.0
    return best_lag, round(confidence, 3)


def detect_beats(path, ffmpeg: str | None = None, timeout: int = 120) -> dict:
    """返回 {'beats': [...], 'bpm': float, 'confidence': float}。失败即空结果。"""
    if not ffmpeg:
        return {"beats": [], "bpm": 0.0, "confidence": 0.0}
    path = Path(path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return {"beats": [], "bpm": 0.0, "confidence": 0.0}
    with _lock:
        if key in _cache:
            return dict(_cache[key])
    try:
        pcm = _decode(path, ffmpeg, timeout)
        env = _envelope(pcm)
        lag, confidence = _tempo(env)
        if lag <= 0:
            out = {"beats": [], "bpm": 0.0, "confidence": 0.0}
        else:
            # 相位：各相位位置上起音之和最大的那个
            best_phase, best_sum = 0, -1.0
            for phase in range(lag):
                total = sum(env[phase::lag])
                if total > best_sum:
                    best_phase, best_sum = phase, total
            duration = len(env) * HOP
            beats = []
            t = best_phase * HOP
            while t < duration:
                beats.append(round(t, 3))
                t += lag * HOP
            out = {"beats": beats, "bpm": round(60.0 / (lag * HOP), 1),
                   "confidence": confidence}
    except Exception:
        out = {"beats": [], "bpm": 0.0, "confidence": 0.0}
    with _lock:
        _cache[key] = dict(out)
    return dict(out)
