"""自动备份 + 全量导出 + 全量恢复（只依赖标准库与 db.py）。

设计口径：
- 自动备份只复制 workbench.sqlite3（sqlite backup API，含 WAL 一致性快照），
  每天最多一份 auto-YYYYMMDD.sqlite3，只保留最近 7 份 auto 备份；
  用户手动放进 backups/ 的文件永不删除。
- 全量包 = 数据库快照 + uploads + artifacts + avatars + 模板授权记录 + manifest.json。
  清单记录每个文件的 sha256/字节数与各表条数/记录摘要，恢复前逐项校验。
- 恢复先写临时目录并校验，全部通过后才落到目标目录；任何失败目标目录保持原样。
- 恢复时把记录里的本机绝对路径重写到新数据目录，换机器后媒体仍可打开。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
BUNDLE_ROOT = APP_ROOT.parent
FORMAT = 'ip-workbench-full-backup'
FORMAT_VERSION = 1
DATA_DIRS = ('uploads', 'artifacts', 'avatars', 'packages')
LICENSE_KINDS = ('cover_template_license', 'avatar_license', 'entitlement')
MEDIA_SUFFIXES = {'.mp4', '.mov', '.webm', '.mkv', '.m4v', '.mp3', '.m4a', '.wav', '.aac',
                  '.png', '.jpg', '.jpeg', '.webp', '.gif'}
ABS_PATH = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')
FOLDER_PATH = re.compile(r'^(?:[A-Za-z]:[\\/]|[\\/]).*?[\\/](uploads|artifacts|avatars|avatar-frames|packages)[\\/](.+)$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
EXPORT_NAME = re.compile(r'^(数据库|全量)-\d{8}-\d{6}(?:-\d+)?\.zip$')
PACKAGE_TYPES = ('database', 'full')


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_version(app_root=APP_ROOT):
    path = Path(app_root) / 'web' / 'version.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        data = {}
    version = str(data.get('version') or 'unknown')
    try:
        schema_version = int(data.get('data_version', 1))
    except (TypeError, ValueError):
        schema_version = 1
    return {'app_version': version, 'schema_version': schema_version}


def db_schema(db_path):
    """按 sqlite_master 里表/索引定义算指纹；空白归一化，跨机器稳定。"""
    con = sqlite3.connect(str(db_path), timeout=20)
    try:
        rows = con.execute("SELECT type,name,sql FROM sqlite_master "
                           "WHERE type IN ('table','index') AND sql IS NOT NULL "
                           "ORDER BY type,name").fetchall()
    finally:
        con.close()
    normalized = '\n'.join('{0}|{1}|{2}'.format(kind, name, re.sub(r'\s+', ' ', sql or '').strip())
                           for kind, name, sql in rows)
    return {'fingerprint': hashlib.sha256(normalized.encode('utf-8')).hexdigest(),
            'tables': sorted(name for kind, name, sql in rows if kind == 'table')}


def core_schema(db_path):
    """只按 records/imports 两张核心表及其索引算指纹。

    真实数据目录里还会由各模块建 content_index、auth_* 等派生表，
    与程序新建空库天然不同；用整库指纹会把同版本导出的备份误判为不兼容。
    """
    con = sqlite3.connect(str(db_path), timeout=20)
    try:
        rows = con.execute("SELECT type,name,sql FROM sqlite_master "
                           "WHERE type IN ('table','index') AND sql IS NOT NULL "
                           "AND name NOT LIKE 'sqlite_%' "
                           "AND tbl_name IN ('records','imports') "
                           "ORDER BY type,name").fetchall()
    finally:
        con.close()
    normalized = '\n'.join('{0}|{1}|{2}'.format(kind, name, re.sub(r'\s+', ' ', sql or '').strip())
                           for kind, name, sql in rows)
    return {'fingerprint': hashlib.sha256(normalized.encode('utf-8')).hexdigest(),
            'tables': sorted(name for kind, name, sql in rows if kind == 'table')}


_SCHEMA_CACHE = {}


def current_schema(app_root=APP_ROOT):
    """用 db.Store 在临时目录建一份当前版空库，得到程序支持的核心表指纹。结果按程序目录缓存。"""
    key = str(Path(app_root).resolve())
    if key in _SCHEMA_CACHE:
        return dict(_SCHEMA_CACHE[key])
    version = load_version(app_root)
    from db import Store
    with tempfile.TemporaryDirectory(prefix='ip-schema-') as tmp:
        Store(Path(tmp))
        info = core_schema(Path(tmp) / 'workbench.sqlite3')
    result = {'schema_version': version['schema_version'],
              'schema_fingerprint': info['fingerprint'], 'tables': info['tables'],
              'app_version': version['app_version']}
    _SCHEMA_CACHE[key] = dict(result)
    return result


def billing_schema_version(data_dir):
    path = Path(data_dir) / 'billing.sqlite3'
    if not path.is_file():
        return None
    con = sqlite3.connect(str(path), timeout=20)
    try:
        value = con.execute('PRAGMA user_version').fetchone()[0]
    finally:
        con.close()
    try:
        value = max(int(value), 0)
    except (TypeError, ValueError):
        value = 0
    return value or None


def _db_snapshot(source, target):
    source = Path(source)
    target = Path(target)
    src = sqlite3.connect('file:{0}?mode=ro'.format(str(source).replace('\\', '/')), uri=True, timeout=20)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def _copy_database(source_db, target_db):
    """把 source_db 的表数据整表覆盖到 target_db（单事务，失败自动回滚）。

    用 ATTACH 而不是替换文件：Windows 上服务进程持有 sqlite 连接时，
    删除/移动 .sqlite3 会被文件锁拒绝；整表覆盖不依赖独占文件句柄。
    """
    target = sqlite3.connect(str(target_db), timeout=30)
    try:
        target.execute('ATTACH DATABASE ? AS restore', (str(Path(source_db).resolve()),))
        tables = [row[0] for row in target.execute(
            "SELECT name FROM restore.sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        try:
            target.execute('BEGIN IMMEDIATE')
            for name in tables:
                quoted = '"' + name.replace('"', '""') + '"'
                target.execute('DELETE FROM main.{0}'.format(quoted))
                target.execute('INSERT INTO main.{0} SELECT * FROM restore.{0}'.format(quoted))
            target.commit()
        except Exception:
            target.rollback()
            raise
        finally:
            target.execute('DETACH DATABASE restore')
    finally:
        target.close()
    return target_db


def _local_date(ts):
    return time.strftime('%Y%m%d', time.localtime(ts))


def _local_stamp(ts):
    return time.strftime('%Y%m%d-%H%M%S', time.localtime(ts))


def _package_type(manifest, path):
    value = str(manifest.get('package_type') or '')
    if value in PACKAGE_TYPES:
        return value
    if any(str(item.get('path') or '').split('/')[0] in DATA_DIRS
           for item in (manifest.get('files') or [])):
        return 'full'
    return 'database' if Path(path).name.startswith('数据库-') else 'full'


def _export_item(path):
    stat = path.stat()
    package_type = 'full'
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
        package_type = _package_type(manifest, path)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        package_type = 'full' if path.name.startswith('全量-') else 'database'
    return {'name': path.name, 'bytes': stat.st_size,
            'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
            'path': str(path), 'package_type': package_type}


def cleanup_exports(data_dir=None, keep=None):
    """只清理本工具生成、且文件名带时间戳的备份包；用户手动放入的文件不动。"""
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime'))
    folder = data_dir / 'exports'
    if not folder.is_dir():
        return []
    if keep is None:
        try:
            keep = int(os.getenv('IP_BACKUP_KEEP') or 10)
        except (TypeError, ValueError):
            keep = 10
    keep = max(1, keep)
    items = []
    for path in folder.glob('*.zip'):
        if not EXPORT_NAME.match(path.name):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue
        if manifest.get('format') != FORMAT:
            continue
        items.append((path.stat().st_mtime, path))
    items.sort(key=lambda item: item[0])
    removed = []
    if len(items) > keep:
        for _, path in items[:-keep]:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def maybe_auto_backup(data_dir, now_ts=None, interval=24 * 3600, keep=7):
    """距最新 auto 备份 >= 24h 才复制；只保留最近 keep 份 auto 备份。"""
    data_dir = Path(data_dir)
    db = data_dir / 'workbench.sqlite3'
    if not db.is_file():
        return {'created': False, 'reason': 'no-database'}
    now_ts = time.time() if now_ts is None else float(now_ts)
    folder = data_dir / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    autos = sorted(folder.glob('auto-*.sqlite3'), key=lambda p: p.stat().st_mtime)
    if autos:
        age = now_ts - autos[-1].stat().st_mtime
        if age < interval:
            return {'created': False, 'reason': 'fresh', 'last': autos[-1].name,
                    'age_hours': round(age / 3600, 2), 'next_due_hours': round((interval - age) / 3600, 2)}
    target = folder / 'auto-{0}.sqlite3'.format(_local_date(now_ts))
    temporary = folder / '.auto-{0}.tmp'.format(os.getpid())
    _db_snapshot(db, temporary)
    temporary.replace(target)
    removed = []
    autos = sorted(folder.glob('auto-*.sqlite3'), key=lambda p: p.stat().st_mtime)
    for old in autos[:-keep] if len(autos) > keep else []:
        old.unlink()
        removed.append(old.name)
    return {'created': True, 'path': str(target), 'filename': target.name,
            'bytes': target.stat().st_size, 'kept': len(autos) - len(removed), 'removed': removed}


def _posix(path):
    return PurePosixPath(Path(path).as_posix())


def _safe_rel(value, data_root):
    path = Path(value).resolve()
    if not path.is_relative_to(Path(data_root).resolve()):
        raise ValueError('文件不在数据目录内，已拒绝打包：' + path.name)
    return PurePosixPath(path.relative_to(Path(data_root).resolve()).as_posix())


def _iter_data_files(data_dir):
    data_dir = Path(data_dir)
    for folder in DATA_DIRS:
        root = data_dir / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob('*')):
            if path.is_file() and not path.name.startswith('.') and not path.is_symlink():
                yield path


def _file_sha256(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        while True:
            raw = handle.read(chunk)
            if not raw:
                break
            digest.update(raw)
            size += len(raw)
    return size, digest.hexdigest()


def _normalize_paths(value):
    """把绝对文件路径归一化为数据目录内相对路径，用于跨机器比较记录摘要。"""
    if isinstance(value, dict):
        return {key: _normalize_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_paths(item) for item in value]
    if isinstance(value, str) and ABS_PATH.match(value):
        match = FOLDER_PATH.match(value)
        if match:
            return match.group(1) + '/' + match.group(2).replace('\\', '/').lstrip('/')
    return value


def table_digests(db_path):
    """每张 kind 的条数与内容摘要；路径字段归一化后再算，换机器仍可对比。"""
    con = sqlite3.connect(str(db_path), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute('SELECT kind,id,data,revision FROM records ORDER BY kind,id').fetchall()
        imports = con.execute('SELECT COUNT(*) FROM imports').fetchone()[0]
    finally:
        con.close()
    groups = {}
    for row in rows:
        try:
            parsed = json.loads(row['data'])
        except ValueError:
            parsed = row['data']
        canonical = json.dumps(_normalize_paths(parsed), ensure_ascii=False, sort_keys=True,
                               separators=(',', ':'))
        item = '{0}\x1f{1}\x1f{2}\x1f{3}'.format(row['kind'], row['id'], row['revision'], canonical)
        groups.setdefault(row['kind'], []).append((row['id'], hashlib.sha256(item.encode('utf-8')).hexdigest()))
    result = {}
    for kind, items in groups.items():
        digest = hashlib.sha256()
        for record_id, record_hash in sorted(items):
            digest.update('{0}\n{1}\n'.format(record_id, record_hash).encode('utf-8'))
        result[kind] = {'count': len(items), 'digest': digest.hexdigest()}
    overall = hashlib.sha256()
    for kind in sorted(result):
        overall.update('{0}:{1}:{2}\n'.format(kind, result[kind]['count'], result[kind]['digest']).encode('utf-8'))
    return {'kinds': result, 'imports': imports, 'total': len(rows), 'overall': overall.hexdigest()}


def _folder_stats(data_dir):
    stats = {}
    for folder in DATA_DIRS:
        root = Path(data_dir) / folder
        files = [p for p in root.rglob('*') if p.is_file()] if root.is_dir() else []
        stats[folder] = {'files': len(files), 'bytes': sum(p.stat().st_size for p in files)}
    return stats


def _licenses(db_path):
    con = sqlite3.connect(str(db_path), timeout=20)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ','.join('?' for _ in LICENSE_KINDS)
        rows = con.execute('SELECT kind,id,data,revision,created_at,updated_at FROM records '
                           'WHERE kind IN ({0}) ORDER BY kind,id'.format(placeholders), LICENSE_KINDS).fetchall()
    finally:
        con.close()
    items = []
    for row in rows:
        try:
            data = json.loads(row['data'])
        except ValueError:
            data = row['data']
        items.append({'kind': row['kind'], 'id': row['id'], 'revision': row['revision'],
                      'created_at': row['created_at'], 'updated_at': row['updated_at'], 'data': data})
    return {'kinds': list(LICENSE_KINDS), 'count': len(items), 'items': items}


def _write_json_member(archive, name, payload):
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, raw)
    return {'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _write_text_member(archive, name, text):
    raw = str(text).encode('utf-8')
    info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, raw)
    return {'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _write_stream_member(archive, name, source, suffix=''):
    source = Path(source)
    stat = source.stat()
    info = zipfile.ZipInfo(name, date_time=time.localtime(stat.st_mtime)[:6])
    info.compress_type = zipfile.ZIP_STORED if source.suffix.lower() in MEDIA_SUFFIXES else zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, 'w', force_zip64=True) as handle, source.open('rb') as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return {'path': name, 'bytes': size, 'sha256': digest.hexdigest()}


def _platform_label():
    import platform
    return '{0} {1} ({2})'.format(platform.system(), platform.release(), platform.machine())


def export_full(data_dir=None, app_root=APP_ROOT, out_dir=None, progress=None, tick=None):
    """tick(done, total)：文件级进度回调，供后台作业写 done/total（文字版仍走 progress）。"""
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime')).resolve()
    db = data_dir / 'workbench.sqlite3'
    if not db.is_file():
        raise ValueError('当前数据目录里没有 workbench.sqlite3，先打开工作台生成数据再导出。')
    schema = core_schema(db)
    version = load_version(app_root)
    out_dir = Path(out_dir or (data_dir / 'exports'))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _local_stamp(time.time())
    target = out_dir / '全量-{0}.zip'.format(stamp)
    sequence = 2
    while target.exists():
        target = out_dir / '全量-{0}-{1}.zip'.format(stamp, sequence)
        sequence += 1
    temporary = out_dir / '.full-{0}.tmp'.format(os.getpid())
    started = time.monotonic()
    files = []
    total_bytes = 0
    try:
        with tempfile.TemporaryDirectory(prefix='ip-export-', dir=str(out_dir)) as tmp:
            tmp = Path(tmp)
            snapshot = _db_snapshot(db, tmp / 'workbench.sqlite3')
            billing = data_dir / 'billing.sqlite3'
            billing_snapshot = None
            if billing.is_file():
                billing_snapshot = _db_snapshot(billing, tmp / 'billing.sqlite3')
            licenses = _licenses(snapshot)
            licenses_file = tmp / 'licenses.json'
            licenses_file.write_text(json.dumps(licenses, ensure_ascii=False, indent=2, sort_keys=True),
                                     encoding='utf-8')
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                files.append(_write_text_member(archive, 'README.txt', _readme_text(version)))
                licenses_member = _write_stream_member(archive, 'licenses.json', licenses_file)
                files.append(licenses_member)
                db_member = _write_stream_member(archive, 'workbench.sqlite3', snapshot)
                files.append(db_member)
                if billing_snapshot:
                    member = _write_stream_member(archive, 'billing.sqlite3', billing_snapshot)
                    files.append(member)
                count = 0
                entries = list(_iter_data_files(data_dir))
                total_files = len(entries)
                if tick:
                    tick(0, total_files)
                for path in entries:
                    rel = _safe_rel(path, data_dir)
                    name = rel.as_posix()
                    member = _write_stream_member(archive, name, path)
                    files.append(member)
                    total_bytes += member['bytes']
                    count += 1
                    if progress and count % 200 == 0:
                        progress('打包 {0} 个文件，{1:.1f} MB'.format(count, total_bytes / 1048576))
                    if tick and (count % 20 == 0 or count == total_files):
                        tick(count, total_files)
                manifest = {
                    'format': FORMAT,
                    'format_version': FORMAT_VERSION,
                    'package_type': 'full',
                    'created_at': _now_iso(),
                    'app_version': version['app_version'],
                    'schema_version': version['schema_version'],
                    'schema_fingerprint': schema['fingerprint'],
                    'billing_schema_version': billing_schema_version(data_dir),
                    'platform': _platform_label(),
                    'python': sys.version.split()[0],
                    'contains': ['workbench.sqlite3', 'uploads', 'artifacts', 'avatars', 'packages',
                                 'template/avatar licenses', 'manifest with sha256 per file'],
                    'excludes': ['logs', 'backups', 'exports', 'diagnostics', 'AI/ASR runtime', 'models', 'cache'],
                    'tables': table_digests(snapshot),
                    'folder_stats': _folder_stats(data_dir),
                    'licenses': {'file': 'licenses.json', 'count': licenses['count']},
                    'files': files,
                    'files_count': len(files),
                    'total_bytes': total_bytes,
                }
                manifest_member = _write_json_member(archive, 'manifest.json', manifest)
        if temporary.stat().st_size <= 0:
            raise ValueError('导出包为空，已中止。')
        temporary.replace(target)
        removed = cleanup_exports(data_dir)
        elapsed = round(time.monotonic() - started, 2)
        result = {'path': str(target), 'filename': target.name, 'bytes': target.stat().st_size,
                  'files': manifest['files_count'], 'media_bytes': total_bytes,
                  'package_type': 'full', 'removed': removed,
                  'tables': manifest['tables'], 'folder_stats': manifest['folder_stats'],
                  'schema_version': manifest['schema_version'], 'app_version': manifest['app_version'],
                  'elapsed_s': elapsed, 'manifest_sha256': manifest_member['sha256']}
        return result
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def export_database(data_dir=None, app_root=APP_ROOT, out_dir=None):
    """只导出数据库（workbench.sqlite3 + billing.sqlite3），体积小、秒级完成。"""
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime')).resolve()
    db = data_dir / 'workbench.sqlite3'
    if not db.is_file():
        raise ValueError('当前数据目录里没有 workbench.sqlite3，先打开工作台生成数据再导出。')
    version = load_version(app_root)
    out_dir = Path(out_dir or (data_dir / 'exports'))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _local_stamp(time.time())
    target = out_dir / '数据库-{0}.zip'.format(stamp)
    sequence = 2
    while target.exists():
        target = out_dir / '数据库-{0}-{1}.zip'.format(stamp, sequence)
        sequence += 1
    temporary = out_dir / '.database-{0}.tmp'.format(os.getpid())
    started = time.monotonic()
    files = []
    total_bytes = 0
    try:
        with tempfile.TemporaryDirectory(prefix='ip-export-db-', dir=str(out_dir)) as tmp:
            tmp = Path(tmp)
            snapshot = _db_snapshot(db, tmp / 'workbench.sqlite3')
            billing = data_dir / 'billing.sqlite3'
            billing_snapshot = None
            if billing.is_file():
                billing_snapshot = _db_snapshot(billing, tmp / 'billing.sqlite3')
            members = [snapshot] + ([billing_snapshot] if billing_snapshot else [])
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED,
                                 compresslevel=1, allowZip64=True) as archive:
                for path in members:
                    member = _write_stream_member(archive, path.name, path)
                    files.append(member)
                    total_bytes += member['bytes']
                schema = core_schema(snapshot)
                manifest = {
                    'format': FORMAT,
                    'format_version': FORMAT_VERSION,
                    'package_type': 'database',
                    'created_at': _now_iso(),
                    'app_version': version['app_version'],
                    'schema_version': version['schema_version'],
                    'schema_fingerprint': schema['fingerprint'],
                    'billing_schema_version': billing_schema_version(data_dir),
                    'platform': _platform_label(),
                    'python': sys.version.split()[0],
                    'contains': ['workbench.sqlite3', 'billing.sqlite3', 'manifest with sha256 per file'],
                    'excludes': ['uploads', 'artifacts', 'avatars', 'packages', 'logs', 'backups',
                                 'exports', 'diagnostics', 'AI/ASR runtime', 'models', 'cache'],
                    'tables': table_digests(snapshot),
                    'folder_stats': {},
                    'files': files,
                    'files_count': len(files),
                    'total_bytes': total_bytes,
                }
                manifest_member = _write_json_member(archive, 'manifest.json', manifest)
        if temporary.stat().st_size <= 0:
            raise ValueError('导出包为空，已中止。')
        temporary.replace(target)
        removed = cleanup_exports(data_dir)
        elapsed = round(time.monotonic() - started, 2)
        return {'path': str(target), 'filename': target.name, 'bytes': target.stat().st_size,
                'files': manifest['files_count'], 'media_bytes': 0,
                'package_type': 'database', 'removed': removed,
                'tables': manifest['tables'], 'folder_stats': {},
                'schema_version': manifest['schema_version'], 'app_version': manifest['app_version'],
                'elapsed_s': elapsed, 'manifest_sha256': manifest_member['sha256']}
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _readme_text(version, data_dir=None):
    return ('IP 工作台全量备份\n'
            '==================\n'
            '应用版本：{0}\n'
            '数据版本：{1}\n\n'
            '包含：workbench.sqlite3、uploads/、artifacts/、avatars/、packages/、模板与形象授权记录、manifest.json。\n'
            '不包含：日志、自动备份、诊断包、导出记录、AI/ASR 运行时、模型与缓存。\n\n'
            '恢复方式（在 app 目录执行）：\n'
            '  tools\\python\\python.exe -m backup_restore restore "本包.zip" --to "新的数据目录"\n'
            '目标目录必须不存在或为空；恢复前会校验清单里每个文件的 sha256 与 schema 版本。\n'
            '校验失败时目标目录不会被写入，原数据保持不变。\n'
            .format(version['app_version'], version['schema_version']))


def _read_manifest(archive):
    try:
        raw = archive.read('manifest.json')
    except KeyError:
        raise ValueError('不是全量备份包：缺少 manifest.json。')
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ValueError('manifest.json 无法解析，备份包可能已损坏。')
    if manifest.get('format') != FORMAT:
        raise ValueError('备份包格式不匹配：需要 {0}，实际 {1}。'.format(FORMAT, manifest.get('format')))
    if manifest.get('format_version') != FORMAT_VERSION:
        raise ValueError('备份包格式版本不兼容：包内 {0}，当前程序 {1}。'.format(
            manifest.get('format_version'), FORMAT_VERSION))
    return manifest


def _version_gap(manifest, current):
    theirs = manifest.get('schema_version')
    ours = current['schema_version']
    try:
        theirs = int(theirs)
    except (TypeError, ValueError):
        raise ValueError('备份包 schema 版本无效：{0}。'.format(theirs))
    if theirs == ours:
        return
    diff = theirs - ours
    if diff < 0:
        raise ValueError('备份 schema 版本 {0}，当前程序 {1}，低 {2} 个版本；本版本没有向下迁移器，'
                         '请用 {3} 或更高兼容版本导出后再恢复。'.format(theirs, ours, abs(diff), current['app_version']))
    raise ValueError('备份 schema 版本 {0}，当前程序 {1}，高 {2} 个版本；请先升级工作台再恢复。'.format(
        theirs, ours, diff))


def _check_manifest_files(manifest):
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('manifest.json 缺少文件清单。')
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('文件清单格式无效。')
        name = str(item.get('path') or '')
        pure = PurePosixPath(name)
        if (not name or pure.is_absolute() or '..' in pure.parts or '\\' in name
                or ':' in name or PureWindowsPath(name).drive):
            # 带盘符的写法在 PurePosixPath 眼里不算绝对路径，但 Windows 下
            # 拼接后会直接落到该盘根目录，必须在清单阶段就拒掉。
            raise ValueError('备份包含非法路径：{0}。'.format(name))
        if name in ('manifest.json',) or name in seen:
            raise ValueError('备份文件清单重复或包含保留文件：{0}。'.format(name))
        seen.add(name)
        if not SHA256.match(str(item.get('sha256') or '')):
            raise ValueError('文件 {0} 的 sha256 无效。'.format(name))
        if not isinstance(item.get('bytes'), int) or item['bytes'] < 0:
            raise ValueError('文件 {0} 的大小无效。'.format(name))
    return {item['path']: item for item in files}


def _target_ready(target):
    target = Path(target)
    if target.exists():
        if not target.is_dir():
            raise ValueError('恢复目标不是目录：{0}。'.format(target))
        if any(target.iterdir()):
            raise ValueError('恢复目标不是空目录：{0}。请指定一个不存在或空的新目录。'.format(target))
    return target


def _extract_verified(archive, expected, temp_dir, tick=None):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError('备份包内有重复文件，已拒绝恢复。')
    wanted = set(expected) | {'manifest.json'}
    extra = set(names) - wanted
    missing = wanted - set(names)
    if extra:
        raise ValueError('备份包含清单外的文件：{0}。'.format('、'.join(sorted(extra)[:3])))
    if missing:
        raise ValueError('备份缺少清单内的文件：{0}。'.format('、'.join(sorted(missing)[:3])))
    temp_dir = Path(temp_dir)
    count = 0
    total = 0
    total_files = len(expected)
    if tick:
        tick(0, total_files)
    for name in sorted(wanted - {'manifest.json'}):
        item = expected[name]
        out = temp_dir / PurePosixPath(name).as_posix()
        out.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with archive.open(name) as reader, out.open('wb') as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('文件校验失败：{0}（大小 {1}≠{2} 或 sha256 不匹配），恢复已中止。'.format(
                name, size, item['bytes']))
        count += 1
        total += size
        if tick and (count % 20 == 0 or count == total_files):
            tick(count, total_files)
    return {'files': count, 'bytes': total}


def _rewrite_value(value, target_root, counter):
    if isinstance(value, dict):
        return {key: _rewrite_value(item, target_root, counter) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, target_root, counter) for item in value]
    if isinstance(value, str) and ABS_PATH.match(value):
        match = FOLDER_PATH.match(value)
        if match and match.group(1) in DATA_DIRS:
            rel = match.group(2).replace('\\', '/').lstrip('/')
            counter[0] += 1
            return str(Path(target_root) / match.group(1) / Path(rel))
    return value


def relocate_paths(db_path, target_dir):
    """把 records 里的旧绝对路径改写到新数据目录；返回改写字段数。"""
    con = sqlite3.connect(str(db_path), timeout=20)
    changed = 0
    try:
        rows = con.execute('SELECT kind,id,data FROM records').fetchall()
        with con:
            for kind, key, raw in rows:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    continue
                counter = [0]
                rewritten = _rewrite_value(parsed, target_dir, counter)
                if counter[0]:
                    con.execute('UPDATE records SET data=? WHERE kind=? AND id=?',
                                (json.dumps(rewritten, ensure_ascii=False), kind, key))
                    changed += counter[0]
    finally:
        con.close()
    return changed


def _broken_media_refs(db_path, target_dir):
    con = sqlite3.connect(str(db_path), timeout=20)
    con.row_factory = sqlite3.Row
    broken = []
    try:
        rows = con.execute("SELECT kind,id,data FROM records WHERE kind IN ('asset','artifact')").fetchall()
        for row in rows:
            try:
                data = json.loads(row['data'])
            except ValueError:
                continue
            for key in ('path', 'subtitle_path', 'recipe_path'):
                value = data.get(key)
                if not isinstance(value, str) or not ABS_PATH.match(value):
                    continue
                path = Path(value)
                if not path.is_relative_to(Path(target_dir)):
                    broken.append({'kind': row['kind'], 'id': row['id'], 'field': key, 'reason': 'outside-target'})
                elif not path.is_file():
                    broken.append({'kind': row['kind'], 'id': row['id'], 'field': key, 'reason': 'missing'})
    finally:
        con.close()
    return broken


def restore_full(zip_path, target_dir, app_root=APP_ROOT, current_data=None, tick=None):
    """校验通过后才落盘；失败不写目标目录，返回可读的中文错误。"""
    zip_path = Path(zip_path).expanduser()
    target = Path(target_dir).expanduser().resolve()
    if not zip_path.is_file():
        raise ValueError('找不到全量备份包：{0}。'.format(zip_path.name))
    if zip_path.suffix.lower() != '.zip':
        raise ValueError('全量备份必须是 .zip 文件。')
    if current_data:
        current_data = Path(current_data).expanduser().resolve()
        if target == current_data or target.is_relative_to(current_data):
            raise ValueError('不能把备份恢复到当前正在使用的数据目录或其子目录。')
    target = _target_ready(target)
    schema = current_schema(app_root)
    started = time.monotonic()
    with zipfile.ZipFile(zip_path) as archive:
        manifest = _read_manifest(archive)
        _version_gap(manifest, schema)
        billing_now = _billing_schema_version_const()
        billing_backup = manifest.get('billing_schema_version')
        if billing_backup is not None and billing_now is not None and int(billing_backup) != int(billing_now):
            raise ValueError('备份的计费库 schema 版本 {0}，当前程序 {1}，不兼容，已拒绝恢复。'.format(
                billing_backup, billing_now))
        expected = _check_manifest_files(manifest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix='.restore-tmp-', dir=str(target.parent)))
        moved = False
        try:
            extracted = _extract_verified(archive, expected, temp_dir, tick=tick)
            db = temp_dir / 'workbench.sqlite3'
            if not db.is_file():
                raise ValueError('备份里没有 workbench.sqlite3。')
            # 用备份库的实际核心表结构比对，而不是清单里记录的旧指纹：
            # 旧包（整库指纹）与新包（核心表指纹）都按同一口径校验。
            if core_schema(db)['fingerprint'] != schema['schema_fingerprint']:
                raise ValueError('备份的数据库结构与当前程序不一致（核心表字段不同）；'
                                 '为避免灌入不兼容数据，已拒绝恢复。请用同版本工作台导出后再恢复。')
            rewrites = relocate_paths(db, target)
            after = table_digests(db)
            before = manifest.get('tables') or {}
            if before.get('overall') and before.get('overall') != after['overall']:
                raise ValueError('恢复后记录摘要与清单不一致（清单 {0}，实际 {1}）；目标目录未写入，已回滚。'.format(
                    str(before.get('overall'))[:12], after['overall'][:12]))
            if before.get('total') is not None and int(before['total']) != after['total']:
                raise ValueError('恢复后记录总数不一致（清单 {0}，实际 {1}）；目标目录未写入，已回滚。'.format(
                    before['total'], after['total']))
            if not target.exists():
                temp_dir.rename(target)
            else:
                for child in list(temp_dir.iterdir()):
                    shutil.move(str(child), str(target / child.name))
                temp_dir.rmdir()
            moved = True
            broken = _broken_media_refs(target / 'workbench.sqlite3', target)
            elapsed = round(time.monotonic() - started, 2)
            return {'restored_to': str(target), 'files': extracted['files'], 'bytes': extracted['bytes'],
                    'tables': after, 'path_rewrites': rewrites,
                    'schema_version': manifest['schema_version'], 'app_version': manifest.get('app_version'),
                    'broken_media_refs': broken, 'elapsed_s': elapsed,
                    'source_manifest_created_at': manifest.get('created_at')}
        finally:
            if not moved and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


def restore_in_place(zip_path, data_dir=None, app_root=APP_ROOT, tick=None):
    """用备份包覆盖当前数据目录。

    数据库包只还原 workbench.sqlite3 / billing.sqlite3，不碰媒体目录；
    全量包连媒体目录一起还原。
    校验通过后先把当前数据库快照到 backups/pre-restore-<时间>/（.bak），再替换；
    全量包替换媒体目录前，会把当前媒体整体移入同一个 pre-restore 目录的 media/
    （备份包里没有的新素材不会丢失，可在那里找回）；
    替换阶段任何失败都会把原数据移回，当前数据保持恢复前状态。
    """
    zip_path = Path(zip_path).expanduser()
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime')).expanduser().resolve()
    current_db = data_dir / 'workbench.sqlite3'
    if not zip_path.is_file():
        raise ValueError('找不到备份包：{0}。'.format(zip_path.name))
    if zip_path.suffix.lower() != '.zip':
        raise ValueError('备份包必须是 .zip 文件。')
    if not current_db.is_file():
        raise ValueError('当前数据目录里没有 workbench.sqlite3，无法执行覆盖恢复。')
    if not zipfile.is_zipfile(zip_path):
        raise ValueError('备份文件已损坏或不是有效的 zip 包。')
    schema = current_schema(app_root)
    started = time.monotonic()
    with zipfile.ZipFile(zip_path) as archive:
        manifest = _read_manifest(archive)
        package_type = _package_type(manifest, zip_path)
        _version_gap(manifest, schema)
        billing_now = _billing_schema_version_const()
        billing_backup = manifest.get('billing_schema_version')
        if billing_backup is not None and billing_now is not None and int(billing_backup) != int(billing_now):
            raise ValueError('备份的计费库 schema 版本 {0}，当前程序 {1}，不兼容，已拒绝恢复。'.format(
                billing_backup, billing_now))
        expected = _check_manifest_files(manifest)

        temp_dir = Path(tempfile.mkdtemp(prefix='.restore-inplace-', dir=str(data_dir)))
        pre_dir = None
        media_trash = None
        placed_folders = []
        moved_aside = []
        billing_replaced = False
        try:
            extracted = _extract_verified(archive, expected, temp_dir, tick=tick)
            staged_db = temp_dir / 'workbench.sqlite3'
            if not staged_db.is_file():
                raise ValueError('备份里没有 workbench.sqlite3。')
            # 同 restore_full：按备份库实际核心表结构校验，兼容旧版清单指纹。
            if core_schema(staged_db)['fingerprint'] != schema['schema_fingerprint']:
                raise ValueError('备份的数据库结构与当前程序不一致（核心表字段不同）；'
                                 '为避免灌入不兼容数据，已拒绝恢复。请用同版本工作台导出后再恢复。')
            rewrites = relocate_paths(staged_db, data_dir)
            after = table_digests(staged_db)
            before = manifest.get('tables') or {}
            if before.get('overall') and before.get('overall') != after['overall']:
                raise ValueError('恢复后记录摘要与清单不一致（清单 {0}，实际 {1}）；当前数据未改动。'.format(
                    str(before.get('overall'))[:12], after['overall'][:12]))
            if before.get('total') is not None and int(before['total']) != after['total']:
                raise ValueError('恢复后记录总数不一致（清单 {0}，实际 {1}）；当前数据未改动。'.format(
                    before['total'], after['total']))

            # 自动备份当前数据库：sqlite 快照（.bak），数据库包秒级完成，不复制媒体。
            stamp = _local_stamp(time.time())
            pre_dir = data_dir / 'backups' / ('pre-restore-' + stamp)
            sequence = 2
            while pre_dir.exists():
                pre_dir = data_dir / 'backups' / ('pre-restore-{0}-{1}'.format(stamp, sequence))
                sequence += 1
            pre_dir.mkdir(parents=True)
            _db_snapshot(current_db, pre_dir / 'workbench.sqlite3.bak')
            billing_current = data_dir / 'billing.sqlite3'
            if billing_current.is_file():
                _db_snapshot(billing_current, pre_dir / 'billing.sqlite3.bak')

            # 替换数据库：在单事务里整表覆盖，不替换文件，避免 Windows 文件占用。
            _copy_database(staged_db, current_db)
            staged_billing = temp_dir / 'billing.sqlite3'
            if staged_billing.is_file():
                if billing_current.is_file():
                    _copy_database(staged_billing, billing_current)
                else:
                    shutil.copy2(str(staged_billing), str(billing_current))
                billing_replaced = True

            # 只有全量包才替换媒体目录；数据库包不动 uploads/artifacts/avatars/packages。
            if package_type == 'full':
                # 旧媒体移到恢复前备份目录而不是临时目录：临时目录成功后会被清空，
                # 备份包里没有的素材（如备份之后新导入的）会永久丢失。
                media_trash = pre_dir / 'media'
                for folder in DATA_DIRS:
                    staged_folder = temp_dir / folder
                    current_folder = data_dir / folder
                    if current_folder.exists():
                        media_trash.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(current_folder), str(media_trash / folder))
                        moved_aside.append(folder)
                    if staged_folder.is_dir():
                        shutil.move(str(staged_folder), str(current_folder))
                        placed_folders.append(folder)

            final = table_digests(current_db)
            if final.get('overall') != after.get('overall'):
                raise ValueError('写入后数据库校验不一致，已回滚到恢复前数据。')
            broken = _broken_media_refs(current_db, data_dir) if package_type == 'full' else []
            elapsed = round(time.monotonic() - started, 2)
            return {'restored_to': str(data_dir), 'mode': 'in-place',
                    'package_type': package_type,
                    'files': extracted['files'], 'bytes': extracted['bytes'],
                    'tables': final, 'path_rewrites': rewrites,
                    'pre_restore_backup': str(pre_dir),
                    'pre_restore_media': str(media_trash) if media_trash else None,
                    'broken_media_refs': broken, 'elapsed_s': elapsed,
                    'source_manifest_created_at': manifest.get('created_at'),
                    'schema_version': manifest['schema_version'],
                    'app_version': manifest.get('app_version')}
        except Exception:
            for folder in placed_folders:
                path = data_dir / folder
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
            if media_trash and media_trash.exists():
                for folder in moved_aside:
                    source = media_trash / folder
                    target = data_dir / folder
                    if source.exists():
                        if target.exists():
                            shutil.rmtree(target, ignore_errors=True)
                        shutil.move(str(source), str(target))
            if pre_dir and pre_dir.exists():
                backup = pre_dir / 'workbench.sqlite3.bak'
                if backup.is_file():
                    try:
                        _copy_database(backup, current_db)
                    except Exception:
                        try:
                            shutil.copy2(str(backup), str(current_db))
                        except OSError:
                            pass
                if billing_replaced:
                    backup = pre_dir / 'billing.sqlite3.bak'
                    if backup.is_file():
                        try:
                            _copy_database(backup, data_dir / 'billing.sqlite3')
                        except Exception:
                            try:
                                shutil.copy2(str(backup), str(data_dir / 'billing.sqlite3'))
                            except OSError:
                                pass
            raise
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 数据库快照 / 完整性校验 / 快照恢复
# 能力搬运自切片工坊 services/backup_service.py（VACUUM INTO / integrity_check /
# Online Backup API），已摘掉其业务表绑定，只服务本机 workbench.sqlite3（含 WAL/SHM）。
# ---------------------------------------------------------------------------
SNAPSHOT_PREFIX = 'snapshot-'
KEEP_SNAPSHOTS = 7
CORE_TABLES = ('records', 'imports')
SNAPSHOT_SUFFIX = '.sqlite3'


def _db_dir(data_dir=None):
    return Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime')).expanduser().resolve()


def _is_busy(error):
    text = str(error).lower()
    return 'locked' in text or 'busy' in text


def _vacuum_into(source_db, target, attempts=5):
    """``VACUUM INTO`` 生成单文件一致性快照；busy 时退避重试。"""
    target = Path(target)
    if target.exists():
        target.unlink()
    con = sqlite3.connect(str(source_db), timeout=30)
    try:
        con.execute('PRAGMA busy_timeout=30000')
        for attempt in range(1, attempts + 1):
            try:
                con.execute('VACUUM INTO ?', (str(target),))
                return target
            except sqlite3.Error as exc:
                if target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass
                if attempt < attempts and _is_busy(exc):
                    time.sleep(1.5 * attempt)
                    continue
                raise
    finally:
        con.close()


def _unique_snapshot(data_dir, prefix):
    folder = Path(data_dir) / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / '{0}{1}'.format(prefix, _local_stamp(time.time()))
    target = base.with_name(base.name + SNAPSHOT_SUFFIX)
    sequence = 2
    while target.exists():
        target = base.with_name('{0}-{1}{2}'.format(base.name, sequence, SNAPSHOT_SUFFIX))
        sequence += 1
    return target


def check_integrity(db_path=None, data_dir=None):
    """``PRAGMA integrity_check`` + 关键表存在性；不抛异常，失败转 ``ok=False``。"""
    path = Path(db_path) if db_path is not None else _db_dir(data_dir) / 'workbench.sqlite3'
    started = time.monotonic()
    result = {'path': str(path), 'checked_at': _now_iso(), 'exists': path.is_file(),
              'size_bytes': 0, 'wal_bytes': 0, 'shm_bytes': 0, 'journal_mode': '',
              'ok': False, 'result': '', 'reason': '', 'core_tables': {},
              'elapsed_ms': 0}
    for suffix, key in (('', 'size_bytes'), ('-wal', 'wal_bytes'), ('-shm', 'shm_bytes')):
        side = Path(str(path) + suffix)
        try:
            result[key] = side.stat().st_size if side.is_file() else 0
        except OSError:
            result[key] = 0
    if not path.is_file():
        result['result'] = '文件不存在'
        result['reason'] = '文件不存在：{0}'.format(path)
        result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
        return result

    con = None
    try:
        con = sqlite3.connect(str(path), timeout=30)
        con.execute('PRAGMA busy_timeout=30000')
        # 只读连接：校验绝不能改库
        con.execute('PRAGMA query_only=ON')
        rows = [str(row[0]) for row in con.execute('PRAGMA integrity_check').fetchall()]
        text = 'ok' if rows == ['ok'] else '; '.join(rows[:10])
        names = {str(row[0]) for row in
                 con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [name for name in CORE_TABLES if name not in names]
        counts = {}
        for name in CORE_TABLES:
            if name in names:
                counts[name] = int(con.execute('SELECT COUNT(*) FROM "{0}"'.format(name)).fetchone()[0])
        try:
            result['journal_mode'] = str(con.execute('PRAGMA journal_mode').fetchone()[0])
        except (sqlite3.Error, TypeError):
            result['journal_mode'] = ''
        result['result'] = text
        result['core_tables'] = counts
        result['ok'] = (text == 'ok' and not missing)
        if missing:
            result['reason'] = '缺少关键表：{0}'.format('、'.join(missing))
        elif not result['ok']:
            result['reason'] = 'SQLite 完整性校验未通过：{0}'.format(text)
    except Exception as exc:  # 截断文件常抛 DatabaseError
        result['result'] = '{0}: {1}'.format(type(exc).__name__, exc)
        result['reason'] = '无法完成完整性校验（数据库可能被截断或损坏）：{0}: {1}'.format(
            type(exc).__name__, exc)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    result['elapsed_ms'] = int((time.monotonic() - started) * 1000)
    return result


def create_snapshot(data_dir=None, reason='manual', keep=KEEP_SNAPSHOTS):
    """生成一致性快照（VACUUM INTO）→ 校验 → 只清理本工具命名的旧快照。"""
    data_dir = _db_dir(data_dir)
    db = data_dir / 'workbench.sqlite3'
    if not db.is_file():
        raise ValueError('数据目录里没有 workbench.sqlite3，无法生成快照：{0}。'.format(data_dir))
    started = time.monotonic()
    target = _unique_snapshot(data_dir, SNAPSHOT_PREFIX)
    part = target.with_name(target.name + '.part')
    try:
        _vacuum_into(db, part)
    except sqlite3.Error as exc:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
        raise ValueError('快照生成失败（VACUUM INTO）：{0}。当前库未改动。'.format(exc))
    check = check_integrity(part)
    if not check['ok']:
        try:
            part.unlink()
        except OSError:
            pass
        raise ValueError('快照生成后完整性校验未通过，已删除半成品：{0}。'.format(check['reason']))
    try:
        part.replace(target)
    except OSError as exc:
        try:
            part.unlink()
        except OSError:
            pass
        raise ValueError('快照落盘失败：{0}。'.format(exc))

    removed = []
    snapshots = sorted((data_dir / 'backups').glob(SNAPSHOT_PREFIX + '*' + SNAPSHOT_SUFFIX),
                       key=lambda item: item.stat().st_mtime)
    keep = max(1, int(keep))
    for old in snapshots[:-keep] if len(snapshots) > keep else []:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass

    final = dict(check)
    final['path'] = str(target)
    final['size_bytes'] = target.stat().st_size
    digests = table_digests(target)
    return {'snapshot': target.name, 'path': str(target),
            'size_bytes': final['size_bytes'],
            'created_at': datetime.fromtimestamp(target.stat().st_mtime, timezone.utc).isoformat(timespec='seconds'),
            'reason': reason, 'integrity': final,
            'tables': {'total': digests['total'], 'imports': digests['imports'],
                       'kinds': {kind: item['count'] for kind, item in digests['kinds'].items()}},
            'retention': {'keep': keep, 'deleted': removed},
            'elapsed_s': round(time.monotonic() - started, 2)}


def list_snapshots(data_dir=None):
    data_dir = _db_dir(data_dir)
    folder = data_dir / 'backups'
    now_ts = time.time()
    items = []
    if folder.is_dir():
        for path in sorted(folder.glob(SNAPSHOT_PREFIX + '*' + SNAPSHOT_SUFFIX),
                           key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({'name': path.name, 'path': str(path), 'bytes': stat.st_size,
                          'created_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
                          'age_hours': round(max(0.0, now_ts - stat.st_mtime) / 3600.0, 2)})
    return items


def _resolve_snapshot(ref, data_dir):
    raw = str(ref or '').strip()
    if not raw:
        raise ValueError('请提供快照文件名或完整路径。')
    path = Path(raw)
    if path.is_absolute():
        if not path.is_file():
            raise ValueError('快照不存在：{0}。'.format(raw))
        return path
    if len(path.parts) != 1:
        raise ValueError('快照只允许写文件名或绝对路径：{0}。'.format(raw))
    target = Path(data_dir) / 'backups' / raw
    if not target.is_file():
        raise ValueError('快照不存在：{0}。'.format(raw))
    return target


def _page_restore(snapshot, live_db, attempts=3):
    """Online Backup API：把快照页级写回活库（不替换文件，Windows 下不被句柄挡住）。"""
    last = None
    for attempt in range(1, attempts + 1):
        src = dst = None
        try:
            src = sqlite3.connect(str(snapshot), timeout=30)
            dst = sqlite3.connect(str(live_db), timeout=30)
            dst.execute('PRAGMA busy_timeout=30000')
            src.backup(dst)
            return
        except sqlite3.Error as exc:
            last = exc
            if attempt < attempts and _is_busy(exc):
                time.sleep(2.0 * attempt)
                continue
            raise
        finally:
            for con in (src, dst):
                if con is not None:
                    try:
                        con.close()
                    except Exception:
                        pass
    if last is not None:
        raise last


def _checkpoint(db_path):
    try:
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            con.close()
    except sqlite3.Error:
        pass


def _counts_view(digests):
    return {'total': digests['total'], 'imports': digests['imports'],
            'kinds': {kind: item['count'] for kind, item in digests['kinds'].items()}}


def restore_snapshot(ref, data_dir=None):
    """从快照恢复本机库：校验快照 → 自动快照当前库 → 页级写回 → 校验 → 失败回退。

    - 只动 ``workbench.sqlite3``（WAL 在恢复后 checkpoint 合并），不碰其他库；
    - 恢复前一定先给当前库打一份 ``snapshot-*``（返回值里给路径），失败时用它回退；
    - 恢复后校验：完整性 + 记录总数 + 各 kind 条数 + imports 条数，全部对齐才算成功。
    """
    data_dir = _db_dir(data_dir)
    live = data_dir / 'workbench.sqlite3'
    if not live.is_file():
        raise ValueError('数据目录里没有 workbench.sqlite3，无法恢复：{0}。'.format(data_dir))
    snapshot = _resolve_snapshot(ref, data_dir)
    started = time.monotonic()

    snapshot_check = check_integrity(snapshot)
    if not snapshot_check['ok']:
        raise ValueError('快照未通过完整性校验，已拒绝恢复：{0}。当前库未做任何改动。'.format(
            snapshot_check['reason']))

    expected = table_digests(snapshot)
    before_digests = table_digests(live)
    before_check = check_integrity(live)
    pre = create_snapshot(data_dir, reason='pre-restore')

    try:
        _page_restore(snapshot, live)
        _checkpoint(live)
        after_check = check_integrity(live)
        after = table_digests(live)
        problems = []
        if not after_check['ok']:
            problems.append('恢复后完整性校验未通过：{0}'.format(after_check['reason']))
        if after['total'] != expected['total']:
            problems.append('记录总数不一致（快照 {0}，实际 {1}）'.format(expected['total'], after['total']))
        if after['imports'] != expected['imports']:
            problems.append('imports 条数不一致（快照 {0}，实际 {1}）'.format(expected['imports'], after['imports']))
        expected_counts = {kind: item['count'] for kind, item in expected['kinds'].items()}
        actual_counts = {kind: item['count'] for kind, item in after['kinds'].items()}
        if expected_counts != actual_counts:
            problems.append('各表条数不一致（快照 {0}，实际 {1}）'.format(expected_counts, actual_counts))
        if problems:
            raise ValueError('；'.join(problems))
    except Exception as exc:
        rollback = {'attempted': True, 'ok': False, 'from': pre['path'], 'error': ''}
        try:
            _page_restore(Path(pre['path']), live)
            _checkpoint(live)
            back = table_digests(live)
            rollback['ok'] = (back['total'] == before_digests['total']
                              and {k: v['count'] for k, v in back['kinds'].items()}
                              == {k: v['count'] for k, v in before_digests['kinds'].items()})
            rollback['counts'] = _counts_view(back)
            if not rollback['ok']:
                rollback['error'] = '回退后记录数与恢复前不一致，请用 {0} 手工恢复。'.format(pre['path'])
        except Exception as rollback_exc:
            rollback['error'] = '回退失败：{0}；请用 {1} 手工恢复。'.format(rollback_exc, pre['path'])
        message = '恢复失败：{0}。恢复前已自动快照当前库：{1}。'.format(exc, pre['path'])
        if rollback['ok']:
            message += '已自动回退到恢复前状态（记录数 {0}）。'.format(rollback['counts']['total'])
        else:
            message += '自动回退未通过校验：{0}'.format(rollback['error'])
        error = ValueError(message)
        error.rollback = rollback
        error.pre_restore_backup = pre['path']
        raise error

    return {'restored_from': snapshot.name, 'snapshot_path': str(snapshot),
            'snapshot_integrity': {'ok': snapshot_check['ok'], 'result': snapshot_check['result']},
            'pre_restore_backup': pre['path'],
            'integrity_before': {'ok': before_check['ok'], 'result': before_check['result'],
                                 'wal_bytes': before_check['wal_bytes'],
                                 'shm_bytes': before_check['shm_bytes']},
            'integrity_after': {'ok': after_check['ok'], 'result': after_check['result'],
                                'wal_bytes': after_check['wal_bytes'],
                                'shm_bytes': after_check['shm_bytes']},
            'counts_before': _counts_view(before_digests),
            'counts_in_snapshot': _counts_view(expected),
            'counts_after': _counts_view(after),
            'elapsed_s': round(time.monotonic() - started, 2)}


def _billing_schema_version_const():
    try:
        import billing_db
        return int(billing_db.SCHEMA_VERSION)
    except Exception:
        return None


def status(data_dir=None):
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime'))
    backups = sorted((data_dir / 'backups').glob('auto-*.sqlite3'), key=lambda p: p.stat().st_mtime) \
        if (data_dir / 'backups').is_dir() else []
    exports = sorted([p for name in ('数据库-*.zip', '全量-*.zip')
                      for p in (data_dir / 'exports').glob(name)], key=lambda p: p.stat().st_mtime) \
        if (data_dir / 'exports').is_dir() else []

    def item(path):
        stat = path.stat()
        return {'name': path.name, 'bytes': stat.st_size,
                'modified_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec='seconds'),
                'path': str(path)}

    now_ts = time.time()
    last = backups[-1] if backups else None
    next_due = 0
    if last:
        next_due = max(0, round(24 - (now_ts - last.stat().st_mtime) / 3600, 2))
    disk = shutil.disk_usage(data_dir if data_dir.exists() else APP_ROOT)
    try:
        keep = int(os.getenv('IP_BACKUP_KEEP') or 10)
    except (TypeError, ValueError):
        keep = 10
    return {'auto_backups': [item(p) for p in backups],
            'auto_backup_count': len(backups), 'last_auto_backup': last.name if last else None,
            'next_auto_backup_in_hours': next_due,
            'exports': [_export_item(p) for p in exports],
            'last_export': exports[-1].name if exports else None,
            'last_export_detail': _export_item(exports[-1]) if exports else None,
            'exports_count': len(exports), 'keep': max(1, keep),
            'exports_dir': str(data_dir / 'exports'),
            'backups_dir': str(data_dir / 'backups'),
            'disk': {'free_gb': round(disk.free / 1073741824, 2), 'total_gb': round(disk.total / 1073741824, 2)}}


def _parse_restore(argv):
    import argparse
    parser = argparse.ArgumentParser(description='IP 工作台全量恢复')
    parser.add_argument('zip_path')
    parser.add_argument('--to', required=True, dest='target')
    parser.add_argument('--data-dir', default=None, help='当前正在使用的数据目录（用于阻止自我覆盖）')
    args = parser.parse_args(argv)
    result = restore_full(args.zip_path, args.target, current_data=args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _parse_export(argv):
    import argparse
    parser = argparse.ArgumentParser(description='IP 工作台全量导出')
    parser.add_argument('--data-dir', default=None)
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    result = export_full(args.data_dir, out_dir=args.out, progress=lambda text: print(text, flush=True))
    print(json.dumps({k: v for k, v in result.items() if k != 'tables'}, ensure_ascii=False, indent=2))
    print('表条数：' + json.dumps({k: v['count'] for k, v in result['tables']['kinds'].items()},
                                   ensure_ascii=False))
    return 0


def _snapshot_parser(prog, description):
    import argparse
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument('--data-dir', default=None)
    return parser


def _parse_snapshot(argv):
    parser = _snapshot_parser('backup_restore snapshot', '生成 workbench.sqlite3 一致性快照')
    parser.add_argument('--reason', default='manual')
    parser.add_argument('--keep', type=int, default=KEEP_SNAPSHOTS)
    args = parser.parse_args(argv)
    try:
        info = create_snapshot(args.data_dir, reason=args.reason, keep=args.keep)
    except ValueError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({'ok': True, **info}, ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_verify(argv):
    parser = _snapshot_parser('backup_restore verify', '校验 workbench.sqlite3 完整性（只读）')
    args = parser.parse_args(argv)
    info = check_integrity(data_dir=args.data_dir)
    print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
    return 0 if info['ok'] else 1


def _parse_snapshots(argv):
    parser = _snapshot_parser('backup_restore snapshots', '列出已有快照')
    args = parser.parse_args(argv)
    print(json.dumps(list_snapshots(args.data_dir), ensure_ascii=False, indent=2, default=str))
    return 0


def _parse_restore_snapshot(argv):
    parser = _snapshot_parser('backup_restore restore-snapshot', '从快照恢复 workbench.sqlite3')
    parser.add_argument('snapshot', help='快照文件名（位于 backups/ 下）或绝对路径')
    args = parser.parse_args(argv)
    try:
        info = restore_snapshot(args.snapshot, data_dir=args.data_dir)
    except ValueError as exc:
        payload = {'ok': False, 'error': str(exc),
                   'rollback': getattr(exc, 'rollback', None),
                   'pre_restore_backup': getattr(exc, 'pre_restore_backup', None)}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 1
    print(json.dumps({'ok': True, **info}, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('用法：python -m backup_restore export [--data-dir DIR] [--out DIR]\n'
              '      python -m backup_restore restore 包.zip --to 新目录\n'
              '      python -m backup_restore snapshot [--data-dir DIR] [--keep N]\n'
              '      python -m backup_restore verify [--data-dir DIR]\n'
              '      python -m backup_restore snapshots [--data-dir DIR]\n'
              '      python -m backup_restore restore-snapshot 快照名|绝对路径 [--data-dir DIR]')
        return 2
    command, rest = argv[0], argv[1:]
    if command == 'export':
        return _parse_export(rest)
    if command == 'restore':
        return _parse_restore(rest)
    if command == 'snapshot':
        return _parse_snapshot(rest)
    if command == 'verify':
        return _parse_verify(rest)
    if command == 'snapshots':
        return _parse_snapshots(rest)
    if command == 'restore-snapshot':
        return _parse_restore_snapshot(rest)
    if command == 'auto':
        print(json.dumps(maybe_auto_backup(Path(os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime'))),
                         ensure_ascii=False, indent=2))
        return 0
    print('未知命令：' + command)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
