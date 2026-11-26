# -*- coding: utf-8 -*-
"""create_desktop_shortcut.py —— 在桌面创建「人生重启系统」快捷入口。

用法：
  python tools\\create_desktop_shortcut.py             # 已存在则跳过（启动器每次自动调用）
  python tools\\create_desktop_shortcut.py --force     # 已存在也重建（刷新目标/图标）
  python tools\\create_desktop_shortcut.py --desktop <临时目录>   # 测试：重定向桌面

设计（2026-11-26，客户反馈「进文件夹找 start.bat 双击不正规」）：
  - 快捷方式名：桌面「人生重启系统.lnk」
  - 目标：包根「启动工作台-无黑窗.vbs」（缺文件回退 start.bat）
  - 图标：app\\web\\brand\\app.ico（缺文件则不设图标，不算失败）
  - 桌面目录：SHGetKnownFolderPath(FOLDERID_DESKTOP)，含 OneDrive 重定向场景
  - 创建动作交给 Windows PowerShell 的 WScript.Shell COM；脚本用 -EncodedCommand
    （UTF-16LE base64）传递，中文路径不经命令行代码页，杜绝乱码
  - 幂等：lnk 已存在即跳过；用户删掉后下次启动自动补建
  - 任何失败都不抛异常：打印一行结果，退出码 0 成功 / 1 失败，绝不影响主程序启动
"""
import argparse
import base64
import ctypes
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tools
ROOT = HERE.parent                              # 包根
LNK_NAME = "人生重启系统"
TARGET_VBS = ROOT / "启动工作台-无黑窗.vbs"
TARGET_BAT = ROOT / "start.bat"
ICON = ROOT / "app" / "web" / "brand" / "app.ico"
CREATE_NO_WINDOW = 0x08000000


def desktop_dir(override=None):
    """真实桌面目录；override 仅供测试重定向。失败回退 %USERPROFILE%\\Desktop。"""
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    try:
        class _KFID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]
        fid = _KFID(0xB4BFCC3A, 0xDB2C, 0x424C,
                    (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(ptr)):
            raise OSError("SHGetKnownFolderPath != 0")
        path = Path(ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(ptr)
        if path.is_dir():
            return path
    except Exception:
        pass
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Desktop"


def build_ps(desktop, target):
    icon = str(ICON) if ICON.is_file() else ""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$d = '%s'" % str(desktop),
        "$lnkPath = Join-Path $d '%s.lnk'" % LNK_NAME,
        "$ws = New-Object -ComObject WScript.Shell",
        "$s = $ws.CreateShortcut($lnkPath)",
        "$s.TargetPath = '%s'" % str(target),
        "$s.WorkingDirectory = '%s'" % str(ROOT),
        "$s.Description = '%s'" % LNK_NAME,
    ]
    if icon:
        lines.append("$s.IconLocation = '%s,0'" % icon)
    lines.append("$s.Save()")
    lines.append("Write-Output 'CREATED'")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="在桌面创建「人生重启系统」快捷方式")
    ap.add_argument("--force", action="store_true", help="已存在也重建")
    ap.add_argument("--desktop", help="重定向桌面目录（测试用）")
    args = ap.parse_args(argv)
    try:
        target = TARGET_VBS if TARGET_VBS.is_file() else TARGET_BAT
        if not target.is_file():
            print("NO-TARGET: no launcher found (vbs/start.bat)")
            return 1
        desktop = desktop_dir(args.desktop)
        lnk = desktop / (LNK_NAME + ".lnk")
        if lnk.exists() and not args.force and not args.desktop:
            print("EXISTS: %s" % lnk)
            return 0
        enc = base64.b64encode(build_ps(desktop, target).encode("utf-16-le")).decode("ascii")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode == 0 and lnk.exists():
            print(("UPDATED: " if args.force else "CREATED: ") + str(lnk))
            print("TARGET: %s" % target)
            if ICON.is_file():
                print("ICON: %s" % ICON)
            return 0
        print("FAILED rc=%s out=%s err=%s" % (proc.returncode, out, err))
        return 1
    except Exception as exc:
        print("FAILED: %r" % (exc,))
        return 1


if __name__ == "__main__":
    sys.exit(main())
