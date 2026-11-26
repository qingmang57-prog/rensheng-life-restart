# -*- coding: utf-8 -*-
"""安装绑定（软绑定，不是硬激活）。

三处交叉校验，任何一处缺失都不算"新装"，只算异常态；迁移计数取三处的最大值，
不会因为删掉某一个文件而清零。

- 三处绑定：HKCU\\Software\\IPWorkbench（注册表）、系统目录 install.json
  （%LOCALAPPDATA%\\IPWorkbench\\install.json）、包内 install_tag.json。
- 三处各存一份 install_id（机器指纹 sha256 前 16 位）、迁移次数、首装时间、
  最后运行时间、完整性哈希；任何一处被改动或缺失都会记入异常列表。
- 全新机器首次安装：三处都不存在，才建立 0 次迁移的新记录。
- 同机换目录 / 删库：系统 ID 不变，包内标记缺失时按系统/注册表记录恢复，不算迁移。
- 包被拷到另一台机器：包内标记的 install_id 与当前机器不符，计一次迁移；免 2 次，
  第 3 次只读。
- 系统目录不可写：自动降级写到包内 app/.runtime/install_fallback.json，并给出提示。
- 系统时间回拨：记录上次运行时间戳，当前时间倒退超过 6 小时标记异常，且不把
  last_seen 往回写。
- 只存机器特征的 sha256 前 16 位，不存原始 MAC/UUID；不上传、不联网、不外传。
"""
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 环境仅用于自测/静态检查
    winreg = None

PRODUCT_DIR = 'IPWorkbench'
MAX_MIGRATIONS = 2
ROOT = Path(__file__).resolve().parent
REG_SUBKEY = 'Software\\IPWorkbench'
REG_VALUE = 'binding'
SCHEMA = 2
LAST_STATE = {}
CLOCK_ROLLBACK_SECONDS = 6 * 3600
LOG_MAX_BYTES = 256 * 1024
EVENT_KEEP = 10
TIME_FMT = '%Y-%m-%d %H:%M:%S'

# 需要人工确认的异常（写权限降级、时钟回拨单独提示，不混进这里）
CONFIRM_CODES = {
    'missing_registry', 'missing_system', 'missing_package_tag',
    'copy_conflict', 'count_mismatch', 'record_tampered',
    'partial_records', 'id_drift',
}

ANOMALY_LABELS = {
    'missing_registry': '注册表绑定记录缺失',
    'missing_system': '系统目录绑定文件缺失',
    'missing_package_tag': '包内绑定标记缺失',
    'copy_conflict': '三处绑定信息不一致',
    'count_mismatch': '三处迁移计数不一致',
    'record_tampered': '绑定记录完整性校验失败',
    'partial_records': '只剩部分绑定记录',
    'id_drift': '机器指纹发生变化',
    'id_rebound': '同机安装标识已自动校正（不算换机）',
    'clock_rollback': '系统时间被回拨',
    'system_readonly': '系统目录不可写',
    'registry_readonly': '注册表不可写',
    'tag_readonly': '程序目录不可写',
}


def _now_ts():
    """当前时间戳；自测通过替换本函数模拟时间。"""
    return time.time()


def _fmt(ts):
    return time.strftime(TIME_FMT, time.localtime(ts))


def _now():
    return _fmt(_now_ts())


def _parse_ts(value, fallback=None):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        for fmt in (TIME_FMT, '%Y-%m-%dT%H:%M:%S'):
            try:
                return time.mktime(time.strptime(value.strip(), fmt))
            except ValueError:
                continue
    return fallback


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _read_json(path):
    try:
        # utf-8-sig 兼容系统工具写出的 BOM
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, payload):
    """写 JSON；目标目录不可写时返回 False，由调用方降级，不阻断启动。"""
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        return False
    return True


class RegistryStore(object):
    """注册表读写；自测可替换成文件版假注册表。"""

    def available(self):
        return False

    def read(self):
        return None

    def write(self, payload):
        return False


class WinRegistryStore(RegistryStore):
    def available(self):
        return winreg is not None

    def read(self):
        if winreg is None:
            return None
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_READ) as key:
                raw, _ = winreg.QueryValueEx(key, REG_VALUE)
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write(self, payload):
        if winreg is None:
            return False
        key = None
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_SUBKEY, 0, winreg.KEY_WRITE)
            winreg.SetValueEx(key, REG_VALUE, 0, winreg.REG_SZ,
                              json.dumps(payload, ensure_ascii=False))
        except OSError:
            return False
        finally:
            if key is not None:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass
        return True


def system_dir():
    """绑定记录的系统目录；LOCALAPPDATA 不可用时退回用户主目录隐藏目录。"""
    base = os.getenv('LOCALAPPDATA') or os.getenv('APPDATA')
    if base:
        return Path(base) / PRODUCT_DIR
    return Path.home() / ('.' + PRODUCT_DIR.lower())


def machine_id():
    """本机指纹哈希（t83 稳定化）：只用硬件硬令牌，不再拿主机名/MAC 当身份。

    根因：旧实现 = sha(主机名 | uuid.getnode() 的 MAC | 架构 | 平台)。
    同机改名、插拔网卡/虚拟网卡、VPN、Wi-Fi 与有线切换都会让 MAC/主机名变化，
    于是同一台电脑被算成「新机器」，触发假的「检测到换过电脑 · 第 N 次迁移」。
    现在：CPU 型号 + 主板序列号 + MachineGuid（都来自 HKLM，重装系统前不变）；
    硬件读不到时才退回旧的 host/MAC（并标记降级），绝不再让软令牌单独决定身份。
    返回 (id 或 None, 是否降级)。
    """
    if os.getenv('IP_CLONE_FORCE_DEGRADED') == '1':
        return None, True
    try:
        import license  # 延迟导入避免循环依赖
        tokens = license.hardware_tokens()
        hard = tokens.get('hard') or {}
        soft = tokens.get('soft') or {}
        parts = [hard.get('cpu') or '', hard.get('board') or '']
        if any(parts):
            return _sha('|'.join(parts))[:16], not all((hard.get('cpu'), hard.get('board')))
        if soft.get('machine'):
            # 硬令牌都读不到时才用 MachineGuid 兜底（HKLM，重装系统前不变）
            return _sha(soft['machine'])[:16], True
    except Exception:
        pass
    try:
        host = platform.node() or ''
        node = '%012x' % uuid.getnode()
        raw = '|'.join((host, node, platform.machine() or '', sys.platform))
        if not host and node == '000000000000':
            return None, True
        return _sha(raw)[:16], True
    except Exception:
        return None, True


def legacy_machine_id():
    """旧版（host+MAC）指纹：只用于识别历史记录是否来自同一台机器，不再用于生成新身份。"""
    try:
        host = platform.node() or ''
        node = '%012x' % uuid.getnode()
        raw = '|'.join((host, node, platform.machine() or '', sys.platform))
        if not host and node == '000000000000':
            return ''
        return _sha(raw)[:16]
    except Exception:
        return ''


def fallback_id(root):
    """取不到机器特征时的兜底：包目录绝对路径哈希（只用于首次生成）。"""
    return _sha(str(Path(root).resolve()).lower())[:16]


def buyer_id():
    data = _read_json(ROOT / 'buyer_id.json') or {}
    value = data.get('id')
    return value if isinstance(value, str) else ''


def _migration_count(data):
    value = data.get('migrations')
    if isinstance(value, list):
        return len(value)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _migration_events(data):
    events = data.get('migration_events')
    if isinstance(events, list):
        return [item for item in events if isinstance(item, dict)][-EVENT_KEEP:]
    value = data.get('migrations')
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)][-EVENT_KEEP:]
    return []


def _canon(record):
    """完整性哈希的规范字段；顺序固定，改字段时要同步升级 SCHEMA。"""
    return '|'.join((
        str(record.get('schema') or ''),
        str(record.get('install_id') or ''),
        str(record.get('buyer') or ''),
        str(record.get('first_install_ts') or ''),
        str(record.get('last_seen_ts') or ''),
        str(record.get('migrations') or 0),
        str(record.get('confirmed_at') or ''),
        str(record.get('pending_confirm') or ''),
    ))


def _seal(record):
    record = dict(record)
    record['schema'] = SCHEMA
    record['integrity'] = _sha(_canon(record))
    return record


def _normalize(data):
    """把旧版/新版记录统一成内部结构；非 dict 返回 None。"""
    if not isinstance(data, dict):
        return None
    count = _migration_count(data)
    first_ts = _parse_ts(data.get('first_install_ts'),
                         _parse_ts(data.get('first_install'),
                                   _parse_ts(data.get('created'))))
    last_ts = _parse_ts(data.get('last_seen_ts'), _parse_ts(data.get('last_seen')))
    record = {
        'install_id': str(data.get('install_id') or '').strip(),
        'buyer': str(data.get('buyer') or ''),
        'first_install_ts': first_ts,
        'last_seen_ts': last_ts,
        'migrations': count,
        'events': _migration_events(data),
        'degraded': bool(data.get('degraded')),
        'confirmed_at': str(data.get('confirmed_at') or ''),
        'confirmed_note': str(data.get('confirmed_note') or ''),
        'pending_confirm': bool(data.get('pending_confirm')),
        'anomalies_total': int(data.get('anomalies_total') or 0)
        if str(data.get('anomalies_total') or '0').isdigit() else 0,
        'last_anomaly': str(data.get('last_anomaly') or ''),
        'integrity_ok': True,
        'legacy': not bool(data.get('integrity')),
    }
    if data.get('integrity'):
        probe = {
            'schema': data.get('schema'),
            'install_id': record['install_id'],
            'buyer': record['buyer'],
            'first_install_ts': record['first_install_ts'],
            'last_seen_ts': record['last_seen_ts'],
            'migrations': record['migrations'],
            'confirmed_at': record['confirmed_at'],
            'pending_confirm': record['pending_confirm'],
        }
        record['integrity_ok'] = _sha(_canon(probe)) == str(data.get('integrity'))
    return record


def _probe_writable(directory):
    """真正试写一次，区分"目录可写"和"目录不可写"，避免静默失败。"""
    directory = Path(directory)
    probe = directory / '.write-probe'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


def _log_path(root):
    return Path(root) / 'app' / '.runtime' / 'clone_guard.log'


def _log(root, entry):
    """包内追加 JSONL 日志；包目录也不可写时静默跳过（启动日志仍会打印）。"""
    path = _log_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name(path.name + '.1'))
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        return True
    except OSError:
        return False


def _message(state):
    if state['read_only']:
        return ('这份拷贝已在多台电脑上使用，已超出 %d 次迁移上限，当前为只读'
                '（查看与导出仍可用）；请联系获取新的安装包' % MAX_MIGRATIONS)
    bits = []
    if state.get('clock_anomaly'):
        hours = abs(state.get('clock_skew_seconds') or 0) / 3600.0
        bits.append('检测到系统时间比上次运行倒退约 %.1f 小时，已标记异常，请校准系统时间'
                    % hours)
    if state.get('needs_confirm'):
        codes = [code for code in state.get('anomalies') or [] if code in CONFIRM_CODES]
        labels = '、'.join(ANOMALY_LABELS.get(code, code) for code in codes[:3])
        if state.get('migrations'):
            bits.append('安装绑定记录异常（%s），迁移次数未清零（当前 %d/%d）；'
                        '如非本人操作，请运行 tools/clone_guard_admin.py confirm 完成确认'
                        % (labels, state['migrations'], state['migrations'] + state['remaining']))
        else:
            bits.append('安装绑定记录异常（%s）；未发生换机迁移，可直接继续使用'
                        % labels)
    if not state.get('system_writable', True):
        if state.get('fallback_used'):
            bits.append('系统目录不可写，绑定记录已降级保存到程序目录（不影响使用）')
        else:
            bits.append('系统目录和程序目录都不可写，绑定记录只能暂存在内存（重启后可能丢失）')
    if state.get('registry_writable') is False:
        bits.append('注册表不可写，已改用系统目录和包内标记校验（不影响使用）')
    if state.get('tag_writable') is False:
        bits.append('程序目录不可写，包内标记无法保存（仍以系统记录为准）')
    if state.get('migrations') and not state.get('needs_confirm'):
        bits.append('检测到这份拷贝换过电脑：已记录第 %d 次迁移，还可免费迁移 %d 次'
                    % (state['migrations'], state['remaining']))
    if state.get('id_rebound') and not state.get('migrations'):
        bits.append('安装标识已按本机硬件自动校正（同机漂移，不算换机、不占迁移次数）')
    if state.get('degraded'):
        bits.append('机器指纹读取失败，已降级为目录标识（不影响使用）')
    if not bits:
        if state.get('fresh'):
            return '安装绑定已建立（全新机器首次安装，迁移 0/%d）' % MAX_MIGRATIONS
        return '安装绑定正常（迁移 0/%d）' % MAX_MIGRATIONS
    return '；'.join(bits)


def _collect(root, registry, sys_dir):
    """读取三处绑定 + 降级备用的第四份，返回原始/规整后的副本。"""
    runtime = Path(root) / 'app' / '.runtime'
    raw = {
        'registry': registry.read(),
        'system': _read_json(Path(sys_dir) / 'install.json'),
        'fallback': _read_json(runtime / 'install_fallback.json'),
        'package': _read_json(runtime / 'install_tag.json'),
    }
    records = {name: _normalize(value) for name, value in raw.items()}
    return raw, records


def _build_record(install_id, buyer, count, first_ts, last_ts, degraded,
                  events, confirmed_at, confirmed_note, anomalies_total, last_anomaly,
                  pending_confirm=False, hardware=None, id_rebound=False, repaired_count=0):
    return {
        'schema': SCHEMA,
        'product': PRODUCT_DIR,
        'install_id': install_id,
        'buyer': buyer,
        'first_install': _fmt(first_ts),
        'first_install_ts': round(first_ts, 3),
        'last_seen': _fmt(last_ts),
        'last_seen_ts': round(last_ts, 3),
        'migrations': int(count),
        'migration_events': list(events or [])[-EVENT_KEEP:],
        'degraded': bool(degraded),
        'confirmed_at': confirmed_at or '',
        'confirmed_note': confirmed_note or '',
        'anomalies_total': int(anomalies_total or 0),
        'last_anomaly': last_anomaly or '',
        'pending_confirm': bool(pending_confirm),
        'id_rebound': bool(id_rebound),
        'false_migration_repaired': int(repaired_count or 0),
        # license.binding 写入的硬件绑定原样透传；不进 _canon，旧记录不会被判篡改。
        'hardware': hardware if isinstance(hardware, dict) else {},
    }


def repair_false_migration(root=None, note='本人确认未换电脑，清除误记迁移'):
    """把「同机标识漂移」误记的迁移次数清零（手动/主理人确认后执行，留审计）。

    只改 migrations 与 migration_events，不动首装时间/绑定；返回改前改后与写盘结果。
    """
    root = Path(root) if root else ROOT.parent
    sys_dir = system_dir()
    runtime = root / 'app' / '.runtime'
    targets = [sys_dir / 'install.json', runtime / 'install_fallback.json', runtime / 'install_tag.json']
    changed = []
    before = None
    for path in targets:
        record = _read_json(path)
        if not record:
            continue
        if before is None:
            before = int(record.get('migrations') or 0)
        record['migrations'] = 0
        record['migration_events'] = []
        record['false_migration_repaired'] = {
            'at': _now(), 'note': str(note or '')[:200],
            'previous_migrations': before}
        sealed = _seal({k: v for k, v in record.items() if k not in ('sig', 'integrity')})
        if _write_json(path, sealed):
            changed.append(str(path))
    return {'ok': bool(changed), 'before': before, 'after': 0, 'written': changed}


def check(root=None, registry=None, sys_dir=None):
    """交叉校验三处绑定，返回状态字典并落盘 clone_state.json / clone_guard.log。

    registry / sys_dir 仅供 tools/clone_guard_selftest.py 注入隔离存储，
    正常启动（tools/boot.py）不传这两个参数。
    """
    root = Path(root) if root else ROOT.parent
    runtime = root / 'app' / '.runtime'
    sys_dir = Path(sys_dir) if sys_dir else system_dir()
    registry = registry if registry is not None else WinRegistryStore()
    state_path = runtime / 'clone_state.json'

    now_ts = _now_ts()
    buyer = buyer_id()
    computed, degraded = machine_id()
    current_id = computed or fallback_id(root)

    raw, records = _collect(root, registry, sys_dir)
    # license.binding 的硬件绑定存在同一份 install.json 里；check() 会整体重写记录，
    # 必须先取出来再原样写回，否则下次启动就被覆盖、绑定永远不持久。
    hardware = {}
    for name in ('system', 'fallback', 'registry', 'package'):
        candidate = (raw.get(name) or {}).get('hardware') if isinstance(raw.get(name), dict) else None
        if isinstance(candidate, dict) and candidate:
            hardware = candidate
            break
    present = {name: value is not None for name, value in records.items()}
    present_sys = present['system'] or present['fallback']
    fresh = not (present['registry'] or present_sys or present['package'])
    pending_before = any(record['pending_confirm'] for record in records.values() if record)
    system_probe_ok = _probe_writable(sys_dir)

    anomalies = []
    if present['registry'] and not records['registry']['integrity_ok']:
        anomalies.append('record_tampered')
    if records['system'] and not records['system']['integrity_ok']:
        anomalies.append('record_tampered')
    if records['fallback'] and not records['fallback']['integrity_ok']:
        anomalies.append('record_tampered')
    if records['package'] and not records['package']['integrity_ok']:
        anomalies.append('record_tampered')

    # 缺失判定：只要还有别的副本存在，就不算新装。
    if registry.available() and not present['registry'] and (present_sys or present['package']):
        anomalies.append('missing_registry')
    if not present['system'] and (present['registry'] or present['package']) and system_probe_ok:
        anomalies.append('missing_system')
        if present['fallback']:
            anomalies.append('partial_records')
    if not present['package'] and (present['registry'] or present_sys):
        anomalies.append('missing_package_tag')

    ids = {name: records[name]['install_id'] for name in records if records[name]}
    counts = {name: records[name]['migrations'] for name in records if records[name]}
    max_count = max(counts.values()) if counts else 0
    if len(set(counts.values())) > 1:
        anomalies.append('count_mismatch')

    reg_id = ids.get('registry') or ''
    sys_id = ids.get('system') or ids.get('fallback') or ''
    tag_id = ids.get('package') or ''
    if reg_id and sys_id and reg_id != sys_id:
        anomalies.append('copy_conflict')

    first_values = [value for value in
                    (records[name]['first_install_ts'] for name in records if records[name])
                    if value]
    first_ts = min(first_values) if first_values else now_ts

    last_values = [value for value in
                   (records[name]['last_seen_ts'] for name in records if records[name])
                   if value]
    stored_last = max(last_values) if last_values else 0

    # 迁移判定：包内标记来自另一台机器 → 计一次迁移；三处计数取最大值，不清零。
    # 2026-11-17 t83：先看 license 绑定里的硬件硬令牌。硬件对得上就是同一台电脑，
    # 旧的「标识变了」只算重绑（rebind），绝不再计一次迁移；真换机（硬件对不上）才计。
    stored_hard = (hardware or {}).get('hard') if isinstance(hardware, dict) else {}
    current_hard = {}
    try:
        import license as _license
        current_hard = (_license.hardware_tokens().get('hard') or {})
    except Exception:
        current_hard = {}
    same_machine = bool(stored_hard) and bool(current_hard) and any(
        stored_hard.get(key) and stored_hard.get(key) == current_hard.get(key)
        for key in ('cpu', 'board'))
    if not same_machine and tag_id:
        # 老记录没有 hardware 或 hardware 未签名时：用旧算法重算一次，能对上就说明是同一台机器。
        legacy = legacy_machine_id()
        if legacy and legacy == tag_id:
            same_machine = True
    id_rebound = False
    repaired_count = 0
    migrated = False
    new_count = max_count
    if same_machine:
        if tag_id and tag_id != current_id:
            # 同机标识漂移：把三处 id 统一到当前值，不写迁移事件、不加迁移次数。
            id_rebound = True
            anomalies.append('id_rebound')
        if max_count > 0 and stored_hard:
            # 硬件证明是同一台电脑：历史「换过电脑」是旧指纹算法造成的误记，自动校正为 0 次，
            # 原迁移事件保留在 migration_events 里作审计（不删除证据）。
            repaired_count = max_count
            new_count = 0
    elif tag_id and tag_id != current_id:
        migrated = True
        new_count = max_count + 1
    elif not present['package'] and sys_id and sys_id != current_id and not present['registry']:
        # 只剩系统副本且与当前机器不符：疑似把系统文件跟着包一起拷走
        migrated = True
        new_count = max_count + 1
        anomalies.append('partial_records')
    elif not present['package'] and reg_id and reg_id != current_id and not present['system']:
        migrated = True
        new_count = max_count + 1
        anomalies.append('partial_records')
    elif (not present['package'] and sys_id and reg_id and sys_id == reg_id
          and sys_id != current_id):
        # 系统目录和注册表一致但对不上当前机器：更像改过主机名（指纹漂移），不重复计迁移
        anomalies.append('id_drift')

    # 时钟：只标记，不修改已记录的最后运行时间（避免把证据往回写）。
    clock_anomaly = False
    clock_skew = 0
    if stored_last and now_ts < stored_last - CLOCK_ROLLBACK_SECONDS:
        clock_anomaly = True
        clock_skew = stored_last - now_ts
        anomalies.append('clock_rollback')
    if first_ts > now_ts + CLOCK_ROLLBACK_SECONDS:
        clock_anomaly = True
        clock_skew = max(clock_skew, first_ts - now_ts)
        if 'clock_rollback' not in anomalies:
            anomalies.append('clock_rollback')

    events = []
    for name in ('system', 'fallback', 'registry', 'package'):
        if name in records and records[name] and records[name]['events']:
            events = records[name]['events']
            break
    if migrated:
        events = list(events) + [{'from': tag_id or reg_id or sys_id, 'to': current_id,
                                  'at': _fmt(now_ts), 'buyer': buyer}]
    if repaired_count:
        events = list(events) + [{'from': '', 'to': current_id, 'at': _fmt(now_ts),
                                  'repaired_false_migration': repaired_count,
                                  'reason': 'same_machine_id_drift'}]

    confirmed_at = ''
    confirmed_note = ''
    for name in ('system', 'fallback', 'registry', 'package'):
        record = records.get(name)
        if record and record['confirmed_at'] and not confirmed_at:
            confirmed_at = record['confirmed_at']
            confirmed_note = record['confirmed_note']
    anomalies_total = max([record['anomalies_total'] for record in records.values()
                           if record] or [0])
    last_anomaly = max([record['last_anomaly'] for record in records.values()
                        if record and record['last_anomaly']] or [''])
    if anomalies:
        anomalies_total += 1
        last_anomaly = anomalies[0]

    next_last = max(stored_last, now_ts) if now_ts else stored_last
    record = _build_record(current_id, buyer, new_count, first_ts, next_last,
                           degraded or any(record['degraded'] for record in records.values()
                                           if record),
                           events, confirmed_at, confirmed_note,
                           anomalies_total, last_anomaly, hardware=hardware,
                           id_rebound=id_rebound, repaired_count=repaired_count)
    sealed = _seal(record)

    # 三处都写一遍：任何一处缺失都会被重建，但计数只会取最大值，不会清零。
    system_writable = _write_json(sys_dir / 'install.json', sealed)
    fallback_used = False
    if not system_writable:
        fallback_used = _write_json(runtime / 'install_fallback.json', sealed)
        if not fallback_used:
            anomalies.append('system_readonly')
    else:
        # 系统目录可写时清掉旧的降级文件，避免它跟着包被拷走造成误判
        stale = runtime / 'install_fallback.json'
        if stale.exists():
            try:
                stale.unlink()
            except OSError:
                pass
    registry_writable = registry.write(sealed)
    if registry.available() and not registry_writable and 'registry_readonly' not in anomalies:
        anomalies.append('registry_readonly')
    tag_writable = _write_json(runtime / 'install_tag.json', sealed)
    if not tag_writable:
        anomalies.append('tag_readonly')

    needs_confirm_codes = [code for code in anomalies if code in CONFIRM_CODES]
    count = new_count
    state = {
        'status': 'readonly' if count > MAX_MIGRATIONS else (
            'abnormal' if (needs_confirm_codes or clock_anomaly)
            else ('migrated' if migrated else 'bound')),
        'read_only': count > MAX_MIGRATIONS,
        'migrations': count,
        'remaining': max(0, MAX_MIGRATIONS - count),
        'install_id': current_id,
        'install_short': current_id[:8],
        'degraded': bool(record['degraded']),
        'system_writable': system_writable,
        'registry_writable': registry_writable,
        'tag_writable': tag_writable,
        'fallback_used': fallback_used,
        'record_created': not present['system'],
        'buyer': buyer,
        'product': PRODUCT_DIR,
        'fresh': fresh,
        'needs_confirm': bool(needs_confirm_codes),
        'anomalies': anomalies,
        'anomaly_labels': [ANOMALY_LABELS.get(code, code) for code in anomalies],
        'clock_anomaly': clock_anomaly,
        'clock_skew_seconds': int(clock_skew),
        'sources': {'registry': present['registry'], 'system': present['system'],
                    'fallback': present['fallback'], 'package': present['package']},
        'first_install': record['first_install'],
        'last_seen': record['last_seen'],
        'first_install_ts': record['first_install_ts'],
        'last_seen_ts': record['last_seen_ts'],
        'migration_events': record['migration_events'],
        'anomalies_total': anomalies_total,
        'last_anomaly': last_anomaly,
        'confirmed_at': confirmed_at,
        'log': str(_log_path(root)),
        'hardware': hardware,
    }
    state['message'] = _message(state)
    _write_json(state_path, {'checked_at': _now(), **state})
    _log(root, {
        'at': _now(), 'event': 'check', 'status': state['status'],
        'migrations': count, 'fresh': fresh, 'id_rebound': bool(state.get('id_rebound')),
        'sources': state['sources'],
        'anomalies': anomalies, 'clock_anomaly': clock_anomaly,
        'clock_skew_seconds': state['clock_skew_seconds'],
        'writable': {'system': system_writable, 'registry': registry_writable,
                     'package': tag_writable},
        'install_short': current_id[:8], 'buyer': buyer,
    })
    LAST_STATE.clear()
    LAST_STATE.update(state)
    return state


def _record_from_state(state, note='', confirmed_at=''):
    return _build_record(
        state['install_id'], state.get('buyer', ''), state['migrations'],
        state.get('first_install_ts') or _now_ts(),
        state.get('last_seen_ts') or _now_ts(),
        state.get('degraded', False), state.get('migration_events') or [],
        confirmed_at or _now(), note, state.get('anomalies_total', 0),
        state.get('last_anomaly', ''), hardware=state.get('hardware') or {})


def confirm(root=None, note='', registry=None, sys_dir=None):
    """人工确认：把三处记录对齐并写入确认标记；绝不下调迁移次数、不解除只读。

    registry / sys_dir 同 check()，仅供自测注入。
    """
    root = Path(root) if root else ROOT.parent
    registry = registry if registry is not None else WinRegistryStore()
    sys_dir = Path(sys_dir) if sys_dir else system_dir()
    before = check(root, registry=registry, sys_dir=sys_dir)
    if before['read_only']:
        return {'ok': False,
                'message': '已超出迁移上限，人工确认不能解除只读；请联系卖家换新安装包',
                'before': before}
    record = _record_from_state(before, note=note, confirmed_at=_now())
    sealed = _seal(record)
    runtime = root / 'app' / '.runtime'
    writes = {
        'system': _write_json(sys_dir / 'install.json', sealed),
        'registry': registry.write(sealed),
        'package': _write_json(runtime / 'install_tag.json', sealed),
    }
    writes['fallback'] = False
    if not writes['system']:
        writes['fallback'] = _write_json(runtime / 'install_fallback.json', sealed)
    after = check(root, registry=registry, sys_dir=sys_dir)
    _log(root, {'at': _now(), 'event': 'confirm', 'note': note, 'writes': writes,
                'migrations': after['migrations'], 'status': after['status'],
                'anomalies': after['anomalies']})
    return {'ok': True, 'message': '已写入人工确认标记；迁移次数保持 %d 次，未清零'
            % after['migrations'], 'writes': writes, 'before': before, 'after': after}


def blocked(method, path, state):
    """只读态拦写请求；GET/HEAD 等查看与导出放行。"""
    if not state or not state.get('read_only'):
        return None
    if method not in ('POST', 'PATCH', 'PUT', 'DELETE'):
        return None
    clean = path.split('?', 1)[0].rstrip('/') or '/'
    if clean == '/api/access':
        return None
    return {'error': state.get('message'), 'code': 'clone_readonly',
            'clone': {'migrations': state.get('migrations'), 'remaining': 0,
                      'degraded': state.get('degraded'),
                      'needs_confirm': state.get('needs_confirm'),
                      'anomalies': state.get('anomalies')}}
