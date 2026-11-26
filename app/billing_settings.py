# -*- coding: utf-8 -*-
"""计费运营配置：试用赠送额度、兑换码风控参数。

与 billing_pricing.json 分开：价目表只放单价，运营参数放这里，改配置不需要动业务代码。
"""
import copy
import json
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'src' / 'billing_config.json'
DEFAULTS = {
    'free_seats': 50,
    'trial_grant_credits': 0,
    'trial_grant_note': '2026-11-16 起停止新增发放；存量余额保留可用',
    'redeem': {
        'code_bytes': 10,
        'max_failures': 5,
        'failure_window_seconds': 600,
        'lock_seconds': 900,
    },
}
_CACHE = {'stamp': None, 'data': None}


def load(path=None):
    path = Path(path) if path else PATH
    try:
        stat = path.stat()
    except OSError:
        return copy.deepcopy(DEFAULTS)
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _CACHE['stamp'] == stamp and _CACHE['data'] is not None:
        return copy.deepcopy(_CACHE['data'])
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raw = {}
    data = copy.deepcopy(DEFAULTS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == 'redeem' and isinstance(value, dict):
                data['redeem'].update(value)
            else:
                data[key] = value
    _CACHE.update(stamp=stamp, data=data)
    return copy.deepcopy(data)
