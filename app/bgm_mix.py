"""成片配乐：按情绪选曲 + 节拍卡点 + 人声闪避（ducking）+ 随包曲库。

来源：切片工坊 `wrap_audio.py`（作者本人两个产品的搬运），按目标系统重写：
- 曲库落 `tools/bgm/`，台账 `library.json` / `CREDITS.md`；未核验或未启用的曲子不参与选曲。
- 节拍检测改为纯标准库（见 beat_detect.py），不依赖 librosa。
- 响度不再写死“人声 -16 LUFS”的假设：先实测本条口播的集成响度，
  再把 BGM 铺底定在“人声 - BGM_DB_BELOW_VOICE”，避免把人声压过头或把配乐铺太响。
- 闪避用 ffmpeg sidechaincompress：人声在说话时 BGM 自动退让，停顿处回抬。

对外接口：
  library_status() / choose(src, data_root=None) / mix(silent, voice, out, picked, duration, ...)
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

import beat_detect
import media

BGM_DB_BELOW_VOICE = 15.0      # 人声段 BGM 比人声低 15 dB（源项目也是 15 起步）
BGM_STATIC_RANGE = (-40.0, 6.0)
DUCK = {'threshold': 0.05, 'ratio': 4.0, 'attack': 20.0, 'release': 400.0}

# 情绪目录：关键词命中越多越优先；都没命中走 default。
MOODS = [
    ('01', '促销/招商', '01-promo', ('促销', '招商', '优惠', '限时', '带货', '下单', '福利', '直播', 'action', 'promo')),
    ('02', '日常/轻快', '02-daily', ('日常', '分享', '记录', '生活', 'vlog', '轻松', '轻快', 'daily')),
    ('03', '温情/成长', '03-warm', ('温情', '成长', '故事', '经历', '坚持', '治愈', '人生', '情感', 'warm')),
    ('04', '搞笑/反差', '04-funny', ('搞笑', '反转', '反差', '沙雕', '吐槽', '段子', 'funny')),
    ('05', '悬念/揭黑', '05-suspense', ('悬念', '内幕', '真相', '揭秘', '骗局', '负债', '坑', '警示', 'suspense')),
    ('06', '商业/高级', '06-premium', ('商业', '高端', '品牌', '创业', '老板', '认知', '商业思维', 'premium')),
    ('07', '安静/铺垫', '07-calm', ('安静', '舒缓', '口播', '干货', '知识', '讲解', 'calm')),
]
DEFAULT_MOOD = '02'

_meta_lock = threading.Lock()
_meta_cache: dict = {}
_lufs_cache: dict = {}


def library_root() -> Path:
    """随包曲库。可用 IP_BGM_LIBRARY 指向自备目录（目录里同样要有 library.json）。"""
    env = os.getenv('IP_BGM_LIBRARY')
    if env and Path(env).is_dir():
        return Path(env)
    return Path(__file__).resolve().parent.parent / 'tools' / 'bgm'


def _load_library(root: Path | None = None) -> dict:
    root = root or library_root()
    manifest = root / 'library.json'
    if not manifest.is_file():
        return {'tracks': [], 'sfx': [], 'root': str(root)}
    key = (str(manifest), manifest.stat().st_mtime, manifest.stat().st_size)
    with _meta_lock:
        if key in _meta_cache:
            return _meta_cache[key]
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {'tracks': [], 'sfx': []}
    data['root'] = str(root)
    for item in data.get('tracks', []) + data.get('sfx', []):
        item['path'] = str(root / item['file'])
    with _meta_lock:
        _meta_cache[key] = data
    return data


def _usable(item: dict) -> bool:
    """来源核验过 + 明确启用 + 文件真的在。"""
    return bool(item.get('verified')) and bool(item.get('enabled', True)) and Path(item.get('path', '')).is_file()


def library_status(root: Path | None = None) -> dict:
    data = _load_library(root)
    tracks = data.get('tracks', [])
    usable = [t for t in tracks if _usable(t)]
    pending = [t for t in tracks if not _usable(t)]
    counts = {}
    for track in usable:
        counts[track.get('mood', '')] = counts.get(track.get('mood', ''), 0) + 1
    return {'root': data['root'], 'total': len(tracks), 'usable': len(usable),
            'pending': len(pending), 'by_mood': counts,
            'bpm_note': '曲目 BPM 由随包节拍检测自动测出',
            'hint': ('随包曲库可用曲目 %d 首；署名见 tools/bgm/CREDITS.md' % len(usable))
                    if usable else '随包曲库没有已核验并启用的曲目，本次不加配乐（见 tools/bgm/CREDITS.md）'}


def choose_mood(src: dict | None) -> str:
    text = ' '.join(str(v) for v in (src or {}).values() if isinstance(v, (str, int, float))).lower()
    best, best_hits = DEFAULT_MOOD, 0
    for mood, _label, _folder, words in MOODS:
        hits = sum(1 for word in words if word.lower() in text)
        if hits > best_hits:
            best, best_hits = mood, hits
    return best


def choose(src: dict | None = None, seed: str | None = None, root: Path | None = None) -> dict | None:
    """按风格/情绪挑一首；同一条内容稳定选同一首（sha1 决定），不会每次随机跳。"""
    data = _load_library(root)
    usable = [t for t in data.get('tracks', []) if _usable(t)]
    if not usable:
        return None
    mood = choose_mood(src)
    pool = [t for t in usable if t.get('mood') == mood] or usable
    pool.sort(key=lambda t: t['file'])
    digest = hashlib.sha1((seed or json.dumps(src or {}, ensure_ascii=False, sort_keys=True)).encode('utf-8')).hexdigest()
    picked = dict(pool[int(digest[:8], 16) % len(pool)])
    picked['mood_label'] = next((label for code, label, _f, _w in MOODS if code == picked.get('mood')), '')
    picked['credit'] = 'BGM：%s - %s (incompetech.com)，%s' % (
        picked.get('title', Path(picked['file']).stem), picked.get('author', 'Kevin MacLeod'), picked.get('license', 'CC BY 4.0'))
    return picked


def _measure_lufs(path: Path) -> float | None:
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    with _meta_lock:
        if key in _lufs_cache:
            return _lufs_cache[key]
    try:
        value = float(media.loudness_measure(str(path))['lufs'])
    except Exception:
        value = None
    with _meta_lock:
        _lufs_cache[key] = value
    return value


def plan_gain(voice_lufs: float, track: Path) -> dict:
    """把 BGM 铺底定在人声下方 BGM_DB_BELOW_VOICE；返回可核对的数字。"""
    track_lufs = _measure_lufs(track)
    if track_lufs is None:
        track_lufs = -16.0
    gain = (voice_lufs - BGM_DB_BELOW_VOICE) - track_lufs
    gain = max(BGM_STATIC_RANGE[0], min(BGM_STATIC_RANGE[1], gain))
    return {'voice_lufs': round(voice_lufs, 1), 'track_lufs': round(track_lufs, 1),
            'bed_lufs_target': round(voice_lufs - BGM_DB_BELOW_VOICE, 1), 'gain_db': round(gain, 2)}


def _fmt(value: float) -> str:
    return ('%.2f' % value).rstrip('0').rstrip('.')


def _snap(points, beats, tolerance=0.28, duration=None):
    """把内容切换点吸到最近的节拍；太远就保持原位置。"""
    if not beats:
        return []
    picked = []
    for point in points or []:
        if duration and point > duration:
            continue
        nearest = min(beats, key=lambda b: abs(b - point))
        picked.append(round(nearest if abs(nearest - point) <= tolerance else point, 3))
    return sorted(set(picked))


def _bed_chain(duration: float, gain_db: float, duck: bool, with_voice_sc: bool) -> tuple[str, str]:
    """BGM 链（可只导 BGM 用于核验闪避量）。返回 (filter_complex, 输出标签)。"""
    parts = []
    if duck and with_voice_sc:
        parts.append('[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                     'apad,atrim=0:%s,asetpts=PTS-STARTPTS[sc]' % _fmt(duration))
    parts.append('[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 'atrim=0:%s,asetpts=PTS-STARTPTS,volume=%sdB[bed]' % (_fmt(duration), _fmt(gain_db)))
    if duck and with_voice_sc:
        parts.append('[bed][sc]sidechaincompress=threshold=%s:ratio=%s:attack=%s:release=%s[outb]'
                     % (DUCK['threshold'], DUCK['ratio'], DUCK['attack'], DUCK['release']))
    else:
        parts.append('[bed]anull[outb]')
    return ';'.join(parts), '[outb]'


def render_bed(voice_source, track, output, duration: float, duck: bool = True) -> dict:
    """只导 BGM 铺底（用同一条人声做侧链），给"闪避到底降了多少 dB"留实测证据。"""
    ffmpeg = media.binary('ffmpeg')
    if not ffmpeg:
        raise ValueError('未找到 ffmpeg，无法核验配乐。')
    duration = max(0.5, float(duration))
    voice_lufs = _measure_lufs(Path(voice_source)) or -16.0
    gain = plan_gain(voice_lufs, Path(track))
    filters, label = _bed_chain(duration, gain['gain_db'], duck, True)
    media.run([ffmpeg, '-y', '-hide_banner', '-i', str(voice_source), '-i', str(voice_source),
               '-stream_loop', '-1', '-i', str(track), '-filter_complex', filters,
               '-map', label, '-t', _fmt(duration), '-ac', '2', '-ar', '48000',
               '-c:a', 'pcm_s16le', str(output)], 1800)
    return gain


def mix(silent_video, voice_source, output, picked: dict, duration: float,
        switch_points=None, opts: dict | None = None) -> dict:
    """把静音画面 + 清理人声 + 随包配乐合成一条成片。

    opts: {'duck': True/False, 'sfx': 'auto'/'off'} —— 默认开闪避、不加点音效。
    """
    opts = opts or {}
    ffmpeg = media.binary('ffmpeg')
    if not ffmpeg:
        raise ValueError('未找到 ffmpeg，无法混音。')
    track = Path(picked['path'])
    if not track.is_file():
        raise ValueError('配乐文件不存在：%s' % track)
    duration = max(0.5, float(duration))

    voice_lufs = _measure_lufs(Path(voice_source))
    if voice_lufs is None:
        voice_lufs = -16.0
    gain = plan_gain(voice_lufs, track)

    beats = beat_detect.detect_beats(track, ffmpeg)
    fade_in, fade_out = 0.8, 1.2
    bed_fade = 'afade=t=in:st=0:d=%s,afade=t=out:st=%s:d=%s' % (
        _fmt(fade_in), _fmt(max(0.0, duration - fade_out)), _fmt(fade_out))

    # 人声：补齐到成片时长后一分为二（一路进混音，一路做侧链）
    voice_chain = ('[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                   'apad,atrim=0:%s,asetpts=PTS-STARTPTS,asplit=2[voice][sc]' % _fmt(duration))
    bed_chain = ('[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                 'atrim=0:%s,asetpts=PTS-STARTPTS,volume=%sdB,%s[bed]'
                 % (_fmt(duration), _fmt(gain['gain_db']), bed_fade))
    parts = [voice_chain, bed_chain]
    if opts.get('duck', True):
        parts.append('[bed][sc]sidechaincompress=threshold=%s:ratio=%s:attack=%s:release=%s[duckbed]'
                     % (DUCK['threshold'], DUCK['ratio'], DUCK['attack'], DUCK['release']))
        mixed = '[duckbed]'
    else:
        mixed = '[bed]'
    parts.append('[voice]%samix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]' % mixed)

    sfx_points = []
    if opts.get('sfx', 'off') != 'off':
        sfx_points = _snap(switch_points, beats['beats'], duration=duration)

    inputs = ['-i', str(silent_video), '-i', str(voice_source), '-stream_loop', '-1', '-i', str(track)]
    filters = ';'.join(parts)
    # 音效按节拍叠在成片音轨上（只有一个点音效文件时复用）
    sfx_used = []
    if sfx_points:
        data = _load_library()
        sfx = next((s for s in data.get('sfx', []) if _usable(s) and s['file'].endswith('switch.ogg')), None)
        if sfx:
            inputs += ['-i', str(sfx['path'])]
            delays = []
            for point in sfx_points:
                ms = max(0, int(round(point * 1000)))
                delays.append('adelay=%d|%d' % (ms, ms))
            parts.append('[3:a]%s,volume=-9dB[sx]' % ','.join(delays))
            parts.append('[aout][sx]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[afinal]')
            filters = ';'.join(parts)
            audio_map = '[afinal]'
            sfx_used = sfx_points
        else:
            audio_map = '[aout]'
    else:
        audio_map = '[aout]'

    args = [ffmpeg, '-y', '-hide_banner'] + inputs + [
        '-filter_complex', filters, '-map', '0:v:0', '-map', audio_map,
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', _fmt(duration),
        '-movflags', '+faststart', str(output)]
    media.run(args, 3600)
    return {'track': str(track), 'title': picked.get('title', track.stem),
            'author': picked.get('author', ''), 'license': picked.get('license', ''),
            'source': picked.get('source', ''), 'credit': picked.get('credit', ''),
            'mood': picked.get('mood', ''), 'mood_label': picked.get('mood_label', ''),
            'bpm': beats['bpm'], 'beat_confidence': beats['confidence'],
            'beats': len(beats['beats']), 'sfx_points': sfx_used,
            'duck': bool(opts.get('duck', True)),
            'duck_settings': dict(DUCK) if opts.get('duck', True) else None,
            'loudness': gain}
