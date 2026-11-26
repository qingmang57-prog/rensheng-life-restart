"""半自动发布包：把一个已完成的任务整理成「可直接拿去抖音创作者中心上传」的目录。

只做本地资料整理与文件落盘：
- 不登录平台、不模拟点击、不代发、不伪造发布回执；
- 成片/封面优先取任务结果里的 artifact，其次取 packages/{job_id} 下的同名产物；
- 缺封面/标题/正文不静默跳过，写进 manifest.json 的 missing 列表并在发布指引里提示。

接线状态：尚未接入 app.py / app/web（这两处有其他线并行改动）；
统一窗口里只需在无副作用的位置调用 build_publish_kit(job_id)。
"""
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_KINDS = ('packaged_video', 'clean_video')
VIDEO_FALLBACKS = ('packaged.mp4', 'packaged_output.mp4', 'video.mp4')
COVER_SUFFIXES = ('.jpg', '.jpeg', '.png')
TITLE_LIMIT = 55
HASHTAG_LIMIT = 10

# 话题提取的极简停用规则：只剔除明显无信息的口语碎片，不引入分词依赖。
# 单字停用只收绝不会构成主题词的虚词；「为/会/有/看/说/时」等会出现在“行为/社会/看法/话题/时间”里，不能一刀切。
STOP_CHARS = set('的了呢吧啊哦嗯呃我你他她它这那哪谁怎么就都也还又很太更最不没可')
STOP_GRAMS = {'我们', '你们', '他们', '她们', '这个', '那个', '什么', '怎么', '为什么', '因为', '所以',
              '但是', '如果', '然后', '可以', '可能', '一个', '没有', '现在', '今天', '明天', '时候',
              '知道', '觉得', '看到', '听到', '说话', '其实', '就是', '还是', '也是', '都是', '不要',
              '不是', '不会', '不能', '这种', '那种', '这样', '那样', '只是', '还有', '而且', '或者',
              '已经', '自己'}
SPLIT_PATTERN = re.compile(r'[^\u4e00-\u9fffA-Za-z0-9]+')

DEFAULT_GUIDE = """# 发布指引 · {title}

> 本目录由个人IP工作台整理，只准备发布资料：不登录你的账号、不模拟点击、不代你发布。

{missing_block}## 第 1 步：打开抖音创作者中心
1. 用电脑浏览器打开 https://creator.douyin.com/ 。
2. 用本人抖音 App 扫码登录；登录后页面左侧出现「内容管理」菜单。
3. 点左侧「内容管理」，在页面右上/顶部找到「发布视频」按钮并点击，进入上传页。
   - 看到中间有大块「点击上传视频」区域，说明已进入正确的发布页。

## 第 2 步：上传 video.mp4 并设置封面
1. 在上传页的大框里点「点击上传」，选中本目录的 `{video_name}` 。
   - 看到视频预览图和上传进度条，等进度条走完再继续。
2. 点「设置封面」或「选择封面」，选择「上传封面」，选中本目录的 `{cover_name}` 。
{cover_note}{cover_fallback}
## 第 3 步：粘贴文案并发布
1. 标题输入框：打开 `title.txt`，全选复制后粘贴。
   - 抖音标题上限 {title_limit} 字；看到标题完整显示即可。
2. 正文与话题：打开 `copy-all.txt`，全选复制，粘贴到「作品描述」输入框。
   - 看到 `#话题` 变成蓝色高亮，说明话题已被识别。
3. 发布前自检：标题不是「未填标题」、描述没有缺字、封面不是黑屏或第 1 帧糊图。
4. 点右下角「发布」；看到「发布成功」提示，并在「内容管理」列表里找到这条作品，即完成。

---

### 本目录文件
{file_list}

### 说明
- 标题、正文、话题可随时在本目录的 txt 里改完再复制，不影响视频。
- 视频是从工作台成片直接链接/复制的，不要在本目录里剪辑后上传（以原成片为准）。
- 这份指引只覆盖抖音创作者中心；其他平台请用对应后台的「发布视频」入口。
"""


def _data_root():
    """每次调用时读环境变量，便于测试/验收切到独立数据目录。"""
    return Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime')))


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _link_or_copy(source, target):
    """同盘优先硬链接（省空间、秒完成），跨盘/权限不足时退回复制。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
        return 'hardlink'
    except OSError:
        shutil.copy2(source, target)
        return 'copy'


def _one_line(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _clean_cover_suffix(path):
    suffix = Path(path).suffix.lower()
    return suffix if suffix in COVER_SUFFIXES else ''


def _normalize_tags(tags):
    result = []
    for tag in tags or []:
        word = _one_line(str(tag)).lstrip('#').strip()
        if word and word not in result:
            result.append(word[:20])
    return result[:HASHTAG_LIMIT]


def _strip_hashtags(text):
    """正文文件不放话题：删掉 #话题 词并按行收敛多余空白。"""
    text = re.sub(r'#[^\s#]+', '', str(text or ''))
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.splitlines()]
    return '\n\n'.join(line for line in lines if line)


def _keyword_tags(text, title='', limit=5):
    """极简中文关键词：只收「在稿件里重复出现」的 2–4 字片段。

    不接 LLM、不引第三方分词。重复出现的短片段通常就是主题词；
    不足 3 个时用标题/正文里被标点隔开的短句兜底，仍不够就如实少给。
    """
    stripped = re.sub(r'#[^\s#]+', ' ', str(text or ''))
    counts = {}
    for run in re.findall(r'[\u4e00-\u9fff]{2,}', stripped):
        for size in range(2, 5):
            for start in range(0, len(run) - size + 1):
                gram = run[start:start + size]
                if gram in STOP_GRAMS or any(ch in STOP_CHARS for ch in gram):
                    continue
                counts[gram] = counts.get(gram, 0) + 1
    repeated = {gram: count for gram, count in counts.items() if count >= 2}
    ranked = sorted(repeated.items(), key=lambda item: (-(item[1] * 3 + len(item[0])), -len(item[0]), item[0]))
    picked = []
    for gram, _count in ranked:
        # 子串/超串只保留更完整的一个，避免「动机」「别人动机」同时出现。
        if any(gram in chosen or chosen in gram for chosen in picked):
            continue
        picked.append(gram)
        if len(picked) >= limit:
            break
    if len(picked) < 3:
        clauses = [part for part in SPLIT_PATTERN.split(str(title or '') + '\n' + stripped)
                   if 2 <= len(part) <= 12]
        for part in clauses:
            if part not in picked and not any(part in chosen or chosen in part for chosen in picked):
                picked.append(part)
                if len(picked) >= limit:
                    break
    return picked[:limit]


def _job_artifact(store, job):
    """任务结果里带的 artifact id；只有本任务产出的才算，避免拿旧成片充数。"""
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    candidate = result.get('artifact') if isinstance(result.get('artifact'), dict) else None
    artifact_id = (candidate or {}).get('id') or result.get('artifact_id')
    return store.get('artifact', artifact_id) if artifact_id else None


def _find_video(store, job, job_id, data_root):
    """成片解析：只认这个任务自己的 artifact，其次 packages/{job_id} 下的同类产物。

    不拿项目里别的成片顶替：发布包必须和用户点下的那个任务一一对应。
    """
    artifact = _job_artifact(store, job)
    if not (artifact and artifact.get('kind') in VIDEO_KINDS and Path(artifact.get('path', '')).is_file()):
        artifact = None
    if artifact:
        return Path(artifact['path']), artifact
    package = data_root / 'packages' / job_id
    for name in VIDEO_FALLBACKS:
        path = package / name
        if path.is_file():
            return path, None
    raise ValueError('这个任务没有可用于发布的成片（既没有成片 artifact，也没有 packages/%s 下的成片文件）。' % job_id)


def _find_cover(store, video_artifact, job_id, data_root):
    """封面解析：成片挂接的 cover artifact → packages/{job_id} 下的 cover 图。"""
    if video_artifact and video_artifact.get('cover_id'):
        cover = store.get('artifact', video_artifact['cover_id'])
        if (cover and cover.get('kind') == 'cover' and cover.get('project_id') == video_artifact.get('project_id')
                and _clean_cover_suffix(cover.get('path', '')) and Path(cover['path']).is_file()):
            return Path(cover['path']), cover
    package = data_root / 'packages' / job_id
    for suffix in COVER_SUFFIXES:
        path = package / ('cover' + suffix)
        if path.is_file():
            return path, None
    return None, None


def _video_info(video_path, artifact):
    info = (artifact or {}).get('info') or {}
    if info.get('width') and info.get('height'):
        return {'duration_ms': info.get('duration_ms'), 'width': info.get('width'), 'height': info.get('height'),
                'source': 'artifact'}
    try:
        import media
        probed = media.probe(video_path)
        return {'duration_ms': probed.get('duration_ms'), 'width': probed.get('width'),
                'height': probed.get('height'), 'source': 'ffprobe'}
    except Exception:
        return {'duration_ms': None, 'width': None, 'height': None, 'source': 'unknown'}


def _clean_previous(out_dir):
    for name in ('video.mp4', 'title.txt', 'caption.txt', 'hashtags.txt', 'copy-all.txt',
                 'manifest.json', '发布指引.md'):
        target = out_dir / name
        if target.exists():
            target.unlink()
    for path in out_dir.glob('cover.*'):
        if path.is_file() and _clean_cover_suffix(path):
            path.unlink()


def build_publish_kit(job_id, out_root=None):
    """按 job_id 生成 publish/{job_id}/ 目录，返回清单与产物路径。

    返回值：
      {'ok': True, 'job_id', 'out_dir', 'files': {...}, 'missing': [...],
       'warnings': [...], 'title', 'tags', 'manifest': {...}}
    """
    from db import Store
    data_root = _data_root()
    store = Store(data_root)
    job = store.get('job', job_id)
    if not job:
        raise KeyError('没有找到该任务：' + str(job_id))
    if job.get('status') != 'succeeded':
        raise ValueError('任务还没完成（当前状态：%s），不能生成发布包。' % job.get('status'))

    project = store.get('project', job.get('project_id') or '') or {}
    pack = project.get('publication_pack') or {}
    version = store.get('script_version', project.get('confirmed_version_id') or '') or None

    video_path, video_artifact = _find_video(store, job, job_id, data_root)
    cover_path, cover_artifact = _find_cover(store, video_artifact, job_id, data_root)
    info = _video_info(video_path, video_artifact)

    missing, warnings = [], []
    # 标题：发布文案包优先，其次作品标题；一行展示，超 55 字截断并提示。
    raw_title = _one_line(pack.get('selected_title') or project.get('title') or '')
    if not raw_title:
        missing.append('title')
        warnings.append('未填标题：title.txt 里是占位「未填标题」，发布前请在抖音标题框补上。')
        title = '未填标题'
    else:
        title = raw_title
        if len(title) > TITLE_LIMIT:
            warnings.append('标题 %d 字，超过抖音上限 %d 字，已截断；请回工作台精简后重新生成。'
                            % (len(title), TITLE_LIMIT))
            title = title[:TITLE_LIMIT]

    # 正文：发布文案包 caption 优先，否则用定稿正文拼起来；不含话题。
    caption = _strip_hashtags(pack.get('caption') or '')
    if not caption and version:
        caption = _strip_hashtags('\n\n'.join(str(p.get('text', '')) for p in version.get('parts', [])
                                               if isinstance(p, dict)))
    if not caption:
        missing.append('caption')
        warnings.append('未填正文：caption.txt 为空，可先在本目录补写，再重新复制 copy-all.txt。')

    # 话题：发布文案包/稿件里已有 #话题 就用现成的；没有才按规则从标题/正文提 3–5 个。
    text_hashtags = re.findall(r'#([^\s#]{2,20})', '\n'.join([pack.get('caption') or '', caption]))
    existing = _normalize_tags(list(pack.get('tags') or project.get('tags') or []) + text_hashtags)
    if existing:
        tags, tags_source = existing, 'existing'
    else:
        tags = _keyword_tags('\n'.join([raw_title, caption]), raw_title, 5)
        tags_source = 'extracted' if tags else 'none'
        if not tags:
            missing.append('hashtags')
            warnings.append('没有可用话题：稿件里也没提到合适关键词，发布时可在描述里手动加 2–3 个。')
        elif len(tags) < 3:
            warnings.append('自动提取的话题只有 %d 个；发布前可在 hashtags.txt 里手动补到 3–5 个。' % len(tags))

    if cover_path is None:
        missing.append('cover')
        warnings.append('没有封面：可在上传页选「推荐封面」或用视频第 1 帧，再点「确定」。')

    out_dir = (Path(out_root) if out_root else data_root / 'publish') / job_id
    # 防止误把输出目录指到成片所在的 packages/{job_id}，清理旧文件时删掉源成片/封面。
    blocked = {video_path.resolve().parent}
    if cover_path:
        blocked.add(cover_path.resolve().parent)
    out_dir = out_dir.resolve()
    if out_dir in blocked:
        raise ValueError('输出目录不能是成片/封面所在的目录；请把 out_root 指到别处。')
    out_dir.mkdir(parents=True, exist_ok=True)
    _clean_previous(out_dir)

    video_name = 'video' + (video_path.suffix.lower() if video_path.suffix.lower() != '.mp4' else '.mp4')
    video_mode = _link_or_copy(video_path, out_dir / video_name)
    files = {video_name: _sha256(out_dir / video_name)}

    cover_name = ''
    if cover_path:
        cover_name = 'cover' + _clean_cover_suffix(cover_path)
        _link_or_copy(cover_path, out_dir / cover_name)
        files[cover_name] = _sha256(out_dir / cover_name)

    title_file = out_dir / 'title.txt'
    title_file.write_text(title + '\n', encoding='utf-8')
    files['title.txt'] = _sha256(title_file)

    caption_file = out_dir / 'caption.txt'
    caption_file.write_text((caption + '\n') if caption else '', encoding='utf-8')
    files['caption.txt'] = _sha256(caption_file)

    tags_text = ' '.join('#' + tag for tag in tags)
    hashtags_file = out_dir / 'hashtags.txt'
    hashtags_file.write_text(('\n'.join('#' + tag for tag in tags) + '\n') if tags else '', encoding='utf-8')
    files['hashtags.txt'] = _sha256(hashtags_file)

    copy_all = title + '\n\n' + caption + '\n\n' + tags_text
    copy_file = out_dir / 'copy-all.txt'
    copy_file.write_text(copy_all + '\n', encoding='utf-8')
    files['copy-all.txt'] = _sha256(copy_file)

    missing_block = ''
    if warnings:
        missing_block = '## 缺件提醒（发布前处理）\n' + '\n'.join('- ' + w for w in warnings) + '\n\n'
    cover_note = ''
    if cover_name:
        cover_info = (cover_artifact or {}).get('info') or {}
        width, height = cover_info.get('width'), cover_info.get('height')
        if isinstance(width, int) and isinstance(height, int) and width and height:
            ratio = width / height
            shape = ''
            if abs(ratio - 0.75) <= 0.05:
                shape = '，竖版 3:4'
            elif abs(ratio - 16 / 9) <= 0.15:
                shape = '，横版 16:9'
            cover_note = '   - 这套封面是 %d×%d%s；看到预览图与视频主体一致后点「确定」。\n' % (width, height, shape)
        else:
            cover_note = '   - 选中封面后看预览图，确认是本人/标题封面再点「确定」。\n'
    cover_fallback = ''
    if not cover_name:
        cover_fallback = ('   - 本目录没有 cover 文件：在封面设置里选「推荐封面」，或拖动时间轴选第 1 帧，'
                          '看到预览后点「确定」。\n')
    file_list = '\n'.join('- `%s`' % name for name in list(files) + ['发布指引.md'])
    guide = DEFAULT_GUIDE.format(title=title, missing_block=missing_block, cover_note=cover_note,
                                 cover_fallback=cover_fallback, video_name=video_name,
                                 cover_name=cover_name or '（无）', title_limit=TITLE_LIMIT,
                                 file_list=file_list)
    guide_file = out_dir / '发布指引.md'
    guide_file.write_text(guide, encoding='utf-8')
    files['发布指引.md'] = _sha256(guide_file)

    manifest = {
        'job_id': job_id,
        'job_kind': job.get('kind'),
        'job_status': job.get('status'),
        'project_id': project.get('id'),
        'script_version_id': project.get('confirmed_version_id'),
        'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'source': {
            'video': {'path': str(video_path), 'artifact_id': (video_artifact or {}).get('id'),
                      'filename': (video_artifact or {}).get('filename'), 'copy_mode': video_mode},
            'cover': ({'path': str(cover_path), 'artifact_id': (cover_artifact or {}).get('id'),
                       'filename': (cover_artifact or {}).get('filename')} if cover_path else None),
        },
        'video': {'filename': video_name, **info},
        'title': {'text': title, 'original_length': len(raw_title), 'limit': TITLE_LIMIT},
        'caption': {'chars': len(caption), 'hashtags_stripped': True},
        'hashtags': {'items': tags, 'source': tags_source},
        'files': files,
        'missing': missing,
        'warnings': warnings,
    }
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    return {'ok': True, 'job_id': job_id, 'out_dir': str(out_dir), 'files': files,
            'missing': missing, 'warnings': warnings, 'title': title, 'tags': tags,
            'manifest': manifest}
