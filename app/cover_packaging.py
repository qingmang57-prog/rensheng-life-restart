"""封面模板包装：驱动 web/covers/ 下已做好的 10 套抖音封面模板（1080×1440，3:4）。

照 eva_packaging.py 的「约定目录 + 元数据 + 自动登记」模式：
  1. 模板库放在 app 能扫描的 web/covers/（templates.js + previews/），不依赖「设计/」目录、不写死外部盘符；
     预览图同时供浏览器直接访问（同源静态文件，零新增路由）。
  2. 元数据不硬编码：用 node 跑 build-one.mjs --dump 从模板库实时导出（id/名称/适用类型/人物默认），
     按 templates.js 的 mtime 缓存，模板库更新后自动重新登记。
  3. 渲染复用 tools/smart-packaging/engine/render.mjs --preview 单帧 PNG 管道（playwright 无头截图），
     产物真实 1080×1440，PNG 头部逐张校验，尺寸不符即报错、不静默放行。

授权（一次扣分、之后复用）：
  本地版没有积分账本，用 records 里 kind='cover_template_license' 过渡，字段对齐未来的
  template_entitlement（template_id / granted_at / source_ledger_id）。解锁只写本地记录，
  绝不假装已扣费成功；云端账本接入点见 unlock() 中的 LEDGER_HOOK。
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import branding
from db import now

ROOT = Path(__file__).resolve().parent
LIB = ROOT / 'web' / 'covers'
TEMPLATES = LIB / 'templates.js'
BUILD = LIB / 'build-one.mjs'
ENGINE = ROOT.parent / 'tools' / 'smart-packaging' / 'engine' / 'render.mjs'
NODE_MODULES = ROOT.parent / 'tools' / 'smart-packaging' / 'node_modules'
WIDTH, HEIGHT = 1080, 1440
# 试用反馈 4a+c：主标题先降字号、再自动换行；到下限仍放不下就提示改写，绝不静默截断。
TITLE_MAX = 12

# 本地占位策略：前 3 套基础版式免费，其余解锁后复用（账号级，不按项目重复收）。
# 云端接计价后此表改为服务端下发；当前仅本地演示两条分支。
FREE_TEMPLATES = {'01-bigtype', '02-whitecard', '03-journal'}

# 计价动作挂点：接积分账本时以这个动作名计价（设计/schema-credits.sql 的动作口径）。
PRICING_ACTION = 'cover_template_unlock'
# 账本挂点：设为可调用对象 ledger(template_id) -> {'ledger_id': ...} 后，unlock 会先真实扣费，
# 扣费失败不放行、不写本地 license。当前为 None = 本地过渡，不扣费。
LEDGER_HOOK = None

_LOCK = threading.Lock()
_META_CACHE = {'stamp': None, 'data': None}

# 内容类型 → 信号权重（复用 smart_packaging.SIGNALS 的 11 类特征打分）
TYPE_WEIGHTS = {
    '情感': {'emotional': 3, 'calm': 2, 'reflective': 1},
    '认知/思维': {'analytical': 3, 'opinion': 2, 'technical': 1},
    '如何赚钱': {'money': 3, 'business': 1},
    '营销方法': {'business': 2, 'practical': 2},
    '知识科普': {'practical': 3, 'technical': 2, 'analytical': 1},
    '自我成长': {'growth': 3, 'reflective': 1},
    '生活方式': {'playful': 2, 'calm': 2, 'reflective': 1},
    '通用观点': {'opinion': 3, 'energetic': 1},
}
# 知识库 category（project.tags 来源）→ 内容类型直通
TAG_TYPE = {'驾驭金钱': '如何赚钱', '事业': '营销方法', '三观': '通用观点', '账号观': '认知/思维'}
# 找选题页「这条讲什么类型」六类 → 封面 typeMap 类型（本人明确选择，权重最高）
BENCH_TYPE_MAP = {'情感': '情感', '认知思维': '认知/思维', '怎么赚钱': '如何赚钱', '营销方法': '营销方法',
                  '商家带货': '商家带货', '商家门店': '商家营销/门店'}
MONEY_RE = r'赚钱|副业|收入|变现|搞钱|月薪|工资|存款|理财|攒钱'


def _node():
    env = os.getenv('IP_NODE', '')
    for cand in (env, ROOT.parent / 'tools/node/bin/node.exe'):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def _chrome():
    for cand in (os.getenv('IP_PACKAGING_CHROME', ''), os.getenv('CHROME_PATH', ''),
                 r'C:/Program Files/Google/Chrome/Application/chrome.exe',
                 r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'):
        if cand and Path(cand).is_file():
            return str(cand)
    return ''


def _run(cmd, env=None, cwd=None, timeout=600):
    result = subprocess.run([str(x) for x in cmd], cwd=cwd, env=env, capture_output=True,
                            encoding='utf-8', errors='replace', timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result


def _stamp():
    try:
        paths = [TEMPLATES, LIB/'shared-xhs'/'templates.cjs', LIB/'shared-xhs'/'reference.css']
        return tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in paths)
    except OSError:
        return None


def metadata():
    """模板库元数据（自动登记）：node 导出，按文件戳缓存。"""
    stamp = _stamp()
    if stamp and _META_CACHE['stamp'] == stamp and _META_CACHE['data']:
        return _META_CACHE['data']
    node = _node()
    if not (node and TEMPLATES.is_file() and BUILD.is_file()):
        return {'templates': [], 'typeMap': {}, 'canvasSize': {'width': WIDTH, 'height': HEIGHT}}
    result = _run([node, BUILD, '--dump'])
    if result.returncode != 0:
        raise ValueError('封面模板库读取失败：' + (result.stderr or '')[:300])
    data = json.loads(result.stdout)
    with _LOCK:
        _META_CACHE['stamp'] = stamp
        _META_CACHE['data'] = data
    return data


def available():
    return bool(metadata()['templates']) and bool(_node()) and bool(_chrome()) and ENGINE.is_file()


def missing():
    gaps = []
    if not metadata()['templates']:
        gaps.append('封面模板库')
    if not _node():
        gaps.append('Node')
    if not _chrome():
        gaps.append('本地浏览器')
    if not ENGINE.is_file():
        gaps.append('渲染引擎')
    return gaps


def _by_id(template_id):
    return next((t for t in metadata()['templates'] if t['id'] == template_id), None)


def is_paid(template_id):
    return template_id not in FREE_TEMPLATES


def licenses(store):
    return [r for r in store.list('cover_template_license', 1000) if r.get('template_id')]


def unlocked(store, template_id):
    """已解锁 = 本地 license 记录存在（免费模板恒为已解锁）。"""
    if not is_paid(template_id):
        return True
    rec = store.get('cover_template_license', 'license:' + template_id)
    return bool(rec and rec.get('template_id') == template_id)


def unlock(store, template_id):
    """解锁一个模板：账号级一次解锁、之后所有作品复用。

    本地过渡：只写 records（kind='cover_template_license'，字段对齐 template_entitlement），
    不接账本、不扣分、也不假装已扣费成功。接账本时设置 LEDGER_HOOK，
    由它在写 license 前完成真实扣费并返回流水号（source_ledger_id）。
    """
    tpl = _by_id(template_id)
    if not tpl:
        raise ValueError('没有这个封面模板。')
    if not is_paid(template_id):
        return {'template_id': template_id, 'unlocked': True, 'already_free': True,
                'message': '这套是免费模板，直接生成即可。'}
    existing = store.get('cover_template_license', 'license:' + template_id)
    if existing and existing.get('template_id') == template_id:
        return {'template_id': template_id, 'unlocked': True, 'duplicate': True,
                'license': existing, 'message': '这套模板已解锁过，直接生成即可。'}
    ledger_id = None
    charge = None
    if LEDGER_HOOK is not None:
        charge = LEDGER_HOOK(template_id)  # 真实扣费挂点（cover_template_unlock 计价动作）
        if not charge or not charge.get('ok'):
            # 余额不足/计费异常都只回人话，不抛异常、不写本地 license。
            return {'template_id': template_id, 'unlocked': False, 'charged': False,
                    'code': str((charge or {}).get('code') or 'billing_failed'),
                    'message': str((charge or {}).get('message') or '扣费未完成，未解锁。'),
                    'billing': charge or {}}
        ledger_id = charge.get('ledger_id')
    try:
        record = store.put('cover_template_license', {
            'template_id': template_id, 'granted_at': now(),
            'source_ledger_id': ledger_id, 'pricing_action': PRICING_ACTION,
            'mode': 'ledger' if ledger_id else 'local_placeholder',
            'note': ''}, key='license:' + template_id)
    except Exception as exc:  # noqa: BLE001
        if charge and charge.get('ok'):
            # 扣分与授权已在账上，只是本地 license 没写进去：照实告知，别让客户以为白扣。
            raise RuntimeError('积分已扣、模板授权已记在账上，只是本地写入失败了。'
                               '再点一次「解锁」即可恢复，不会重复扣分。') from exc
        raise
    return {'template_id': template_id, 'unlocked': True, 'license': record,
            'charged': bool(ledger_id),
            'charged_credits': (charge or {}).get('charged_credits'),
            'balance_credits': (charge or {}).get('balance_credits'),
            'granted_credits': (charge or {}).get('granted_credits'),
            'purchased_credits': (charge or {}).get('purchased_credits'),
            'message': '已解锁，之后所有作品可直接复用这套模板。'}


def styles(store=None):
    """接进前端的模板列表：元数据 + 预览图地址 + 解锁状态。"""
    out = []
    for t in metadata()['templates']:
        item = {'id': t['id'], 'name': t['name'], 'types': t['types'], 'structure': t['structure'],
                'person_default': t['personDefault'], 'fields_default': t['fieldsDefault'],
                'requires_person': bool(t.get('requiresPerson')),
                'fields_extra': t.get('fieldsExtra') or [],
                'preview_url': '/covers/previews/' + t['id'] + '.png?v=' + str(t.get('version') or '1'),
                'shared': bool(t.get('shared')), 'version': t.get('version') or '',
                'paid': is_paid(t['id']),
                'unlocked': unlocked(store, t['id']) if store else not is_paid(t['id'])}
        out.append(item)
    return out


def _features(text):
    """复用 smart_packaging.SIGNALS 的打分公式，另补「赚钱」词类（封面 06 的类型依据）。"""
    from smart_packaging import SIGNALS
    feats = {key: min(1.0, len(re.findall(pat, text, re.I)) / 3) for key, (_, pat) in SIGNALS.items()}
    feats['money'] = min(1.0, len(re.findall(MONEY_RE, text, re.I)) / 3)
    return feats


def recommend(store, project):
    """按内容类型自动选模板：定稿正文信号打分 + 知识库分类直通，取 typeMap 首选。

    返回 {'template_id', 'content_type', 'reason', 'signals'}；无信号时退回通用观点。
    """
    meta = metadata()
    if not meta['templates']:
        return {'template_id': '', 'content_type': '', 'reason': '封面模板库未就绪', 'signals': []}
    text = ''
    version = store.get('script_version', project.get('confirmed_version_id') or '') if project.get('confirmed_version_id') else None
    if version and version.get('project_id') == project['id']:
        text = ''.join(str(p.get('text', '')) for p in version.get('parts', []))
    if not text.strip():
        source = store.get('source', project.get('source_id') or '') if project.get('source_id') else None
        text = (source or {}).get('text', '')
    text = text[:6000]
    feats = _features(text)
    scores = {ctype: sum(feats.get(k, 0) * w for k, w in weights.items())
              for ctype, weights in TYPE_WEIGHTS.items()}
    for tag in project.get('tags') or []:
        ctype = TAG_TYPE.get(str(tag))
        if ctype:
            scores[ctype] = scores.get(ctype, 0) + 2  # 本人知识库分类是明确依据，权重高于词频
    chosen = BENCH_TYPE_MAP.get(str((project.get('selected_topic') or {}).get('content_type') or ''))
    if chosen:
        scores[chosen] = scores.get(chosen, 0) + 3  # 找选题时本人选的类型，权重最高
    ranked_types = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    ctype, score = ranked_types[0] if ranked_types else ('通用观点', 0)
    if score <= 0:
        ctype = '通用观点'
    from smart_packaging import SIGNALS
    labels = {k: v[0] for k, v in SIGNALS.items()}
    labels['money'] = '赚钱话题'
    signals = [labels[k] for k, v in sorted(feats.items(), key=lambda x: x[1], reverse=True) if v > 0][:3]
    candidates = [tid for tid in (meta['typeMap'].get(ctype) or []) if _by_id(tid)]
    template_id = candidates[0] if candidates else next(
        (t['id'] for t in meta['templates'] if t['id'] == '01-bigtype'), meta['templates'][0]['id'])
    reason = ('选题时选的类型「' + chosen + '」；' if chosen else '') + ('、'.join(signals[:2]) or '表达线索较少，先用通用大字版式')
    return {'template_id': template_id, 'content_type': ctype, 'reason': reason, 'signals': signals}


def defaults(store, project):
    """封面字段默认值：标题取发布资料选定标题，副标题取开头第一句，账号取白牌配置源。"""
    pack = project.get('publication_pack') or {}
    title = str(pack.get('selected_title') or project.get('title') or '').strip()
    hook = ''
    version = store.get('script_version', project.get('confirmed_version_id') or '') if project.get('confirmed_version_id') else None
    if version and version.get('project_id') == project['id']:
        hook = str(next((p.get('text', '') for p in version.get('parts', []) if p.get('role') == 'hook'), '')).strip()
    return {'title': title[:40], 'subtitle': hook[:40], 'account': branding.account(store)}


def studio(store, project):
    """作品页封面区块的数据入口：模板列表 + 推荐 + 默认字段 + 我的形象 + 环境状态。"""
    import avatar_service
    return {'available': available(), 'missing': missing(),
            'default_available': default_available(), 'default_missing': default_missing(),
            'styles': styles(store), 'recommendation': recommend(store, project),
            'defaults': defaults(store, project),
            'avatar': avatar_service.status(store),
            'brand': branding.name(store),
            'note': '本机渲染，1080×1440 PNG。'}


# 2026-11-17 t130：默认可靠封面——ffmpeg 抽帧＋标题，只需 ffmpeg（不要 node/模板库/解锁）。
def ffmpeg_path():
    try:
        import media
        return str(media.binary('ffmpeg') or '')
    except Exception:
        return shutil.which('ffmpeg') or ''


def default_available():
    return bool(ffmpeg_path())


def default_missing():
    return [] if default_available() else ['ffmpeg']


def _font_file():
    for path in (r'C:\Windows\Fonts\msyhbd.ttc', r'C:\Windows\Fonts\msyh.ttc',
                 r'C:\Windows\Fonts\simhei.ttf', r'C:\Windows\Fonts\simsun.ttc'):
        if Path(path).is_file():
            return path
    return ''


def _draw_escape(text):
    value = str(text or '').replace('\\', '\\\\').replace("'", "\\'")
    return value.replace(':', '\\:').replace('%', '\\%').replace('\n', ' ')


def _wrap_title(title, width=12):
    title = re.sub(r'\s+', ' ', str(title or '')).strip() or '封面'
    lines = [title[i:i + width] for i in range(0, min(len(title), width * 2), width)]
    return lines or ['封面']


def default_cover(work, video_path, title, start=3.0):
    """抽帧（第 3 秒，失败回退首帧）＋标题，输出 1080×1440 PNG。返回路径。"""
    ff = ffmpeg_path()
    if not ff:
        raise ValueError('默认封面需要 ffmpeg；当前环境没有找到 ffmpeg。')
    video = Path(str(video_path or ''))
    if not video.is_file():
        raise ValueError('没有可抽帧的底片文件：' + str(video))
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    out = work / 'cover-default.png'
    font = _font_file()
    font_arg = ("fontfile='%s':" % font.replace('\\', '/').replace(':', '\\:')) if font else ''
    vf = 'scale=1080:1440:force_original_aspect_ratio=increase,crop=1080:1440'
    lines = _wrap_title(title)
    for idx, line in enumerate(lines[:2]):
        y = 'h-text_h-%d' % (240 if len(lines) > 1 and idx == 0 else 150)
        vf += (",drawtext=%stext='%s':fontcolor=white:fontsize=%d:line_spacing=14:"
               "box=1:boxcolor=black@0.45:boxborderw=28:x=(w-text_w)/2:y=%s"
               % (font_arg, _draw_escape(line), 78 if len(line) <= 10 else 66, y))
    last = None
    for start_at in (start, 0):
        cmd = [ff, '-y', '-ss', str(start_at), '-i', str(video), '-frames:v', '1',
               '-vf', vf, '-update', '1', str(out)]
        last = _run(cmd, timeout=120)
        if last.returncode == 0 and out.is_file() and out.stat().st_size > 0:
            break
    if not (out.is_file() and out.stat().st_size > 0):
        raise ValueError('ffmpeg 抽帧失败：' + ((last.stderr if last else '') or '')[-300:])
    width, height = _png_size(out)
    if (width, height) != (WIDTH, HEIGHT):
        raise ValueError('默认封面尺寸不符：%s×%s（应为 1080×1440）。' % (width, height))
    return out


def sanitize(data):
    """入参白名单化：模板存在、字段限长、人物层合法。"""
    template_id = str(data.get('template_id') or '')
    tpl = _by_id(template_id)
    if not tpl:
        raise ValueError('没有这个封面模板。')
    raw = data.get('fields') if isinstance(data.get('fields'), dict) else {}
    if tpl.get('shared'):
        for key,cap in (('title',12),('subtitle',18),('badge',20),('account',30)):
            text=str(raw.get(key) or '').strip()
            if key=='title':text=text.replace('【','').replace('】','')
            if len(text)>cap:raise ValueError('%s 最多%d字，请缩短后生成；不会截断文字。' % (key,cap))
    caps = (('title', 40), ('subtitle', 40), ('badge', 20), ('account', 30))
    fields = {k: str(raw.get(k) or '').strip()[:cap] for k, cap in caps}
    # 新增模板的附加字段（旧模板忽略）
    for key, cap in (('keyword', 12), ('episode', 16), ('claim', 30), ('tagline', 30)):
        val = str(raw.get(key) or '').strip()
        if val:
            fields[key] = val[:cap]
    if 'items' in raw:
        items = raw.get('items')
        if isinstance(items, list):
            fields['items'] = [str(x).strip()[:16] for x in items if str(x).strip()][:3]
        else:
            fields['items'] = [x.strip()[:16] for x in re.split(r'[|\n]', str(items or '')) if x.strip()][:3]
    plain_title = str(fields.get('title') or '').replace('【', '').replace('】', '')
    if len(plain_title) > TITLE_MAX:
        raise ValueError('主标题 %d 字，缩到最小字号并换行也放不下；请改写到 %d 字以内（可用【】标重点）。'
                         % (len(plain_title), TITLE_MAX))
    person = data.get('person') if isinstance(data.get('person'), dict) else {}
    return template_id, fields, {'on': person.get('on') is True,
                                 'x': person.get('x') if person.get('x') in ('left', 'center', 'right') else 'center',
                                 'outline': person.get('outline') if person.get('outline') in ('auto', 'none', 'yellow', 'white') else 'auto'}


def _png_size(path):
    """读 PNG IHDR 头部取真实尺寸，不依赖外部工具。"""
    with open(path, 'rb') as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise ValueError('渲染产物不是有效 PNG。')
    return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')


def requires_person(template_id):
    tpl = _by_id(template_id)
    return bool(tpl and tpl.get('requiresPerson'))


def render(work, template_id, fields, person, avatar_path=None):
    """渲染一张封面：模板 HTML → 无头截图 → PNG 尺寸校验。返回 PNG 路径。

    work 为本任务的 packages/<job_id> 目录；中间文件写 work/cover-src/，产物 work/cover.png。
    avatar_path 为「我的形象」透明 PNG；人像模板缺图时直接报错，不静默生成缺人封面。
    """
    node = _node()
    chrome = _chrome()
    if not available():
        raise ValueError('封面环境缺少：' + '、'.join(missing()))
    tpl = _by_id(template_id)
    merged = {**tpl['fieldsDefault'], **{k: v for k, v in fields.items() if v not in ('', None)}}
    person = dict(person) if person else dict(tpl['personDefault'])
    src = Path(work) / 'cover-src'
    src.mkdir(parents=True, exist_ok=True)
    avatar = Path(avatar_path) if avatar_path else None
    has_avatar = bool(avatar and avatar.is_file())
    if tpl.get('requiresPerson'):
        if not has_avatar:
            raise ValueError('这套模板需要「我的形象」：请先上传一张半身/正面照，抠图后会自动复用。')
    if has_avatar and (not tpl.get("shared") or tpl.get("requiresPerson") or person.get("on")):
        # 有「我的形象」时旧模板统一启用人物层并复用同一份透明 PNG：
        # 不再回退剪影、不再显示「人物位」占位标签（只影响有形象时的行为）。
        target = src / 'avatar.png'
        shutil.copy2(str(avatar), str(target))
        person['on'] = True
        person['image'] = 'avatar.png'
        if person.get('outline') in (None, '', 'auto'):
            person['outline'] = (tpl.get('personDefault') or {}).get('outline', 'none')
    params_file = Path(work) / 'cover-params.json'
    params_file.write_text(json.dumps({'template_id': template_id, 'fields': merged,
                                       'person': person, 'out_dir': str(src)}, ensure_ascii=False),
                           encoding='utf-8')
    built = _run([node, BUILD, '--params', params_file])
    if built.returncode != 0 or not (src / 'index.html').is_file() or not (src / 'settings.json').is_file():
        raise ValueError('封面页面构建失败：' + (built.stderr or built.stdout or '')[:300])
    out = Path(work) / 'cover.png'
    if out.exists():
        out.unlink()  # render.mjs 拒绝覆盖已有产物，先清掉本任务旧产物
    env = dict(os.environ)
    tmp_dir = Path(work) / 'cover-tmp'
    tmp_dir.mkdir(exist_ok=True)
    # playwright 在系统 Temp 建 artifacts 目录，个别环境无写权限；定向到本任务目录内更稳。
    env.update(CHROME_PATH=chrome, NODE_PATH=str(NODE_MODULES),
               TMP=str(tmp_dir), TEMP=str(tmp_dir), TMPDIR=str(tmp_dir),
               PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    shot = _run([node, ENGINE, '--root', src, '--out', out, '--preview', '--time', '0'],
                env=env, cwd=str(src), timeout=900)
    if shot.returncode != 0 or not out.is_file():
        raise ValueError('封面渲染失败：' + ((shot.stderr or shot.stdout or '')[:400] or '渲染引擎未返回产物。'))
    w, h = _png_size(out)
    if (w, h) != (WIDTH, HEIGHT):
        raise ValueError('封面尺寸 %dx%d 不符（要求 %dx%d），不放行。' % (w, h, WIDTH, HEIGHT))
    return out


def probe():
    """环境自检：给前端/诊断用。"""
    return {'ready': available(), 'missing': missing(), 'node': str(_node() or ''),
            'chrome': _chrome(), 'library': str(LIB),
            'templates': [t['id'] for t in metadata()['templates']],
            'free': sorted(FREE_TEMPLATES), 'pricing_action': PRICING_ACTION}
