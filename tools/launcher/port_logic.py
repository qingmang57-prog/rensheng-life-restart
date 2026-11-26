# -*- coding: utf-8 -*-
"""端口候选纯逻辑（可单测，不依赖 Windows 环境）。

2026-11-24 t191：把「保留段过滤 + 跨区间候选 + OS 分配兜底」从 launcher 里抽出，
便于回归注入 mock netsh 输出/占用探测，不触碰系统配置。
"""
from __future__ import annotations

import socket


def parse_excluded(text):
    """解析 netsh excludedportrange 输出 → [(start, end)]。

    2026-11-24 t192：优先直接按 bytes 行解析数字端口（不依赖 GBK/UTF-8 解码成功）；
    str 输入同样支持（中英文表头、尾部 *）。只认前两列都是数字的行。
    """
    import re
    if isinstance(text, (bytes, bytearray)):
        out = []
        for m in re.finditer(rb"(?m)^\s*(\d{1,5})\s+(\d{1,5})\b", bytes(text)):
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b <= 65535:
                out.append((a, b))
        if out:
            return out
        try:
            text = bytes(text).decode("latin-1", "ignore")
        except Exception:
            return []
    ranges = []
    for line in str(text or "").splitlines():
        parts = line.replace("*", " ").split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            a, b = int(parts[0]), int(parts[1])
            if 0 < a <= b <= 65535:
                ranges.append((a, b))
    return ranges


def candidate_ports(base: int, fallback: int = 45):
    """base + 连续 fallback 个，再追加跨区间分散候选（去重、保序）。"""
    out = []
    for p in [base] + [base + i for i in range(1, max(0, fallback) + 1)]:
        out.append(p)
    # 跨区间：紧接 fallback 之后继续取（覆盖 8791-8890 这类连续保留段），再跳到常用高位段
    for start in (base + max(0, fallback) + 1, base + max(0, fallback) + 101,
                  base + max(0, fallback) + 201):
        out.extend(range(start, start + 100))
    for start, count in ((10000, 20), (15000, 20), (20000, 20), (25000, 20),
                         (30000, 20), (35000, 20), (40000, 20), (45000, 20), (50000, 20)):
        out.extend(range(start, start + count))
    seen = set()
    uniq = []
    for p in out:
        if 1024 <= p <= 65535 and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def in_reserved(port: int, reserved) -> bool:
    return any(a <= port <= b for a, b in (reserved or []))


def pick_port(candidates, reserved, probe, tried=None):
    """按顺序挑端口。probe(port)->(ok, why)。返回 (port, probes)。

    probes: [(port, why, extra)]，供失败页报告；reserved 命中记为 reserved。
    """
    tried = set(tried or ())
    probes = []
    for c in candidates:
        if c in tried:
            continue
        hit = next((r for r in (reserved or []) if r[0] <= c <= r[1]), None)
        if hit:
            probes.append((c, "reserved", "%d-%d" % hit))
            continue
        ok, why = probe(c)
        if not ok:
            probes.append((c, why, ""))
            continue
        return c, probes
    return None, probes


def ephemeral_port(probe):
    """让 OS 分配一个临时端口，再用 probe 真实验证（bind/listen 由 probe 负责）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
    except OSError:
        return None
    finally:
        s.close()
    ok, _ = probe(port)
    return port if ok else None
