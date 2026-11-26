#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共用桌面外壳启动器（人生重启系统 / 切片工坊 共用一份代码）。

职责：
  1. 读产品配置（configs/*.json），解析占位符；
  2. 单实例互斥 + 端口占用处理；
  3. 启动后端、3 次重试、就绪后通知窗口；
  4. 拉起原生 WebView2 窗口宿主（host/LauncherHost.exe）；
  5. 托盘由窗口宿主负责；WebView2 缺失时给中文引导页并回落浏览器；
  6. watchdog 30 秒探活，连续两次失败重启后端；
  7. 开机自启读写 HKCU Run（默认关闭）。

只做外壳，不改产品业务代码。Python 3.8+，仅依赖标准库；托盘依赖包内 pystray/Pillow。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.wintypes as wt
import faulthandler
import html
import json
import logging
import os
import socket
import subprocess
import port_logic
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VENDOR = HERE / "vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
# 启动那一刻有没有控制台：pythonw 双击进入时为 False。要在任何重定向之前记下来。
_HAS_CONSOLE = sys.stderr is not None

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

kernel32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]

log = logging.getLogger("launcher")


# ---------------------------------------------------------------- 基础工具

def resolve(value, ctx):
    if isinstance(value, str):
        try:
            return value.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return value
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


def load_config(path: Path, port_override=None, host_override=None):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    ctx = {
        "root": str(ROOT),
        "product_id": raw.get("product_id", "product"),
        "port": port_override if port_override else raw.get("port", 8791),
        "host": host_override if host_override else raw.get("host", "127.0.0.1"),
    }
    cfg = resolve(raw, ctx)
    # 后端命令不在这里解析：换端口/换 host 后要用当前值重新拼，否则会拿旧端口去绑，
    # Windows 上表现为 WinError 10013（而不是 10048）。
    if raw.get("backend", {}).get("command"):
        cfg["_backend_template"] = raw["backend"]["command"]
    if port_override:
        cfg["port"] = int(port_override)
    if host_override:
        cfg["host"] = host_override
    return cfg


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "launcher.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(stream)


def message_box(text, title):
    try:
        # MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND：启动早期没有窗口，
        # 不加 TOPMOST 时对话框会被浏览器/其它窗口压在后面，用户根本看不到
        user32.MessageBoxW(None, text, title, 0x40 | 0x40000 | 0x10000)
    except Exception:
        pass


_STDIO_SINK = None


def redirect_stdio(log_dir: Path):
    """pythonw 没有控制台：入口被双击时 sys.stdout/sys.stderr 是 None。

    2026-11-16 实测：此时任何未捕获异常（例如某个 run 目录文件被占用 / 只读）都只会
    写进一个不存在的 stderr，结果就是「launcher.log 只有配置一行，然后静默退出」。
    这里把 stdout/stderr（含 fd 1/2 和 faulthandler）接到 launcher-error.log，
    保证入口失败一定留下可读文本。控制台运行时（python.exe）保持原样，不改行为。
    """
    global _STDIO_SINK
    if sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        sink = open(log_dir / "launcher-error.log", "a", encoding="utf-8",
                    errors="replace", buffering=1)
    except Exception:
        return None
    _STDIO_SINK = sink
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink
    for fd in (1, 2):
        try:
            os.dup2(sink.fileno(), fd)
        except OSError:
            pass
    try:
        # 原生层崩溃（WebView2 / 驱动 DLL）也落盘，否则同样一声不吭
        faulthandler.enable(sink)
    except Exception:
        pass
    return sink


def write_crash_report(exc, cfg, argv):
    """把未捕获异常写成一段人能看懂的失败记录，返回日志路径。"""
    try:
        log_dir = Path(cfg["log_dir"]) if (cfg and cfg.get("log_dir")) else HERE
    except Exception:
        log_dir = HERE
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "launcher-error.log"
        admin = False
        try:
            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            pass
        with path.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write("\n===== %s 启动失败\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            fh.write("python=%s console=%s admin=%s\n"
                     % (sys.executable, _HAS_CONSOLE, admin))
            fh.write("cwd=%s\nargv=%s\n" % (os.getcwd(), " ".join(argv)))
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            fh.flush()
        return str(path)
    except Exception:
        return ""


def focus_window(title: str) -> bool:
    seen = []

    @WNDENUMPROC
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == title:
            # 隐藏到托盘的窗口也收：优先取可见的那个
            seen.append((bool(user32.IsWindowVisible(hwnd)), hwnd))
        return True

    user32.EnumWindows(cb, 0)
    if not seen:
        return False
    visible = [hwnd for shown, hwnd in seen if shown]
    hwnd = visible[0] if visible else seen[0][1]
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


def port_probe(port: int, host: str = "0.0.0.0"):
    """探测端口可否监听，返回 (可用, 原因码)。
    原因码：""=可用；"blocked"=被安全软件/系统策略拦（WinError 10013）；
    "occupied"=被其他程序占用（10048 等）。
    只 bind 不够：本机实测 8800 能 bind、后端 listen 时报 WinError 10013，
    端口被系统/安全软件拦时 bind 仍可能成功，所以这里补一次 listen。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, port))
        s.listen(1)
        return True, ""
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10013:
            return False, "blocked"
        return False, "occupied"
    finally:
        s.close()


def port_free(port: int) -> bool:
    return port_probe(port)[0]


# 2026-11-20：netsh 查 TCP 保留端口段（Hyper-V/WSL/WinNAT 会动态保留整段端口，
# 保留段里的端口 bind/listen 直接被拒，与被占用无法区分，必须提前跳过）。
_EXCLUDED_RANGES = None
_EXCLUDED_OK = None


def windows_excluded_ranges():
    """返回 [(起, 止)]；查询失败/超时返回 []，并置 _EXCLUDED_OK=False。

    2026-11-24 t191：bytes 读 + GBK 优先（netsh 中文输出不是 UTF-8），
    失败与「无保留段」严格区分：失败降级为不过滤，绝不卡死（timeout=10）。
    """
    global _EXCLUDED_RANGES, _EXCLUDED_OK
    if _EXCLUDED_RANGES is not None:
        return _EXCLUDED_RANGES
    text, ok = "", False
    try:
        proc = subprocess.run(
            ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
            capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW)
        raw = proc.stdout or b""
        # 2026-11-24 t192：数字端口直接从 bytes 行解析，不依赖 GBK/UTF-8 解码成功
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        ok = proc.returncode == 0
        if not ok:
            log.warning("查询系统保留端口段失败（exit=%s），降级为不过滤", proc.returncode)
    except Exception as exc:
        log.warning("查询系统保留端口段失败/超时，降级为不过滤：%s", exc)
        ok = False
    _EXCLUDED_RANGES = port_logic.parse_excluded(raw if raw else text) if ok else []
    _EXCLUDED_OK = ok
    return _EXCLUDED_RANGES


def excluded_scan_ok():
    """True=netsh 成功（空列表代表无保留段）；False=查询失败已降级。"""
    return bool(_EXCLUDED_OK)

ZONE_STREAM = ":Zone.Identifier"
# 只抽查几个必定存在的锚点文件，避免每次启动遍历上万文件
MOTW_ANCHORS = ("start.bat", "app/web/version.json", "tools/launcher/launcher.py")


def zone_id(path: Path):
    """读文件的 Zone.Identifier 备用流；没有联网标记返回空串。"""
    try:
        with open(str(path) + ZONE_STREAM, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.strip().lower().startswith("zoneid="):
                    return line.strip().split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def motw_marked():
    """抽查锚点文件的联网标记；命中即认为整包来自网络下载。"""
    return [rel for rel in MOTW_ANCHORS if zone_id(ROOT / rel)]


def unblock_tree(root: Path) -> int:
    """删除整棵树的 Zone.Identifier 备用流，返回删除数量。只在用户明确点“是”后调用。"""
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            stream = os.path.join(dirpath, name) + ZONE_STREAM
            if not os.path.exists(stream):
                continue
            try:
                os.remove(stream)
                removed += 1
            except OSError:
                continue
    return removed


def confirm_box(text, title):
    """是/否对话框；返回用户是否点了“是”。"""
    try:
        # MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
        return user32.MessageBoxW(None, text, title,
                                  0x04 | 0x30 | 0x40000 | 0x10000) == 6  # IDYES
    except Exception:
        return False


def _pywebview_ready():
    """外壳依赖是否真的能加载：webview 能 import 不代表 .NET 那层能起，所以连 clr 一起试。"""
    try:
        import webview  # noqa: F401
        import clr  # noqa: F401
        return True
    except Exception:
        return False


def detect_webview2():
    force = os.environ.get("IP_WEBVIEW2_FORCE", "").strip().lower()
    if force == "missing":
        return None
    if force == "installed":
        return "forced"

    sub = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
                         (winreg.HKEY_CURRENT_USER, r"Software"),
                         (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE")):
        try:
            with winreg.OpenKey(hive, prefix + "\\" + sub) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and pv != "0.0.0.0":
                    return str(pv)
        except OSError:
            continue

    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if not base:
            continue
        folder = Path(base) / "Microsoft" / "EdgeWebView" / "Application"
        if folder.is_dir():
            versions = sorted([p.name for p in folder.iterdir() if p.is_dir()])
            if versions:
                return versions[-1]
    return None


def make_icon(path: Path, letter: str, background: str, foreground: str):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 128
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([2, 2, size - 2, size - 2], radius=26, fill=background)
        font = None
        for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "segoeui.ttf"):
            try:
                font = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name), 62)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        box = draw.textbbox((0, 0), letter, font=font)
        x = (size - (box[2] - box[0])) / 2 - box[0]
        y = (size - (box[3] - box[1])) / 2 - box[1]
        draw.text((x, y), letter, font=font, fill=foreground)
        img.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128)])
        return True
    except Exception as exc:
        log.warning("生成图标失败：%s", exc)
        return False


def autostart_state(value_name: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ) as k:
            try:
                winreg.QueryValueEx(k, value_name)
                return True
            except FileNotFoundError:
                return False
    except OSError:
        return False


def set_autostart(value_name: str, command: str, enabled: bool):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, value_name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(k, value_name)
            except FileNotFoundError:
                pass
    log.info("开机自启 -> %s", "开" if enabled else "关")


# ---------------------------------------------------------------- HTML 模板

def _data_uri(path, mime="image/png"):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}" if data else ""


# 外壳自绘标题条的唯一实现放在 app/web，splash / 失败页 / 重连页直接内联同一份，
# 避免"某一页有标题条、另一页没有"或两套外观慢慢走样。
TITLEBAR_DIR = Path(__file__).resolve().parents[2] / "app" / "web"


def titlebar_head():
    try:
        css = (TITLEBAR_DIR / "shell-titlebar.css").read_text(encoding="utf-8")
    except OSError:
        return ""
    return f"<style>{css}</style>"


def titlebar_body(cfg):
    try:
        js = (TITLEBAR_DIR / "shell-titlebar.js").read_text(encoding="utf-8")
    except OSError:
        return ""
    # NavigateToString 没有站点根，/brand/*.png 取不到，logo 走 data URI
    init = {"name": cfg.get("product_name", ""), "logo": _data_uri(TITLEBAR_DIR / "brand" / "logo-32.png")}
    return ("<script>window.__SHELL_TITLEBAR__=%s;</script>\n<script>%s</script>"
            % (json.dumps(init, ensure_ascii=False), js))


def apply_taskbar_icon(icon_file, timeout=30.0):
    """把品牌 .ico 挂到 pywebview 顶层窗口上（WM_SETICON），修任务栏白纸图标。

    2026-11-15 根因：pywebview 的 edgechromium 窗口是 WinForms 窗体，没设 Icon，
    任务栏按钮就用系统默认「白纸」图标；托盘走 pystray、自带 image，所以托盘正常、
    只有任务栏空白。Windows 任务栏图标只认顶层窗口的 ICON_BIG/ICON_SMALL，
    必须在窗口真正创建后按进程号找 HWND 再发消息，不能只传 create_window 参数。
    """
    if os.name != "nt":
        return False
    path = Path(str(icon_file or ""))
    if not path.is_file():
        log.warning("窗口图标文件不存在：%s", path)
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        wndproc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        user32.EnumWindows.argtypes = [wndproc, wt.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        user32.IsWindowVisible.argtypes = [wt.HWND]
        user32.GetWindowTextLengthW.argtypes = [wt.HWND]
        user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                      ctypes.c_int, ctypes.c_int, wt.UINT]
        user32.LoadImageW.restype = wt.HANDLE

        pid = os.getpid()
        deadline = time.time() + float(timeout)
        hwnd = None
        while not hwnd and time.time() < deadline:
            found = []

            def _cb(handle, _param):
                owner = wt.DWORD()
                user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
                if (owner.value == pid and user32.IsWindowVisible(handle)
                        and user32.GetWindowTextLengthW(handle)):
                    found.append(handle)
                    return False
                return True

            user32.EnumWindows(wndproc(_cb), 0)
            hwnd = found[0] if found else None
            if not hwnd:
                time.sleep(0.5)
        if not hwnd:
            log.warning("未找到窗口句柄，任务栏图标未设置")
            return False

        image_icon, load_from_file, default_size = 1, 0x10, 0x40
        wm_seticon, icon_small, icon_big = 0x0080, 0, 1
        big = user32.LoadImageW(None, str(path), image_icon, 0, 0,
                                load_from_file | default_size)
        small = user32.LoadImageW(None, str(path), image_icon, 16, 16, load_from_file)
        if big:
            user32.SendMessageW(hwnd, wm_seticon, icon_big, big)
        if small:
            user32.SendMessageW(hwnd, wm_seticon, icon_small, small)
        log.info("任务栏图标已设置：%s", path)
        return True
    except Exception as exc:
        log.warning("设置任务栏图标失败：%s", exc)
        return False


def display_path(path):
    """把安装目录内的绝对路径改成相对写法：界面提示里不出现打包机/本机路径。

    2026-11-15：启动失败卡片直接显示了 log_dir 的绝对路径，客户能看到打包机目录。
    安装目录之外的路径只保留末级目录名，仍能定位问题但不再暴露上层目录。
    """
    try:
        rel = os.path.relpath(str(path), str(ROOT))
    except ValueError:
        return Path(str(path)).name
    if rel.startswith(".."):
        return Path(str(path)).name
    return "\\" + rel.replace("/", "\\")


def splash_html(cfg):
    s = cfg["splash"]
    # 启动页恒为深色（与标题条/登录门禁同一套暖黑），不跟主界面皮肤切换
    accent = s.get("accent", "#e3b341")
    bg = s.get("background", "#100F0D")
    text = s.get("text", "#fbf8f1")
    muted = s.get("muted", "#8f887c")
    title = html.escape(s.get("title", cfg.get("product_name", "")))
    subtitle = html.escape(s.get("subtitle", "正在启动"))
    letter = html.escape(cfg.get("icon", {}).get("letter", "人"))
    logo = f'<div class="logo">{letter}</div>'
    shot = _data_uri(s.get("image", "")) if s.get("image") else ""
    if shot:
        logo = f'<img class="shot" src="{shot}" alt="">'
    # 深色启动页直接放 on-dark 产品标，不垫黑色圆角方块
    mark = _data_uri(s.get("logo", "")) if s.get("logo") else ""
    if mark:
        logo = f'<img class="mark" src="{mark}" alt="">'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  html,body{{height:100%;margin:0;background:{bg};color:{text};
    font-family:"Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif;-webkit-user-select:none;user-select:none;}}
  .wrap{{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px}}
  .logo{{width:78px;height:78px;border-radius:20px;background:{accent};color:#1a1916;display:flex;
    align-items:center;justify-content:center;font-size:38px;font-weight:700;
    box-shadow:0 10px 30px {accent}33;animation:pulse 2.4s ease-in-out infinite}}
  .shot{{width:124px;height:124px;border-radius:26px;object-fit:cover;
    box-shadow:0 14px 36px #00000066;animation:pulse 3.2s ease-in-out infinite}}
  .mark{{width:78px;height:78px;object-fit:contain;border:0;border-radius:0;background:none;
    box-shadow:none;animation:pulse 3.2s ease-in-out infinite}}
  @keyframes pulse{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.04)}}}}
  h1{{margin:0;font-size:22px;letter-spacing:1px}}
  .sub{{color:{muted};font-size:13px;margin-top:-6px}}
  .bar{{width:280px;height:6px;border-radius:99px;background:#ffffff14;overflow:hidden}}
  .bar i{{display:block;height:100%;width:6%;background:{accent};border-radius:99px;transition:width .45s ease}}
  .msg{{font-size:13px;color:{text};min-height:20px}}
  .tip{{position:fixed;bottom:14px;font-size:11px;color:{muted}}}
  #fail{{display:none;width:560px;max-width:86%;margin:8vh auto 0;background:#1a1916;border:1px solid #2c2823;border-radius:14px;padding:26px 30px;
    box-shadow:0 16px 40px #00000066;text-align:left}}
  #fail h2{{font-size:20px;margin:0 0 10px;color:{text}}}
  #fail p{{color:#b7aea0;font-size:13px;line-height:1.7;margin:6px 0}}
  #fail .path{{background:#100f0d;border:1px solid #2c2823;border-radius:8px;padding:10px 12px;font-size:12px;word-break:break-all;color:#cfc8bb}}
  #fail button{{border:0;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;margin:14px 8px 0 0}}
  #fail .primary{{background:{accent};color:#1a1916}} #fail .ghost{{background:#26241f;color:#d9d2c4}}
</style>{titlebar_head()}</head>
<body>{titlebar_body(cfg)}<div class="wrap" id="boot">
  {logo}
  <h1>{title}</h1>
  <div class="sub">{subtitle}</div>
  <div class="bar"><i id="bar"></i></div>
  <div class="msg" id="msg">正在启动…</div>
</div>
<div id="fail">
  <h2>{title} 启动失败</h2>
  <p id="failmsg">服务没有在预期时间内就绪。</p>
  <p id="faildetail" style="white-space:pre-line;color:#d9d2c4;font-size:12px;margin:8px 0"></p>
  <p>日志在这个位置（可复制发给技术支持）：</p>
  <div class="path" id="faillog"></div>
  <button class="primary" onclick="retry()">重新启动</button>
  <button class="ghost" onclick="openlog()">打开日志目录</button>
  <button class="ghost" onclick="copylog()">复制日志路径</button>
  <button class="ghost" onclick="packlog()">打包诊断日志</button>
  <button class="ghost" onclick="useBrowser()">改用浏览器打开</button>
  <p style="margin-top:16px;color:#8a8797;font-size:12px">连不上或反复失败时，优先点「打包诊断日志」：桌面会生成一个 zip，发给我们即可。</p>
</div>
<div class="tip">关闭窗口会最小化到托盘</div>
<script>
var msgEl = document.getElementById('msg');
function setBar(p){{
  var bar = document.getElementById('bar');
  if(bar) bar.style.width = Math.max(4, Math.min(100, p)) + '%';
}}
function showFail(s){{
  document.getElementById('boot').style.display = 'none';
  document.getElementById('fail').style.display = 'block';
  if(s && s.message) document.getElementById('failmsg').textContent = s.message;
  var fd = document.getElementById('faildetail');
  if(fd) fd.textContent = (s && s.detail) || '';
  document.getElementById('faillog').textContent = (s && s.log) || '';
}}
async function tick(){{
  if(!window.pywebview || !window.pywebview.api){{ setTimeout(tick, 300); return; }}
  var s = null;
  try {{ s = await window.pywebview.api.state(); }} catch(e) {{}}
  if(s){{
    if(typeof s.progress === 'number') setBar(s.progress);
    if(s.message) msgEl.textContent = s.message;
    if(s.phase === 'ready' && s.url){{ location.replace(s.url); return; }}
    if(s.phase === 'failed'){{ showFail(s); return; }}
  }}
  setTimeout(tick, 700);
}}
async function retry(){{
  document.getElementById('fail').style.display = 'none';
  document.getElementById('boot').style.display = 'flex';
  setBar(6); msgEl.textContent = '正在重新启动…';
  try {{ await window.pywebview.api.retry(); }} catch(e) {{}}
  setTimeout(tick, 500);
}}
function openlog(){{ try {{ window.pywebview.api.openlog(); }} catch(e) {{}} }}
function copylog(){{ try {{ window.pywebview.api.copylog(); }} catch(e) {{}} }}
function packlog(){{ try {{ window.pywebview.api.bundlelog(); }} catch(e) {{}} }}
function useBrowser(){{ try {{ window.pywebview.api.browser(); }} catch(e) {{}} }}
tick();
</script></body></html>"""


def status_html(cfg, title, message):
    accent = cfg["splash"].get("accent", "#6A60BE")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>html,body{{height:100%;margin:0;background:#F6F5FA;color:#2B2A33;
font-family:"Microsoft YaHei UI",sans-serif;display:flex;align-items:center;justify-content:center}}
.box{{text-align:center}} .dot{{width:46px;height:46px;border-radius:50%;border:4px solid {accent}33;
border-top-color:{accent};margin:0 auto 18px;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}} h2{{font-size:18px;margin:0 0 8px}}
p{{color:#7A7788;font-size:13px;margin:0}}</style>{titlebar_head()}</head>
<body>{titlebar_body(cfg)}<div class="box"><div class="dot"></div><h2>{html.escape(title)}</h2><p>{html.escape(message)}</p></div></body></html>"""


def failure_html(cfg, detail):
    title = html.escape(cfg.get("product_name", "程序"))
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>html,body{{height:100%;margin:0;background:#F6F5FA;color:#2B2A33;
font-family:"Microsoft YaHei UI",sans-serif;display:flex;align-items:center;justify-content:center}}
.card{{width:560px;background:#fff;border-radius:14px;padding:28px 30px;box-shadow:0 16px 40px #00000012}}
h1{{font-size:20px;margin:0 0 10px}} p{{color:#55525f;font-size:13px;line-height:1.7;margin:6px 0}}
.path{{background:#F3F2F8;border-radius:8px;padding:10px 12px;font-size:12px;word-break:break-all;color:#4a4756}}
button{{border:0;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;margin:14px 8px 0 0}}
.primary{{background:#6A60BE;color:#fff}} .ghost{{background:#ECEAF6;color:#4a4756}}</style>{titlebar_head()}</head>
<body>{titlebar_body(cfg)}<div class="card">
<h1>{title} 启动失败</h1>
<p>{html.escape(detail)}</p>
<p>日志在这个位置（可复制发给技术支持）：</p>
<div class="path">{html.escape(str(cfg.get("_log_dir_display", "")))}</div>
<button class="primary" onclick="post('retry')">重新启动</button>
<button class="ghost" onclick="post('openlog')">打开日志目录</button>
<button class="ghost" onclick="post('copylog')">复制日志路径</button>
<button class="ghost" onclick="post('browser')">改用浏览器打开</button>
<p style="margin-top:16px;color:#8a8797;font-size:12px">如果连续失败，请先看日志里最后 20 行；也可以直接用浏览器打开排查。</p>
</div>
<script>function post(m){{ if(window.chrome && window.chrome.webview) window.chrome.webview.postMessage(m); }}</script>
</body></html>"""


def guide_html(cfg, ready, detail=""):
    title = html.escape(cfg.get("product_name", "程序"))
    url = f"http://127.0.0.1:{cfg['port']}/"
    guide = cfg.get("webview2", {}).get("guide_url", "https://developer.microsoft.com/zh-cn/microsoft-edge/webview2/")
    app_button = (f'<a class="primary" href="{html.escape(url)}">用浏览器打开工作台</a>'
                  if ready else '<span class="disabled">后端未就绪，暂时不能打开</span>')
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title} · 需要安装 WebView2</title>
<style>body{{margin:0;background:#F6F5FA;color:#2B2A33;font-family:"Microsoft YaHei UI",sans-serif}}
.card{{max-width:680px;margin:8vh auto;background:#fff;border-radius:16px;padding:32px 36px;
box-shadow:0 18px 50px #00000014}} h1{{font-size:22px;margin:0 0 14px}}
p{{font-size:14px;line-height:1.9;color:#4d4a56}} .hint{{background:#F3F2F8;border-radius:10px;padding:12px 16px;font-size:13px}}
a{{display:inline-block;margin:16px 10px 0 0;padding:10px 18px;border-radius:9px;text-decoration:none;font-size:14px}}
.primary{{background:#6A60BE;color:#fff}} .ghost{{background:#ECEAF6;color:#4a4756}}
.disabled{{display:inline-block;margin-top:16px;color:#8a8797;font-size:13px}}
small{{color:#8a8797}}</style></head>
<body><div class="card">
<h1>还差一个 Windows 组件：WebView2 运行时</h1>
<p>{title} 用的是和 Edge 同一个内核（WebView2），没有它就无法打开独立窗口。安装一次即可，之后不用再装。</p>
<div class="hint">步骤：点下面的“下载 WebView2 运行时” → 打开微软官方页面 → 下载 Evergreen Bootstrapper → 双击安装 → 回来点“用浏览器打开工作台”或重新双击程序。</div>
<a class="primary" href="{html.escape(guide)}" target="_blank" rel="noopener">下载 WebView2 运行时（微软官方）</a>
{app_button}
<p style="margin-top:22px"><small>安装包是微软官方提供的免费组件，不需要付费。{html.escape(detail)}</small></p>
</div></body></html>"""


# ---------------------------------------------------------------- JS 桥

def set_clipboard(text):
    """把文本放进剪贴板（CF_UNICODETEXT），失败静默。"""
    GMEM_MOVEABLE, CF_UNICODETEXT = 0x0002, 13
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalLock.restype = wt.LPVOID
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE
    try:
        user32.OpenClipboard(None)
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
        user32.CloseClipboard()
        return True
    except Exception as exc:
        log.warning("写剪贴板失败：%s", exc)
        try:
            user32.CloseClipboard()
        except Exception:
            pass
        return False


# ---- 无边框窗口的坐标换算（自绘标题条用）
class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL


def _hwnd(window):
    """取 pywebview 窗口的原生句柄；拿不到就返回 0（调用方各自兜底）。"""
    try:
        return int(window.native.Handle.ToInt64())
    except Exception:
        return 0


def _dpi_scale(hwnd):
    """物理像素 / 逻辑像素。老系统没有 GetDpiForWindow 时按 96 DPI 算。"""
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) or 96
    except Exception:
        dpi = 96
    return dpi / 96.0


def _pin_maximized_bounds(window):
    """无边框窗体自己最大化会盖住任务栏，把最大化范围钉在当前显示器工作区。"""
    try:
        native = window.native
        import clr  # noqa: F401  pythonnet 已随 pywebview 加载，这里只为拿到 System 命名空间
        from System.Drawing import Rectangle
        from System.Windows.Forms import Screen
        area = Screen.FromHandle(native.Handle).WorkingArea
        native.MaximizedBounds = Rectangle(area.X, area.Y, area.Width, area.Height)
    except Exception as exc:
        log.warning("设置最大化范围失败（不影响使用）：%s", exc)


class ShellApi:
    """splash/失败页的 JS 桥：只读状态 + 四个按钮动作。

    注意：属性一律下划线开头。pywebview 会把 js_api 对象的公开属性当成可调用层级
    递归展开，`app.window.native.DefaultFont…` 会一路走到 WinForms 控件里，
    实测会触发 pythonnet 的字体解析递归、刷满日志并让外壳进程崩掉。
    """

    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010

    def __init__(self, app):
        self._app = app

    def state(self):
        return self._app.read_state()

    def retry(self):
        self._app.request_retry()
        return True

    def openlog(self):
        self._app.open_log_dir()
        return True

    def copylog(self):
        return self._app.copy_log_path()

    def browser(self):
        self._app.open_in_browser()
        return True

    def bundlelog(self):
        # A3（2026-11-21）：失败页「打包诊断日志」按钮；zip 放桌面并复制路径。
        # 打包动作在 pywebview 的 JS 桥线程里执行，日志目录通常只有几十 MB，秒级完成。
        try:
            path = self._app.bundle_logs()
            message_box(
                "诊断包已生成，路径已复制到剪贴板：\n\n" + path +
                "\n\n请把这个 zip 文件发给技术支持。", "人生重启 诊断包")
            return path
        except Exception as exc:
            message_box(
                "诊断包生成失败：" + str(exc)[:200] +
                "\n\n可手动压缩日志文件夹发给技术支持。\n日志目录：" +
                str(self._app.display_log_dir), "人生重启 诊断包")
            return ""

    # ---- 自绘标题条：最小化 / 最大化还原 / 关闭（缩托盘）/ 八向缩放
    # 无边框窗口（frameless）丢了系统标题栏和系统缩放边，这几件事只能由壳层补。
    def shell_min(self):
        self._app.window.minimize()
        return True

    def shell_max(self):
        window = self._app.window
        hwnd = _hwnd(window)
        if hwnd and user32.IsZoomed(hwnd):
            window.restore()
        else:
            _pin_maximized_bounds(window)
            window.maximize()
        return {"maximized": bool(hwnd and user32.IsZoomed(hwnd))}

    def shell_close(self):
        # 走 window.destroy -> closing 事件 -> 隐藏到托盘，后端不停
        self._app.window.destroy()
        return True

    def shell_bounds(self):
        window = self._app.window
        hwnd = _hwnd(window)
        if not hwnd:
            return {}
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        scale = _dpi_scale(hwnd)
        win_cfg = self._app.cfg.get("window", {})
        return {
            "x": round(rect.left / scale),
            "y": round(rect.top / scale),
            "w": round((rect.right - rect.left) / scale),
            "h": round((rect.bottom - rect.top) / scale),
            "min_w": int(win_cfg.get("min_width", 980)),
            "min_h": int(win_cfg.get("min_height", 640)),
            "maximized": bool(user32.IsZoomed(hwnd)),
        }

    def shell_set_bounds(self, x, y, w, h):
        window = self._app.window
        hwnd = _hwnd(window)
        if not hwnd:
            return False
        win_cfg = self._app.cfg.get("window", {})
        w = max(int(w), int(win_cfg.get("min_width", 980)))
        h = max(int(h), int(win_cfg.get("min_height", 640)))
        scale = _dpi_scale(hwnd)
        user32.SetWindowPos(hwnd, None,
                            int(round(int(x) * scale)), int(round(int(y) * scale)),
                            int(round(w * scale)), int(round(h * scale)),
                            self.SWP_NOZORDER | self.SWP_NOACTIVATE)
        return True


# ---------------------------------------------------------------- 主流程

class Launcher:
    def __init__(self, cfg):
        self.cfg = cfg
        self.product_id = cfg.get("product_id", "product")
        self.port = int(cfg["port"])
        self.base_port = self.port
        self.tried_ports = set()
        self.host = cfg.get("host", "127.0.0.1")
        self.health_path = cfg["health"]["path"]
        self.health_match = cfg["health"].get("match", "")
        self.log_dir = Path(cfg["log_dir"])
        cfg["_log_dir"] = self.log_dir
        # 界面上只显示相对安装目录的写法（\.runtime\launcher\xxx），绝对路径留在内部用
        self.display_log_dir = display_path(self.log_dir)
        cfg["_log_dir_display"] = self.display_log_dir
        self.run_dir = self.log_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.run_dir / "state.json"
        self.retry_flag = self.run_dir / "retry.flag"
        self.splash_path = self.run_dir / "splash.html"
        self.failure_path = self.run_dir / "failure.html"
        self.connecting_path = self.run_dir / "connecting.html"
        self.guide_path = self.run_dir / "webview2-guide.html"
        self.host_ini = self.run_dir / "host.ini"
        self.icon_path = self.run_dir / "icon.ico"
        self.mutex = None
        self.window = None
        self.tray = None
        self.proc = None
        self.proc_log = None
        self.attach_existing = False
        self.focused_existing = False
        self.starting = False
        self.failed = False
        self.exiting = False
        self.lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.started_at = 0.0

    # ---- 状态与健康检查
    def write_state(self, phase, message="", progress=None, detail=""):
        state = {
            "phase": phase,
            "message": message,
            "detail": detail or f"日志：{self.display_log_dir}",
            "url": f"http://127.0.0.1:{self.port}/",
            "log": self.display_log_dir,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if progress is not None:
            state["progress"] = progress
        # t151：启动重试/进度线程可能并发写；用唯一临时文件＋锁＋替换重试，
        # 避免 WinError 5 导致 progress_loop 线程直接死掉、窗口停在旧状态。
        tmp = self.state_path.with_name(self.state_path.name + '.' + str(os.getpid()) + '.' + str(threading.get_ident()) + '.tmp')
        with self.state_lock:
            for attempt in range(6):
                try:
                    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                    os.replace(str(tmp), str(self.state_path))
                    break
                except OSError as exc:
                    if attempt >= 5:
                        log.warning("写 state 失败：%s", exc)
                        try: tmp.unlink()
                        except OSError: pass
                        break
                    time.sleep(0.05)
        log.info("state=%s %s", phase, message)

    def health(self, timeout_sec=None):
        # 2026-11-17（t50 根因修复）：探活超时可传参。默认 2s 在 ASR/GPU 重负载下会误判
        # 假死（08:38 事故诱因）；watchdog 用长超时，重启前再用更长超时复核。
        url = f"http://127.0.0.1:{self.port}{self.health_path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ipo-launcher"})
            with urllib.request.urlopen(req, timeout=int(timeout_sec if timeout_sec is not None else self.cfg["health"].get("timeout_sec", 2))) as resp:
                body = resp.read(4096).decode("utf-8", "ignore")
                return resp.status == 200 and (not self.health_match or self.health_match in body)
        except Exception:
            return False

    # ---- 单实例
    def acquire_single_instance(self):
        name = self.cfg["mutex_name"]
        self.mutex = kernel32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if err == ERROR_ALREADY_EXISTS:
            # t151：已有 launcher 但后端可能已死/卡住。第二次双击写 retry.flag，
            # 让已存在的 launcher 的 retry_loop 立刻重新拉起后端，再聚焦窗口。
            try:
                self.retry_flag.parent.mkdir(parents=True, exist_ok=True)
                self.retry_flag.write_text("1", encoding="utf-8")
            except OSError:
                pass
            title = self.cfg.get("window_title", self.cfg.get("product_name", ""))
            if not focus_window(title):
                message_box("程序已经在运行（可能最小化到了任务栏右下角托盘）。", title)
            return False
        return True

    def close_mutex(self):
        if self.mutex:
            kernel32.CloseHandle(self.mutex)
            self.mutex = None

    # ---- 端口
    def choose_port(self):
        if self.health():
            # t113：后端已在跑（例如 boot.py 裸起）时，快捷方式也必须开原生窗。
            self.attach_existing = True
            title = self.cfg.get("window_title", self.cfg.get("product_name", ""))
            if focus_window(title):
                self.focused_existing = True
                log.info("端口 %s 已有后端和原生窗，聚焦已有窗口", self.port)
            else:
                log.info("端口 %s 已有后端但没有原生窗，创建 attach 原生窗", self.port)
            return self.port
        fallback = int(self.cfg.get("port_fallback_count", 10))
        candidates = port_logic.candidate_ports(self.base_port, fallback)
        reserved = windows_excluded_ranges()
        self.port_probes = []
        chosen, probes = port_logic.pick_port(candidates, reserved, port_probe, self.tried_ports)
        self.port_probes.extend(probes)
        if chosen is None:
            # 兜底：让 OS 分配临时端口，再真实 bind/listen 验证（不猜、不碰系统保留段）
            chosen = port_logic.ephemeral_port(port_probe)
            if chosen is None:
                return None
            log.warning("候选端口耗尽，使用系统分配端口 %s", chosen)
        if chosen != self.port:
            log.warning("端口 %s 不可用，改用 %s", self.port, chosen)
        self.port = chosen
        self.tried_ports.add(chosen)
        return chosen

    def port_failure_report(self):
        """所有候选端口都不可用时，生成逐端口原因与对应解法（失败页/弹窗用）。"""
        probes = getattr(self, "port_probes", None) or []
        if not probes:
            return "未能确定原因，请联系支持并提供日志目录内容。"
        verdicts = {"reserved": "系统保留段（Hyper-V/WSL 等）占用",
                    "blocked": "被安全软件或系统策略拦截",
                    "occupied": "被其他程序占用"}
        lines = []
        for port, why, extra in probes[:10]:
            note = "（保留段 %s）" % extra if why == "reserved" and extra else ""
            lines.append("%s：%s%s" % (port, verdicts.get(why, why), note))
        kinds = {why for _, why, _ in probes}
        hints = []
        if "blocked" in kinds:
            hints.append("把整个软件文件夹加入杀毒软件/防火墙白名单后重试")
        if "occupied" in kinds:
            hints.append("关闭占用上述端口的其他程序（重启电脑通常也能释放）")
        if "reserved" in kinds:
            hints.append("重启电脑让系统重新分配保留段；仍不行请联系支持调整默认端口")
        return "\n".join(lines) + "\n建议：" + "；".join(hints) + "。"

    def diagnose_early_exit(self):
        """后端进程提前退出时的定向提示：本次启动无任何输出且关键文件缺失 ->
        解压不完整；无输出但文件齐全 -> 疑似安全软件拦截。有输出返回 None（看 backend.log）。"""
        base = getattr(self, "_spawn_log_size", None)
        if base is None:
            return None
        try:
            if (self.log_dir / "backend.log").stat().st_size > base:
                return None  # 后端有输出，死因看 backend.log
        except OSError:
            return None
        missing = [rel for rel in ("tools/python/python.exe", "tools/boot.py")
                   if not (ROOT / rel).is_file()]
        if missing:
            return ("文件缺失，解压不完整（缺 " + "、".join(missing) +
                    "）。请删除整个文件夹后重新完整解压：右键压缩包解压到新建的文件夹，"
                    "不要在压缩软件里直接双击运行。")
        return ("后端进程立即退出且没有任何输出，疑似被安全软件拦截。"
                "请把整个软件文件夹加入杀毒软件/防火墙白名单后重试。")

    # ---- 2026-11-25 资料外置：后端起来之前，把老客户包内的资料搬到包外资料夹
    def prepare_user_data(self):
        """返回 True=可以继续启动；False=停下（会打开一个空库的情况），原因已写进状态给人看。

        为什么放在启动器里做：客户资料可能有几个 GB，放进后端启动流程会被 wait_ready 的时限
        误判成「启动失败」；在这里做、做完才开始计启动时间，窗口上能看到「已搬多少」。
        走不到这里的两种情况本来就不该搬：端口上已有后端在应答（数据正被使用）；
        启动器重试（第 2、3 次）时已经做过。
        规则只有 tools/launch_root.py 一处；tools/boot.py 里再调一次是给「不经启动器直启」兜底。
        """
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            import launch_root
        except Exception:
            log.exception("资料夹定位模块加载失败，按包内旧方式继续")
            return True
        last = [0.0]

        def progress(done, total, name):
            now = time.time()
            if now - last[0] < 0.4 and done < total:
                return
            last[0] = now
            self.write_state(
                "starting", "正在把你的资料搬到软件文件夹外面（只需这一次）…",
                progress=4 + int(6 * done / max(1, total)),
                detail="已搬 %.1f%%（%s）\n资料会复制到「文档\\人生重启系统数据」，软件文件夹里的旧资料不会删除。"
                       % (100.0 * done / max(1, total), name))

        try:
            outcome = launch_root.prepare(ROOT, progress=progress)
        except Exception:
            log.exception("资料搬迁出错，按包内旧方式继续")
            return True
        for line in launch_root.describe(outcome):
            log.info("资料 | %s", line)
        self._data_home = outcome
        self._data_note = "\n".join(launch_root.describe(outcome))
        if not outcome.get("ok"):
            self.write_state("failed", "没有可用的资料文件夹，已停止启动（你的资料没有被改动）。",
                             progress=0, detail=str(outcome.get("error") or "") + "\n日志目录：" + self.display_log_dir)
            return False
        return True

    # ---- 后端
    def start_backend(self):
        # t165：端口已有监听进程时绝不 spawn 第二个后端（旧进程启动慢/健康探测暂失败时
        # 会触发 app.py 的 .backend.lock 单实例，spawn 进程随即退出，看着像“起不来”）。
        owner = self.port_owner_pid(self.port)
        if owner and owner != os.getpid():
            raise RuntimeError(f"端口 {self.port} 已有后端进程 PID {owner}，拒绝重复启动")
        ctx = {"root": str(ROOT), "product_id": self.product_id,
               "port": self.port, "host": self.host}
        cmd = resolve(self.cfg.get("_backend_template") or self.cfg["backend"]["command"], ctx)
        # .bat/.cmd 必须经 cmd.exe；引号形式：cmd /d /s /c ""带空格路径\start.bat" 参数…"
        # 多一层或少一层引号都会让 cmd 把整条命令当成文件名（实测 code=1）。
        if cmd[0].lower().endswith((".bat", ".cmd")):
            comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            inner = " ".join([f'"{cmd[0]}"'] + [f'"{c}"' if " " in c else c for c in cmd[1:]])
            launch = f'"{comspec}" /d /s /c "{inner}"'
        else:
            launch = cmd
        backend_log = self.log_dir / "backend.log"
        self.proc_log = open(backend_log, "ab", buffering=0)
        # 记录 spawn 前日志末尾位置：后端早退时据此区分「有输出（死因看 backend.log）」
        # 和「零输出（解压不完整/安全软件拦截）」
        self._spawn_log_size = self.proc_log.tell()
        self.proc = subprocess.Popen(
            launch, cwd=self.cfg["backend"].get("cwd", str(ROOT)),
            stdout=self.proc_log, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, close_fds=True,
        )
        log.info("后端启动 pid=%s port=%s host=%s", self.proc.pid, self.port, self.host)

    def stop_backend(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=15)
            except Exception as exc:
                log.warning("停止后端失败：%s", exc)
        try:
            if self.proc_log:
                self.proc_log.close()
        except Exception:
            pass
        self.proc_log = None

    # ---- 2026-11-17（t50 根因修复①）：重启前杀旧后端＋等端口释放
    def port_owner_pid(self, port):
        """找监听指定端口的 PID（netstat -ano，隐藏子进程）；找不到返回 None。"""
        try:
            # 2026-11-26（t20）：不开启 subprocess 文本模式——中文 Windows 系统命令输出
            # 是 GBK，文本模式按系统首选编码 strict 解码（客户机开「UTF-8 全局」模式时
            # 抛 UnicodeDecodeError 被裸 except 吞掉，恒返回 None → 端口误判被占 →
            # 启动重试耗尽）。改按 port_doctor.run_cmd 同款字节捕获 + GBK 优先解码链。
            proc = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                  capture_output=True, timeout=10,
                                  creationflags=CREATE_NO_WINDOW)
        except Exception:
            return None
        raw = proc.stdout or b""
        text = None
        if isinstance(raw, str):
            text = raw
        else:
            for enc in ("gbk", "utf-8", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        if text is None:
            log.warning("netstat 输出无法解码（GBK/UTF-8/Latin-1 全失败，%d 字节），端口 %s 占用者按未知处理",
                        len(raw), port)
            return None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].rsplit(":", 1)[-1] == str(port):
                    try:
                        return int(parts[4])
                    except ValueError:
                        return None
        return None

    def _image_name(self, pid):
        try:
            # 2026-11-26（t20）：同 port_owner_pid，字节捕获 + GBK 优先解码链，
            # 不开启 subprocess 文本模式（中文 Windows 下输出为 GBK）。
            proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                  capture_output=True, timeout=10,
                                  creationflags=CREATE_NO_WINDOW)
        except Exception:
            return ""
        raw = proc.stdout or b""
        text = None
        if isinstance(raw, str):
            text = raw
        else:
            for enc in ("gbk", "utf-8", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        if text is None:
            log.warning("tasklist 输出无法解码（GBK/UTF-8/Latin-1 全失败，%d 字节），pid=%s 映像名按未知处理",
                        len(raw), pid)
            return ""
        return text.strip().split('","')[0].strip('"').lower()

    def wait_port_release(self, port, timeout):
        """重启路径专用：端口被占时杀掉旧后端（含不是本外壳起的进程）并等释放。

        只杀 python/pythonw/cmd 这类本产品的后端链进程；无关进程占端口则不杀、返回 False，
        由调用方如实报「重启失败」，绝不换端口起第二实例共用同一 DATA。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if port_free(port):
                return True
            pid = self.port_owner_pid(port)
            if pid and pid != os.getpid():
                image = self._image_name(pid)
                if image in ("python.exe", "pythonw.exe", "cmd.exe"):
                    log.warning("重启：杀掉占用端口 %s 的旧后端 pid=%s (%s)", port, pid, image)
                    try:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=CREATE_NO_WINDOW, timeout=15)
                    except Exception as exc:
                        log.warning("杀旧后端失败：%s", exc)
                else:
                    log.warning("端口 %s 被非后端进程占用 pid=%s (%s)，不杀", port, pid, image)
                    return False
            time.sleep(1.0)
        return port_free(port)

    def wait_ready(self, timeout):
        # 2026-11-21（A1 自适应等待）：本进程拉起的后端仍活着、且 backend.log 还在
        # 增长 ⇒ 是在正常初始化（首启杀软全量查杀/机械盘冷启动可远超 90s 固定阈值）。
        # 此时到点不杀不重试（杀了重启会触发杀软重新扫描，越重试越慢），把截止时间
        # 顺延到 startup_max_wait_sec（默认 300s）；只有进程死掉或日志停止增长才按
        # 超时判失败。认领外部后端（self.proc 为 None）的路径维持原固定超时。
        hard_cap = time.time() + max(float(timeout),
                                     float(self.cfg["backend"].get("startup_max_wait_sec", 300)))
        deadline = time.time() + timeout
        last_size = self._backend_log_size()
        self._slow_boot = False
        self._extending = False
        while time.time() < deadline:
            if self.health():
                return True
            if self.proc is not None and self.proc.poll() is not None:
                log.warning("后端进程提前退出，code=%s", self.proc.returncode)
                time.sleep(0.4)
                if self.health():
                    return True
                break
            size = self._backend_log_size()
            if size > last_size and self.proc is not None:
                last_size = size
                if deadline < hard_cap:
                    deadline = min(hard_cap, deadline + 15)
                    self._slow_boot = True
                    self._extending = True
                    log.info("后端仍在初始化（backend.log 增长），延长等待，剩余上限 %.0fs",
                             max(0.0, hard_cap - time.time()))
            time.sleep(0.5)
        self._extending = False
        return self.health()

    def _backend_log_size(self):
        try:
            return (self.log_dir / "backend.log").stat().st_size
        except OSError:
            return 0

    def progress_loop(self, attempt, timeout):
        started = time.time()
        while self.starting:
            elapsed = time.time() - started
            pct = min(88, 8 + int(80 * elapsed / max(1, timeout)))
            # A1：等待被顺延时进度条会停在高位，这里把文案换成慢启动提示，避免用户误以为卡死
            if getattr(self, "_extending", False):
                msg = f"检测到安全软件查杀或磁盘较慢，仍在等待后端初始化（第 {attempt}/{self._retry_limit()} 次）…"
                detail = "首次启动遇到杀毒软件全量扫描时可能需要几分钟，程序正在耐心等待，请勿反复重启。"
            else:
                msg = f"正在启动（第 {attempt}/{self._retry_limit()} 次）…"
                detail = "首次启动可能需要 1–2 分钟，请稍候"
            self.write_state("starting", msg, progress=pct, detail=detail)
            time.sleep(1.0)

    def _retry_limit(self):
        return int(self.cfg["backend"].get("retry_limit", 3))

    def startup(self):
        with self.lock:
            if self.starting or self.exiting:
                return False
            self.starting = True
            self.failed = False
        attempts = self._retry_limit()
        timeout = int(self.cfg["backend"].get("startup_timeout_sec", 90))
        last_start_error = ""
        try:
            for attempt in range(1, attempts + 1):
                # 端口上已经有同产品后端在应答，直接认领，别再起第二个（两个后端共用
                # 同一端口会随机返回 500，健康检查随之误判）。
                if self.health():
                    self.starting = False
                    self.failed = False
                    self.write_state("ready", "已就绪", progress=100)
                    log.info("端口 %s 已有后端应答，直接使用", self.port)
                    return True
                # 端口绑不上（被占或被系统拦住）时，重试换下一个端口，别在同一个端口上硬撞三次
                if attempt > 1 and self.choose_port() is None:
                    log.warning("没有找到可用端口，停止重试")
                    break
                if attempt == 1 and not self.prepare_user_data():
                    self.starting = False
                    self.failed = True
                    return False
                log.info("第 %s/%s 次启动", attempt, attempts)
                self.write_state("starting", f"正在启动（第 {attempt}/{attempts} 次）…", progress=8)
                owner = self.port_owner_pid(self.port)
                if owner and owner != os.getpid():
                    log.info("端口 %s 已有后端进程 pid=%s；等待其健康，不重复 spawn", self.port, owner)
                    if self.wait_ready(timeout):
                        self.starting = False
                        self.failed = False
                        self.write_state("ready", "已就绪", progress=100)
                        return True
                    self.starting = False
                    self.failed = True
                    self.write_state("failed", "端口已有后端但暂时没有响应；稍后自动重试。",
                                     progress=0, detail="日志目录：" + self.display_log_dir)
                    return False
                try:
                    self.start_backend()
                except Exception as exc:
                    log.exception("后端启动命令失败")
                    last_start_error = str(exc)[:160]
                    self.write_state("failed", "后端启动命令失败：" + str(exc)[:160],
                                     progress=0, detail="日志目录：" + self.display_log_dir)
                    self.stop_backend()
                    self.failed = True
                    if attempt < attempts:
                        time.sleep(2.0)
                        continue
                    break
                ticker = threading.Thread(target=self.progress_loop, args=(attempt, timeout), daemon=True)
                ticker.start()
                if self.wait_ready(timeout):
                    self.starting = False
                    self.failed = False
                    # 资料刚搬过时，把新位置写进状态（启动器日志和窗口都留痕）
                    self.write_state("ready", "已就绪", progress=100,
                                     detail=(getattr(self, "_data_note", "") or f"日志：{self.display_log_dir}"))
                    log.info("后端就绪 http://127.0.0.1:%s/", self.port)
                    return True
                early = self.proc is not None and self.proc.poll() is not None
                self.starting = False
                self.stop_backend()
                if early:
                    hint = self.diagnose_early_exit()
                    if hint:
                        self._early_exit_hint = hint
                        log.error("早退诊断：%s", hint)
                if attempt < attempts:
                    time.sleep(1.0)
            self.failed = True
            msg = f"后端连续 {attempts} 次没有启动成功。"
            if last_start_error:
                msg += " 最后一次错误：" + last_start_error
            extra = ""
            hint = getattr(self, "_early_exit_hint", None)
            if hint:
                extra += "\n" + hint
            # A2（2026-11-21）：后端活着但一直没就绪（等待被顺延过）⇒ 超时大概率是
            # 安全软件首启全量查杀，定向给加白指引；与「零输出疑似拦截」的早退诊断互补。
            if getattr(self, "_slow_boot", False):
                extra += ("\n启动超时且后端仍在初始化，最常见原因是安全软件首次运行全量查杀。"
                          "请把整个软件文件夹加入白名单后点「重新启动」：\n"
                          "· Windows 安全中心：病毒和威胁防护 → 管理设置 → 排除项 → 添加文件夹\n"
                          "· 360 / 火绒：信任区（白名单）→ 添加文件夹\n"
                          "· 仍失败：点「打包诊断日志」，把生成的 zip 发给客服。")
            if getattr(self, "port_probes", None):
                extra += "\n端口探测：\n" + self.port_failure_report()
            self.write_state("failed", msg,
                             progress=0, detail=f"日志目录：{self.display_log_dir}" + extra)
            log.error("启动失败，已重试 %s 次", attempts)
            return False
        finally:
            self.starting = False

    # ---- watchdog
    def watchdog_loop(self):
        interval = int(self.cfg["backend"].get("watchdog_interval_sec", 30))
        limit = int(self.cfg["backend"].get("watchdog_failures", 2))
        timeout = int(self.cfg["backend"].get("watchdog_restart_timeout_sec", 45))
        fails = 0
        backoff = 5
        while not self.exiting:
            time.sleep(interval)
            if self.exiting:
                break
            if self.starting:
                fails = 0
                continue
            # 2026-11-17（t50）：watchdog 探活用长超时，避免重负载误判假死。
            if self.health(timeout_sec=max(20,int(self.cfg["health"].get("watchdog_timeout_sec", 10)))):
                fails = 0
                backoff = 5
                if self.failed:
                    self.failed = False
                    self.write_state("ready", "已重新连接", progress=100)
                continue
            fails += 1
            log.warning("watchdog 探活失败 %s/%s", fails, limit)
            if fails < limit:
                continue
            fails = 0
            if self.exiting:
                break
            # 长超时复核：只是慢响应就不杀，避免误杀健康后端。
            if self.health(timeout_sec=35):
                log.info("watchdog 长超时复核通过（重负载慢响应），放弃重启")
                backoff = 5
                continue
            self.write_state("connecting", "服务没有响应，正在重新连接…", progress=30)
            self.stop_backend()
            # 不换端口、不起第二实例；旧进程占端口则按端口 owner 清理。
            if not self.wait_port_release(self.port, 20):
                if self.health(timeout_sec=5):
                    continue
                self.failed = True
                self.write_state("failed", f"服务重启失败：端口 {self.port} 仍被占用，未起第二实例。",
                                 progress=0, detail=f"日志目录：{self.display_log_dir}")
                log.error("watchdog 重启失败：端口 %s 未释放；%s 秒后重试", self.port, backoff)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
                continue
            self.starting = True
            self.failed = False
            try:
                self.start_backend()
                ok = self.wait_ready(timeout)
            except Exception as exc:
                log.exception("watchdog 重新拉起后端失败")
                ok = False
                self.write_state("failed", "后端重新拉起失败：" + str(exc)[:160],
                                 progress=0, detail="日志目录：" + self.display_log_dir)
            finally:
                self.starting = False
            if ok:
                self.failed = False
                backoff = 5
                self.write_state("ready", "已重新连接", progress=100)
                log.info("watchdog 已重新拉起后端 port=%s", self.port)
            else:
                self.failed = True
                early = self.proc is not None and self.proc.poll() is not None
                hint = self.diagnose_early_exit() if early else None
                detail = f"日志目录：{self.display_log_dir}"
                if hint:
                    detail += "\n" + hint
                    log.error("早退诊断：%s", hint)
                self.write_state("failed", "服务重启失败，稍后会自动重试。", progress=0,
                                 detail=detail)
                log.error("watchdog 重启失败；%s 秒后重试", backoff)
                self.stop_backend()
                time.sleep(backoff)
                backoff = min(60, backoff * 2)

    def retry_loop(self):
        while not self.exiting:
            time.sleep(1.0)
            if self.retry_flag.exists():
                try:
                    self.retry_flag.unlink()
                except OSError:
                    pass
                log.info("收到窗口重试请求")
                if not self.starting:
                    threading.Thread(target=self.startup, daemon=True).start()

    # ---- pywebview 外壳
    def read_state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {"phase": "starting", "message": "正在启动…", "progress": 6,
                    "detail": "首次启动可能需要 1–2 分钟",
                    "url": f"http://127.0.0.1:{self.port}/", "log": self.display_log_dir}

    def request_retry(self):
        log.info("窗口点了“重新启动”")
        if not self.starting:
            threading.Thread(target=self.startup, daemon=True).start()

    def open_log_dir(self):
        try:
            os.startfile(str(self.log_dir))
        except Exception as exc:
            log.warning("打开日志目录失败：%s", exc)

    def copy_log_path(self):
        return set_clipboard(str(self.log_dir))

    def _desktop_dir(self):
        """A3：取真实桌面目录（含 OneDrive 重定向）；失败回退到日志目录的父级。"""
        try:
            class _KNOWNFOLDERID(ctypes.Structure):
                _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD),
                            ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]
            desktop_id = _KNOWNFOLDERID(
                0xB4BFCC3A, 0xDB2C, 0x424C,
                (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(desktop_id), 0, None, ctypes.byref(ptr)):
                raise OSError("SHGetKnownFolderPath != 0")
            path = Path(ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path if path.is_dir() else self.log_dir.parent
        except Exception:
            return self.log_dir.parent

    def bundle_logs(self, max_tail=10 * 1024 * 1024):
        """A3（2026-11-21）一键诊断包：把日志目录打成 zip 放到桌面。
        只收日志目录（launcher/backend/运行状态/崩溃报告），不碰用户数据；
        单文件超 10MB 只留尾部（文件名加 .tail），manifest.txt 记录环境与裁剪；
        生成后复制路径到剪贴板并在资源管理器里定位。返回 zip 绝对路径。"""
        import zipfile
        name = self.cfg.get("product_name", "人生重启")
        ts = time.strftime("%Y%m%d-%H%M%S")
        out = self._desktop_dir() / f"{name}-诊断包-{ts}.zip"
        suffix = 2
        while out.exists():
            out = out.with_name(f"{name}-诊断包-{ts}-{suffix}.zip")
            suffix += 1
        manifest = []
        try:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in sorted(self.log_dir.rglob("*")):
                    if not p.is_file() or p.suffix.lower() == ".zip":
                        continue
                    rel = p.relative_to(self.log_dir).as_posix()
                    size = p.stat().st_size
                    if size > max_tail:
                        with open(p, "rb") as fh:
                            fh.seek(-max_tail, 2)
                            zf.writestr(rel + ".tail", fh.read())
                        manifest.append(f"{rel}\t{size}\tTAIL{max_tail // (1024 * 1024)}MB")
                    else:
                        zf.write(p, rel)
                        manifest.append(f"{rel}\t{size}")
                try:
                    winver = " ".join(str(v) for v in sys.getwindowsversion())
                except Exception:
                    winver = "?"
                info = [
                    f"product_id={self.product_id}",
                    f"port={self.port}",
                    f"host={self.host}",
                    f"windows={winver}",
                    f"machine={os.environ.get('COMPUTERNAME', '?')}",
                    f"launcher_pid={os.getpid()}",
                    f"python={sys.version.split()[0]}",
                    f"created={time.strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                    "files:",
                ] + manifest
                zf.writestr("manifest.txt", "\n".join(info))
        except Exception:
            try:
                out.unlink()
            except OSError:
                pass
            raise
        set_clipboard(str(out))
        try:
            subprocess.Popen(["explorer", f"/select,{out}"],
                             creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        log.info("诊断包已生成：%s", out)
        return str(out)

    def open_in_browser(self):
        webbrowser.open(f"http://127.0.0.1:{self.port}/")

    def _on_closing(self):
        """点窗口右上角 X：隐藏到托盘，不退出（退出走托盘菜单）。"""
        try:
            url = self.window.get_current_url()
        except Exception:
            url = "?"
        log.info("窗口关闭事件 url=%s exiting=%s", url, self.exiting)
        if self.exiting:
            return True
        try:
            self.window.hide()
        except Exception as exc:
            log.warning("隐藏窗口失败：%s", exc)
        log.info("关窗 -> 隐藏到托盘，后端继续运行")
        return False

    def start_tray(self):
        def serve():
            try:
                import pystray
                from PIL import Image
                icon_file = Path(self.cfg.get("icon", {}).get("path", "") or "")
                image = Image.open(icon_file if icon_file.is_file() else self.icon_path)
                tray_cfg = self.cfg.get("tray", {})

                def on_show(icon=None, item=None):
                    try:
                        self.window.show()
                        self.window.restore()
                    except Exception as exc:
                        log.warning("显示窗口失败：%s", exc)

                def on_quit(icon=None, item=None):
                    self.exiting = True
                    try:
                        self.window.destroy()
                    except Exception:
                        pass
                    try:
                        icon.stop()
                    except Exception:
                        pass

                menu = pystray.Menu(
                    pystray.MenuItem(tray_cfg.get("show", "显示主界面"), on_show, default=True),
                    pystray.MenuItem(tray_cfg.get("browser", "用浏览器打开"),
                                     lambda: self.open_in_browser()),
                    pystray.MenuItem("打开日志目录", lambda: self.open_log_dir()),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem(tray_cfg.get("quit", "退出"), on_quit),
                )
                self.tray = pystray.Icon(f"ipo-{self.product_id}", image,
                                         tray_cfg.get("tooltip", ""), menu)
                log.info("托盘已启动 icon=%s", icon_file if icon_file.is_file() else self.icon_path)
                self.tray.run()
            except Exception as exc:
                log.warning("托盘不可用：%s", exc)

        threading.Thread(target=serve, daemon=True).start()

    def run_pywebview(self, webview):
        win_cfg = self.cfg.get("window", {})
        self.set_app_user_model_id()
        self.window = webview.create_window(
            self.cfg.get("window_title") or self.cfg.get("product_name", ""),
            html=splash_html(self.cfg),
            js_api=ShellApi(self),
            width=int(win_cfg.get("width", 1280)),
            height=int(win_cfg.get("height", 820)),
            min_size=(int(win_cfg.get("min_width", 980)), int(win_cfg.get("min_height", 640))),
            background_color=win_cfg.get("background", "#100F0D"),
            text_select=False,
            # 无系统标题栏：顶部改由 app/web/shell-titlebar.js 自绘。
            # easy_drag 必须关掉——它是"整窗随便拖"，会把点击按钮也当拖动。
            frameless=True,
            easy_drag=False,
        )
        self.window.events.closing += self._on_closing
        self.start_tray()
        self.install_taskbar_icon()
        threading.Thread(target=self.startup, daemon=True).start()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()
        # t151：第二次双击写 retry.flag；没有消费者就永远不会立刻重拉。
        threading.Thread(target=self.retry_loop, daemon=True).start()
        storage = self.log_dir / "webview2-data"
        storage.mkdir(parents=True, exist_ok=True)
        log.info("pywebview 外壳启动：%s", self.cfg.get("window_title"))
        try:
            webview.start(gui="edgechromium", debug=False, private_mode=False,
                          storage_path=str(storage))
        except Exception:
            # start() 抛异常时也必须走后端清理，否则后端会变成没有主的孤儿进程占着端口
            log.exception("窗口层异常退出")
            return 1
        finally:
            self.exiting = True
            try:
                if self.tray:
                    self.tray.stop()
            except Exception:
                pass
            self.stop_backend()
            self.close_mutex()
            log.info("窗口已关闭，后端已停止，退出")
        return 0

    def install_taskbar_icon(self):
        """在窗口显示后设置任务栏图标，并隔几秒补两次。

        WinForms 窗体在 shown 时会把自己的默认图标重新写回窗口，只在创建后立刻设
        会被覆盖（2026-11-15 实测：启动慢时 WM_GETICON 又变回 pythonw 白纸）。
        """
        icon = self.window_icon_file()

        def _on_shown(*_args):
            threading.Thread(target=apply_taskbar_icon, args=(icon, 6.0),
                             daemon=True).start()

        try:
            self.window.events.shown += _on_shown
        except Exception as exc:
            log.warning("注册 shown 回调失败：%s", exc)
        threading.Thread(target=self._reapply_icon, args=(icon,), daemon=True).start()

    def _reapply_icon(self, icon):
        for delay in (1.0, 3.0, 7.0):
            time.sleep(delay)
            apply_taskbar_icon(icon, timeout=3.0)

    def window_icon_file(self):
        """窗口/任务栏图标：优先配置里的品牌 ico，缺失时退回自绘图标。"""
        path = Path(str(self.cfg.get("icon", {}).get("path", "") or ""))
        return path if path.is_file() else self.icon_path

    def set_app_user_model_id(self):
        """任务栏分组身份独立于 pythonw.exe，不和其他 Python 程序并成一组。"""
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "JidiXH.%s.Workbench" % self.product_id)
        except Exception as exc:
            log.warning("设置 AppUserModelID 失败：%s", exc)

    # ---- 宿主窗口
    def write_host_files(self, webview2_version):
        self.splash_path.write_text(splash_html(self.cfg), encoding="utf-8")
        self.failure_path.write_text(failure_html(self.cfg, "服务没有在预期时间内就绪。"), encoding="utf-8")
        self.connecting_path.write_text(status_html(self.cfg, "正在重新连接", "服务暂时没有响应，稍等几秒会自动恢复。"), encoding="utf-8")
        self.guide_path.write_text(guide_html(self.cfg, False), encoding="utf-8")
        make_icon(self.icon_path, self.cfg.get("icon", {}).get("letter", "人"),
                  self.cfg.get("icon", {}).get("background", "#6A60BE"),
                  self.cfg.get("icon", {}).get("foreground", "#FFFFFF"))
        userdata = self.log_dir / "webview2-data"
        userdata.mkdir(parents=True, exist_ok=True)
        autostart_cmd = self.cfg.get("autostart", {}).get("command", "")
        lines = [
            f"title={self.cfg.get('window_title', self.cfg.get('product_name', ''))}",
            f"width={self.cfg['window'].get('width', 1280)}",
            f"height={self.cfg['window'].get('height', 820)}",
            f"minwidth={self.cfg['window'].get('min_width', 980)}",
            f"minheight={self.cfg['window'].get('min_height', 640)}",
            f"splash={self.splash_path}",
            f"failure={self.failure_path}",
            f"connecting={self.connecting_path}",
            f"state={self.state_path}",
            f"retry_flag={self.retry_flag}",
            f"log={self.display_log_dir}",
            f"userdata={userdata}",
            f"icon={self.icon_path}",
            f"mutex={self.cfg['mutex_name']}_host",
            f"tray_tooltip={self.cfg.get('tray', {}).get('tooltip', '')}",
            f"tray_show={self.cfg.get('tray', {}).get('show', '显示主界面')}",
            f"tray_browser={self.cfg.get('tray', {}).get('browser', '用浏览器打开')}",
            f"tray_autostart={self.cfg.get('tray', {}).get('autostart', '开机自启')}",
            f"tray_quit={self.cfg.get('tray', {}).get('quit', '退出')}",
            f"autostart_name={self.cfg.get('autostart', {}).get('value_name', '')}",
            f"autostart_cmd={autostart_cmd}",
            f"webview2={webview2_version}",
        ]
        self.host_ini.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run_host(self):
        exe = HERE / "host" / "LauncherHost.exe"
        if not exe.exists():
            log.error("宿主不存在：%s", exe)
            return 3
        return subprocess.Popen([str(exe), str(self.host_ini)], cwd=str(exe.parent),
                                creationflags=CREATE_NO_WINDOW).wait()

    # ---- WebView2 缺失：引导页 + 托盘回落
    def run_guide_mode(self):
        ok = self.startup()
        self.guide_path.write_text(guide_html(self.cfg, ok), encoding="utf-8")
        try:
            webbrowser.open(self.guide_path.as_uri())
        except Exception as exc:
            log.warning("打开引导页失败：%s", exc)
        log.info("WebView2 缺失，已打开引导页：%s", self.guide_path)
        self.run_guide_tray(ok)

    def run_guide_tray(self, backend_ok):
        try:
            import pystray
            from PIL import Image
            image = Image.open(self.icon_path) if self.icon_path.exists() else Image.new("RGBA", (64, 64), (106, 96, 190, 255))
            url = f"http://127.0.0.1:{self.port}/"

            def open_app(icon=None, item=None):
                if backend_ok:
                    webbrowser.open(url)
                else:
                    webbrowser.open(self.guide_path.as_uri())

            def open_log(icon=None, item=None):
                os.startfile(str(self.log_dir))

            def quit_app(icon=None, item=None):
                self.exiting = True
                self.stop_backend()
                try:
                    icon.stop()
                except Exception:
                    pass

            menu = pystray.Menu(
                pystray.MenuItem("用浏览器打开", open_app, default=True),
                pystray.MenuItem("打开日志目录", open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", quit_app),
            )
            icon = pystray.Icon(f"ipo-{self.product_id}-guide", image,
                                self.cfg.get("tray", {}).get("tooltip", self.cfg.get("product_name", "")), menu)
            threading.Thread(target=lambda: self._guide_keepalive(), daemon=True).start()
            icon.run()
        except Exception as exc:
            log.warning("引导模式托盘不可用：%s", exc)
            while not self.exiting:
                time.sleep(1)

    def _guide_keepalive(self):
        while not self.exiting:
            time.sleep(1)

    # ---- 联网标记：先提示，用户点“是”才批量解除
    def handle_motw(self):
        hits = motw_marked()
        if not hits:
            return
        log.warning("检测到联网标记（%s），提示用户解除锁定", ", ".join(hits))
        title = self.cfg.get("product_name", "程序")
        text = (
            "检测到程序是从网上下载的（文件带联网标记），Windows 可能拦截启动。\n\n"
            "建议先解除锁定再运行：\n"
            "1. 右键 start.bat → 属性 → 勾选“解除锁定” → 确定\n"
            "2. 或者点“是”，由本程序一次性解除整个文件夹的锁定\n"
            "   （只改本文件夹，不动系统设置）\n\n"
            "现在解除锁定吗？"
        )
        if not confirm_box(text, title + " · 先解除锁定"):
            log.info("用户选择暂不解除锁定")
            return
        removed = unblock_tree(ROOT)
        log.info("已解除 %s 个文件的联网标记", removed)
        message_box(
            "已解除 %s 个文件的锁定。\n\n如果仍然弹出蓝色的“Windows 已保护你的电脑”，\n"
            "点“更多信息” → “仍要运行”。" % removed,
            title,
        )

    def open_attach_in_browser(self, exc=None):
        """attach 场景下 pywebview 不可用：提示并打开已运行服务的浏览器页。"""
        message_box("原生窗口组件不可用，已用浏览器打开工作台。\n\n%s"
                    % (exc or "pywebview 未加载"),
                    self.cfg.get("product_name", "启动"))
        try:
            webbrowser.open(f"http://127.0.0.1:{self.port}/")
        except Exception as exc2:
            log.warning("降级浏览器失败：%s", exc2)
        self.close_mutex()

    # ---- 入口
    def run(self):
        # 升级到一半的包不能被静默启动：先看 journal，active/broken 一律拒绝并给恢复命令。
        try:
            sys.path.insert(0, str(ROOT / "tools"))
            from upgrade_journal import guard as _upgrade_guard
        except Exception:
            _upgrade_guard = None
        if _upgrade_guard is not None:
            _can_start, _upgrade_msg, _ = _upgrade_guard(ROOT)
            if not _can_start:
                log.error("升级未完成，拒绝启动：%s", _upgrade_msg)
                self.write_state("failed", "升级未完成，程序文件可能不完整。", progress=0,
                                 detail=_upgrade_msg)
                message_box(_upgrade_msg, self.cfg.get("product_name", "启动失败"))
                return 3
        self.handle_motw()
        self.write_host_files(detect_webview2() or "missing")
        if not self.acquire_single_instance():
            log.info("已有实例，退出")
            return 0
        port = self.choose_port()
        if port is None:
            report = self.port_failure_report()
            log.error("没有可用端口：\n%s", report)
            self.write_state("failed", "没有可用端口。", progress=0, detail=report)
            message_box("没有可用端口。\n\n" + report,
                        self.cfg.get("product_name", "启动失败"))
            return 2
        if self.focused_existing:
            log.info("已有原生窗已聚焦，不重复创建")
            self.close_mutex()
            return 0
        if self.attach_existing:
            log.info("attach 模式：复用已运行服务 http://127.0.0.1:%s/，不启动第二个后端", self.port)

        webview2 = detect_webview2()
        log.info("WebView2=%s port=%s", webview2 or "missing", self.port)
        if not webview2:
            self.run_guide_mode()
            self.stop_backend()
            self.close_mutex()
            return 0

        self.write_host_files(webview2)
        self.write_state("starting", "正在启动…", progress=6)
        try:
            import webview
        except Exception as exc:
            log.error("pywebview 不可用：%s", exc, exc_info=True)
            if self.attach_existing:
                # 裸后端已在跑，pywebview 不可用时才降级浏览器；不启动 guide 模式的后端。
                self.open_attach_in_browser(exc)
                return 0
            message_box("外壳组件加载失败，已改用浏览器打开。\n\n%s" % exc,
                        self.cfg.get("product_name", "启动"))
            self.run_guide_mode()
            self.stop_backend()
            self.close_mutex()
            return 0
        return self.run_pywebview(webview)


def ensure_desktop_shortcut_async():
    """后台创建/补建桌面快捷入口（幂等，绝不影响启动）。
    2026-11-26：客户反馈「总要进文件夹找 start.bat 双击，不正规」→
    启动器每次拉起时后台补建桌面「人生重启系统.lnk」（已存在即跳过；
    用户删掉后下次启动自动恢复）。见 tools/create_desktop_shortcut.py。"""
    script = HERE.parent / "create_desktop_shortcut.py"
    if not script.is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW, close_fds=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="共用桌面外壳启动器")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--check", action="store_true", help="只打印检测结果")
    parser.add_argument("--set-autostart", choices=["on", "off"])
    args = parser.parse_args()

    console = _HAS_CONSOLE
    cfg = None
    try:
        cfg = load_config(Path(args.config), args.port, args.host)
        setup_logging(Path(cfg["log_dir"]))
        redirect_stdio(Path(cfg["log_dir"]))
        log.info("配置=%s 产品=%s 端口=%s console=%s python=%s",
                 args.config, cfg.get("product_id"), cfg.get("port"),
                 console, Path(sys.executable).name)

        if not args.check and not args.set_autostart:
            ensure_desktop_shortcut_async()

        if args.set_autostart:
            auto = cfg.get("autostart", {})
            set_autostart(auto.get("value_name", ""), auto.get("command", ""), args.set_autostart == "on")
            return 0

        if args.check:
            print(json.dumps({
                "product": cfg.get("product_id"),
                "port": cfg.get("port"),
                "port_free": port_free(int(cfg["port"])),
                "webview2": detect_webview2() or "missing",
                "host_exe": str(HERE / "host" / "LauncherHost.exe"),
                "host_exe_exists": (HERE / "host" / "LauncherHost.exe").exists(),
                "log_dir": cfg["log_dir"],
                "autostart": autostart_state(cfg.get("autostart", {}).get("value_name", "")),
                "motw": motw_marked(),
                "shell": "pywebview",
                "pywebview": _pywebview_ready(),
                "splash_image": bool(cfg.get("splash", {}).get("image")
                                     and Path(cfg["splash"]["image"]).is_file()),
            }, ensure_ascii=False, indent=2))
            return 0

        return Launcher(cfg).run()
    except SystemExit:
        raise
    except BaseException as exc:
        # 2026-11-16：入口静默死的根因就是这里——pythonw 下没有 stderr，异常没人接。
        # 现在一律落盘成可读失败记录，并弹一次提示，避免「双击后什么都没发生」。
        path = write_crash_report(exc, cfg, sys.argv)
        try:
            log.exception("启动失败：%s", exc)
        except Exception:
            pass
        if not args.check and not args.set_autostart:
            message_box("启动失败，已把错误写入日志：\n%s\n\n%s: %s"
                        % (path or "（日志写入失败）", type(exc).__name__, exc),
                        (cfg or {}).get("product_name", "启动失败"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
