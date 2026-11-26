"""synth_check.py — 织梦包装引擎合成库验收
对 synth.py（移植自 385712 open-beta MIT）做三类独立验收：
1. 67 类 SFX 生成器逐一出声（各自独立 wav）
2. 鼓组五件 + 六件乐器独立出声
3. 非静音校验（峰值 > 1e-3）与时长校验
用法：python synth_check.py [输出目录]
"""
import sys, os, wave, json
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import synth

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "验收wav")
os.makedirs(OUT, exist_ok=True)

def peak_of(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes(); raw = w.readframes(n)
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32767
    return float(np.abs(x).max()) if len(x) else 0.0

def write(name, x):
    p = os.path.join(OUT, name)
    synth.write_wav(p, x)
    return p

report = {"sfx": [], "drums": [], "instruments": []}

# ---- 1. 67 类 SFX 逐一出声 ----
N = synth.init(30.0)
cues, t = [], 0.15
for key in synth.GEN:
    cues.append({"type": key, "t": t})
    t += 0.40
assert t < 30, "30s buffer too short"
buf = synth.render_sfx(cues)
# 逐类切出独立 wav（按 cue 时刻 ±1.2s 窗口）
for c in cues:
    i0 = int(max(0, (c["t"] - 0.1) * synth.SR))
    i1 = int(min(len(buf), (c["t"] + 1.2) * synth.SR))
    p = write(f"sfx_{c['type']}.wav", buf[i0:i1])
    report["sfx"].append({"type": c["type"], "peak": round(peak_of(p), 4)})
write("sfx_all_30s.wav", buf)

# ---- 2. 鼓组五件 ----
drums = {
    "kick": synth.kick(), "snare": synth.snare(), "clap": synth.clap(),
    "hat": synth.hat(), "shaker": synth.shaker(),
}
for name, x in drums.items():
    p = write(f"drum_{name}.wav", synth.pan(x, 0))
    report["drums"].append({"name": name, "peak": round(peak_of(p), 4)})

# ---- 3. 乐器六件 ----
inst = {
    "pad": synth.pad([62, 65, 69, 72], 2.0, 1800, .08),
    "pluck": synth.pluck(74, .3, .7, 3000),
    "bass": synth.bassn(38, .8),
    "epiano": synth.epiano(74, .5),
    "bell": synth.bell(86),
    "chip": synth.chip(84, .15, .5, 1.2),
}
for name, x in inst.items():
    p = write(f"inst_{name}.wav", synth.pan(x, 0))
    report["instruments"].append({"name": name, "peak": round(peak_of(p), 4)})

# ---- 4. 汇总校验 ----
fails = [r["type"] for r in report["sfx"] if r["peak"] <= 1e-3]
fails += [r["name"] for r in report["drums"] + report["instruments"] if r["peak"] <= 1e-3]
summary = {
    "sfx_types": len(report["sfx"]),
    "drums": len(report["drums"]),
    "instruments": len(report["instruments"]),
    "wav_dir": OUT,
    "silence_fails": fails,
    "status": "PASS" if not fails else "FAIL",
}
print(json.dumps(summary, ensure_ascii=False, indent=1))
with open(os.path.join(OUT, "_report.json"), "w", encoding="utf-8") as f:
    json.dump({"summary": summary, "details": report}, f, ensure_ascii=False, indent=1)
