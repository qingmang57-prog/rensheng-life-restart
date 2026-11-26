"""Director pass (导演三件套): mood arc, pacing design and visual pre-placement,
generated alongside the confirmed script. Each item regenerates independently and every
visual suggestion must quote the actual script text. Falls back to rule suggestions when
no LLM is configured (mirrors media.director)."""
import re

import media
import prompt_store
import providers

MOODS = ('温情陪伴', '燃', '沉郁叙事', '轻快')
ITEMS = ('mood_arc', 'pacing', 'visual_plan')


def _validate(item, data, parts):
    by_id = {p['id']: p for p in parts}
    if item == 'mood_arc':
        arc = data.get('mood_arc')
        if not isinstance(arc, dict) or arc.get('overall') not in MOODS:
            raise ValueError('情绪弧线格式无效。')
        segs = arc.get('segments')
        if not isinstance(segs, list):
            raise ValueError('情绪分段格式无效。')
        clean = []
        for s in segs:
            if not isinstance(s, dict) or s.get('part_id') not in by_id:
                continue
            try:
                intensity = int(s.get('intensity', 3))
            except (TypeError, ValueError):
                intensity = 3
            clean.append({'part_id': s['part_id'], 'mood': str(s.get('mood', arc['overall']))[:20],
                          'intensity': max(1, min(5, intensity)), 'reason': str(s.get('reason', ''))[:300]})
        if len(clean) < 3:
            raise ValueError('情绪分段至少要覆盖三段稿本。')
        return {'overall': arc['overall'], 'segments': clean}
    if item == 'pacing':
        rows = data.get('pacing')
        if not isinstance(rows, list) or not rows:
            raise ValueError('节奏设计格式无效。')
        clean = []
        for r in rows:
            if not isinstance(r, dict) or r.get('part_id') not in by_id:
                continue
            try:
                pause = int(r.get('pause_before_ms', 0))
            except (TypeError, ValueError):
                pause = 0
            clean.append({'part_id': r['part_id'],
                          'pace': r.get('pace') if r.get('pace') in ('收紧', '标准', '留白') else '标准',
                          'pause_before_ms': max(0, min(1500, pause)),
                          'reason': str(r.get('reason', ''))[:300]})
        return clean
    plan = data.get('visual_plan')
    if not isinstance(plan, dict):
        raise ValueError('视觉预埋格式无效。')
    cards, moments = [], []
    for c in plan.get('quote_cards') or []:
        if not isinstance(c, dict) or c.get('part_id') not in by_id:
            continue
        quote = str(c.get('quote', '')).strip()
        if quote and quote in by_id[c['part_id']]['text']:
            cards.append({'part_id': c['part_id'], 'quote': quote[:60], 'reason': str(c.get('reason', ''))[:300]})
    for k in plan.get('keyword_moments') or []:
        if not isinstance(k, dict) or k.get('part_id') not in by_id:
            continue
        kw = str(k.get('keyword', '')).strip()
        if kw and len(kw) <= 6 and kw in by_id[k['part_id']]['text']:
            moments.append({'part_id': k['part_id'], 'keyword': kw, 'reason': str(k.get('reason', ''))[:300]})
    if not cards:
        raise ValueError('值得转发的卡建议至少要有一条且必须逐字引用稿本。')
    return {'quote_cards': cards[:6], 'keyword_moments': moments[:5]}


def rule_fallback(parts):
    """Same shape as the LLM brief, sourced from media.director rules; reasons are rule-based."""
    rules = media.director(parts)
    quote_parts=[p for p in parts if p.get('role') == 'quote' and p.get('text','').strip()]
    cards=[{'part_id': r['block_id'], 'quote': r['overlay'], 'reason': r['reason']}
           for r in rules if r['visual'] == 'quote_emphasis']
    if not cards and quote_parts:
        cards=[{'part_id': quote_parts[0]['id'], 'quote': quote_parts[0]['text'][:28],
                'reason': '值得被转发的一句话默认上卡（规则兜底）。'}]
    return {
        'mood_arc': {'overall': '温情陪伴', 'segments': [
            {'part_id': p['id'], 'mood': '温情陪伴', 'intensity': 3,
             'reason': '规则默认；配置模型后可生成真实情绪弧线。'} for p in parts], 'method': 'rule_fallback'},
        'pacing': [{'part_id': p['id'], 'pace': '标准', 'pause_before_ms': 400 if p['role'] == 'quote' else 0,
                    'reason': '规则默认；值得被转发的一句话前默认留气口。'} for p in parts],
        'visual_plan': {'quote_cards': cards,
                        'keyword_moments': [], 'method': 'rule_fallback'},
    }


def generate_item(item, parts):
    """Generate one brief item via LLM with validation. Caller merges into the stored brief."""
    if item not in ITEMS:
        raise ValueError('导演三件套条目无效。')
    answer = providers.llm_json(prompt_store.get('director.' + item), {'parts': [{'id': p['id'], 'role': p['role'], 'label': p['label'],
                                                           'text': p['text'][:800]} for p in parts]})
    return _validate(item, answer, parts)


def ensure_brief(store, script_version):
    key = 'director_brief_' + script_version['id']
    brief = store.get('director_brief', key)
    if brief is None:
        brief = store.put('director_brief', {'script_version_id': script_version['id'],
                                             'project_id': script_version['project_id'],
                                             **rule_fallback(script_version['parts'])})
    return brief
