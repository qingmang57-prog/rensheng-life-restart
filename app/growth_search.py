"""Read-only cognitive search over growth answers, fragments, goals and knowledge."""
import json
import re
from pathlib import Path

from growth_service import Growth
from growth_ask import PILLAR_NAMES, active_pillars, questions as growth_questions

ASSETS = Path(__file__).resolve().parent / 'assets'
ALIAS_FILE = ASSETS / 'growth_search_aliases.json'
MAX_RESULTS = 50
MAX_QUESTIONS = 3


def load_aliases():
    return json.loads(ALIAS_FILE.read_text(encoding='utf-8'))


def alias_topics():
    return (load_aliases().get('topics') or [])


def normalize(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip().lower()


def query_terms(raw):
    aliases = load_aliases()
    stopwords = set(aliases.get('stopwords') or [])
    query = normalize(raw)
    if not query:
        return [], [], []
    topic = None
    terms = [query]
    for item in alias_topics():
        labels = [item.get('label', '')] + list(item.get('terms') or [])
        if any(normalize(x) and normalize(x) in query for x in labels):
            topic = item
            terms.extend(normalize(x) for x in labels if normalize(x))
            break
    tokens = [x for x in re.split(r'[\s,，、/]+', query) if x and x not in stopwords]
    return topic, sorted(set(terms)), tokens


def matches(text, terms, tokens):
    value = normalize(text)
    if not value:
        return False
    if any(term and term in value for term in terms):
        return True
    return bool(tokens) and all(token in value for token in tokens)


def excerpt(text, terms, size=180):
    value = re.sub(r'\s+', ' ', str(text or '')).strip()
    low = value.lower()
    position = -1
    for term in terms:
        position = low.find(term)
        if position >= 0:
            break
    if position < 0:
        return value[:size]
    start = max(0, position - 45)
    return ('…' if start else '') + value[start:start + size] + ('…' if start + size < len(value) else '')


def answer_record(store):
    return {a['question_id']: a for a in Growth(store).all('growth_answer')}


def knowledge_items(store):
    return [x for x in store.list('knowledge', 10000) if x.get('scope') != 'example']


def goal_paths(goals):
    def path(key):
        parts, seen = [], set()
        while key and key not in seen:
            seen.add(key)
            goal = next((g for g in goals if g['id'] == key), None)
            if not goal:
                break
            parts.append(goal['title'])
            key = goal.get('parent_id')
        return ' / '.join(reversed(parts))

    return path


def search(store, raw, include_archived=False):
    topic, terms, tokens = query_terms(raw)
    if not terms and not tokens:
        return {'query': str(raw or ''), 'results': [], 'groups': [], 'questions': [], 'stats': {}, 'note': '请输入要找的主题，例如：金钱、创业、信任。'}
    growth = Growth(store)
    goals = growth.all('growth_goal')
    fragments = growth.all('growth_fragment')
    answer_items = growth.all('growth_answer')
    answers = {a['question_id']: a for a in answer_items}
    answer_ids = {a['id'] for a in answer_items}
    knowledge = knowledge_items(store)
    if not include_archived:
        fragments = [f for f in fragments if not f.get('archived')]
    path_of = goal_paths(goals)

    # Group projections back to their source: answer or original knowledge.
    by_answer = {}
    by_knowledge = {}
    standalone = []
    for fragment in fragments:
        origin_answer = fragment.get('origin_answer_id')
        origin_knowledge = fragment.get('origin_knowledge_id')
        if origin_answer and origin_answer in answer_ids:
            by_answer.setdefault(origin_answer, []).append(fragment)
        elif origin_knowledge:
            by_knowledge.setdefault(origin_knowledge, []).append(fragment)
        else:
            standalone.append(fragment)

    results = []
    for key, record in answers.items():
        if record.get('status') != 'answered':
            continue
        snapshot = record.get('question_snapshot') or {}
        body = ' '.join(str(x or '') for x in
                        [snapshot.get('text'), record.get('text'), *(p.get('question', '') + ' ' + p.get('answer', '') for p in record.get('probes') or []), snapshot.get('pillar'), *(snapshot.get('tags') or [])])
        if matches(body, terms, tokens):
            fragments_for_answer = by_answer.get(record['id'], [])
            goal_ids = []
            for fragment in fragments_for_answer:
                goal_ids.extend(fragment.get('goal_ids') or [])
            results.append({
                'id': record['id'], 'kind': 'answer', 'title': snapshot.get('text') or key,
                'excerpt': excerpt(record.get('text'), terms), 'pillar': snapshot.get('pillar'),
                'goal_ids': sorted(set(goal_ids)), 'fragment_ids': [f['id'] for f in fragments_for_answer],
                'status': record.get('status'), 'evidence': {'question': snapshot.get('text'), 'answer': record.get('text'),
                                                             'probes': record.get('probes') or []},
                'boundary': '', 'endorsement': 'personal', 'matched': '本人回答',
            })

    knowledge_lookup = {x['id']: x for x in knowledge}
    for item in knowledge:
        body = ' '.join(str(x or '') for x in [item.get('title'), item.get('claim'), item.get('body'), item.get('boundary'), item.get('category')])
        if not matches(body, terms, tokens):
            continue
        fragments_for_knowledge = by_knowledge.get(item['id'], [])
        goal_ids = []
        for fragment in fragments_for_knowledge:
            goal_ids.extend(fragment.get('goal_ids') or [])
        results.append({
            'id': item['id'], 'kind': 'knowledge', 'title': item.get('title'),
            'excerpt': excerpt(item.get('claim') or item.get('body'), terms),
            'boundary': excerpt(item.get('boundary'), terms) or '尚未整理',
            'goal_ids': sorted(set(goal_ids)), 'fragment_ids': [f['id'] for f in fragments_for_knowledge],
            'endorsement': item.get('endorsement'), 'category': item.get('category'),
            'evidence': {'knowledge_id': item['id'], 'claim': item.get('claim')}, 'matched': '认知库',
        })

    for fragment in standalone:
        body = ' '.join(str(x or '') for x in [fragment.get('title'), fragment.get('text'), fragment.get('source_url')])
        if not matches(body, terms, tokens):
            continue
        results.append({
            'id': fragment['id'], 'kind': 'fragment', 'title': fragment.get('title'),
            'excerpt': excerpt(fragment.get('text'), terms), 'boundary': '尚未整理',
            'goal_ids': fragment.get('goal_ids') or [], 'endorsement': fragment.get('endorsement'),
            'evidence': {'fragment_id': fragment['id'], 'text': fragment.get('text')}, 'matched': '成长碎片',
        })

    # Goal hits only add context; they do not duplicate content cards.
    goal_hits = [g for g in goals if matches(' '.join(str(x or '') for x in [g.get('title'), g.get('description')]), terms, tokens)
                 and (include_archived or not g.get('archived'))]

    priority = {'answer': 0, 'knowledge': 1, 'fragment': 2}
    results.sort(key=lambda x: (priority.get(x['kind'], 3), 0 if x.get('endorsement') in ('personal', 'accepted') else 1, x['id']))
    results = results[:MAX_RESULTS]
    groups = []
    pillar_ids = active_pillars(goals)
    for index, name in enumerate(PILLAR_NAMES):
        pillar_id = pillar_ids[index] if index < len(pillar_ids) else ''
        scope = set(growth.descendants(goals, pillar_id)) if pillar_id else set()
        items = [r for r in results if scope.intersection(r.get('goal_ids') or [])]
        if items:
            groups.append({'title': name + '支柱', 'pillar': name, 'results': items})
    placed = {r['id'] for group in groups for r in group['results']}
    others = [r for r in results if r['id'] not in placed]
    if others:
        groups.append({'title': '待归位 / 跨支柱', 'pillar': '', 'results': others})

    questions = []
    existing = answers
    question_items = growth_questions()
    ids = []
    if topic:
        ids = list(topic.get('question_ids') or [])
    else:
        for item in question_items:
            body = item.get('text', '') + ' ' + ' '.join(item.get('tags') or [])
            if matches(body, terms, tokens):
                ids.append(item['id'])
    for question_id in ids:
        record = existing.get(question_id)
        if record and record.get('status') in ('answered', 'empty'):
            continue
        for item in question_items:
            if item['id'] == question_id:
                questions.append({k: item.get(k) for k in ('id', 'text', 'pillar', 'level', 'tags')})
                break
        if len(questions) >= MAX_QUESTIONS:
            break

    return {
        'query': str(raw or ''), 'topic': topic.get('label') if topic else '',
        'terms': terms, 'results': results, 'groups': groups, 'questions': questions[:MAX_QUESTIONS],
        'goal_hits': [{'id': g['id'], 'title': g['title'], 'path': path_of(g['id'])} for g in goal_hits[:10]],
        'stats': {'answers': sum(r['kind'] == 'answer' for r in results), 'knowledge': sum(r['kind'] == 'knowledge' for r in results),
                  'fragments': sum(r['kind'] == 'fragment' for r in results), 'goals': len(goal_hits)},
        'note': '搜索只读；结果可点回原始回答或认知，不替你判断。',
    }


def timeline(store):
    growth = Growth(store)
    answers = growth.all('growth_answer')
    rows = []
    for record in answers:
        if record.get('status') != 'answered':
            continue
        snapshot = record.get('question_snapshot') or {}
        rows.append({'id': record['id'], 'kind': 'answer', 'at': record.get('answered_at') or record.get('created_at'),
                     'title': snapshot.get('text') or record['question_id'], 'text': record.get('text'),
                     'pillar': snapshot.get('pillar'), 'fragment_id': record.get('fragment_id')})
    rows.sort(key=lambda x: (x.get('at') or '', x['id']))
    return {'items': rows, 'note': '时间线只按真实记录时间排序，不生成评价。'}


def route(handler, method, bits, query, store):
    if bits[:2] != ['api', 'growth']:
        return False
    tail = bits[2:]
    if method == 'GET' and tail == ['search']:
        include = query.get('archived', [''])[0] == '1'
        return handler.json(search(store, query.get('q', [''])[0], include))
    if method == 'GET' and tail == ['timeline']:
        return handler.json(timeline(store))
    return False
