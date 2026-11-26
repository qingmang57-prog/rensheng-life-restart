"""链接解析能力的实测记录。状态只认「真实抓取成功过」，不按环境变量写死。

每次 /api/wash/link 的真实抓取都会落一条记录；启动后再跑一次真实自测（抓一篇公开文章）。
设置页「链接解析」直接读这里：有成功记录 = 已找到；没有就把最近一次真实失败原因原样报出来。
只做读写 JSON，不依赖 providers / wash_service，避免循环导入。
"""
import json
import os
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE_NAME = 'resolver-status.json'
_LOCK = threading.Lock()


def _path():
    base = Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))
    return base / FILE_NAME


def _load():
    try:
        data = json.loads(_path().read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, path)
    except Exception:
        pass


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M')


def record(result, url, elapsed_ms, source='link'):
    """result 是 wash_service.fetch_link 的返回值。成功才更新 last_success，失败只更新 last_attempt。"""
    if not isinstance(result, dict):
        return
    ok = bool(result.get('ok'))
    text = result.get('text') or ''
    entry = {'ok': ok,
             'url': str(url or '')[:600],
             'title': str(result.get('title') or '')[:120],
             'chars': int(result.get('chars') or len(text) or 0),
             'engine': str(result.get('engine') or ''),
             'elapsed_ms': int(elapsed_ms or 0),
             'reason': '' if ok else str(result.get('reason') or '')[:400],
             'source': source,
             'checked_at': _now()}
    with _LOCK:
        data = _load()
        data['last_attempt'] = entry
        if ok:
            data['last_success'] = entry
        _save(data)


def _clean(entry):
    if not isinstance(entry, dict):
        return None
    return {'ok': bool(entry.get('ok')), 'url': entry.get('url') or '',
            'title': entry.get('title') or '', 'chars': int(entry.get('chars') or 0),
            'elapsed_ms': int(entry.get('elapsed_ms') or 0),
            'engine': entry.get('engine') or '', 'reason': entry.get('reason') or '',
            'source': entry.get('source') or '', 'checked_at': entry.get('checked_at') or ''}


def snapshot():
    """设置页要的三件事：能不能用、依据哪次真实抓取、抓不到时到底为什么。"""
    data = _load()
    success = _clean(data.get('last_success'))
    attempt = _clean(data.get('last_attempt'))
    if success:
        title = success['title'] or success['url'] or '公开文章'
        note = '上次实测成功：%s · %s · %s 字 · %.1f 秒' % (
            success['checked_at'], title[:40], success['chars'], success['elapsed_ms'] / 1000.0)
    elif attempt:
        note = '实测没抓成功（%s）：%s' % (attempt['checked_at'], attempt['reason'] or '原因未记录')
    else:
        note = '还没做过实测抓取，稍后自动重试；也可以直接粘贴一个链接试一次。'
    return {'ok': bool(success), 'note': note, 'success': success, 'attempt': attempt}
