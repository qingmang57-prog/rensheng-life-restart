# intake_history.py —— 转写/摘录入库与检索（t207 队长写域；复用 Store 主库）
# v2.4 按 r3 复验整改：幂等改 DB 级 CAS——store.put(..., key=稳定ID, expected=0)，
#   BEGIN IMMEDIATE 内查 revision，冲突抛 db.Conflict → 读旧稿比哈希 → 200/409。
#   （INSERT OR REPLACE 不抛主键冲突；expected=0 才是唯一原子闸门，进程锁不可替代。）
#   - 有 client_request_id 时记录 ID = f(rid)（SHA256 确定性导出）；
#   - 重放 = 同 ID 命中 + 正文哈希一致 → 200 幂等返回；
#   - 同 ID 命中但正文哈希不一致 → 409 rid_conflict（不静默回旧稿）；
#   - 并发/跨进程同 rid → BEGIN IMMEDIATE 串行化，expected=0 保证仅首写创建。
# 红线不变：全部记录进 workbench.sqlite3（随备份/导入导出/升级走）；删除仅限显式请求。
import hashlib
import json

from db import Conflict as _StoreConflict

KIND = 'intake_history'
IDEM_KIND = 'intake_idem'  # rid -> {target: 记录id}，仅加速索引；语义以稳定 ID 查询为准

def _stable_id(rid):
    h = hashlib.sha256(('idem:' + rid).encode('utf-8')).hexdigest()
    return 'intake_history_' + h[:32]

def _meta(it):
    t = it.get('transcript') or ''
    return {'id': it.get('id'), 'created_at': it.get('created_at'),
            'updated_at': it.get('updated_at'), 'revision': it.get('revision'),
            'source': it.get('source', 'text'), 'title': it.get('title', ''),
            'file_name': it.get('file_name', ''), 'chars': len(t),
            'preview': t[:120]}

def _replay(old, body_hash):
    """已存在同 ID 记录：正文一致→200 幂等重放；不一致→409 冲突。"""
    old_hash = (old.get('client_request_hash') or
                hashlib.sha256(str(old.get('transcript') or '').encode('utf-8')).hexdigest())
    if old_hash != body_hash:
        return {'ok': False, 'code': 'rid_conflict',
                'id': old.get('id'),
                'message': '同一请求 ID 已用于另一份正文；请改用新的 client_request_id 或保留原稿。'}, 409
    return {'ok': True, 'id': old['id'], 'created_at': old['created_at'],
            'revision': old['revision'], 'idempotent': True}, 200

def create(store, data):
    """POST /api/intake/history。无字数上限；空文本 400；同 client_request_id 幂等/冲突。"""
    text = str((data or {}).get('transcript') or '')
    if not text.strip():
        return {'ok': False, 'code': 'empty_transcript'}, 400
    rid = str((data or {}).get('client_request_id') or '')[:128]
    body_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    payload = {
        'source': str((data or {}).get('source') or 'text')[:20],
        'title': str((data or {}).get('title') or '')[:200],
        'file_name': str((data or {}).get('file_name') or '')[:200],
        'source_url': str((data or {}).get('source_url') or '')[:500],
        'client_request_id': rid,
        'client_request_hash': body_hash,
        'transcript': text,
    }
    if rid:
        try:
            item = store.put(KIND, payload, key=_stable_id(rid), expected=0)  # DB 级 CAS：已存在即 Conflict
        except _StoreConflict:
            old = store.get(KIND, _stable_id(rid))
            if old is None:
                raise
            return _replay(old, body_hash)
        try:
            store.put(IDEM_KIND, {'target': item['id']}, key=rid)  # 加速索引，可中断可回填
        except Exception:
            pass
        return {'ok': True, 'id': item['id'], 'created_at': item['created_at'],
                'revision': item['revision'], 'idempotent': False}, 201
    item = store.put(KIND, payload)
    return {'ok': True, 'id': item['id'], 'created_at': item['created_at'],
            'revision': item['revision'], 'idempotent': False}, 201

def list_items(store, limit=20, offset=0):
    """GET /api/intake/history?limit=N&offset=M。返回 items/total/next_offset(或 null)。"""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            'SELECT id,created_at,updated_at,revision,data FROM records '
            'WHERE kind=? ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?',
            (KIND, limit, offset)).fetchall()]
        total = con.execute('SELECT COUNT(*) AS n FROM records WHERE kind=?',
                            (KIND,)).fetchone()['n']
    items = []
    for r in rows:
        try:
            payload = json.loads(r.get('data') or '{}')
        except (ValueError, TypeError):
            payload = {}
        it = dict(payload)
        it.update({'id': r.get('id'), 'created_at': r.get('created_at'),
                   'updated_at': r.get('updated_at'), 'revision': r.get('revision')})
        items.append(_meta(it))
    nxt = offset + limit if offset + limit < total else None
    return {'ok': True, 'items': items, 'total': total, 'next_offset': nxt}

def get(store, iid):
    """GET /api/intake/history/<id>。返回全文。"""
    it = store.get(KIND, iid)
    if not it:
        return {'ok': False, 'code': 'not_found'}, 404
    return {'ok': True, 'item': it}, 200

def delete(store, iid):
    """DELETE /api/intake/history/<id>。仅显式用户请求；无任何自动清理路径。"""
    with store.connect() as con:
        cur = con.execute('DELETE FROM records WHERE kind=? AND id=?', (KIND, iid))
        if cur.rowcount <= 0:
            return {'ok': False, 'code': 'not_found'}, 404
    return {'ok': True}, 200
