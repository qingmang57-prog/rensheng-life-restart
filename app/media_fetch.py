# -*- coding: utf-8 -*-
"""统一「取媒体」：B站 / 小红书视频 等平台的公开链接 → 本机音频文件 + 元数据。

分工（不重复实现下载器）：
- 下载器用随包的 yt-dlp（tools/yt-dlp/yt-dlp.exe，协议为公有领域），跑在子进程里；
- 转写仍走 providers.asr；热评走 hot_comments；图文笔记走 page_notes + content_capture。

纪律：
- 只收 http/https 且域名在 ALLOWED_HOSTS 里的链接：不接受 file:// 等本地协议，
  也不接受调用方传进来的任何命令行参数（防参数注入）。
- 失败必须说清「卡在哪一步、下一步怎么办」，不静默、不编造。
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
BINARY = Path(os.environ.get('IP_YTDLP') or (ROOT / 'tools' / 'yt-dlp' / 'yt-dlp.exe'))
VERSION = '1.0'
PROBE_TIMEOUT = 60
FETCH_TIMEOUT = 15 * 60
CREATE_NO_WINDOW = 0x08000000

ALLOWED_HOSTS = ('bilibili.com', 'b23.tv', 'xiaohongshu.com', 'xhslink.com')
# 引擎（单文件版）在系统临时目录不可写时的原话；认它，不靠自己预测。
_TEMP_DENIED = re.compile(r'could not create temporary directory|unable to create temporary directory', re.I)

# 取不到时的人话：按引擎输出里的关键词归类，不把英文堆栈直接甩给用户。
_FAILURES = (
    (('sign in to confirm', 'confirm you are not a bot', 'login required', 'cookies from your browser'),
     '对方要求登录后才能看这条（平台风控）。可以把视频保存到电脑后直接拖进输入框，或粘贴文字。',
     'login_required'),
    (('http error 412', 'precondition failed'),
     '被对方风控拦住了（412）。过几分钟再试一次，或把视频保存到电脑后拖进输入框。', 'risk_control'),
    (('http error 429', 'too many requests'),
     '请求太频繁，被对方暂时限制了。稍等一会儿再试。', 'rate_limited'),
    (('http error 404', 'video unavailable', 'private video', 'this video is private', 'does not exist',
      '已失效', '稿件不可见'),
     '这条内容已经打不开了（可能被删除或设为仅自己可见）。', 'gone'),
    (('could not create temporary directory', 'permission denied'),
     '取视频的组件启动不了（系统的临时目录不可写）。把视频保存到电脑后直接拖进输入框即可。',
     'temp_blocked'),
    (('unsupported url', 'is not a valid url'),
     '这个链接不是单条内容（可能是主页、合集，或这个平台还没支持的形式）。请贴具体那一条。', 'unsupported'),
    (('timed out', 'timeout', 'connection reset', 'unable to download'),
     '连接对方服务器失败或超时。检查网络后再试，或把视频保存到电脑后拖进输入框。', 'network'),
)


class MediaFetchError(RuntimeError):
    def __init__(self, message, code='media_fetch_failed'):
        super().__init__(message)
        self.code = code


def available():
    return BINARY.is_file()


def version():
    code, out = _run(['--version'], PROBE_TIMEOUT)
    return out.strip().splitlines()[-1] if code == 0 and out.strip() else ''


def guard(url):
    """只放行支持清单里的网页链接。不合规直接报错，不交给引擎。"""
    raw = str(url or '').strip()
    parts = urlsplit(raw)
    host = (parts.hostname or '').lower()
    if parts.scheme not in ('http', 'https') or not host:
        raise MediaFetchError('这看起来不是完整链接，请以 http:// 或 https:// 开头。', 'bad_url')
    if not any(host == d or host.endswith('.' + d) for d in ALLOWED_HOSTS):
        raise MediaFetchError('这个站点不在可取视频的清单里。', 'unsupported')
    return raw


def _data_tmp_env():
    """引擎自己报「建不了临时目录」时才用：把临时目录换到数据目录再跑一次。

    不做「提前探测」——实测探测能建目录、引擎照样起不来，预测不可靠；只认引擎自己的报错。
    数据目录也建不了就返回 None，由那次报错走 _explain 给客户说清。
    """
    base = Path(os.environ.get('IP_DATA_DIR') or (ROOT / 'app' / '.runtime'))
    try:
        target = base / 'tmp'
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    env = os.environ.copy()
    env['TEMP'] = env['TMP'] = str(target)
    return env


def _spawn(args, timeout, on_line, env):
    proc = subprocess.Popen(  # noqa: S603  参数全部由本模块拼出，不接收界面输入
        [str(BINARY)] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, env=env, encoding='utf-8', errors='replace',
        creationflags=CREATE_NO_WINDOW)
    lines = []

    def pump():
        for raw in proc.stdout:
            lines.append(raw)
            if on_line:
                on_line(raw)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise MediaFetchError('取视频超过 %d 分钟还没完成，已停止。可以稍后重试，或把视频保存到电脑后拖进输入框。'
                              % max(1, timeout // 60), 'timeout') from None
    reader.join(5)
    return proc.returncode, ''.join(lines)


def _run(args, timeout, on_line=None):
    if not available():
        raise MediaFetchError(
            '取视频的组件不在（tools/yt-dlp/yt-dlp.exe）。重新解压完整安装包即可恢复；'
            '也可以把视频保存到电脑后直接拖进输入框。', 'engine_missing')
    code, out = _spawn(args, timeout, on_line, os.environ.copy())
    if code != 0 and _TEMP_DENIED.search(out):
        # 单文件版引擎启动时要在系统临时目录里自解压；被拒就原样重跑一次，临时目录换到数据目录。
        env = _data_tmp_env()
        if env:
            code, out = _spawn(args, timeout, on_line, env)
    return code, out


def _explain(output):
    text = str(output or '').lower()
    for keys, message, code in _FAILURES:
        if any(key in text for key in keys):
            return message, code
    tail = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), '')
    return '取视频失败%s。可以重试一次，或把视频保存到电脑后拖进输入框。' % (
        ('（%s）' % tail[:160]) if tail else ''), 'media_fetch_failed'


def probe(url):
    """只读元数据（标题/作者/时长/互动数），不下载。"""
    target = guard(url)
    code, out = _run(['--no-warnings', '--no-colors', '--no-playlist', '--dump-single-json', target],
                     PROBE_TIMEOUT)
    if code != 0:
        message, reason = _explain(out)
        raise MediaFetchError(message, reason)
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise MediaFetchError('对方返回的内容读不出来（可能链接不是单条视频）。', 'bad_response') from None
    duration = info.get('duration')
    return {
        'id': str(info.get('id') or ''),
        'title': str(info.get('title') or '').strip(),
        'author': str(info.get('uploader') or info.get('creator') or '').strip(),
        'duration_ms': int(duration * 1000) if isinstance(duration, (int, float)) else None,
        'view_count': info.get('view_count'),
        'like_count': info.get('like_count'),
        'comment_count': info.get('comment_count'),
    }


def _info_of(stem):
    """取媒体时顺带落盘的元数据（--write-info-json）。没有或读不出就当没有，不影响已取到的音频。"""
    try:
        info = json.loads(Path(str(stem) + '.info.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    duration = info.get('duration')
    return {'title': str(info.get('title') or '').strip(),
            'author': str(info.get('uploader') or info.get('creator') or '').strip(),
            'duration_ms': int(duration * 1000) if isinstance(duration, (int, float)) else None,
            'stats': {'view': info.get('view_count'), 'like': info.get('like_count'),
                      'comment': info.get('comment_count')}}


def fetch_audio(url, stem, progress=None):
    """取这条内容的音频到本机。返回 (音频文件路径, 元数据)；stem 是不带扩展名的目标路径。"""
    target = guard(url)
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda _m: None)
    percent = re.compile(r'\[download\]\s+([\d.]+)%')

    def on_line(raw):
        found = percent.search(raw)
        if found:
            try:
                notify('下载音频… %d%%' % int(float(found.group(1))))
            except ValueError:
                pass

    notify('下载音频…')
    code, out = _run(['--no-warnings', '--no-colors', '--no-playlist', '--newline',
                      '--socket-timeout', '20', '--retries', '2', '--write-info-json',
                      '-f', 'bestaudio[ext=m4a]/bestaudio/best',
                      '-o', str(stem) + '.%(ext)s', target],
                     FETCH_TIMEOUT, on_line=on_line)
    if code != 0:
        message, reason = _explain(out)
        raise MediaFetchError(message, reason)
    files = sorted((p for p in stem.parent.glob(stem.name + '.*')
                    if p.is_file() and p.suffix != '.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise MediaFetchError('取视频结束了，但没有拿到音频文件。可以重试一次，或把视频保存到电脑后拖进输入框。')
    return files[0], _info_of(stem)
