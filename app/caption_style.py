# -*- coding: utf-8 -*-
"""字幕字体与样式注册表（2026-11-02，自切片工坊 jy_bridge/font_registry.py 移植机制）。

机制差异说明：切片工坊走剪映云字体（pyJianYingDraft FontType 枚举），本工作台
字幕由 ffmpeg+libass 烧录（media.render_packaged 生成 ASS），因此这里登记的是
**本地字体文件**：非默认字体从 app/assets/fonts/ 目录经 ass 滤镜的 fontsdir 加载，
libass 按family 名匹配（2026-11-02 实测 5 款全部命中、无静默回退）。

兜底口径（沿用切片工坊 2026-10-15 的约定）：key 不认识 / 字体文件缺失 →
安静回落默认微软雅黑（打一条 [warn]），绝不抛错中断导出。

增强样式三项（移植自切片工坊 product_to_draft.py 2026-11-02 Top1+2+3，
数据源：_字幕预设解析-20261102/二次挖掘-109预设可抄规律-20261102.md）：
  Top1 阴影套餐：黑 / alpha 0.70 / distance 5（剪映预设高度模板化参数）；
  Top2 描边随字号：预设「描边/字号」中位 1.78（剪映 UI 空间）→ 按 ASS 像素
      空间等比例适配为字号 × 0.07（下限 2px），方向为「细描边 + 柔影」组合；
  Top3 关键词放大 1.5× + 奶油黄强调 #FFF4C1（预设公式 2「白字打底 +
      奶油黄强调 + 放大 1.5 倍左右」）。
"""
from pathlib import Path
from typing import Optional, Tuple

FONT_DIR = Path(__file__).resolve().parent / 'assets' / 'fonts'

#: 默认字体＝既有行为（微软雅黑，系统字体，无需 fontsdir）。
DEFAULT_FONT_KEY = 'default'

#: key -> {label, family(ASS Fontname), file(app/assets/fonts 下文件名), license}。
#: 全部为免费可商用字体，文件真实存在于本机（2026-11-02 核对）。
FONT_TABLE = {
    'default': {
        'label': '默认（微软雅黑）',
        'family': 'Microsoft YaHei',
        'file': None,
        'license': '系统自带（沿用既有默认行为）',
    },
    'harmony': {
        'label': 'HarmonyOS Sans 粗体',
        'family': 'HarmonyOS Sans SC',
        'file': 'HarmonyOS_Sans_SC_Bold.ttf',
        'license': 'HarmonyOS Sans：华为免费商用授权',
    },
    'noto': {
        'label': '思源黑体 粗体',
        'family': 'Noto Sans SC',
        'file': 'NotoSansSC-Bold.otf',
        'license': 'Noto Sans SC：SIL OFL 1.1（Adobe/Google）',
    },
    'serif': {
        'label': '思源宋体 Heavy',
        'family': 'Source Han Serif SC Heavy',
        'file': 'SourceHanSerifSC-Heavy.ttf',
        'license': '思源宋体 Source Han Serif：SIL OFL 1.1',
    },
    'smiley': {
        'label': '得意黑',
        'family': 'Smiley Sans Oblique',
        'file': 'SmileySans-Oblique.otf',
        'license': 'Smiley Sans 得意黑：SIL OFL 1.1（atelier-anchor/smiley-sans v2.0.1）',
    },
}

# ---- 增强样式常数（ASS 像素空间适配值，见模块 docstring 出处） ----
SHADOW_ALPHA_HEX = 0x4D        # Top1：黑色阴影 70% 不透明（ASS alpha=透明度，0x4D≈30% 透明）
SHADOW_DISTANCE_RATIO = 5 / 27  # Top1：默认字号 27px 时阴影 ≈5px，随字号缩放
OUTLINE_RATIO = 0.07           # Top2：细描边 = 字号 × 0.07（剪映 1.78 UI 比例的像素适配）
OUTLINE_MIN = 2
HL_SCALE = 1.5                 # Top3：关键词放大倍率
HL_COLOR_ASS = '&HC1F4FF&'     # Top3：奶油黄 #FFF4C1 的 ASS BGR 写法

#: 常用字幕预设（克制口径：6 套够用，不搞花字军备，见竞品差距矩阵 2026-11-03）。
#: scale=字号倍率，color=主字幕色 RGB hex；enhanced 沿用增强三项。
PRESETS = {
    'classic': {'label': '经典白字', 'font': 'default', 'enhanced': False, 'scale': 1.0, 'color': 'FFFFFF'},
    'cream': {'label': '奶油黄强调', 'font': 'harmony', 'enhanced': True, 'scale': 1.0, 'color': 'FFFFFF'},
    'big': {'label': '大白字', 'font': 'noto', 'enhanced': False, 'scale': 1.25, 'color': 'FFFFFF'},
    'gold': {'label': '金黄醒目', 'font': 'noto', 'enhanced': True, 'scale': 1.1, 'color': 'FFD700'},
    'serif': {'label': '宋体重墨', 'font': 'serif', 'enhanced': True, 'scale': 1.0, 'color': 'FFFFFF'},
    'smiley': {'label': '得意黑活泼', 'font': 'smiley', 'enhanced': False, 'scale': 1.1, 'color': 'FFFFFF'},
}


def resolve(key: Optional[str]) -> Tuple[str, Optional[str]]:
    """key → (ASS family 名, fontsdir 或 None)。

    未知 key / 字体文件缺失 → 默认微软雅黑（安静回落，仅 [warn]），不抛错。
    """
    k = (key or '').strip()
    entry = FONT_TABLE.get(k)
    if k and k not in FONT_TABLE:
        print('[warn] 未知字幕字体 key「%s」，回落默认字体（可选：%s）'
              % (k, '/'.join(FONT_TABLE)))
        entry = None
    if not entry:
        entry = FONT_TABLE[DEFAULT_FONT_KEY]
    font_file = entry.get('file')
    if font_file and not (FONT_DIR / font_file).is_file():
        print('[warn] 字体文件缺失「%s」，回落默认字体' % font_file)
        entry = FONT_TABLE[DEFAULT_FONT_KEY]
        font_file = entry.get('file')
    fontsdir = str(FONT_DIR) if font_file else None
    return entry['family'], fontsdir


def choices():
    """前端下拉用清单：[{key,label,license}...]，顺序即注册表顺序。"""
    return [{'key': k, 'label': v['label'], 'license': v['license']}
            for k, v in FONT_TABLE.items()]


def preset_choices():
    """前端预设下拉用清单：[{key,label,font,enhanced}...]，供选中时同步字体/增强开关。"""
    return [{'key': k, 'label': v['label'], 'font': v['font'], 'enhanced': v['enhanced']}
            for k, v in PRESETS.items()]


def sanitize(data) -> Optional[dict]:
    """请求体 → 规范化样式；空值返回 None（＝完全保持既有默认行为）。"""
    if not isinstance(data, dict) or not data:
        return None
    preset = data.get('preset')
    if preset and preset not in PRESETS:
        print('[warn] 未知字幕预设「%s」，忽略预设（可选：%s）' % (preset, '/'.join(PRESETS)))
        preset = None
    base = PRESETS.get(preset or '', {})
    font = data.get('font') or base.get('font')
    if not isinstance(font, str) or font not in FONT_TABLE:
        if font:
            print('[warn] 请求中的字幕字体「%s」无效，回落默认' % font)
        font = DEFAULT_FONT_KEY
    enhanced = bool(data['enhanced']) if 'enhanced' in data else bool(base.get('enhanced'))
    out = {'font': font, 'enhanced': enhanced,
           'scale': base.get('scale', 1.0), 'color': base.get('color', 'FFFFFF')}
    if preset:
        out['preset'] = preset
    return out
