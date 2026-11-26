"""Pick BGM from a local library by mood, loop it to video duration.

Usage: python pick_bgm.py --library <dir> --duration 218 --bpm 100 --brightness 520 --workdir <dir>

Analyzes every mp3 (duration / BPM via envelope autocorrelation / brightness via
spectral centroid), ranks by distance to the mood targets, crossfade-loops the
winner to --duration and writes <workdir>/stems/bgm.mp3. Prints the ranking so
the agent can swap choices (e.g. user dislikes the pick).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np

# 工具解析优先级：包内 tools/ffmpeg/bin > IP_FFMPEG/IP_FFPROBE 环境变量 > 系统 PATH。
# 换机器/换目录时优先用随包的 ffmpeg，缺件时仍可用环境变量兜底。
PACKAGE_BIN = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / "bin"


def _binary(name: str) -> str:
    candidates = [PACKAGE_BIN / (name + ".exe")]
    env = os.getenv("IP_" + name.upper())
    if env:
        candidates.append(Path(env))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or name


FF = _binary("ffmpeg")
FP = _binary("ffprobe")


def duration(p: Path) -> float:
    r = subprocess.run([FP, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def features(p: Path):
    r = subprocess.run([FF, "-v", "error", "-i", str(p), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                       capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    if len(x) < 80000:
        return None
    x = x[:80000]
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fr = np.fft.rfftfreq(len(x), 1 / 8000)
    cent = float((sp * fr).sum() / sp.sum())
    env = np.abs(x[:64000]).reshape(-1, 200).mean(axis=1)
    env = env - env.mean()
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    lo, hi = int(60 / 180 * 40), int(60 / 40 * 40)  # 40-180 BPM
    lag = int(np.argmax(ac[lo:hi])) + lo
    bpm = 60 / (lag * 200 / 8000)
    return bpm, cent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--bpm", type=float, required=True)
    ap.add_argument("--brightness", type=float, required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--ffmpeg", default=FF)
    args = ap.parse_args()

    rows = []
    for p in sorted(Path(args.library).glob("*.mp3")):
        d = duration(p)
        f = features(p)
        if not f or d < 60:
            continue
        bpm, cent = f
        score = abs(bpm - args.bpm) / 10 + abs(cent - args.brightness) / 60
        rows.append((round(score, 2), p.name, round(d), round(bpm, 1), round(cent)))
    rows.sort()
    print("排名 score 文件 时长s BPM 亮度Hz")
    for r in rows[:6]:
        print(r)
    if not rows:
        raise SystemExit("曲库无可用曲目")

    best = Path(args.library) / rows[0][1]
    src_dur = float(rows[0][2])
    need = args.duration
    out = Path(args.workdir) / "stems" / "bgm.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    if src_dur >= need + 1:
        subprocess.run([args.ffmpeg, "-v", "error", "-y", "-i", str(best),
                        "-t", f"{need:.3f}", str(out)], check=True)
    else:
        second = min(need - src_dur + 4, src_dur)  # 交叉淡化重叠 4s
        fc = (f"[0:a]asplit=2[a][b];[a]atrim=0:{src_dur:.1f},asetpts=PTS-STARTPTS[a1];"
              f"[b]atrim=0:{second:.1f},asetpts=PTS-STARTPTS[b1];"
              f"[a1][b1]acrossfade=d=4:c1=tri:c2=tri[out]")
        subprocess.run([args.ffmpeg, "-v", "error", "-y", "-i", str(best),
                        "-filter_complex", fc, "-map", "[out]", "-t", f"{need:.3f}", str(out)], check=True)
    print("已写入", out, "←", best.name)


if __name__ == "__main__":
    main()
