# -*- coding: utf-8 -*-
"""12 题原话与经历 → 素材层。

- 每题答案存一条 voice_sample：逐字保留本人原话，带题号、题面、章节和时间；
- 答案里有叙事线（我那时候/后来/有一次…）的，另存一条 personal_story，标成故事素材；
- 洗稿时按与选题/原文的字面重合挑相关素材（最多 5 条）；挑不到就不塞，
  让提示词老实写【待补：此处需要你的真实经历】，绝不拿模型编的经历凑数。
"""
import re

from db import now

STORY_MARKS = ('我那时候', '那年', '后来', '当时', '我记得', '我开', '我做过',
               '我第一次', '有一次', '最后我', '我用了', '结果我')
STOP_GRAMS = {'我们', '你们', '他们', '自己', '时候', '一个', '这个', '那个', '就是',
              '因为', '所以', '但是', '没有', '可以', '什么', '怎么', '这样', '那样',
              '不是', '还是', '已经', '现在', '然后', '如果', '觉得', '知道'}
CJK_RE = re.compile(r'[^\u4e00-\u9fa5A-Za-z0-9]')


def _clean(text):
    return CJK_RE.sub('', str(text or ''))


def _grams(text):
    text = _clean(text)
    grams = set()
    for size in (2, 3):
        for index in range(0, max(0, len(text) - size + 1)):
            gram = text[index:index + size]
            if size == 2 and gram in STOP_GRAMS:
                continue
            grams.add(gram)
    return grams


def is_story(text):
    return (len(_clean(text)) >= 30 and any(mark in str(text) for mark in STORY_MARKS))


def sync_answers(store, rec, questions):
    """把 flow 里的 12 题答案同步成素材；重复保存只更新变化的题，不重复入库。"""
    answers = rec.get('answers') if isinstance(rec.get('answers'), dict) else {}
    meta = rec.get('answer_meta') if isinstance(rec.get('answer_meta'), dict) else {}
    written = 0
    for question in questions:
        qid = question['id']
        text = str(answers.get(qid) or '').strip()
        if not text:
            continue
        existing = store.get('voice_sample', 'vs_' + qid)
        if existing and str(existing.get('text') or '') == text:
            continue
        store.put('voice_sample', {
            'qid': qid, 'question': question['text'], 'section': question['section'],
            'text': text, 'raw_text': text,
            'captured_via': str((meta.get(qid) or {}).get('via') or 'text')[:20],
            'origin': '12 题建档', 'created_at': (existing or {}).get('created_at') or now(),
            'updated_at': now(),
        }, 'vs_' + qid)
        written += 1
        if is_story(text):
            store.put('personal_story', {
                'qid': qid, 'question': question['text'], 'section': question['section'],
                'excerpt': text[:300], 'text': text, 'origin': '12 题建档',
                'created_at': (store.get('personal_story', 'ps_' + qid) or {}).get('created_at') or now(),
                'updated_at': now(),
            }, 'ps_' + qid)
    return written


def count(store):
    return {'voice': len(store.list('voice_sample', 200)),
            'story': len(store.list('personal_story', 200))}


def pick(store, query, limit=5):
    """按字面重合挑素材；宁缺毋滥，挑不到就返回空。"""
    query_grams = _grams(query)
    if not query_grams:
        return []
    scored = {}
    for kind in ('personal_story', 'voice_sample'):
        for item in store.list(kind, 200):
            qid = item.get('qid') or item['id']
            grams = _grams(str(item.get('text') or '') + str(item.get('question') or ''))
            overlap = query_grams & grams
            score = len(overlap)
            if score < 2:
                continue
            current = scored.get(qid)
            # 同一题的故事素材优先，其次比重合度
            rank = (1 if kind == 'personal_story' else 0, score)
            if not current or rank > current[0]:
                scored[qid] = (rank, item)
    ordered = sorted(scored.values(), key=lambda pair: (-pair[0][0], -pair[0][1]))
    result = []
    for _rank, item in ordered[:limit]:
        text = str(item.get('text') or '')
        result.append({'id': item['id'], 'qid': item.get('qid'),
                       'kind': 'personal_story' if item['id'].startswith('ps_') else 'voice_sample',
                       'question': item.get('question'),
                       'excerpt': text[:120], 'text': text[:300]})
    return result


def context(store, query, limit=5):
    items = pick(store, query, limit)
    return {'items': items, 'ids': [x['id'] for x in items], 'has': bool(items),
            'counts': count(store)}
