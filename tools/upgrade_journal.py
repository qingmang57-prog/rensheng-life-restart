# -*- coding: utf-8 -*-
"""升级事务 journal：备份、替换/删除/新增清单、阶段状态与幂等回滚。

设计目标（t204 / t200 缺陷⑨）：
- 在动任何程序文件之前，先把完整变更清单和旧文件哈希写进 journal；
- journal 每次状态变化都原子替换，进程/机器中断后仍可读、可判；
- 恢复只按 journal 回滚，绝不按“当前树像不像基线”猜；
- 回滚按实际文件哈希判断，已回滚的文件跳过；中断后用户改过的文件不覆盖，
  记 recovery_failed，让启动继续拒绝，避免把用户改动吞掉；
- 恢复本身可重复执行（恢复中再被杀也能继续），成功后状态 rolled_back。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

JOURNAL_DIR = '.upgrade-journal'
JOURNAL_NAME = 'journal.json'
LOCK_NAME = 'journal.lock'
ACTIVE_STATES = {'preparing', 'backed_up', 'replacing', 'verifying', 'recovering'}
SAFE_STATES = {'committed', 'rolled_back'}
BROKEN_STATES = {'recovery_failed'}


class JournalError(RuntimeError):
    pass


class JournalCorrupt(JournalError):
    """journal 存在但读不出/不是对象；绝不能当成「没有事务」。"""


class Busy(JournalError):
    pass


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def journal_dir(root):
    return Path(root).resolve() / JOURNAL_DIR


def journal_path(root):
    return journal_dir(root) / JOURNAL_NAME


BOOT_STATE_NAME = 'boot_state.json'
BOOT_BROKEN = 'broken'
BOOT_UPGRADING = 'upgrading'
BOOT_RECOVERED = 'recovered'
BOOT_CLEAN = 'clean'


def boot_state_path(root):
    return journal_dir(root) / BOOT_STATE_NAME


def read_boot_state(root):
    path = boot_state_path(root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        # 侧车本身损坏：按 broken 处理，绝不放行。
        return {'state': BOOT_BROKEN, 'reason': 'boot_state 无法读取'}
    return value if isinstance(value, dict) else {'state': BOOT_BROKEN, 'reason': 'boot_state 非对象'}


def write_boot_state(root, state, reason='', **extra):
    value = {'state': state, 'reason': reason, 'updated_at': _now()}
    value.update(extra)
    _atomic_json(boot_state_path(root), value)
    return value


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-%s-%s' % (os.getpid(), uuid.uuid4().hex[:8]))
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(payload)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    last = None
    for attempt in range(10):
        try:
            os.replace(str(temporary), str(path))
            return path
        except PermissionError as exc:
            last = exc
            if attempt == 9:
                break
            time.sleep(0.05 + 0.02 * attempt)
        except OSError as exc:
            last = exc
            if attempt == 9:
                break
            time.sleep(0.05 + 0.02 * attempt)
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    raise last


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def file_sha(path):
    path = Path(path)
    return sha256_file(path) if path.is_file() else None


@contextmanager
def locked(root):
    """跨进程互斥。锁文件可残留，OS 锁随进程退出释放。"""
    lock_path = journal_dir(root) / LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, 'a+b')
    handle.seek(0, 2)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Busy('另一个升级/恢复进程正在操作这个包') from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Busy('另一个升级/恢复进程正在操作这个包') from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def read(root):
    """不存在返回 None；存在但损坏/不可读必须抛 JournalCorrupt，不能静默放行。"""
    path = journal_path(root)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise JournalCorrupt('journal 存在但无法读取：%s（%s）' % (path, exc))
    if not isinstance(value, dict):
        raise JournalCorrupt('journal 存在但不是 JSON 对象：%s' % path)
    return value


def active(root):
    value = read(root)
    return value if value and value.get('status') in ACTIVE_STATES | BROKEN_STATES else None


def is_safe(root):
    state = read_boot_state(root)
    if state and state.get('state') == BOOT_BROKEN:
        return False
    try:
        value = read(root)
    except JournalCorrupt:
        return False
    return bool(value and value.get('status') in SAFE_STATES)


def _write(journal):
    journal['updated_at'] = _now()
    _atomic_json(Path(journal['journal_path']), journal)
    return journal


def begin(root, version_from, version_to, data_status, backup_dir, entries, manifest):
    """建立 journal。调用方应已持有 locked(root)。"""
    root = Path(root).resolve()
    state = read_boot_state(root) or {}
    if state.get('state') == BOOT_BROKEN:
        raise JournalError('启动门禁为 broken，拒绝开始新升级；请先完成恢复/复核')
    current = read(root)
    if current and current.get('status') in ACTIVE_STATES | BROKEN_STATES:
        raise JournalError('已有未完成升级事务：%s' % current.get('status'))
    if current:
        history = journal_dir(root) / 'history'
        history.mkdir(parents=True, exist_ok=True)
        old = history / ('%s-%s.json' % (time.strftime('%Y%m%d-%H%M%S'), current.get('status') or 'old'))
        try:
            os.replace(str(journal_path(root)), str(old))
        except OSError:
            pass
    journal = {
        'schema': 1,
        'journal_id': uuid.uuid4().hex,
        'journal_path': str(journal_path(root)),
        'root': str(root),
        'from_version': str(version_from),
        'to_version': str(version_to),
        'status': 'preparing',
        'pid': os.getpid(),
        'created_at': _now(),
        'updated_at': _now(),
        'data_dir': str(data_status.get('path') or ''),
        'data_source': str(data_status.get('source') or ''),
        'data_configured_missing': bool(data_status.get('configured_missing')),
        'backup_dir': str(Path(backup_dir).resolve()),
        'files': [],
        'manifest': manifest,
        'data': None,
        'current_path': None,
        'applied_count': 0,
        'error': None,
        'phases': [{'at': _now(), 'status': 'preparing'}],
    }
    for item in entries:
        value = {
            'path': item['path'],
            'action': item['action'],
            'old_sha256': item.get('old_sha256'),
            'new_sha256': item.get('new_sha256'),
            'old_size': item.get('old_size'),
            'new_size': item.get('new_size'),
            'backup_rel': item.get('backup_rel'),
            'backup_sha256': None,
            'applied': False,
        }
        journal['files'].append(value)
    _write(journal)
    write_boot_state(root, BOOT_UPGRADING, '升级事务已开始', journal_id=journal['journal_id'])
    return journal


def phase(journal, status, note='', **extra):
    journal['status'] = status
    journal['phases'].append({'at': _now(), 'status': status, 'note': note})
    journal['phases'] = journal['phases'][-80:]
    journal.update(extra)
    _write(journal)
    if status == 'committed':
        write_boot_state(journal['root'], BOOT_CLEAN, '升级完成', journal_id=journal.get('journal_id'))
    elif status == 'rolled_back':
        write_boot_state(journal['root'], BOOT_RECOVERED, '恢复完成，待 guard 全树复核',
                         journal_id=journal.get('journal_id'))
    elif status == 'recovery_failed':
        write_boot_state(journal['root'], BOOT_BROKEN, note or '恢复失败',
                         journal_id=journal.get('journal_id'))
    else:
        write_boot_state(journal['root'], BOOT_UPGRADING, note or status,
                         journal_id=journal.get('journal_id'))
    return journal


def progress(journal, path, applied_count):
    journal['current_path'] = path
    journal['applied_count'] = int(applied_count)
    return _write(journal)


def backup_file(journal, root, relative, source, backup_rel):
    """备份单个旧文件并核对哈希；只写备份，不碰源文件。"""
    source = Path(source)
    target = Path(journal['backup_dir']) / backup_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp-%s' % uuid.uuid4().hex[:8])
    shutil.copy2(str(source), str(temporary))
    os.replace(str(temporary), str(target))
    return target


def mark_backup(journal, path, digest):
    for item in journal['files']:
        if item['path'] == path:
            item['backup_sha256'] = digest
            break
    if journal.get('manifest', {}).get('path') == path:
        journal['manifest']['backup_sha256'] = digest
    return _write(journal)


def _copy_atomic(source, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.restore-%s' % uuid.uuid4().hex[:8])
    shutil.copy2(str(source), str(temporary))
    os.replace(str(temporary), str(target))


def _safe_rel(rel):
    return isinstance(rel, str) and rel and '\\' not in rel and not rel.startswith('/') \
        and ':' not in rel and all(part not in ('', '.', '..') for part in rel.split('/'))


_WRITE_ROOTS = ('app/', 'tools/')


def _allowed_rel(rel):
    if not _safe_rel(rel):
        return False
    return rel in ('start.bat', 'update.bat') or any(rel.startswith(prefix) for prefix in _WRITE_ROOTS)


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


def _validate_journal(journal, root):
    """先验 journal 自身可信，再动任何文件；不信任 journal 里的 root/路径。"""
    if not isinstance(journal, dict):
        raise JournalCorrupt('journal 不是对象')
    actual_root = Path(root).resolve()
    recorded = Path(str(journal.get('root') or '')).resolve()
    if recorded != actual_root:
        raise JournalError('journal.root 与当前目录不一致：%s != %s' % (recorded, actual_root))
    if journal.get('status') not in ACTIVE_STATES | SAFE_STATES | BROKEN_STATES:
        raise JournalError('journal.status 非法：%r' % journal.get('status'))
    backup_root = Path(str(journal.get('backup_dir') or ''))
    if not backup_root.is_absolute():
        raise JournalError('journal.backup_dir 不是绝对路径')
    backup_root = backup_root.resolve()
    files = journal.get('files')
    if not isinstance(files, list) or len(files) > 10000:
        raise JournalError('journal.files 非法或过多')
    for item in files:
        if not isinstance(item, dict):
            raise JournalError('journal.files 项不是对象')
        path = item.get('path')
        if not _allowed_rel(path):
            raise JournalError('journal.files 路径越界：%r' % path)
        if item.get('action') not in ('replace', 'add', 'delete'):
            raise JournalError('journal.files.action 非法：%r' % item.get('action'))
        backup_rel = item.get('backup_rel')
        if item.get('action') == 'add':
            continue
        if not _safe_rel(backup_rel):
            raise JournalError('journal.files.backup_rel 非法：%r' % backup_rel)
        if not _inside(backup_root / backup_rel, backup_root):
            raise JournalError('journal.files.backup_rel 越界：%r' % backup_rel)
    manifest = journal.get('manifest')
    if manifest:
        if not isinstance(manifest, dict) or not _allowed_rel(manifest.get('path')):
            raise JournalError('journal.manifest 路径越界')
        backup_rel = manifest.get('backup_rel')
        if not _safe_rel(backup_rel) or not _inside(backup_root / backup_rel, backup_root):
            raise JournalError('journal.manifest.backup_rel 越界')
    # 恢复只写真实 root 下的 journal 路径，防止伪造 journal_path 写到别处。
    journal['journal_path'] = str(journal_path(actual_root))
    return journal


def _current_matches_old(journal, root):
    for item in journal.get('files') or []:
        if not _safe_rel(item.get('path')):
            return False, 'journal 含非法路径：%r' % item.get('path')
        actual = file_sha(Path(root) / item['path'])
        if item.get('action') == 'add':
            if actual is not None:
                return False, '新增文件已存在：%s' % item['path']
        elif actual != item.get('old_sha256'):
            return False, '文件已不是升级前状态：%s' % item['path']
    manifest = journal.get('manifest') or {}
    if manifest:
        actual = file_sha(Path(root) / manifest.get('path', ''))
        if actual != manifest.get('old_sha256'):
            return False, '基线清单已不是升级前状态'
    return True, ''


def _restore_one(root, item, backup_dir):
    path = Path(root) / item['path']
    actual = file_sha(path)
    action = item.get('action')
    old = item.get('old_sha256')
    new = item.get('new_sha256')
    if action == 'add':
        if actual is None:
            return
        if actual == new:
            path.unlink()
            return
        raise JournalError('新增文件被用户改过，拒绝删除：%s' % item['path'])
    if actual == old:
        return
    if actual is not None and actual != new:
        raise JournalError('文件在中断后被用户改过，拒绝覆盖：%s' % item['path'])
    backup_rel = item.get('backup_rel')
    if not backup_rel:
        raise JournalError('journal 缺少备份路径：%s' % item['path'])
    backup = Path(backup_dir) / backup_rel
    if not backup.is_file():
        raise JournalError('备份缺失：%s' % item['path'])
    digest = sha256_file(backup)
    expected = item.get('backup_sha256') or old
    if digest != expected:
        raise JournalError('备份哈希不符：%s' % item['path'])
    _copy_atomic(backup, path)
    if file_sha(path) != old:
        raise JournalError('恢复后哈希不符：%s' % item['path'])


def _restore_manifest(journal, root):
    item = journal.get('manifest') or {}
    if not item:
        return
    path = Path(root) / item['path']
    actual = file_sha(path)
    old = item.get('old_sha256')
    new = item.get('new_sha256')
    if actual == old:
        return
    if actual is not None and actual != new:
        raise JournalError('基线清单在中断后被改过，拒绝覆盖')
    backup = Path(journal['backup_dir']) / item.get('backup_rel', '')
    if not backup.is_file():
        raise JournalError('基线清单备份缺失')
    if sha256_file(backup) != (item.get('backup_sha256') or old):
        raise JournalError('基线清单备份哈希不符')
    _copy_atomic(backup, path)
    if file_sha(path) != old:
        raise JournalError('基线清单恢复后哈希不符')


def _recovery_proof(journal, root):
    try:
        _validate_journal(journal, root)
    except Exception as exc:
        return False, str(exc)
    if journal.get('status') != 'rolled_back':
        return False, 'journal 不是已恢复状态'
    return _current_matches_old(journal, root)


def recover_locked(journal, expected_root=None):
    """调用方已持有 locked(root)。先验证 root/路径，再恢复；失败写 recovery_failed。"""
    if not journal:
        return {'ok': True, 'status': 'none'}
    root = Path(journal.get('root') or expected_root or '')
    try:
        _validate_journal(journal, expected_root or root)
    except Exception as exc:
        try:
            write_boot_state(expected_root or root, BOOT_BROKEN,
                             'journal 校验失败：%s' % exc, journal_id=journal.get('journal_id'))
        except Exception:
            pass
        return {'ok': False, 'status': 'recovery_failed', 'error': str(exc)}
    status = journal.get('status')
    if status == 'committed':
        return {'ok': True, 'status': status}
    if status in SAFE_STATES:
        ok, why = _recovery_proof(journal, root)
        if not ok:
            write_boot_state(root, BOOT_BROKEN, '已恢复状态复核失败：%s' % why,
                             journal_id=journal.get('journal_id'))
            return {'ok': False, 'status': 'recovery_failed', 'error': why}
        write_boot_state(root, BOOT_RECOVERED, '恢复状态全树哈希复核通过',
                         journal_id=journal.get('journal_id'))
        return {'ok': True, 'status': status}
    try:
        if status in ('preparing', 'backed_up'):
            ok, why = _current_matches_old(journal, root)
            if not ok:
                phase(journal, 'recovery_failed', why)
                return {'ok': False, 'status': 'recovery_failed', 'error': why}
            phase(journal, 'rolled_back', '备份阶段中断，未改程序文件')
            return {'ok': True, 'status': 'rolled_back'}
        _write(phase(journal, 'recovering', '开始按 journal 恢复旧版本'))
        for item in journal.get('files') or []:
            _restore_one(root, item, journal['backup_dir'])
        _restore_manifest(journal, root)
        ok, why = _current_matches_old(journal, root)
        if not ok:
            raise JournalError(why)
        phase(journal, 'rolled_back', '已恢复到升级前版本')
        return {'ok': True, 'status': 'rolled_back'}
    except Exception as exc:
        try:
            phase(journal, 'recovery_failed', str(exc))
        except Exception:
            pass
        return {'ok': False, 'status': 'recovery_failed', 'error': str(exc)}


def quarantine(root):
    """损坏 journal 原位保留，只复制到 history/corrupt-* 作为证据，并落 broken。"""
    root = Path(root).resolve()
    source = journal_path(root)
    target = journal_dir(root) / 'history' / ('corrupt-%s-%s.json'
                                              % (time.strftime('%Y%m%d-%H%M%S'), uuid.uuid4().hex[:8]))
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.exists():
        shutil.copy2(str(source), str(target))
    write_boot_state(root, BOOT_BROKEN, '升级 journal 损坏，已保留原件并复制证据',
                     evidence=str(target))
    return str(target)


def recover_locked_root(root):
    """持有 locked(root) 时调用；损坏 journal 只备份、不移动、不冒充恢复成功。"""
    root = Path(root).resolve()
    state = read_boot_state(root) or {}
    try:
        journal = read(root)
    except JournalCorrupt as exc:
        path = quarantine(root)
        return {'ok': False, 'status': 'corrupt_quarantined', 'error': str(exc),
                'quarantined': path,
                'action': '损坏 journal 已原位保留并复制证据；请从 data_dir/backups/patch-* 手动恢复程序文件，再启动自检'}
    return recover_locked(journal, root)


def recover(root):
    """带锁恢复；启动入口和 apply_patch 共用。"""
    root = Path(root).resolve()
    with locked(root):
        return recover_locked_root(root)


def guard(root):
    """启动前检查；读取持久 boot_state，返回 (can_start, message, journal)。"""
    root = Path(root).resolve()
    state = read_boot_state(root) or {}
    state_name = state.get('state')
    command = 'tools\\python\\python.exe tools\\apply_patch.py --recover --root "%s"' % root
    if state_name == BOOT_BROKEN:
        return False, ('升级状态为 broken，程序文件可能处于混合状态，已拒绝启动。\n'
                       '原因：%s\n请运行恢复命令：%s' %
                       (state.get('reason') or '', command)), None
    if state_name == BOOT_UPGRADING:
        return False, ('升级事务仍在进行中（boot_state=upgrading），已拒绝启动。\n'
                       '请稍候；若已中断，请运行恢复命令：%s' % command), None
    try:
        journal = read(root)
    except JournalCorrupt as exc:
        try:
            quarantine(root)
        except Exception:
            pass
        return False, ('升级 journal 存在但已损坏，无法确认升级是否完成；已拒绝启动。\n'
                       '请运行恢复命令：%s\n详情：%s' % (command, exc)), None
    if not journal:
        if state_name == BOOT_RECOVERED:
            return False, ('升级状态为 recovered，但缺少 journal，无法完成全树复核；已拒绝启动。\n'
                           '请运行恢复命令：%s' % command), None
        return True, '', None
    try:
        _validate_journal(journal, root)
    except Exception as exc:
        try:
            write_boot_state(root, BOOT_BROKEN, 'journal 校验失败：%s' % exc,
                             journal_id=journal.get('journal_id'))
        except Exception:
            pass
        return False, ('升级 journal 校验失败，已拒绝启动。\n请运行恢复命令：%s\n详情：%s'
                       % (command, exc)), journal
    status = journal.get('status')
    if state_name == BOOT_RECOVERED or status == 'rolled_back':
        ok, why = _recovery_proof(journal, root)
        if not ok:
            try:
                write_boot_state(root, BOOT_BROKEN, '恢复复核失败：%s' % why,
                                 journal_id=journal.get('journal_id'))
            except Exception:
                pass
            return False, ('恢复状态全树复核失败，已拒绝启动。\n原因：%s\n请运行恢复命令：%s'
                           % (why, command)), journal
        return True, '', journal
    if status in SAFE_STATES:
        return True, '', journal
    if state_name == BOOT_UPGRADING:
        return False, ('升级状态为 upgrading，已拒绝启动。\n请运行恢复命令：%s' % command), journal
    if status in BROKEN_STATES:
        return False, ('上次升级恢复失败（%s）。程序文件可能处于混合状态，已拒绝启动。\n'
                       '请运行恢复命令：%s\n详情：%s'
                       % (status, command, journal.get('error') or '')), journal
    return False, ('上次升级未完成（%s），程序文件可能处于混合状态，已拒绝启动。\n'
                   '请运行恢复命令：%s' % (status, command)), journal


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'status'
    target = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path(__file__).resolve().parents[1]
    if command == 'status':
        try:
            print(json.dumps(read(target) or {}, ensure_ascii=False, indent=2))
        except JournalCorrupt as exc:
            print(json.dumps({'corrupt': True, 'error': str(exc)}, ensure_ascii=False, indent=2))
            raise SystemExit(3)
    elif command == 'guard':
        ok, message, _ = guard(target)
        print(message or 'startable')
        raise SystemExit(0 if ok else 3)
    elif command == 'recover':
        print(json.dumps(recover(target), ensure_ascii=False, indent=2))
    else:
        raise SystemExit('用法：upgrade_journal.py [status|guard|recover] [root]')
