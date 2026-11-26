# -*- coding: utf-8 -*-
"""知识卡（需求单 H，2026-11-25）：用户在输入框里描述时，基于他自己的认知实时出一张卡。

两段式（方案 A′，用户选定「自动精修默认开」）：
- 卡坯：零模型、确定性，本地就算。标题取首句；主张 = 原文；边界 = 原文里带条件的句子；
  归类看「相近的已有认知」里最多的分类，没有相近就归「随手记」。**主张和边界永远不经过模型。**
- 精修：模型只给标题和分类（在六类里选），不碰主张。同一段文字只调一次（结果缓存）。
- 存为图片：PIL + 随包字体，落到 artifacts，走现成的 /api/artifacts/。

对照的成熟做法：flomo 的卡片就是记录本身（不另造结构）；微信输入法「问 AI」结果可一键成图；
MD2Card 出图时才渲染。所以「存下来」直接走现有 /api/knowledge，不另建一套存储。
"""
import hashlib
import re
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import content_index
import providers

MAX_TEXT = 4000          # 进卡的最大原文
CLAIM_MAX = 1200         # 存进认知时的主张上限（/api/knowledge 自己还有 12000 的上限）
TITLE_MAX = 30           # 卡坯标题最多 30 字
CACHE_TTL = 7 * 86400    # 精修结果缓存 7 天
CACHE_MAX = 500          # 最多缓存这么多段文字
FONT = Path(__file__).resolve().parent / 'assets' / 'fonts' / 'HarmonyOS_Sans_SC_Bold.ttf'
# 原文里带这些词的句子，说明用户自己说了「这条在什么范围内成立」
CONDITION_WORDS = ('如果', '只有', '除非', '只要', '一旦', '否则', '取决于', '得看', '的前提', '的时候才')

_cache = {}
_lock = threading.Lock()

SYSTEM = (
    '你在帮用户给他自己写下的一段话起标题、选分类。\n'
    '硬规则：\n'
    '1. 标题不超过 20 个字，用用户自己的说法，不要书面化、不要加用户没说的概念。\n'
    '2. category 只能从给定的分类里原样选一个；判断不了就选「随手记」。\n'
    '3. 不要改写、不要评价用户的话，不要补充内容。\n'
    '只输出 JSON：{"title": "标题", "category": "分类名"}'
)


def _sentences(text):
    return [s.strip() for s in re.split(r'[。！？；\n]+', text) if s.strip()]


def text_hash(text):
    return hashlib.sha1(str(text or '').strip().encode('utf-8')).hexdigest()[:16]


def draft(store, text):
    """卡坯：零模型。返回 {text_hash, title, claim, boundary, category, related, refined}。"""
    text = str(text or '').strip()[:MAX_TEXT]
    if len(text) < 20:
        raise ValueError('再多写几句（20 字以上），我才能帮你整理成一张卡。')
    sentences = _sentences(text)
    first = sentences[0] if sentences else text
    title = first if len(first) <= TITLE_MAX else first[:TITLE_MAX] + '…'
    boundary = ''
    for sentence in sentences[1:]:
        if len(sentence) <= 120 and any(word in sentence for word in CONDITION_WORDS):
            boundary = sentence
            break
    # 「相近的已有认知」用产品自己的检索（知识库搜索同一套：词频加权、要求至少命中 2 个词、自带排除示例库）。
    related = []
    try:
        for row in (content_index.search(store, text, 12) or {}).get('items') or []:
            if row.get('source_kind') != 'knowledge':
                continue
            related.append({'id': row.get('source_id'), 'title': str(row.get('title') or '')[:60],
                            'category': row.get('category') or ''})
            if len(related) >= 3:
                break
    except Exception:
        related = []
    votes = {}
    for row in related:
        votes[row['category']] = votes.get(row['category'], 0) + 1
    category, legacy = content_index.normalize_category(max(votes, key=votes.get) if votes else '随手记')
    return {'text_hash': text_hash(text), 'title': title, 'claim': text[:CLAIM_MAX], 'boundary': boundary,
            'category': category, 'category_legacy': legacy, 'related': related, 'refined': False}


def refine(store, text, card):
    """精修：模型只给标题和分类。同一段文字命中缓存时不调模型（cached=True）。"""
    key = card['text_hash']
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit['at'] < CACHE_TTL:
            return dict(card, **hit['data'], refined=True, cached=True)
    if not providers.available()['llm']:
        raise providers.ProviderMissing('写稿模型还没配置或云端没登录。这张卡坯可以直接存下来；连上后再点「精修」。')
    payload = {'text': str(text or '').strip()[:2000], 'categories': list(content_index.TARGET_CATEGORIES),
               'draft_title': card['title'], 'related_titles': [row['title'] for row in card['related']]}
    data = providers.llm_json(SYSTEM, payload, tag='knowledge_card:refine')
    data = data if isinstance(data, dict) else {}
    title = str(data.get('title') or '').strip()
    category = str(data.get('category') or '').strip()
    # 硬约束：标题限长；分类必须在六类里。主张和边界不从模型取。
    result = {}
    if title and len(title) <= 40:
        result['title'] = title
    if category in content_index.TARGET_CATEGORIES:
        result['category'] = category
        result['category_legacy'] = ''
    if not result:
        raise RuntimeError('模型这次没给出合用的标题，卡坯照常可用，可以再点一次「精修」。')
    note = '' if ('title' in result and 'category' in result) else '模型只给了一部分，其余沿用卡坯。'
    with _lock:
        if len(_cache) >= CACHE_MAX:
            _cache.clear()
        _cache[key] = {'at': now, 'data': result}
    return dict(card, **result, refined=True, cached=False, refine_note=note)


def _wrap(draw, text, font, max_width, max_lines):
    """按像素宽度折行（中文逐字排）；超过行数上限时最后一行加省略号。"""
    lines, current = [], ''
    for char in str(text or ''):
        if char == '\n':
            lines.append(current)
            current = ''
            continue
        if draw.textlength(current + char, font=font) <= max_width or not current:
            current += char
        else:
            lines.append(current)
            current = char
        if len(lines) > max_lines:
            break
    if current and len(lines) <= max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + '…', font=font) > max_width:
            last = last[:-1]
        lines[-1] = last + '…'
    return lines or ['']


CARD_WIDTH = 1080
PAD = 72


def render_png(card, out_dir):
    """把一张卡画成 1080 宽的 PNG（高度随内容），返回文件路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    category = card.get('category') if card.get('category') in content_index.TARGET_CATEGORIES else '随手记'
    title = str(card.get('title') or '').strip()[:60] or '知识卡'
    claim = str(card.get('claim') or '').strip()[:CLAIM_MAX]
    boundary = str(card.get('boundary') or '').strip()[:300]
    related = card.get('related_count')
    related = related if isinstance(related, int) and related > 0 else 0
    if not claim:
        raise ValueError('这张卡没有正文，画不出来。')

    font_title = ImageFont.truetype(str(FONT), 60)
    font_body = ImageFont.truetype(str(FONT), 42)
    font_small = ImageFont.truetype(str(FONT), 30)
    measure = ImageDraw.Draw(Image.new('RGB', (8, 8)))
    inner = CARD_WIDTH - PAD * 2
    title_lines = _wrap(measure, title, font_title, inner, 2)
    claim_lines = _wrap(measure, claim, font_body, inner, 16)
    boundary_lines = _wrap(measure, '边界：' + boundary, font_body, inner, 4) if boundary else []
    foot = '关联已有认知 %d 条 · 人生重启系统' % related if related else '人生重启系统'

    # 高度 = 各段高度之和，和下面绘制的顺序、间距一一对应
    height = PAD + 64 + 28 + len(title_lines) * 88 + 36 + len(claim_lines) * 64
    if boundary_lines:
        height += 28 + 2 + 28 + len(boundary_lines) * 64
    height += 56 + 2 + 20 + 44 + PAD
    height = max(height, 540)

    image = Image.new('RGB', (CARD_WIDTH, height), (250, 248, 243))
    draw = ImageDraw.Draw(image)
    y = PAD
    # 分类标签
    chip_text = '【' + category + '】'
    chip_w = int(draw.textlength(chip_text, font=font_small)) + 44
    draw.rounded_rectangle((PAD, y, PAD + chip_w, y + 56), radius=28, fill=(230, 197, 106))
    draw.text((PAD + 22, y + 11), chip_text, font=font_small, fill=(40, 34, 20))
    y += 64 + 28
    for line in title_lines:
        draw.text((PAD, y), line, font=font_title, fill=(28, 24, 18))
        y += 88
    y += 36
    for line in claim_lines:
        draw.text((PAD, y), line, font=font_body, fill=(52, 48, 42))
        y += 64
    if boundary_lines:
        y += 28
        draw.line((PAD, y, CARD_WIDTH - PAD, y), fill=(214, 206, 188), width=2)
        y += 28
        for line in boundary_lines:
            draw.text((PAD, y), line, font=font_body, fill=(110, 100, 84))
            y += 64
    y += 56
    draw.line((PAD, y, CARD_WIDTH - PAD, y), fill=(214, 206, 188), width=2)
    draw.text((PAD, y + 20), foot, font=font_small, fill=(140, 130, 112))

    safe = re.sub(r'[\\/:*?"<>|\s]+', '', title)[:12] or '知识卡'
    target = out_dir / ('知识卡-%s-%s.png' % (safe, time.strftime('%Y%m%d-%H%M%S')))
    image.save(str(target), 'PNG')
    return target
