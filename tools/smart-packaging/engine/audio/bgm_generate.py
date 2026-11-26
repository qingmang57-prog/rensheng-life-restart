# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""bgm_generate.py — 程序化配乐生成（P0.6：免授权 BGM，synth.py mbar 和弦进行）
用法：python bgm_generate.py <秒数> <输出.wav> [--intensity 0.7]
验收：响度 -23±2 LUFS 目标由 mix.py 精调；此处先出可用底床。"""
from pathlib import Path
import argparse, importlib.util, json
import numpy as np

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('duration', type=float)
    ap.add_argument('out')
    ap.add_argument('--intensity', type=float, default=.7)
    a = ap.parse_args()
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('dw_synth', here / 'synth.py')
    A = importlib.util.module_from_spec(spec); spec.loader.exec_module(A)
    A.init(a.duration)
    A.music = np.zeros((A.N, 2))                       # mbar 写入目标（上游全局约定）
    t_end = a.duration
    bar = 0
    tb = 0.0
    while tb < t_end - 0.1:
        A.mbar(tb, bar, a.intensity, t_end)
        tb += 4 * A.B; bar += 1
    x = A.music
    peak = np.abs(x).max()
    if peak > 0: x = x / peak * .8
    A.write_wav(a.out, x)
    print(json.dumps({"out": a.out, "bars": bar, "sec": round(a.duration, 1), "sr": A.SR}))

if __name__ == '__main__':
    main()
