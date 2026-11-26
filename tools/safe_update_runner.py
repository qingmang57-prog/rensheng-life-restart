# -*- coding: utf-8 -*-
"""包外安全升级引导器（t204）。

本文件不依赖被升级包内的 updater。使用方式：
  python safe_update_runner.py --root <包根> --zip <补丁.zip>
  python safe_update_runner.py --root <包根> --recover

流程：
1. 先把补丁完整解到 <包根>/.safe-update/stage-*/，逐文件校验 sha256；
2. 只有 stage 全部落齐且 hash 通过，才建立 journal 并切换入口文件；
3. 切换前备份数据库与旧程序文件；中断后由本 runner（而不是包内旧 updater）恢复；
4. 自检通过才提交。入口文件（version.json / release-manifest / update.bat）最后切换。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
try:
    import upgrade_journal as journal
except Exception as exc:  # pragma: no cover
    print('无法加载 upgrade_journal.py：%s' % exc, file=sys.stderr)
    raise SystemExit(4)

DB_NAME = 'workbench.sqlite3'
ASSET_DIRS = ('uploads', 'artifacts', 'avatars', 'packages')
ENTRY_LAST = {
    'app/web/version.json',
    'tools/release-manifest.json',
    'update.bat',
    'start.bat',
}


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def safe_rel(rel):
    if not isinstance(rel, str) or not rel or rel.startswith('/') or '\\' in rel:
        return False
    return all(part not in ('', '.', '..') for part in rel.split('/'))


def allowed_rel(rel):
    return safe_rel(rel) and (rel.startswith('app/') or rel.startswith('tools/')
                              or rel in ('start.bat', 'update.bat'))


def _read_env_file(path):
    values = {}
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


POINTER_SCHEMA = 1
PRODUCT = 'rensheng'


def _local_root():
    custom = (os.getenv('IP_STABLE_DATA_ROOT') or '').strip()
    if custom:
        return Path(custom).expanduser().resolve()
    base = (os.getenv('LOCALAPPDATA') or '').strip()
    if base:
        return Path(base) / 'IPWorkbench'
    return Path.home() / '.ipworkbench'


def read_data_pointer():
    path = _local_root() / 'data-root.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get('schema') != POINTER_SCHEMA:
        return {}
    if value.get('product') != PRODUCT:
        return {}
    return value


def resolve_data_dir(root):
    root = Path(root).resolve()
    raw = os.environ.get('IP_DATA_DIR', '').strip()
    source = 'env'
    if not raw and os.getenv('IP_SKIP_LOCAL_CONFIG') != '1':
        for candidate, label in ((root / '.env.local', '.env.local'),
                                 (root / 'app' / '.env.local', 'app/.env.local')):
            values = _read_env_file(candidate)
            if values.get('IP_DATA_DIR', '').strip():
                raw = values['IP_DATA_DIR'].strip()
                source = label
                break
    if not raw:
        pointer = read_data_pointer()
        if pointer.get('path'):
            pointed = Path(pointer['path']).expanduser()
            if not pointed.is_absolute():
                pointed = (root / pointed).resolve()
            if pointed.is_dir():
                return {'path': pointed.resolve(), 'source': 'pointer', 'configured': str(pointed),
                        'configured_missing': False, 'fallback': None, 'pointer': str(pointed)}
            raise ValueError('本机数据指针指向的目录不存在：%s；拒绝升级，绝不回落到空库' % pointed)
        # 2026-11-25 资料外置：默认位置不再各自写死「包内 app/.runtime」，改引 tools/launch_root.py 的同一处规则。
        import launch_root as _launch_root
        raw = str(_launch_root.resolve_data_dir(root)['path'])
        source = 'default'
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    configured = bool(raw) and source != 'default'
    missing = not path.is_dir()
    if missing and configured:
        # 显式配置不存在时绝不静默换库：直接拒绝，保留用户配置路径供修复。
        raise ValueError('配置的数据目录不存在：%s（来源 %s）；拒绝升级，绝不回落到空库' % (path, source))
    if missing:
        path.mkdir(parents=True, exist_ok=True)
    return {'path': path, 'source': source, 'configured': str(path),
            'configured_missing': False, 'fallback': None}


def table_counts(con):
    rows = con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'").fetchall()
    return {name: con.execute('select count(*) from "%s"' % name.replace('"', '""')).fetchone()[0]
            for (name,) in rows}


def snapshot_db(source, target, sidecar=True):
    source = Path(source).resolve()
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    uri = source.as_uri() + '?mode=ro'
    src = sqlite3.connect(uri, uri=True, timeout=30)
    src.isolation_level = None
    dst = sqlite3.connect(str(target))
    try:
        src.execute('BEGIN')
        before = table_counts(src)
        src.backup(dst)
        after = table_counts(dst)
        integrity = dst.execute('PRAGMA integrity_check').fetchone()[0]
        if before != after:
            raise RuntimeError('快照表行数不一致：%s -> %s' % (before, after))
        if integrity != 'ok':
            raise RuntimeError('快照完整性检查失败：%s' % integrity)
        return {'source': str(source), 'snapshot': str(target), 'method': 'sqlite_backup_api',
                'integrity': integrity, 'tables': len(after), 'rows': sum(after.values()),
                'table_counts': after, 'sha256': sha256_file(target)}
    finally:
        try:
            src.execute('COMMIT')
        except Exception:
            pass
        src.close()
        dst.close()


def backup_data(data_dir, backup):
    data_dir = Path(data_dir)
    backup = Path(backup)
    backup.mkdir(parents=True, exist_ok=True)
    info = {'data_dir': str(data_dir), 'db': None, 'assets': {}}
    db = data_dir / DB_NAME
    if db.is_file():
        info['db'] = snapshot_db(db, backup / 'data' / DB_NAME)
    for name in ASSET_DIRS:
        base = data_dir / name
        if not base.is_dir():
            continue
        inventory = []
        for path in sorted(base.rglob('*')):
            if path.is_file():
                inventory.append({'path': path.relative_to(data_dir).as_posix(),
                                  'size': path.stat().st_size, 'sha256': sha256_file(path)})
        info['assets'][name] = inventory
    return info


def choose_port():
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_bootstrap(port, timeout=120):
    deadline = time.time() + timeout
    url = 'http://127.0.0.1:%s/api/bootstrap' % port
    last = ''
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                body = r.read(200000)
                if r.status == 200:
                    data = json.loads(body.decode('utf-8'))
                    if isinstance(data, dict):
                        return True, 'bootstrap 200，profile/capabilities 就绪'
                    last = 'bootstrap 返回非对象'
        except Exception as exc:
            last = str(exc)
        time.sleep(0.4)
    return False, 'bootstrap 超时：%s' % last


def selfcheck(root, data_dir, stage):
    root = Path(root)
    check = Path(stage) / 'selfcheck-data'
    if check.exists():
        shutil.rmtree(check)
    check.mkdir(parents=True)
    db = Path(data_dir) / DB_NAME
    if db.is_file():
        snapshot_db(db, check / DB_NAME)
    for name in ASSET_DIRS:
        src = Path(data_dir) / name
        if src.is_dir():
            shutil.copytree(src, check / name)
    port = choose_port()
    env = dict(os.environ)
    env['IP_DATA_DIR'] = str(check)
    env['IP_SKIP_LOCAL_CONFIG'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    log_path = Path(stage) / 'selfcheck.log'
    with open(log_path, 'wb') as log:
        proc = subprocess.Popen([sys.executable, str(root / 'app' / 'app.py'),
                                 '--port', str(port), '--host', '127.0.0.1'],
                                cwd=str(root), env=env, stdout=log,
                                stderr=subprocess.STDOUT)
    try:
        ok, detail = wait_bootstrap(port, 120)
        if not ok:
            try:
                tail = log_path.read_text(encoding='utf-8', errors='replace')[-800:]
            except OSError:
                tail = ''
            return False, detail + ('；日志：' + tail if tail else '')
        return True, detail
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def parse_bundle(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        if 'manifest.json' not in names or 'tools/release-manifest.json' not in names:
            raise ValueError('补丁缺少 manifest.json 或 tools/release-manifest.json')
        manifest = json.loads(z.read('manifest.json').decode('utf-8'))
        files = manifest.get('files')
        removed = manifest.get('removed') or []
        if not isinstance(files, list) or not isinstance(removed, list):
            raise ValueError('manifest 文件清单格式不对')
        for item in files:
            if not isinstance(item, dict) or not allowed_rel(item.get('path')):
                raise ValueError('补丁包含不允许的文件路径')
        for rel in removed:
            if not allowed_rel(rel):
                raise ValueError('删除清单包含不允许的路径')
        expected = {'manifest.json', 'tools/release-manifest.json'} | {x['path'] for x in files}
        extra = names - expected
        missing = expected - names
        if extra:
            raise ValueError('补丁包里有清单外文件：%s' % ', '.join(sorted(extra)))
        if missing:
            raise ValueError('补丁包缺文件：%s' % ', '.join(sorted(missing)))
        manifest_bytes = z.read('tools/release-manifest.json')
        return manifest, files, removed, manifest_bytes


def stage_bundle(root, zip_path):
    root = Path(root).resolve()
    manifest, files, removed, manifest_bytes = parse_bundle(zip_path)
    version = str(manifest.get('version') or '')
    if not version:
        raise ValueError('manifest 缺少 version')
    safe_update = root / '.safe-update'
    safe_update.mkdir(exist_ok=True)
    stage = safe_update / ('stage-%s-%s' % (version, uuid.uuid4().hex[:8]))
    stage.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        for item in files:
            rel = item['path']
            raw = z.read(rel)
            digest = sha256_bytes(raw)
            if digest != item.get('sha256'):
                raise ValueError('%s sha256 与清单不符' % rel)
            if item.get('size') is not None and len(raw) != int(item['size']):
                raise ValueError('%s 大小与清单不符' % rel)
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
    meta = {'version': version, 'manifest': manifest,
            'files': files, 'removed': removed,
            'manifest_sha256': sha256_bytes(manifest_bytes),
            'staged_at': time.strftime('%Y-%m-%dT%H:%M:%S')}
    (stage / 'stage.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return stage, manifest, files, removed, manifest_bytes


GUARD_MARK = 'REM safe-update-guard'
GUARD_SOURCE = '''# -*- coding: utf-8 -*-
"""最小启动守卫：只依赖标准库，不导入被升级包里的新模块。

升级事务未完成、journal 损坏或 boot_state 为 broken/upgrading 时拒绝启动。
"""
import argparse
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
journal = root / '.upgrade-journal' / 'journal.json'
boot = root / '.upgrade-journal' / 'boot_state.json'
if journal.exists():
    try:
        data = json.loads(journal.read_text(encoding='utf-8'))
        status = str(data.get('status') or '')
    except Exception as exc:
        print('升级 journal 无法读取，拒绝启动：%s' % exc, file=sys.stderr)
        sys.exit(1)
    if status not in ('committed', 'rolled_back'):
        print('升级未完成（%s），拒绝启动；请先运行安全更新器 --recover。' % status, file=sys.stderr)
        sys.exit(1)
if boot.exists():
    try:
        state = str((json.loads(boot.read_text(encoding='utf-8')) or {}).get('state') or '')
    except Exception as exc:
        print('启动状态无法读取，拒绝启动：%s' % exc, file=sys.stderr)
        sys.exit(1)
    if state in ('broken', 'upgrading'):
        print('启动门禁为 %s，拒绝启动；请先运行安全更新器 --recover。' % state, file=sys.stderr)
        sys.exit(1)
sys.exit(0)
'''


def install_guard(root):
    """先把最小启动守卫完整落盘（原子），再动入口。"""
    root = Path(root)
    target = root / 'tools' / 'safe_boot_guard.py'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.safe-new')
    temporary.write_text(GUARD_SOURCE, encoding='utf-8', newline='\n')
    temporary.replace(target)


def wire_start(root):
    """入口切换：start.bat 先调用守卫；守卫只依赖已完整的 safe_boot_guard.py。"""
    root = Path(root)
    target = root / 'start.bat'
    if not target.is_file():
        raise RuntimeError('缺少 start.bat，无法安装启动守卫')
    raw = target.read_bytes()
    mark = GUARD_MARK.encode('ascii')
    if mark in raw:
        # v1 守卫把缺文件/未完成都压成 exit 1；改为原样传递明确退出码。
        old = b'if errorlevel 1 exit /b 1\r\n'
        new = b'if errorlevel 1 exit /b %errorlevel%\r\n'
        if old in raw:
            temporary = target.with_name('start.bat.safe-new')
            temporary.write_bytes(raw.replace(old, new, 1))
            temporary.replace(target)
        return
    prefix = ('@echo off\r\n' + GUARD_MARK + '\r\n'
              '"%~dp0tools\\python\\python.exe" "%~dp0tools\\safe_boot_guard.py" --check\r\n'
              'if errorlevel 1 exit /b %errorlevel%\r\n').encode('ascii')
    temporary = target.with_name('start.bat.safe-new')
    temporary.write_bytes(prefix + raw)
    temporary.replace(target)


def entry_order(rel):
    return (1 if rel in ENTRY_LAST else 0, 0 if rel == 'app/web/version.json' else 1, rel)


def apply_stage(root, stage, manifest, files, removed, manifest_bytes):
    root = Path(root).resolve()
    current = read_json(root / 'tools' / 'release-manifest.json', {}) or {}
    current_version = str(current.get('version') or '')
    version = str(manifest.get('version') or '')
    if current_version == version:
        target_manifest = root / 'tools' / 'release-manifest.json'
        same = target_manifest.is_file() and sha256_file(target_manifest) == sha256_bytes(manifest_bytes)
        if same:
            for item in files:
                target = root / item['path']
                if not target.is_file() or sha256_file(target) != item.get('sha256'):
                    same = False
                    break
        if same:
            for rel in removed:
                if (root / rel).exists():
                    same = False
                    break
        if same:
            return {'ok': True, 'already': True, 'version': version,
                    'detail': '目标文件与清单已全部一致，无需重复安装'}
    base_version = str(manifest.get('base_version') or '')
    if base_version and base_version != current_version:
        raise ValueError('补丁基线是 %s，当前包基线是 %s' % (base_version, current_version))
    data_status = resolve_data_dir(root)
    backup = data_status['path'] / 'backups' / ('safe-%s-%s' % (version, time.strftime('%Y%m%d-%H%M%S')))
    entries = []
    base_files = current.get('files') or {}
    for item in files:
        rel = item['path']
        target = root / rel
        old_sha = sha256_file(target) if target.is_file() else None
        if old_sha and base_files.get(rel) and old_sha != base_files.get(rel):
            raise ValueError('%s 与基线不一致，可能被本地改过，拒绝覆盖' % rel)
        entries.append({'path': rel, 'action': 'replace' if target.is_file() else 'add',
                        'old_sha256': old_sha, 'new_sha256': item.get('sha256'),
                        'backup_rel': ('files/%s' % rel) if target.is_file() else None})
    for rel in removed:
        target = root / rel
        old_sha = sha256_file(target) if target.is_file() else None
        if old_sha and base_files.get(rel) and old_sha != base_files.get(rel):
            raise ValueError('%s 与基线不一致，拒绝删除' % rel)
        entries.append({'path': rel, 'action': 'delete', 'old_sha256': old_sha,
                        'new_sha256': None, 'backup_rel': ('files/%s' % rel) if target.is_file() else None})
    entries.sort(key=lambda x: entry_order(x['path']))
    # 先装最小守卫与入口，再开始事务；守卫不依赖任何尚未切换的新模块。
    install_guard(root)
    wire_start(root)
    manifest_entry = {'path': 'tools/release-manifest.json',
                      'old_sha256': sha256_file(root / 'tools' / 'release-manifest.json')
                      if (root / 'tools' / 'release-manifest.json').is_file() else None,
                      'new_sha256': sha256_bytes(manifest_bytes),
                      'backup_rel': 'files/tools/release-manifest.json'}
    state = journal.begin(root, current_version, version, data_status, str(backup), entries, manifest_entry)
    try:
        journal.phase(state, 'preparing', 'stage 已完整落齐，开始备份')
        data_info = backup_data(data_status['path'], backup)
        if manifest_entry.get('old_sha256'):
            src_manifest = root / 'tools' / 'release-manifest.json'
            journal.backup_file(state, root, 'tools/release-manifest.json', src_manifest,
                                manifest_entry['backup_rel'])
            if sha256_file(backup / manifest_entry['backup_rel']) != manifest_entry['old_sha256']:
                raise RuntimeError('备份校验失败：tools/release-manifest.json')
        records = []
        for entry in entries:
            rel = entry['path']
            if entry['old_sha256']:
                src = root / rel
                journal.backup_file(state, root, rel, src, entry['backup_rel'])
                written = backup / entry['backup_rel']
                if sha256_file(written) != entry['old_sha256']:
                    raise RuntimeError('备份校验失败：%s' % rel)
            journal.mark_backup(state, rel, entry['old_sha256'])
            records.append({'path': rel, 'existed': bool(entry['old_sha256']),
                            'sha256': entry['old_sha256']})
        (backup / 'backup.json').write_text(json.dumps(
            {'from': current_version, 'to': version, 'files': records, 'data': data_info},
            ensure_ascii=False, indent=2), encoding='utf-8')
        journal.phase(state, 'backed_up', '数据与旧程序文件已备份')
        journal.phase(state, 'replacing', '从已校验 stage 切换文件')
        for index, entry in enumerate(entries, 1):
            rel = entry['path']
            target = root / rel
            if entry['action'] == 'delete':
                if target.exists():
                    target.unlink()
            else:
                raw = (stage / rel).read_bytes()
                if sha256_bytes(raw) != entry['new_sha256']:
                    raise RuntimeError('stage 文件 hash 不符：%s' % rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + '.safe-patching')
                temporary.write_bytes(raw)
                if sha256_file(temporary) != entry['new_sha256']:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError('临时文件 hash 不符：%s' % rel)
                temporary.replace(target)
            journal.progress(state, rel, index)
        manifest_target = root / 'tools' / 'release-manifest.json'
        temp = manifest_target.with_name('release-manifest.json.safe-patching')
        temp.write_bytes(manifest_bytes)
        temp.replace(manifest_target)
        journal.phase(state, 'verifying', '入口切换完成，开始自检')
        ok, detail = selfcheck(root, data_status['path'], stage)
        if not ok:
            journal.recover_locked(state, root)
            raise RuntimeError('自检未通过，已回滚：%s' % detail)
        journal.phase(state, 'committed', '自检通过')
        return {'ok': True, 'version': version, 'backup': str(backup),
                'detail': detail, 'tables': (data_info.get('db') or {}).get('tables'),
                'rows': (data_info.get('db') or {}).get('rows')}
    except Exception as exc:
        try:
            result = journal.recover_locked(state, root)
            if result.get('ok'):
                return {'ok': False, 'rolled_back': True, 'error': str(exc),
                        'backup': str(backup)}
        except Exception as inner:
            return {'ok': False, 'rolled_back': False, 'error': '%s；恢复失败：%s' % (exc, inner),
                    'backup': str(backup)}
        return {'ok': False, 'rolled_back': False, 'error': str(exc), 'backup': str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='包外安全升级引导器（stage 完整后切换）')
    parser.add_argument('--root', required=True)
    parser.add_argument('--zip')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--keep-stage', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.recover:
        with journal.locked(root):
            result = journal.recover_locked_root(root)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if result.get('ok') else 2
    if not args.zip:
        print('缺少 --zip（或使用 --recover）', file=sys.stderr)
        return 2
    with journal.locked(root):
        active = journal.read(root)
        if active and active.get('status') not in ('committed', 'rolled_back'):
            print('[安全更新] 发现未完成事务（%s），先恢复...' % active.get('status'), flush=True)
            result = journal.recover_locked(active, root)
            if not result.get('ok'):
                print('[安全更新] 恢复失败：%s' % (result.get('error') or result.get('status')), file=sys.stderr)
                return 2
        stage = None
        try:
            stage, manifest, files, removed, manifest_bytes = stage_bundle(root, args.zip)
            print('[安全更新] stage 已完整落齐并校验：%s（%d 文件）' % (stage, len(files)), flush=True)
            result = apply_stage(root, stage, manifest, files, removed, manifest_bytes)
        except Exception as exc:
            print('[安全更新] 失败：%s' % exc, file=sys.stderr)
            return 2
        finally:
            if stage and not args.keep_stage and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(main())
