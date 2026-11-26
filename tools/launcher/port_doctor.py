# -*- coding: utf-8 -*-
"""端口诊断（随包版，2026-11-24 t191 四项修正）。

用法（软件根目录）：
    tools\\python\\python.exe tools\\launcher\\port_doctor.py
    tools\\python\\python.exe tools\\launcher\\port_doctor.py --port 8791 --count 45

修正（相对附件 9791/3 版）：
1) 默认端口/连续候选数从 tools/launcher/configs/rensheng.json 实读（--port/--count 可覆盖）；
2) 空排除段 = 正常（没有保留段），不判失败；
3) netsh 命令失败/超时与「无排除段」分开报告；
4) 输出按 GBK 读（bytes + 依次 gbk/utf-8/latin-1），不假设 text=True 的 UTF-8。
"""
import argparse
import json
import socket
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
HERE = Path(__file__).resolve().parent
CFG = HERE / "configs" / "rensheng.json"


def run_cmd(cmd, timeout=15):
    """返回 (ok, text, err, raw)。bytes 读；失败与空输出严格区分。"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        return False, "", str(exc), b""
    raw = proc.stdout or b""
    if isinstance(raw, str):
        text = raw
    else:
        text = None
        for enc in ("gbk", "utf-8", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("utf-8", "ignore")
    if proc.returncode not in (0, None):
        return False, text, "exit=%s" % proc.returncode, raw
    return True, text, "", raw


def excluded_segments():
    """返回 (ok, segs)。ok=False 表示命令失败/超时（与「无保留段」区分）。"""
    ok, out, err, raw = run_cmd(["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"])
    if not ok:
        return False, []
    import port_logic
    return True, port_logic.parse_excluded(raw if raw else out)


def listening():
    ok, out, _, _raw = run_cmd(["netstat", "-ano", "-p", "tcp"])
    table = {}
    if not ok:
        return table
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            try:
                table.setdefault(int(parts[1].rsplit(":", 1)[-1]), int(parts[4]))
            except ValueError:
                pass
    return table


def bindable(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        s.listen(1)
        return True, ""
    except OSError as exc:
        return False, "[WinError %s] %s" % (exc.errno, exc.strerror)
    finally:
        s.close()


def load_config():
    try:
        return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description="人生重启系统 端口诊断")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--count", type=int, default=None)
    args = ap.parse_args()
    cfg = load_config()
    port = args.port if args.port is not None else int(cfg.get("port", 8791))
    count = args.count if args.count is not None else int(cfg.get("port_fallback_count", 45))

    ok, segs = excluded_segments()
    live = listening()

    print("=" * 60)
    print(" Windows 保留端口段（netstat 看不到，但 bind/listen 会被拒）")
    print("=" * 60)
    if not ok:
        print("  ！读取失败/超时（本次不按保留段过滤，继续实测端口）")
    elif not segs:
        print("  （无保留段：正常状态，不是失败）")
    else:
        for a, b in segs:
            mark = "  <- * 默认端口 %d 就在这里面" % port if a <= port <= b else ""
            print("  %d - %d%s" % (a, b, mark))

    print()
    print("=" * 60)
    print(" 候选端口逐个实测（%d 起，连续 %d 个）" % (port, count))
    print("=" * 60)
    usable = []
    for p in range(port, port + max(0, count)):
        excluded = any(a <= p <= b for a, b in segs)
        owner = live.get(p)
        b_ok, err = bindable(p)
        why = []
        if excluded:
            why.append("被系统保留")
        if owner:
            why.append("被 PID %d 占用" % owner)
        if not b_ok and not excluded and not owner:
            why.append(err)
        print("  %d : %s%s" % (p, "可用" if b_ok else "不可用", ("  <- " + "、".join(why)) if why else ""))
        if b_ok:
            usable.append(p)

    print()
    if usable:
        print(">> 可用端口：%s" % ", ".join(str(p) for p in usable[:8]))
        print("  把 tools\\launcher\\configs\\rensheng.json 的 \"port\" 改成 %d 可立刻启动。" % usable[0])
    else:
        print(">> 这一段没有可用端口；启动器会跨区间/系统分配兜底，请把 launcher.log 发回。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
