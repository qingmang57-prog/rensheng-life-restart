"""Wide (1920x1080) background generator. See koubo-packaging-wide SKILL.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
W, H = 1920, 1080
TOP = np.array([9, 20, 36], dtype=np.float32)
BOT = np.array([18, 36, 56], dtype=np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    root = Path(args.workdir)
    root.mkdir(parents=True, exist_ok=True)

    ys = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    xs = np.linspace(0, 1, W, dtype=np.float32)[None, :, None]
    t = np.clip(ys * 0.7 + xs * 0.3, 0, 1)
    t = t * t * (3 - 2 * t)
    img = Image.fromarray((TOP * (1 - t) + BOT * t).astype(np.uint8), "RGB").convert("RGBA")

    poly = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(poly).polygon([(0, 720), (W, 420), (W, 1080), (0, 1080)], fill=(30, 66, 90, 60))
    img.alpha_composite(poly)

    d = ImageDraw.Draw(img)
    d.line((140, 1028, 1780, 1028), fill=(116, 150, 174), width=2)
    footer = cfg.get("footer", "")
    if footer:
        d.text((140, 1038), footer, font=ImageFont.truetype(FONT, 26), fill=(223, 228, 232))
    img.convert("RGB").save(root / "background.png", optimize=True)

    shade = np.zeros((H, W, 4), dtype=np.uint8)
    for y in range(760, H):
        a = int(215 * (y - 760) / (H - 760))
        shade[y, :, :] = (7, 16, 28, a)
    Image.fromarray(shade, "RGBA").save(root / "subtitle_shade.png", optimize=True)
    print("横版 background.png / subtitle_shade.png 已生成")


if __name__ == "__main__":
    main()
