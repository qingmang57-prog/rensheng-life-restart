# -*- coding: utf-8 -*-
"""billing_entitlements —— C1 变量裁决上移的**客户端地基**（t165，按《契约-billing_entitlements-t195.md》）

为什么单独一个件（而不是塞进 billing_quota）：`limits()` 是纯计算件，一旦让它自己去够网络，
断网/未登录/验签失败就会以"返回什么数"的形式污染裁决。本件把**取数与校验**收在一处，
`billing_quota` 只吃已校验的字典。

对齐契约的义务逐条落位：
 · §3.1 **内存态**：正文只进 `_BODY`；本件**不写任何文件**（无 cache_path/无 _save_cache），
   缓存名 `billing_entitlements` 落盘即违规——由 `make_release` 的打包禁入兜底（t164 同口径）。
 · §3.2 **`limits()` 吃云端**：提供 `entitlements()` 供上层注入；`HARD_CUTOVER=1` 时
   取不到云端 ⇒ 返回原因码、**不回落本地**（现网默认 `0`＝行为一字不变，等与 deploy 同版再翻，
   否则会出现"客户端已删兜底、云端还没这份资产"的空窗——契约 §5.3 的硬顺序）。
 · §3.3 **null 不许猜**：`PENDING_NULL_FIELDS` 显式列出三项主理人待裁字段，
   `pending_nulls()` 把它们点名给上层（上层据此出审计/界面提示），**既不当不限、也不当 0**。
 · §3.4 **降级开关不在客户端手里**：`read_only_basis.local_override_allowed/offline_fallback_allowed`
   只用于**告警**（`basis_warnings()`），绝不参与行为判定。
 · §3.5 **幂等与版本**：`ingest()` 必校验正文 sha256 == `X-Asset-Sha256`、`root_digest` 等于
   规范算法复算值、`ETag` 供上层省流；任一不符 ⇒ 明确原因码，不将就。

通道（§1）本件**不猜**：传输用 `configure(fetch=...)` 注入。
人生重启这棵树现状（实测）：只有 ip-api（`auth_service.cloud_base()` 默认
`https://api.jidixh.com`，Bearer `cloud_token`），`api/v1/` 全树零命中、无 mc 账户 token、
无资产验签件 ⇒ mc 通用资产通道要接哪一端属**通道裁定**，等队长/主理人拍完由我接上（本件的
`validate/ingest/root_digest` 与通道无关，拍完即用）。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

ASSET_KIND = "billing-entitlements-v1"
ASSET_TYPE = "billing_entitlements"          # 与 mc 类型同名（t173 口径）
SIG_PREFIX = "QP2ASSET."
SIG_DOMAIN = b"slice-asset-v1"               # 验签域前缀（契约 §1）
PLAN_KEYS = ("free", "month", "year")
ROOT_FIELDS = ("currency", "credit_milli", "hold_ttl_seconds", "plans",
               "free_limits", "actions", "credit_validity")
REQUIRED_TOP = ("asset_kind", "entitlement_version", "source", "pricing_version",
                "currency", "credit_milli", "hold_ttl_seconds", "plans", "free_limits",
                "actions", "credit_validity", "read_only_basis", "root_digest")
# 🔴 契约 §3.3：这三项在真源里是**显式 null**，属主理人待裁；未裁前不得当"不限"也不得当 0
PENDING_NULL_FIELDS = ("exports_per_day", "brain_ask_per_day", "max_duration_seconds")
POSITIVE_INT_FIELDS = ("exports_per_month", "max_height") + PENDING_NULL_FIELDS

_lock = threading.RLock()
_BODY: dict | None = None
_META = {"etag": "", "version": "", "sha256": "", "fetched_at": 0.0,
         "origin": "", "last_error": "", "basis_warnings": []}
_FETCH = None                                # 传输注入位（见 configure）


def hard_cutover() -> bool:
    """是否已切断本地兜底（默认关；与 deploy 同版才允许开，契约 §5.3）。"""
    return str(os.environ.get("BILLING_ENTITLEMENTS_HARD", "0")).strip() == "1"


def configure(fetch=None) -> None:
    """注入传输：`fetch(etag) -> (raw_bytes|None, headers_dict, reason_code)`。

    通道怎么接（mc 通用资产通道 vs ip-api 代理）由队长/主理人拍板后实现；
    未注入时 `entitlements()` 只可能返回 `(None, 'transport_not_configured')`——
    **不静默、不回落、不猜端点**。
    """
    global _FETCH
    with _lock:
        _FETCH = fetch


def canonical_root_digest(doc: dict) -> str:
    """契约 §2 规范算法的**复算实现**（只复算，不改口径）：
    覆盖字段＝ROOT_FIELDS；逐字段 `k=json.dumps(值, sort_keys=True, separators=(",",":"))`；
    按 **key 本身升序**拼 `k=v\\n`；整体 sha256。
    （t144 教训：按拼接串排序时 `.` 与 `:` 先后会翻转，同内容会算出两个摘要。）
    """
    parts = []
    for k in sorted(ROOT_FIELDS):
        v = json.dumps(doc.get(k), sort_keys=True, separators=(",", ":"))
        parts.append("%s=%s\n" % (k, v))
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def _is_int(v) -> bool:
    """整数但要**显式拒 bool**（契约 §2：真源里 bool 不算数）。"""
    return isinstance(v, int) and not isinstance(v, bool)


def validate(doc) -> list:
    """形状闸（字段与类型是闸，不是建议）。返回错误码列表，空＝通过。"""
    errs = []
    if not isinstance(doc, dict):
        return ["body_not_object"]
    for k in REQUIRED_TOP:
        if k not in doc:
            errs.append("missing_%s" % k)
    if errs:
        return errs
    if doc["asset_kind"] != ASSET_KIND:
        errs.append("bad_asset_kind")
    if not (isinstance(doc["entitlement_version"], str) and doc["entitlement_version"].strip()):
        errs.append("bad_entitlement_version")
    src = doc["source"]
    if not isinstance(src, dict):
        errs.append("bad_source")
    else:
        if not (isinstance(src.get("sha256"), str) and len(src["sha256"]) == 64
                and all(c in "0123456789abcdef" for c in src["sha256"].lower())):
            errs.append("bad_source_sha256")          # 没有真源指纹就证明不了这份表出自 billing_pricing.json
        if not (_is_int(src.get("bytes")) and src["bytes"] > 0):
            errs.append("bad_source_bytes")
        if "pricing_version" not in src or "pricing_updated_at" not in src:
            errs.append("bad_source_provenance")
        if not isinstance(src.get("drift_accepted"), bool):
            errs.append("bad_drift_accepted")
    if not _is_int(doc["pricing_version"]):
        errs.append("bad_pricing_version")
    for k in ("credit_milli", "hold_ttl_seconds"):
        if not (_is_int(doc[k]) and doc[k] >= 0):
            errs.append("bad_%s" % k)                # 拒绝 bool 与字符串数字
    if not isinstance(doc["actions"], dict) or not doc["actions"]:
        errs.append("bad_actions")
    else:
        for code, a in doc["actions"].items():
            if not isinstance(a, dict):
                errs.append("bad_action:%s" % code); continue
            if not (isinstance(a.get("settle_rule"), str) and a["settle_rule"].strip()):
                errs.append("missing_settle_rule:%s" % code)
            if not isinstance(a.get("billable"), bool):
                errs.append("bad_billable:%s" % code)
            for f in ("unit_milli", "min_milli"):
                if not (_is_int(a.get(f)) and a[f] >= 0):
                    errs.append("bad_%s:%s" % (f, code))
    plans = doc["plans"]
    if not isinstance(plans, dict) or "free" not in plans:
        errs.append("missing_free_plan")             # free_limits 的归属档
    else:
        for name, p in plans.items():
            if name not in PLAN_KEYS:
                errs.append("unknown_plan:%s" % name); continue
            if not isinstance(p, dict):
                errs.append("bad_plan:%s" % name); continue
            if not (isinstance(p.get("pricing_status"), str) and p["pricing_status"].strip()):
                errs.append("missing_pricing_status:%s" % name)
            if not isinstance(p.get("enabled"), bool):
                errs.append("bad_enabled:%s" % name)
            for f in ("days", "price_cents", "list_price_cents"):
                if f in p and not (_is_int(p[f]) and p[f] >= 0):
                    errs.append("bad_%s:%s" % (f, name))
    fl = doc["free_limits"]
    if not isinstance(fl, dict):
        errs.append("bad_free_limits")
    else:
        for f in POSITIVE_INT_FIELDS:
            if f not in fl:
                errs.append("missing_free_limit:%s" % f)
            elif fl[f] is not None and not (_is_int(fl[f]) and fl[f] >= 0):
                errs.append("bad_free_limit:%s" % f)
        if not isinstance(fl.get("watermark"), bool):
            errs.append("bad_watermark_type")        # 必须 bool（不能拿 0/1 冒充）
    rob = doc["read_only_basis"]
    if not isinstance(rob, dict):
        errs.append("bad_read_only_basis")
    else:
        auth = rob.get("authoritative_fields")
        if not isinstance(auth, list) or "plans" not in auth or "free_limits" not in auth:
            errs.append("bad_authoritative_fields")
        if "pending_owner_decisions" not in rob or not isinstance(rob["pending_owner_decisions"], list):
            errs.append("missing_pending_owner_decisions")   # 必须显式存在（可为空）
    rd = doc["root_digest"]
    if not (isinstance(rd, str) and len(rd) == 64):
        errs.append("bad_root_digest_len")
    elif rd != canonical_root_digest(doc):
        errs.append("root_digest_mismatch")          # 单一真源：只复算，不另写口径
    return errs


def basis_warnings(doc) -> list:
    """契约 §3.4：降级开关**不参与行为判定**，翻 true 只用于告警。"""
    out = []
    try:
        rob = (doc or {}).get("read_only_basis") or {}
        for f in ("local_override_allowed", "offline_fallback_allowed"):
            if rob.get(f) is not False:
                out.append("basis_%s_not_false" % f)
    except Exception:
        pass
    return out


def ingest(raw: bytes, headers: dict | None = None) -> tuple:
    """校验并**只进内存**。返回 (ok, reason_code)。"""
    global _BODY
    headers = headers or {}
    if not isinstance(raw, (bytes, bytearray)):
        return False, "bad_body_type"
    sha = hashlib.sha256(bytes(raw)).hexdigest()
    claimed = str(headers.get("X-Asset-Sha256") or headers.get("x-asset-sha256") or "").strip()
    if claimed and claimed.lower() != sha:
        with _lock:
            _META["last_error"] = "sha_mismatch"
            _META["origin"] = "error"
        return False, "sha_mismatch"                 # 视同取到脏数据，明确报错不将就
    try:
        doc = json.loads(bytes(raw).decode("utf-8"))
    except Exception:
        with _lock:
            _META["last_error"] = "bad_json"
            _META["origin"] = "error"
        return False, "bad_json"
    errs = validate(doc)
    if errs:
        with _lock:
            _META["last_error"] = ";".join(errs[:4])
            _META["origin"] = "error"
        return False, "shape_rejected:%s" % errs[0]
    with _lock:
        _BODY = doc
        _META.update({"sha256": sha, "version": str(headers.get("X-Asset-Version") or ""),
                      "etag": str(headers.get("ETag") or headers.get("etag") or ""),
                      "fetched_at": time.time(), "origin": "cloud", "last_error": "",
                      "basis_warnings": basis_warnings(doc)})
    return True, ""


def pending_nulls(doc=None) -> list:
    """契约 §3.3：点名"显式 null 且主理人未裁"的字段（**既不当不限、也不当 0**）。"""
    d = doc if isinstance(doc, dict) else _BODY
    fl = ((d or {}).get("free_limits") or {})
    return [f for f in PENDING_NULL_FIELDS if f in fl and fl[f] is None]


def entitlements() -> tuple:
    """给上层的唯一入口：返回 (已校验正文 | None, reason_code)。

    · 内存已有 ⇒ 直接给（幂等）；
    · 没有且已注入传输 ⇒ 拉一次（带 ETag 省流）再给；
    · 没有且未注入传输 ⇒ `(None, 'transport_not_configured')`——**不回落本地**。
    """
    with _lock:
        if _BODY is not None:
            return _BODY, ""
        fetch = _FETCH
    if fetch is None:
        return None, "transport_not_configured"
    try:
        raw, headers, reason = fetch(_META.get("etag", ""))
    except Exception as exc:                          # noqa: BLE001
        with _lock:
            _META["last_error"] = "transport:%s" % type(exc).__name__
        return None, "transport_failed"
    if raw is None:
        with _lock:
            _META["last_error"] = reason or "unavailable"
            _META["origin"] = "error"
        return None, reason or "unavailable"
    ok, code = ingest(raw, headers)
    if not ok:
        return None, code
    with _lock:
        return _BODY, ""


def meta() -> dict:
    with _lock:
        m = dict(_META)
        m["loaded"] = _BODY is not None
        m["pending_nulls"] = pending_nulls()
        m["hard_cutover"] = hard_cutover()
        return m


def reset_for_tests() -> None:
    """仅测试用：清内存态与传输注入（本件本来就无任何落盘）。"""
    global _BODY, _FETCH
    with _lock:
        _BODY = None
        _FETCH = None
        _META.update({"etag": "", "version": "", "sha256": "", "fetched_at": 0.0,
                      "origin": "", "last_error": "", "basis_warnings": []})
