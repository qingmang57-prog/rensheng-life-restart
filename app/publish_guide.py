# -*- coding: utf-8 -*-
"""多平台发布引导（2026-11-17 t47）：把它平台从「预留窗口」升级为**合规人工引导 + 四件套**。

合规依据（与切片工坊 `publish/profiles/manual.py` 同源，只读参考）：
· 小红书：社区规范 4.4.3 禁机器发布；且无面向创作者的发布 API ⇒ 只能人工。
· 视频号：《微信个人账号使用规范》1.2.6（禁止第三方对界面/模块做访问、读取或控制）、
  《视频号运营规范》4.3（使用外挂，不区分自动化程度）⇒ 只能人工。
· 本包对上述两平台**没有**任何注入/填表/点击通路；抖音仍走既有的「本人扫码 + 本人确认」辅助发布。

本模块只做三件人畜无害的事（对照 manual.py 的 guide / kit_text / open_page）：
  1. 给出该平台发布页 URL（点「打开创作页」= 前端新窗口导航，等价于用户自己点）；
  2. 把四件套拼成可直接粘贴的文本（剪贴板由前端做，后端只产文本）；
  3. 给出人工步骤清单与平台规格提示（标题字数、封面比例）。
不读取平台页面、不注入文件、不填表、不点任何按钮；平台入口与步骤是**公开信息**，
所以放在本地静态配置里，不进云端提示词（不属于核心 IP）。

四件套来源＝本项目 publication_pack（release_service.prepare 的输出）：
  selected_title → 标题；caption → 描述；tags → 话题；封面文案由标题前 10 字派生（可自行精简）。
"""
import json

# 能力位：与切片工坊 CAPABILITY_BITS 同形同值；本包没有快手/ B 站自动化通路，一律 policy_off。
CAPABILITY_BITS = {
    'douyin': {'upload_file': 'on', 'autofill': 'on', 'mode': 'auto'},
    'xiaohongshu': {'upload_file': 'policy_off', 'autofill': 'policy_off', 'mode': 'manual'},
    'wechat_channels': {'upload_file': 'policy_off', 'autofill': 'policy_off', 'mode': 'manual'},
    'bilibili': {'upload_file': 'policy_off', 'autofill': 'policy_off', 'mode': 'manual'},
    'kuaishou': {'upload_file': 'policy_off', 'autofill': 'policy_off', 'mode': 'manual'},
}

# 平台公开入口 + 人工步骤 + 规格（静态配置；不进云端提示词）
GUIDES = {
    'xiaohongshu': {
        'publish_url': 'https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video',
        'reason': '小红书社区规范 4.4.3 禁止机器发布，且没有面向创作者的发布 API ⇒ 只做人工引导。',
        'title_max': 20,
        'ratio': '封面 3:4（1080×1440）竖版优先；视频竖屏 9:16',
        'steps': ['逐项复制四件套（标题 / 描述 / 话题 / 封面文案）',
                  '点「打开创作页」，用你自己的账号登录',
                  '人工上传视频、封面，粘贴四件套（标题 20 字内）',
                  '在小红书里人工点发布',
                  '回来点「我已在平台发布」登记作品链接'],
    },
    'wechat_channels': {
        'publish_url': 'https://channels.weixin.qq.com/platform/post/create',
        'reason': '《微信个人账号使用规范》1.2.6 与《视频号运营规范》4.3 明禁第三方对界面做自动化 ⇒ 只做人工引导。',
        'title_max': 0,
        'ratio': '竖屏 9:16（1080×1920）优先，也支持 16:9 / 4:3 / 9:19.5；分辨率建议 1080p 以上',
        'steps': ['逐项复制四件套',
                  '点「打开创作页」，人工扫码登录视频号助手',
                  '人工上传视频、粘贴四件套、设置封面',
                  '在视频号里人工点发布',
                  '回来点「我已在平台发布」登记作品链接'],
    },
    'bilibili': {
        'publish_url': 'https://member.bilibili.com/platform/upload/video/frame',
        'reason': '本包没有 B 站发布通路，按人工引导处理（不假装能代发）。',
        'title_max': 80,
        'ratio': '横屏 16:9（1920×1080）优先',
        'steps': ['逐项复制四件套', '点「打开创作页」，人工登录',
                  '人工上传视频与封面，粘贴四件套', '在 B 站里人工点发布',
                  '回来点「我已在平台发布」登记作品链接'],
    },
    'kuaishou': {
        'publish_url': 'https://cp.kuaishou.com/article/publish/video',
        'reason': '本包没有快手发布通路，按人工引导处理（不假装能代发）。',
        'title_max': 0,
        'ratio': '竖屏 9:16 优先',
        'steps': ['逐项复制四件套', '点「打开创作页」，人工登录',
                  '人工上传视频与封面，粘贴四件套', '在快手里人工点发布',
                  '回来点「我已在平台发布」登记作品链接'],
    },
}

MANUAL_NOTE = '本平台不接入自动化：不注入文件、不填表、不点发布，发布动作全部由你在平台完成。'
AUTO_NOTE = '抖音走「本人扫码 + 本人确认」辅助发布：不替你点发布，只在你确认后提交。'
COVER_CHARS = 10


def _name(platform):
    return {'douyin': '抖音', 'xiaohongshu': '小红书', 'wechat_channels': '视频号',
            'bilibili': 'B站', 'kuaishou': '快手'}.get(platform, platform)


def platforms():
    """平台能力清单（前端卡片用）：能力位如实标注，不猜、不冒充。"""
    items = []
    for key in ('douyin', 'xiaohongshu', 'wechat_channels', 'bilibili', 'kuaishou'):
        bits = CAPABILITY_BITS[key]
        manual = bits['mode'] == 'manual'
        items.append({
            'platform': key, 'name': _name(key), 'mode': bits['mode'],
            'upload_file': bits['upload_file'], 'autofill': bits['autofill'],
            'manual': manual,
            'label': '人工引导' if manual else '自动辅助（本人确认）',
            'note': MANUAL_NOTE if manual else AUTO_NOTE,
            'publish_url': (GUIDES.get(key) or {}).get('publish_url', ''),
            'title_max': (GUIDES.get(key) or {}).get('title_max', 0),
            'ratio': (GUIDES.get(key) or {}).get('ratio', ''),
        })
    return {'ok': True, 'count': len(items), 'items': items}


def guide(platform):
    key = str(platform or '').strip()
    bits = CAPABILITY_BITS.get(key)
    if not bits:
        return {'ok': False, 'error': 'platform_unknown',
                'message': '平台未知：%s（不回落抖音）' % (platform or '')}
    if bits['mode'] != 'manual':
        return {'ok': True, 'platform': key, 'name': _name(key), 'mode': 'auto',
                'automation': True, 'note': AUTO_NOTE,
                'publish_url': (GUIDES.get(key) or {}).get('publish_url', '')}
    g = GUIDES[key]
    return {'ok': True, 'platform': key, 'name': _name(key), 'mode': 'manual',
            'automation': False, 'publish_url': g['publish_url'], 'reason': g['reason'],
            'steps': list(g['steps']), 'title_max': g['title_max'], 'ratio': g['ratio'],
            'cover_chars': COVER_CHARS, 'note': MANUAL_NOTE}


def kit(prepared, platform):
    """四件套拼粘贴文本（前端复制；后端只产文本）。prepared＝release_service.prepare 的输出。"""
    key = str(platform or '').strip()
    if key not in CAPABILITY_BITS:
        return {'ok': False, 'error': 'platform_unknown', 'message': '平台未知'}
    g = GUIDES.get(key) or {}
    title = str((prepared or {}).get('selected_title') or '')
    tmax = int(g.get('title_max') or 0)
    truncated = bool(tmax and len(title) > tmax)
    if truncated:
        title = title[:tmax]
    desc = str((prepared or {}).get('caption') or '')
    tags = ' '.join('#' + str(t).lstrip('#') for t in ((prepared or {}).get('tags') or []))
    cover = str((prepared or {}).get('selected_title') or '')[:COVER_CHARS]
    blocks = ['【标题】', title, '', '【描述】', desc, '', '【话题】', tags, '',
              '【封面文案】', cover]
    return {'ok': True, 'platform': key, 'name': _name(key),
            'text': '\n'.join(blocks).strip(),
            'fields': {'title': title, 'desc': desc, 'tags': tags, 'cover': cover},
            'title_max': tmax, 'title_truncated': truncated,
            'ratio': g.get('ratio', ''), 'mode': CAPABILITY_BITS[key]['mode'],
            'note': '标题/封面比例按平台要求，人工核对后再发；本工具不代发。'}


def open_page(platform):
    """只导航：返回该平台发布页 URL（前端新窗口打开），不做任何注入。"""
    g = guide(platform)
    if not g.get('ok'):
        return g
    return {'ok': True, 'platform': g['platform'], 'url': g.get('publish_url', ''),
            'mode': g['mode'], 'note': '只打开创作页；登录、上传、粘贴、发布都由你在平台完成。'}


if __name__ == '__main__':
    print(json.dumps(platforms(), ensure_ascii=False, indent=1))
