"""多平台矩阵发布适配器（《链路扩展契约_2026-10-27》第 4 节）。

统一 PlatformAdapter 接口。未实现的方法一律抛 NotImplementedError，由服务层转成
明确的「未接入/待验收」，绝不返回模拟成功。抖音适配器复用现有 publish_worker.py
CLI（子进程隔离 + 超时），其余四平台为占位适配器。

ctx 注入边界（由 app.py 在启动时组装，便于测试替换）：
- run_worker:  callable(command_list, timeout=...) -> dict（现为 app.run_publish_worker）
- session_dir: 会话文件根目录（session_ref 相对路径只允许解析到此目录内）
- default_session: 占位 session_ref（'default'/'builtin'）对应的默认会话文件
- progress:    登录/发布进度文件
- artifacts_dir: 演练截图等中间产物目录
- publishing_ready: 发布执行器是否真实可用（浏览器环境 + publish_worker.py）
"""
import json
from pathlib import Path


class PlatformAdapter:
    """平台适配器抽象基类；任何方法未实现都抛 NotImplementedError（待验收）。"""
    platform = ''
    display_name = ''
    implemented = False

    def _not_implemented(self, method):
        raise NotImplementedError(
            f'{self.display_name or self.platform} 适配器未接入/待验收：{method} 尚未实现，不提供模拟结果。')

    def capabilities(self, ctx):
        return {'platform': self.platform, 'implemented': False, 'ready': False,
                'message': f'{self.display_name or self.platform} 未接入/待验收：适配器尚未实现，'
                           '不返回任何模拟结果。'}

    def login(self, account, ctx):
        """发起登录（如二维码），产出/刷新会话。"""
        self._not_implemented('login')

    def account_status(self, account, ctx):
        """实测会话有效性。"""
        self._not_implemented('account_status')

    def validate_content(self, task, artifact, account):
        """发布前校验（纯本地，不触网）：缺平台必填字段在此失败。"""
        self._not_implemented('validate_content')

    def upload(self, task, artifact, account, ctx):
        self._not_implemented('upload')

    def publish_plan(self, task, artifact, account, ctx):
        """演练：上传 + 填表，不点击发布。"""
        self._not_implemented('publish_plan')

    def submit(self, task, artifact, account, ctx):
        """真实发布。成功必须带平台作品 ID + 链接，否则不得判成功。"""
        self._not_implemented('submit')

    def cancel(self, task, account, ctx):
        self._not_implemented('cancel')

    def publish_status(self, platform_work, account, ctx):
        self._not_implemented('publish_status')

    def metrics(self, platform_work, account, ctx):
        self._not_implemented('metrics')

    def comments(self, platform_work, account, ctx, cursor=None):
        self._not_implemented('comments')


class DouyinAdapter(PlatformAdapter):
    """抖音：复用现有 publish_worker.py CLI 与单账号扫码会话。

    多账号经 platform_account.session_ref 传入（相对 session_dir 的路径，或占位
    'default' 用现有 PUBLISH_SESSION）；worker 本身支持 --session 参数化，但多账号
    并发/多开浏览器尚未验收，capabilities 中明示该限制。
    """
    platform = 'douyin'
    display_name = '抖音'
    implemented = True
    TITLE_MAX = 55        # 工作台上限；worker 内部再按平台规则截断到 30 字
    DESC_MAX = 1000
    TAGS_MAX = 10

    def capabilities(self, ctx):
        ready = bool(ctx and ctx.get('publishing_ready'))
        message = ('抖音单账号链路已接入（扫码登录 → 发布演练 → 本人确认发布）。'
                   '多账号经 session_ref 相对路径复用同一执行器，多账号并发尚未验收。'
                   if ready else
                   '发布执行器未就绪（需要 .runtime/browser-env 与 publish_worker.py），'
                   '登录与发布会真实失败并说明原因。')
        return {'platform': self.platform, 'implemented': True, 'ready': ready, 'message': message}

    def resolve_session(self, account, ctx):
        """session_ref 只允许占位标识或 session_dir 内相对路径；越界一律拒绝。"""
        ref = (account.get('session_ref') or 'default').strip()
        if ref in ('default', 'builtin', ''):
            return Path(ctx['default_session'])
        root = Path(ctx['session_dir']).resolve()
        target = (root / ref).resolve()
        if not target.is_relative_to(root):
            raise ValueError('session_ref 越界：只能指向会话目录内的相对路径。')
        return target

    def login(self, account, ctx):
        session = self.resolve_session(account, ctx)
        session.parent.mkdir(parents=True, exist_ok=True)
        result = ctx['run_worker'](
            ['login', '--session', str(session), '--progress', str(ctx['progress']),
             '--timeout', '280'], timeout=320)
        return {'ok': bool(result.get('logged_in')), 'logged_in': bool(result.get('logged_in')),
                'session_ref': account.get('session_ref') or 'default',
                'message': str(result.get('message') or ''), 'raw': result}

    def account_status(self, account, ctx):
        session = self.resolve_session(account, ctx)
        result = ctx['run_worker'](['status', '--session', str(session)], timeout=180)
        return {'valid': bool(result.get('logged_in')),
                'reason': str(result.get('reason') or ''),
                'message': str(result.get('message') or ''), 'raw': result}

    def validate_content(self, task, artifact, account):
        """抖音必填：标题（platform_payload.title）与真实存在的包装成片。"""
        payload = task.get('platform_payload') or {}
        issues = []
        title = str(payload.get('title') or '').strip()
        if not title:
            issues.append('缺少标题（platform_payload.title 为平台必填）')
        elif len(title) > self.TITLE_MAX:
            issues.append(f'标题超过 {self.TITLE_MAX} 字')
        desc = str(payload.get('desc') or payload.get('caption') or '')
        if len(desc) > self.DESC_MAX:
            issues.append(f'发布文案超过 {self.DESC_MAX} 字')
        tags = payload.get('tags') or []
        if not isinstance(tags, list):
            issues.append('tags 必须是数组')
        elif len(tags) > self.TAGS_MAX:
            issues.append(f'话题超过 {self.TAGS_MAX} 个')
        if artifact.get('kind') != 'packaged_video':
            issues.append('待发布产物不是包装成片（packaged_video）')
        video = artifact.get('path') or ''
        if not video or not Path(video).is_file():
            issues.append('待发布成片文件不存在')
        return {'ok': not issues, 'issues': issues,
                'raw': {'title': title, 'desc_len': len(desc),
                        'tags_count': len(tags) if isinstance(tags, list) else None,
                        'video': str(video)}}

    def _publish_command(self, command, task, artifact, account, ctx, extra=()):
        payload = task.get('platform_payload') or {}
        title = str(payload.get('title') or '').strip()
        desc = str(payload.get('desc') or payload.get('caption') or '')
        tags = payload.get('tags') or []
        session = self.resolve_session(account, ctx)
        return [command, '--session', str(session), '--video', str(artifact['path']),
                '--title', title, '--desc', desc,
                '--tags-json', json.dumps(tags, ensure_ascii=False), *extra]

    def publish_plan(self, task, artifact, account, ctx):
        screenshot = Path(ctx['artifacts_dir']) / ('matrix-plan-' + task['id'] + '.png')
        command = self._publish_command(
            'plan', task, artifact, account, ctx,
            extra=('--screenshot', str(screenshot), '--progress', str(ctx['progress'])))
        result = ctx['run_worker'](command, timeout=900)
        ok = bool(result.get('ok')) and bool(result.get('dry_run'))
        return {'ok': ok, 'screenshot': str(result.get('screenshot') or ''),
                'form_state': {'title': str((task.get('platform_payload') or {}).get('title') or '')},
                'message': str(result.get('message') or ''), 'raw': result}

    def submit(self, task, artifact, account, ctx):
        """准备发布资料：上传草稿并填表，绝不点击发布（红线：发布必须由本人在平台确认）。"""
        command = self._publish_command(
            'submit', task, artifact, account, ctx,
            extra=('--progress', str(ctx['progress'])))
        result = ctx['run_worker'](command, timeout=900)
        if result.get('needs_action'):
            return {'prepared': False, 'published': False, 'needs_action': True,
                    'message': str(result.get('message') or '平台要求本人完成额外验证（如短信验证）。'),
                    'raw': result}
        if not (result.get('ok') and result.get('prepared')):
            detail = str(result.get('message') or '').strip()
            message = '发布资料准备未完成' + (f'：{detail}' if detail else '（执行器未回报资料已就绪）。')
            return {'prepared': False, 'published': False, 'needs_action': False,
                    'message': message, 'raw': result}
        return {'prepared': True, 'published': False, 'needs_manual_publish': True,
                'upload_url': str(result.get('upload_url') or ''),
                'message': str(result.get('message') or ''),
                'raw': result}


class UnimplementedAdapter(PlatformAdapter):
    """未接入平台的占位适配器：全部方法抛 NotImplementedError，capabilities 明示待验收。"""
    def __init__(self, platform, display_name):
        self.platform = platform
        self.display_name = display_name

    def capabilities(self, ctx):
        return {'platform': self.platform, 'implemented': False, 'ready': False,
                'message': f'{self.display_name} 未接入/待验收：适配器尚未实现，'
                           '不返回任何模拟结果。'}


ADAPTERS = {
    'douyin': DouyinAdapter(),
    'kuaishou': UnimplementedAdapter('kuaishou', '快手'),
    'xiaohongshu': UnimplementedAdapter('xiaohongshu', '小红书'),
    'shipinhao': UnimplementedAdapter('shipinhao', '视频号'),
    'bilibili': UnimplementedAdapter('bilibili', 'B站'),
}


def get_adapter(platform):
    adapter = ADAPTERS.get(platform)
    if adapter is None:
        raise ValueError(f'未知平台：{platform!r}；支持 {sorted(ADAPTERS)}。')
    return adapter
