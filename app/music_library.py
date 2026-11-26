"""配乐目录：随包曲库 + 用户自选本地文件夹。

规则：
  - 默认用随包曲库 tools/bgm（按情绪自动选曲，授权见 tools/bgm/CREDITS.md）
  - 用户在设置里选过文件夹 → 优先用他的；目录无音频时退回随包曲库
  - 两处都没有 → 包装照常出片，只是不加配乐，并给一句人话提示
  - 自选目录里有多首时轮播，不连续用同一首
  - 只认常见音频后缀，不扫描子目录（避免把长音频、录音当成 BGM）
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.wma'}
DEFAULT_DIR = Path(__file__).resolve().parent.parent / 'tools' / 'eva' / 'engine' / 'audio' / 'music'

MOOD_WORDS = {
    'upbeat': ('轻快', '活力', 'upbeat', 'happy', 'energetic', 'vlog'),
    'calm': ('舒缓', '安静', 'calm', 'soft', 'chill', 'lofi'),
    'serious': ('严肃', '专业', 'serious', 'corporate', 'news'),
    'warm': ('温暖', '治愈', 'warm', 'acoustic', 'piano'),
    'dramatic': ('紧张', '悬念', 'dramatic', 'tension', 'suspense', 'epic'),
}


def _data_dir(data_root=None):
    if data_root:
        return Path(data_root)
    env = os.getenv('IP_DATA_DIR', '')
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / '.runtime'


def _path(data_root=None):
    return _data_dir(data_root) / 'packaging-music.json'


def _read(data_root=None):
    try:
        return json.loads(_path(data_root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _write(data_root=None, **kw):
    rec = _read(data_root)
    rec.update(kw)
    p = _path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding='utf-8')
    return rec


def folder(data_root=None):
    env = os.getenv('IP_PACKAGING_MUSIC_DIR', '')
    if env:
        return Path(env)
    saved = str(_read(data_root).get('folder') or '').strip()
    if saved:
        return Path(saved)
    return DEFAULT_DIR


def source(data_root=None):
    if os.getenv('IP_PACKAGING_MUSIC_DIR', ''):
        return '环境变量 IP_PACKAGING_MUSIC_DIR'
    if str(_read(data_root).get('folder') or '').strip():
        return '设置里选的文件夹'
    return '默认曲库位置'


def tracks(data_root=None):
    root = folder(data_root)
    if not root.is_dir():
        return []
    try:
        return sorted([p for p in root.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXT],
                      key=lambda p: p.name.lower())
    except OSError:
        return []


def status(data_root=None):
    root = folder(data_root)
    found = tracks(data_root)
    try:
        import bgm_mix
        bundled = bgm_mix.library_status()
    except Exception:
        bundled = {'usable': 0, 'hint': ''}
    if found and source(data_root) != '默认曲库位置':
        hint = '找到 %d 首自备配乐，包装时优先用它们（不会连续用同一首）。' % len(found)
    elif bundled.get('usable'):
        hint = '随包曲库有 %d 首已核验配乐，按情绪自动选曲；署名见 tools/bgm/CREDITS.md。' % bundled['usable']
    else:
        hint = ('没有可用配乐，本次不加配乐。'
                '可以在「设置 → 配乐文件夹」里选一个本地目录（支持 mp3/wav/m4a/aac/flac/ogg）。')
    return {
        'folder': str(root),
        'exists': root.is_dir(),
        'source': source(data_root),
        'count': len(found),
        'bundled': bundled.get('usable', 0),
        'tracks': [{'name': p.name, 'size': p.stat().st_size} for p in found[:200]],
        'hint': hint,
    }


def set_folder(path, data_root=None):
    text = str(path or '').strip().strip('"')
    if not text:
        _write(data_root, folder='')
        return {'ok': True, **status(data_root)}
    target = Path(text)
    if not target.is_dir():
        return {'ok': False, 'error': '这个文件夹不存在，请重新选择：' + text, **status(data_root)}
    _write(data_root, folder=str(target))
    return {'ok': True, **status(data_root)}


def _mood_of(plan):
    blob = ' '.join(str(x or '') for x in (
        (plan.get('selected') or {}).get('theme'),
        (plan.get('selected') or {}).get('category'),
        (plan.get('selected') or {}).get('name'),
        (plan.get('context') or {}).get('title'),
    )).lower()
    for mood, words in MOOD_WORDS.items():
        if any(w.lower() in blob for w in words):
            return mood
    return ''


def pick(plan=None, data_root=None):
    """按风格优先 + 轮播挑一首；没有可用曲目返回 None。"""
    found = tracks(data_root)
    if not found:
        return None
    rec = _read(data_root)
    last = str(rec.get('last') or '')
    mood = _mood_of(plan or {})
    pool = found
    if mood:
        liked = [p for p in found if any(w.lower() in p.name.lower() for w in MOOD_WORDS[mood])]
        if liked:
            pool = liked
    names = [p.name for p in pool]
    if last in names and len(names) > 1:
        chosen = pool[(names.index(last) + 1) % len(pool)]
    else:
        chosen = pool[0]
    _write(data_root, last=chosen.name)
    return chosen


def pick_folder_dialog(timeout=300):
    """Windows 原生长文件夹选择框；返回选择的路径，取消/失败返回 ''。"""
    if os.name != 'nt':
        return ''
    script = (
        'Add-Type -AssemblyName System.Windows.Forms | Out-Null; '
        '$d = New-Object System.Windows.Forms.FolderBrowserDialog; '
        "$d.Description = '选择配乐文件夹（里面放 mp3/wav/m4a 等音乐）'; "
        '$d.ShowNewFolderButton = $false; '
        "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
    )
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-STA', '-Command', script],
                           capture_output=True, encoding='utf-8', errors='replace',
                           timeout=timeout,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return (r.stdout or '').strip().splitlines()[-1].strip() if (r.stdout or '').strip() else ''
    except (OSError, subprocess.SubprocessError):
        return ''
