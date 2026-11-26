# -*- coding: utf-8 -*-
"""安装增量补丁：整体校验 → 备份 → 替换 → 启动自检 → 失败自动回滚。

用法（包根目录）：
  python tools/apply_patch.py --zip patch-20261103-1230.zip
买家侧可直接把补丁 zip 拖到 update.bat 上。
"""
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
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_patch import is_excluded, path_allowed, read_json, sha256_file, version_newer  # noqa: E402
import launch_root  # noqa: E402
import upgrade_journal as journal  # noqa: E402

ROOT_DEFAULT = Path(__file__).resolve().parents[1]
EXTRA_ALLOWED = {'tools/release-manifest.json'}
DB_NAME = 'workbench.sqlite3'
# 升级只替换程序文件，不改这些数据目录；备份清单用于证明附件仍在原处。
ASSET_DIRS = ('uploads', 'artifacts', 'avatars', 'packages')


def fail(message, code=2):
    print('[补丁] 已拒绝：%s' % message, flush=True)
    sys.exit(code)


def safe_rel(rel):
    if not isinstance(rel, str) or not rel:
        return False
    if '\\' in rel or rel.startswith('/') or ':' in rel:
        return False
    return all(part not in ('', '.', '..') for part in rel.split('/'))


def current_version(root):
    data = read_json(root / 'app' / 'web' / 'version.json') or {}
    return str(data.get('version') or '0'), int(data.get('data_version') or 1)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def tail(path, limit=600):
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return text[-limit:].strip()


def bootstrap_ok(payload):
    """bootstrap 就绪判据（2026-11-18 修正，积分真源设计稿 §5.1）。

    旧判据只认 payload['projects'] 是 list；现版 app.py 的 /api/bootstrap
    已不再返回 projects（返回 profile/capabilities/trial/billing/flow 等），
    导致任何补丁自检必然超时回滚。修正为：
    - 200 + JSON dict 且 projects 为 list（旧版包兼容分支）；或
    - profile 与 capabilities 均为 dict（现版判据；预热期允许为空/部分降级，
      不要求非空，不要求 billing/flow 字段——它们有 _cap 兜底、形状不稳）。
    其余隔离/超时/回滚机制不动。
    """
    if not isinstance(payload, dict):
        return False, 'bootstrap 返回的不是 JSON 对象'
    projects = payload.get('projects')
    if isinstance(projects, list):
        return True, 'bootstrap 200，读到 %d 个项目' % len(projects)
    if isinstance(payload.get('profile'), dict) and isinstance(payload.get('capabilities'), dict):
        return True, 'bootstrap 200，profile/capabilities 就绪'
    return False, 'bootstrap 200 但 profile/capabilities 未就绪'


def _read_local_config(path):
    """读 .env.local 的 IP_* 项，规则与 app.py:135-144 一致。"""
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


def resolve_data_dir(root):
    """按 app.py 的优先级 + 本机 pointer 解析数据目录。

    与 launch_root.resolve_data_dir 是同一套规则；额外 pointer 只用于
    「整包解压到新目录」时承接旧库。显式路径不存在仍单列，由调用方拒绝。
    """
    return launch_root.resolve_data_dir(Path(root).resolve())


def effective_data_dir(root):
    """兼容旧调用：只取解析后的路径；需要区分回落时用 resolve_data_dir。"""
    return resolve_data_dir(root)['path']


def data_dir_blocker(status):
    """显式数据目录不存在且包内回落库也没有可保护的数据时，拒绝升级。"""
    if not status.get('configured_missing'):
        return None
    fallback = Path(status.get('fallback') or '')
    db = fallback / DB_NAME
    if db.is_file():
        try:
            con = sqlite3.connect(str(db), timeout=10)
            try:
                counts = _sqlite_table_counts(con)
            finally:
                con.close()
            if counts:
                return None
        except sqlite3.Error:
            pass
    return ('配置的数据目录不存在：%s（来源 %s）。按 app.py 会回落到包内 %s，'
            '但那里没有可保护的旧库；为避免把空库当旧库备份，已拒绝升级。'
            '请先把 IP_DATA_DIR 指回原数据目录，或在 .env.local 修正后重试。'
            % (status.get('configured'), status.get('source'), fallback))


def _sqlite_table_counts(con):
    names = [row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: int(con.execute('SELECT COUNT(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0])
            for name in names}


def snapshot_sqlite(src_db, dst_db):
    """用 SQLite backup API 取一致快照，不裸拷 WAL/SHM。

    返回源/快照逐表行数、integrity_check、sha256；行数不一致直接抛错。
    """
    src_db = Path(src_db)
    dst_db = Path(dst_db)
    if not src_db.is_file():
        return {'skipped': '源数据库不存在', 'source': str(src_db)}
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ('-wal', '-shm'):
        Path(str(dst_db) + suffix).unlink(missing_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    src = None
    try:
        src = sqlite3.connect(src_db.resolve().as_uri() + '?mode=ro', uri=True, timeout=30,
                              isolation_level=None)
        src.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
    except sqlite3.Error:
        # 少数 WAL 残留没有 -shm 时只读连接打不开；退回普通连接，只调用 backup()。
        if src is not None:
            src.close()
        src = sqlite3.connect(str(src_db), timeout=30, isolation_level=None)
    dst = sqlite3.connect(str(dst_db), timeout=30)
    try:
        # 先开读事务并数源库：backup API 与后续计数都看同一个快照时点，
        # 避免备份结束后另读活库、把客户并发新增误判成快照损坏。
        src.execute('BEGIN')
        source_counts = _sqlite_table_counts(src)
        src.backup(dst)
        snapshot_counts = _sqlite_table_counts(dst)
        integrity = str(dst.execute('PRAGMA integrity_check').fetchone()[0])
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
        raise RuntimeError('快照完整性检查失败：%s' % integrity)
    if source_counts != snapshot_counts:
        raise RuntimeError('快照行数与源库不一致：源 %s / 快照 %s' % (source_counts, snapshot_counts))
    return {
        'source': str(src_db),
        'snapshot': str(dst_db),
        'method': 'sqlite_backup_api',
        'integrity': integrity,
        'tables': len(snapshot_counts),
        'rows': sum(snapshot_counts.values()),
        'row_counts': snapshot_counts,
        'sha256': sha256_file(dst_db),
    }


def backup_data(data_dir, backup):
    """替换程序文件前，先把权威数据库做一致性快照，并记录附件目录清点。"""
    data_dir = Path(data_dir)
    info = {'source_data_dir': str(data_dir), 'database': None, 'assets': {}}
    src_db = data_dir / DB_NAME
    if src_db.is_file():
        info['database'] = snapshot_sqlite(src_db, Path(backup) / 'data' / DB_NAME)
    else:
        info['database'] = {'skipped': '源数据库不存在', 'source': str(src_db)}
    for name in ASSET_DIRS:
        folder = data_dir / name
        if folder.is_dir():
            files = [p for p in folder.rglob('*') if p.is_file()]
            info['assets'][name] = {
                'exists': True, 'files': len(files),
                'bytes': sum(p.stat().st_size for p in files),
            }
        else:
            info['assets'][name] = {'exists': False, 'files': 0, 'bytes': 0}
    (Path(backup) / 'data_backup.json').write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    return info


def check_full(root, backup):
    status = resolve_data_dir(root)
    block = data_dir_blocker(status)
    if block:
        return False, block
    data_dir = status['path']
    check_dir = (backup / 'selfcheck-data').resolve()  # 必须绝对路径：子进程 cwd 不同
    check_dir.mkdir(parents=True, exist_ok=True)
    try:
        snapshot = snapshot_sqlite(data_dir / DB_NAME, check_dir / DB_NAME)
    except Exception as exc:
        return False, '数据快照失败（%s）：%s' % (data_dir, exc)
    data_note = '数据目录 %s（来源 %s）%s；SQLite backup API 快照 %s' % (
        data_dir, status.get('source'), '（显式路径不存在，已回落）' if status.get('configured_missing') else '',
        ('%d 表/%d 行' % (snapshot['tables'], snapshot['rows']))
        if snapshot.get('row_counts') is not None else snapshot.get('skipped'))
    port = free_port()
    env = os.environ.copy()
    env['IP_DATA_DIR'] = str(check_dir)
    env['PYTHONIOENCODING'] = 'utf-8'
    log_path = backup / 'selfcheck.log'
    handle = log_path.open('w', encoding='utf-8')
    proc = subprocess.Popen(
        [sys.executable, str(root / 'app' / 'app.py'), '--port', str(port)],
        cwd=str(root), env=env, stdout=handle, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机自检不走系统代理
    try:
        deadline = time.time() + 120
        time.sleep(3)  # 首次 bootstrap 要探测封面模板（node），给启动留出预热时间
        while time.time() < deadline:
            if proc.poll() is not None:
                return False, '应用启动后立即退出（退出码 %s；%s）：%s' % (
                    proc.returncode, data_note, tail(log_path))
            try:
                with opener.open('http://127.0.0.1:%d/api/bootstrap' % port, timeout=10) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                    ok, detail = bootstrap_ok(payload)
                    if ok:
                        return True, '%s；%s' % (detail, data_note)
            except Exception:
                pass
            time.sleep(2)
        return False, '120 秒内 /api/bootstrap 没有返回 200（%s）：%s' % (data_note, tail(log_path))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        handle.close()


def check_templates(root):
    node = root / 'tools' / 'node' / 'bin' / 'node.exe'
    if not node.is_file():
        return False, '缺少 Node（tools/node/bin/node.exe），无法校验模板库'
    env = os.environ.copy()
    env['IP_NODE'] = str(node)
    env['PYTHONIOENCODING'] = 'utf-8'
    code = ("import json,sys;sys.path.insert(0,'app');import cover_packaging;"
            "meta=cover_packaging.metadata();print(json.dumps({'templates':len(meta.get('templates') or [])}))")
    result = subprocess.run([sys.executable, '-c', code], cwd=str(root), env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=90)
    if result.returncode != 0:
        return False, '模板库自检失败：%s' % (result.stderr or result.stdout)[-500:]
    try:
        count = int(json.loads(result.stdout.strip().splitlines()[-1])['templates'])
    except (IndexError, ValueError, KeyError):
        return False, '模板库自检返回异常：%s' % result.stdout[-300:]
    if count < 1:
        return False, '模板库为空（templates.js 无法读出模板）'
    return True, '模板库可读，共 %d 套' % count


def _file_digest(path):
    return sha256_file(path) if Path(path).is_file() else None


def installed_matches(root, version, bundle_manifest_bytes, files, removed):
    """同版本幂等判据：版本、补丁清单、每个文件/删除项都已是目标状态。"""
    current, _ = current_version(root)
    if current != version:
        return False, '版本仍是 %s' % current
    manifest_path = root / 'tools' / 'release-manifest.json'
    if not manifest_path.is_file():
        return False, '缺少 tools/release-manifest.json'
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != hashlib.sha256(bundle_manifest_bytes).hexdigest():
        return False, 'release-manifest 与补丁不一致'
    for item in files:
        target = root / item['path']
        if not target.is_file() or _file_digest(target) != item.get('sha256'):
            return False, '%s 不是目标版本' % item['path']
    for rel in removed:
        if (root / rel).exists():
            return False, '%s 仍存在' % rel
    return True, '目标版本文件与清单全部一致'


def build_entries(root, files, removed):
    entries = []
    for item in files:
        rel = item['path']
        old = root / rel
        entries.append({
            'path': rel,
            'action': 'replace' if old.is_file() else 'add',
            'old_sha256': _file_digest(old),
            'new_sha256': item.get('sha256'),
            'backup_rel': ('files/%s' % rel) if old.is_file() else None,
            'existed': old.is_file(),
        })
    for rel in removed:
        entries.append({'path': rel, 'action': 'delete', 'old_sha256': _file_digest(root / rel),
                        'new_sha256': None, 'backup_rel': 'files/%s' % rel, 'existed': True})
    return entries


def backup_program(root, state, entries, backup, manifest_bytes):
    """先备份所有会被替换/删除的程序文件和旧 manifest，再动任何文件。"""
    records = []
    for entry in entries:
        rel = entry['path']
        src = root / rel
        if entry['existed']:
            if not src.is_file():
                raise RuntimeError('备份时找不到原文件：%s' % rel)
            dst = journal.backup_file(state, root, rel, src, entry['backup_rel'])
            if sha256_file(dst) != entry['old_sha256']:
                raise RuntimeError('备份校验失败：%s' % rel)
            journal.mark_backup(state, rel, entry['old_sha256'])
            records.append({'path': rel, 'existed': True, 'sha256': entry['old_sha256']})
        else:
            records.append({'path': rel, 'existed': False, 'sha256': None})
    old_manifest = root / 'tools' / 'release-manifest.json'
    if old_manifest.is_file():
        journal.backup_file(state, root, 'tools/release-manifest.json', old_manifest,
                            'files/tools/release-manifest.json')
        journal.mark_backup(state, 'tools/release-manifest.json', _file_digest(old_manifest))
        records.append({'path': 'tools/release-manifest.json', 'existed': True,
                        'sha256': _file_digest(old_manifest)})
    (backup / 'backup.json').write_text(json.dumps(
        {'from': state.get('version_from'), 'to': state.get('version_to'),
         'files': records, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()},
        ensure_ascii=False, indent=2), encoding='utf-8')
    (backup / 'manifest.json').write_bytes(manifest_bytes)
    return records


def apply_entries(root, state, bundle, entries, manifest_bytes):
    for index, entry in enumerate(entries, 1):
        rel = entry['path']
        target = root / rel
        if entry['action'] == 'delete':
            target.unlink(missing_ok=True)
        else:
            raw = bundle.read(rel)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != entry.get('new_sha256'):
                raise RuntimeError('写入前校验失败：%s' % rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.patching')
            temporary.write_bytes(raw)
            if sha256_file(temporary) != digest:
                temporary.unlink(missing_ok=True)
                raise RuntimeError('临时文件校验失败：%s' % rel)
            temporary.replace(target)
        journal.progress(state, rel, index)
    manifest_target = root / 'tools' / 'release-manifest.json'
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_target.with_name(manifest_target.name + '.patching')
    temporary.write_bytes(manifest_bytes)
    temporary.replace(manifest_target)


def _recover_after_failure(root, state, reason):
    try:
        recovered = journal.recover_locked(state, root)
    except Exception as exc:
        print('[补丁] 自动恢复失败：%s；journal 保留在 %s' % (exc, journal.journal_path(root)), flush=True)
        print('[补丁] 请运行：tools\\python\\python.exe tools\\apply_patch.py --recover --root "%s"' % root,
              flush=True)
        return False
    if recovered.get('ok'):
        print('[补丁] 已自动回滚到 v%s：%s' % (state.get('version_from'), reason), flush=True)
        print('[补丁] 备份保留在：%s' % state.get('backup_dir'), flush=True)
        return True
    print('[补丁] 自动回滚未完成（%s）：%s'
          % (recovered.get('error') or recovered.get('status'), reason), flush=True)
    print('[补丁] journal 保留在：%s；可运行 --recover 人工恢复。' % journal.journal_path(root), flush=True)
    return False


def _run_patch(args):
    root = Path(args.root).resolve()
    zip_path = Path(args.zip)
    if not zip_path.is_file():
        fail('找不到补丁包：%s' % zip_path)
    current, current_data = current_version(root)
    base = read_json(root / 'tools' / 'release-manifest.json')
    if not base or not base.get('files'):
        fail('缺少基线清单 tools/release-manifest.json，无法确认当前包身份')

    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
        if 'manifest.json' not in names:
            fail('补丁包缺少 manifest.json')
        try:
            manifest = json.loads(bundle.read('manifest.json').decode('utf-8'))
        except ValueError:
            fail('manifest.json 不是有效的 JSON')
        kind = manifest.get('kind')
        if kind not in ('full', 'templates'):
            fail('未知的补丁类型：%s' % kind)
        version = str(manifest.get('version') or '')
        if not version:
            fail('manifest 缺少 version')
        if int(manifest.get('data_min') or 0) > current_data:
            fail('该补丁要求数据版本 ≥ %s，当前只有 %s' % (manifest.get('data_min'), current_data))
        files = manifest.get('files') or []
        removed = manifest.get('removed') or []
        if not isinstance(files, list) or not isinstance(removed, list):
            fail('manifest 的文件清单格式不对')
        seen = set()
        for item in files:
            if not isinstance(item, dict):
                fail('manifest 文件条目格式不对')
            rel = item.get('path')
            if not safe_rel(rel):
                fail('非法路径：%s' % rel)
            if rel in seen:
                fail('文件重复出现：%s' % rel)
            seen.add(rel)
            if is_excluded(rel) or not path_allowed(rel, kind):
                fail('补丁包含不允许的文件：%s' % rel)
        for rel in removed:
            if not safe_rel(rel) or is_excluded(rel) or not path_allowed(rel, kind):
                fail('删除清单包含不允许的文件：%s' % rel)

        expected = {'manifest.json', 'tools/release-manifest.json'} | seen
        extra = names - expected
        missing = expected - names
        if extra:
            fail('补丁包里有清单外的文件：%s' % ', '.join(sorted(extra)))
        if missing:
            fail('补丁包缺少清单里的文件：%s' % ', '.join(sorted(missing)))

        for item in files:
            raw = bundle.read(item['path'])
            digest = hashlib.sha256(raw).hexdigest()
            if digest != item.get('sha256'):
                fail('%s 的 sha256 与清单不符' % item['path'])
            if item.get('size') is not None and len(raw) != int(item['size']):
                fail('%s 的大小与清单不符' % item['path'])

        manifest_bytes = bundle.read('tools/release-manifest.json')
        # 同版本重跑：先按目标清单逐项核对，避免 bootstrap 已落盘后因基线版本不同卡死。
        if current == version:
            ok, reason = installed_matches(root, version, manifest_bytes, files, removed)
            if ok:
                print('[补丁] 当前已是 v%s，目标文件与清单全部一致，无需重复安装。' % version, flush=True)
                return 0
            fail('当前版本是 v%s，但与补丁目标不一致（%s）；请先 --recover 或恢复备份' % (version, reason))

        base_version = str(base.get('version') or '')
        if manifest.get('base_version') and str(manifest['base_version']) != base_version:
            fail('补丁基线是 %s，当前包基线是 %s' % (manifest['base_version'], base_version))
        base_files = base['files']
        for item in files:
            rel = item['path']
            expected_hash = base_files.get(rel)
            if not expected_hash:
                if (root / rel).exists():
                    fail('%s 已存在但不在基线清单里，可能被本地改过' % rel)
                continue
            target = root / rel
            if not target.is_file():
                fail('当前包缺少基线文件：%s' % rel)
            if sha256_file(target) != expected_hash:
                fail('%s 与基线不一致，可能被本地改过，已拒绝覆盖' % rel)
        for rel in removed:
            expected_hash = base_files.get(rel)
            target = root / rel
            if not expected_hash or not target.is_file():
                fail('当前包缺少要删除的基线文件：%s' % rel)
            if sha256_file(target) != expected_hash:
                fail('%s 与基线不一致，已拒绝删除' % rel)

        if not version_newer(version, current):
            fail('补丁版本 %s 不高于当前 %s，不允许降级或重复安装' % (version, current))

        data_status = resolve_data_dir(root)
        block = data_dir_blocker(data_status)
        if block:
            fail(block, code=3)
        data_dir = data_status['path']
        print('[补丁] 数据目录：%s（来源 %s）' % (data_dir, data_status.get('source')), flush=True)
        if data_status.get('configured_missing'):
            print('[补丁] 警告：配置的数据目录 %s 不存在，按 app.py 回落 %s；'
                  '该回落库已有数据，继续备份。' % (data_status.get('configured'), data_dir), flush=True)
        backup_root = data_dir / 'backups'
        backup = backup_root / ('patch-%s' % version)
        suffix = 1
        while backup.exists():
            backup = backup_root / ('patch-%s-%d' % (version, suffix))
            suffix += 1
        (backup / 'files').mkdir(parents=True, exist_ok=True)
        entries = build_entries(root, files, removed)
        manifest_entry = {
            'path': 'tools/release-manifest.json',
            'old_sha256': _file_digest(root / 'tools' / 'release-manifest.json'),
            'new_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'backup_rel': 'files/tools/release-manifest.json',
        }
        state = journal.begin(root, current, version, data_status, str(backup), entries,
                              manifest_entry)
        try:
            data_info = backup_data(data_dir, backup)
            backup_records = backup_program(root, state, entries, backup, manifest_bytes)
            (backup / 'backup.json').write_text(json.dumps(
                {'from': current, 'to': version, 'files': backup_records, 'data': data_info},
                ensure_ascii=False, indent=2), encoding='utf-8')
            journal.phase(state, 'backed_up', '数据与程序文件备份完成')
        except Exception as exc:
            _recover_after_failure(root, state, '备份失败：%s' % exc)
            fail('备份失败，未替换任何程序文件：%s' % exc, code=2)

        try:
            journal.phase(state, 'replacing', '开始替换程序文件')
            apply_entries(root, state, bundle, entries, manifest_bytes)
        except Exception as exc:
            _recover_after_failure(root, state, '写入失败：%s' % exc)
            fail('写入失败，未能自动恢复：%s' % exc, code=3)

        journal.phase(state, 'verifying', '文件写入完成，开始自检')
        ok, detail = check_templates(root) if kind == 'templates' else check_full(root, backup)
        if not ok:
            recovered = _recover_after_failure(root, state, '自检未通过：%s' % detail)
            print('[补丁] 自检未通过：%s' % detail, flush=True)
            sys.exit(3 if recovered else 2)
        journal.phase(state, 'committed', '自检通过')
        print('[补丁] 安装成功：v%s -> v%s' % (current, version), flush=True)
        print('[补丁] 改动 %d 个文件，删除 %d 个；自检：%s'
              % (len(seen), len(removed), detail), flush=True)
        print('[补丁] 原文件已备份到：%s' % backup, flush=True)
        return 0


def main():
    parser = argparse.ArgumentParser(description='安装增量补丁包（事务化，可自动恢复）')
    parser.add_argument('--zip')
    parser.add_argument('--root', default=str(ROOT_DEFAULT))
    parser.add_argument('--recover', action='store_true', help='只恢复未完成/中断的升级，不安装补丁')
    args = parser.parse_args()
    root = Path(args.root).resolve()

    try:
        with journal.locked(root):
            if args.recover:
                result = journal.recover_locked_root(root)
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
                return 0 if result.get('ok') else 2
            try:
                active = journal.read(root)
            except journal.JournalCorrupt as exc:
                fail('升级 journal 损坏，已拒绝继续（不会当成没有事务）：%s\n'
                     '先运行 --recover 隔离，再从备份手动恢复；未恢复前不要启动。' % exc, code=3)
            if active and active.get('status') not in ('committed', 'rolled_back'):
                print('[补丁] 发现未完成升级（%s），先自动恢复 v%s...'
                      % (active.get('status'), active.get('version_from')), flush=True)
                result = journal.recover_locked(active, root)
                if not result.get('ok'):
                    fail('上次升级未能自动恢复：%s；请运行 --recover 查看'
                         % (result.get('error') or result.get('status')), code=3)
                print('[补丁] 已恢复到 v%s，继续安装。' % result.get('version'), flush=True)
            if not args.zip:
                fail('缺少 --zip（或使用 --recover）')
            return _run_patch(args)
    except journal.Busy:
        fail('另一个升级进程正在运行，请等它结束；不要同时双击更新', code=4)
    except journal.JournalError as exc:
        fail('升级 journal 异常：%s' % exc, code=3)


if __name__ == '__main__':
    raise SystemExit(main())
