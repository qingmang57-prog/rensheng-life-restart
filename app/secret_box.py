# -*- coding: utf-8 -*-
"""Windows DPAPI 小封装：把本地保存的云端令牌加密成「只有本机本用户能解」的密文。

- 只用 ctypes 调系统 crypt32，不引入新依赖；
- 密文带 `enc1:` 前缀，便于老数据识别与平滑迁移；
- 解密失败一律返回空串（当作未登录），绝不回显、不写日志。
"""
import base64
import ctypes
import os

PREFIX = 'enc1:'


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_char))]


def _call(data, unprotect):
    if os.name != 'nt':
        raise OSError('DPAPI 仅在 Windows 可用')
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    buffer = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    if unprotect:
        ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError('DPAPI 调用失败（错误码 %d）' % ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def seal(text):
    """明文 → 'enc1:<base64>'；空值原样返回。失败返回空串，调用方需自行决定是否降级。"""
    raw = (text or '').strip()
    if not raw:
        return ''
    try:
        return PREFIX + base64.b64encode(_call(raw.encode('utf-8'), False)).decode('ascii')
    except OSError:
        return ''


def unseal(value):
    """'enc1:<base64>' → 明文；兼容旧的明文值（原样返回）；解不开返回空串。"""
    text = str(value or '')
    if not text.startswith(PREFIX):
        return text.strip()
    try:
        return _call(base64.b64decode(text[len(PREFIX):]), True).decode('utf-8')
    except (OSError, ValueError):
        return ''
