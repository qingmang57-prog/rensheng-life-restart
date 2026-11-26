"""计费数据层：五张表 + entitlement 两个部分唯一索引（本地 SQLite）。

沿用 app/db.py 的连接方式：sqlite3.connect(timeout=20) + row_factory=Row + `with con`，
显式事务用 BEGIN IMMEDIATE（写锁先行；配合幂等唯一索引，解决并发重复扣）。

为什么单独一个 billing.sqlite3：
  1. workbench.sqlite3 由 app/db.py 维护，本轮不改它；
  2. app/credits/（20261027 P0 草稿）自带 users 与 plan=free|member|svip 的 membership，
     与本方案的 free|month|year 语义不同，共库会互相污染。
切云端时只替换本文件的 connect()/migrate()，五张表结构保持不变。

金额约定（硬约束，禁止浮点）：
  - 积分：1 积分 = 1000 毫分（balance_milli / delta_milli / unit_milli / qty_milli）。
  - 人民币：分（price_cents），只出现在 billing_pricing.json 与会员退款计算。
  - qty_milli = 用量 × 1000（3.5 分钟 = 3500），保证按分钟计费也不落浮点。

积分有效期（2026-11-03 定稿）：
  - 购买积分：expires_at = NULL，永久有效、永不清零。
  - 赠送积分：expires_at = 名义到期日；expiring_at = expires_at + 30 天宽限，宽限过仍未用才作废。
  - 作废必须落一条 kind='void' 的追加流水（lot_id 指向原赠送流水），不许直接改余额。
"""
import itertools
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 5

LEDGER_KINDS = ('grant', 'purchase', 'hold', 'settle', 'release', 'adjust', 'void')
MEMBERSHIP_STATUSES = ('active', 'paused', 'expired', 'cancelled')
ENTITLEMENT_KINDS = ('template', 'avatar')
GRANT_SOURCES = ('purchase', 'membership', 'gift', 'legacy')

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS membership (
    user_id      TEXT PRIMARY KEY,
    plan         TEXT NOT NULL DEFAULT 'free',
    status       TEXT NOT NULL DEFAULT 'active',
    started_at   INTEGER,
    expire_at    INTEGER,
    paused_at    INTEGER,
    paused_until INTEGER,
    pause_used   INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL,
    CHECK (plan <> ''),
    CHECK (status IN ('active','paused','expired','cancelled'))
);

CREATE TABLE IF NOT EXISTS credit_account (
    user_id         TEXT PRIMARY KEY,
    balance_milli   INTEGER NOT NULL DEFAULT 0,
    frozen_milli    INTEGER NOT NULL DEFAULT 0,
    granted_milli   INTEGER NOT NULL DEFAULT 0,
    purchased_milli INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    delta_milli     INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    action          TEXT,
    qty_milli       INTEGER,
    unit_milli      INTEGER,
    discount_bp     INTEGER NOT NULL DEFAULT 10000,
    job_id          TEXT,
    lot_id          INTEGER,
    idempotency_key TEXT NOT NULL,
    expires_at      INTEGER,
    expiring_at     INTEGER,
    note            TEXT,
    created_at      INTEGER NOT NULL,
    CHECK (kind IN ('grant','purchase','hold','settle','release','adjust','void')),
    CHECK (discount_bp BETWEEN 0 AND 10000)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_idem
    ON credit_ledger(user_id, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_ledger_user_time
    ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_job
    ON credit_ledger(user_id, job_id, kind);
CREATE INDEX IF NOT EXISTS idx_ledger_lot
    ON credit_ledger(user_id, lot_id);

CREATE TABLE IF NOT EXISTS entitlement (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ref_id       TEXT,
    source_sha   TEXT,
    grant_source TEXT NOT NULL,
    granted_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    ledger_id    INTEGER,
    status       TEXT NOT NULL DEFAULT 'active',
    CHECK (kind IN ('template','avatar')),
    CHECK (grant_source IN ('purchase','membership','gift','legacy')),
    CHECK (status IN ('active','revoked')),
    CHECK ((kind = 'template' AND ref_id IS NOT NULL AND source_sha IS NULL)
        OR (kind = 'avatar'   AND ref_id IS NULL     AND source_sha IS NOT NULL))
);

-- SQLite 的 UNIQUE 对 NULL 不生效：模板/形象必须各用一条部分唯一索引。
CREATE UNIQUE INDEX IF NOT EXISTS ux_ent_template
    ON entitlement(user_id, ref_id) WHERE kind = 'template';
CREATE UNIQUE INDEX IF NOT EXISTS ux_ent_avatar
    ON entitlement(user_id, source_sha) WHERE kind = 'avatar';
CREATE INDEX IF NOT EXISTS idx_ent_user_kind
    ON entitlement(user_id, kind, status);

CREATE TABLE IF NOT EXISTS funnel_event (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    event      TEXT NOT NULL,
    project_id TEXT,
    props_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_funnel_user_event
    ON funnel_event(user_id, event, created_at);
-- 同一 user+event+project 只写一次；project 为空时按 user+event 去重（NULL 不参与 UNIQUE）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_funnel_project
    ON funnel_event(user_id, event, project_id) WHERE project_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_funnel_no_project
    ON funnel_event(user_id, event) WHERE project_id IS NULL;

-- 卡密兑换：只存哈希，不存明文；积分码与套餐码二选一。
CREATE TABLE IF NOT EXISTS redeem_code (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash     TEXT NOT NULL UNIQUE,
    code_tail     TEXT NOT NULL,
    batch         TEXT NOT NULL,
    credits_milli INTEGER NOT NULL DEFAULT 0,
    plan          TEXT,
    plan_days     INTEGER,
    expires_at    INTEGER,
    created_at    INTEGER NOT NULL,
    created_by    TEXT,
    used_at       INTEGER,
    used_by       TEXT,
    ledger_id     INTEGER,
    note          TEXT,
    CHECK (credits_milli >= 0),
    CHECK ((credits_milli > 0 AND plan IS NULL AND plan_days IS NULL)
        OR (credits_milli = 0 AND plan IS NOT NULL AND plan_days > 0))
);
CREATE INDEX IF NOT EXISTS idx_redeem_batch ON redeem_code(batch, created_at);
CREATE INDEX IF NOT EXISTS idx_redeem_used ON redeem_code(used_by, used_at);

-- 兑换尝试审计 + 同设备连续失败限制。
CREATE TABLE IF NOT EXISTS redeem_attempt (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        INTEGER NOT NULL,
    device    TEXT NOT NULL,
    code_tail TEXT,
    ok        INTEGER NOT NULL,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_redeem_attempt_device ON redeem_attempt(device, id);

-- 免费额度/每日配额（2026-11-14 G4）：计数表 + 幂等事件表。
-- 计数只是缓存，consume 与事件写入同事务；重复提交靠 ux_quota_event 只记一次。
CREATE TABLE IF NOT EXISTS quota_usage (
    user_id      TEXT NOT NULL,
    feature      TEXT NOT NULL,
    period_key   TEXT NOT NULL,
    used         INTEGER NOT NULL DEFAULT 0,
    limit_value  INTEGER,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (user_id, feature, period_key)
);

CREATE TABLE IF NOT EXISTS quota_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    feature    TEXT NOT NULL,
    period_key TEXT NOT NULL,
    idem_key   TEXT NOT NULL,
    action     TEXT,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_quota_event
    ON quota_event(user_id, feature, idem_key);
CREATE INDEX IF NOT EXISTS idx_quota_event_time
    ON quota_event(user_id, feature, created_at);
"""

# v1 -> v2 的列增量（v1 只可能在开发机上出现过；无库时 CREATE 已含全部列）。
_COLUMN_UPGRADES = (
    ('credit_account', 'granted_milli', 'INTEGER NOT NULL DEFAULT 0'),
    ('credit_account', 'purchased_milli', 'INTEGER NOT NULL DEFAULT 0'),
    ('credit_ledger', 'lot_id', 'INTEGER'),
    ('credit_ledger', 'expiring_at', 'INTEGER'),
)

_SAVEPOINT = itertools.count(1)

# v4 -> v5：档位不再写死枚举（档位可增删）；redeem_code 支持套餐码。
_MEMBERSHIP_V5 = """
CREATE TABLE membership_v5 (
    user_id      TEXT PRIMARY KEY,
    plan         TEXT NOT NULL DEFAULT 'free',
    status       TEXT NOT NULL DEFAULT 'active',
    started_at   INTEGER,
    expire_at    INTEGER,
    paused_at    INTEGER,
    paused_until INTEGER,
    pause_used   INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL,
    CHECK (plan <> ''),
    CHECK (status IN ('active','paused','expired','cancelled'))
);
INSERT INTO membership_v5
    (user_id, plan, status, started_at, expire_at, paused_at, paused_until,
     pause_used, updated_at)
    SELECT user_id, plan, status, started_at, expire_at, paused_at, paused_until,
           pause_used, updated_at FROM membership;
DROP TABLE membership;
ALTER TABLE membership_v5 RENAME TO membership;
"""

_REDEEM_V5 = """
CREATE TABLE redeem_code_v5 (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash     TEXT NOT NULL UNIQUE,
    code_tail     TEXT NOT NULL,
    batch         TEXT NOT NULL,
    credits_milli INTEGER NOT NULL DEFAULT 0,
    plan          TEXT,
    plan_days     INTEGER,
    expires_at    INTEGER,
    created_at    INTEGER NOT NULL,
    created_by    TEXT,
    used_at       INTEGER,
    used_by       TEXT,
    ledger_id     INTEGER,
    note          TEXT,
    CHECK (credits_milli >= 0),
    CHECK ((credits_milli > 0 AND plan IS NULL AND plan_days IS NULL)
        OR (credits_milli = 0 AND plan IS NOT NULL AND plan_days > 0))
);
INSERT INTO redeem_code_v5
    (id, code_hash, code_tail, batch, credits_milli, plan, plan_days, expires_at,
     created_at, created_by, used_at, used_by, ledger_id, note)
    SELECT id, code_hash, code_tail, batch, credits_milli, NULL, NULL, expires_at,
           created_at, created_by, used_at, used_by, ledger_id, note FROM redeem_code;
DROP TABLE redeem_code;
ALTER TABLE redeem_code_v5 RENAME TO redeem_code;
CREATE INDEX IF NOT EXISTS idx_redeem_batch ON redeem_code(batch, created_at);
CREATE INDEX IF NOT EXISTS idx_redeem_used ON redeem_code(used_by, used_at);
"""


def _migrate_v5(con):
    """v4 -> v5：重建 membership（解除档位枚举）与 redeem_code（加套餐字段）。"""
    row = con.execute("SELECT sql FROM sqlite_master WHERE type='table'"
                      " AND name='membership'").fetchone()
    if row is not None and "plan IN ('free','month','year')" in (row[0] or ''):
        con.executescript(_MEMBERSHIP_V5)
    cols = [item[1] for item in con.execute('PRAGMA table_info(redeem_code)')]
    if 'plan' not in cols:
        con.executescript(_REDEEM_V5)


def default_db_path():
    """与 app/db.py 同一数据根目录，但独立文件：IP_DATA_DIR/billing.sqlite3。"""
    root = Path(os.getenv('IP_DATA_DIR') or (Path(__file__).resolve().parent / '.runtime'))
    return root / 'billing.sqlite3'


@contextmanager
def connect(db_path):
    """一次短连接；与 app/db.py 同一口径（timeout=20 + Row + with con 自动提交/回滚）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=20)
    con.row_factory = sqlite3.Row
    # 本地桌面库：WAL + NORMAL 足够（断电最多丢最后一次提交，不会坏库），避免每次扣费卡秒级 fsync。
    try:
        con.execute('PRAGMA synchronous=NORMAL')
    except sqlite3.Error:
        pass
    try:
        with con:
            yield con
    finally:
        con.close()


@contextmanager
def transaction(con):
    """BEGIN IMMEDIATE 写事务；已在事务中则用 SAVEPOINT（可组合）。

    BEGIN IMMEDIATE 先拿写锁：两个请求同幂等键并发时，后到者等前者提交，
    再读到已有流水返回 duplicate，不会各写一条。
    """
    if con.in_transaction:
        name = 'sp_%d' % next(_SAVEPOINT)
        con.execute('SAVEPOINT ' + name)
        try:
            yield con
        except BaseException:
            try:
                con.execute('ROLLBACK TO ' + name)
                con.execute('RELEASE ' + name)
            except sqlite3.Error:
                pass
            raise
        else:
            con.execute('RELEASE ' + name)
    else:
        con.execute('BEGIN IMMEDIATE')
        try:
            yield con
        except BaseException:
            try:
                con.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        else:
            con.execute('COMMIT')


def _ensure_columns(con):
    for table, column, ddl in _COLUMN_UPGRADES:
        cols = [row[1] for row in con.execute('PRAGMA table_info(%s)' % table)]
        if column not in cols:
            con.execute('ALTER TABLE %s ADD COLUMN %s %s' % (table, column, ddl))


def migrate(con):
    """建表；可重复执行（CREATE ... IF NOT EXISTS + 列增量 + v5 重建）。"""
    con.executescript(SCHEMA)
    _ensure_columns(con)
    _migrate_v5(con)
    version = con.execute('PRAGMA user_version').fetchone()[0]
    if version < SCHEMA_VERSION:
        con.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        version = SCHEMA_VERSION
    return {'schema_version': version}


def ensure(db_path):
    """开库 -> 迁移 -> 关库。服务启动时调用一次。"""
    with connect(db_path) as con:
        return migrate(con)


def now_ts():
    return int(time.time())
