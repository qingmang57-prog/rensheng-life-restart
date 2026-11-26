# -*- coding: utf-8 -*-
"""启动链端口选择回归（t191，可重复执行，不碰系统配置/他人进程）。

运行：tools\\python\\python.exe tools\\launcher\\test_port_choice.py
覆盖：排除段解析/过滤、连续 100+ 保留段压住原候选、占用、直选、netsh 失败降级、OS 分配兜底、多开不互踩。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import port_logic


def ok_probe(port):
    return True, ""


def probe_map(bad):
    def _p(port):
        if port in bad:
            return False, bad[port]
        return True, ""
    return _p


def main():
    failures = []
    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name + ("  " + detail if detail else ""))
        if not cond:
            failures.append(name)

    # 1 解析（中英文表头、* 尾部）
    text = "协议 tcp 端口排除范围\n----------    --------\n      5357        5357\n     50000       50059     *\n"
    r = port_logic.parse_excluded(text)
    check("parse_excluded", r == [(5357, 5357), (50000, 50059)], str(r))

    # 2 保留段过滤
    probe = lambda p: (False, "reserved") if 8791 <= p <= 8900 else (True, "")
    chosen, probes = port_logic.pick_port([8791, 8792, 9000], [(8791, 8900)], probe)
    check("reserved_filter", chosen == 9000 and probes[0][1] == "reserved", str(chosen))

    # 3 连续 100+ 保留段压住原候选段 → 跨区间仍能选出
    cands = port_logic.candidate_ports(8791, 45)
    chosen, _ = port_logic.pick_port(cands, [(8791, 8900)], ok_probe)
    check("cross_range_when_100plus_reserved", chosen is not None and chosen not in range(8791, 8901), str(chosen))

    # 3b 老板同场景：8791-8890 保留、8891 起可用 → 必须 8891
    cands2 = port_logic.candidate_ports(8791, 45)
    chosen, _ = port_logic.pick_port(cands2, [(8791, 8890)], ok_probe)
    check("boss_scenario_8791_8890_reserved", chosen == 8891, str(chosen))

    # 4 前 3 个被占用 → 第 4 个
    bad = {8791: "occupied", 8792: "occupied", 8793: "occupied"}
    chosen, probes = port_logic.pick_port([8791, 8792, 8793, 8794], [], probe_map(bad))
    check("occupied_skip", chosen == 8794 and len(probes) == 3, str(chosen))

    # 5 可用口直选
    chosen, probes = port_logic.pick_port([8791], [], ok_probe)
    check("direct_free", chosen == 8791 and probes == [], str(chosen))

    # 6 netsh 失败降级：reserved=[] 且 scan_ok=False → 不过滤，仍选 base
    chosen, _ = port_logic.pick_port(cands, [], ok_probe)
    check("netsh_fail_degrade_no_filter", chosen == 8791, str(chosen))

    # 7 OS 分配兜底：真实 bind 0 + probe
    e1 = port_logic.ephemeral_port(ok_probe)
    e2 = port_logic.ephemeral_port(ok_probe)
    check("os_ephemeral", e1 and 1024 <= e1 <= 65535, str(e1))
    check("multi_open_distinct", e1 != e2, "%s vs %s" % (e1, e2))

    # 8 tried_ports 兼容：已试端口跳过
    chosen, _ = port_logic.pick_port([8791, 8792], [], ok_probe, tried={8791})
    check("tried_ports_skip", chosen == 8792, str(chosen))

    # 9 bytes 解析：GBK / UTF8 / 空输出（不依赖解码成功）
    gbk = "协议 tcp 端口排除范围\r\n 8791 8890\r\n     50000       50059     *\r\n".encode("gbk")
    check("parse_excluded_gbk_bytes", port_logic.parse_excluded(gbk) == [(8791, 8890), (50000, 50059)], str(port_logic.parse_excluded(gbk)))
    utf8 = "协议 tcp 端口排除范围\n 8791 8890\n".encode("utf-8")
    check("parse_excluded_utf8_bytes", port_logic.parse_excluded(utf8) == [(8791, 8890)], str(port_logic.parse_excluded(utf8)))
    check("parse_excluded_empty_bytes", port_logic.parse_excluded(b"") == [], "")
    # 10 命令非零返回码：失败与空输出分开
    import port_doctor
    ok, text, err, raw = port_doctor.run_cmd(["cmd", "/c", "exit", "1"])
    check("run_cmd_nonzero", (not ok) and ("exit=1" in err), "ok=%s err=%s" % (ok, err))
    ok2, text2, err2, raw2 = port_doctor.run_cmd(["cmd", "/c", "echo", "hello"])
    check("run_cmd_zero", ok2 and "hello" in text2 and err2 == "", "ok=%s" % ok2)

    print("\nRESULT", "PASS" if not failures else ("FAIL " + ",".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
