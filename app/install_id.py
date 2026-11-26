# -*- coding: utf-8 -*-
"""本机安装号（2026-11-16，t83 稳定化）：同一台电脑永远同一个号。

- 首次生成 32 位随机十六进制；之后**多位置互相备份**，任一份还在就复用：
  1) 系统目录 %LOCALAPPDATA%\\IPWorkbench\\install_id（跨数据目录/跨包升级稳定）
  2) %APPDATA%\\IPWorkbench\\install_id（LOCALAPPDATA 不可用时）
  3) 包内 app/.runtime/install_id
  4) 传入的数据目录 install_id（兼容旧布局）
- 取所有现存副本里**最早的一份**（mtime 最老）当权威，再补写回其它可写位置；
  只有四处都不存在时才生成新号——避免「删一个文件/换数据目录就变新设备」。
- 纯随机串：不读硬件、不读用户名、不读内容；只在注册/激活请求里上报一次，
  不进日志、不进导出、不进备份清单。
- 运维可用环境变量 IP_INSTALL_ID 强制指定（仅排障用）。
"""
import os
import secrets
import time

FILENAME = 'install_id'
_CACHE = {}
VALID = set('0123456789abcdef')


def _valid(value):
    return len(value) == 32 and all(ch in VALID for ch in value.lower())


def _read(path):
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            value = (fp.read() or '').strip().lower()
    except OSError:
        return ''
    return value if _valid(value) else ''


def _write(path, value):
    # 2026-11-25 t207 修复：不用 tempfile.mkstemp——它在不可写目录（沙箱/企业策略）下
    # 不是抛 OSError 而是内部重试到死（TMP_MAX），调用方拿不到异常就永久挂起。
    # 改为自拼临时名 + open('x')：不可写立即 PermissionError（OSError 子类）→ 调用方 continue。
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, '.install_id-%d-%s' % (os.getpid(), secrets.token_hex(4)))
    try:
        with open(temp, 'x', encoding='utf-8') as fp:
            fp.write(value)
        os.replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _system_dirs():
    dirs = []
    for key in ('LOCALAPPDATA', 'APPDATA'):
        base = os.getenv(key)
        if base:
            dirs.append(os.path.join(base, 'IPWorkbench'))
    return dirs


def _candidate_paths(data_dir):
    paths = []
    for folder in _system_dirs():
        paths.append(os.path.join(folder, FILENAME))
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, '.runtime', FILENAME))       # 包内
    base = str(data_dir or '').strip()
    if base:
        paths.append(os.path.join(base, FILENAME))                # 旧布局/数据目录
        if os.path.basename(os.path.normpath(base)) != '.runtime':
            paths.append(os.path.join(base, '.runtime', FILENAME))
    return list(dict.fromkeys(paths))


def install_id(data_dir):
    """取（必要时生成）本机安装号；所有位置都不可写时返回内存值，不阻断注册。"""
    base = str(data_dir or '').strip()
    override = (os.getenv('IP_INSTALL_ID') or '').strip().lower()
    if _valid(override):
        return override
    paths = _candidate_paths(base)
    key = '|'.join(paths)
    cached = _CACHE.get(key)
    if cached:
        return cached
    found = []
    for path in paths:
        value = _read(path)
        if not value:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = time.time()
        found.append((mtime, path, value))
    if found:
        # 最早的那份最可信（历史最久，最不可能来自某次临时目录）
        found.sort(key=lambda x: (x[0], x[1]))
        value = found[0][2]
        for _mtime, path, existing in found:
            if existing != value:
                try:
                    _write(path, value)
                except OSError:
                    pass
        for path in paths:
            if _read(path) != value:
                try:
                    _write(path, value)
                except OSError:
                    pass
    else:
        value = secrets.token_hex(16)
        for path in paths:
            try:
                _write(path, value)
            except OSError:
                continue
    _CACHE[key] = value
    return value
