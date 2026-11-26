# -*- coding: utf-8 -*-
"""per-user（本机单用户版）缓存命中与省钱遥测：只读 llm_usage.jsonl，零写、零网络。

用途：给 web 线 UI「越用越省」直接出数——按天命中率曲线、按功能命中、
前缀 epoch（stable_sha）首调/暖调对比、以及按官方价折算的省钱估算。

集成（app.py，3 行；app.py 不在缓存写域，由 web 线接线）：
    if path == '/api/telemetry/cache' and method == 'GET':
        import cache_report
        return self.json(cache_report.report())

数据口径：
- hit/miss 来自网关回传的 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
  网关没回传的记录不参与命中率（不估算、不补零，与 providers.cache_telemetry 一致）。
- 价格：deepseek-flash 输入 未命中 1 元/百万 token、命中 0.02 元/百万（0.02 倍，省 98%）；
  高峰翻倍（2 / 0.04），用 price_miss=2.0 传参即可。只折输入侧（输出不受缓存影响）。
"""
import json
import os
import time
from pathlib import Path

PRICE_MISS_CNY_PER_M = 1.0      # 空闲时段：未命中输入 1 元/百万
PRICE_HIT_RATIO = 0.02          # 命中输入按未命中的 0.02 倍计费
DEFAULT_DAYS = 14


def _data_dir():
    root = os.getenv('IP_DATA_DIR')
    return Path(root) if root else Path(__file__).resolve().parent / '.runtime'


def _rows(path):
    out = []
    try:
        with open(str(path), encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def _split(row):
    hit, miss = row.get('hit'), row.get('miss')
    if not isinstance(hit, int) or not isinstance(miss, int):
        return None
    return hit, miss


def _rate(hit, miss):
    total = hit + miss
    return round(hit / total, 4) if total else None


def report(days=DEFAULT_DAYS, price_miss=PRICE_MISS_CNY_PER_M, hit_ratio=PRICE_HIT_RATIO):
    """返回可直接 json 化的遥测报告；供 UI 展示「越用越省」。"""
    path = _data_dir() / 'llm_usage.jsonl'
    rows = _rows(path)
    series, by_day = [], {}
    by_call, epochs = {}, {}
    for row in rows:
        pair = _split(row)
        if pair is None:
            continue
        hit, miss = pair
        day = str(row.get('ts') or '')[:10]
        d = by_day.setdefault(day, {'date': day, 'calls': 0, 'hit': 0, 'miss': 0, 'prompt': 0})
        d['calls'] += 1
        d['hit'] += hit
        d['miss'] += miss
        d['prompt'] += (row.get('prompt_tokens') or 0)
        call = str(row.get('call') or 'unknown')
        c = by_call.setdefault(call, {'call': call, 'calls': 0, 'hit': 0, 'miss': 0, 'prompt': 0})
        c['calls'] += 1
        c['hit'] += hit
        c['miss'] += miss
        c['prompt'] += (row.get('prompt_tokens') or 0)
        sha = row.get('stable_sha') or ''
        e = epochs.setdefault(sha, {'stable_sha': sha, 'stable_chars': row.get('stable_chars'),
                                    'calls': 0, 'hit': 0, 'miss': 0, 'first_hit': hit,
                                    'first_miss': miss})
        e['calls'] += 1
        e['hit'] += hit
        e['miss'] += miss

    total_hit = sum(d['hit'] for d in by_day.values())
    total_miss = sum(d['miss'] for d in by_day.values())
    total_prompt = sum(d['prompt'] for d in by_day.values())
    cum_hit = cum_miss = 0
    for day in sorted(by_day):
        d = by_day[day]
        cum_hit += d['hit']
        cum_miss += d['miss']
        series.append({'date': day, 'calls': d['calls'], 'hit': d['hit'], 'miss': d['miss'],
                       'prompt_tokens': d['prompt'],
                       'hit_rate': _rate(d['hit'], d['miss']),
                       'cum_hit_rate': _rate(cum_hit, cum_miss)})
    series = series[-max(1, int(days)):]

    functions = []
    for c in sorted(by_call.values(), key=lambda x: -x['calls']):
        functions.append({'call': c['call'], 'calls': c['calls'], 'hit': c['hit'], 'miss': c['miss'],
                          'hit_rate': _rate(c['hit'], c['miss']),
                          'avg_prompt_tokens': round(c['prompt'] / c['calls'], 1) if c['calls'] else None})
    epoch_rows = []
    for e in sorted(epochs.values(), key=lambda x: -x['calls']):
        warm_calls = e['calls'] - 1
        warm_hit = e['hit'] - e['first_hit'] if warm_calls > 0 else 0
        warm_miss = e['miss'] - e['first_miss'] if warm_calls > 0 else 0
        epoch_rows.append({'stable_sha': e['stable_sha'], 'stable_chars': e['stable_chars'],
                           'calls': e['calls'], 'hit_rate': _rate(e['hit'], e['miss']),
                           'first_hit_rate': _rate(e['first_hit'], e['first_miss']),
                           'warm_hit_rate': _rate(warm_hit, warm_miss) if warm_calls else None})

    # 省钱：命中 token 本该按未命中价计，实际按 hit_ratio 倍 → 省 (1-hit_ratio)
    saved_tokens = total_hit * (1.0 - hit_ratio)
    saved_cny = saved_tokens / 1e6 * price_miss
    actual_cny = (total_miss + total_hit * hit_ratio) / 1e6 * price_miss
    without_cny = (total_miss + total_hit) / 1e6 * price_miss
    return {
        'source': str(path), 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'user': {'scope': 'per-user', 'owner': 'owner（本机单用户版）'},
        'price': {'currency': 'CNY', 'price_miss_cny_per_million': price_miss,
                  'hit_ratio': hit_ratio, 'note': 'deepseek-flash 空闲价；高峰传 price_miss=2.0'},
        'total': {'calls': sum(d['calls'] for d in by_day.values()),
                  'hit': total_hit, 'miss': total_miss,
                  'hit_rate': _rate(total_hit, total_miss),
                  'prompt_tokens': total_prompt},
        'savings': {'hit_tokens': total_hit,
                    'saved_cny': round(saved_cny, 6),
                    'actual_cny': round(actual_cny, 6),
                    'without_cache_cny': round(without_cny, 6),
                    'saved_pct': round(saved_cny / without_cny, 4) if without_cny else None,
                    'formula': 'hit × (1 - hit_ratio) × price_miss / 1e6'},
        'series': series,
        'by_function': functions,
        'prefix_epochs': epoch_rows,
        'climb_note': ('cum_hit_rate 随调用次数上升＝「越用越省」；prefix_epochs 里 '
                       'first_hit_rate（冷）与 warm_hit_rate（暖）的差＝改一次档案/加卡的一次性再暖成本。'),
    }


if __name__ == '__main__':
    print(json.dumps(report(), ensure_ascii=False, indent=1))
