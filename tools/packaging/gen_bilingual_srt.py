"""Generate bilingual subtitle SRTs.

Usage: python gen_bilingual_srt.py --config config.json --translations translations.json --workdir <dir>

translations.json: JSON array of English strings, one per transcript segment
(the agent translates the CORRECTED meaning; length need not match Chinese).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def ts(s: float) -> str:
    n = round(s * 1000)
    h, n = divmod(n, 3600000)
    m, n = divmod(n, 60000)
    sec, ms = divmod(n, 1000)
    return f"{h:02}:{m:02}:{sec:02},{ms:03}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--translations", required=True)
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    tr = json.loads(Path(cfg["transcript"]).read_text(encoding="utf-8"))
    en = json.loads(Path(args.translations).read_text(encoding="utf-8"))
    segs = tr["segments"]
    assert len(segs) == len(en), f"翻译条数 {len(en)} != 转写段数 {len(segs)}"
    corr = cfg.get("corrections", {})

    stems = Path(args.workdir) / "stems"
    stems.mkdir(parents=True, exist_ok=True)
    cn = "\n".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{corr.get(s['text'], s['text'])}\n"
                   for i, s in enumerate(segs, 1))
    en_txt = "\n".join(f"{i}\n{ts(s['start'])} --> {ts(s['end'])}\n{t}\n"
                       for i, (s, t) in enumerate(zip(segs, en), 1))
    (stems / "01_口播字幕.srt").write_text(cn, encoding="utf-8-sig")
    (stems / "06_英文字幕.srt").write_text(en_txt, encoding="utf-8-sig")
    print("双语 SRT 完成:", len(segs), "条")


if __name__ == "__main__":
    main()
