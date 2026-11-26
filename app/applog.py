"""最小滚动日志：只记错误与关键操作，不记稿件正文、密钥、Cookie、媒体内容。

写入 <数据目录>/logs/app.log（JSONL，单文件上限 1MB，保留 3 份）。
所有写入都会先脱敏：API key / Bearer / Cookie / 本地绝对路径。
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))
LOG_DIR = DATA / 'logs'
MAX_BYTES = 1000 * 1000
KEEP = 3
MAX_MESSAGE = 500
_LOCK = threading.Lock()

_SECRET_PATTERNS = (
    (re.compile(r'(?i)\b(authorization|proxy-authorization)\b\s*[:=]\s*\S+'), r'\1=<已脱敏>'),
    (re.compile(r'(?i)\b(cookie|set-cookie)\b\s*[:=]\s*.+'), r'\1=<已脱敏>'),
    (re.compile(r'(?i)\b(api[_-]?key|access[_-]?key|secret[_-]?key|auth[_-]?token|access[_-]?token|token)\b'
                r'\s*[:=]\s*["\']?[^\s"\',;&]{4,}'), r'\1=<已脱敏>'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}'), 'Bearer <已脱敏>'),
    (re.compile(r'(?i)\b(sk|ak|pk)-[A-Za-z0-9_-]{8,}'), '<已脱敏>'),
)


def scrub(text):
    """用于日志与诊断包：去掉密钥、Cookie、本机绝对路径。"""
    value = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        value = pattern.sub(repl, value)
    replacements = [(str(DATA), '<数据目录>'), (str(ROOT.parent), '<程序目录>'), (str(ROOT), '<程序目录>/app')]
    home = str(Path.home())
    if home and home != str(ROOT):
        replacements.append((home, '<用户目录>'))
    for raw, mask in replacements:
        if raw:
            value = re.sub(re.escape(raw) + r'[^\s"\'<>]*', mask, value)
    return value


def _rotate():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / 'app.log'
    if not log.is_file() or log.stat().st_size < MAX_BYTES:
        return log
    oldest = LOG_DIR / 'app.log.{0}'.format(KEEP - 1)
    if oldest.is_file():
        oldest.unlink()
    for index in range(KEEP - 2, 0, -1):
        source = LOG_DIR / 'app.log.{0}'.format(index)
        if source.is_file():
            source.replace(LOG_DIR / 'app.log.{0}'.format(index + 1))
    log.replace(LOG_DIR / 'app.log.1')
    return log


def write(level, event, message='', **fields):
    """写一条日志；任何异常都吞掉，绝不影响主流程。"""
    try:
        safe_message = scrub(message)[:MAX_MESSAGE]
        safe_fields = {}
        for key, value in fields.items():
            text = scrub(json.dumps(value, ensure_ascii=False, default=str))
            safe_fields[key] = text[:300]
        record = {'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'level': level if level in ('info', 'warning', 'error') else 'info',
                  'event': scrub(event)[:120], 'message': safe_message}
        if safe_fields:
            record['fields'] = safe_fields
        line = json.dumps(record, ensure_ascii=False, separators=(',', ':'))
        with _LOCK:
            log = _rotate()
            with log.open('a', encoding='utf-8') as handle:
                handle.write(line + '\n')
    except Exception:
        pass


def info(event, message='', **fields):
    write('info', event, message, **fields)


def error(event, message='', **fields):
    write('error', event, message, **fields)


def exception(event, exc, context=''):
    """只记异常类型、脱敏消息与最后 5 个栈帧，不记局部变量。"""
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)[-5:]
    where = ' <- '.join('{0}:{1}:{2}'.format(Path(f.filename).name, f.lineno, f.name) for f in frames)
    message = '{0}: {1}'.format(type(exc).__name__, exc)
    if context:
        message = context + ' | ' + message
    if where:
        message += ' | ' + where
    write('error', event, message)


def _read_lines(newest_first=True):
    files = []
    for index in range(KEEP - 1, -1, -1):
        path = LOG_DIR / ('app.log' if index == 0 else 'app.log.{0}'.format(index))
        if path.is_file():
            files.append(path)
    if newest_first:
        files.reverse()
    for path in files:
        try:
            with path.open('r', encoding='utf-8', errors='replace') as handle:
                for line in handle:
                    yield line.rstrip('\n')
        except OSError:
            continue


def recent_errors(limit=200):
    items = []
    for line in _read_lines(newest_first=True):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get('level') == 'error':
            items.append(record)
            if len(items) >= limit:
                break
    items.reverse()
    return items


def stats():
    files = []
    total = 0
    for path in sorted(LOG_DIR.glob('app.log*')) if LOG_DIR.is_dir() else []:
        size = path.stat().st_size
        total += size
        files.append({'name': path.name, 'bytes': size})
    return {'dir': 'logs', 'files': files, 'bytes': total,
            'errors': len(recent_errors(1000))}
