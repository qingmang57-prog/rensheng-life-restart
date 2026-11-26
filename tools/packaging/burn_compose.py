# -*- coding: utf-8 -*-
"""burn_compose.py —— 最小烧录合成脚本（B1 交付物④雏形 / 一期核心交付物）

把 render_stems 产出的 stems/00_画面底片.mp4 与 6 轨 SRT 按 koubo-packaging-wide
layout-wide.md 横版版式坐标烧录合成，并按 config.bgm 混入 BGM（音量钳制 ≤0.2，人声优先）。
纯标准库 + ffmpeg 子进程；不 import 工作台任何代码，不改工作台任何文件。

用法：python burn_compose.py --config config.json --workdir <dir> --ffmpeg <ffmpeg.exe>
输出：<workdir>/packaged_output.mp4（1920×1080 h264 + aac）
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

W, H = 1920, 1080

# layout-wide.md 实测参数：元素 -> (字号, RGB, 对齐, MarginL, MarginV)
# ASS 对齐：7=左上 / 9=右上（顶部元素一律左上=7）；2=底部居中
STYLES = {
    '01_口播字幕.srt': ('CaptionZH', 59, (250, 251, 251), 2, 0, 95),
    '02_章节标签.srt': ('Chapter',   30, (214, 224, 234), 7, 75, 70),
    '03_卡片标题.srt': ('CardTag',   48, (242, 211, 151), 7, 137, 82),
    '04_卡片主文案.srt': ('CardLines', 84, (250, 251, 251), 7, 200, 300),
    '05_卡片英文.srt': ('CardEn',    31, (202, 214, 219), 7, 200, 560),
    '06_英文字幕.srt': ('CaptionEN', 28, (217, 220, 225), 2, 0, 51),
}
# 大字 84px 用于单行≤10字金句（本片全部≤10字）；小字 72px 行备用
SMALL_SIZE = 72


def parse_srt(path):
    raw = Path(path).read_text(encoding='utf-8-sig')
    cues = []
    for block in raw.replace('\r\n', '\n').split('\n\n'):
        lines = [l for l in block.split('\n') if l.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r'(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)', lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start = ((h1 * 60 + m1) * 60 + s1) * 1000 + ms1
        end = ((h2 * 60 + m2) * 60 + s2) * 1000 + ms2
        cues.append({'start': start, 'end': end, 'text': '\n'.join(lines[2:])})
    return cues


def ass_time(ms):
    cs = max(0, round(ms / 10))
    return f'{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}'


def bgr(rgb):
    r, g, b = rgb
    return f'&H00{b:02X}{g:02X}{r:02X}'


def clean(text):
    return str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝').replace('\r', '').replace('\n', r'\N')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--ffmpeg', required=True)
    args = ap.parse_args()

    root = Path(args.workdir)
    stems = root / 'stems'
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8'))

    # ---- 组装 ASS（6 风格，PlayRes 1920×1080）----
    header = ('[Script Info]\nScriptType: v4.00+\n'
              f'PlayResX: {W}\nPlayResY: {H}\nWrapStyle: 0\n'
              '[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
              'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, '
              'Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n')
    style_lines = []
    for srt_name, (style, size, rgb, align, ml, mv) in STYLES.items():
        style_lines.append(
            f'Style: {style},Microsoft YaHei,{size},{bgr(rgb)},&H00FFFFFF,&H00191919,&H90000000,'
            f'-1,0,0,0,100,100,0,0,1,2,1,{align},{ml},{ml},{mv},1')
    events = []
    for srt_name, (style, size, rgb, align, ml, mv) in STYLES.items():
        path = stems / srt_name
        if not path.is_file():
            print('跳过缺失轨道:', srt_name)
            continue
        for cue in parse_srt(path):
            # 金句大字行≤10字用 84px，>10 字行换 72px 小字版（layout-wide 换算规则）
            eff_size = size
            if style == 'CardLines' and any(len(l) > 10 for l in cue['text'].split('\n')):
                eff_size = SMALL_SIZE
            # Layer: 小数字在上层（卡片元素盖章节，字幕在最下层）
            layer = {'Chapter': 6, 'CaptionZH': 8, 'CaptionEN': 9, 'CardTag': 3,
                     'CardLines': 2, 'CardEn': 1}.get(style, 5)
            events.append(f'Dialogue: {layer},{ass_time(cue["start"])},{ass_time(cue["end"])},'
                          f'{style},,0,0,0,,{clean(cue["text"])}')
    ass_text = header + '\n'.join(style_lines) + '\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n' + '\n'.join(events) + '\n'
    ass_path = root / 'burn_styles.ass'
    ass_path.write_text(ass_text, encoding='utf-8-sig')

    # ---- ffmpeg 合成：底片 + ASS + BGM ----
    volume = max(0.0, min(0.2, float(cfg.get('bgm', {}).get('volume', 0.2))))
    bgm_path = root / cfg['bgm']['file'] if cfg.get('bgm', {}).get('file') else None
    escaped = str(ass_path.resolve()).replace('\\', '/').replace(':', r'\:').replace("'", r"\'").replace(',', '\\,').replace('[', '\\[').replace(']', '\\]')
    output = root / 'packaged_output.mp4'
    if bgm_path and bgm_path.is_file():
        fc = (f'[1:a]volume={volume:.3f}[bgm];'
              '[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]')
        cmd = [args.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y',
               '-i', str(stems / '00_画面底片.mp4'), '-i', str(bgm_path),
               '-filter_complex', fc, '-vf', f"ass=filename='{escaped}'",
               '-map', '0:v:0', '-map', '[aout]',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-r', '30', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output)]
        print(f'BGM: {bgm_path.name} @ {volume:.2f}（人声全量，BGM 低音量混入）')
    else:
        print('BGM 未配置或缺失，出无配乐成片（不模拟）。')
        cmd = [args.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y',
               '-i', str(stems / '00_画面底片.mp4'),
               '-vf', f"ass=filename='{escaped}'",
               '-map', '0:v:0', '-map', '0:a:0',
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-r', '30', '-pix_fmt', 'yuv420p',
               '-c:a', 'copy', '-movflags', '+faststart', str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError('烧录失败：\n' + result.stderr[-2000:])
    print('成片已输出:', output, '| SRT 轨道:', len(events), '条事件')


if __name__ == '__main__':
    main()
