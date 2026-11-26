# -*- coding: utf-8 -*-
"""密钥读取：只按「进程环境变量 → 包外 secrets 文件 → 本机配置」取值，任何日志只记来源与是否为空。

2026-11-14 约定：凭据以文件交接，明文只存在包外 .secrets 目录或本机数据目录；
调用方不得把值写进日志、异常消息、报告或任何会随包分发的文件。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent
# 2026-11-16 修正：BASE 的基准是本文件的位置——本模块恒在 `<包根>/app/` 下，
# 所以 BASE = 包根（不是「往上第 N 层目录」）。包根之外要再往上一级找 .secrets 时，
# 必须判断是否已经到盘符根（包解在盘根下一层时，再往上就是那个盘的根），
# 不能写死 parents[1]：那种布局下 BASE.parents 只有 1 项，直接取 [1] 会抛 IndexError: 1，
# 进而让 /api/asr/status 这类只读接口 500。
# 进程环境变量快照：本模块在 app.py 读取配置文件之前被导入，
# 因此这里的键就是「真正的环境变量」，可与 .env.local 写入的值区分开。
_REAL_ENV = set(os.environ)


def _outside_secret_dirs(levels=2):
    """包外 .secrets 候选：从包根往上逐级取，遇到盘符根/文件系统根就停（不越界）。"""
    out = []
    cur = BASE
    for _ in range(levels):
        parent = cur.parent
        if parent == cur:      # 已经是盘符根，再往上还是自己
            break
        out.append(parent / '.secrets')
        cur = parent
    return out


def secrets_dir():
    """包外密钥目录：优先 IP_SECRETS_DIR，其次包根同级/.secrets，再次包根上一级/.secrets。

    跳过盘符根目录（例如某个盘的根），避免密钥目录被解析到盘符根下的 .secrets。
    包直接解在盘根下一层时，没有更上层的可写包外目录，读到的是「未配置」，
    写入由调用方按 OSError 降级（不再抛 IndexError）。
    """
    raw = (os.getenv('IP_SECRETS_DIR') or '').strip()
    if raw:
        return Path(raw)
    for cand in _outside_secret_dirs():
        if cand.is_dir() and len(cand.parts) > 2:
            return cand
    return _outside_secret_dirs()[0] if _outside_secret_dirs() else BASE / '.secrets'


def read(env_names, file_name):
    """返回 (值, 来源)：'env' / 'file' / 'config' / ''（未配置）。

    只接受单行、去掉首尾引号与空白；文件读不到或为空时继续走下一级。
    """
    names = [env_names] if isinstance(env_names, str) else list(env_names)
    for name in names:
        if name in _REAL_ENV:
            value = (os.environ.get(name) or '').strip()
            if value:
                return value, 'env'
    path = secrets_dir() / file_name
    try:
        if path.is_file():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                value = line.strip().strip('"\'')
                if value:
                    return value, 'file'
    except OSError:
        pass
    for name in names:
        value = (os.environ.get(name) or '').strip()
        if value:
            return value, 'config'
    return '', ''


def describe(label, value, source):
    """只输出「来源 + 是否为空」，绝不带值本身。"""
    return '%s：来源=%s，是否为空=%s' % (label, source or '未配置', '是' if not value else '否')


def write(file_name, value):
    """写包外 secrets 文件；目录不可写时抛 OSError，由调用方决定降级。

    可写判据：先往目标目录写一个探测文件再删掉（不用 os.access 预检，避免误判）。
    """
    directory = secrets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ('.write-probe-%s.tmp' % os.urandom(4).hex())
    probe.write_bytes(b'')
    probe.unlink()
    target = directory / file_name
    tmp = directory / (file_name + '.tmp')
    tmp.write_text(value.strip() + '\n', encoding='utf-8')
    os.replace(tmp, target)
    return target
