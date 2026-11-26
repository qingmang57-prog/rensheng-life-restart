"""Schema + connection management for the credits ledger (P0, local SQLite).

DDL mirrors 设计稿 `个人IP工作台/设计/schema-credits.sql` table by table (same tables,
columns, constraints and indexes), plus one extra lookup index that does not change the
contract. Migration is idempotent: `CREATE ... IF NOT EXISTS` + `INSERT OR IGNORE` seeds.
The storage layer is intentionally thin so a cloud DB can replace it later (see
`_B1-计费骨架.md` §迁移方式).
"""
import itertools
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

# ---- canonical schema: keep in sync with 设计/schema-credits.sql -------------
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    phone         TEXT UNIQUE,
    email         TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS membership (
    user_id     TEXT PRIMARY KEY REFERENCES users(id),
    plan        TEXT NOT NULL DEFAULT 'free',
    expire_at   INTEGER,
    grace_days  INTEGER NOT NULL DEFAULT 3,
    discount_bp INTEGER NOT NULL DEFAULT 10000,
    quota_json  TEXT NOT NULL DEFAULT '{}',
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_account (
    user_id       TEXT PRIMARY KEY REFERENCES users(id),
    balance_milli INTEGER NOT NULL DEFAULT 0,
    frozen_milli  INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    delta_milli     INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    action          TEXT,
    qty             REAL,
    unit_milli      INTEGER,
    discount_bp     INTEGER NOT NULL DEFAULT 10000,
    job_id          TEXT,
    ticket_id       TEXT,
    idempotency_key TEXT NOT NULL UNIQUE,
    note            TEXT,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user_time ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_job ON credit_ledger(job_id);
-- extra (not in the design file): open-hold / closing-entry lookups by ticket.
CREATE INDEX IF NOT EXISTS idx_ledger_ticket_kind ON credit_ledger(ticket_id, kind);

CREATE TABLE IF NOT EXISTS job_ticket (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    job_id         TEXT NOT NULL,
    action         TEXT NOT NULL,
    quoted_milli   INTEGER NOT NULL,
    nonce          TEXT NOT NULL,
    issued_at      INTEGER NOT NULL,
    expire_at      INTEGER NOT NULL,
    signature      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'issued',
    created_at     INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_job ON job_ticket(job_id);

CREATE TABLE IF NOT EXISTS publish_result (
    id               TEXT PRIMARY KEY,
    ticket_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    platform         TEXT NOT NULL,
    platform_work_id TEXT,
    work_url         TEXT,
    status           TEXT NOT NULL,
    message          TEXT,
    reported_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_ticket ON publish_result(ticket_id);

CREATE TABLE IF NOT EXISTS price_book (
    action      TEXT PRIMARY KEY,
    unit        TEXT NOT NULL,
    unit_milli  INTEGER NOT NULL,
    min_milli   INTEGER NOT NULL DEFAULT 0,
    billable    INTEGER NOT NULL DEFAULT 1,
    settle_rule TEXT NOT NULL DEFAULT 'on_success',
    updated_at  INTEGER NOT NULL
);
"""

# ---- seed data -------------------------------------------------------------
# Values are the draft numbers from 设计稿 §三 (方案B-20261027, "金额全部待你定价").
# They live in `price_book` so a price change is a data update, never a code change
# (设计稿 §七 "价目热更"). Override with prices.set_price(); re-running migrate()
# never overwrites an operator-set price (INSERT OR IGNORE).
# 1 credit = 1000 milli.
DEFAULT_PRICES = (
    # action, unit, credits, min_credits, billable, settle_rule
    ('resolve', 'piece', 2, 0, 1, 'on_complete'),
    ('asr_minute', 'minute', 3, 0, 1, 'on_complete'),
    ('prepare_reference', 'piece', 5, 0, 1, 'on_complete'),
    ('prepare_content', 'piece', 20, 0, 1, 'on_complete'),
    ('keyword_caption', 'piece', 5, 0, 1, 'on_complete'),
    ('semantic_cut', 'piece', 5, 0, 1, 'on_complete'),
    ('subtitle_render', 'minute', 2, 0, 1, 'on_complete'),
    ('cover_gen', 'piece', 1, 0, 1, 'on_complete'),
    ('platform_login', 'piece', 5, 0, 1, 'on_complete'),
    ('publish_plan', 'piece', 0, 0, 0, 'free'),
    ('publish_submit', 'platform', 30, 0, 1, 'on_success'),
    ('publish_scheduled', 'piece', 30, 0, 1, 'on_success'),
    ('zip_download', 'piece', 0, 0, 0, 'free'),
    # 平台加权（§三末，待定价）：抖音30 / 快手30 / 小红书35 / 视频号40 / B站25
    ('publish_douyin', 'platform', 30, 0, 1, 'on_success'),
    ('publish_kuaishou', 'platform', 30, 0, 1, 'on_success'),
    ('publish_xiaohongshu', 'platform', 35, 0, 1, 'on_success'),
    ('publish_shipinhao', 'platform', 40, 0, 1, 'on_success'),
    ('publish_bilibili', 'platform', 25, 0, 1, 'on_success'),
)

_SAVEPOINT = itertools.count(1)


@contextmanager
def connect(db_path):
    """One short-lived SQLite connection (WAL, FK on, explicit transactions)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=20.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA busy_timeout=5000')
        con.execute('PRAGMA journal_mode=WAL')
        yield con
    finally:
        con.close()


@contextmanager
def transaction(con):
    """BEGIN IMMEDIATE, or a SAVEPOINT when already inside one (composable)."""
    if con.in_transaction:
        name = 'sp_%d' % next(_SAVEPOINT)
        con.execute('SAVEPOINT %s' % name)
        try:
            yield con
        except BaseException:
            try:
                con.execute('ROLLBACK TO %s' % name)
                con.execute('RELEASE %s' % name)
            except sqlite3.Error:
                pass
            raise
        else:
            con.execute('RELEASE %s' % name)
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


def migrate(con, *, seed=True, now=None):
    """Create/upgrade all tables. Safe to run on every process start."""
    con.executescript(SCHEMA)
    seeded = 0
    if seed:
        ts = int(now if now is not None else time.time())
        with transaction(con):
            for action, unit, credits, min_credits, billable, rule in DEFAULT_PRICES:
                cur = con.execute(
                    'INSERT OR IGNORE INTO price_book'
                    '(action, unit, unit_milli, min_milli, billable, settle_rule, updated_at)'
                    ' VALUES (?,?,?,?,?,?,?)',
                    (action, unit, int(credits) * 1000, int(min_credits) * 1000,
                     int(billable), rule, ts))
                seeded += cur.rowcount
    version = con.execute('PRAGMA user_version').fetchone()[0]
    if version < SCHEMA_VERSION:
        con.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
        version = SCHEMA_VERSION
    return {'schema_version': version, 'seeded_prices': seeded}


def ensure(db_path, *, seed=True, now=None):
    """Convenience: open, migrate, close. Returns the migration report."""
    with connect(db_path) as con:
        return migrate(con, seed=seed, now=now)


def now_ts():
    return int(time.time())
