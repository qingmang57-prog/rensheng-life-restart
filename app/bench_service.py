"""对标账号库 → 作品/热评 → 待拍清单：第一阶段后端服务层。

路由由 app.py 挂载（见 app.py 中 /api/bench/* 分支），本模块只做业务：
- 账号保存/复用（校验抖音 HTTPS 主页 URL；昵称等未取得字段为 null，不编造）
- 幂等采集 job（复用 new_job；浏览器能力不可用时真实失败并说明）
- 作品/评论分页列表（来自 bench_work / bench_comment）
- 选题生成：只能基于指定作品的标题、指标、已采评论，经 providers.llm_json；
  每条 topic 保存 bench_work_ids / bench_comment_ids 与「模型推断 / 待验证」标记
- start-project：用现有 project 结构立项，关联记录在 topic.data.project_id，幂等
"""
import hashlib
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import creation_service
import bench_collect
import contracts
import prompt_store
import providers
import style_profile

STORE = None
_new_job = None
ROLES = (('hook', '开头'), ('content', '主体'), ('anchor', '让人记住的那一句'), ('quote', '值得被转发的一句话'), ('ending', '结尾'))

MAX_TOPICS = 8
MAX_TOPICS_HARD = 20

def init(store, new_job, roles=None):
    global STORE, _new_job, ROLES
    STORE = store
    _new_job = new_job
    if roles:
        ROLES = tuple(roles)


def _require(kind, key):
    item = STORE.get(kind, key)
    if item is None:
        raise KeyError('没有找到该记录。')
    return item


def _page(items, query):
    page = max(1, int(query.get('page', ['1'])[0]))
    size = min(100, max(1, int(query.get('page_size', ['20'])[0])))
    return {'items': items[(page - 1) * size: page * size], 'total': len(items),
            'page': page, 'page_size': size}


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

_DOUYIN_HOME = re.compile(r'^https?://(?:www\.|v\.)?(?:ies)?douyin\.com/(?:share/)?user/([^/?#]+)/?$')
_SHARE_URL = re.compile(r'https?://[^\s<>"\'“”‘’()（）【】\[\]]+', re.I)
_SHARE_TRAIL = re.compile(r'[。，,；;！!、：:）)】」』”’"\'\]>]+$')
_DOUYIN_ITEM = re.compile(r'/(?:share/)?(video|note|slides)/(\d{8,30})')
_DOUYIN_MODAL = re.compile(r'[?&](?:modal_id|aweme_id|vid)=(\d{8,30})')
_DOUYIN_SHORT = re.compile(r'^https?://(?:v\.douyin\.com|v\.iesdouyin\.com)/', re.I)


def _short_to_final(url, timeout=10):
    """跟一次跳转拿落地地址；失败返回原地址（不把网络故障说成链接格式不对）。"""
    request = urllib.request.Request(url, headers={
        'User-Agent': ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 '
                       '(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1'),
        'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'})
    try:
        with urllib.request.build_opener().open(request, timeout=timeout) as response:
            return response.geturl() or url
    except urllib.error.HTTPError as exc:
        return exc.geturl() or url
    except Exception:
        return url


def parse_home_url(url):
    """校验抖音主页链接，返回 (home_url, external_id)。

    t196（2026-11-24）：客户是在手机上「分享 → 复制链接」粘过来的，拿到的常是
    整段分享文字或 v.douyin.com 短链，不是网页地址栏里那串 /user/<ID>。
    这里先从文字里取链接、短链再跟跳转拿落地地址，最后才判类型；
    落到作品页就明说「这是作品不是主页」并指出该去哪，不糊成一句格式不对。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError('请提供抖音主页 HTTPS 链接。')
    raw = url.strip()
    picked = _SHARE_URL.search(raw)
    if picked:
        raw = _SHARE_TRAIL.sub('', picked.group(0)).rstrip()
    final = raw
    if _DOUYIN_SHORT.match(raw):
        final = _short_to_final(raw)
    match = _DOUYIN_HOME.match(final)
    if match:
        return final.replace('http://', 'https://'), match.group(1)
    item = _DOUYIN_ITEM.search(final) or _DOUYIN_MODAL.search(final)
    if item:
        raise ValueError('这是抖音作品链接（%s），不是账号主页；单条作品请贴到「开始做内容 → 有现成链接」洗稿，'
                         '或在「发布与复盘」登记本人作品。' % final[:120])
    if final != raw:
        raise ValueError('这条短链打不开账号主页（解析到 %s）；请重新复制账号主页链接，或在网页版打开主页后复制地址。'
                         % final[:120])
    raise ValueError('仅支持 https://www.douyin.com/user/<ID> 形式的抖音主页链接（可以直接粘贴 App 里的分享文字或短链）。')


def save_account(payload):
    """保存/复用 bench_account；不自动采集。昵称等未取得字段保持 null。"""
    home_url, external_id = parse_home_url((payload or {}).get('url'))
    existing = next((x for x in STORE.list(contracts.KIND_BENCH_ACCOUNT, 5000)
                     if x.get('platform') == 'douyin' and x.get('external_id') == external_id), None)
    if existing is not None:
        return {**existing, 'reused': True}
    # 建档时平台昵称尚未采集：display_name 先用 URL 中的真实标识（sec_uid），
    # 采集成功后会以平台展示名更新；粉丝等指标一律 null。
    record = contracts.create_bench_account(
        'douyin', external_id, home_url=home_url, external_id=external_id,
        raw={'source': 'manual_url', 'profile_pending': True})
    return {**STORE.put(contracts.KIND_BENCH_ACCOUNT, record), 'reused': False}


def list_accounts():
    """账号列表：每条附最近采集 job 的 collect_status / collect_error；
    生命周期 status 字段原样保留（contracts 命名）。"""
    jobs = STORE.list('job', 1000)
    latest = {}
    for job in jobs:
        if job.get('kind') != 'bench_collect' or not job.get('project_id'):
            continue
        current = latest.get(job['project_id'])
        if current is None or job.get('updated_at', '') > current.get('updated_at', ''):
            latest[job['project_id']] = job
    items = []
    for account in STORE.list(contracts.KIND_BENCH_ACCOUNT, 5000):
        job = latest.get(account['id'])
        collect_status = job['status'] if job else 'none'  # none=尚未采集
        collect_error = job.get('error') if job and job['status'] == 'failed' else None
        # 生命周期 status：contracts.py 的 bench_account schema 未定义该字段，
        # 账号实体上也没有；列表层给出缺省 'active'，若实体日后带 status 则原样保留。
        items.append({**account, 'status': account.get('status') or 'active',
                      'collect_status': collect_status,
                      'collect_error': collect_error})
    return items


# ---------------------------------------------------------------------------
# 采集 job（幂等；失败真实报告）
# ---------------------------------------------------------------------------

def collect_account(account_id, payload):
    account = _require(contracts.KIND_BENCH_ACCOUNT, account_id)
    raw = (payload or {}).get('raw')
    if raw is not None and not isinstance(raw, dict):
        raise ValueError('raw 采集 JSON 必须是对象。')

    def work(job_id):
        if raw is not None:
            STORE.patch('job', job_id, {'stage': '校验并导入真实采集 JSON'})
            result = bench_collect.import_raw(STORE, raw, account_id=account['id'])
            return {'stage': '采集 JSON 导入完成',
                    'works_created': result['works_created'],
                    'comments_created': result['comments_created'],
                    'account_created': result['account_created']}
        STORE.patch('job', job_id, {'stage': '浏览器采集抖音主页作品与公开评论'})
        collector = bench_collect.DouyinBenchCollector()
        try:
            collected = collector.collect_account(account['home_url'])
        except bench_collect.BrowserUnavailable as exc:
            raise ValueError(str(exc)) from None
        except bench_collect.CollectionFailed as exc:
            if exc.needs_action:
                return {'requires_action': True,
                        'stage': '平台要求登录或验证，未绕过；请本人处理后重试',
                        'message': str(exc),
                        'classification': exc.classification}
            raise ValueError(str(exc)) from None
        STORE.patch('job', job_id, {'stage': '采集完成，正在入库（增量去重）'})
        result = bench_collect.import_raw(STORE, collected, account_id=account['id'])
        return {'stage': f"采集完成：新增作品 {result['works_created']} 条、评论 {result['comments_created']} 条",
                'works_created': result['works_created'],
                'comments_created': result['comments_created']}

    inputs = {'account_id': account['id'], 'home_url': account['home_url'],
              'mode': 'raw_import' if raw is not None else 'browser'}
    return _new_job(account['id'], 'bench_collect', inputs, work)


# ---------------------------------------------------------------------------
# 作品 / 评论列表
# ---------------------------------------------------------------------------

def _like_key(work):
    """点赞排序键：未取得(null)排最后；点赞降序；同分保持原顺序（稳定排序）。"""
    like = work.get('like_count')
    return (like is None, -(like if isinstance(like, (int, float)) else 0))


def _share_rate(work):
    """转发率 = share_count ÷ like_count。
    share 或 like 未取得(null) → None（显示"—"，不补 0、不参与排序）；
    like 为 0 → 'zero'（算不出，不参与排序）。"""
    like = work.get('like_count')
    share = work.get('share_count')
    if like is None or share is None:
        return None
    if like == 0:
        return 'zero'
    return share / like


def list_works(query):
    account_id = query.get('account_id', [''])[0]
    works = STORE.list(contracts.KIND_BENCH_WORK, 10000)
    if account_id:
        _require(contracts.KIND_BENCH_ACCOUNT, account_id)
        works = [x for x in works if x.get('bench_account_id') == account_id]
    works.sort(key=lambda x: (x.get('collected_at') or '', x.get('id') or ''), reverse=True)
    sort_mode = query.get('sort', [''])[0]
    if sort_mode == 'like':
        # 按点赞：点赞降序，未取得的排最后；同分回落到原顺序。
        works.sort(key=_like_key)
    elif sort_mode == 'share_rate':
        # 按转发率：能算的按转发率降序在前；转发率相同回落点赞降序；
        # 算不出的（缺 share/like 或 like=0）整体排在后面并按点赞降序，
        # 不把任何一条当 0 参与转发率排序。
        def _rate_key(x):
            rate = _share_rate(x)
            computable = rate is not None and rate != 'zero'
            return (not computable, -(rate if computable else 0), *_like_key(x))
        works.sort(key=_rate_key)
    return _page(works, query)


def list_comments(query):
    work_id = query.get('work_id', [''])[0]
    if not work_id:
        raise ValueError('缺少 work_id。')
    _require(contracts.KIND_BENCH_WORK, work_id)
    comments = [x for x in STORE.list(contracts.KIND_BENCH_COMMENT, 20000)
                if x.get('bench_work_id') == work_id]
    comments.sort(key=lambda x: (x.get('collected_at') or '', x.get('id') or ''), reverse=True)
    return _page(comments, query)


# ---------------------------------------------------------------------------
# 待拍清单
# ---------------------------------------------------------------------------

def topics_from_evidence(payload):
    """只能基于指定作品的标题、指标、已采评论生成 topic；来源 ID 逐条回验。"""
    work_ids = (payload or {}).get('work_ids')
    if not isinstance(work_ids, list) or not work_ids or not all(isinstance(k, str) and k for k in work_ids):
        raise ValueError('work_ids 必须是非空字符串数组。')
    try:
        max_topics = int((payload or {}).get('max_topics') or MAX_TOPICS)
    except (TypeError, ValueError):
        raise ValueError('max_topics 必须是整数。') from None
    if not 1 <= max_topics <= MAX_TOPICS_HARD:
        raise ValueError(f'max_topics 须在 1 到 {MAX_TOPICS_HARD} 之间。')
    works = [_require(contracts.KIND_BENCH_WORK, key) for key in dict.fromkeys(work_ids)]
    comments = [x for x in STORE.list(contracts.KIND_BENCH_COMMENT, 20000)
                if x.get('bench_work_id') in set(work_ids)]
    if not comments:
        raise ValueError('指定作品还没有已采评论；请先完成评论采集，不允许凭标题猜选题。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '生成模型尚未配置；不能凭空开题。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    evidence = {
        'works': [{'id': w['id'], 'title': w.get('title') or '', 'source_url': w.get('source_url') or '',
                   'like_count': w.get('like_count'), 'comment_count': w.get('comment_count'),
                   'share_count': w.get('share_count'), 'collect_count': w.get('collect_count'),
                   'play_count': w.get('play_count')} for w in works],
        'comments': [{'id': c['id'], 'bench_work_id': c['bench_work_id'],
                      'author_name': c.get('author_name') or '', 'content': c.get('content') or '',
                      'like_count': c.get('like_count')} for c in comments]}
    # t1 消息序：规范 per-user 稳定段（命题+主线→口吻→积累→档案）前置到功能模板之前，
    # 与选题/写稿/问答/复盘共用一个字节恒定的前缀→任一功能暖过的前缀这里直接命中；
    # 变量（作品指标/评论正文）严格留在最后 payload。
    stable_blocks = creation_service.stable_blocks_for(store=STORE)
    answer = providers.llm_json(
        prompt_store.get('bench.topic', basis_list='、'.join(creation_service.BASIS_LIST)),
        {**evidence, 'max_topics': max_topics},
        stable_blocks=stable_blocks)
    topics = answer.get('topics')
    if not isinstance(topics, list) or not 1 <= len(topics) <= max_topics:
        raise ValueError(f'模型选题格式无效：需要 1 至 {max_topics} 条；未入库任何选题。')
    saved = []
    for item in topics:
        saved.append(_save_topic(item, works, comments))
    return saved


def _save_topic(item, works, comments):
    if not isinstance(item, dict):
        raise ValueError('选题条目必须是对象。')
    for fake_key in ('published', 'platform_url', 'platform_video_id', 'verified'):
        if item.get(fake_key):
            raise ValueError(f'选题声称存在平台状态/链接（{fake_key}），拒绝入库；禁止编造来源。')
    title = str(item.get('title') or '').strip()
    if not title:
        raise ValueError('选题缺少标题，拒绝入库。')
    work_pool = {w['id'] for w in works}
    comment_pool = {c['id'] for c in comments}
    evidence_work_ids = item.get('evidence_work_ids')
    evidence_comment_ids = item.get('evidence_comment_ids') or []
    if (not isinstance(evidence_work_ids, list) or not evidence_work_ids
            or not all(isinstance(k, str) and k in work_pool for k in evidence_work_ids)):
        raise ValueError('每条选题必须给出真实作品来源ID（evidence_work_ids 且全部来自输入作品）。')
    if (not isinstance(evidence_comment_ids, list)
            or not all(isinstance(k, str) and k in comment_pool for k in evidence_comment_ids)):
        raise ValueError('evidence_comment_ids 必须全部来自已采评论。')
    angle = str(item.get('angle') or '')[:600]
    thesis = str(item.get('thesis') or '')[:1200]
    basis = creation_service._norm_basis(item.get('basis'))
    if not basis:
        raise ValueError('选题未按判法清单标注角度（basis），拒绝入库。')
    note = ('；'.join(x for x in (angle, thesis) if x))[:1200] or '模型依据已采证据生成，待本人核对。'
    record = contracts.create_topic(
        title[:160], bench_work_id=evidence_work_ids[0], note=note, status='candidate')
    record.update({
        'bench_work_ids': list(dict.fromkeys(evidence_work_ids)),
        'bench_comment_ids': list(dict.fromkeys(evidence_comment_ids)),
        'origin': 'bench',
        'angle': angle, 'thesis': thesis, 'basis': basis,
        'markers': ['模型推断', '待验证'],
        'validation': 'unverified',
        'created_by': 'model_assisted',
        'data': {}})
    return STORE.put(contracts.KIND_TOPIC, record)


# ---------------------------------------------------------------------------
# 按转写全文直接开题（文件/录屏作品没有评论时，以正文为证据）
# ---------------------------------------------------------------------------


TEXT_TOPIC_MIN_CHARS = 50
TEXT_TOPIC_COUNT = 3

_TRAILING_PUNCT = '，。！？；：、,.!?;: \t\r\n"\'“”‘’（）()《》【】…'


def _locate_quote(text, quote):
    """在正文里定位模型引用的原话；返回 (start,end) 字符下标，找不到返回 None。

    先按原样找；找不到时忽略空白和标点再找一次，再映射回原文下标
    （语音转写常把标点吞掉，前端要高亮仍必须落在原句上）。
    """
    if not quote:
        return None
    idx = text.find(quote)
    if idx >= 0:
        return idx, idx + len(quote)
    keep_chars = []
    keep_pos = []
    for i, ch in enumerate(text):
        if not ch.isspace() and ch not in _TRAILING_PUNCT:
            keep_chars.append(ch)
            keep_pos.append(i)
    needle = ''.join(ch for ch in quote if not ch.isspace() and ch not in _TRAILING_PUNCT)
    if not needle:
        return None
    idx = ''.join(keep_chars).find(needle)
    if idx < 0:
        return None
    return keep_pos[idx], keep_pos[idx + len(needle) - 1] + 1


def _save_text_topic(item, text, work):
    """落一条「按正文开题」的 topic；评论为空，证据是原文引用句。"""
    if not isinstance(item, dict):
        raise ValueError('选题条目必须是对象。')
    for fake_key in ('published', 'platform_url', 'platform_video_id', 'verified'):
        if item.get(fake_key):
            raise ValueError(f'选题声称存在平台状态/链接（{fake_key}），拒绝入库；禁止编造来源。')
    title = str(item.get('title') or '').strip()
    if not title:
        raise ValueError('选题缺少标题，拒绝入库。')
    basis = creation_service._norm_basis(item.get('basis'))
    if not basis:
        raise ValueError('选题未按判法清单标注角度（basis），拒绝入库。')
    quote = str(item.get('quote') or '').strip()
    span = _locate_quote(text, quote)
    if span is None:
        raise ValueError('模型引用的原句在正文里找不到，未入库；请重试一次。')
    angle = str(item.get('angle') or '')[:600]
    thesis = str(item.get('thesis') or '')[:1200]
    note = ('；'.join(x for x in (angle, thesis) if x))[:1200] or '模型依据转写正文生成，待本人核对。'
    record = contracts.create_topic(
        title[:160], bench_work_id=(work or {}).get('id'), note=note, status='candidate')
    record.update({
        'bench_work_ids': [work['id']] if work else [],
        'bench_comment_ids': [],
        'origin': 'capture_text',
        'angle': angle, 'thesis': thesis, 'basis': basis,
        'evidence_quote': quote, 'evidence_quote_start': span[0], 'evidence_quote_end': span[1],
        'markers': ['模型推断', '待验证'],
        'validation': 'unverified',
        'created_by': 'model_assisted',
        'data': {'evidence_kind': ('pasted_text' if (work.get('raw') or {}).get('capture_source') == 'pasted_text'
                                   else 'transcript')}})
    return STORE.put(contracts.KIND_TOPIC, record)


def _work_from_text(text):
    """直接粘贴的正文也要有来源记录：按正文指纹建一条 platform=file 的作品，重复粘贴复用。"""
    digest = hashlib.sha1(re.sub(r'\s+', '', text).encode('utf-8')).hexdigest()[:16]
    platform_id = 'text_' + digest
    for item in STORE.list(contracts.KIND_BENCH_WORK, 10000):
        if item.get('platform') == 'file' and item.get('platform_video_id') == platform_id:
            return item
    account = next((a for a in STORE.list(contracts.KIND_BENCH_ACCOUNT, 5000)
                    if a.get('platform') == 'file' and a.get('external_id') == 'local_files'), None)
    if account is None:
        account = STORE.put(contracts.KIND_BENCH_ACCOUNT,
                            contracts.create_bench_account('file', '本地文件', home_url='', external_id='local_files'))
    title = (re.split(r'[。！？\n]', text.strip())[0] or text.strip())[:40]
    record = contracts.create_bench_work(account['id'], 'file', platform_id, title, source_url='',
                                         raw={'transcript_text': text, 'capture_source': 'pasted_text'})
    return STORE.put(contracts.KIND_BENCH_WORK, record)


def topics_from_text(payload):
    """work_id（已转写作品）或 text（直接粘贴正文）→ 3 条选题，入同一个选题池。"""
    payload = payload or {}
    work = None
    if payload.get('work_id'):
        work = _require(contracts.KIND_BENCH_WORK, str(payload['work_id']))
        text = str((work.get('raw') or {}).get('transcript_text') or '')
        if not text.strip():
            raise ValueError('这条作品没有转写正文，不能按正文开题；请先上传文件转写，或直接给 text。')
    else:
        text = str(payload.get('text') or '')
    text = text.strip()
    if len(re.sub(r'\s+', '', text)) < TEXT_TOPIC_MIN_CHARS:
        raise ValueError('这段内容太短，拆不出稳定选题，建议换一段完整口播。')
    if work is None:
        work = _work_from_text(text)
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '生成模型尚未配置；不能凭空开题。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    # 2026-11-17 内容人格引擎最小版（直接写稿分支）：本人三观×口吻×主线随 payload 下发，
    # 让按正文拆出的选题也「更像我」、服务主线；本地口径，不改云端模板。
    # t30 conviction 钩子：三观原话作素材（带源 id）＋本地钩子规则一并下发；无源不编。
    axis = creation_service.axis_context(STORE)
    principles = creation_service.default_principles_snapshot()
    hook_sources = creation_service.conviction_sources([], principles)
    hooks = creation_service.conviction_hooks([], principles)
    # t3 去重：口吻已在规范稳定段，persona 不再重复内嵌。
    persona = creation_service.persona_block([], principles, '', axis, hooks=hooks)
    # t1 契约序：规范 per-user 稳定段（命题+主线→口吻→append-only 积累→档案）跨功能一致，
    # 统一经 stable_blocks_for 取段；功能常量次之；persona/钩子（随本次三观/选用认知变）
    # 放稳定段末尾；变量正文留最后 payload。
    stable_blocks = creation_service.stable_blocks_for([
        creation_service.PERSONA_RULE_TOPICS, creation_service.PERSONA_RULE_HOOK,
        persona, creation_service.conviction_hook_block(hook_sources)], STORE)
    answer = providers.llm_json(
        prompt_store.get('bench.text_topic', basis_list='、'.join(creation_service.BASIS_LIST)),
                                {'text': text[:12000], 'work_id': (work or {}).get('id'),
                                 'max_topics': TEXT_TOPIC_COUNT},
                                stable_blocks=stable_blocks)
    topics = (answer or {}).get('topics')
    if not isinstance(topics, list) or not 1 <= len(topics) <= TEXT_TOPIC_COUNT:
        raise ValueError('模型选题格式无效：需要 1 至 %d 条；未入库任何选题。' % TEXT_TOPIC_COUNT)
    return [_save_text_topic(item, text, work) for item in topics]


def list_topics(query=None):
    """选题列表：统一 {items,total,page,page_size}；每条为 contracts.topic 形状。"""
    topics = STORE.list(contracts.KIND_TOPIC, 5000)
    topics.sort(key=lambda x: (x.get('created_at') or '', x.get('id') or ''), reverse=True)
    # 2026-11-17 可见化预留：复盘洞察来源的选题带「来自你的爆款复盘」标注（仅视图层，不落库）。
    for item in topics:
        if item.get('origin') == 'feedback' and not item.get('origin_label'):
            item['origin_label'] = '来自你的爆款复盘'
    if query is None:
        return {'items': topics, 'total': len(topics), 'page': 1,
                'page_size': max(1, len(topics))}
    return _page(topics, query)


# ---------------------------------------------------------------------------
# 贴一条视频链接 → 读入这条视频 → 拆成 5 个能自己讲的角度
#
# 与「账号采集」的分工：账号采集是长期对标，一条一条视频贴进来是临时起意
# （刷到了、觉得这题我也能讲）。两者共用 bench_work / bench_comment 表与
# import_raw 入库路径，所以贴过的视频同时进作品库，不会重复建两份。
# ---------------------------------------------------------------------------

# 分享文本里夹带的链接（抖音「复制链接」给的是整段话，不是裸 URL）
_URL_IN_TEXT = re.compile(r"https?://[^\s，。；、！？、\"'《》【】（）\u4e00-\u9fff]+")

# 五个角度槽位：固定骨架保证「五个角度」不是五种说法，而是五种切法。
ANGLE_SLOTS = (
    ('inverse', '反过来讲'),
    ('subject', '换个人讲'),
    ('time', '换个时间讲'),
    ('cause', '往前推一层'),
    ('action', '落到动作'),
)
ANGLES_DEFAULT = 5
ANGLES_HARD = 8

# 评论分层抽样：拆角度要的是信息量和分歧，不是点赞共鸣。
# 点赞高的评论大多是「我太同意了」，拆不出角度；提问/反驳/具体经历各有配额。
_COMMENT_STRATA = (
    ('question', re.compile(r'[?？]|吗|怎么|为什么|为啥|是不是|能不能|该不该|咋办|请问')),
    ('rebuttal', re.compile(r'才不是|说得不对|不对|不一定|未必|可是|但是|难道|凭什么|想多了|太绝对|别瞎')),
    ('story', re.compile(r'我也|我曾经|我家|我当时|我去年|我上个月|去年|上个月|亲身|本人|我朋友|我同事|我闺蜜|我老公|我妈')),
)


def _stratified_comments(comments, total=20, quota=5):
    """分层抽样：提问型/反驳型/具体经历型各取 quota 条（层内按点赞降序），
    余额用「高赞共鸣型」（剩余评论按点赞降序）补足 total 条。
    返回 [(source_type, comment)]；不足 total 时如实返回更少，不凑数。"""
    used = set()
    picked = []
    for key, pattern in _COMMENT_STRATA:
        hits = [c for c in comments if id(c) not in used and pattern.search(c.get('content') or '')]
        hits.sort(key=lambda x: -(x.get('like_count') or 0))
        for c in hits[:quota]:
            used.add(id(c))
            picked.append((key, c))
    rest = [c for c in comments if id(c) not in used]
    rest.sort(key=lambda x: -(x.get('like_count') or 0))
    for c in rest[:max(0, total - len(picked))]:
        used.add(id(c))
        picked.append(('resonance', c))
    return picked


def parse_video_url(text):
    """从分享文本或裸链接取出抖音视频地址；返回 (url, video_id, is_short)。

    抖音「复制链接」给的是整段分享话术，这里只抽其中的 URL；短链拿不到作品 ID，
    交给浏览器跟随跳转后再取（不在本地猜 ID）。
    """
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('请粘贴抖音视频链接，或抖音「分享 → 复制链接」得到的整段文字。')
    match = _URL_IN_TEXT.search(raw)
    url = match.group(0) if match else raw
    url = url.rstrip('）)】」.,，。')
    scheme, sep, rest = url.partition('://')
    if not sep:
        raise ValueError('没找到可用的链接；请在抖音里点「分享 → 复制链接」，把整段贴进来。')
    if scheme.lower() != 'https':
        raise ValueError('链接必须是 https；请重新复制一次抖音分享链接。')
    host = rest.split('/', 1)[0].split('?', 1)[0].lower()
    if not (host == 'douyin.com' or host.endswith('.douyin.com')):
        raise ValueError(f'只支持抖音链接（douyin.com），这条来自 {host or "未知域名"}。')
    video_id = bench_collect.extract_video_id(url)
    is_short = not video_id
    return url, video_id, is_short


def _find_work_by_video_id(video_id):
    for work in STORE.list(contracts.KIND_BENCH_WORK, 10000):
        if str(work.get('platform_video_id') or '') == str(video_id):
            return work
    return None


def collect_video_link(payload):
    """贴链接 → 幂等采集 job。

    已在库里的视频直接复用（秒回），不重复开浏览器；refresh=True 可强制重采。
    真正失败时如实返回，不返回模拟的标题或评论。
    """
    payload = payload or {}
    url, video_id, is_short = parse_video_url(payload.get('url'))
    refresh = bool(payload.get('refresh'))
    if video_id and not refresh:
        existing = _find_work_by_video_id(video_id)
        if existing is not None:
            return {'work': _work_view(existing), 'reused': True, 'job': None}

    def work(job_id):
        STORE.patch('job', job_id, {'stage': '打开视频页，读取文案、指标与热评'})
        collector = bench_collect.DouyinBenchCollector()
        try:
            raw = collector.collect_video(url)
        except bench_collect.BrowserUnavailable as exc:
            raise ValueError(str(exc)) from None
        except bench_collect.CollectionFailed as exc:
            if exc.needs_action:
                stage = ('这条作品现在读不到（可能已被删除或设为仅自己可见）'
                         if exc.classification == bench_collect.NOT_FOUND
                         else '平台要求登录或验证，未绕过；请本人处理后重试')
                return {'requires_action': True,
                        'stage': stage,
                        'message': str(exc),
                        'classification': exc.classification}
            raise ValueError(str(exc)) from None
        STORE.patch('job', job_id, {'stage': '入库：视频与评论去重写入'})
        bench_collect.import_raw(STORE, raw)
        target = _find_work_by_video_id(
            bench_collect.extract_video_id(raw.get('source_url') or url) or video_id)
        if target is None:
            raise ValueError('采集完成但没能定位到这条作品记录；请重新贴一次链接。')
        comments = [x for x in STORE.list(contracts.KIND_BENCH_COMMENT, 20000)
                    if x.get('bench_work_id') == target['id']]
        return {'stage': f"已读入：评论 {len(comments)} 条",
                'work_id': target['id'], 'work': _work_view(target),
                'comment_count': len(comments)}

    inputs = {'url': url, 'video_id': video_id or '', 'mode': 'single_video'}
    job = _new_job(video_id or url, 'bench_video', inputs, work)
    return {'job': job, 'reused': False, 'video_id': video_id or None, 'is_short': is_short}


def _work_view(work):
    """喂给前端的作品视图：只放这条视频本身的资料，评论条数另算。"""
    comments = [x for x in STORE.list(contracts.KIND_BENCH_COMMENT, 20000)
                if x.get('bench_work_id') == work['id']]
    account = STORE.get(contracts.KIND_BENCH_ACCOUNT, work.get('bench_account_id'))
    return {
        'id': work['id'],
        'title': work.get('title') or '',
        'source_url': work.get('source_url') or '',
        'platform_video_id': work.get('platform_video_id') or '',
        'author': (account or {}).get('display_name') or '',
        'like_count': work.get('like_count'),
        'comment_count': work.get('comment_count'),
        'share_count': work.get('share_count'),
        'collect_count': work.get('collect_count'),
        'play_count': work.get('play_count'),
        'published_at': work.get('published_at'),
        'collected_at': work.get('collected_at'),
        'captured_comments': len(comments),
        'angles': work.get('angles') or None,
        'angles_at': work.get('angles_at') or None,
    }


def angles_from_work(payload):
    """按一条已入库视频，拆 5 个不同角度的选题（不落 topic 池，先供人挑）。"""
    work_id = str((payload or {}).get('work_id') or '').strip()
    if not work_id:
        raise ValueError('缺少 work_id。')
    work = _require(contracts.KIND_BENCH_WORK, work_id)
    count = (payload or {}).get('count') or ANGLES_DEFAULT
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ValueError('count 必须是整数。') from None
    if not 3 <= count <= ANGLES_HARD:
        raise ValueError(f'count 须在 3 到 {ANGLES_HARD} 之间。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '生成模型尚未配置；不能凭空拆角度。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    comments = [x for x in STORE.list(contracts.KIND_BENCH_COMMENT, 20000)
                if x.get('bench_work_id') == work_id]
    strata = _stratified_comments(comments, total=20, quota=5)
    account = STORE.get(contracts.KIND_BENCH_ACCOUNT, work.get('bench_account_id')) or {}
    evidence = {
        'video': {
            'title': work.get('title') or '',
            'author': account.get('display_name') or '',
            'like_count': work.get('like_count'),
            'comment_count': work.get('comment_count'),
            'share_count': work.get('share_count'),
            'collect_count': work.get('collect_count'),
        },
        'comments': [{'author': c.get('author_name') or '',
                      'content': (c.get('content') or '')[:300],
                      'like_count': c.get('like_count'),
                      'source_type': source_type}
                     for source_type, c in strata],
        'want': count,
    }
    # t1 消息序：同一份 per-user 稳定段前置（与选题/写稿/问答共享缓存前缀）；变量仍在最后。
    stable_blocks = creation_service.stable_blocks_for(store=STORE)
    answer = providers.llm_json(
        prompt_store.get('bench.angle', basis_list='、'.join(creation_service.BASIS_LIST)),
        evidence, stable_blocks=stable_blocks)
    angles = _validate_angles(answer, count)
    STORE.patch(contracts.KIND_BENCH_WORK, work_id,
                {'angles': angles, 'angles_at': contracts.now_ms()})
    return {'work': _work_view({**work, 'angles': angles}), 'angles': angles,
            'comment_used': len(strata)}


def _validate_angles(answer, count):
    """校验模型输出：槽位齐全、字段齐全、没有编造痕迹；不合格整批拒绝。"""
    raw = (answer or {}).get('angles')
    if not isinstance(raw, list) or len(raw) < 3:
        raise ValueError('模型没有给出可用的角度（少于 3 条）；未保存任何结果。')
    slots = {key for key, _label in ANGLE_SLOTS}
    cleaned = []
    seen = set()
    for item in raw[:count]:
        if not isinstance(item, dict):
            raise ValueError('角度条目必须是对象。')
        title = str(item.get('title') or '').strip()
        why = str(item.get('why') or '').strip()
        hook = str(item.get('hook') or '').strip()
        if not title or not why or not hook:
            raise ValueError('每条角度都要有 title / why / hook；未保存任何结果。')
        kind = str(item.get('type') or '').strip()
        kind = kind if kind in slots else ('inverse' if 'inverse' not in seen else 'subject')
        seen.add(kind)
        basis = creation_service._norm_basis(item.get('basis'))
        if not basis:
            raise ValueError('每条角度都要按判法清单标注 basis；未保存任何结果。')
        cleaned.append({'type': kind,
                        'label': (str(item.get('label') or '').strip() or
                                  dict(ANGLE_SLOTS).get(kind, '')),
                        'title': title[:60], 'why': why[:300], 'hook': hook[:80],
                        'basis': basis})
    labels = {x['label'] for x in cleaned}
    if len(labels) < len(cleaned):
        # 角度名重复说明模型在原地打转，交回前端前如实报错，不硬凑五条。
        raise ValueError('模型给出的角度有重复；请重试一次。')
    return cleaned


# ---------------------------------------------------------------------------
# 选题立项（幂等；关联写入 topic.data.project_id，不静默丢失）
# ---------------------------------------------------------------------------

def start_project(topic_id):
    """原子立项并保留创作简报，重复点击只复用已关联作品。"""
    return creation_service.start_project(topic_id, ROLES, STORE)
