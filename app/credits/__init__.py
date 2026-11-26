"""计费 P0 账本骨架（本地 SQLite 先行，云端后续替换存储层）。

设计稿：`个人IP工作台/设计/计费体系-会员加积分-方案B-20261027.md`
建表稿：`个人IP工作台/设计/schema-credits.sql`

公开入口只有 `BillingService`；子模块职责：
    db_migrate  建表/连接/事务（可重复执行）
    accounts    用户 / 会员档 / 余额缓存行
    prices      price_book 读取 / 折扣报价（价目不硬编码）
    ledger      credit_ledger 只追加 / 余额由流水重算 / hold/settle/release/对账
    tickets     job_ticket HMAC 签名 / TTL / nonce / 状态机
    service     §四 全流程编排 + 幂等

不做的事（P0 边界）：不改 app.py、不建 HTTP 路由、不接支付、不执行配额（§六 P2）。
"""
from .errors import (AccountDisabled, AccountError, AccountNotFound, CreditsError,
                     IdempotencyConflict, InsufficientCredits, InvalidResult,
                     InvalidState, InvalidTicketSignature, TicketConflict, TicketError,
                     TicketExpired, TicketNotFound, UnknownAction, ValidationError)
from .service import BillingService

__all__ = [
    'BillingService',
    'CreditsError', 'ValidationError', 'AccountError', 'AccountNotFound',
    'AccountDisabled', 'UnknownAction', 'InsufficientCredits', 'IdempotencyConflict',
    'InvalidState', 'TicketError', 'TicketNotFound', 'TicketConflict',
    'InvalidTicketSignature', 'TicketExpired', 'InvalidResult',
]

__version__ = '0.1.0-p0'
