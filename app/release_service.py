"""辅助发布：只整理当前定稿与登记本人回填，不操作平台、不伪造发布回执。"""
import hashlib
import re
import threading
from urllib.parse import urlsplit
from db import now
import contracts
import receipt_verify

_LOCK = threading.Lock()
PLATFORMS = {
    'douyin': ('抖音', 'https://creator.douyin.com/', ('douyin.com',)),
    'xiaohongshu': ('小红书', 'https://creator.xiaohongshu.com/', ('xiaohongshu.com', 'xhslink.com')),
    'bilibili': ('B站', 'https://member.bilibili.com/', ('bilibili.com', 'b23.tv')),
    'wechat_channels': ('视频号', 'https://channels.weixin.qq.com/platform', ('channels.weixin.qq.com',)),
    'kuaishou': ('快手', 'https://cp.kuaishou.com/', ('kuaishou.com', 'gifshow.com')),
}

def prepare(store, project_id):
    project = store.get('project', project_id)
    if not project:
        raise KeyError('作品不存在。')
    version = store.get('script_version', project.get('confirmed_version_id') or '')
    ready = bool(version and version.get('project_id') == project_id and not project.get('draft_changed'))
    pack = project.get('publication_pack') or {}
    if pack.get('script_version_id') != project.get('confirmed_version_id'):
        pack = {}
    parts = version.get('parts', []) if ready else []
    artifact = store.get('artifact', project.get('reviewed_artifact_id') or '')
    if not (artifact and artifact.get('project_id') == project_id and artifact.get('script_version_id') == project.get('confirmed_version_id') and not project.get('draft_changed') and artifact.get('kind') in ('clean_video','packaged_video')):
        artifact = None
    # 封面只在成片为「字幕包装」时生成；跨作品/错类型的 id 一律不认。
    cover = None
    if artifact and artifact.get('cover_id'):
        candidate = store.get('artifact', artifact['cover_id'])
        if candidate and candidate.get('project_id') == project_id and candidate.get('kind') == 'cover':
            cover = candidate
    return {'project_id': project_id, 'title': project.get('title',''), 'ready': ready,
            'script_version_id': version['id'] if ready else None,
            'selected_title': pack.get('selected_title') or project.get('title',''),
            'caption': pack.get('caption','') if ready else '', 'tags': pack.get('tags',[]),
            'text': '\n\n'.join(p.get('text','') for p in parts),
            'artifact_id': artifact['id'] if artifact else None,
            'video_url': '/api/artifacts/' + artifact['id'] if artifact else None,
            'cover_id': cover['id'] if cover else None,
            'cover_url': '/api/artifacts/' + cover['id'] if cover else None,
            'platforms': [{'id': key, 'name': value[0], 'creator_url': value[1]} for key, value in PLATFORMS.items()],
            'receipts': [r for r in store.list('publication_receipt', 10000) if r.get('project_id') == project_id]}

def kit(store, project_id):
    """四件套打包视图：标题/简介/话题 + 成片/封面资源地址，供手机端复制与存相册。

    是 prepare() 的超集（旧字段全部保留），只做资料整理：
    不操作平台、不自动发布、不伪造回执；资源地址均为本站同源接口。
    """
    prepared = prepare(store, project_id)
    tags = ' '.join('#' + str(t).lstrip('#') for t in prepared.get('tags') or [])
    share_text = '\n\n'.join(x for x in (prepared.get('selected_title'), prepared.get('caption'), tags) if x)
    video_id = prepared.get('artifact_id')
    cover_id = prepared.get('cover_id')
    script_ready = bool(prepared.get('ready'))
    video_ready = bool(video_id)
    # 文稿定稿不等于可以交付：成片没审阅就不能算 ready，否则会给出一个点了必报错的导出口
    #（历史缺陷：ready=true 但 video=null，export?include_video=1 直接 404）。
    ready = script_ready and video_ready
    if not script_ready:
        blocked_reason, next_action = '当前文稿还没有定稿。', 'confirm_script'
    elif not video_ready:
        blocked_reason, next_action = '成片还没审阅，先点「确认审阅通过」再导出。', 'review'
    else:
        blocked_reason, next_action = '', ''
    return {**prepared,
            'ready': ready, 'script_ready': script_ready, 'video_ready': video_ready,
            'blocked_reason': blocked_reason, 'next_action': next_action,
            'review_url': '/api/projects/' + project_id + '/review',
            'share_text': share_text,
            'video': ({'id': video_id, 'url': prepared['video_url'],
                       'download_url': prepared['video_url'] + '?download=1'} if video_id else None),
            'cover': ({'id': cover_id, 'url': prepared['cover_url'],
                       'download_url': prepared['cover_url'] + '?download=1'} if cover_id else None),
            # 只有审阅通过才给含视频的四件套地址；未审阅不给必报错的链接。
            'export_zip_url': ('/api/projects/' + project_id + '/export?include_video=1') if ready else None,
            'delivery_note': '交付资料，尚未发送到任何平台；发布由本人在平台完成，此处不自动代发。'}

def register(store, data):
    if data.get('confirmed') is not True:
        raise ValueError('请确认这是本人已经发布的作品链接。')
    prepared = prepare(store, data.get('project_id') or '')
    if not prepared['ready']:
        raise ValueError('请先确认当前文稿版本，再登记发布。')
    if data.get('script_version_id') != prepared['script_version_id']:
        raise ValueError('文稿版本已变化，请刷新发布资料。')
    platform = data.get('platform')
    if platform not in PLATFORMS:
        raise ValueError('请选择支持的平台。')
    url = str(data.get('url') or '').strip()
    parsed = urlsplit(url)
    hosts = PLATFORMS[platform][2]
    if (len(url) > 2048 or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or
        parsed.port not in (None,443) or not any(parsed.hostname == h or parsed.hostname.endswith('.'+h) for h in hosts) or
        not parsed.path.strip('/') or any(ord(c)<33 for c in url)):
        raise ValueError('请填写所选平台的 HTTPS 作品链接，不是创作后台或账号主页。')
    if parsed.hostname.startswith(('creator.','cp.','member.')) or parsed.path.startswith(('/user/','/profile/','/platform','/creator','/publish','/article/publish')):
        raise ValueError('请回填发布后的作品链接，不是后台或账号主页。')
    # 回执核验（P0-3）：抖音链接解析不出作品 ID 时不再整单拒绝——
    # 仍保存 URL 并附 unparsed_post_url 警告（任务书 §1.3：解析失败仍落库）；
    # 其他平台仍是待验收占位，只记 self_reported，不把「没解析」冒充成「已核验」。
    checked = receipt_verify.parse_post_ref(platform, url)
    unparsed = None
    failure = None       # 失败结论（解析失败或轻核验不通过）→ platform_work.last_error_* 字段
    light_note = None
    if platform == 'douyin' and not checked['ok']:
        unparsed = {'code': checked['error_code'], 'message': checked['error_message'],
                    'checked_at': checked['checked_at']}
        failure = {'code': checked['error_code'], 'message': checked['error_message'],
                   'at': checked['checked_at']}
    # 本人回填默认仍是自述（self_reported）：链接解析成功不等于平台内容已验证，
    # 只有显式轻核验通过才升级 verified、不通过才降级 unverified。
    verification = 'self_reported'
    # 可选轻核验：打开作品公开页比对标题/作者；单次尝试，绝不重试轰炸。
    # ok=None（被平台拦截/环境不可用）只留痕，不降级核验等级——skip 不是比对不通过。
    if data.get('light_verify') is True and platform == 'douyin' and checked['ok']:
        light_note = receipt_verify.light_verify(platform, url,
                                                 expected_title=prepared.get('selected_title'))
        if light_note['ok'] is True:
            verification = 'verified'
        elif light_note['ok'] is False:
            verification = 'unverified'
            failure = {'code': light_note['error_code'], 'message': light_note['error_message'],
                       'at': light_note['checked_at']}
    key = 'receipt_' + hashlib.sha256((platform+'|'+url.split('#')[0]).encode()).hexdigest()[:24]
    with _LOCK:
        raw_extra = {'receipt_check': checked}
        if light_note:
            raw_extra['receipt_light_check'] = light_note
        platform_work = _link_platform_work(store, prepared, platform, url, verification,
                                            failure=failure, raw_extra=raw_extra)
        existing = store.get('publication_receipt', key)
        if existing:
            if existing.get('project_id') != data['project_id']:
                raise ValueError('这个链接已经关联另一条作品，请核对。')
            return {**existing, 'duplicate': True, 'platform_work_id': platform_work['id']}
        receipt = {
            'project_id': data['project_id'], 'script_version_id': prepared['script_version_id'],
            'platform': platform, 'source_url': url, 'title': prepared['selected_title'],
            'published_at': None, 'registered_at': now(), 'verification': verification,
            'status': 'reported_published', 'artifact_id': prepared['artifact_id'],
            # 2026-11-14：发布登记以 platform_work 为权威，回执只留审计信息并指向它。
            'platform_work_id': platform_work['id']}
        if unparsed:
            receipt['unparsed_post_url'] = unparsed  # 解析失败仍落库的警告（不冒充已解析）
        if light_note:
            # 轻核验摘要只进回执留痕；完整原始信号在 platform_work.raw.receipt_light_check。
            receipt['light_check'] = {k: light_note.get(k) for k in
                                      ('ok', 'verification', 'error_code', 'error_message',
                                       'classification', 'work_title', 'work_author',
                                       'login_state', 'page_url', 'checked_at')}
        record = store.put('publication_receipt', receipt, key)
        return {**record, 'platform_work_id': platform_work['id']}


def _video_id(platform, url):
    if platform == 'douyin':
        try:
            import feedback_collect
            return feedback_collect.parse_video_url(url)[1]
        except Exception:
            pass
    return 'url_' + hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]


def _link_platform_work(store, prepared, platform, url, verification=None,
                        failure=None, raw_extra=None):
    """登记发布时同步写入 platform_work；复盘、矩阵、打卡都只认这一条权威记录。

    P0-3：失败结论落到作品级 last_error_code/last_error_message/last_attempt_at，
    不只留在回执/任务里；已有核验等级不被降级（failure 只补失败字段落痕）。
    """
    video_id = _video_id(platform, url)
    for work in store.list(contracts.KIND_PLATFORM_WORK, 20000):
        same = (work.get('source_url') == url or
                (work.get('platform') == platform and str(work.get('platform_video_id') or '') == video_id))
        if not same:
            continue
        owner = work.get('project_id')
        if owner and owner != prepared['project_id']:
            raise ValueError('这条作品已关联另一条内容，请核对链接后重试；本次未做任何改动。')
        if not owner:
            changes = {'project_id': prepared['project_id'],
                       'registration_mode': work.get('registration_mode') or 'manual',
                       'verification': work.get('verification') or verification}
            if failure:
                changes.update({'last_error_code': failure.get('code'),
                                'last_error_message': failure.get('message'),
                                'last_attempt_at': failure.get('at')})
            if raw_extra:
                changes['raw'] = {**(work.get('raw') or {}), **raw_extra}
            return store.patch(contracts.KIND_PLATFORM_WORK, work['id'], changes)
        return work
    metrics = {'raw': dict({'registered_url': url, 'registered_at': now(),
                            'registered_from': 'release_register'}, **(raw_extra or {}))}
    if failure:
        metrics.update({'last_error_code': failure.get('code'),
                        'last_error_message': failure.get('message'),
                        'last_attempt_at': failure.get('at')})
    return store.put(contracts.KIND_PLATFORM_WORK, contracts.create_platform_work(
        None, platform, video_id, prepared.get('selected_title') or prepared.get('title') or '平台作品',
        source_url=url, project_id=prepared['project_id'], registration_mode='manual',
        verification=verification, **metrics))
