# -*- coding: utf-8 -*-
"""t165 锁：C1 云端权益地基（形状闸/root_digest 复算/内存态/不降级/null 显式分支）

依据＝《契约-billing_entitlements-t195.md》§2/§3；跑法（人生重启口径，禁 build venv pytest）：
    cd app && python -m unittest -v test_billing_entitlements_client_20261119
零网络零花费：不接任何真端点（通道未裁定），只喂构造/产物正文给校验与内存层。

T1  **真对拍**：施工 t195 产物正文的 `root_digest` 必须等于我这边**独立复算**值，且 `validate()==[]`
T2  形状反例 8 条**各自独立构造**（改摘要／watermark 非 bool／价格字符串化／缺 free 档／
    authoritative_fields 缺 free_limits／缺 pending_owner_decisions／source.sha256 非 hex／bool 冒充正整数）
T3  正文 sha 与 `X-Asset-Sha256` 不符 ⇒ 拒 `sha_mismatch`，且不污染已加载正文
T4  **内存态**：ingest 后临时目录零新文件；静态锁——本件源码不得出现任何写文件调用
T5  未注入传输 ⇒ `(None,'transport_not_configured')`；默认（HARD=0）`limits()` 与**改前 .bak 逐字一致**
T6  `BILLING_ENTITLEMENTS_HARD=1` 且无云端 ⇒ 抛 `EntitlementsUnavailable`（明确拒，不回落本地）
T7  传入云端正文 ⇒ 阈值取云端（与本地表不同值即刻可见），本地表不参与
T8  null 显式分支：`status()['pendingDecisions']` 点名待裁项；且**不因未裁就拦客户**（不当 0）；
    两臂对照——改前 .bak 的 `status()` **没有**该字段（证锁非恒绿）
T9  `offline_fallback_allowed`/`local_override_allowed` 翻 true ⇒ 只出告警，**行为不变**（契约 §3.4）
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
STAGE = Path(r"G:\极地信号科技\中控系统\上云改造阶段12-20261117")
BODY_T195 = STAGE / "t195-计费权益资产" / "body.a21f2c86f7a0.json"

import billing_entitlements as be            # noqa: E402
import billing_quota as bq                    # noqa: E402

PRICING = {"free_limits": {"exports_per_month": 3, "exports_per_day": None,
                           "brain_ask_per_day": None, "max_height": 720,
                           "watermark": False, "max_duration_seconds": None}}


def good_body():
    """按契约 §2 造一份**能通过闸**的最小正文（root_digest 现场复算，不硬编码）。"""
    doc = {
        "asset_kind": be.ASSET_KIND, "entitlement_version": "t165-test-abc",
        "source": {"path": "app/src/billing_pricing.json", "kind": "client_ship",
                   "sha256": "a" * 64, "bytes": 4097, "pricing_version": 2,
                   "pricing_updated_at": "2026-11-14", "drift_accepted": False,
                   "drift_note": []},
        "pricing_version": 2, "currency": "CNY", "credit_milli": 1000, "hold_ttl_seconds": 900,
        "plans": {"free": {"label": "免费", "period": "month", "pricing_status": "pending",
                           "enabled": True},
                  "month": {"label": "月", "period": "month", "pricing_status": "active",
                            "enabled": True, "days": 31, "price_cents": 3900,
                            "list_price_cents": 4900}},
        "free_limits": {"exports_per_month": 7, "exports_per_day": None,
                        "brain_ask_per_day": None, "max_height": 1080,
                        "max_duration_seconds": None, "watermark": True},
        "actions": {"export": {"label": "导出", "unit": "次", "settle_rule": "on_success",
                               "billable": True, "unit_milli": 1000, "min_milli": 0}},
        "credit_validity": {"purchased": 365, "granted_grace_days": 7,
                            "remind_days_before_expiry": [3]},
        "read_only_basis": {"authority": "member-center",
                            "authoritative_fields": ["plans", "free_limits", "actions"],
                            "local_override_allowed": False, "offline_fallback_allowed": False,
                            "pending_owner_decisions": ["exports_per_day"], "note": "t165"},
    }
    doc["root_digest"] = be.canonical_root_digest(doc)
    return doc


def con_with_usage():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE quota_usage (user_id TEXT, feature TEXT, period_key TEXT, used INT)")
    return con


class Gate(unittest.TestCase):
    def setUp(self):
        be.reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="t165_be_")

    def tearDown(self):
        be.reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("BILLING_ENTITLEMENTS_HARD", None)

    def test_T1_root_digest_matches_server_body(self):
        if not BODY_T195.is_file():
            self.skipTest("施工 t195 产物不在位（%s），真对拍留到接线轮" % BODY_T195)
        raw = BODY_T195.read_bytes()
        doc = json.loads(raw.decode("utf-8"))
        self.assertEqual(be.validate(doc), [], "契约产物应通过闸，实际被拒")
        self.assertEqual(doc["root_digest"], be.canonical_root_digest(doc),
                         "🔴 我的复算与服务端产物摘要不一致 ⇒ 两份口径（t144 教训复发）")
        ok, code = be.ingest(raw, {"X-Asset-Sha256": __import__("hashlib").sha256(raw).hexdigest()})
        self.assertTrue(ok, code)
        body, why = be.entitlements()
        self.assertIsNotNone(body, why)
        # 真源三项 null 必须被点名（不许被解释掉）
        self.assertEqual(set(body["free_limits"][k] for k in be.PENDING_NULL_FIELDS), {None},
                         "t195 产物里这三项应仍是显式 null")

    def test_T2_shape_rejections(self):
        cases = []
        d = good_body(); d["root_digest"] = "b" * 64; cases.append((d, "root_digest_mismatch"))
        d = good_body(); d["free_limits"]["watermark"] = 1; cases.append((d, "bad_watermark_type"))
        d = good_body(); d["plans"]["month"]["price_cents"] = "3900"; cases.append((d, "bad_price_cents"))
        d = good_body(); d["plans"].pop("free"); cases.append((d, "missing_free_plan"))
        d = good_body(); d["read_only_basis"]["authoritative_fields"] = ["actions"]
        cases.append((d, "bad_authoritative_fields"))
        d = good_body(); d["read_only_basis"].pop("pending_owner_decisions")
        cases.append((d, "missing_pending_owner_decisions"))
        d = good_body(); d["source"]["sha256"] = "zz"; cases.append((d, "bad_source_sha256"))
        d = good_body(); d["credit_milli"] = True; cases.append((d, "bad_credit_milli"))
        self.assertEqual(len(cases), 8)
        for doc, want in cases:
            errs = be.validate(doc)
            self.assertTrue(any(e.startswith(want) for e in errs),
                          "反例 %s 没被拒（errs=%s）" % (want, errs))

    def test_T3_sha_mismatch_and_no_poisoning(self):
        raw = json.dumps(good_body(), ensure_ascii=False).encode("utf-8")
        ok, code = be.ingest(raw, {"X-Asset-Sha256": "0" * 64})
        self.assertFalse(ok)
        self.assertEqual(code, "sha_mismatch")
        self.assertIsNone(be._BODY, "校验不过却把脏正文装进了内存")

    def test_T4_memory_only_no_disk_write(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            raw = json.dumps(good_body(), ensure_ascii=False).encode("utf-8")
            import hashlib
            be.ingest(raw, {"X-Asset-Sha256": hashlib.sha256(raw).hexdigest()})
            left = list(Path(self.tmp).rglob("*"))
            self.assertEqual([p for p in left if p.is_file()], [],
                             "🔴 权益正文落盘了（契约 §3.1 内存态）：%s" % left)
        finally:
            os.chdir(cwd)
        src = (APP / "billing_entitlements.py").read_text(encoding="utf-8")
        src = re.sub(r'""".*?"""', "", src, flags=re.S)   # 去 docstring
        src = re.sub(r"#[^\n]*", "", src)                  # 去行内注释
        for bad in ("write_text", "open(", "_save_cache", "json.dump(", "mkdir("):
            self.assertNotIn(bad, src, "静态锁：内存态件里不该出现 %r" % bad)

    def test_T5_default_behaviour_identical_to_before(self):
        body, why = be.entitlements()
        self.assertIsNone(body)
        self.assertEqual(why, "transport_not_configured")
        new = bq.limits(PRICING, "free")
        bak = APP / "billing_quota.py.bak-20261119-t165"
        if not bak.is_file():
            self.skipTest("改前 .bak 不在位，无法两臂对照")
        import importlib.util as spec
        m = spec.module_from_spec(spec.spec_from_file_location("bq_before", str(bak)))
        spec.loader.exec_module(m)
        self.assertEqual(new, m.limits(PRICING, "free"),
                         "🔴 默认路径行为变了——现网离线兜底语义必须一字不变")
        self.assertEqual(bq.status(con_with_usage(), "u1", "export", PRICING, "free")["blocked"],
                         m.status(con_with_usage(), "u1", "export", PRICING, "free")["blocked"])

    def test_T6_hard_cutover_refuses_local_fallback(self):
        os.environ["BILLING_ENTITLEMENTS_HARD"] = "1"
        with self.assertRaises(bq.EntitlementsUnavailable) as ctx:
            bq.limits(PRICING, "free")
        self.assertIn("不回落本地", str(ctx.exception))

    def test_T7_cloud_values_win(self):
        lim = bq.limits(PRICING, "free", entitlements=good_body())
        self.assertEqual(lim["export"]["month"], 7, "云端 exports_per_month=7 没生效")
        self.assertNotEqual(lim["export"]["month"],
                            bq.limits(PRICING, "free")["export"]["month"])

    def test_T8_pending_nulls_named_not_silent(self):
        con = con_with_usage()
        st = bq.status(con, "u1", "export", PRICING, "free", entitlements=good_body())
        self.assertIn("exports_per_day", st["pendingDecisions"], "null 没被点名＝仍在静默当不限")
        self.assertIn("max_duration_seconds", st["pendingDecisions"])
        self.assertFalse(st["blocked"], "未裁项不得当成 0 去拦客户")
        self.assertEqual([p["period"] for p in st["periods"]], ["month"],
                         "未裁的 day 周期不该进限额表")
        bak = APP / "billing_quota.py.bak-20261119-t165"
        if bak.is_file():
            import importlib.util as spec
            m = spec.module_from_spec(spec.spec_from_file_location("bq_before2", str(bak)))
            spec.loader.exec_module(m)
            old = m.status(con_with_usage(), "u1", "export", PRICING, "free")
            self.assertNotIn("pendingDecisions", old,
                             "🔴 改前也有该字段 ⇒ 本条锁是恒绿假闸")

    def test_T9_basis_flags_do_not_change_behaviour(self):
        doc = good_body()
        doc["read_only_basis"]["offline_fallback_allowed"] = True
        doc["read_only_basis"]["local_override_allowed"] = True
        doc["root_digest"] = be.canonical_root_digest(doc)     # 注释类字段不进摘要，重算仍自洽
        self.assertIn("basis_offline_fallback_allowed_not_false",
                      ",".join(be.basis_warnings(doc)))
        self.assertEqual(bq.limits(PRICING, "free", entitlements=doc)["export"]["month"], 7,
                         "降级开关影响了行为（契约 §3.4 禁止）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
