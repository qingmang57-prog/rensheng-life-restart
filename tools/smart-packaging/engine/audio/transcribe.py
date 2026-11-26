import json, sys
from faster_whisper import WhisperModel
model = WhisperModel(sys.argv[3], device="cpu", compute_type="int8",
                     cpu_threads=int(sys.argv[4]), num_workers=1)
segments, info = model.transcribe(sys.argv[1], language="zh", word_timestamps=False)
out = [{"i": n, "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
       for n, s in enumerate(segments)]
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("segments:", len(out), "lang:", info.language)
