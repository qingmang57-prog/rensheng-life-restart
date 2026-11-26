"""发布数据与评论回流 → 复盘：第一阶段后端服务层。

路由由 app.py 挂载到 /api/feedback/*，本模块只做业务：
- 总览/作品列表：platform_work 经 published 的 matrix_publish_task 关联项目；
  项目没有已发布作品时 platform_works 为真实空列表。
- 幂等采集 job（复用 new_job）：一个项目一个 collect job，逐作品采集、单作品失败隔离；
  未接入平台如实跳过；浏览器环境不可用/平台风控时真实失败或 needs_action，
  绝不假成功、不自动绕过验证。
- 指标快照只新增不覆盖；评论按平台评论 ID 去重分页；评论分类写回 category 字段。
- 评论分类与复盘洞察只产出「待验证经验」：必须调用 providers.llm_json，
  每条结论引用真实评论 ID / 快照 ID 证据，缺 LLM / 空证据明确失败；
  保存 markers=["模型推断","待验证"]，insight 状态默认 candidate（本人确认后才 confirmed），
  禁止声称因果已验证。
"""
import os
import time
import platform_adapters
import bench_service
import contracts
import creation_service
import feedback_collect
import rate_limit
import prompt_store
import providers

STORE = None
_new_job = None

# 评论分类固定枚举（前端按此显示）；未分类为 null。
CATEGORIES = ('question', 'painpoint', 'objection', 'praise', 'sequel', 'other')

# 复盘结论禁止出现「因果已验证」类措辞：模型只能给待验证经验，不能写成事实。
BANNED_VERIFIED_CLAIMS = ('因果已验证', '已验证因果', '因果关系已证实', '已被证实',
                          '已证实', '确定性因果', '被数据证明', '已被数据证明',
                          '已被数据验证', '必然有效')


def init(store, new_job):
    global STORE, _new_job
    STORE = store
    _new_job = new_job


# 2026-11-16（点检 S2）：按 platform_work_id 取快照/评论以前走 STORE.list 全表扫（5 万条），
# 单次调用就要几百毫秒，逐作品调用变成 N+1。这里用 SQLite JSON 表达式索引按作品过滤；
# 索引只建一次（IF NOT EXISTS），旧库首次调用时自动补上。
_WORK_INDEX_READY = False


def _ensure_work_index():
    global _WORK_INDEX_READY
    if _WORK_INDEX_READY:
        return
    try:
        with STORE.connect() as con:
            con.execute("CREATE INDEX IF NOT EXISTS by_kind_work_id "
                        "ON records(kind, json_extract(data,'$.platform_work_id'))")
        _WORK_INDEX_READY = True
    except Exception as exc:
        # 数据库被占用时下次再试；其它错误（旧 SQLite 不支持 JSON 索引等）不再反复重试。
        if 'locked' not in str(exc).lower() and 'busy' not in str(exc).lower():
            _WORK_INDEX_READY = True


def _list_by_work_ids(kind, work_ids, limit=50000):
    ids = list(dict.fromkeys([str(x) for x in work_ids if x]))
    if not ids:
        return []
    _ensure_work_index()
    marks = ','.join('?' * len(ids))
    # 不带 ORDER BY：带上 updated_at 排序会让 SQLite 改走 by_kind 索引、退化成全量扫；
    # 调用方各自按 collected_at/updated_at 排序，这里只要按作品过滤即可。
    sql = ("SELECT * FROM records WHERE kind=? AND "
           "json_extract(data,'$.platform_work_id') IN (%s) LIMIT ?" % marks)
    with STORE.connect() as con:
        return [STORE.unpack(r) for r in con.execute(sql, [kind] + ids + [limit])]


def _require(kind, key):
    item = STORE.get(kind, key)
    if item is None:
        raise KeyError('没有找到该记录。')
    return item


def _require_project(project_id):
    if not isinstance(project_id, str) or not project_id:
        raise ValueError('project_id 必须是非空字符串。')
    return _require('project', project_id)


def _project_works(project_id):
    """项目下平台作品：矩阵代发 published 回登 + 本人手动登记（platform_work.project_id）。"""
    work_ids = []
    for task in STORE.list(contracts.KIND_MATRIX_PUBLISH_TASK, 10000):
        if task.get('project_id') != project_id:
            continue
        if task.get('status') != 'published' or not task.get('platform_work_id'):
            continue
        if task['platform_work_id'] not in work_ids:
            work_ids.append(task['platform_work_id'])
    # 本人自己在平台发布、再回工作台登记的作品：没有 matrix_publish_task，
    # 直接按 platform_work.project_id 关联（否则永远进不了复盘）。
    for work in STORE.list(contracts.KIND_PLATFORM_WORK, 20000):
        if work.get('project_id') == project_id and work['id'] not in work_ids:
            work_ids.append(work['id'])
    return [_require(contracts.KIND_PLATFORM_WORK, key) for key in work_ids]


def _page(items, query):
    page = max(1, int(query.get('page', ['1'])[0]))
    size = min(100, max(1, int(query.get('page_size', ['20'])[0])))
    return {'items': items[(page - 1) * size: page * size], 'total': len(items),
            'page': page, 'page_size': size}


def _work_view(work):
    view = dict(work)
    # 前端按 platform_url 对接；与 source_url 同源（平台侧可核验链接）。
    view['platform_url'] = work.get('source_url')
    return view


def _work_summary(work):
    """采集/总览用的平台作品摘要（固定字段）。"""
    return {'platform_work_id': work['id'], 'platform': work.get('platform'),
            'platform_video_id': work.get('platform_video_id'),
            'title': work.get('title') or '', 'platform_url': work.get('source_url'),
            'published_at': work.get('published_at')}


def _job_view(job):
    view = {'id': job['id'], 'status': job.get('status'), 'stage': job.get('stage'),
            'error': job.get('error')}
    return view


# ---------------------------------------------------------------------------
# 总览 / 作品列表
# ---------------------------------------------------------------------------

def overview(project_id):
    project = _require_project(project_id)
    works = _project_works(project_id)
    work_ids = {w['id'] for w in works}
    latest = {}
    latest_snapshot_at = None
    for snapshot in _list_by_work_ids(contracts.KIND_METRIC_SNAPSHOT, work_ids):
        if snapshot.get('platform_work_id') not in work_ids:
            continue
        stamp = snapshot.get('collected_at') or ''
        if latest_snapshot_at is None or stamp > latest_snapshot_at:
            latest_snapshot_at = stamp
        key = (snapshot['platform_work_id'], snapshot.get('metric_key') or '')
        if key not in latest or stamp > (latest[key].get('collected_at') or ''):
            latest[key] = snapshot
    latest_snapshots = sorted(latest.values(),
                              key=lambda s: ((s.get('platform_work_id') or ''),
                                             (s.get('metric_key') or '')))
    comments_count = len(_list_by_work_ids(contracts.KIND_PLATFORM_COMMENT, work_ids))
    insights_count = sum(1 for i in STORE.list(contracts.KIND_REVIEW_INSIGHT, 5000)
                         if i.get('project_id') == project_id)
    return {'project': project, 'platform_works': [_work_view(w) for w in works],
            'platform_work_count': len(works),
            'latest_snapshots': latest_snapshots,
            'latest_snapshot_at': latest_snapshot_at,
            'comments_count': comments_count, 'insights_count': insights_count}


def list_works(project_id):
    _require_project(project_id)
    works = _project_works(project_id)
    return {'items': [_work_view(w) for w in works], 'total': len(works)}


# ---------------------------------------------------------------------------
# 采集 job（幂等；单 job 逐作品隔离；真实失败）
# ---------------------------------------------------------------------------

def collect_work_now(work_id):
    """定时回采用：按 platform_work_id 采一次（只采集数据，绝不触发发布）。"""
    work = STORE.get(contracts.KIND_PLATFORM_WORK, work_id)
    if work is None:
        raise KeyError('没有找到该作品。')
    return _collect_one(work, None, 'schedule')


def _collect_one(work, raw, job_id):
    """采集单个作品：raw 模式导入真实采集 JSON；浏览器模式读页面自身已签名响应。

    浏览器模式先过频控闸门（P0-5）：默认 observe 只标记不拦；enforce 时才跳过。
    """
    if raw is not None:
        result = feedback_collect.import_raw(STORE, raw, work['id'])
        return {'snapshots_created': result['snapshots_created'],
                'comments_created': result['comments_created']}
    account_id = work.get('platform_account_id') or ''
    gate = rate_limit.guard(STORE, 'collect', account_id=account_id, work_id=work['id'])
    if gate['blocked']:
        return {'snapshots_created': 0, 'comments_created': 0, 'skipped': True,
                'skip_code': gate['code'], 'message': gate['reason'], 'rate_limit': gate}
    event = rate_limit.begin(STORE, 'collect', account_id, work['id'])
    # 2026-11-17 t73 采集韧性：瞬时失败自动重试（2 次退避），仍失败则降级返回，不中断整轮复盘。
    collected = None
    last_error = None
    for attempt in range(3):
        try:
            collector = feedback_collect.DouyinWorkCollector()
            collected = collector.collect(work.get('source_url') or '')
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if isinstance(exc, (feedback_collect.BrowserUnavailable, feedback_collect.CollectionFailed)):
                break
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    if collected is None:
        rate_limit.finish(STORE, event, 'failed', message=str(last_error or '')[:200])
        if isinstance(last_error, (feedback_collect.BrowserUnavailable, feedback_collect.CollectionFailed)):
            raise last_error
        return {'snapshots_created': 0, 'comments_created': 0, 'degraded': True,
                'message': '采集暂时失败（已重试 %d 次）：%s；本轮复盘继续，其他作品不受影响。'
                           % (3, str(last_error)[:160])}
    try:
        result = feedback_collect.import_raw(STORE, collected, work['id'])
    except Exception as exc:
        rate_limit.finish(STORE, event, 'failed', message=str(exc)[:200])
        raise
    needs_action = bool(collected.get('requires_action'))
    rate_limit.finish(STORE, event, 'needs_action' if needs_action else 'ok',
                      message=collected.get('error') or '')
    outcome = {'snapshots_created': result['snapshots_created'],
               'comments_created': result['comments_created'],
               'rate_limit': gate}
    if needs_action:
        outcome['requires_action'] = True
        outcome['message'] = str(collected.get('error') or '平台要求本人处理。')
    return outcome


def register_work(payload):
    """登记本人已在平台发布的作品（工作台不代发，只登记+真采集）。

    输入 project_id + 平台作品 URL；作品落 platform_work.project_id 进入复盘，
    并当场复用 DouyinWorkCollector 采集评论。平台要求登录/验证时如实返回
    needs_action + 中文说明，绝不假成功、不绕过验证。
    固定返回 {platform_work, project, reused, collect:{...}, needs_action, message}。
    """
    data = payload or {}
    project = _require_project(data.get('project_id'))
    raw_url = data.get('url')
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ValueError('请粘贴抖音作品链接。')
    source_url, video_id = feedback_collect.parse_video_url(raw_url)

    existing = None
    for work in STORE.list(contracts.KIND_PLATFORM_WORK, 20000):
        if (work.get('platform') == feedback_collect.PLATFORM
                and str(work.get('platform_video_id') or '') == video_id):
            existing = work
            break
    reused = existing is not None
    if existing is not None:
        owner = existing.get('project_id')
        if not owner and existing.get('publish_task_id'):
            task = STORE.get(contracts.KIND_MATRIX_PUBLISH_TASK,
                             existing['publish_task_id']) or {}
            owner = task.get('project_id')
        if owner and owner != project['id']:
            raise ValueError('这条作品已关联另一条内容，请核对链接后重试；本次未做任何改动。')
        if not existing.get('project_id'):
            existing = STORE.patch(contracts.KIND_PLATFORM_WORK, existing['id'],
                                   {'project_id': project['id']})
    else:
        existing = STORE.put(contracts.KIND_PLATFORM_WORK, contracts.create_platform_work(
            None, feedback_collect.PLATFORM, video_id, f'抖音作品 {video_id}',
            source_url=source_url,
            raw={'registered_url': raw_url.strip(), 'registered_at': contracts.now_ms()},
            project_id=project['id'], registration_mode='manual'))

    comments_created = 0
    snapshots_created = 0
    comments_skipped = 0
    platform_total = None
    needs_action = False
    collect_error = None
    gate = rate_limit.guard(STORE, 'collect',
                            account_id=existing.get('platform_account_id') or '',
                            work_id=existing['id'])
    if gate['blocked']:
        collect_error = gate['reason']
    else:
        event = rate_limit.begin(STORE, 'collect',
                                 existing.get('platform_account_id') or '', existing['id'])
        try:
            collected = feedback_collect.DouyinWorkCollector().collect(source_url, 60)
        except feedback_collect.BrowserUnavailable as exc:
            rate_limit.finish(STORE, event, 'failed', message=str(exc)[:200])
            collect_error = str(exc)
        except feedback_collect.CollectionFailed as exc:
            needs_action = bool(exc.needs_action)
            rate_limit.finish(STORE, event, 'failed', message=str(exc)[:200])
            collect_error = str(exc)
        except Exception as exc:
            rate_limit.finish(STORE, event, 'failed', message=str(exc)[:200])
            collect_error = str(exc)[:600]
        else:
            result = feedback_collect.import_raw(STORE, collected, existing['id'])
            comments_created = result['comments_created']
            snapshots_created = result['snapshots_created']
            comments_skipped = int(collected.get('comments_skipped') or 0)
            platform_total = collected.get('comments_total')
            needs_action = bool(collected.get('requires_action'))
            collect_error = str(collected.get('error') or '').strip() or None
            rate_limit.finish(STORE, event, 'needs_action' if needs_action else 'ok',
                              message=collect_error or '')
            detail = (collected.get('metrics_raw') or {}).get('aweme_detail') or {}
            desc = str(detail.get('desc') or '').strip()
            if desc and existing.get('title') == f'抖音作品 {video_id}':
                existing = STORE.patch(contracts.KIND_PLATFORM_WORK, existing['id'],
                                       {'title': desc[:160]})

    comments_total = len(_list_by_work_ids(contracts.KIND_PLATFORM_COMMENT, [existing['id']]))
    skipped_note = f'；另有 {comments_skipped} 条空正文（纯表情/图片）未入库' if comments_skipped else ''
    if needs_action:
        message = (f'作品已登记，但平台要求本人处理后才能继续采集：本次只取到 {comments_total} 条评论'
                   + (f'（平台显示共 {platform_total} 条）' if platform_total else '')
                   + skipped_note + f'。{collect_error or ""}')
    elif collect_error:
        message = f'作品已登记，但本次采集未成功：{collect_error}。可稍后重新采集。'
    else:
        message = (f'作品已登记并采集完成：本次新增 {comments_created} 条评论，'
                   f'当前共 {comments_total} 条'
                   + (f'（平台显示共 {platform_total} 条）' if platform_total else '')
                   + skipped_note + '。')
    return {'platform_work': _work_view(existing), 'project': {'id': project['id'],
            'title': project.get('title') or ''}, 'reused': reused,
            'collect': {'ok': collect_error is None, 'comments_created': comments_created,
                        'comments_total': comments_total, 'platform_total': platform_total,
                        'comments_skipped': comments_skipped, 'snapshots_created': snapshots_created,
                        'needs_action': needs_action, 'error': collect_error,
                        'rate_limit': gate},
            'needs_action': needs_action, 'message': message}


def collect(payload):
    """固定返回 {job:{id,status,stage,error}, works:[...], message}。

    一个项目一个 collect job（同 inputs 进行中任务复用，幂等）；job 内逐作品采集，
    单作品失败/未接入不影响其他作品；works 为涉及的平台作品摘要列表。
    """
    data = payload or {}
    project = _require_project(data.get('project_id'))
    raw = data.get('raw')
    if raw is not None and not isinstance(raw, dict):
        raise ValueError('raw 采集 JSON 必须是对象。')
    works = _project_works(project['id'])
    if not works:
        return {'job': None, 'works': [],
                'message': '该项目还没有已发布的平台作品；可先「登记作品」粘贴本人已在平台发布的抖音链接，'
                           '或完成矩阵发布后再采集。未发布作品没有可回流的平台数据。'}
    summaries = [_work_summary(w) for w in works]

    def work_fn(job_id):
        results = []
        attention = False
        total = len(works)
        for index, w in enumerate(works, 1):
            # 2026-11-26 同类排查：done/total 落字段，前端能出真实百分比（原先只有 stage 文本）。
            STORE.patch('job', job_id, {'done': index - 1, 'total': total})
            adapter = platform_adapters.ADAPTERS.get(w.get('platform') or '')
            if adapter is None or not adapter.implemented:
                results.append({'platform_work_id': w['id'], 'ok': False, 'skipped': True,
                                'error': f"平台 {w.get('platform')} 未接入/待验收：跳过采集，不影响其他作品。"})
                continue
            STORE.patch('job', job_id,
                        {'stage': f'采集本人作品指标与评论（{index}/{total}）：{w.get("title") or w["id"]}'})
            try:
                outcome = _collect_one(w, raw, job_id)
            except feedback_collect.BrowserUnavailable as exc:
                raise ValueError(str(exc)) from None  # 执行器缺失：整个 job 真实失败
            except feedback_collect.CollectionFailed as exc:
                if exc.needs_action:
                    attention = True
                    results.append({'platform_work_id': w['id'], 'ok': False,
                                    'requires_action': True, 'error': str(exc),
                                    'classification': exc.classification})
                    continue
                results.append({'platform_work_id': w['id'], 'ok': False, 'error': str(exc)})
                continue
            except Exception as exc:  # 单作品失败隔离：记录后继续下一个作品
                results.append({'platform_work_id': w['id'], 'ok': False,
                                'error': str(exc)[:600]})
                continue
            entry = {'platform_work_id': w['id'], 'ok': True,
                     'snapshots_created': outcome['snapshots_created'],
                     'comments_created': outcome['comments_created']}
            if outcome.get('skipped'):
                results.append({'platform_work_id': w['id'], 'ok': False, 'skipped': True,
                                'code': outcome.get('skip_code'),
                                'error': outcome.get('message'),
                                'rate_limit': outcome.get('rate_limit')})
                continue
            if outcome.get('rate_limit'):
                entry['rate_limit'] = outcome['rate_limit']
            if outcome.get('requires_action'):
                attention = True
                entry['requires_action'] = True
                entry['message'] = outcome.get('message')
            results.append(entry)
        STORE.patch('job', job_id, {'done': total, 'total': total})
        failed = sum(1 for r in results if not r.get('ok'))
        ok_count = sum(1 for r in results if r.get('ok'))
        message = {'stage': f'采集完成：成功 {ok_count} 个作品'
                            + (f'，失败/跳过 {failed} 个（详见 result.works）' if failed else ''),
                   'works': results, 'works_ok': ok_count, 'works_failed': failed}
        if attention:
            message['requires_action'] = True
            message['message'] = '部分作品平台要求登录或验证，未绕过；请本人处理后重试。'
        return message

    job = _new_job(project['id'], 'feedback_collect',
                   {'project_id': project['id'],
                    'mode': 'raw_import' if raw is not None else 'browser'}, work_fn)
    return {'job': _job_view(job), 'works': summaries,
            'message': f'采集任务已开始（涉及 {len(summaries)} 个平台作品），请轮询 job.status。'}


# ---------------------------------------------------------------------------
# 快照 / 评论列表
# ---------------------------------------------------------------------------

def list_snapshots(platform_work_id):
    _require(contracts.KIND_PLATFORM_WORK, platform_work_id)
    snapshots = _list_by_work_ids(contracts.KIND_METRIC_SNAPSHOT, [platform_work_id])
    snapshots.sort(key=lambda s: ((s.get('collected_at') or ''), (s.get('id') or '')))
    return {'items': snapshots, 'total': len(snapshots)}


def list_comments(query):
    work_id = query.get('platform_work_id', [''])[0]
    _require(contracts.KIND_PLATFORM_WORK, work_id)
    comments = _list_by_work_ids(contracts.KIND_PLATFORM_COMMENT, [work_id])
    comments.sort(key=lambda c: ((c.get('collected_at') or ''), (c.get('id') or '')))
    # 固定含 category 字段：未分类为 null。
    comments = [{**c, 'category': c.get('category')} for c in comments]
    return _page(comments, query)


# ---------------------------------------------------------------------------
# 评论分类（仅基于已采评论；待验证经验；category 写回评论对象）
# ---------------------------------------------------------------------------

def classify_comments(platform_work_id):
    """固定返回 {items:[更新后的评论对象], counts:{六类计数}, message}。"""
    work = _require(contracts.KIND_PLATFORM_WORK, platform_work_id)
    comments = _list_by_work_ids(contracts.KIND_PLATFORM_COMMENT, [work['id']])
    if not comments:
        raise ValueError('该作品还没有已采评论；请先执行评论采集，不允许凭空分类。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '分类模型尚未配置；不能凭空分类。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    # t2 取舍：评论分类是短调用（prompt 约 400-1300 token），稳定段约 1k token。加上去
    # 命中率会好看，但「多出来的未命中」比「命中省下的」更贵（估算：886 未命中原样 vs
    # 1000 命中×0.02＋500 未命中，实付反而升）。分类也不依赖人设/命题，故不加稳定段，
    # 只享全局铁律前缀；要抬 feedback 命中率主攻 insight/topic。
    answer = providers.llm_json(prompt_store.get('feedback.classify'), {
        'platform_work_id': work['id'],
        'comments': [{'id': c['id'], 'author_name': c.get('author_name') or '',
                      'content': c.get('content') or '', 'like_count': c.get('like_count')}
                     for c in comments]})
    items = answer.get('classifications')
    if not isinstance(items, list) or not 1 <= len(items) <= len(comments):
        raise ValueError(f'模型分类格式无效：需要 1 至 {len(comments)} 条；未入库任何反馈。')
    pool = {c['id']: c for c in comments}
    counts = {name: 0 for name in CATEGORIES}
    saved_feedback = []
    seen_ids = set()
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('分类条目必须是对象；未入库任何反馈。')
        comment_id = item.get('comment_id')
        if comment_id not in pool:
            raise ValueError(f'分类引用了不存在的评论ID：{comment_id!r}；拒绝入库。')
        if comment_id in seen_ids:
            raise ValueError(f'同一条评论被重复分类：{comment_id!r}；拒绝入库。')
        seen_ids.add(comment_id)
        category = item.get('category')
        if category not in CATEGORIES:
            raise ValueError(f'category 无效：{category!r}；只允许 {list(CATEGORIES)}，拒绝入库。')
        parsed.append((comment_id, category, str(item.get('note') or '')[:1200]))
    # 分类准确率要“每条可抽检”，所以要求模型覆盖全部评论（一条不漏、不重复）。
    if len(parsed) != len(comments):
        raise ValueError(f'模型分类未覆盖全部 {len(comments)} 条评论（收到 {len(parsed)} 条）；'
                         '未入库任何反馈。')
    for comment_id, category, note in parsed:
        record = contracts.create_comment_feedback(
            None, None, 'none', note=note,
            platform_comment_id=comment_id, platform_work_id=work['id'])
        record.update({'category': category, 'markers': ['模型推断', '待验证'],
                       'validation': 'unverified', 'created_by': 'model_assisted'})
        saved_feedback.append(STORE.put(contracts.KIND_COMMENT_FEEDBACK, record))
        # category 写回评论对象：GET comments 的 items 固定含 category。
        STORE.patch(contracts.KIND_PLATFORM_COMMENT, comment_id, {'category': category})
        counts[category] += 1
    updated = [STORE.get(contracts.KIND_PLATFORM_COMMENT, f['platform_comment_id'])
               for f in saved_feedback]
    return {'items': updated, 'counts': counts,
            'message': f'已分类 {len(updated)} 条评论（模型推断，待验证，可经本人确认后采用）。'}


# ---------------------------------------------------------------------------
# 复盘洞察（证据链；待验证标记；禁止伪造因果；status=candidate 待本人确认）
# ---------------------------------------------------------------------------

def _has_banned_claim(text):
    return any(phrase in text for phrase in BANNED_VERIFIED_CLAIMS)


def create_insight(project_id):
    """固定同步返回 {item: review_insight, message}；落库后 GET insights 立即可查。"""
    project = _require_project(project_id)
    works = _project_works(project['id'])
    work_ids = {w['id'] for w in works}
    work_url = {w['id']: w.get('source_url') for w in works}
    snapshots = _list_by_work_ids(contracts.KIND_METRIC_SNAPSHOT, work_ids)
    feedbacks = _list_by_work_ids(contracts.KIND_COMMENT_FEEDBACK, work_ids)
    if not snapshots and not feedbacks:
        raise ValueError('该项目还没有任何指标快照或已分类评论；请先采集并分类，不允许凭空复盘。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '复盘模型尚未配置；不能凭空生成洞察。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    evidence_pool = {s['id']: 'metric_snapshot' for s in snapshots}
    evidence_pool.update({f['id']: 'comment_feedback' for f in feedbacks})
    evidence_owner = {s['id']: s.get('platform_work_id') for s in snapshots}
    evidence_owner.update({f['id']: f.get('platform_work_id') for f in feedbacks})
    comments_by_id = {c['id']: c for c in _list_by_work_ids(contracts.KIND_PLATFORM_COMMENT, work_ids)}
    # 2026-11-17 激活闭环 G2/G3：回溯项目源积累（knowledge_ids＋knowledge_candidate_ids），
    # 洞察要能说「来自你的卡X」；直接写稿项目源积累为空，如实标注，不报错。
    knowledge_ids, source_knowledge = creation_service.source_knowledge_snapshot(STORE, project)
    axis = creation_service.axis_context(STORE)
    thesis = str((project.get('selected_topic') or {}).get('thesis') or '').strip()[:1200] or axis['thesis']
    # t34 flywheel／t2：规范 per-user 段＋persona（信念×口吻×主线）＋本条命题＋本地口径
    # 常量前置到功能模板之前；变量证据（项目/作品/快照/评论/源积累）留最后 payload。
    # 本地口径，不改云端模板。
    stable_blocks = creation_service.guidance_stable_blocks(STORE, [
        ('本条内容命题（待验证假设）：' + thesis) if thesis else '',
        ('source_knowledge 是这条内容真实用过的源积累（可能为空＝直接写稿、未关联积累，'
         '如实说明即可，不要编造来源）。复盘时指出哪些源主张与观众反应呼应，'
         '写成待验证假设并引用真实证据ID；结合稳定段的主线目标与命题看内容是否服务目标'
         '（赚钱也是正当目标，不回避）；不替本人下人生结论，不声称因果已被验证。')])
    answer = providers.llm_json(prompt_store.get('feedback.insight'), {
        'project': {'id': project['id'], 'title': project.get('title') or ''},
        'works': [{'id': w['id'], 'platform': w.get('platform'),
                   'platform_video_id': w.get('platform_video_id'),
                   'title': w.get('title') or ''} for w in works],
        'snapshots': [{'id': s['id'], 'platform_work_id': s.get('platform_work_id'),
                       'metric_key': s.get('metric_key'), 'value': s.get('value'),
                       'collected_at': s.get('collected_at')} for s in snapshots],
        'comment_feedback': [{'id': f['id'], 'platform_work_id': f.get('platform_work_id'),
                              'category': f.get('category'), 'note': f.get('note') or '',
                              'comment': (comments_by_id.get(f.get('platform_comment_id') or {})
                                          or {}).get('content') or ''} for f in feedbacks],
        'evidence_pool': sorted(evidence_pool),
        # G3：源知识随 payload 下发（命题/主线已前置进稳定段）。
        'source_knowledge': source_knowledge,
        'knowledge_ids': knowledge_ids},
        stable_blocks=stable_blocks)
    title = str(answer.get('title') or '').strip()
    body = str(answer.get('body') or '').strip()
    if not title or not body:
        raise ValueError('模型复盘缺少标题或正文；未入库任何洞察。')
    if _has_banned_claim(title) or _has_banned_claim(body):
        raise ValueError('复盘声称因果/效果已被验证；只允许待验证经验，拒绝入库。')
    insight_type = answer.get('insight_type') or 'data'
    if insight_type not in contracts.ENUMS['insight_type']:
        raise ValueError(f'insight_type 无效：{insight_type!r}；拒绝入库。')
    findings = answer.get('findings')
    if not isinstance(findings, list) or not findings:
        raise ValueError('模型复盘缺少 findings 证据链；未入库任何洞察。')
    clean_findings = []
    used_evidence = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError('finding 必须是对象；未入库任何洞察。')
        claim = str(item.get('claim') or '').strip()
        evidence_ids = item.get('evidence_ids')
        if not claim:
            raise ValueError('finding 缺少 claim；未入库任何洞察。')
        if _has_banned_claim(claim):
            raise ValueError('finding 声称因果/效果已被验证；只允许待验证经验，拒绝入库。')
        if (not isinstance(evidence_ids, list) or not evidence_ids
                or not all(isinstance(k, str) for k in evidence_ids)):
            raise ValueError('每条 finding 必须给出非空 evidence_ids 数组；未入库任何洞察。')
        for key in evidence_ids:
            if key not in evidence_pool:
                raise ValueError(f'复盘引用了不存在的证据ID：{key!r}；拒绝入库。')
        clean_findings.append({'claim': claim[:1200],
                               'evidence_ids': list(dict.fromkeys(evidence_ids))})
        used_evidence.extend(evidence_ids)
    evidence_ids = sorted(set(used_evidence))
    if len(evidence_ids) < 2:
        raise ValueError('复盘洞察至少需要 2 条可回溯证据ID（指标快照或已分类评论）；未入库任何洞察。')
    covered_work_ids = sorted({evidence_owner[key] for key in evidence_ids
                               if evidence_owner.get(key)})
    platform_work_id = covered_work_ids[0] if len(covered_work_ids) == 1 else None
    record = contracts.create_review_insight(
        title[:160], body[:12000], insight_type,
        project_id=project['id'], platform_work_id=platform_work_id,
        # G2：knowledge_id 不再恒空——回溯项目源积累填第一条；无源积累保持 None。
        knowledge_id=knowledge_ids[0] if knowledge_ids else None)
    record.update({'findings': clean_findings,
                   'evidence_ids': evidence_ids,
                   'evidence_count': len(evidence_ids),
                   'platform_work_ids': covered_work_ids,
                   'platform_work_urls': [work_url.get(wid) for wid in covered_work_ids],
                   'status': 'candidate',  # 候选待验证；本人确认后才置 confirmed
                   'markers': ['模型推断', '待验证'], 'created_by': 'model_assisted',
                   'validation': 'unverified',
                   # G3：源积累明细＋命题＋主线落库，前端可点 knowledge_id 跳卡（可见化预留）。
                   'knowledge_ids': knowledge_ids,
                   'source_knowledge': source_knowledge,
                   'thesis': thesis,
                   'main_goal': axis['main_goal'],
                   'source_note': '已关联源积累' if knowledge_ids else '直接写稿项目，未关联积累'})
    saved = STORE.put(contracts.KIND_REVIEW_INSIGHT, record)
    # 2026-11-17 t68：回流闭环——强洞察生成候选卡/命题修订候选；失败不影响洞察本身落库。
    reflow = None
    try:
        reflow = reflow_insight(saved)
    except Exception as exc:  # noqa: BLE001
        reflow = {'ok': False, 'reason': '回流生成失败：%s' % type(exc).__name__,
                  'knowledge_candidate_id': '', 'thesis_proposal_id': ''}
    if reflow and (reflow.get('knowledge_candidate_id') or reflow.get('thesis_proposal_id')):
        saved = STORE.patch(contracts.KIND_REVIEW_INSIGHT, saved['id'], {
            'candidate_knowledge_id': reflow.get('knowledge_candidate_id') or '',
            'thesis_proposal_id': reflow.get('thesis_proposal_id') or '',
            'auto_accepted_card': bool(reflow.get('auto_accepted_card')),
            'auto_thesis_applied': bool(reflow.get('auto_thesis_applied')),
            'thesis_applied': reflow.get('thesis_applied') or '',
            'thesis_previous': reflow.get('thesis_previous') or '',
            'auto_processed_at': contracts.now_ms() if reflow.get('ok') else None})
    message = '复盘洞察已生成并保存（模型推断，待验证；本人确认后 status 才能变为 confirmed）。'
    if reflow and reflow.get('ok'):
        message += ' 这条复盘已自动沉淀成认知卡（可随时撤销）'
        if reflow.get('auto_thesis_applied'):
            message += '；命题已按高置信候选自动更新，可在成长页一键回滚。'
        elif reflow.get('thesis_proposal_id'):
            message += '；命题有出入的候选已列出，等你拍板。'
        else:
            message += '。'
    elif reflow and reflow.get('reason') and reflow.get('reason') != '':
        message += '（这次没有生成候选：%s）' % reflow.get('reason')
    return {'item': saved, 'reflow': reflow, 'message': message}



# ---------------------------------------------------------------------------
# 2026-11-17 t68 回流闭环（飞轮命门）：复盘洞察 → 候选认知卡 ＋ 命题修订候选。
# 原则（主理人"少而精"）：只在洞察足够强时生成；AI 只出候选，背书/拍板永远由用户做。
#   - 候选卡：content_index 建卡，endorsement=reference_only、markers 带「待本人背书」；
#     用户背书 accepted 后由既有 creation_service 积累稳定段自动收录，下轮选题/写稿真用到。
#   - 命题修订候选：挂 growth 根目标 thesis_proposals，用户在 growth 侧采纳才改命题写版本史。
#   - 弱 insight 不生成任何候选；与现有认知卡重复的结论也不生成（宁缺毋滥）。
# ---------------------------------------------------------------------------
REFLOW_MIN_EVIDENCE = int(os.getenv('IP_REFLOW_MIN_EVIDENCE', '4') or 4)
REFLOW_MIN_FINDINGS = int(os.getenv('IP_REFLOW_MIN_FINDINGS', '2') or 2)
REFLOW_SIMILARITY = float(os.getenv('IP_REFLOW_SIMILARITY', '0.62') or 0.62)
AUTO_THESIS_MIN_EVIDENCE = int(os.getenv('IP_REFLOW_AUTO_THESIS_MIN_EVIDENCE', '6') or 6)
REFLOW_CONFLICT_WORDS = ('但', '反而', '并不', '不一定', '前提', '修正', '推翻', '失效',
                         '其实', '重新理解', '问题不在', '不是', '变化', '代价')


def _first_sentence(text, limit=160):
    body = ' '.join(str(text or '').replace('\r', ' ').split())
    for sep in ('。', '！', '？', '；', '\n'):
        idx = body.find(sep)
        if 8 <= idx <= limit:
            return body[:idx].strip()
    return body[:limit].strip()


def _reflow_gate(rec):
    """少而精门槛：证据充分＋结论 distinct＋可行动。返回 (ok, reason, claim)。"""
    evidence_count = int(rec.get('evidence_count') or len(rec.get('evidence_ids') or []))
    findings = rec.get('findings') or []
    claim = str(rec.get('title') or '').strip() or _first_sentence(rec.get('body') or '', 120)
    if evidence_count < REFLOW_MIN_EVIDENCE:
        return False, '证据只有 %d 条（门槛 %d）' % (evidence_count, REFLOW_MIN_EVIDENCE), claim
    if len(findings) < REFLOW_MIN_FINDINGS:
        return False, '证据链只有 %d 条 finding（门槛 %d）' % (len(findings), REFLOW_MIN_FINDINGS), claim
    if rec.get('insight_type') not in ('data', 'pattern', 'content', 'audience', 'works'):
        return False, '洞察类型 %s 不生成候选' % rec.get('insight_type'), claim
    if len(claim) < 8:
        return False, '结论太短，无法作为一张认知卡', claim
    if not str(rec.get('body') or '').strip():
        return False, '洞察没有正文，无法沉淀成认知卡', claim
    if _has_banned_claim(claim):
        return False, '结论声称因果/效果已被验证，不能入库', claim
    similar = []
    try:
        import content_index
        similar = content_index.similar_cards(
            STORE, claim + ' ' + str(rec.get('body') or '')[:1500],
            threshold=REFLOW_SIMILARITY)
    except Exception as exc:  # noqa: BLE001
        similar = []
        return False, '去重检查失败（%s），本次不生成候选' % type(exc).__name__, claim
    if similar:
        return False, '与现有认知卡重复（%s，重合 %.2f）' % (similar[0][1].get('title'), similar[0][0]), claim
    return True, '', claim


def _candidate_card(rec, claim):
    """把强洞察落成候选认知卡（待背书；不自动采纳）。"""
    import content_index
    category, legacy = content_index.normalize_category('随手记')
    boundary = ('来自一次复盘洞察（证据 %d 条），样本有限；AI 推断，已自动沉淀进积累，可随时撤销。'
                % int(rec.get('evidence_count') or 0))
    item = STORE.put('knowledge', {
        'legacy_id': '复盘沉淀', 'title': claim[:160], 'category': category,
        'category_legacy': legacy, 'category_aliases': content_index.row_aliases(category, legacy),
        'claim': claim[:1200], 'boundary': boundary[:2000],
        'body': str(rec.get('body') or '')[:40000], 'body_origin': 'manual',
        'source_id': None,
        'source_locator': {'kind': 'review_insight', 'insight_id': rec['id'],
                           'project_id': rec.get('project_id') or ''},
        'source_type': 'review_insight', 'source_label': '复盘洞察 · %s' % (rec.get('title') or ''),
        'source_date': '',
        'links': list(rec.get('knowledge_ids') or [])[:20],
        # 2026-11-17 t73 回流自动化：过少而精门槛的候选卡自动背书进积累（人做例外监督，可 PATCH 撤销）。
        'markers': ['模型推断', '自动沉淀'],
        'endorsement': 'accepted', 'validation': 'unverified',
        'lifecycle': 'active', 'content_use': 'available',
        'auto_accepted_at': contracts.now_ms(), 'auto_source': 'reflow_auto',
        'reflow_insight_id': rec['id'], 'reflow_evidence_count': rec.get('evidence_count') or 0})
    try:
        content_index.ensure_fresh(STORE, wait=True)
    except Exception:
        pass
    return item


def _thesis_candidate(rec, claim):
    """洞察若与当前命题冲突/深化，登记命题修订候选（只登记，不采纳）。"""
    import growth_service
    state = growth_service.thesis_proposals(STORE)
    current = str(state.get('thesis') or '').strip()
    if not current:
        return None
    text = (str(rec.get('title') or '') + ' ' + str(rec.get('body') or ''))
    if not any(word in text for word in REFLOW_CONFLICT_WORDS):
        return None
    proposed = claim if len(claim) <= 200 else _first_sentence(claim, 200)
    if not proposed or proposed == current:
        return None
    reason = '复盘洞察与当前命题有出入：%s' % _first_sentence(rec.get('body') or '', 240)
    try:
        proposal = growth_service.create_thesis_proposal(
            STORE, proposed, original=current, reason=reason,
            insight_id=rec['id'], source='review_insight',
            evidence_count=rec.get('evidence_count'))
    except (ValueError, KeyError):
        return None
    # 2026-11-17 t73：高置信候选自动采纳（留上一版＋可在 growth 页一键回滚）；低置信仍列候选待拍板。
    hits = sum(1 for w in REFLOW_CONFLICT_WORDS if w in text)
    auto = (int(rec.get('evidence_count') or 0) >= AUTO_THESIS_MIN_EVIDENCE
            and hits >= 2 and len(reason) >= 24)
    proposal['confidence'] = 'high' if auto else 'normal'
    if auto:
        try:
            applied = growth_service.decide_thesis_proposal(STORE, proposal['id'], True,
                                                            decided_by='reflow_auto')
            proposal.update({'auto_applied': True, 'status': 'accepted',
                             'thesis_applied': applied.get('thesis') or proposed,
                             'thesis_previous': current,
                             'auto_applied_at': contracts.now_ms()})
        except (ValueError, KeyError):
            proposal['auto_applied'] = False
    else:
        proposal['auto_applied'] = False
    return proposal


def reflow_insight(record):
    """回流入口：强洞察 → 候选卡（待背书）＋命题修订候选（待拍板）。返回结构化结果给复盘页。"""
    ok, reason, claim = _reflow_gate(record)
    result = {'ok': False, 'reason': reason, 'claim': claim,
              'knowledge_candidate_id': '', 'thesis_proposal_id': '',
              'auto_accepted_card': False, 'auto_thesis_applied': False,
              'thesis_applied': '', 'thesis_previous': '',
              'undo': {'knowledge_id': '', 'thesis_proposal_id': ''},
              'gate': {'min_evidence': REFLOW_MIN_EVIDENCE, 'min_findings': REFLOW_MIN_FINDINGS,
                       'similarity': REFLOW_SIMILARITY}}
    if not ok:
        return result
    card = _candidate_card(record, claim)
    proposal = _thesis_candidate(record, claim)
    auto_thesis = bool(proposal and proposal.get('auto_applied'))
    result.update({'ok': True,
                   'reason': '已自动沉淀进积累' + ('，命题也已按高置信候选自动更新（可回滚）' if auto_thesis else ''),
                   'knowledge_candidate_id': card['id'],
                   'thesis_proposal_id': (proposal or {}).get('id') or '',
                   'auto_accepted_card': True,
                   'auto_thesis_applied': auto_thesis,
                   'thesis_applied': (proposal or {}).get('thesis_applied') or '',
                   'thesis_previous': (proposal or {}).get('thesis_previous') or '',
                   'undo': {'knowledge_id': card['id'],
                            'thesis_proposal_id': (proposal or {}).get('id') or ''}})
    return result

def _evidence_items(evidence_ids):
    """把证据ID还原成可点回原评论/指标的明细；只读已落库记录，查不到就跳过。"""
    feedbacks = {f['id']: f for f in STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)}
    snapshots = {s['id']: s for s in STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)}
    comments = {c['id']: c for c in STORE.list(contracts.KIND_PLATFORM_COMMENT, 50000)}
    items = []
    for key in evidence_ids:
        feedback = feedbacks.get(key)
        if feedback:
            comment = comments.get(feedback.get('platform_comment_id') or '') or {}
            items.append({'id': key, 'kind': 'comment_feedback',
                          'comment_id': comment.get('id'),
                          'platform_comment_id': comment.get('platform_comment_id'),
                          'author_name': comment.get('author_name') or '',
                          'content': comment.get('content') or '',
                          'category': feedback.get('category'),
                          'note': feedback.get('note') or '',
                          'source_url': comment.get('source_url') or '',
                          'platform_created_at': comment.get('platform_created_at')})
            continue
        snapshot = snapshots.get(key)
        if snapshot:
            items.append({'id': key, 'kind': 'metric_snapshot',
                          'metric_key': snapshot.get('metric_key'),
                          'value': snapshot.get('value'),
                          'collected_at': snapshot.get('collected_at')})
    return items


def _insight_view(item):
    """固定字段：evidence_count/evidence_ids/evidence_items/platform_work_ids/platform_work_urls/status。"""
    view = dict(item)
    evidence_ids = view.get('evidence_ids')
    if not isinstance(evidence_ids, list):
        findings = view.get('findings') if isinstance(view.get('findings'), list) else []
        evidence_ids = [key for finding in findings if isinstance(finding, dict)
                        for key in (finding.get('evidence_ids') or []) if isinstance(key, str)]
    view['evidence_ids'] = list(evidence_ids or [])
    view['evidence_count'] = len(view['evidence_ids'])
    work_ids = view.get('platform_work_ids')
    if not isinstance(work_ids, list):
        work_ids = [view['platform_work_id']] if view.get('platform_work_id') else []
    view['platform_work_ids'] = list(work_ids)
    urls = view.get('platform_work_urls')
    if not isinstance(urls, list):
        by_id = {w['id']: w for w in STORE.list(contracts.KIND_PLATFORM_WORK, 5000)}
        urls = [(by_id.get(wid) or {}).get('source_url') for wid in view['platform_work_ids']]
    view['platform_work_urls'] = urls
    view['evidence_items'] = _evidence_items(view['evidence_ids'])
    view['status'] = view.get('status') or 'candidate'
    # 2026-11-17 激活闭环：老洞察没有这些字段时给空值，前端可统一渲染「来源积累＋服务主线」区。
    view['knowledge_ids'] = view.get('knowledge_ids') or (
        [view['knowledge_id']] if view.get('knowledge_id') else [])
    view['source_knowledge'] = view.get('source_knowledge') or []
    view['thesis'] = view.get('thesis') or ''
    view['main_goal'] = view.get('main_goal') or None
    view['source_note'] = view.get('source_note') or (
        '已关联源积累' if view['knowledge_ids'] else '未关联积累')
    return view


def list_insights(project_id):
    _require_project(project_id)
    items = [i for i in STORE.list(contracts.KIND_REVIEW_INSIGHT, 5000)
             if i.get('project_id') == project_id]
    items.sort(key=lambda i: ((i.get('created_at') or ''), (i.get('id') or '')), reverse=True)
    return {'items': [_insight_view(i) for i in items], 'total': len(items)}


# ---------------------------------------------------------------------------
# 复盘洞察 → 新选题 → 立项（复用 bench_service.start_project，幂等）
# ---------------------------------------------------------------------------

def _capture_entry(project_id):
    """G4 捕获入口（可见化预留）：定稿后一键捕获讲稿回积累，走作品既有入口。"""
    if not project_id:
        return None
    return {'available': True, 'mode': 'capture',
            'endpoint': '/api/projects/%s/writing-suggestions' % project_id,
            'note': '定稿后调用；预览不写库，confirm=true 才入库，可拒可去重。'}


def start_topic(insight_id):
    """基于 review_insight 生成一条 topic 并直接立项。

    只允许 status 为 candidate/confirmed 且存在 project_id 的洞察；
    证据 ID 逐条回验真实存在；模型输出禁止「已被数据验证/必然有效」类措辞。
    固定返回 {topic_id, project_id, reused}；同一 insight 重复调用复用已创建的
    topic/project，不重复立项、不覆盖已有项目。
    """
    if not isinstance(insight_id, str) or not insight_id:
        raise ValueError('insight_id 必须是非空字符串。')
    insight = STORE.get(contracts.KIND_REVIEW_INSIGHT, insight_id)
    if insight is None:
        raise KeyError('没有找到该复盘洞察。')
    status = insight.get('status') or 'candidate'
    if status not in ('candidate', 'confirmed'):
        raise ValueError(f'洞察状态 {status!r} 不能生成选题；只允许 candidate/confirmed。')
    project_id = insight.get('project_id')
    if not project_id:
        raise ValueError('该洞察未关联项目，不能生成选题。')
    if STORE.get('project', project_id) is None:
        raise KeyError('洞察关联的项目不存在。')
    # 2026-11-17 激活闭环：选题继承洞察的源积累＋命题＋主线（G1/G3 延伸到开题）。
    knowledge_ids = [k for k in (insight.get('knowledge_ids')
                                 or ([insight['knowledge_id']] if insight.get('knowledge_id') else []))
                     if isinstance(k, str) and k]
    source_knowledge = insight.get('source_knowledge') or []
    axis = creation_service.axis_context(STORE)
    source_thesis = str(insight.get('thesis') or '').strip()[:1200] or axis['thesis']
    # 可见化预留：前端据这些字段显示「来自你的爆款复盘」＋源卡＋主线＋捕获入口。
    visibility = {'origin': 'feedback', 'origin_label': '来自你的爆款复盘',
                  'insight_id': insight_id, 'insight_title': insight.get('title') or '',
                  'knowledge_ids': knowledge_ids, 'main_goal': axis['main_goal']}
    # 幂等：同一 insight 复用已创建的 topic/project。
    existing = next((t for t in STORE.list(contracts.KIND_TOPIC, 5000)
                     if t.get('review_insight_id') == insight_id), None)
    if existing is not None:
        linked = (existing.get('data') or {}).get('project_id') or existing.get('project_id')
        return {'topic_id': existing['id'], 'project_id': linked, 'reused': True,
                **visibility,
                'knowledge_ids': existing.get('knowledge_ids') or knowledge_ids,
                'capture_entry': _capture_entry(linked)}
    evidence_ids = list(insight.get('evidence_ids') or [])
    if not evidence_ids:
        raise ValueError('该洞察没有证据链，不能生成选题；不允许凭空开题。')
    snapshots = {s['id']: s for s in STORE.list(contracts.KIND_METRIC_SNAPSHOT, 50000)}
    feedbacks = {f['id']: f for f in STORE.list(contracts.KIND_COMMENT_FEEDBACK, 50000)}
    comments = {c['id']: c for c in STORE.list(contracts.KIND_PLATFORM_COMMENT, 50000)}
    evidence_detail = []
    for key in evidence_ids:
        if key in snapshots:
            snap = snapshots[key]
            evidence_detail.append({'id': key, 'kind': 'metric_snapshot',
                                    'metric_key': snap.get('metric_key'),
                                    'value': snap.get('value'),
                                    'collected_at': snap.get('collected_at')})
        elif key in feedbacks:
            feedback = feedbacks[key]
            comment = comments.get(feedback.get('platform_comment_id') or '')
            evidence_detail.append({'id': key, 'kind': 'comment_feedback',
                                    'category': feedback.get('category'),
                                    'note': feedback.get('note') or '',
                                    'comment': (comment or {}).get('content') or ''})
        else:
            raise ValueError(f'洞察引用了不存在的证据ID：{key!r}；拒绝生成选题。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing(
            '生成模型尚未配置；不能凭空开题。配置 IP_LLM_URL、IP_LLM_MODEL 后重试。')
    works = [w for w in STORE.list(contracts.KIND_PLATFORM_WORK, 5000)
             if w['id'] in set(insight.get('platform_work_ids') or [])]
    # t34 flywheel／t2：规范 per-user 段＋persona（信念×口吻×主线）＋本条选题命题＋本地口径
    # 常量前置到功能模板之前；变量证据留最后 payload。
    stable_blocks = creation_service.guidance_stable_blocks(STORE, [
        ('本条选题命题（待验证假设）：' + source_thesis) if source_thesis else '',
        ('新选题要从 source_knowledge 的本人真实主张里长出来（为空则如实基于洞察证据），'
         '服务稳定段的主线目标（赚钱也是正当目标）；命题是待验证假设，'
         '不得声称已被数据验证/必然有效；不虚构本人经历。')])
    answer = providers.llm_json(prompt_store.get('feedback.topic'), {
        'insight': {'id': insight['id'], 'title': insight.get('title') or '',
                    'body': insight.get('body') or '', 'status': status},
        'works': [{'id': w['id'], 'title': w.get('title') or '',
                   'platform_video_id': w.get('platform_video_id')} for w in works],
        'evidence': evidence_detail,
        # 2026-11-17 激活闭环：源积累进选题提示（命题/主线已前置进稳定段；本地口径，不改云端模板）。
        'source_knowledge': source_knowledge},
        stable_blocks=stable_blocks)
    if not isinstance(answer, dict):
        raise ValueError('模型选题格式无效；未创建任何选题。')
    title = str(answer.get('title') or '').strip()
    if not title:
        raise ValueError('模型选题缺少标题；未创建任何选题。')
    angle = str(answer.get('angle') or '')[:600]
    thesis = str(answer.get('thesis') or '')[:1200]
    if _has_banned_claim(title) or _has_banned_claim(angle) or _has_banned_claim(thesis):
        raise ValueError('选题声称已被数据验证/必然有效；只允许待验证假设，拒绝入库。')
    note = ('；'.join(x for x in (angle, thesis) if x))[:1200] or '模型依据复盘洞察生成，待本人核对。'
    record = contracts.create_topic(title[:160], status='candidate', note=note,
                                    review_insight_id=insight_id,
                                    knowledge_id=knowledge_ids[0] if knowledge_ids else None)
    record.update({
        'review_insight_id': insight_id,
        # 注意：顶层 project_id 留给立项逻辑回写（新内容项目）；
        # 洞察来源项目记在 source_project_id，避免被 start_project 误判为已立项。
        'source_project_id': project_id,
        'platform_work_ids': list(insight.get('platform_work_ids') or []),
        'evidence_ids': evidence_ids,
        'origin': 'feedback',
        'markers': ['模型推断', '待验证'],
        'validation': 'unverified',
        'created_by': 'model_assisted',
        'angle': angle, 'thesis': thesis,
        'insight_title': insight.get('title') or '',
        # 2026-11-17 激活闭环：源积累随选题带入新项目（start_project 复制 knowledge_ids），
        # 命题/主线留快照供写稿与复盘溯源；无源积累为空列表，不编造。
        'knowledge_ids': knowledge_ids,
        'source_knowledge_ids': knowledge_ids,
        'source_thesis': source_thesis,
        'main_goal_id': (axis['main_goal'] or {}).get('id'),
        'main_goal': axis['main_goal'],
        'data': {}})
    saved = STORE.put(contracts.KIND_TOPIC, record)
    started = bench_service.start_project(saved['id'])
    return {'topic_id': saved['id'], 'project_id': started['project_id'],
            'reused': bool(started.get('reused')),
            **visibility, 'capture_entry': _capture_entry(started['project_id'])}
