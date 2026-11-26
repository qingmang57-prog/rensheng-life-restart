"""Wide (1920x1080) stem renderer. Usage: render_stems.py --config c.json --workdir <dir>

Outputs under <dir>/stems/: base video (bg + person full-frame + cards + shade +
audio), card_blank_XX.png (horizontal layout), 5 SRT skeletons, transparent PiP.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = r"C:\Windows\Fonts\msyhbd.ttc"
W, H = 1920, 1080


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--ffmpeg", default=str(Path(__file__).resolve().parents[1] / "ffmpeg" / "bin" / "ffmpeg.exe"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.workdir)
    stems = root / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    source = Path(cfg["source"])
    cards = cfg["cards"]
    duration = json.loads(Path(cfg["transcript"]).read_text(encoding="utf-8"))["duration"]

    # ---- cards (horizontal geometry) ----
    bg = Image.open(root / "background.png").convert("RGBA")
    for i, card in enumerate(cards):
        img = bg.copy()
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rectangle((0, 0, W, H), fill=(4, 14, 30, 165))
        d.rounded_rectangle((100, 60, 1820, 175), radius=24,
                            fill=(33, 68, 94, 212), outline=(108, 142, 168, 180), width=2)
        kind = card.get("kind", "quote")
        if kind == "diagram":
            for n in range(3):
                x = 120 + n * 520
                d.rounded_rectangle((x, 380, x + 440, 530), radius=20,
                                    fill=(34, 62, 83, 225), outline=(131, 161, 178, 175), width=3)
                if n < 2:
                    ax = x + 440
                    d.line((ax + 10, 455, ax + 66, 455), fill=(237, 205, 138), width=7)
                    d.polygon([(ax + 54, 439), (ax + 54, 471), (ax + 78, 455)], fill=(237, 205, 138))
        elif kind == "pillars":
            for n in range(3):
                x = 340 + n * 490
                d.rounded_rectangle((x, 400, x + 260, 640), radius=24,
                                    fill=(27, 59, 81, 225), outline=(224, 190, 123), width=4)
                d.text((x + 20, 356), f"0{n+1}", font=ImageFont.truetype(FONT, 34), fill=(235, 202, 135))
        else:
            y = 300 + len(card["lines"]) * 150 + 25
            d.rectangle((200, y, 380, y + 9), fill=(234, 198, 132))
        circle = Image.new("RGBA", (190, 190), (0, 0, 0, 0))
        ImageDraw.Draw(circle).ellipse((2, 2, 188, 188), fill=(9, 28, 49), outline=(237, 204, 138), width=6)
        layer.alpha_composite(circle, (1625, 365))
        img.alpha_composite(layer)
        img.convert("RGB").save(stems / f"card_blank_{i:02}.png", optimize=True)

    # ---- SRT skeletons (same as vertical) ----
    def ts(s: float) -> str:
        n = round(s * 1000)
        h, n = divmod(n, 3600000)
        m, n = divmod(n, 60000)
        s2, ms = divmod(n, 1000)
        return f"{h:02}:{m:02}:{s2:02},{ms:03}"

    def srt(name, entries):
        text = "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(entries, 1))
        (stems / name).write_text(text, encoding="utf-8-sig")

    corr = cfg.get("corrections", {})
    tr = json.loads(Path(cfg["transcript"]).read_text(encoding="utf-8"))
    srt("01_口播字幕.srt", [(s["start"], s["end"], corr.get(s["text"], s["text"])) for s in tr["segments"]])
    srt("02_章节标签.srt", [(c["start"], c["end"], c["label"]) for c in cfg["chapters"]])
    srt("03_卡片标题.srt", [(c["start"], c["end"], c["tag"]) for c in cards])
    srt("04_卡片主文案.srt", [(c["start"], c["end"], "\n".join(c["lines"])) for c in cards])
    srt("05_卡片英文.srt", [(c["start"], c["end"], c.get("english", "")) for c in cards])

    # ---- base video ----
    x, y, w, h = cfg["source_crop"]
    fargs = ["-i", str(source), "-loop", "1", "-framerate", "30", "-i", str(root / "background.png"),
             "-loop", "1", "-framerate", "30", "-i", str(root / "subtitle_shade.png")]
    filters = [
        f"[0:v]crop={w}:{h}:{x}:{y},scale=1920:1080:flags=lanczos,fps=30,setsar=1[person]",
        "[1:v]scale=1920:1080[bgimg]",
        "[bgimg][person]overlay=0:0:shortest=1[base]",
        "[base][2:v]overlay=0:0:shortest=1[scene0]",
    ]
    prev = "scene0"
    for i, card in enumerate(cards):
        start, end = card["start"], card["end"]
        dur = end - start
        fargs += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.3f}", "-i", str(stems / f"card_blank_{i:02}.png")]
        filters.append(f"[{i+3}:v]format=rgba,fade=t=in:st=0:d=0.32:alpha=1,"
                       f"fade=t=out:st={dur-0.32:.3f}:d=0.32:alpha=1,setpts=PTS+{start:.3f}/TB[c{i}]")
        filters.append(f"[{prev}][c{i}]overlay=0:0:eof_action=pass:shortest=0[scene{i+1}]")
        prev = f"scene{i+1}"
    fargs += ["-filter_complex", ";".join(filters), "-map", f"[{prev}]", "-map", "0:a:0",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
              "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
              "-t", f"{duration:.3f}", str(stems / "00_画面底片.mp4")]
    subprocess.run([args.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", *fargs], check=True)

    # ---- PiP (190px circle target; mov is 290px -> scale 190/1920=0.099 in draft) ----
    px, py, pw, ph = cfg["portrait_crop"]
    mask = root / "pip_mask.png"
    if not mask.is_file():
        m = Image.new("L", (290, 290), 0)
        ImageDraw.Draw(m).ellipse((0, 0, 290, 290), fill=255)
        m.save(mask)
    intervals = "+".join(f"between(t,{c['start']:.3f},{c['end']:.3f})" for c in cards)
    filters = [
        f"[0:v]crop={pw}:{ph}:{px}:{py},scale=290:290:flags=lanczos,fps=30,format=rgb24[piprgb]",
        "[1:v]format=gray[mask]",
        "[piprgb][mask]alphamerge,format=argb[pip]",
        f"color=c=black@0.0:s=290x290:r=30:d={duration:.3f},format=argb[blank]",
        f"[blank][pip]overlay=0:0:format=auto:enable='{intervals}':shortest=1,format=argb[out]",
    ]
    subprocess.run([args.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
                    "-i", str(source), "-loop", "1", "-framerate", "30", "-i", str(mask),
                    "-filter_complex", ";".join(filters), "-map", "[out]", "-an",
                    "-c:v", "qtrle", "-pix_fmt", "argb", "-t", f"{duration:.3f}",
                    str(stems / "06_同步圆形动态小窗_透明.mov")], check=True)
    print("横版 stems 完成:", stems)


if __name__ == "__main__":
    main()
