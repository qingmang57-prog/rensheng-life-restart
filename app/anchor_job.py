"""Script-anchored cutting (稿本锚定): align confirmed script parts to the word-level
transcript, score candidate takes, and turn the user's per-part selection into the
existing keep-plan + real-cut pipeline. Model judgments only reference existing cue IDs;
every millisecond interval is derived from source timestamps by this module."""
import difflib
import re

import media
import prompt_store
import providers

TAKE_GAP_MS = 20000      # same-part retakes further apart than this are separate takes
MIN_SENTENCE = 4         # norm chars; shorter sentences are too noisy to anchor on
SHINGLE = 4
TAKE_MAX_PER_PART = 4
DENSE_WINDOW_MS = 20000  # R2: >=2 restart signals inside this window make a restart-dense region
SENTENCE_END = tuple('。！？”）」』')


def norm(text):
    return re.sub(r'[\s，。！？、：；""''（）《》〈〉【】,.!?;:·\-—…]', '', str(text))


def split_sentences(text):
    pieces = re.split(r'[。！？!?\n；;]', str(text))
    return [norm(p) for p in pieces if len(norm(p)) >= MIN_SENTENCE]


def disfluency_signals(words, win_ms=5000, min_len=4, max_len=12):
    """Port of the J1 prototype feature: repeated substrings within a short window are
    restart/stutter signals. Rules never decide deletions here; they only feed take scoring
    and the R2 dense-region check in semantic_plan_valid. Returns merged (start_ms,end_ms) spans."""
    n = len(words)
    text = ''.join(w['text'] for w in words)
    hits = []
    for i in range(n):
        for L in range(min_len, max_len + 1):
            if i + L > n:
                break
            seg = text[i:i + L]
            if not seg.strip():
                continue
            j = i + L
            found = None
            while j < n and words[j]['start_ms'] - words[i]['start_ms'] <= win_ms:
                if text[j:j + L] == seg:
                    found = j
                    break
                j += 1
            if found is None:
                continue
            m = L
            while i + m < n and found + m < n and text[i + m] == text[found + m] and m < 24:
                m += 1
            hits.append((words[i]['start_ms'], words[min(found + m, n) - 1]['end_ms']))
            break
    if not hits:
        return []
    hits.sort()
    merged = [list(hits[0])]
    for s, e in hits[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def repeat_candidates(words, win_ms=5000, min_len=4, max_len=12, min_gap=2):
    """P1-a：把规则检测到的「原样重复」输出成候选（供建议生成，不只喂打分）。

    返回 [{'del_word_ids': [...], 'keep_word_ids': [...], 'text': '重复片段',
           'start_ms':.., 'end_ms':.., 'distance': 词距}]；
    语义：后出现的这段与前面某段逐字相同 → 后段可删、前段保留（非紧邻原样重复）。
    """
    n = len(words)
    text = ''.join(str(w.get('text') or '') for w in words)
    out = []
    seen = set()
    for i in range(n):
        for L in range(min_len, max_len + 1):
            if i + L > n:
                break
            seg = text[i:i + L]
            if not seg.strip():
                continue
            j = i + L
            found = None
            while j < n and words[j]['start_ms'] - words[i]['start_ms'] <= win_ms:
                if text[j:j + L] == seg:
                    found = j
                    break
                j += 1
            if found is None:
                continue
            m = L
            while (i + m < n and found + m < n and text[i + m] == text[found + m] and m < 24
                   and text[i + m] not in '，。！？、；：,.!?;: '):
                m += 1
            seg = text[i:i + m]
            del_ids = [w['id'] for w in words[i:i + m]]
            keep_ids = [w['id'] for w in words[found:found + m]]
            if not del_ids or not keep_ids or set(del_ids) & set(keep_ids):
                continue
            key = tuple(del_ids)
            if key in seen:
                continue
            seen.add(key)
            out.append({'del_word_ids': del_ids, 'keep_word_ids': keep_ids, 'text': seg,
                        'start_ms': words[i]['start_ms'], 'end_ms': words[i + m - 1]['end_ms'],
                        'distance': found - i})
            break
    out.sort(key=lambda x: x['start_ms'])
    return out


def dense_regions(words, extra_points=(), window_ms=DENSE_WINDOW_MS):
    """R2 helper: a 20s window counts as a restart-dense region only when it contains at
    least one real string-level restart signal AND >=2 total points (signals + semantic
    deletion starts). Deletion density alone never disables the sentence-level repair
    protection; the region must actually look like a failed take (画像报告 R2)."""
    signals = [s for s, _ in disfluency_signals(words)]
    points = sorted(signals + list(extra_points))
    regions = []
    for sig in signals:
        cluster = [x for x in points if sig <= x <= sig + window_ms]
        if len(cluster) >= 2:
            regions.append((sig, sig + window_ms))
    return regions


def find_candidates(words, cues, parts):
    """Deterministic pre-filter: score each script sentence against every transcript cue
    (paraphrase-tolerant ratio), then cluster the best hits into candidate takes.
    No model involved; every interval comes from existing cue timestamps."""
    norm_cues = [(c, norm(c['text'])) for c in cues]

    def best_hits(sentence):
        out = []
        for c, nc in norm_cues:
            if len(nc) < SHINGLE - 1:
                continue
            r = difflib.SequenceMatcher(None, sentence, nc).ratio()
            if r >= 0.35:
                out.append({'cue_id': c['id'], 'ratio': round(r, 3), 'chars': len(sentence),
                            'start_ms': c['start_ms'], 'end_ms': c['end_ms']})
        out.sort(key=lambda h: -h['ratio'])
        return out

    result = []
    for part in parts:
        norm_part = norm(part['text'])
        sentences = split_sentences(part['text'])
        sent_hits = []
        for si, s in enumerate(sentences):
            seen = set()
            for h in best_hits(s):      # 同一句可命中多处（重录 take），同一 cue 只记一次
                if h['cue_id'] in seen:
                    continue
                seen.add(h['cue_id'])
                sent_hits.append((si, h))
        sent_hits.sort(key=lambda x: x[1]['start_ms'])
        groups = []
        for si, h in sent_hits:
            if groups and h['start_ms'] - groups[-1][-1][1]['end_ms'] <= TAKE_GAP_MS:
                groups[-1].append((si, h))
            else:
                groups.append([(si, h)])
        cue_by_id = {c['id']: c for c in cues}
        takes = []
        for g in groups[:TAKE_MAX_PER_PART]:
            best_by_si = {}
            for si, h in g:
                best_by_si[si] = max(best_by_si.get(si, 0), h['ratio'])
            matched = sum(len(sentences[si]) * r for si, r in best_by_si.items())
            completeness = min(1.0, matched / max(1, len(norm_part)))
            if completeness < 0.25 and norm_part:
                continue
            ids = [h['cue_id'] for si, h in g]
            # 补齐组内命中之间的桥接 cue，让 take 连续成段
            lo, hi = g[0][1]['start_ms'], g[-1][1]['end_ms']
            for c in cues:
                if c['start_ms'] > lo and c['end_ms'] < hi:
                    if c['id'] not in ids:
                        ids.append(c['id'])
            cue_ids = sorted(ids, key=lambda i: cue_by_id[i]['start_ms'])
            s_ms = cue_by_id[cue_ids[0]]['start_ms']
            e_ms = cue_by_id[cue_ids[-1]]['end_ms']
            take_words = [w for w in words if s_ms - 100 <= w['start_ms'] and w['end_ms'] <= e_ms + 100]
            signals = disfluency_signals(take_words)
            density = len(signals) / max(1, (e_ms - s_ms) / 60000)
            takes.append({'cue_ids': cue_ids, 'start_ms': s_ms, 'end_ms': e_ms,
                          'text': ''.join(cue_by_id[c]['text'] for c in cue_ids)[:600],
                          'completeness': round(completeness, 3), 'signals': len(signals),
                          'density': round(density, 2)})
        result.append({'part_id': part['id'], 'role': part['role'], 'label': part['label'],
                       'text': part['text'][:300], 'norm_len': len(norm_part),
                       'sentences': len(sentences), 'takes': takes})
    return result



def judge_takes(parts_with_takes):
    """LLM pass over candidate takes; returns validated judgments keyed (part_id, take_index)."""
    payload = {'parts': [{'part_id': p['part_id'], 'label': p['label'], 'text': p['text']} for p in parts_with_takes],
               'takes': {p['part_id']: [{'index': i, 'start_s': round(t['start_ms'] / 1000, 1),
                                         'text': t['text'][:400]}
                                        for i, t in enumerate(p['takes'])] for p in parts_with_takes if p['takes']}}
    answer = providers.llm_json(prompt_store.get('anchor.judge'), payload)
    raw = answer.get('judgments')
    if not isinstance(raw, list):
        raise ValueError('锚定分析没有返回判定列表；未采纳任何内容。')
    valid = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = item.get('part_id')
        ti = item.get('take_index')
        entry = next((p for p in parts_with_takes if p['part_id'] == pid), None)
        if entry is None or not isinstance(ti, int) or not 0 <= ti < len(entry['takes']):
            continue
        take = entry['takes'][ti]
        drops = item.get('drop_cue_ids') or []
        if not isinstance(drops, list) or any(c not in take['cue_ids'] for c in drops):
            drops = []
        perf = item.get('performance')
        if not isinstance(perf, (int, float)) or not 1 <= perf <= 5:
            perf = None
        valid[(pid, ti)] = {'usable': item.get('usable') is not False, 'drop_cue_ids': drops,
                            'performance': perf, 'reason': str(item.get('reason', ''))[:300]}
    return valid


def score_take(take, judgment):
    perf = (judgment or {}).get('performance')
    perf_part = perf / 5 if perf else 0.5
    density_part = max(0.0, 1 - take['density'] / 3)
    return round(0.45 * take['completeness'] + 0.35 * perf_part + 0.20 * density_part, 3)


def finalize_parts(parts_with_takes, judgments):
    """Apply judgments, recompute spans after cue drops, attach scores and recommendations."""
    for p in parts_with_takes:
        kept = []
        for i, t in enumerate(p['takes']):
            j = judgments.get((p['part_id'], i))
            if j and not j['usable']:
                continue
            cue_ids = [c for c in t['cue_ids'] if c not in ((j or {}).get('drop_cue_ids') or [])]
            if not cue_ids:
                continue
            t = dict(t, cue_ids=cue_ids)
            t['score'] = score_take(t, j)
            t['performance'] = (j or {}).get('performance')
            t['reason'] = (j or {}).get('reason', '')
            kept.append(t)
        kept.sort(key=lambda t: t['start_ms'])
        for rank, t in enumerate(kept):
            t['take_index'] = rank
        p['takes'] = kept
        if kept:
            best = max(kept, key=lambda t: t['score'])
            p['recommended'] = best['take_index']
        else:
            p['recommended'] = None
    return parts_with_takes


def coverage_issues(parts_with_takes):
    issues = []
    last_end = -1
    for p in parts_with_takes:
        if not p['takes']:
            issues.append(f"缺少段落「{p['label']}」：原片里没有找到对应内容，需要补拍或改用剪掉停顿和口误。")
            continue
        rec = next(t for t in p['takes'] if t['take_index'] == p['recommended'])
        if last_end >= 0 and rec['start_ms'] < last_end:
            issues.append(f"段落「{p['label']}」的推荐 take 出现在前一段之前，原片顺序与稿本不一致，请人工核对。")
        last_end = max(last_end, rec['end_ms'])
    return issues


def build_plan(store, project, asset, job_id, data_root, stage):
    """Job body: transcribe if needed, align, judge, score, store anchor_plan. No media is cut."""
    if project.get('draft_changed') or not project.get('confirmed_version_id') or \
            asset.get('purpose', 'shooting') != 'shooting' or asset.get('script_version_id') != project['confirmed_version_id']:
        raise ValueError('素材与当前定稿不一致，请先核对拍摄版本。')
    if not providers.available()['asr']:
        raise providers.ProviderMissing('本地字幕转写尚未就绪。')
    script = store.get('script_version', project['confirmed_version_id'])
    if not script:
        raise ValueError('找不到当前定稿版本。')
    transcript = next((x for x in store.list('transcript', 10000) if x.get('asset_id') == asset['id']), None)
    if transcript is None:
        stage('语音识别转写，保留原始字幕与时间戳')
        audio = data_root / 'artifacts' / (job_id + '.mp3')
        media.extract_audio(asset['path'], audio)
        transcript = store.put('transcript', {**providers.asr(audio, stage=stage),
            'asset_id': asset['id'], 'project_id': project['id']})
    words = transcript.get('words', [])
    cues = transcript.get('cues', [])
    if not words or not cues:
        raise ValueError('这份转写缺少词级时间戳或断句，无法建立锚定计划。')
    stage('按定稿逐段定位候选 take（本地模糊匹配）')
    parts = find_candidates(words, cues, script['parts'])
    judgments = {}
    llm_used = False
    if providers.available()['llm'] and any(p['takes'] for p in parts):
        stage('模型判定 take 可用性、修剪边界与表演分')
        judgments = judge_takes(parts)
        llm_used = True
    parts = finalize_parts(parts, judgments)
    stage('稿本覆盖校验')
    used = {c for p in parts for t in p['takes'] for c in t['cue_ids']}
    improvised = [{'cue_id': c['id'], 'start_ms': c['start_ms'], 'end_ms': c['end_ms'], 'text': c['text'][:200]}
                  for c in cues if c['id'] not in used]
    issues = coverage_issues(parts)
    plan = store.put('anchor_plan', {'project_id': project['id'], 'asset_id': asset['id'],
        'transcript_id': transcript['id'], 'script_version_id': project['confirmed_version_id'],
        'parts': parts, 'improvised': improvised, 'issues': issues,
        'llm_judged': llm_used, 'method': 'anchor_llm_judged' if llm_used else 'anchor_fuzzy_only',
        'note': '模型只引用已存在的 cue_id，毫秒区间由后端回源生成；尚未剪切任何音视频。'
                + ('' if llm_used else ' 生成模型不可用：表演维度按中性分计，建议配置后重新分析。')})
    return {'plan': plan, 'requires_action': True,
            'stage': (f"已定位 {sum(len(p['takes']) for p in parts)} 个候选 take"
                      + (f'，{len(issues)} 个覆盖问题' if issues else '，稿本覆盖完整')
                      + '，请逐段确认后执行剪切')}


def closure_warnings(words, cues, keep_cue_ids):
    """甲方铁律机检（D23/D16）：成片结尾必须完整收束，不能止于半句。"""
    keep = sorted((c for c in cues if c['id'] in keep_cue_ids), key=lambda c: c['start_ms'])
    if not keep:
        return ['没有任何保留内容。']
    warnings = []
    if keep[-1]['text'].strip() and keep[-1]['text'].strip()[-1] not in SENTENCE_END:
        warnings.append('结尾疑似止于半句（铁律：讲稿必须完整收束），请换一条 take、勾选后续即兴句，或在剪掉停顿和口误中处理。')
    if len(keep) > 1 and keep[0]['text'].strip() and keep[0]['text'].strip()[-1] not in SENTENCE_END:
        warnings.append('开头段落在句中起剪，请试听确认开头第一句完整。')
    return warnings


def apply_plan(store, project, asset, plan, selection, keep_improvised_ids, silence_mode, job_id, data_root, stage):
    """Turn the confirmed per-part take selection into a real cut, reusing the semantic-cut pipeline."""
    if silence_mode not in ('conservative', 'natural', 'tight'):
        raise ValueError('停顿清理档位无效，仅支持 conservative/natural/tight。')
    if project.get('draft_changed') or project.get('confirmed_version_id') != plan.get('script_version_id') \
            or plan.get('asset_id') != asset['id'] or asset.get('script_version_id') != project['confirmed_version_id']:
        raise ValueError('这份锚定计划与当前定稿或素材不一致，请重新分析。')
    if not isinstance(selection, dict) or not selection:
        raise ValueError('请先为每段稿本选择一条 take。')
    chosen = {}
    for p in plan['parts']:
        if not p['takes']:
            continue  # 缺段在分析阶段已报覆盖问题；剪切只要求有候选的段落完成选择
        want = selection.get(p['part_id'])
        take = next((t for t in p['takes'] if t['take_index'] == want), None) if isinstance(want, int) else None
        if take is None:
            raise ValueError(f"段落「{p['label']}」还没有选择 take。")
        chosen[p['part_id']] = take
    improv_ids = {x['cue_id'] for x in plan.get('improvised', [])}
    keep_extra = [c for c in (keep_improvised_ids or []) if c in improv_ids]
    script_id = project['confirmed_version_id']

    transcript = store.get('transcript', plan['transcript_id'])
    cues = transcript.get('cues', [])
    keep_cues = set()
    for take in chosen.values():
        keep_cues.update(take['cue_ids'])
    keep_cues.update(keep_extra)
    removed = []
    for w in transcript.get('words', []):
        hit = next((c for c in cues if c['start_ms'] - 100 <= w['start_ms'] and w['end_ms'] <= c['end_ms'] + 100), None)
        if hit is None or hit['id'] not in keep_cues:
            removed.append({'start_ms': w['start_ms'], 'end_ms': w['end_ms'], 'reason': 'anchor'})
    stage('切点吸附到邻近静音，避免切进字头字尾')
    silences = media.silence_intervals(asset['path']) if asset['info']['audio'] else []
    removed = media.snap_edges_to_silence(silences, removed)
    stage('与保守静音清理合并，形成源时间保留计划')
    silence = media.silence_plan(asset['path'], asset['info'], silence_mode)
    removed += [{**x, 'reason': 'silence'} for x in silence['removed']]
    segments, merged = media.combine_removed(asset['info']['duration_ms'], removed)
    stage('按源时间线剪切并编码')
    output = data_root / 'artifacts' / (job_id + '.mp4')
    mapping = media.render_cut(asset['path'], asset['info'], segments, output)
    info = media.probe(output)
    expected = sum(x['end_ms'] - x['start_ms'] for x in segments)
    diff = abs(info['duration_ms'] - expected)
    # 2026-11-10（试用反馈 P0-4）：200ms 级差异（如 31065 vs 30835）不等于坏片。
    # 可播放且差异有限时保留成片、标「需人工复核」；差异大到像丢段才判失败。
    if diff > max(2000, expected * 0.05):
        raise ValueError(f'成片时长 {info["duration_ms"]}ms 与保留计划 {expected}ms 相差 {diff}ms，超出可播放容差；中间文件保留：{output}')
    needs_review = diff > 200
    warnings = closure_warnings(transcript.get('words', []), cues, keep_cues)
    saved = store.put('cleanup_plan', {'segments': segments, 'removed': merged, 'method': 'anchor_plus_silence',
        'silence_mode': silence_mode, 'edge_snap': {'window_ms': 250, 'extend_ms': 200, 'source': 'ffmpeg_silencedetect'},
        'anchor_plan_id': plan['id'], 'selection': {k: v['take_index'] for k, v in chosen.items()},
        'keep_improvised': keep_extra, 'asset_id': asset['id'], 'script_version_id': script_id, 'mapping': mapping})
    artifact = store.put('artifact', {'project_id': project['id'], 'script_version_id': script_id, 'asset_id': asset['id'],
        'kind': 'clean_video', 'path': str(output), 'filename': '稿本锚定-' + project['title'][:30] + '.mp4',
        'info': info, 'plan_id': saved['id'], 'anchor_plan_id': plan['id'], 'mapping': mapping,
        'closure_warnings': warnings, 'needs_review': needs_review, 'duration_diff_ms': diff})
    return {'artifact': artifact, 'plan': saved, 'warnings': warnings,
            'stage': '稿本锚定剪切完成，请试听审阅'
                     + (f'（时长与保留计划相差 {diff}ms，需人工核对音画）' if needs_review else '')
                     + (f'（{len(warnings)} 条收束提醒）' if warnings else '，开头结尾完整句校验通过')}
