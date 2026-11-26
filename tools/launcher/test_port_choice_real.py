# -*- coding: utf-8 -*-
"""t193 真实回归：自持 listen 占用 / netsh 失败注入 / 两活实例多开（非 stub）。

运行：tools\\python\\python.exe tools\\launcher\\test_port_choice_real.py
"""
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import port_logic
import launcher


def real_probe(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", port))
        s.listen(1)
        return True, ""
    except OSError as exc:
        return False, ("blocked" if getattr(exc, "winerror", None) == 10013 else "occupied")
    finally:
        s.close()


def free_port(start=9100):
    p = start
    while p < 65000:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            p += 1
        finally:
            s.close()
    raise RuntimeError("no free port")


def wait_health(port, timeout=60):
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/version/status" % port, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    fails = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail else ""))
        if not cond:
            fails.append(name)

    # 1 真实自持 listen 占用
    p0 = free_port(9100)
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    held.bind(("0.0.0.0", p0))
    held.listen(1)
    try:
        chosen, probes = port_logic.pick_port([p0, p0 + 1], [], real_probe)
        check("real_occupied_skip", chosen == p0 + 1 and probes and probes[0][1] == "occupied", "%s %s" % (chosen, probes))
    finally:
        held.close()

    # 2 netsh 失败：返回码 & 异常两路
    orig_run = launcher.subprocess.run
    try:
        launcher._EXCLUDED_RANGES = None
        launcher._EXCLUDED_OK = None
        launcher.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b"")
        r = launcher.windows_excluded_ranges()
        check("netsh_rc1_degrade", r == [] and launcher.excluded_scan_ok() is False, str(r))
        launcher._EXCLUDED_RANGES = None
        launcher._EXCLUDED_OK = None
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="netsh", timeout=10)
        launcher.subprocess.run = boom
        r = launcher.windows_excluded_ranges()
        check("netsh_exception_degrade", r == [] and launcher.excluded_scan_ok() is False, str(r))
    finally:
        launcher.subprocess.run = orig_run
        launcher._EXCLUDED_RANGES = None
        launcher._EXCLUDED_OK = None

    # 3 两活实例并发（隔离 PID/数据目录）
    py = ROOT / 'tools/python/python.exe'
    app = ROOT / 'app/app.py'
    base = ROOT / '2026-11-24/验证/启动修复/多开回归'
    ports = [free_port(9200), free_port(9300)]
    procs = []
    try:
        for i, port in enumerate(ports):
            data = base / ('data%d' % i)
            if data.exists():
                import shutil
                shutil.rmtree(data)
            data.mkdir(parents=True)
            env = dict(**__import__('os').environ)
            env['IP_DATA_DIR'] = str(data)
            procs.append(subprocess.Popen([str(py), str(app), '--port', str(port), '--host', '127.0.0.1'],
                                          cwd=str(ROOT), env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=0x08000000))
        ok1 = wait_health(ports[0], 90)
        ok2 = wait_health(ports[1], 90)
        pids = [p.pid for p in procs]
        check("multiopen_health", ok1 and ok2, "%s/%s" % (ok1, ok2))
        check("multiopen_distinct_pid", len(set(pids)) == 2, pids)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()

    print("\nRESULT", "PASS" if not fails else ("FAIL " + ",".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
