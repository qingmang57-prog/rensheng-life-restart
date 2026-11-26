"""B1 lite packaging (学习包装风格): adapt the cut transcript to the packaging schema,
generate a grounded config (cards/chapters/mood-BGM/bilingual translations) via the LLM,
run the migrated koubo-packaging-wide script chain as controlled subprocesses, and burn
the final composite. Every quote must come verbatim from the actual cut transcript."""
import json
import os
import subprocess
import sys
from pathlib import Path

import media
import prompt_store
import providers

PACKAGING_DIR = Path(__file__).resolve().parent.parent / 'tools' / 'packaging'
SCRIPT_PY = os.getenv('IP_PACKAGING_PYTHON',
    str(PACKAGING_DIR.parent.parent / 'tools' / 'python' / 'python.exe'))
MOOD_BGM = {'温情陪伴': (90, 520), '燃': (108, 600), '沉郁叙事': (78, 460), '轻快': (122, 670)}
MAX_CARDS = 12


def _as_string_list(en):
    if isinstance(en, list):
        return en
    if isinstance(en, dict):
        for k in ('translations', 'english', 'items', 'result', 'data', 'list', 'segments'):
            if isinstance(en.get(k), list):
                return en[k]
        vals = [en[k] for k in sorted(en, key=str) if isinstance(en[k], str)]
        if vals and len(vals) == len(en):
            return vals                      # {"1":"…","2":"…"} 形态
    return None


def _fetch_translations(texts):
    for attempt in (1, 2):
        en = providers.llm_json(
            prompt_store.get('packaging.translation').replace('{count}', str(len(texts))),
            {'segments': texts})
        translations = _as_string_list(en)
        if isinstance(translations, list) and len(translations) == len(texts):
            return [str(t)[:200] for t in translations]
    raise ValueError('英文字幕条数与字幕段数不一致（重试后仍不符）；未渲染。')


def _run(cmd, stage_name, cwd=None):
    env = media.runtime_environment()
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([str(x) for x in cmd], capture_output=True, encoding='utf-8',
                            errors='replace', env=env, timeout=1200, cwd=str(cwd) if cwd else None,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(f'{stage_name} 失败：{(result.stderr or result.stdout)[-400:]}')
    return result


def _cues_by_sentence(cues, mapping):
    out = []
    for c in cues:
        for seg in mapping:
            a = max(c['start_ms'], seg['start_ms'])
            b = min(c['end_ms'], seg['end_ms'])
            if b - a < (c['end_ms'] - c['start_ms']) * 0.5:
                continue
            out.append({'start_ms': round(seg['output_start_ms'] + a - seg['start_ms']),
                        'end_ms': round(seg['output_start_ms'] + b - seg['start_ms']),
                        'text': c['text']})
            break
    out.sort(key=lambda x: x['start_ms'])
    return out


def map_cues(cues, mapping, words=None):
    """保留词表是字幕唯一真相：有词级时间戳时按词中点回源、从保留词重建字幕。

    2026-11-10（试用反馈 P0-3）：词级结果可能部分为空（缺时间戳/空文本），
    cues_from_words 会跳过这类词，导致整句字幕静默消失。这里对"句级有内容、
    词级一个字都没留下"的句子回退到句级时间轴并报数供核对；词级有内容的句子
    仍以词级为准，已删的词不会被写回。
    """
    if not words:
        return _cues_by_sentence(cues, mapping), {'mode': 'sentence', 'fallback_cues': 0}
    word_cues = media.cues_from_words(words, mapping)
    sentence_cues = _cues_by_sentence(cues, mapping)
    out = list(word_cues)
    fallback = 0
    for sc in sentence_cues:
        if not any(wc['start_ms'] < sc['end_ms'] and sc['start_ms'] < wc['end_ms'] for wc in word_cues):
            out.append(sc)
            fallback += 1
    out.sort(key=lambda x: x['start_ms'])
    return out, {'mode': 'words', 'fallback_cues': fallback}


def adapt_transcript(transcript, mapping, duration_ms):
    cues, stats = map_cues(transcript.get('cues', []), mapping, transcript.get('words'))
    if not cues:
        raise ValueError('没有可映射到剪后时间轴的字幕；请先核对转写。')
    segments = [{'start': round(c['start_ms'] / 1000, 3), 'end': round(c['end_ms'] / 1000, 3),
                 'text': c['text']} for c in cues]
    result = {'duration': round(duration_ms / 1000, 3), 'segments': segments,
              'caption_mode': stats['mode']}
    if stats['fallback_cues']:
        # 不静默：写进 transcript.json，审阅时能看到哪几句是靠句级时间保下来的。
        result['caption_note'] = ('%d 句词级结果缺失，已按句级时间保留；请对照原声核对字幕。'
                                  % stats['fallback_cues'])
        result['caption_fallback_cues'] = stats['fallback_cues']
    return result, cues


def generate_config(project, artifact, segments, brief):
    """LLM config with verbatim grounding; raises on any fabricated quote."""
    duration_s = segments['duration']
    mood = (brief or {}).get('mood_arc', {}).get('overall')
    bpm, brightness = MOOD_BGM.get(mood, (100, 560))
    full_text = ''.join(s['text'] for s in segments['segments'])
    answer = providers.llm_json(
        prompt_store.get('packaging.config').replace('{duration_s}', f'{duration_s:.1f}'), {
        'title': project['title'], 'segments': segments['segments'],
        'mood': mood or '未提供', 'director_cards': (brief or {}).get('visual_plan', {}).get('quote_cards', [])})
    cards = answer.get('cards')
    if not isinstance(cards, list) or not 1 <= len(cards) <= MAX_CARDS:
        raise ValueError('值得转发的卡配置格式无效（需1-12张）。')
    clean_cards = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        try:
            start = float(card.get('start'))
            end = float(card.get('end'))
        except (TypeError, ValueError):
            continue
        lines = [str(x).strip() for x in (card.get('lines') or []) if str(x).strip()][:3]
        if not lines or any(len(l) > 10 for l in lines) or any(l not in full_text for l in lines):
            continue                                  # 逐字回源：编造引用直接拒
        if not (0 <= start < end <= duration_s + 0.5):
            continue
        clean_cards.append({'start': round(start, 2), 'end': round(end, 2),
                            'tag': str(card.get('tag', ''))[:12] or lines[0][:6],
                            'lines': lines, 'english': str(card.get('english', ''))[:80],
                            'kind': 'quote'})
    if not clean_cards:
        raise ValueError('没有通过逐字回源校验的值得转发的卡；未渲染，原始底片保留。')
    # 时间窗去重：同窗口多卡会叠字（render_stems 一窗一卡），保留先出现的一张
    clean_cards.sort(key=lambda c: (c['start'], -(c['end'] - c['start'])))
    deduped = []
    for c in clean_cards:
        if any(not (c['end'] <= k['start'] or c['start'] >= k['end']) for k in deduped):
            continue
        deduped.append(c)
    clean_cards = deduped[:MAX_CARDS]
    translations = _fetch_translations([s['text'] for s in segments['segments']])
    chapters = []
    for ch in answer.get('chapters') or []:
        if not isinstance(ch, dict):
            continue
        try:
            start, end = float(ch.get('start')), float(ch.get('end'))
        except (TypeError, ValueError):
            continue
        if 0 <= start < end <= duration_s + 0.5:
            chapters.append({'start': round(start, 2), 'end': round(end, 2),
                             'label': str(ch.get('label', ''))[:24]})
    config = {'source': artifact['path'],
              'transcript': 'transcript.json',
              'title': str(answer.get('title') or project['title'])[:30],
              'tagline': str(answer.get('tagline', ''))[:80],
              'source_crop': [0, 0, artifact['info']['width'], artifact['info']['height']],
              'portrait_crop': [660, 60, 700, 700],
              'cards': clean_cards, 'chapters': chapters, 'corrections': {},
              'bgm': {'file': 'stems/bgm.mp3', 'volume': 0.2}}
    return config, translations, {'bpm': bpm, 'brightness': brightness}


def _bgm_library():
    """配置里的曲库有效则用配置；无效/不存在时回落到包内 tools/bgm，并打印实际路径。"""
    env = (os.getenv('IP_BGM_LIBRARY', '') or '').strip()
    if env:
        configured = Path(env)
        if configured.is_dir() and any(configured.glob('*.mp3')):
            print('[packaging] BGM library = ' + str(configured))
            return str(configured)
        print('[packaging] IP_BGM_LIBRARY invalid, fallback: ' + env)
    fallback = Path(__file__).resolve().parent.parent / 'tools' / 'bgm'
    if fallback.is_dir() and any(fallback.glob('*.mp3')):
        print('[packaging] BGM library = ' + str(fallback))
    else:
        print('[packaging] BGM library missing, skip BGM: ' + str(fallback))
    return str(fallback)


def run_chain(workdir, duration_s, bgm_targets, bgm_library, stage):
    """Run the migrated script chain with explicit interpreter/paths (portable defaults)."""
    ffmpeg = media.binary('ffmpeg')
    ffprobe = media.binary('ffprobe')
    cfg = workdir / 'config.json'
    stage('生成包装底图与卡片底片')
    _run([SCRIPT_PY, PACKAGING_DIR / 'make_background.py', '--config', cfg, '--workdir', workdir], '包装底图', cwd=workdir)
    _run([SCRIPT_PY, PACKAGING_DIR / 'render_stems.py', '--config', cfg, '--workdir', workdir,
          '--ffmpeg', ffmpeg], '卡片底片', cwd=workdir)
    stage('生成双语字幕')
    _run([SCRIPT_PY, PACKAGING_DIR / 'gen_bilingual_srt.py', '--config', cfg,
          '--translations', workdir / 'translations.json', '--workdir', workdir], '双语字幕', cwd=workdir)
    bgm_note = '无BGM（未配置曲库）'
    if bgm_library and Path(bgm_library).is_dir() and any(Path(bgm_library).glob('*.mp3')):
        stage('按情绪参数从本地曲库选 BGM')
        launcher = workdir / 'run_pick_bgm.py'
        launcher.write_text(
            'import sys\nsys.path.insert(0, r%r)\nimport pick_bgm\n'
            'pick_bgm.FF = r%r\npick_bgm.FP = r%r\n'
            'sys.argv = ["pick_bgm.py", "--library", r%r, "--duration", %r, "--bpm", %r, "--brightness", %r,'
            ' "--workdir", r%r, "--ffmpeg", r%r]\npick_bgm.main()\n' % (
                str(PACKAGING_DIR), ffmpeg, ffprobe, str(bgm_library),
                f'{duration_s:.3f}', str(bgm_targets['bpm']), str(bgm_targets['brightness']),
                str(workdir), ffmpeg), encoding='utf-8')
        _run([SCRIPT_PY, launcher], '选择BGM', cwd=workdir)
        bgm_note = 'BGM 来自本地曲库（人声优先，音量≤0.2）'
    else:
        config = json.loads(cfg.read_text(encoding='utf-8'))
        config.pop('bgm', None)
        cfg.write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding='utf-8')
    stage('烧录 6 轨字幕与混音')
    _run([SCRIPT_PY, PACKAGING_DIR / 'burn_compose.py', '--config', cfg,
          '--workdir', workdir, '--ffmpeg', ffmpeg], '烧录合成', cwd=workdir)
    return bgm_note


def package_job(store, project, artifact, job_id, data_root, stage):
    if artifact.get('project_id') != project['id'] or artifact.get('kind') != 'clean_video':
        raise ValueError('请选择一个本作品的清理底片。')
    if project.get('draft_changed') or artifact.get('script_version_id') != project.get('confirmed_version_id'):
        raise ValueError('这版清理与当前定稿不一致，请核对版本。')
    if not artifact.get('mapping') or not artifact.get('asset_id'):
        raise ValueError('这版清理缺少剪后时间轴映射，无法包装。')
    if not providers.available()['llm']:
        raise providers.ProviderMissing('包装配置需要生成模型；底片已保留，可稍后重试。')
    transcript = next((x for x in store.list('transcript', 10000) if x.get('asset_id') == artifact['asset_id']), None)
    if transcript is None:
        raise ValueError('找不到对应转写；请先完成本地转写。')
    brief = next((x for x in store.list('director_brief', 50)
                  if x.get('script_version_id') == project['confirmed_version_id']), None)
    workdir = data_root / 'packages' / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    stage('把字幕映射到剪后时间轴')
    segments, cues = adapt_transcript(transcript, artifact['mapping'], artifact['info']['duration_ms'])
    (workdir / 'transcript.json').write_text(json.dumps(segments, ensure_ascii=False), encoding='utf-8')
    stage('DeepSeek 生成值得转发的卡、章节与英文字幕（逐字回源校验）')
    config, translations, bgm_targets = generate_config(project, artifact, segments, brief)
    (workdir / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding='utf-8')
    (workdir / 'translations.json').write_text(json.dumps(translations, ensure_ascii=False), encoding='utf-8')
    bgm_note = run_chain(workdir, segments['duration'], bgm_targets,
                         os.getenv('IP_BGM_LIBRARY', ''), stage)
    output = workdir / 'packaged_output.mp4'
    if not output.is_file():
        raise ValueError('合成未产出成片；中间文件保留，请人工核对。')
    info = media.probe(output)
    diff = abs(info['duration_ms'] - artifact['info']['duration_ms'])
    # 2026-11-10（试用反馈 P0-4）：毫秒级差异不等于坏片。能探测到视频轨、差异有限时
    # 保留成片并标「需人工复核」；只有差异大到像丢段/坏文件才判失败，不静默放行。
    if diff > max(2000, artifact['info']['duration_ms'] * 0.05):
        raise ValueError(f'包装成片时长 {info["duration_ms"]}ms 与清理底片 {artifact["info"]["duration_ms"]}ms 相差 {diff}ms，超出可播放容差；中间文件保留：{output}')
    needs_review = diff > 300
    item = store.put('artifact', {'project_id': project['id'],
        'script_version_id': project['confirmed_version_id'], 'asset_id': artifact['asset_id'],
        'kind': 'packaged_video', 'path': str(output), 'filename': '学习包装-' + project['title'][:30] + '.mp4',
        'info': info, 'clean_artifact_id': artifact['id'], 'packaging_method': 'b1_lite_learning_pack',
        'packaging_note': bgm_note, 'caption_cues': cues,
        'needs_review': needs_review, 'duration_diff_ms': diff})
    return {'artifact': item, 'bgm_note': bgm_note,
            'stage': '学习包装（轻）已完成，请播放审阅' + (f'（时长与底片相差 {diff}ms，需人工核对音画）' if needs_review else '')}
