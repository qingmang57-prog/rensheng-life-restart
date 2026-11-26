# -*- coding: utf-8 -*-
r"""数据目录解析与换目录承接（t204）。

优先级与 app.py 一致：
  进程环境 IP_DATA_DIR > 包根 .env.local > app/.env.local

在这之后增加一个本机稳定承接 pointer（仅 Windows/本机，不含客户隐私）：
  %LOCALAPPDATA%\\IPWorkbench\\data-root.json
完整包解压到新目录、没有 .env.local 时，launcher/boot 读取 pointer，仍指向旧库。
pointer 只存一个绝对目录，不扫描盘、不上传任何内容。

迁移用显式 `--adopt --source <旧数据目录>`：用 SQLite backup API 复制数据库，
按原样复制 uploads/artifacts 等附件，写迁移清单；源目录不改。禁止自动扫描。

2026-11-25 资料外置（客户删软件文件夹 = 资料全没）：
  资料夹定位只有这一处规则，顺序：
    进程环境 IP_DATA_DIR > 包根 .env.local > app/.env.local
    > 开发机标记 app/.runtime/data-home.lock（维持包内，只给开发机用，不进包）
    > 本机指针 > 包内「数据已迁出」标记 > 默认「文档\人生重启系统数据」
  老客户第一次启动新版本：prepare() 把包内 app\.runtime 里的客户资料**复制**到包外资料夹
  （只复制、不删包内旧资料；可续；磁盘不够或两边都有数据时不搬，照旧用包内那份）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

PRODUCT = 'rensheng'
DB_NAME = 'workbench.sqlite3'
ASSET_DIRS = ('uploads', 'artifacts', 'avatars', 'packages')
POINTER_SCHEMA = 1

# ---- 资料外置（2026-11-25）
DATA_HOME_NAME = '人生重启系统数据'
DEV_LOCK = 'data-home.lock'            # 包内 app/.runtime：开发机标记，维持包内
MIGRATED_MARK = '数据已迁出.json'       # 包内：指向包外资料夹
MIGRATING_MARK = '.搬迁进行中.json'      # 包外：搬迁没做完就中断了，下次接着搬
MIGRATION_LOG = '资料搬迁记录.json'      # 包外：搬完的清单
NOT_MIGRATED_NOTE = '资料未搬迁-原因.txt'  # 包内：这次没搬的原因，给人和设置页看
README_NAME = '说明.txt'
# 包内 app/.runtime 里「不是客户资料」的东西：随包运行环境、安装/防克隆标识（按包路径读写）、
# 解压快照标记、后端单实例锁。搬迁时跳过。
RUNTIME_NAMES = frozenset((
    'ai-env', 'asr-workshop', 'browser-env', 'native', 'models', 'reference-documents',
    'asr-cache', 'local-asr-ready.json', 'handoff-location.json', '.backend.lock'))
TEMP_NAMES = frozenset(('tmp', 'capture_tmp', 'child-temp'))   # 转写/抽帧临时工作区，可重建
IDENTITY_PREFIXES = ('install_', 'install.', 'clone_')          # 安装标识、防克隆状态
# 搬迁前至少给系统盘/目标盘留的余量
FREE_MARGIN = 64 * 1024 * 1024


def _local_root():
    custom = (os.getenv('IP_STABLE_DATA_ROOT') or '').strip()
    if custom:
        return Path(custom).expanduser().resolve()
    base = (os.getenv('LOCALAPPDATA') or '').strip()
    if base:
        return Path(base) / 'IPWorkbench'
    return Path.home() / '.ipworkbench'


def pointer_path():
    return _local_root() / 'data-root.json'


def stable_data_dir(product=PRODUCT):
    return _local_root() / 'data' / product


def _read_config(path):
    values = {}
    path = Path(path)
    if not path.is_file() or os.getenv('IP_SKIP_LOCAL_CONFIG') == '1':
        return values
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except OSError:
        return values
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        name, sep, value = line.partition('=')
        name = name.strip()
        value = value.strip().strip('"\'')
        if sep and name.startswith('IP_') and value:
            values[name] = value
    return values


def _abs(root, raw):
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    return path.resolve()


def read_pointer(product=PRODUCT):
    path = pointer_path()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get('schema') != POINTER_SCHEMA:
        return {}
    if value.get('product') != product:
        return {}
    return value


def read_mark(root):
    """包内「数据已迁出」标记：{'target': 包外资料夹, 'at': 时间}；没有/损坏返回 {}。"""
    try:
        value = json.loads((Path(root) / 'app' / '.runtime' / MIGRATED_MARK).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not value.get('target'):
        return {}
    return value


def is_package_data(root, path):
    root = Path(root).resolve()
    pkg = (root / 'app' / '.runtime').resolve()
    path = Path(path).resolve()
    return path == pkg or pkg in path.parents or root == path or root in path.parents and path == root


def resolve_data_dir(root, product=PRODUCT):
    """返回数据目录、来源、显式路径是否缺失，以及可用的回落目录。"""
    root = Path(root).resolve()
    default = (root / 'app' / '.runtime').resolve()
    configured = None
    source = 'default'
    if 'IP_DATA_DIR' in os.environ and str(os.environ.get('IP_DATA_DIR') or '').strip():
        configured = _abs(root, os.environ['IP_DATA_DIR'])
        source = 'env'
    elif os.getenv('IP_SKIP_LOCAL_CONFIG') != '1':
        for cfg in (root / '.env.local', root / 'app' / '.env.local'):
            values = _read_config(cfg)
            if values.get('IP_DATA_DIR'):
                configured = _abs(root, values['IP_DATA_DIR'])
                source = '.env.local' if cfg.parent == root else 'app/.env.local'
                break
    if configured is not None:
        if configured.exists():
            return {'path': configured, 'source': source, 'configured': str(configured),
                    'configured_missing': False, 'fallback': None,
                    'pointer': read_pointer(product).get('path')}
        return {'path': default, 'source': source, 'configured': str(configured),
                'configured_missing': True, 'fallback': default,
                'pointer': read_pointer(product).get('path')}
    pointer = read_pointer(product)
    if pointer.get('path'):
        pointed = Path(pointer['path']).expanduser()
        if not pointed.is_absolute():
            pointed = (root / pointed).resolve()
        if pointed.exists():
            return {'path': pointed.resolve(), 'source': 'pointer', 'configured': str(pointed),
                    'configured_missing': False, 'fallback': None, 'pointer': str(pointed)}
        # 2026-11-25：指针记着资料夹、但它不在了（移动硬盘拔了/被改名）。绝不能悄悄落到默认位置
        # 新开一个空库——那会让客户以为资料全丢。单列出来，由 prepare() 停下并给出路。
        return {'path': default, 'source': 'pointer', 'configured': str(pointed),
                'configured_missing': True, 'fallback': default, 'pointer': str(pointed)}
    # 2026-11-25 资料外置：没显式配置、也没指针时的默认
    if not (root / 'app' / '.runtime' / DEV_LOCK).is_file():
        mark = read_mark(root)
        if mark.get('target'):
            marked = Path(mark['target']).expanduser()
            if not marked.is_absolute():
                marked = (root / marked).resolve()
            missing = not marked.is_dir()
            return {'path': default if missing else marked.resolve(), 'source': 'migrated',
                    'configured': str(marked), 'configured_missing': missing,
                    'fallback': default if missing else None, 'pointer': pointer.get('path')}
        candidate, why = default_data_dir()
        return {'path': candidate, 'source': 'default', 'configured': None,
                'configured_missing': False, 'fallback': None, 'pointer': pointer.get('path'),
                'default_reason': why}
    return {'path': default, 'source': 'default', 'configured': None,
            'configured_missing': False, 'fallback': None,
            'pointer': pointer.get('path')}


def effective_data_dir(root, product=PRODUCT):
    return resolve_data_dir(root, product)['path']


def write_pointer(path, product=PRODUCT):
    target = Path(path).expanduser().resolve()
    if not target.is_dir():
        raise ValueError('数据目录不存在：%s' % target)
    value = {'schema': POINTER_SCHEMA, 'product': product, 'path': str(target),
             'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    dest = pointer_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + '.tmp-%s' % uuid.uuid4().hex[:8])
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temporary), str(dest))
    return value


def _sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_db(source, target):
    source = Path(source)
    target = Path(target)
    if not source.is_file():
        return {'skipped': '源数据库不存在', 'source': str(source)}
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ('-wal', '-shm'):
        Path(str(target) + suffix).unlink(missing_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(str(source), timeout=30, isolation_level=None)
    dst = sqlite3.connect(str(target), timeout=30)
    try:
        src.execute('BEGIN')
        src.backup(dst)
        integrity = str(dst.execute('PRAGMA integrity_check').fetchone()[0])
        counts = {}
        names = [row[0] for row in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for name in names:
            counts[name] = int(dst.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0])
        src.execute('COMMIT')
    except Exception:
        try:
            src.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        dst.close()
        src.close()
    if integrity.lower() != 'ok':
        raise RuntimeError('迁移快照完整性失败：%s' % integrity)
    return {'source': str(source), 'target': str(target), 'integrity': integrity,
            'tables': len(counts), 'rows': sum(counts.values()), 'row_counts': counts,
            'sha256': _sha(target)}


# ===================== 资料外置（2026-11-25）=====================

def read_mark(root):
    """包内「数据已迁出」标记：{'target': 包外资料夹, 'at': 时间}；没有/损坏返回 {}。"""
    try:
        value = json.loads((Path(root) / 'app' / '.runtime' / MIGRATED_MARK).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or not value.get('target'):
        return {}
    return value


def is_package_data(root, path):
    """资料目录是不是包内 app\\.runtime（旧式：删软件文件夹 = 资料全没）。"""
    return Path(path).resolve() == (Path(root) / 'app' / '.runtime').resolve()


def documents_dir():
    """系统「文档」文件夹（用户可能改过位置、也可能被 OneDrive 接管）。取不到返回 None。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf) == 0 and buf.value:
            return Path(buf.value)
    except Exception:
        pass
    return None


def default_data_dir():
    """默认资料夹。返回 (路径, 理由)。

    IP_DATA_HOME 显式指定 > 「文档\\人生重启系统数据」 > 「文档」被 OneDrive 接管或取不到时
    放「用户目录\\人生重启系统数据」（和「文档」并排，不被同步，客户也找得到）。
    """
    custom = (os.getenv('IP_DATA_HOME') or '').strip()
    if custom:
        return Path(custom).expanduser().resolve(), 'IP_DATA_HOME'
    docs = documents_dir()
    if docs is not None and 'onedrive' not in str(docs).lower():
        return Path(docs) / DATA_HOME_NAME, 'documents'
    return Path.home().resolve() / DATA_HOME_NAME, 'home'


def _low_integrity():
    """当前进程是否被压到 Low 完整性（exe 继承了 Low 标签时会封顶进程级别）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        k = ctypes.windll.kernel32
        a = ctypes.windll.advapi32
        k.GetCurrentProcess.restype = wt.HANDLE
        k.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        k.OpenProcessToken.restype = wt.BOOL
        a.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                          ctypes.POINTER(wt.DWORD)]
        a.GetTokenInformation.restype = wt.BOOL
        handle = wt.HANDLE()
        if not k.OpenProcessToken(k.GetCurrentProcess(), 0x0008, ctypes.byref(handle)):
            return False
        buf = ctypes.create_string_buffer(256)
        ret = wt.DWORD()
        ok = a.GetTokenInformation(handle, 25, buf, 256, ctypes.byref(ret))
        k.CloseHandle(handle)
        if not ok:
            return False
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        count = ctypes.cast(sid, ctypes.POINTER(ctypes.c_ubyte))[1]
        rid = ctypes.cast(sid + 8 + 4 * (count - 1), ctypes.POINTER(wt.DWORD))[0]
        return rid == 0x1000
    except Exception:
        return False


def _writable(folder):
    """以「真的写一个探测文件成功」为准，不以存在性为准。"""
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ('.write-probe-' + uuid.uuid4().hex[:6])
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


def is_database_file(path):
    """资料目录里的数据库文件（workbench.sqlite3、billing.sqlite3 …）及其配套日志。"""
    name = Path(path).name.lower()
    for suffix in ('.sqlite3', '.sqlite', '.db'):
        if name.endswith(suffix) or name.endswith(suffix + '-wal') or name.endswith(suffix + '-shm') \
                or name.endswith(suffix + '-journal'):
            return name.endswith(suffix)   # 只把主文件算作数据库；日志文件由 db_sidecars 一并排除
    return False


def user_data_entries(package_runtime):
    """包内 app\\.runtime 里属于客户资料的条目（跳过运行环境、安装标识、临时工作区、搬迁标记）。"""
    folder = Path(package_runtime)
    if not folder.is_dir():
        return []
    out = []
    for entry in sorted(folder.iterdir()):
        name = entry.name
        if name in RUNTIME_NAMES or name in TEMP_NAMES or name in (MIGRATED_MARK, NOT_MIGRATED_NOTE):
            continue
        if name.startswith(IDENTITY_PREFIXES) or name.endswith('.lock') or name == '.write-probe.tmp':
            continue
        out.append(entry)
    return out


def _iter_files(entry):
    entry = Path(entry)
    if entry.is_file():
        yield entry
        return
    for path in sorted(entry.rglob('*')):
        if path.is_file():
            yield path


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-%s' % uuid.uuid4().hex[:8])
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temporary), str(path))


def _human(num):
    num = float(num)
    for unit in ('B', 'KB', 'MB'):
        if num < 1024:
            return ('%.0f %s' if unit == 'B' else '%.1f %s') % (num, unit)
        num /= 1024.0
    if num < 1024:
        return '%.1f GB' % num
    return '%.1f TB' % (num / 1024.0)


README_TEXT = (
    '这是「人生重启系统」保存你个人资料的文件夹。\r\n'
    '\r\n'
    '里面有什么：知识库、答题记录、诊断进度、作品记录、额度账本、你上传的文件、导出和备份。\r\n'
    '\r\n'
    '你要记住的一件事：\r\n'
    '  删除或重装软件，不会影响这个文件夹。换电脑或重装时，把整个文件夹拷走、放回「文档」里，资料就回来了。\r\n'
    '  也请把它当成你要备份的东西之一（复制到U盘或网盘）。\r\n'
    '\r\n'
    '升级软件时：把新压缩包解压后覆盖到软件文件夹，不要先删除旧文件夹。\r\n'
    '\r\n'
    '里面可以放心删的：tmp、capture_tmp（处理过程的临时文件）。\r\n'
)


def _note_text(title, body):
    return (title + '\r\n\r\n' + body + '\r\n\r\n（这个说明写在软件文件夹里，只给人看，不影响使用。）\r\n'
            '时间：' + time.strftime('%Y-%m-%d %H:%M:%S') + '\r\n')


PROTECTED_KEYS = frozenset(('text', 'body', 'source_snapshot', 'source_quote', 'share_text', 'title', 'claim',
                            'boundary', 'caption'))   # 与 tools/relocate_snapshot.py 同一份：这些是正文，不是路径


def _rebase_value(value, old, new, key=''):
    """把 JSON 里「指向旧资料目录」的路径改成新目录。正文类字段不碰（与 relocate_snapshot 同规则）。

    old、new 都是「正斜杠、无结尾斜杠」的形式；返回值统一成本机分隔符（Windows 是反斜杠）。
    """
    if isinstance(value, dict):
        return {k: _rebase_value(v, old, new, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase_value(v, old, new, key) for v in value]
    if isinstance(value, str) and key not in PROTECTED_KEYS:
        flat = value.replace('\\', '/')
        folded = flat.casefold()
        if folded == old.casefold() or folded.startswith(old.casefold() + '/'):
            rest = flat[len(old):]
            rest = rest.replace('/', os.sep) if os.sep == '\\' else rest
            return str(Path(new)) + rest
    return value


def rebase_records(db_path, old_dir, new_dir):
    """资料搬家后，改写数据库 records 表里指向旧目录的绝对路径（素材、成片、作业等）。

    不做「媒体文件必须存在」的硬校验：老库里本来就可能有过期记录，不能因为这个拦住客户启动。
    返回 {'changed': 改了几条, 'dangling': 改完后仍指向不存在文件的条数}。
    """
    path = Path(db_path)
    old = str(old_dir).replace('\\', '/').rstrip('/')
    new = str(new_dir).replace('\\', '/').rstrip('/')
    con = sqlite3.connect(str(path), timeout=30)
    changed = 0
    dangling = 0
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'records' in names:
            for kind, key, raw in con.execute('SELECT kind,id,data FROM records').fetchall():
                try:
                    original = json.loads(raw)
                except ValueError:
                    continue
                updated = _rebase_value(original, old, new)
                if updated != original:
                    con.execute('UPDATE records SET data=? WHERE kind=? AND id=?',
                                (json.dumps(updated, ensure_ascii=False), kind, key))
                    changed += 1
            for (raw,) in con.execute("SELECT data FROM records WHERE kind IN ('asset','artifact')").fetchall():
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get('path') and not Path(str(value['path'])).is_file():
                    dangling += 1
        con.commit()
    finally:
        con.close()
    return {'changed': changed, 'dangling': dangling}


def prepare(root, product=PRODUCT, progress=None):
    """启动时调用（启动器和 boot.py 各调一次；做完后是瞬间返回的空操作）。

    返回 dict：ok / path（本次实际使用的资料目录）/ in_package / migrated / created / note / error。
      ok=False 只用于「绝对不能打开一个空库」的情况（本机记录的资料夹不见了、哪里都写不了）；
      调用方应停下并把 error 原样给人看。
      ok=True 且 in_package=True：搬迁没做成（磁盘不够等），照旧用包内那份，note 写明原因。
    定位到包外资料夹时把 IP_DATA_DIR 写进进程环境（外部显式设过的不动，那一支不走到这里）。
    """
    root = Path(root).resolve()
    pkg = (root / 'app' / '.runtime').resolve()
    status = resolve_data_dir(root, product)
    result = {'ok': True, 'path': str(pkg), 'source': status['source'], 'in_package': True,
              'migrated': False, 'created': False, 'note': '', 'error': ''}
    if status['source'] in ('env', '.env.local', 'app/.env.local'):
        result.update(path=str(status['path']), in_package=is_package_data(root, status['path']))
        return result
    target = Path(status['configured'] or status['path']) if status['source'] in ('pointer', 'migrated') else Path(status['path'])
    if target == pkg:
        return result   # 开发机标记（data-home.lock）：维持包内，不搬、不写指针

    pkg_has_data = bool(user_data_entries(pkg))

    def fallback_in_package(reason):
        # 外部资料夹不可用、但包内还有客户资料：回包内继续启动，绝不硬停、绝不悄悄开空库。
        # 写进进程环境，让随后 spawn 的后端也用包内这份（后端独立解析会再次撞到写保护）。
        os.environ['IP_DATA_DIR'] = str(pkg)
        result.update(path=str(pkg), in_package=True,
                      note='外部资料夹暂时不可用，本次先用软件文件夹内的资料；你的资料没有丢，外部那份原样还在。\n' + reason)
        return result

    external_reason = ''
    if status['source'] in ('pointer', 'migrated'):
        if status.get('configured_missing') or not target.is_dir():
            external_reason = '本机记录的资料文件夹不在了：%s' % target
        elif not _writable(target):
            external_reason = '资料文件夹现在写不进去：%s（可能是权限、杀软拦截或写保护）' % target
    elif not _writable(target):
        external_reason = '默认资料夹写不进去：%s' % target

    if external_reason and _low_integrity():
        external_reason += ('\n技术线索：当前进程完整性级别=Low（软件目录继承了 Low 完整性标签，'
                            '写不进普通目录）。解除：对软件目录执行 icacls "目录" /setintegritylevel M 后重试。')

    if external_reason:
        if pkg_has_data:
            return fallback_in_package(external_reason)
        if status['source'] in ('pointer', 'migrated'):
            # 记录的资料夹丢了、包内也没资料：不能悄悄开空库，停下并给出路（失败卡会显示这段）。
            result.update(ok=False, error=(
                external_reason + '\n为了不让你误以为资料丢了，程序先不打开一个空库。\n'
                '出路：① 把这个文件夹放回原位（或插回U盘/移动硬盘）再启动；\n'
                '② 确实不要这份资料了：删除文件 %s 后再启动，会在「文档」里新建一份。' % pointer_path()))
            return result
        spare = stable_data_dir(product)
        if not _writable(spare):
            result.update(ok=False, error=(
                external_reason + '\n备用位置也写不进：%s\n请检查权限、杀软或磁盘空间后重试；资料没有被动过。' % spare))
            return result
        target = spare

    target.mkdir(parents=True, exist_ok=True)
    if not (target / README_NAME).is_file():
        (target / README_NAME).write_text(README_TEXT, encoding='utf-8')
        result['created'] = True
    write_pointer(target, product)

    def use_external(migrated=False, note=''):
        result.update(path=str(target), in_package=False, migrated=migrated, note=note)
        os.environ.setdefault('IP_DATA_DIR', str(target))
        return result

    # ---- 搬迁：包内还有客户资料、且没搬过
    entries = user_data_entries(pkg)
    src_db = pkg / DB_NAME
    if (pkg / MIGRATED_MARK).is_file() or not entries:
        return use_external()

    def stay(reason, detail):
        """不搬，照旧用包内那份；原因写进包内说明文件，设置页也读得到。"""
        try:
            (pkg / NOT_MIGRATED_NOTE).write_text(_note_text(reason, detail), encoding='utf-8')
        except OSError:
            pass
        result.update(note=reason + '：' + detail)
        return result

    tgt_db = target / DB_NAME
    marker = target / MIGRATING_MARK
    try:
        state = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {}
    except (OSError, ValueError):
        state = {}
    # 只有「开始了、没做完」才算续搬。做完的标记留着（不删文件），绝不能当成续搬——
    # 否则同一台电脑上另一份带旧资料的软件文件夹会覆盖这里正在用的库。
    resuming = bool(state.get('source')) and state.get('done') is False
    if src_db.is_file() and tgt_db.is_file() and not resuming:
        return stay('软件文件夹里还有一份旧资料，没有并进现在的资料文件夹',
                    '两边都有数据，程序不敢自动覆盖任何一边。现在用的是：%s\n'
                    '旧资料原样留在软件文件夹的 app\\.runtime 里；需要的话先用旧版本打开，'
                    '到「资产与备份」导出，再在这里导入。' % target)
    files = [(path, path.relative_to(pkg)) for entry in entries for path in _iter_files(entry)]
    db_files = [path for path, _ in files if is_database_file(path)]
    db_sidecars = set(db_files)
    for path in db_files:
        db_sidecars.update(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    # 普通复制循环不碰数据库及其日志文件（数据库只走一致性快照）
    files = [item for item in files if item[0] not in db_sidecars]
    todo = [item for item in files
            if not (target / item[1]).is_file() or (target / item[1]).stat().st_size != item[0].stat().st_size]
    need = sum(item[0].stat().st_size for item in todo)
    db_bytes = 0
    for path in db_files:
        target_db = target / path.relative_to(pkg)
        db_bytes += max(0, path.stat().st_size - (target_db.stat().st_size if target_db.is_file() else 0))
    free = shutil.disk_usage(str(target)).free
    if need + db_bytes + FREE_MARGIN > free:
        return stay('资料还没有搬到软件文件夹外面',
                    '要搬 %s，目标盘只剩 %s。资料原样留在软件文件夹里，照常可用。\n'
                    '清理出足够空间后重新打开软件，会自动再试一次。' % (_human(need + db_bytes), _human(free)))

    failing = ''
    try:
        if src_db.is_file():
            _write_json(marker, {'source': str(pkg), 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'done': False})
        rebase = {'changed': 0, 'dangling': 0}
        db_errors = []
        # 所有数据库（主库、账本……）只走 SQLite 一致性快照：原样拷主文件会丢配套日志里的最近写入，
        # 也会盖掉刚改好路径的那份。快照做不了的（不是 SQLite 库）退回原样复制并记下来。
        for db_file in db_files:
            dest_db = target / db_file.relative_to(pkg)
            failing = db_file.relative_to(pkg).as_posix()
            try:
                _snapshot_db(db_file, dest_db)
            except sqlite3.DatabaseError as exc:
                db_errors.append('%s：%s' % (db_file.relative_to(pkg).as_posix(), exc))
                todo.append((db_file, db_file.relative_to(pkg)))
                continue
            if db_file.name == DB_NAME:
                # 库里存的是指向旧目录的绝对路径（素材、成片、作业）——不改写的话，客户删掉旧文件夹后这些引用全断
                rebase = rebase_records(dest_db, pkg, target)
        done = db_bytes
        total = need + db_bytes or 1
        for path, rel in todo:
            failing = rel.as_posix()
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            staging = dest.with_name(dest.name + '.migrating')
            shutil.copy2(str(path), str(staging))
            os.replace(str(staging), str(dest))
            done += path.stat().st_size
            if progress:
                try:
                    progress(min(done, total), total, rel.as_posix())
                except Exception:
                    pass
        _write_json(target / MIGRATION_LOG, {
            'schema': 1, 'product': product, 'source': str(pkg), 'target': str(target),
            'finished_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'database_copied': src_db.is_file(), 'files': len(todo),
            'databases': [p.relative_to(pkg).as_posix() for p in db_files], 'database_fallbacks': db_errors,
            'bytes': sum(item[0].stat().st_size for item in todo),
            'paths_rebased': rebase.get('changed', 0), 'records_pointing_to_missing_files': rebase.get('dangling', 0),
            'names': [entry.name for entry in entries]})
        if marker.is_file():
            _write_json(marker, {'source': str(pkg), 'done': True,
                                 'finished_at': time.strftime('%Y-%m-%d %H:%M:%S')})
        _write_json(pkg / MIGRATED_MARK, {
            'target': str(target), 'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'note': '客户资料已复制到包外；包内 app\\.runtime 里的旧资料未删除。'})
        write_pointer(target, product)
    except Exception as exc:
        return stay('资料搬到一半没搬完',
                    '下次打开软件会接着搬，已搬好的不重复。原因：%s（卡在：%s）\n'
                    '资料原样留在软件文件夹里，照常可用。' % (exc, failing))

    return use_external(migrated=True, note='已把你的资料搬到软件文件夹外面：%s' % target)


def describe(result):
    """prepare() 结果 → 给人看的几行话（启动器日志、boot.py 控制台、启动器状态）。"""
    if not result.get('ok'):
        return ['资料文件夹不可用：' + str(result.get('error') or '')]
    if result.get('in_package'):
        lines = ['资料目录：%s（在软件文件夹内）' % result.get('path')]
        if result.get('note'):
            lines.append('  ' + str(result['note']).replace('\n', ' '))
        return lines
    lines = ['资料目录：%s（在软件文件夹外，删除软件不影响）' % result.get('path')]
    if result.get('migrated'):
        lines.append('  已把旧资料搬过来（软件文件夹里的旧资料没有删除）。')
    return lines


def adopt(root, source=None, dest=None, product=PRODUCT):
    """显式迁移：源目录只读；目标空/不存在才允许，避免覆盖现有数据。"""
    root = Path(root).resolve()
    status = resolve_data_dir(root, product)
    source = Path(source).expanduser().resolve() if source else Path(status['fallback'] or status['path'])
    dest = Path(dest).expanduser().resolve() if dest else stable_data_dir(product)
    if source == dest:
        write_pointer(dest, product)
        return {'ok': True, 'source': str(source), 'target': str(dest), 'noop': True}
    if not source.is_dir():
        raise ValueError('源数据目录不存在：%s' % source)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError('目标数据目录非空，拒绝覆盖：%s' % dest)
    dest.mkdir(parents=True, exist_ok=True)
    info = {'schema': 1, 'product': product, 'source': str(source), 'target': str(dest),
            'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'database': None, 'databases': [], 'assets': []}
    try:
        # 与 prepare() 同一份客户资料白名单：checkin 答题目录等非资产目录也要带走
        entries = user_data_entries(source)
        files = [(path, path.relative_to(source)) for entry in entries for path in _iter_files(entry)]
        db_files = [path for path, _ in files if is_database_file(path)]
        sidecars = set(db_files)
        for path in db_files:
            sidecars.update(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
        for db_file in db_files:
            dest_db = dest / db_file.relative_to(source)
            dest_db.parent.mkdir(parents=True, exist_ok=True)
            snap = _snapshot_db(db_file, dest_db)
            info['databases'].append(db_file.relative_to(source).as_posix())
            if db_file.name == DB_NAME:
                info['database'] = snap
                info['paths_rebased'] = rebase_records(dest_db, source, dest).get('changed', 0)
        for path, rel in files:
            if path in sidecars:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(target))
            info['assets'].append({'path': rel.as_posix(), 'sha256': _sha(path)})
        info['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        (dest / 'data-migration.json').write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
        write_pointer(dest, product)
        return {'ok': True, 'source': str(source), 'target': str(dest), 'database': info['database'],
                'assets': len(info['assets'])}
    except Exception:
        # 失败不删源；目标留下 partial 供人工核查，不自动当成功。
        raise


def main():
    parser = argparse.ArgumentParser(description='解析/承接工作台数据目录')
    parser.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--product', default=PRODUCT)
    parser.add_argument('--show', action='store_true')
    parser.add_argument('--set-pointer')
    parser.add_argument('--adopt', action='store_true')
    parser.add_argument('--source')
    parser.add_argument('--dest')
    parser.add_argument('--prepare', action='store_true',
                        help='启动时用：定位包外资料夹，老客户首次启动搬迁；结果写一行 JSON')
    parser.add_argument('--print-dir', action='store_true',
                        help='prepare 之后只打印资料目录的路径（给 .bat 用）')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if args.prepare or args.print_dir:
        def _progress(done, total, name):
            if not args.print_dir:
                print('[资料] 搬迁 %d%%  %s' % (100 * done // max(1, total), name), flush=True)
        outcome = prepare(root, product=args.product, progress=_progress)
        if args.print_dir:
            if not outcome.get('ok'):
                return 4
            print(outcome.get('path') or '', flush=True)
            return 0
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
        for line in describe(outcome):
            print(line, flush=True)
        return 0 if outcome.get('ok') else 4
    if args.set_pointer:
        print(json.dumps(write_pointer(Path(args.set_pointer), args.product), ensure_ascii=False))
        return 0
    if args.adopt:
        print(json.dumps(adopt(root, args.source, args.dest, args.product), ensure_ascii=False, indent=2))
        return 0
    status = resolve_data_dir(root, args.product)
    print(json.dumps({key: str(value) if isinstance(value, Path) else value
                      for key, value in status.items()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
