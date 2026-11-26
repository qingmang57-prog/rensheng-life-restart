# /// script
# requires-python = ">=3.10"
# dependencies = ["jsonschema"]
# ///
"""织梦包装引擎 · adapter.py（施工 0.2 落实版）
流程移植自 385712 open-beta adapter.py（MIT），改造：
- 内容模型 = dreamweaver-kit：segments(控件语义组) + narrationMap(连续字幕契约)
- 控件 props 合同由 schemas/catalog.json 校验（替代上游每个模板内嵌 schema）
- 人脸裁剪★公式与三层 sha256 回归锁原样保留
- needs-binding 纪律：缺素材/超容量一律抛错，无 fallback
验收：python adapter.py plan --input <params.json> 全绿即过。"""
from pathlib import Path
import argparse, json, hashlib, math, shutil, subprocess, sys

ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT.parent / 'schemas'

def read(p): return json.loads(Path(p).read_text(encoding='utf-8'))
def save(p, o):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
def sha(p):
    with Path(p).open('rb') as f: return hashlib.file_digest(f, 'sha256').hexdigest()

class NeedsBinding(ValueError): pass

# ---------- 画幅表（新增 4x3 只加行，不改已有画幅行为） ----------
# 输出尺寸：landscape 16:9 / portrait 9:16 / 4x3 竖 3:4（高与竖屏一致，宽 +33%）
LAYOUT_SIZES = {'landscape': (1920, 1080), 'portrait': (1080, 1920), '4x3': (1440, 1920)}
# 口播素材的裁剪盒：横竖沿用 720×1280（原行为），3:4 用 720×960（同高减宽，不拉变形）
PERSON_BOX = {'landscape': (720, 1280), 'portrait': (720, 1280), '4x3': (720, 960)}

def layout_size(layout):
    if layout not in LAYOUT_SIZES:
        raise NeedsBinding('layout 必须是 ' + ','.join(LAYOUT_SIZES))
    return LAYOUT_SIZES[layout]

# ---------- 基础校验 ----------
def check_shape(d):
    """外形校验：有 jsonschema 用标准校验器，否则用内置轻量检查
    （实质校验在 check_segments/check_narration_map，此处只管形状）。"""
    try:
        import jsonschema
        jsonschema.Draft202012Validator(read(SCHEMAS / 'params.schema.json')).validate(d)
        return
    except ImportError:
        pass
    if not isinstance(d, dict): raise NeedsBinding('输入必须是 JSON 对象')
    if d.get('workflow') != 'dreamweaver-kit/1': raise NeedsBinding('workflow 必须是 dreamweaver-kit/1')
    tl = d.get('timeline')
    if not isinstance(tl, dict): raise NeedsBinding('缺 timeline')
    for k in ('duration', 'segments', 'voiceover', 'music'):
        if k not in tl: raise NeedsBinding('timeline 缺必填: ' + k)
    if not isinstance(tl['segments'], list) or not tl['segments']:
        raise NeedsBinding('segments 至少 1 段')
    for i, s in enumerate(tl['segments']):
        for k in ('control', 't0', 't1', 'props'):
            if k not in s: raise NeedsBinding(f'segments[{i}] 缺 {k}')
    if 'shortTitle' not in d.get('header', {}): raise NeedsBinding('header.shortTitle 必填')

def check_segments(d, catalog):
    segs = d['timeline']['segments']
    controls = catalog['controls']
    fam = d['timeline'].get('family')
    if fam not in catalog['families']:
        raise NeedsBinding('family 必须是 ' + ','.join(catalog['families']))
    prev_end = 0.0
    for i, s in enumerate(segs):
        c = s.get('control')
        if c not in controls: raise NeedsBinding(f'segments[{i}]: 未知控件 {c}')
        if c not in catalog['families'][fam]['controls']:
            raise NeedsBinding(f'segments[{i}]: 控件 {c} 未注册到家族 {fam}')
        if s['t0'] < prev_end - 1e-6:
            raise NeedsBinding(f'segments[{i}]: t0={s["t0"]} 与上一段 {prev_end} 不相接（段必须首尾相接）')
        # props 合同（catalog 登记了 max/maxItems/enum 的字段才校验）
        spec = controls[c]['props']
        props = s.get('props', {})
        for k, rule in spec.items():
            if k not in props:
                if 'required' in controls[c] and k in controls[c]['required'] or c in ('keyword', 'quote', 'tip') and k in ('word', 'text'):
                    raise NeedsBinding(f'segments[{i}].props.{k}: 必填')
                continue
            v = props[k]
            if 'enum' in rule and v not in rule['enum']:
                raise NeedsBinding(f'segments[{i}].props.{k}: 不在 {rule["enum"]}')
            if isinstance(v, str) and 'max' in rule and len(v) > rule['max']:
                raise NeedsBinding(f'segments[{i}].props.{k}: 超容量 {len(v)}>{rule["max"]}')
            if isinstance(v, list) and 'maxItems' in rule and len(v) > rule['maxItems']:
                raise NeedsBinding(f'segments[{i}].props.{k}: 超数量 {len(v)}>{rule["maxItems"]}')
        prev_end = s['t1']
    if abs(prev_end - d['timeline']['duration']) > 1 / 60:
        raise NeedsBinding(f'时长不符：段尾 {prev_end} vs 声明 {d["timeline"]["duration"]}')

def check_narration_map(d):
    """连续字幕三条契约（试制实证版）：
    ① captionIndices = 与该镜头相交的全部字幕（跨界句两边都列）
    ② beats 保留原句完整起止（不钳位到镜头内）
    ③ 每行 captionBinding:"continuous" """
    caps = d['timeline'].get('captionsData') or []
    nm = d['timeline'].get('narrationMap') or []
    if not nm: return
    for row in nm:
        if row.get('captionBinding') != 'continuous':
            raise NeedsBinding(f'narrationMap[{row["shot"]}]: 缺 captionBinding:"continuous"')
        t0, t1 = row['start'], row['end']
        inter = [i for i, c in enumerate(caps) if c['start'] < t1 and c['end'] > t0]
        if row['captionIndices'] != inter:
            raise NeedsBinding(f'narrationMap[{row["shot"]}]: captionIndices 必须是相交字幕全集')
        for b in row.get('beats', []):
            ci = b.get('ci')
            if ci is not None and 0 <= ci < len(caps):
                c = caps[ci]
                if b['at'] < c['start'] - 1e-3 or b['until'] > c['end'] + 1e-3:
                    raise NeedsBinding(f'narrationMap[{row["shot"]}] beat: 句区间被改动')


def _auto_face(root, src):
    """调 detect_face.py 自动探测；逐候选解释器尝试（cv2 只在特定 venv）。"""
    import os, subprocess
    cands = [os.environ.get('DW_FACE_PY', ''), sys.executable,
             '']
    p = media_path(root, src)
    for py in cands:
        if not py: continue
        try:
            r = subprocess.run([py, str(ROOT / 'detect_face.py'), str(p)],
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                d = json.loads(r.stdout.strip().splitlines()[-1])
                if d.get('ok'):
                    return {'cx': d['cx'], 'cy': d['cy'], 'h': d['h'],
                            'tracking': d.get('tracking') or []}
        except Exception:
            continue
    return None

def media_path(root, value):
    if not isinstance(value, str) or not value: raise NeedsBinding('needs-binding: media file')
    q = Path(value); raw = q if q.is_absolute() else root / q
    p = raw.resolve()
    if raw.is_symlink() or not p.is_file(): raise NeedsBinding('Missing media or symlink: ' + value)
    if not q.is_absolute() and not p.is_relative_to(root.resolve()):
        raise NeedsBinding('Relative media escapes the input directory')
    return p

def probe(p):
    return json.loads(subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(p)]))

# ---------- plan ----------
def plan(input_file):
    source = Path(input_file).resolve()
    d = read(source); root = source.parent
    catalog = read(SCHEMAS / 'catalog.json')
    check_shape(d)
    tl = d['timeline']
    lay = tl.get('layout', 'portrait')
    layout_size(lay)                      # 画幅白名单：landscape / portrait / 4x3
    check_segments(d, catalog)
    check_narration_map(d)
    # 口播窗：face 归一化几何必填（圆形小窗一等公民）
    pres = tl.get('presenter') or {}
    if pres.get('src'):
        face = pres.get('face') or {}
        if pres.get('framing') != 'none' and not all(isinstance(face.get(k), (int, float)) and math.isfinite(face[k]) and 0 < face[k] <= 1
                   for k in ('cx', 'cy', 'h')):
            # P1 人脸 auto 检测：显式 face 缺失时自动探测（detect_face.py）
            det = _auto_face(root, pres['src'])
            if det is None:
                raise NeedsBinding('needs-binding: face geometry (cx/cy/h) 且 auto 检测失败')
            pres['face'] = det
            face = det
        p = media_path(root, pres['src'])
        meta = probe(p)
        vs = next((s for s in meta['streams'] if s['codec_type'] == 'video'), None)
        dur = float(meta['format'].get('duration', 0))
        if dur < tl['duration'] - 1 / 60:
            raise NeedsBinding('口播视频必须覆盖整条输出时钟')
        consumed = [{'role': 'presenter', 'sha256': sha(p), 'dimensions': [vs['width'], vs['height']], 'duration': dur}]
    else:
        consumed = []
    music = tl.get('music', '')
    if music:
        p = media_path(root, music)
        meta = probe(p)
        if not any(s['codec_type'] == 'audio' for s in meta['streams']):
            raise NeedsBinding('music 必须是音频文件（或空字符串=无音乐）')
        if float(meta['format'].get('duration', 0)) < tl['duration'] - 1 / 60:
            raise NeedsBinding('配乐必须覆盖整条输出时钟')
        consumed.append({'role': 'music', 'sha256': sha(p)})
    compliance = {'fonts': read(SCHEMAS / 'allowed-fonts.json')['allowed'],
                  'musicLicense': 'user-provided' if music else 'none',
                  'assetsGenerated': []}
    return {'profile': 'dreamweaver-plan/1', 'inputSHA256': sha(source),
            'compliance': compliance,
            'family': tl.get('family'), 'layout': lay,
            'duration_s': tl['duration'], 'frames': round(tl['duration'] * 60),
            'segments': tl['segments'], 'inputs': consumed}

# ---------- prepare ----------
def prepare(input_file, out):
    report = plan(input_file)
    d = read(Path(input_file).resolve())
    tl = d['timeline']
    out = Path(out)
    if out.exists() or out.resolve().is_relative_to(ROOT):
        raise ValueError('Preserve existing directories')
    out.mkdir(parents=True)
    (out / 'user-assets').mkdir()
    bound, media = {}, {}
    # ★人脸裁剪公式（上游 adapter L139-143）：cover 720×1280，face 坐标重映射
    pres = tl.get('presenter') or {}
    if pres.get('src'):
        p = media_path(Path(input_file).resolve().parent, pres['src'])
        meta = probe(p); vs = next(s for s in meta['streams'] if s['codec_type'] == 'video')
        w, h = vs['width'], vs['height']
        # Workbench integration: explicit no-crop mode retains the whole source frame.
        if pres.get('framing') == 'none':
            f2, tracking = {}, []
        else:
            pw, ph = PERSON_BOX.get(report['layout'], (720, 1280))
            scale = max(pw / w, ph / h)
            dx = (w * scale - pw) / 2
            dy = (h * scale - ph) / 2
            face = pres.get('face') or _auto_face(Path(input_file).resolve().parent, pres['src'])
            if not face: raise NeedsBinding('auto 人脸检测失败，请使用保留全画面模式')
            f2 = {'cx': (face['cx'] * w * scale - dx) / pw,
                  'cy': (face['cy'] * h * scale - dy) / ph,
                  'h': face['h'] * h * scale / ph}
            if not all(0 < x < 1 for x in f2.values()):
                raise NeedsBinding('Face falls outside the prepared crop')
            tracking = []
        dest = out / 'user-assets' / 'presenter.mp4'
        pw, ph = PERSON_BOX.get(report['layout'], (720, 1280))
        subprocess.run(['ffmpeg', '-v', 'error', '-n', '-i', str(p),
                        '-vf', (f'scale={pw}:{ph}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1' if pres.get('framing') == 'none' else f'scale={pw}:{ph}:force_original_aspect_ratio=increase,crop={pw}:{ph}'),
                        '-c:v', 'libx264', '-crf', '20', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '128k', str(dest)], check=True)
        # 口播帧序列（12fps JPEG，对齐上游 sc/<role>/f_XXXX.jpg 架构，渲染时按序取帧）
        seqdir = out / 'sc' / 'presenter'
        seqdir.mkdir(parents=True, exist_ok=True)
        sequence_fps = 12
        if pres.get('framing') == 'none':
            try:
                numerator, denominator = vs.get('avg_frame_rate', '24/1').split('/')
                sequence_fps = min(30, max(1, float(numerator) / float(denominator)))
            except (ValueError, ZeroDivisionError): sequence_fps = 24
        subprocess.run(['ffmpeg', '-v', 'error', '-n', '-i', str(dest),
                        '-vf', f'fps={sequence_fps:g}', '-start_number', '0', '-q:v', '3',
                        str(seqdir / 'f_%04d.jpg')], check=True)
        nframes = len(list(seqdir.glob('*.jpg')))
        media['presenter'] = {'mode': 'video', 'file': 'user-assets/presenter.mp4',
                              'sequence': {'dir': 'sc/presenter', 'fps': sequence_fps, 'frames': nframes},
                              'face': f2, 'tracking': tracking, 'framing': pres.get('framing', 'auto')}
        bound['presenter'] = media['presenter']
    for role in ('music',):
        v = tl.get(role, '')
        if v:
            p = media_path(Path(input_file).resolve().parent, v)
            dest = out / 'user-assets' / ('music' + p.suffix.lower())
            shutil.copy2(p, dest)
            media[role] = {'mode': 'file', 'file': str(dest.relative_to(out))}
    _themes = read(SCHEMAS / 'catalog.json')['themes']
    _fam = report['family']
    _mood = tl.get('mood', '')
    _theme_key = f'{_fam}-{_mood}' if _mood and f'{_fam}-{_mood}' in _themes else _fam
    assert _theme_key in _themes, f'theme 未注册: {_theme_key}'
    settings = {'family': _fam, 'layout': report['layout'],
                'duration': report['duration_s'], 'fps': 30,
                'width': layout_size(report['layout'])[0],
                'height': layout_size(report['layout'])[1],
                'segments': tl['segments'], 'theme': _themes[_theme_key], 'mood': _mood,
                'presenterLabel': d.get('header', {}).get('shortTitle', ''),
                'narrationMap': tl.get('narrationMap', []),
                'media': media, 'subtitles': tl.get('subtitles', ''), 'captionsData': tl.get('captionsData', []), 'music': media.get('music', {'mode': 'none'}),
                'openingCard': tl.get('openingCard'), 'captionStyle': tl.get('captionStyle') or {'mode': 'single', 'hiWords': []}}
    save(out / 'settings.json', settings)
    (out / 'user-input.js').write_text('window.USER_INPUT=' + json.dumps(settings, ensure_ascii=False) + ';\n', encoding='utf-8')
    # 运行时装配：core 页面 + runtime 模块 + 字体（对齐上游 copytree CORE 的做法）
    engine = ROOT
    for item in ['index.html', 'bootstrap.js', 'layouts.js', 'family-overrides.js']:
        shutil.copy2(engine / 'core' / item, out / item)
    shutil.copytree(engine / 'core' / 'scenes', out / 'scenes', dirs_exist_ok=True)
    shutil.copytree(engine / 'runtime', out / 'runtime', dirs_exist_ok=True)
    (out / 'assets').mkdir(exist_ok=True)
    for f in ['Anton-Regular.ttf', 'NotoSansSC.ttf']:
        src = ROOT / 'assets' / 'fonts' / f
        if not src.exists():
            raise NeedsBinding('字体缺失: engine/assets/fonts/' + f + '（字体必须随引擎分发）')
        shutil.copy2(src, out / 'assets' / f)
    config = {'brand': 'DREAMWEAVER', 'account': settings['presenterLabel'],
              'fps': 30, 'width': settings['width'], 'height': settings['height'],
              'end': settings['duration'], 'fontChecks': ['900 40px NotoSC', '40px Anton']}
    (out / 'config.js').write_text('window.CONFIG=' + json.dumps(config, ensure_ascii=False) + ';\n', encoding='utf-8')
    save(out / 'user-plan.json', report)
    save(out / 'binding-report.json', {'inputSHA256': report['inputSHA256'],
        'boundFiles': {n: sha(out / n) for n in ['settings.json', 'user-input.js']},
        'media': list(bound.keys())})
    return report

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('command', choices=['plan', 'prepare'])
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--out', type=Path)
    a = ap.parse_args()
    if a.command == 'plan':
        print(json.dumps(plan(a.input), ensure_ascii=False, indent=1)); return
    if not a.out: raise SystemExit('prepare 需要 --out')
    print(json.dumps(prepare(a.input, a.out), ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
