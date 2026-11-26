"""job_ticket: HMAC-signed execution credential + TTL + nonce + status machine.

Design source: 设计稿 §四 步骤 4/5/7 and schema-credits.sql job_ticket.
Signed tuple = (user_id, job_id, action, quoted_milli, nonce, expire_at) per the design.
We encode the tuple as canonical JSON before HMAC (instead of pipe-joining) so a '|'
inside an id cannot shift field boundaries.

Status machine: issued -> consumed -> settled | released | expired
                issued ----------> settled | released | expired   (short tasks may skip consume)
Terminal states are final; a second *different* operation is an InvalidState error,
while a repeated *same* operation is idempotent at the service layer.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid

from . import db_migrate
from .errors import (InvalidState, InvalidTicketSignature, TicketConflict,
                     TicketExpired, TicketNotFound, ValidationError)

TOKEN_PREFIX = 'v1'
STATUSES = ('issued', 'consumed', 'settled', 'released', 'expired')
TERMINAL = ('settled', 'released', 'expired')


def _canonical(user_id, job_id, action, quoted_milli, nonce, expire_at):
    fields = [str(user_id), str(job_id), str(action), int(quoted_milli),
              str(nonce), int(expire_at)]
    return json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def sign(secret, *, user_id, job_id, action, quoted_milli, nonce, expire_at):
    if not isinstance(secret, (str, bytes)) or not secret:
        raise ValidationError('ticket 密钥不能为空。')
    key = secret.encode('utf-8') if isinstance(secret, str) else secret
    return hmac.new(key, _canonical(user_id, job_id, action, quoted_milli, nonce,
                                    expire_at), hashlib.sha256).hexdigest()


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def _b64url_decode(text):
    pad = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def encode_token(secret, ticket):
    rows = {k: ticket[k] for k in ('user_id', 'job_id', 'action',
                                    'quoted_milli', 'nonce', 'expire_at')}
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    sig = sign(secret, **rows)
    return '%s.%s.%s' % (TOKEN_PREFIX, _b64url_encode(payload), sig)


def verify_token(token, secret, *, now=None):
    """Offline check (step 5): signature + TTL only. Returns the signed payload."""
    if not isinstance(token, str) or token.count('.') != 2:
        raise InvalidTicketSignature('ticket 格式非法。')
    prefix, body, sig = token.split('.')
    if prefix != TOKEN_PREFIX:
        raise InvalidTicketSignature('ticket 版本不受支持。')
    try:
        payload = json.loads(_b64url_decode(body).decode('utf-8'))
        required = ('user_id', 'job_id', 'action', 'quoted_milli', 'nonce', 'expire_at')
        if not isinstance(payload, dict) or set(payload) != set(required):
            raise ValueError('fields')
        for key in ('quoted_milli', 'expire_at'):
            if isinstance(payload[key], bool) or not isinstance(payload[key], int):
                raise ValueError(key)
        for key in ('user_id', 'job_id', 'action', 'nonce'):
            if not isinstance(payload[key], str) or not payload[key]:
                raise ValueError(key)
    except (ValueError, TypeError) as exc:
        raise InvalidTicketSignature('ticket 载荷非法。') from exc
    expected = sign(secret, **{k: payload[k] for k in required})
    if not hmac.compare_digest(expected, sig):
        raise InvalidTicketSignature('ticket 签名不匹配（伪造或密钥不符）。')
    ts = int(now if now is not None else time.time())
    if ts >= int(payload['expire_at']):
        raise TicketExpired('ticket 已过期。', expire_at=int(payload['expire_at']), now=ts)
    return payload


def _ticket_dict(row):
    return dict(row) if row is not None else None


def load(con, *, job_id=None, ticket_id=None):
    if job_id is not None:
        return _ticket_dict(con.execute('SELECT * FROM job_ticket WHERE job_id=?', (job_id,)).fetchone())
    if ticket_id is not None:
        return _ticket_dict(con.execute('SELECT * FROM job_ticket WHERE id=?', (ticket_id,)).fetchone())
    raise ValidationError('job_id 或 ticket_id 必填。')


def require(con, *, job_id=None, ticket_id=None):
    ticket = load(con, job_id=job_id, ticket_id=ticket_id)
    if ticket is None:
        raise TicketNotFound('ticket 不存在：job_id=%r ticket_id=%r' % (job_id, ticket_id),
                             job_id=job_id, ticket_id=ticket_id)
    return ticket


def token_for(secret, ticket):
    return encode_token(secret, ticket)


def issue(con, *, user_id, job_id, action, quoted_milli, secret, ttl_seconds, now=None,
          ticket_id=None):
    """Sign and persist a ticket for a held job. One ticket per job_id (unique index)."""
    if not isinstance(job_id, str) or not job_id:
        raise ValidationError('job_id 必填。')
    if not isinstance(action, str) or not action:
        raise ValidationError('action 必填。')
    quoted_milli = int(quoted_milli)
    if quoted_milli < 0:
        raise ValidationError('quoted_milli 不能为负。')
    ttl_seconds = int(ttl_seconds)
    if ttl_seconds <= 0:
        raise ValidationError('ticket TTL 必须为正。')
    ts = int(now if now is not None else time.time())
    ticket = {
        'id': ticket_id or ('tkt_' + uuid.uuid4().hex[:16]),
        'user_id': str(user_id),
        'job_id': job_id,
        'action': action,
        'quoted_milli': quoted_milli,
        'nonce': secrets.token_hex(16),
        'issued_at': ts,
        'expire_at': ts + ttl_seconds,
        'status': 'issued',
        'created_at': ts,
    }
    ticket['signature'] = sign(secret, **{k: ticket[k] for k in
                                          ('user_id', 'job_id', 'action', 'quoted_milli',
                                           'nonce', 'expire_at')})
    try:
        con.execute('INSERT INTO job_ticket'
                    '(id, user_id, job_id, action, quoted_milli, nonce, issued_at,'
                    ' expire_at, signature, status, created_at)'
                    ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (ticket['id'], ticket['user_id'], ticket['job_id'], ticket['action'],
                     ticket['quoted_milli'], ticket['nonce'], ticket['issued_at'],
                     ticket['expire_at'], ticket['signature'], ticket['status'],
                     ticket['created_at']))
    except db_migrate.sqlite3.IntegrityError as exc:
        raise TicketConflict('该 job_id 已签发 ticket。', job_id=job_id) from exc
    ticket['token'] = encode_token(secret, ticket)
    return ticket


def transition(con, ticket_id, to_status, *, allowed_from, now=None):
    """CAS status change; raises InvalidState when the ticket is not in allowed_from."""
    if to_status not in STATUSES:
        raise ValidationError('未知 ticket 状态：%r' % (to_status,))
    allowed = tuple(allowed_from)
    row = con.execute('SELECT status FROM job_ticket WHERE id=?', (ticket_id,)).fetchone()
    if row is None:
        raise TicketNotFound('ticket 不存在：%r' % (ticket_id,), ticket_id=ticket_id)
    current = row['status']
    if current == to_status:
        if to_status in TERMINAL:
            # repeating the same terminal transition is a no-op, not an error
            return _ticket_dict(con.execute('SELECT * FROM job_ticket WHERE id=?',
                                            (ticket_id,)).fetchone())
    if current not in allowed:
        raise InvalidState('非法状态转移：%r -> %r。' % (current, to_status),
                           ticket_id=ticket_id, current=current, attempted=to_status)
    ts = int(now if now is not None else time.time())
    cur = con.execute('UPDATE job_ticket SET status=? WHERE id=? AND status=?',
                      (to_status, ticket_id, current))
    if cur.rowcount != 1:
        raise InvalidState('状态转移竞争失败：%r -> %r。' % (current, to_status),
                           ticket_id=ticket_id, current=current, attempted=to_status)
    return _ticket_dict(con.execute('SELECT * FROM job_ticket WHERE id=?', (ticket_id,)).fetchone())


def consume(con, token, secret, *, user_id=None, now=None):
    """Server-side check: signature + TTL + DB record + issued->consumed transition."""
    payload = verify_token(token, secret, now=now)
    ticket = require(con, job_id=payload['job_id'])
    expected = sign(secret, **{k: ticket[k] for k in
                               ('user_id', 'job_id', 'action', 'quoted_milli',
                                'nonce', 'expire_at')})
    for key in ('user_id', 'job_id', 'action', 'quoted_milli', 'nonce', 'expire_at'):
        if payload[key] != ticket[key]:
            raise InvalidTicketSignature('ticket 载荷与签发记录不一致。', field=key)
    if not hmac.compare_digest(expected, ticket['signature']):
        raise InvalidTicketSignature('签发记录签名损坏。')
    if user_id is not None and str(user_id) != ticket['user_id']:
        raise InvalidTicketSignature('ticket 不属于该用户。')
    if ticket['status'] == 'consumed':
        return {**ticket, 'duplicate': True}
    if ticket['status'] in TERMINAL:
        raise InvalidState('ticket 已是终态，不能开工：%r' % (ticket['status'],),
                           ticket_id=ticket['id'], current=ticket['status'])
    updated = transition(con, ticket['id'], 'consumed', allowed_from=('issued',), now=now)
    return {**updated, 'duplicate': False}
