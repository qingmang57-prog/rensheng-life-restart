# -*- coding: utf-8 -*-
"""授权状态：本地试用计时 + 云端账号态；未登录/会话过期不再视为已付费。

范围（2026-11-03 确认，2026-11-16 收紧）：
- 纯本机计时，不做机器绑定、不发激活文件、不采集指纹、不做跨机器校验；
- 首次启动由 tools/boot.py 调 initialize() 写入 <IP_DATA_DIR>/trial.json；
- 第 1 天结束进入 expired：只能查看和导出，不能新增生成；
- 云端账号态优先：会员 active 解锁，试用 trial 起算，试用到期 expired 只读；
- 未登录、会话过期、云端不可达且云端无法给出账号态时，**回落到本地试用计时**，
  不再默认解锁（否则退出登录即可永久白用）；
- trial.json 记录“已见最大时间”，系统时间回拨超过容差即进入 tampered 并冻结只读；
- 解锁入口留给计费线：set_entitlement_provider(fn)，fn 返回真值即视为已激活。
  未接入前恒为本地试用，本模块不自行编造解锁逻辑。

边界说明：HMAC 只防手改文件，不防删文件；删掉 trial.json 需重新首启才会重建，
这是不做机器绑定的已知取舍。
"""
import hashlib
import hmac
import json
import math
import os
import platform
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 环境仅用于自测/静态检查
    winreg = None

TRIAL_DAYS = 1          # 2026-11-15：免费体验 1 天，注册成功即起算（云端落库）
TRIAL_SECONDS = TRIAL_DAYS * 24 * 3600
ROLLBACK_TOLERANCE = 600      # 秒：系统校时的小幅回拨不判作弊
MAX_SEEN_PERSIST = 3600       # 秒：运行中最多每小时把最大时间落盘一次
SIGN_KEY = 'ipwb-trial-v1-20261103'   # 弱校验密钥，随包分发，仅用于识别手改
ROOT = Path(__file__).resolve().parent
RUNTIME_MAX = [0.0]           # 进程内已见最大时间（不落盘也能挡住同进程回拨）
ENTITLEMENT = None            # 计费线注入：返回真值即已解锁
MEMBERSHIP = None             # 云端会员/试用注入：返回 {state, vip_level, vip_expires_at, trial_*} 
NO_TRIAL = False              # 2026-11-16：恢复本地试用计时；未登录/会话过期按试用走，不再等同已付费


def _data_dir():
    return Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))


def _file():
    return _data_dir() / 'trial.json'


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')


def _sign(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(SIGN_KEY.encode('utf-8'), raw, hashlib.sha256).hexdigest()


def _save(payload):
    payload = {k: v for k, v in payload.items() if k not in ('sig', '_broken')}
    record = dict(payload)
    record['sig'] = _sign(payload)
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    return record


def _load():
    path = _file()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'_broken': True}
    if not isinstance(data, dict):
        return {'_broken': True}
    payload = {k: v for k, v in data.items() if k != 'sig'}
    signature = data.get('sig', '')
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _sign(payload)):
        payload['_broken'] = True
    return payload


def _ts(value, fallback):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return value if math.isfinite(value) and value > 0 else fallback


def _fresh(first_ts, now_ts):
    return {'v': 1, 'first_start_ts': first_ts, 'first_start': _iso(first_ts),
            'max_seen_ts': max(first_ts, now_ts), 'max_seen': _iso(max(first_ts, now_ts)),
            'tampered': False, 'tampered_at': None, 'tampered_reason': None}


def set_entitlement_provider(provider):
    """计费线接入点：provider() 返回真值（如 {'plan': 'pro'}）即视为已解锁。"""
    global ENTITLEMENT
    ENTITLEMENT = provider


def set_membership_provider(provider):
    """云端会员/试用接入点：provider() 返回当前账号的会员快照或 None。

    快照字段（云端 /api/auth/me 下发）：
      state            active / trial / expired / legacy
      vip_level        已废弃（2026-11-16）：仅作兼容字段透传，会员判定看 state
      vip_expires_at   会员到期 epoch 秒
      trial_ends_at    试用截止 epoch 秒
    未登录或老账号（legacy）返回 None，此时沿用本地逻辑（默认已解锁），
    不把存量账号一次性锁成只读。
    """
    global MEMBERSHIP
    MEMBERSHIP = provider


def _membership():
    if MEMBERSHIP is None:
        return None
    try:
        return MEMBERSHIP() or None
    except Exception:
        return None


def _cloud_state(now_ts):
    """账号态优先：会员 > 试用 > 试用到期；legacy/未登录返回 None 走本地兜底。"""
    m = _membership()
    if not m:
        return None
    mode = str(m.get('state') or '')
    try:
        vip = int(m.get('vip_level') or 0)
    except (TypeError, ValueError):
        vip = 0
    if mode == 'active':
        expires = _ts(m.get('vip_expires_at'), 0)
        return {'mode': 'active', 'days_left': None, 'read_only': False, 'days': TRIAL_DAYS,
                'first_start': None,
                'expires_at': _iso(expires) if expires else None,
                'checked_at': _iso(now_ts), 'source': 'cloud', 'vip_level': vip,
                'message': '会员权益生效中，导出不限'}
    if mode == 'trial':
        ends = _ts(m.get('trial_ends_at'), 0)
        left = max(0.0, ends - now_ts)
        hours = max(1, int(math.ceil(left / 3600.0)))
        return {'mode': 'trial', 'days_left': 0, 'read_only': False, 'days': TRIAL_DAYS,
                'first_start': _iso(_ts(m.get('trial_started_at'), ends - TRIAL_SECONDS)),
                'expires_at': _iso(ends) if ends else None, 'hours_left': hours,
                'checked_at': _iso(now_ts), 'source': 'cloud', 'vip_level': 0,
                'message': '免费体验 1 天，剩余 %d 小时' % hours}
    if mode == 'expired':
        ends = _ts(m.get('trial_ends_at'), now_ts)
        return {'mode': 'expired', 'days_left': 0, 'read_only': True, 'days': TRIAL_DAYS,
                'first_start': _iso(_ts(m.get('trial_started_at'), ends - TRIAL_SECONDS)),
                'expires_at': _iso(ends), 'checked_at': _iso(now_ts), 'source': 'cloud',
                'vip_level': 0,
                'message': '免费体验 1 天已结束，开通年度会员可继续使用；已有内容可查看，素材与剪映草稿仍可导出，成片导出与新增生成暂停'}
    return None


def entitlement():
    """当前权益：NO_TRIAL 关闭时只看计费线注入；未注入返回 None（走本地试用/过期判定）。"""
    if NO_TRIAL:
        return {'source': 'no_trial'}
    if ENTITLEMENT is None:
        return None
    try:
        return ENTITLEMENT() or None
    except Exception:
        return None


def _build_state(payload, now_ts):
    ent = entitlement()
    if ent:
        return {'mode': 'active', 'days_left': None, 'read_only': False, 'days': TRIAL_DAYS,
                'first_start': _iso(_ts((payload or {}).get('first_start_ts'), now_ts)),
                'expires_at': None, 'checked_at': _iso(now_ts), 'source': 'entitlement',
                'message': '已解锁，无试用期限'}
    if payload is None:
        return {'mode': 'tampered', 'days_left': 0, 'read_only': True, 'days': TRIAL_DAYS,
                'first_start': None, 'expires_at': None, 'checked_at': _iso(now_ts), 'source': None,
                'message': '试用记录缺失，已按只读处理'}
    first = _ts(payload.get('first_start_ts'), now_ts)
    expires_ts = first + TRIAL_SECONDS
    tampered = bool(payload.get('tampered')) or bool(payload.get('_broken'))
    if tampered:
        reason = payload.get('tampered_reason') or ('试用记录校验失败（文件被改动或损坏），已冻结为只读'
                                                     if payload.get('_broken')
                                                     else '检测到系统时间回拨，已冻结为只读（只能查看和导出）')
        return {'mode': 'tampered', 'days_left': 0, 'read_only': True, 'days': TRIAL_DAYS,
                'first_start': _iso(first), 'expires_at': _iso(expires_ts), 'checked_at': _iso(now_ts),
                'source': 'trial', 'message': reason}
    remaining = expires_ts - now_ts
    if remaining <= 0:
        return {'mode': 'expired', 'days_left': 0, 'read_only': True, 'days': TRIAL_DAYS,
                'first_start': _iso(first), 'expires_at': _iso(expires_ts), 'checked_at': _iso(now_ts),
                'source': 'trial', 'message': '已到期，只能查看和导出'}
    return {'mode': 'trial', 'days_left': max(0, math.ceil(remaining / 86400)), 'read_only': False,
            'days': TRIAL_DAYS, 'first_start': _iso(first), 'expires_at': _iso(expires_ts),
            'checked_at': _iso(now_ts), 'source': 'trial',
            'message': '试用剩余 %d 天（到期后只能查看和导出）' % max(0, math.ceil(remaining / 86400))}


def initialize():
    """首启写记录、更新已见最大时间、识别回拨。由 tools/boot.py 在 app.py 启动前调用。"""
    now_ts = time.time()
    payload = _load()
    if payload is None:
        payload = _fresh(now_ts, now_ts)
        RUNTIME_MAX[0] = payload['max_seen_ts']
        _save(payload)
        return _build_state(payload, now_ts)
    broken = bool(payload.pop('_broken', False))
    if broken:
        first = _ts(payload.get('first_start_ts'), now_ts)
        payload = _fresh(first, now_ts)
        payload['max_seen_ts'] = max(now_ts, _ts(payload.get('max_seen_ts'), now_ts))
        payload['tampered'] = True
        payload['tampered_at'] = _iso(now_ts)
        payload['tampered_reason'] = '试用记录校验失败（文件被改动或损坏），已冻结为只读'
    elif now_ts < _ts(payload.get('max_seen_ts'), 0) - ROLLBACK_TOLERANCE:
        payload['tampered'] = True
        payload['tampered_at'] = _iso(now_ts)
        payload['tampered_reason'] = '检测到系统时间回拨，已冻结为只读（只能查看和导出）'
    payload['max_seen_ts'] = max(now_ts, _ts(payload.get('max_seen_ts'), now_ts))
    payload['max_seen'] = _iso(payload['max_seen_ts'])
    RUNTIME_MAX[0] = payload['max_seen_ts']
    _save(payload)
    return _build_state(payload, now_ts)


def state():
    """当前授权状态：{mode: trial|active|expired|tampered, days_left, read_only, message, ...}。"""
    now_ts = time.time()
    cloud = _cloud_state(now_ts)
    if cloud is not None:
        return cloud
    payload = _load()
    if payload is None:
        return _build_state(None, now_ts)
    broken = bool(payload.pop('_broken', False))
    payload['_broken'] = broken
    seen = max(RUNTIME_MAX[0], _ts(payload.get('max_seen_ts'), 0))
    if now_ts > RUNTIME_MAX[0]:
        RUNTIME_MAX[0] = now_ts
    if not broken and not payload.get('tampered') and now_ts < seen - ROLLBACK_TOLERANCE:
        payload['tampered'] = True
        payload['tampered_at'] = _iso(now_ts)
        payload['tampered_reason'] = '检测到系统时间回拨，已冻结为只读（只能查看和导出）'
        payload['max_seen_ts'] = seen
        payload['max_seen'] = _iso(seen)
        _save(payload)
    elif not broken and not payload.get('tampered') and now_ts > _ts(payload.get('max_seen_ts'), 0) + MAX_SEEN_PERSIST:
        payload['max_seen_ts'] = now_ts
        payload['max_seen'] = _iso(now_ts)
        _save(payload)
    return _build_state(payload, now_ts)


def status_text(st=None):
    """启动日志用的一行中文状态。"""
    st = st or state()
    tag = '[授权]' if st.get('mode') == 'active' else '[试用]'
    return tag + ' ' + st.get('message', '状态未知')


BINDING_DIR_NAME = 'IPWorkbench'
SOFT_CONTACT = '请联系客服核对（不影响当前使用）'


def binding_dir():
    """硬件绑定记录的系统目录，与 clone_guard 使用同一个目录。"""
    base = os.getenv('LOCALAPPDATA') or os.getenv('APPDATA')
    return (Path(base) / BINDING_DIR_NAME) if base else (Path.home() / '.ipworkbench')


def _binding_paths(root=None, sys_dir=None):
    root = Path(root) if root else ROOT.parent
    system = (Path(sys_dir) if sys_dir else binding_dir()) / 'install.json'
    tag = root / 'app' / '.runtime' / 'install_tag.json'
    return [system, tag]


def _sha16(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _win_value(path, name):
    if winreg is None:
        return ''
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | getattr(winreg, 'KEY_WOW64_64KEY', 0)) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return str(value or '').strip()
    except OSError:
        return ''


def _board_serial():
    """主板序列号；wmic 约 75ms，失败返回空串不阻断。"""
    if os.name != 'nt':
        return platform.machine()
    kwargs = {'capture_output': True, 'timeout': 6}
    if hasattr(subprocess, 'CREATE_NO_WINDOW'):
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    # 2026-11-17 t83：wmic 在 Win11 可能被移除/禁用，先用 wmic，失败再用 PowerShell CIM 兜底，
    # 避免「有时读得到、有时读不到」导致同机指纹漂移。
    try:
        result = subprocess.run(['wmic', 'baseboard', 'get', 'serialnumber'], **kwargs)
        lines = [line.strip() for line in result.stdout.decode('gbk', 'ignore').splitlines() if line.strip()]
        value = lines[1] if len(lines) > 1 and lines[1].lower() != 'serialnumber' else ''
        if value:
            return value
    except Exception:
        pass
    try:
        script = ('(Get-CimInstance Win32_BaseBoard -ErrorAction Stop).SerialNumber')
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], **kwargs)
        value = (result.stdout or b'').decode('utf-8', 'ignore').strip().splitlines()
        value = value[0].strip() if value else ''
        return '' if value.lower() in ('', 'serialnumber', 'none') else value
    except Exception:
        return ''


def _volume_serial(root=None):
    if os.name != 'nt':
        return ''
    try:
        import ctypes
        drive = ((Path(root).drive if root else Path(ROOT).drive) or 'C:') + '\\'
        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), None, 0, ctypes.byref(serial), None, None, None, 0)
        return '%08X' % serial.value if ok else ''
    except Exception:
        return ''


def hardware_tokens(root=None):
    """采集硬件指纹并只返回 sha256 前 16 位。

    硬令牌：CPU 型号 + 主板序列号；软令牌：卷序列号 + MachineGuid。
    任何一项读不到就留空，由 binding() 按降级处理，不阻断启动。
    """
    if os.name == 'nt':
        cpu = _win_value(r'HARDWARE\DESCRIPTION\System\CentralProcessor\0', 'ProcessorNameString')
        board = _board_serial()
        volume = _volume_serial(root)
        machine = _win_value(r'SOFTWARE\Microsoft\Cryptography', 'MachineGuid')
    else:
        cpu = platform.processor()
        board = platform.machine()
        volume = ''
        machine = (platform.node() or '') + '|' + ('%012x' % uuid.getnode())
    token = lambda value: _sha16(value) if value else ''
    return {'hard': {'cpu': token(cpu), 'board': token(board)},
            'soft': {'volume': token(volume), 'machine': token(machine)}}


def _binding_sign(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hmac.new(SIGN_KEY.encode('utf-8'), raw, hashlib.sha256).hexdigest()


def _binding_verify(hardware):
    payload = {key: hardware.get(key) for key in ('hard', 'soft', 'bound_at')}
    return hmac.compare_digest(str(hardware.get('sig') or ''), _binding_sign(payload))


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, payload):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        return True
    except OSError:
        return False


def _read_binding(paths):
    """优先取签名有效的一份；都无效时返回第一份坏记录用于提示。"""
    fallback = None
    for path in paths:
        record = _read_json(path)
        hardware = record.get('hardware') if record else None
        if not isinstance(hardware, dict) or not hardware:
            continue
        if _binding_verify(hardware):
            return hardware, str(path), True
        if fallback is None:
            fallback = (hardware, str(path), False)
    return fallback or (None, None, False)


def _write_binding(paths, hardware):
    """合并写回：保留 clone_guard 写入的 install_id/完整性哈希等字段，只替换 hardware。"""
    written = []
    for path in paths:
        record = _read_json(path) or {}
        record['hardware'] = hardware
        if _write_json(path, record):
            written.append(str(path))
    return written


def _seal_binding(hard, soft, bound_at):
    payload = {'hard': dict(hard or {}), 'soft': dict(soft or {}), 'bound_at': bound_at}
    hardware = dict(payload)
    hardware['sig'] = _binding_sign(payload)
    return hardware


def binding(root=None, sys_dir=None):
    """首绑写入与漂移校验：硬令牌任一匹配即同机；两个硬令牌都变才算异常。

    只返回状态和一句人话，不抛异常、不阻断使用；install.json 由 clone_guard
    整体重写时会原样保留 hardware 字段，因此绑定可以跨启动持久。
    """
    current = hardware_tokens(root)
    paths = _binding_paths(root, sys_dir)
    stored, source, signature_ok = _read_binding(paths)
    if stored is None:
        hardware = _seal_binding(current['hard'], current['soft'], round(time.time(), 3))
        written = _write_binding(paths, hardware)
        return {'status': 'fresh', 'ok': bool(written), 'message': '首次绑定已建立，绑定信息只存本机',
                'hardware': hardware, 'written': written}
    if not signature_ok:
        return {'status': 'tampered', 'ok': False,
                'message': '绑定信息校验失败，' + SOFT_CONTACT, 'hardware': stored, 'source': source}
    if not any((stored.get('hard') or {}).values()):
        if any(current['hard'].values()):
            hardware = _seal_binding(current['hard'], current['soft'], stored.get('bound_at') or round(time.time(), 3))
            _write_binding(paths, hardware)
            return {'status': 'bound', 'ok': True, 'message': '安装绑定已补齐硬件信息',
                    'hardware': hardware, 'source': source}
        return {'status': 'degraded', 'ok': False,
                'message': '硬件信息读取失败，本次跳过绑定校验', 'hardware': stored, 'source': source}
    if not any(current['hard'].values()):
        return {'status': 'degraded', 'ok': False,
                'message': '硬件信息读取失败，本次跳过绑定校验', 'hardware': stored, 'source': source}
    stored_hard = stored.get('hard') or {}
    hits = [key for key in ('cpu', 'board')
            if stored_hard.get(key) and stored_hard.get(key) == current['hard'].get(key)]
    changed = [key for key in ('cpu', 'board')
               if stored_hard.get(key) and current['hard'].get(key) and stored_hard.get(key) != current['hard'].get(key)]
    if hits:
        stored_soft = stored.get('soft') or {}
        drift = any(stored_soft.get(key) and stored_soft.get(key) != current['soft'].get(key)
                    for key in ('volume', 'machine'))
        hardware = _seal_binding(stored_hard, current['soft'], stored.get('bound_at') or round(time.time(), 3))
        if drift:
            _write_binding(paths, hardware)
        return {'status': 'bound', 'ok': True,
                'message': '安装绑定正常，软令牌已自动更新' if drift else '安装绑定正常',
                'hardware': hardware, 'source': source}
    if len(changed) >= 2:
        return {'status': 'mismatch', 'ok': False,
                'message': '硬件指纹与首次绑定不一致，' + SOFT_CONTACT, 'hardware': stored, 'source': source}
    return {'status': 'drift', 'ok': False,
            'message': '硬件指纹有变化，' + SOFT_CONTACT, 'hardware': stored, 'source': source}
