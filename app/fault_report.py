# -*- coding: utf-8 -*-
"""ccE 客户端故障上报（t199，2026-11-25）——trace 贯穿 + 离线有界队列 + 脱敏。

设计：
- fault_id 与 trace_id 分开：fault_id 是 UUID 故障号；trace_id 由当前请求/任务带下来，
  客户端响应头 X-Trace-Id 可直接回填，同一条故障在客户端日志与中控检索里用同一个号。
- 只收白名单上下文（阶段/错误码/依赖能力/可操作原因/版本/构建/平台/请求/job），
  不存正文、Cookie、密钥、完整个人 URL；URL 只留 scheme+host，路径和查询串掩码。
- 数据目录 IP_DATA_DIR；队列 <数据目录>/faults/queue.jsonl，未发送环形上限 MAX_QUEUE，
  已发送保留 RETAIN_SENT 条供审计；flush 逐条 POST 中控 /api/defects/ingest，2xx 记 sent。
- 任何异常都吞掉，绝不影响主流程；离线队列可 export 人工转交。
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    from applog import scrub
except ImportError:  # 允许从包内任意目录导入
    from app.applog import scrub

MAX_QUEUE = 200
MAX_SUMMARY = 500
MAX_FIELD = 300
SENT_MARK = 'sent'
RETAIN_SENT = 50
MAX_TRACE = 64
MAX_FAULT_ID = 64

SAFE_CONTEXT_KEYS = (
    'request_id', 'job_id', 'trace_id', 'stage', 'error_code', 'capability',
    'actionable', 'client_version', 'build', 'platform', 'source', 'method',
    'route', 'module', 'detail', 'attempt', 'elapsed_ms', 'status', 'url',
)
_CLIENT_KEYS = ('client_version', 'build', 'platform')
_TRACE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$')
_URL_RE = re.compile(r'(?i)\b(https?)://([^/\s?#]+)([^\s]*?)(?=[\s"\'<>）)]|$)')
_LOCK = threading.RLock()
_TRACE = threading.local()


def _data_dir():
    root = os.getenv('IP_DATA_DIR')
    return Path(root) if root else Path(__file__).resolve().parent / '.runtime'


def _fault_dir():
    return _data_dir() / 'faults'


def _queue_path():
    return _fault_dir() / 'queue.jsonl'


def new_trace():
    return 'tr-' + uuid.uuid4().hex[:16]


def clean_trace(value):
    text = str(value or '').strip()
    return text if _TRACE_RE.match(text) else ''


def bind_trace(value=''):
    """给当前线程绑定 trace；空/非法时生成新号。返回最终 trace。"""
    trace = clean_trace(value) or new_trace()
    _TRACE.value = trace
    return trace


def current_trace():
    return getattr(_TRACE, 'value', '') or ''


def client_meta():
    """当前客户端版本/构建/平台；版本文件缺失时留空，不阻断上报。"""
    version = build = ''
    try:
        meta = json.loads((Path(__file__).resolve().parent / 'web' / 'version.json').read_text(encoding='utf-8'))
        version = str(meta.get('version') or '')
        build = str(meta.get('build') or '')
    except Exception:
        pass
    return {'client_version': version, 'build': build, 'platform': sys.platform}


def _mask_urls(text):
    def repl(match):
        return match.group(1) + '://' + match.group(2) + '/<路径已隐藏>'
    return _URL_RE.sub(repl, str(text))


def _clean(value, limit=MAX_FIELD):
    """先按 applog 口径去密钥/路径，再把完整 URL 降成 scheme+host。"""
    return _mask_urls(scrub(str(value)))[:limit]


def _safe_id(value, limit=MAX_FAULT_ID):
    text = str(value or '').strip()
    return text if re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$', text) else ''


def _rows():
    path = _queue_path()
    out = []
    try:
        with open(str(path), encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def _rewrite(rows):
    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    with open(str(tmp), 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
    tmp.replace(path)


def record(kind, summary='', trace_id=None, stage='', error_code='', capability='',
           actionable='', detail='', **context):
    """记录一条故障（本地入队，不上网）。返回 fault_id；任何异常吞掉返回 None。

    t207 约定也支持 record(kind, stage=..., detail=..., capability=...)：
    detail 是给客户看的可操作短句，和 summary 分开存；未给 summary 时回填。
    """
    try:
        if detail and not summary:
            summary = detail
        if detail and 'detail' not in context:
            context['detail'] = detail
        with _LOCK:
            fault_id = str(uuid.uuid4())
            trace = clean_trace(trace_id) or current_trace() or new_trace()
            safe = {
                'fault_id': fault_id,
                'trace_id': trace,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'product': 'rensheng',
                'kind': _clean(kind or 'unknown', 120),
                'summary': _clean(summary, MAX_SUMMARY),
                'stage': _clean(stage, 60),
                'error_code': _clean(error_code, 80),
                'capability': _clean(capability, 120),
                'actionable': _clean(actionable, MAX_FIELD),
                'status': 'pending',
            }
            client = {k: v for k, v in client_meta().items() if v}
            for key in _CLIENT_KEYS:
                if context.get(key):
                    client[key] = _clean(context[key], 120)
            if client:
                safe['client'] = client
            fields = {}
            for key, value in context.items():
                key = str(key)
                if key in _CLIENT_KEYS or key not in SAFE_CONTEXT_KEYS:
                    continue
                if value is None or value == '':
                    continue
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, default=str)
                fields[key] = _clean(value)
            if fields:
                safe['context'] = fields
            rows = _rows()
            rows.append(safe)
            pending = [r for r in rows if r.get('status') != SENT_MARK]
            if len(pending) > MAX_QUEUE:
                pending = pending[-MAX_QUEUE:]
            sent = [r for r in rows if r.get('status') == SENT_MARK][-RETAIN_SENT:]
            _rewrite(pending + sent)
            return fault_id
    except Exception:
        return None


def record_exception(exc, kind='unhandled', summary='', **context):
    """把异常收成一条脱敏故障；只记类型，不记正文/局部变量。

    t207 修复：调用方可在 context 里带 error_code（如 app.py 500 兜底传
    'HTTP_500'）——此处合并为一次（调用方优先，其次 exc.code，再次类名），
    杜绝 record() 收到重复关键字 TypeError 反把异常处理器炸掉、客户拿到
    断连而非结构化 500。本函数自身绝不抛：任何失败静默返回 None。
    """
    try:
        error_code = str(context.pop('error_code', '') or getattr(exc, 'code', '')
                         or type(exc).__name__)
        text = summary or ('%s 失败：%s' % (kind, type(exc).__name__))
        return record(kind, text, error_code=error_code, **context)
    except Exception:
        return None


def record_from_request(body, trace_id=''):
    """app.py 接线入口：POST /api/faults/report。"""
    if not isinstance(body, dict):
        return {'ok': False, 'error': 'body must be json object'}
    context = body.get('context') if isinstance(body.get('context'), dict) else {}
    client = body.get('client') if isinstance(body.get('client'), dict) else {}
    for key in _CLIENT_KEYS:
        if client.get(key) and key not in context:
            context[key] = client[key]
    allowed = {k: v for k, v in context.items() if k in SAFE_CONTEXT_KEYS}
    # t207 同型排查：trace_id 也在 SAFE_CONTEXT_KEYS 里，若不在此处消费掉，
    # client/context.trace_id 会随 **allowed 重复传给 record() 的具名参数
    # → TypeError（客户端可远程触发的处理器崩溃路径）。
    ctx_trace = str(allowed.pop('trace_id', '') or '')
    fault_id = record(
        str(body.get('kind') or 'unknown'),
        str(body.get('summary') or ''),
        trace_id=str(body.get('trace_id') or trace_id or ctx_trace or ''),
        stage=str(body.get('stage') or allowed.pop('stage', '') or ''),
        error_code=str(body.get('error_code') or allowed.pop('error_code', '') or ''),
        capability=str(body.get('capability') or allowed.pop('capability', '') or ''),
        actionable=str(body.get('actionable') or allowed.pop('actionable', '') or ''),
        **allowed
    )
    return {'ok': bool(fault_id), 'fault_id': fault_id,
            'trace_id': clean_trace(str(body.get('trace_id') or trace_id or ctx_trace or '')) or current_trace()}


def flush(base_url, token=None, timeout=5, install_id=''):
    """向中控 ingest 端点补报全部未发送记录（幂等）。返回 {'sent':n,'kept':n}。

    t207 竞态修复：POST 期间 record() 新增的记录绝不被回写抹掉——
    回写时锁内**重读当前队列**，仅对本次真正 2xx 的 fault_id 置 sent，其余原样保留。
    """
    import urllib.request
    import urllib.error
    with _LOCK:
        rows = _rows()
    pending = [r for r in rows if r.get('status') != SENT_MARK]
    sent = kept = 0
    delivered = {}  # fault_id -> sent_ts：本次 flush 真正送达的记录
    for row in pending:
        payload = json.dumps({k: v for k, v in row.items() if k != 'status'},
                             ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(base_url.rstrip('/') + '/api/defects/ingest',
                                     data=payload, method='POST',
                                     headers={'Content-Type': 'application/json'})
        if install_id:                # 中控安装白名单认证用（补丁草稿 §2 契约）
            req.add_header('X-Install-Id', str(install_id))
        if token:
            req.add_header('X-Client-Token', token)
        if row.get('trace_id'):
            req.add_header('X-Trace-Id', str(row['trace_id']))
        ok_row = False
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if 200 <= resp.status < 300:
                        delivered[str(row.get('fault_id'))] = time.strftime('%Y-%m-%dT%H:%M:%S')
                        sent += 1
                        ok_row = True
                break
            except urllib.error.HTTPError:
                break
            except (urllib.error.URLError, OSError, ValueError):
                # 本地连接抖动/超时重试一次；中控按 fault_id 幂等，不会重复建单。
                if attempt == 0:
                    time.sleep(0.2)
                    continue
                break
        if not ok_row:
            kept += 1
    if delivered:
        with _LOCK:
            fresh = _rows()  # 重读现队列：包含 POST 期间 record() 新增的条目
            out = []
            for r in fresh:
                fid = str(r.get('fault_id'))
                if fid in delivered and r.get('status') != SENT_MARK:
                    r['status'] = SENT_MARK
                    r['sent_ts'] = delivered[fid]
                out.append(r)
            pend = [r for r in out if r.get('status') != SENT_MARK][-MAX_QUEUE:]
            mark = [r for r in out if r.get('status') == SENT_MARK][-RETAIN_SENT:]
            _rewrite(pend + mark)
    return {'sent': sent, 'kept': kept}


def export(path=None):
    """脱敏导出兜底：把队列（含未发送）导出为可人工转交的 JSON 包。返回路径。"""
    rows = _rows()
    out = path or str(_fault_dir() / ('export-%s.json' % time.strftime('%Y%m%d-%H%M%S')))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w', encoding='utf-8') as fh:
        json.dump({'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
                   'count': len(rows), 'items': rows}, fh, ensure_ascii=False, indent=1)
    return out


def _selftest():
    os.environ['IP_DATA_DIR'] = str(Path(__file__).resolve().parent / '.selftest_data')
    import shutil
    shutil.rmtree(_fault_dir(), ignore_errors=True)
    trace = bind_trace('tr-selftest-0001')
    for _ in range(MAX_QUEUE + 20):
        record('noise', 'filler')
    r1 = record('llm_timeout', '调用超时 api_key=sk-abcdef1234567890 home=' + 'C:' + '\\Users\\Administrator\\x',
                trace_id=trace, stage='diagnosis', error_code='TIMEOUT',
                actionable='检查网络后重试', client_version='2026.11.19.4', build='20261119',
                platform='windows', url='https://space.bilibili.com/123456?x=1')
    r2 = record('ui_crash', '渲染崩溃（正文不含）', job_id='job_9')
    rows = _rows()
    pending = [r for r in rows if r.get('status') != SENT_MARK]
    raw = open(str(_queue_path()), encoding='utf-8').read()
    checks = {
        'uuid_format': bool(r1) and len(r1) == 36 and r1.count('-') == 4,
        'two_recorded': r1 is not None and r2 is not None and r1 != r2,
        'bound_to_max': len(pending) <= MAX_QUEUE,
        'scrubbed_key': 'sk-abcdef1234567890' not in raw and '已脱敏' in raw,
        'scrubbed_path': ('C:' + '\\Users\\Administrator') not in raw,
        'trace_kept': trace in raw and 'job_9' in raw,
        'url_masked': 'space.bilibili.com/<路径已隐藏>' in raw and '123456' not in raw,
        'client_fields': '"client_version":"2026.11.19.4"' in raw and '"platform":"windows"' in raw,
    }
    exp = export()
    checks['export_written'] = Path(exp).is_file() and Path(exp).stat().st_size > 2
    ok = all(checks.values())
    print(json.dumps({'selftest': 'PASS' if ok else 'FAIL', 'checks': checks,
                      'queue_pending': len(pending), 'export': exp},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == '__main__':
    import sys
    sys.exit(_selftest() if len(sys.argv) > 1 and sys.argv[1] == 'selftest' else 0)
