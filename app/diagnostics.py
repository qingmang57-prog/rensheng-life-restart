"""一键诊断包：系统/版本/数据量/依赖自检/最近错误日志。

硬口径：只输出计数、版本、相对路径与布尔值；不打包任何媒体原文件、稿件正文、
API key、Cookie 或本机绝对路径。生成 <数据目录>/diagnostics/诊断包-YYYYMMDD-HHMM.zip。
"""
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
BUNDLE_ROOT = APP_ROOT.parent
MAX_ERRORS = 200
ABS_PATH = re.compile(r'(?:[A-Za-z]:[\\/]|\\\\)[^\s"\'<>]*')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _clean(text, data_dir=None):
    """诊断包统一脱敏：密钥、Cookie、本机绝对路径。"""
    import applog
    value = applog.scrub(str(text))
    masks = [(str(data_dir), '<数据目录>')] if data_dir else []
    masks.append((str(BUNDLE_ROOT), '<程序目录>'))
    masks.append((str(APP_ROOT), '<程序目录>/app'))
    home = str(Path.home())
    if home:
        masks.append((home, '<用户目录>'))
    for raw, mask in masks:
        if raw:
            value = value.replace(raw, mask)
    value = ABS_PATH.sub('<绝对路径>', value)
    return value


def _safe_path(path, app_root=APP_ROOT):
    if not path or str(path) in ('', '.'):
        return ''
    path = Path(path)
    try:
        return PurePosixPath(path.resolve().relative_to(Path(app_root).resolve().parent).as_posix()).as_posix()
    except ValueError:
        return '<外部>/{0}'.format(path.name or 'unknown')


def _run_version(command, timeout=8):
    if not command:
        return None
    try:
        flags = 0x08000000 if os.name == 'nt' else 0
        out = subprocess.run(command, capture_output=True, timeout=timeout, creationflags=flags)
        raw = (out.stdout or out.stderr).decode('utf-8', 'replace').strip().splitlines()
        return raw[0][:200] if raw else None
    except Exception:
        return None


def _ffmpeg_path(app_root=APP_ROOT):
    env = os.getenv('IP_FFMPEG')
    if env and Path(env).is_file():
        return Path(env)
    candidate = Path(app_root).parent / 'tools' / 'ffmpeg' / 'bin' / 'ffmpeg.exe'
    if candidate.is_file():
        return candidate
    return Path(shutil.which('ffmpeg') or '')


def _node_path(app_root=APP_ROOT):
    env = os.getenv('IP_NODE')
    if env and Path(env).is_file():
        return Path(env)
    candidate = Path(app_root).parent / 'tools' / 'node' / 'bin' / 'node.exe'
    return candidate if candidate.is_file() else Path(shutil.which('node') or '')


def _memory():
    try:
        if os.name == 'nt':
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            status = MEMORYSTATUSEX()
            status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return {'total_mb': round(status.ullTotalPhys / 1048576),
                    'available_mb': round(status.ullAvailPhys / 1048576)}
    except Exception:
        pass
    return {'total_mb': None, 'available_mb': None}


def system_info(data_dir=None, app_root=APP_ROOT):
    data_dir = Path(data_dir or (APP_ROOT / '.runtime'))
    disk = shutil.disk_usage(data_dir if data_dir.exists() else app_root)
    ffmpeg = _ffmpeg_path(app_root)
    node = _node_path(app_root)
    return {'os': platform.system(), 'os_release': platform.release(), 'os_version': platform.version(),
            'arch': platform.machine(), 'cpu_count': os.cpu_count(),
            'python': platform.python_version(), 'python_implementation': platform.python_implementation(),
            'memory': _memory(),
            'disk': {'free_gb': round(disk.free / 1073741824, 2), 'total_gb': round(disk.total / 1073741824, 2)},
            'node_version': _run_version([str(node), '--version']) if node.is_file() else None,
            'ffmpeg_version': (_run_version([str(ffmpeg), '-version']) if ffmpeg.is_file() else None),
            'timezone': time.strftime('%Z')}


def _db_counts(db_path):
    path = Path(db_path)
    if not path.is_file():
        return {'available': False, 'kinds': {}, 'total': 0, 'imports': 0, 'bytes': 0}
    con = sqlite3.connect('file:{0}?mode=ro'.format(str(path).replace('\\', '/')), uri=True, timeout=20)
    try:
        rows = con.execute('SELECT kind,COUNT(*) FROM records GROUP BY kind ORDER BY kind').fetchall()
        imports = con.execute('SELECT COUNT(*) FROM imports').fetchone()[0]
    finally:
        con.close()
    return {'available': True, 'kinds': {kind: count for kind, count in rows},
            'total': sum(count for _, count in rows), 'imports': imports, 'bytes': path.stat().st_size}


def _folder_stats(data_dir):
    stats = {}
    for folder in ('uploads', 'artifacts', 'avatars', 'packages', 'backups'):
        root = Path(data_dir) / folder
        files = [p for p in root.rglob('*') if p.is_file()] if root.is_dir() else []
        stats[folder] = {'files': len(files), 'bytes': sum(p.stat().st_size for p in files)}
    return stats


def _schema(data_dir, app_root=APP_ROOT):
    import backup_restore
    db = Path(data_dir) / 'workbench.sqlite3'
    version = backup_restore.load_version(app_root)
    if not db.is_file():
        return {'schema_version': version['schema_version'],
                'schema_fingerprint': backup_restore.current_schema(app_root)['schema_fingerprint']}
    info = backup_restore.db_schema(db)
    return {'schema_version': version['schema_version'], 'schema_fingerprint': info['fingerprint'],
            'tables': info['tables']}


def dependencies(app_root=APP_ROOT):
    """直接复用 smart_packaging.runtime() 的 8 项自检；路径改成相对路径/外部文件名。"""
    try:
        import smart_packaging
        runtime = smart_packaging.runtime()
        kit = smart_packaging.KIT
    except Exception as exc:
        return {'ready': False, 'missing': ['自检模块加载失败'], 'error': _clean(exc),
                'checks': [], 'count': 0}
    checks = [
        ('包装引擎', Path(kit) / 'engine' / 'adapter.py'),
        ('模板字体', Path(kit) / 'engine' / 'assets' / 'fonts' / 'NotoSansSC.ttf'),
        ('Python', Path(runtime.get('python') or '')),
        ('Node', Path(runtime.get('node') or '')),
        ('本地浏览器', Path(runtime.get('chrome') or '')),
        ('Playwright', Path(kit) / 'node_modules' / 'playwright-core' / 'package.json'),
        ('FFmpeg', Path(_ffmpeg_path(app_root) or '')),
        ('FFprobe', Path(_ffmpeg_path(app_root)).parent / 'ffprobe.exe' if _ffmpeg_path(app_root) else Path('')),
    ]
    items = [{'name': name, 'ok': bool(path and path.is_file()), 'where': _safe_path(path, app_root) if path else ''}
             for name, path in checks]
    return {'ready': bool(runtime.get('ready')), 'missing': list(runtime.get('missing') or []),
            'checks': items, 'count': len(items)}


def _provider_flags():
    try:
        import providers
        values = providers.available()
        return {'llm': bool(values.get('llm')), 'asr': bool(values.get('asr')),
                'asr_mode': values.get('asr_mode'), 'resolver': bool(values.get('resolver'))}
    except Exception:
        return {'llm': False, 'asr': False, 'asr_mode': 'unknown', 'resolver': False}


def _startup_status(bundle_root=BUNDLE_ROOT):
    """启动侧证据：launcher/backend 日志尾、端口判定、核心文件在不在。
    客户「打不开」类问题（保留端口段/杀毒隔离/解压不完整）的痕迹在 launcher 侧日志，
    app.log 抓不到；诊断包不补这一段就瞎一半。"""
    root = Path(bundle_root)
    logs = {}
    for base in (root / '.runtime' / 'launcher' / 'rensheng',
                 root / 'app' / '.runtime' / 'launcher' / 'rensheng'):
        for name in ('launcher.log', 'launcher-error.log', 'backend.log'):
            if logs.get(name):
                continue
            path = base / name
            if path.is_file():
                try:
                    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
                    logs[name] = [_clean(line) for line in lines[-60:]]
                except OSError:
                    logs[name] = []
            else:
                logs.setdefault(name, [])
    port = {}
    launcher_dir = root / 'tools' / 'launcher'
    # port_doctor 在函数体内 import 同目录 port_logic；调用结束前目录必须留在 sys.path。
    sys.path.insert(0, str(launcher_dir))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('ip_port_doctor', launcher_dir / 'port_doctor.py')
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        cfg = json.loads((launcher_dir / 'configs' / 'rensheng.json').read_text(encoding='utf-8'))
        default = int(cfg.get('port') or 8791)
        segs_ok, segs = doctor.excluded_segments()
        ok, err = doctor.bindable(default)
        port = {'default_port': default, 'excluded_segments': segs, 'netsh_ok': segs_ok,
                'default_port_bindable': ok, 'bind_error': err,
                'reserved': any(a <= default <= b for a, b in segs)}
    except Exception as exc:
        port = {'error': _clean(exc)}
    finally:
        try:
            sys.path.remove(str(launcher_dir))
        except ValueError:
            pass
    files = []
    for rel in ('app/browser_fetch.py', 'app/bench_collect.py', 'app/video_intake.py', 'app/video_link.py',
                'app/douyin_session.py', 'app/local_asr.py', 'app/wash_service.py', 'app/creation_service.py',
                'app/backup_restore.py', 'tools/launcher/launcher.py', 'tools/launcher/port_doctor.py',
                'tools/avatar-model/models/u2net/u2net.onnx', 'app/.runtime/browser-env/Scripts/python.exe'):
        path = root / rel
        present = path.is_file()
        files.append({'path': rel, 'present': present, 'bytes': path.stat().st_size if present else 0})
    missing = [item['path'] for item in files if not item['present']]
    return {'launcher_logs': logs, 'port': port, 'core_files': files, 'missing': missing,
            'hint': ('缺文件多为杀毒隔离或解压不完整：到杀毒软件隔离区恢复并加信任，'
                     '或重新解压完整包并核对 SHA256SUMS.txt。') if missing else ''}


def _recent_errors(data_dir, limit=MAX_ERRORS):
    folder = Path(data_dir) / 'logs'
    lines = []
    for name in ('app.log', 'app.log.1', 'app.log.2'):
        path = folder / name
        if path.is_file():
            try:
                lines.extend(path.read_text(encoding='utf-8', errors='replace').splitlines())
            except OSError:
                continue
    items = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get('level') != 'error':
            continue
        items.append({key: _clean(value, data_dir) if isinstance(value, str) else value
                      for key, value in record.items()})
        if len(items) >= limit:
            break
    items.reverse()
    return items


def _diagnostic_notes():
    return ('IP 工作台诊断包\n'
            '================\n'
            '这是客服排障用的最小信息包，故意不包含：\n'
            '- 任何 API key、Cookie、授权码\n'
            '- 任何视频/音频/照片原文件\n'
            '- 任何稿件、文稿、逐字稿正文\n'
            '- 本机绝对路径（只保留相对路径）\n\n'
            '内容：system.json（系统与磁盘）、app.json（版本/数据条数/备份状态）、'
            'deps.json（8 项依赖自检）、startup.json（启动器日志尾/端口判定/核心文件在不在）、'
            'logs/recent-errors.jsonl（最近错误）。\n'
            '可以直接把这个 zip 发给技术支持。\n')


def _unique_target(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M')
    target = folder / '诊断包-{0}.zip'.format(stamp)
    index = 2
    while target.exists():
        target = folder / '诊断包-{0}-{1}.zip'.format(stamp, index)
        index += 1
    return target


def build_package(data_dir=None, app_root=APP_ROOT, out_dir=None, error_limit=MAX_ERRORS, progress=None):
    data_dir = Path(data_dir or os.getenv('IP_DATA_DIR') or (APP_ROOT / '.runtime')).resolve()
    out = Path(out_dir) if out_dir else data_dir / 'diagnostics'
    target = _unique_target(out)
    temporary = out / ('.diagnostics-{0}.tmp'.format(os.getpid()))
    import backup_restore
    db = data_dir / 'workbench.sqlite3'
    counts = _db_counts(db)
    errors = _recent_errors(data_dir, error_limit)
    package_files = []
    try:
        with tempfile.TemporaryDirectory(prefix='ip-diag-', dir=str(out)) as tmp:
            tmp = Path(tmp)
            system = system_info(data_dir, app_root)
            app = {'generated_at': _now(),
                   'app_version': backup_restore.load_version(app_root)['app_version'],
                   'schema': _schema(data_dir, app_root),
                   'data': {'database': {'present': counts['available'], 'bytes': counts['bytes'],
                                          'records_total': counts['total'], 'kinds': counts['kinds'],
                                          'imports': counts['imports']},
                           'folders': _folder_stats(data_dir),
                           'backups': backup_restore.status(data_dir)},
                   'process': {'pid': os.getpid(), 'python_executable': '<程序目录>/tools/python/python.exe'
                               if Path(sys.executable).is_relative_to(BUNDLE_ROOT) else '<外部Python>'},
                   'redaction': {'contains_media': False, 'contains_scripts': False,
                                  'contains_secrets': False, 'paths': 'relative-only'},
                   'providers': _provider_flags()}
            payloads = {
                'system.json': system,
                'app.json': app,
                'deps.json': dependencies(app_root),
                'startup.json': _startup_status(app_root.parent),
            }
            if progress:
                progress('收集系统与数据统计')
            for name, payload in payloads.items():
                raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
                (tmp / name).write_bytes(raw)
            logs_dir = tmp / 'logs'
            logs_dir.mkdir()
            log_lines = '\n'.join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in errors)
            log_bytes = (log_lines + ('\n' if log_lines else '')).encode('utf-8')
            (logs_dir / 'recent-errors.jsonl').write_bytes(log_bytes)
            (tmp / 'README.txt').write_text(_diagnostic_notes(), encoding='utf-8')
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(tmp.rglob('*')):
                    if not path.is_file():
                        continue
                    name = path.relative_to(tmp).as_posix()
                    stat = path.stat()
                    info = zipfile.ZipInfo(name, date_time=time.localtime(stat.st_mtime)[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o600 << 16
                    archive.writestr(info, path.read_bytes())
                    package_files.append({'path': name, 'bytes': stat.st_size})
        temporary.replace(target)
        return {'path': str(target), 'filename': target.name, 'bytes': target.stat().st_size,
                'files': package_files, 'error_count': len(errors), 'data_total': counts['total'],
                'app_version': app['app_version'], 'generated_at': app['generated_at']}
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='生成 IP 工作台诊断包')
    parser.add_argument('--data-dir', default=None, help='默认 <app>/.runtime 或 IP_DATA_DIR')
    parser.add_argument('--out', default=None, help='输出目录，默认 <数据目录>/diagnostics')
    parser.add_argument('--errors', type=int, default=MAX_ERRORS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    result = build_package(args.data_dir, out_dir=args.out, error_limit=args.errors,
                           progress=lambda text: print(text, flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
