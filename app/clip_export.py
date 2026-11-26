"""送剪映：本机剪映探测/启动 + 「剪映素材包」导出 + 「可编辑草稿」导出。

零第三方依赖，只做本地整理：
- 素材包 = 成片 mp4 + 字幕 SRT + 封面 + 标题/文案/话题 + 使用说明.txt（同目录再打一个 zip）；
- 可编辑草稿 = 剪映明文草稿（draft_info.json/draft_content.json + draft_meta_info.json）：
  成片按已剪好的分段拆成多个片段，字幕还原成文本轨，素材复制进草稿目录，再打 zip；
- 只读记录里的成片与封面，不登录平台、不代发；
- 绝不修改或覆盖用户已有的剪映草稿目录，只在自己数据目录里生成待导入的草稿文件夹。

草稿结构参照 pyJianYingDraft 0.3.1（Apache-2.0）生成的明文草稿实测校准；
剪映 6.0+ 默认把草稿存成加密串，明文草稿能否被本机版本读取见 _DRAFT_NOTE。

路由挂载：本模块自带 route()，由 app.py 的 route() 调用；命中 /api/clip/* 时返回 True。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import aigc_mark
import branding
import release_service

VIDEO_KINDS = ('packaged_video', 'clean_video')
# 隐式标识里的服务提供者名称（GB 45438-2025 附录 E 的 ContentProducer）
AIGC_PRODUCER = '极地信号科技'
FILENAME_RE = re.compile(r'[\\/:*?"<>|]+')
ASS_TIME_RE = re.compile(r'^(\d+):(\d{2}):(\d{2})[.,](\d{2})$')
ASS_TAG_RE = re.compile(r'\{[^}]*\}')

JIANYING_DOWNLOAD = 'https://www.capcut.cn/'

GUIDE = """剪映素材包 · 使用说明

一、导入成片
1. 打开剪映专业版，点「开始创作」新建一个草稿（不要动你原有的草稿）。
2. 把本文件夹里的 {video_name} 拖进下方轨道，原声保留，不要另加滤镜。
3. 如果剪映问是否「自动识别字幕」，选「否」，字幕按下面第二步手动导入。

二、贴字幕
1. 顶部切到「文本」，找到「导入本地字幕」入口。
2. 选择 {srt_name}。
3. 预览一遍：字幕时间轴来自工作台这条成片；个别行不齐时按住字幕块前后拖动即可，行序不用改。

三、封面
发布时把 {cover_name} 上传为封面；也可以在剪映里把它拖到第一帧当封面参考。

四、标题、文案、话题
打开 标题与文案.txt，按「标题」「正文」「话题」分别复制粘贴。抖音标题上限 55 字。

五、说明
- 本素材包只整理文件，不会修改或覆盖你已有的剪映草稿，也不替你发布。
- 缺件提醒：{missing}

生成时间：{generated_at}
"""


_DRAFT_NOTE = ('剪映从 6.0 起把草稿主文件加密存放；这份工程是明文草稿，'
               '剪映 11.4 及部分 6.x 可直接打开并自动升级，11.7 尚未验证。'
               '打不开时请改用「剪映素材包」：成片拖进轨道 + 导入字幕.srt。')

EDIT_GUIDE = """剪映可编辑工程 · 使用说明

一、里面有什么
- 剪映工程/：一份明文剪映草稿，已把成片按分段切成 {clips} 个片段，并还原了 {texts} 条字幕文本。
- 字幕.srt：同一份字幕，草稿打不开时可单独导入。
- 成片与字幕都来自这条作品的已审成片，不改原片。

二、怎么进剪映
1. 打开剪映，右上角「菜单 → 全局设置 → 草稿位置」，记下草稿文件夹路径。
2. 把 剪映工程 这个文件夹整个复制进去（不要只复制里面的 json）。
3. 完全退出剪映再重开，在草稿列表里找「{draft_name}」。
4. 列表里没有：用剪映的「导入草稿」选这个文件夹或本压缩包。
5. 打开后可以改切点、改字幕、替换片段、加音乐，再导出。

三、如果提示「草稿内容已损坏」或列表里始终没有
说明你这一版剪映只认加密草稿（{version}），明文草稿写不进去。
这时请改用「剪映素材包」：把成片拖进轨道，再「导入本地字幕」选 字幕.srt，
效果一样能编辑，只是切点要手动对。

四、说明
- 本工程只在你自己的工作台数据目录里生成，不会改动你已有的剪映草稿。
- {note}

生成时间：{generated_at}
"""


def _data_root_default():
    return Path(os.getenv('IP_DATA_DIR', str(Path(__file__).resolve().parent / '.runtime')))


def _local_appdata():
    return Path(os.getenv('LOCALAPPDATA') or os.path.expanduser('~'))


def _version_key(name):
    parts = [int(x) for x in re.findall(r'\d+', name)]
    return parts or [0]


def _registry_candidates():
    """从卸载表里找剪映安装目录；读注册表失败就当没有，不影响主流程。"""
    results = []
    try:
        import winreg
    except Exception:
        return results
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall',
                                     0, winreg.KEY_READ | view)
            except OSError:
                continue
            for index in range(0, 4096):
                try:
                    sub = winreg.EnumKey(key, index)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(key, sub) as item:
                        display = str(winreg.QueryValueEx(item, 'DisplayName')[0] or '')
                        if not any(word in display for word in ('剪映', 'Jianying', 'CapCut')):
                            continue
                        location = str(winreg.QueryValueEx(item, 'InstallLocation')[0] or '')
                        icon = str(winreg.QueryValueEx(item, 'DisplayIcon')[0] or '')
                except OSError:
                    continue
                for value in (location, icon.split(',')[0]):
                    value = value.strip().strip('"')
                    if value and Path(value).exists():
                        results.append(Path(value))
    return results


def _candidate_dirs():
    roots = [_local_appdata() / 'JianyingPro']
    for var in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432'):
        base = os.getenv(var)
        if base:
            roots.append(Path(base) / 'JianyingPro')
    roots.append(_local_appdata() / 'Programs' / 'JianyingPro')
    return roots


def detect():
    """返回 {'installed','exe','version','download_url'}；找不到就明说，不静默。"""
    exe_candidates = []
    for root in _candidate_dirs():
        if not root.is_dir():
            continue
        apps = root / 'Apps'
        if apps.is_dir():
            # 入口优先用 Apps\JianyingPro.exe：它负责选版本、清理旧包；直接起版本目录里的 exe 在自动升级后会秒退。
            exe = apps / 'JianyingPro.exe'
            if exe.is_file():
                exe_candidates.append(exe)
            versions = sorted((p for p in apps.iterdir() if p.is_dir()), key=lambda p: _version_key(p.name),
                              reverse=True)
            for folder in versions:
                exe = folder / 'JianyingPro.exe'
                if exe.is_file():
                    exe_candidates.append(exe)
        exe = root / 'JianyingPro.exe'
        if exe.is_file():
            exe_candidates.append(exe)
    for path in _registry_candidates():
        if path.is_dir():
            for exe in sorted(path.glob('*.exe')):
                if exe.name.lower() == 'jianyingpro.exe':
                    exe_candidates.append(exe)
        elif path.is_file() and path.name.lower() == 'jianyingpro.exe':
            exe_candidates.append(path)
    if not exe_candidates:
        return {'installed': False, 'exe': None, 'version': None, 'download_url': JIANYING_DOWNLOAD}
    exe = exe_candidates[0]
    version = exe.parent.name
    if not re.match(r'^\d+(\.\d+)+$', version):
        siblings = [p.name for p in exe.parent.iterdir()
                    if p.is_dir() and re.match(r'^\d+(\.\d+)+$', p.name)]
        if siblings:
            version = max(siblings, key=_version_key)
    return {'installed': True, 'exe': str(exe), 'version': version, 'download_url': JIANYING_DOWNLOAD}


def _jianying_running():
    if sys.platform != 'win32':
        return False
    try:
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq JianyingPro.exe', '/NH'],
                                capture_output=True, text=True, errors='replace', timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return 'JianyingPro.exe' in (result.stdout or '')
    except Exception:
        return False


def open_app():
    state = detect()
    if not state['installed']:
        return {'ok': False, 'code': 'not_installed', 'message': '这台电脑没找到剪映，先装一下再回来点。',
                'download_url': JIANYING_DOWNLOAD}
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    try:
        process = subprocess.Popen([state['exe']], cwd=str(Path(state['exe']).parent),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    except OSError as exc:
        return {'ok': False, 'code': 'launch_failed', 'message': '剪映没启动起来：%s' % exc,
                'exe': state['exe'], 'download_url': JIANYING_DOWNLOAD}
    # Apps\JianyingPro.exe 只是启动器，拉起主程序后自己就退出，所以不能只看子进程存活；
    # 等 6 秒按进程名确认剪映真的在跑，避免升级失败时假报成功。
    import time
    time.sleep(6)
    if not _jianying_running():
        return {'ok': False, 'code': 'launch_exited', 'message': '剪映启动后马上退出了，可能在升级或安装不完整；手动再开一次试试。',
                'exe': state['exe'], 'version': state['version'], 'download_url': JIANYING_DOWNLOAD}
    return {'ok': True, 'message': '剪映已打开；接着新建草稿，把素材包里的成片拖进去。',
            'exe': state['exe'], 'version': state['version']}


# ---------------------------------------------------------------- 素材包
def _safe_name(value, fallback='素材'):
    text = FILENAME_RE.sub('_', str(value or '')).strip(' ._')
    return (text[:40] or fallback)


def _ass_clock(value):
    match = ASS_TIME_RE.match(value.strip())
    if not match:
        raise ValueError(value)
    hour, minute, second, centi = (int(x) for x in match.groups())
    return ((hour * 60 + minute) * 60 + second) * 1000 + centi * 10


def _srt_clock(ms):
    ms = max(0, int(round(ms)))
    return '%02d:%02d:%02d,%03d' % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def _srt_from_cues(cues):
    return '\n\n'.join('%d\n%s --> %s\n%s' % (index + 1, _srt_clock(cue.get('start_ms', 0)),
                                             _srt_clock(cue.get('end_ms', 0)), str(cue.get('text', '')).strip())
                       for index, cue in enumerate(cues) if str(cue.get('text', '')).strip())


def _srt_from_ass(path):
    """把成片自带的 ASS 字幕转成 SRT（只取 Caption 行，去掉花字标签）。"""
    lines, in_events = [], False
    text = Path(path).read_text(encoding='utf-8-sig', errors='replace')
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('['):
            in_events = line.lower().startswith('[events]')
            continue
        if not in_events or not line.lower().startswith('dialogue:'):
            continue
        body = line.split(':', 1)[1].strip()
        fields = body.split(',', 9)
        if len(fields) < 10:
            continue
        style, start, end, content = fields[3].strip(), fields[1].strip(), fields[2].strip(), fields[9]
        if style.lower() != 'caption':
            continue
        content = ASS_TAG_RE.sub('', content).replace(r'\N', ' ').replace(r'\n', ' ').strip()
        if not content:
            continue
        lines.append((_ass_clock(start), _ass_clock(end), content))
    return '\n\n'.join('%d\n%s --> %s\n%s' % (index + 1, _srt_clock(start), _srt_clock(end), content)
                       for index, (start, end, content) in enumerate(lines))


def _subtitle_text(store, prepared, artifact):
    """字幕来源优先级：成片自带 .srt → 自带 .ass 转换 → 成片 caption_cues → 按讲稿估算。"""
    subtitle = Path(str(artifact.get('subtitle_path') or '')) if artifact else None
    if subtitle and subtitle.is_file():
        if subtitle.suffix.lower() == '.srt':
            return subtitle.read_text(encoding='utf-8-sig', errors='replace'), '成片自带 SRT', None
        if subtitle.suffix.lower() == '.ass':
            body = _srt_from_ass(subtitle)
            if body:
                return body, '成片自带 ASS 已转 SRT', None
    cues = (artifact or {}).get('caption_cues') or []
    text = _srt_from_cues(cues)
    if text:
        return text, '成片字幕时间轴', None
    try:
        import read_script
        version = store.get('script_version', prepared.get('script_version_id') or '')
        if version:
            body = read_script.srt(read_script.build_captions(version))
            if body:
                return body, '按讲稿估算时间轴', '字幕是按讲稿字数估算的，导入后需按实际口播微调时间。'
    except Exception as exc:
        return '', None, '字幕生成失败：%s' % exc
    return '', None, '这条成片没有字幕来源，素材包只含视频与文案。'


def _billing():
    import billing_route
    return billing_route


def _export_gate(project_id, channel='clip_pack'):
    """免费额度闸门（G4）：拦截返回 dict，放行返回 None。计费模块异常时放行，
    不能让计费故障把导出整个堵死。channel 见 billing_route.EXPORT_CHANNELS。"""
    try:
        return _billing().export_gate(project_id, channel)
    except Exception:  # noqa: BLE001
        return None


def _export_cancel(project_id, channel='clip_pack'):
    try:
        _billing().export_cancel(project_id, channel)
    except Exception:  # noqa: BLE001
        pass


def _export_max_height():
    """分辨率上限：会员取本机可用的最高清晰度（不限高）；试用/到期才回落 720p。

    云端 entitlements.max_height=None 表示不限；这里再按会员状态兜一层，
    避免云端漏发或旧快照仍带 1080/720 时把会员的清晰度压下去。
    """
    try:
        svc = _billing().service()
        member = svc.membership()
        feat = member.get('features') or {}
        if (member.get('active') or feat.get('vip_service') or feat.get('asr_unlimited')
                or feat.get('max_height') is None):
            return None
        return feat.get('max_height')
    except Exception:  # noqa: BLE001
        return None


def _ffmpeg_exe(name):
    root = Path(__file__).resolve().parent.parent
    for candidate in (root / 'tools' / 'ffmpeg' / 'bin' / (name + '.exe'),
                      root / 'tools' / 'ffmpeg' / 'bin' / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def _probe_height(path):
    exe = _ffmpeg_exe('ffprobe')
    if not exe:
        return 0
    try:
        proc = subprocess.run([exe, '-v', 'error', '-select_streams', 'v:0',
                               '-show_entries', 'stream=height', '-of', 'csv=p=0', str(path)],
                              capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return int((proc.stdout or '').strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0


def _limit_height(path, max_height):
    """免费档导出限高：超过上限就重编码到 max_height，失败保留原片。"""
    if not max_height:
        return path, False
    height = _probe_height(path)
    if not height or height <= int(max_height):
        return path, False
    exe = _ffmpeg_exe('ffmpeg')
    if not exe:
        return path, False
    temp = path.with_name(path.stem + '.720p' + path.suffix)
    try:
        proc = subprocess.run([exe, '-y', '-i', str(path), '-vf', 'scale=-2:%d' % int(max_height),
                               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                               '-c:a', 'copy', str(temp)],
                              capture_output=True, text=True, timeout=1200,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode == 0 and temp.is_file() and temp.stat().st_size > 0:
            temp.replace(path)
            return path, True
    except Exception:  # noqa: BLE001
        pass
    if temp.exists():
        try:
            temp.unlink()
        except OSError:
            pass
    return path, False


def prepare_download(source, data_dir, max_height=None, project_id=''):
    """导出用的成片副本：按档位限高 + 写入 AIGC 隐式标识，原片不动。

    副本放 <数据目录>/export_cache，源文件或清晰度档位变化时重建。
    标识写入失败直接抛错——隐式标识不可关闭，不带标识的成片不能出门。
    """
    src = Path(source)
    if not src.is_file():
        raise ValueError('成片文件不存在或已被清理，请重新生成成片。')
    src_tag = hashlib.sha1(str(src).encode('utf-8')).hexdigest()[:8]
    version = '%s-%s' % (int(max_height or 0), src.stat().st_mtime_ns)
    cache = Path(data_dir) / 'export_cache'
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / ('%s.%s.%s%s' % (src.stem, src_tag, version, src.suffix))
    if target.is_file() and target.stat().st_size > 0:
        try:
            if aigc_mark.has_mark(target):
                return target, False
        except Exception:  # noqa: BLE001
            try:
                target.unlink()  # 缓存破损时删掉重建，避免一次损坏变成永久失败
            except OSError:
                pass
    building = cache / ('%s.%s.%s.building%s' % (src.stem, src_tag, version, src.suffix))
    shutil.copy2(src, building)
    downgraded = False
    if max_height:
        _, downgraded = _limit_height(building, max_height)
    try:
        aigc_mark.stamp(str(building), producer=AIGC_PRODUCER, produce_id=project_id)
    except Exception as exc:  # noqa: BLE001
        try:
            building.unlink()
        except OSError:
            pass
        raise ValueError('成片隐式标识写入失败，已停止导出：%s' % exc)
    building.replace(target)
    return target, downgraded


def build_pack(store, data_dir, project_id, max_height=None):
    prepared = release_service.prepare(store, project_id)
    if not prepared.get('ready'):
        raise ValueError('当前文稿还没有定稿，先确认版本再导出剪映素材包。')
    artifact_id = prepared.get('artifact_id')
    if not artifact_id:
        raise ValueError('这条作品还没有已审成片，先在「编辑成片」里审阅通过再导出。')
    artifact = store.get('artifact', artifact_id) or {}
    if artifact.get('kind') not in VIDEO_KINDS or not Path(str(artifact.get('path') or '')).is_file():
        raise ValueError('成片文件不存在或已被清理，请重新生成成片。')
    video_path = Path(artifact['path'])
    cover = store.get('artifact', prepared.get('cover_id') or '') if prepared.get('cover_id') else None

    out_dir = Path(data_dir) / 'clip_packs' / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in list(out_dir.iterdir()):
        # 只清上一版散件；zip 可能正被浏览器下载，删了会中断下载或在 Windows 上抛占用错误。
        if path.is_file() and path.suffix.lower() not in ('.zip', '.tmp'):
            path.unlink()

    missing, warnings = [], []
    video_name = '成片' + (video_path.suffix.lower() if video_path.suffix.lower() != '.mp4' else '.mp4')
    shutil.copy2(video_path, out_dir / video_name)
    downgraded = False
    if max_height:
        _, downgraded = _limit_height(out_dir / video_name, max_height)
        if downgraded:
            warnings.append('免费版导出上限 %dp，成片已按此分辨率转码；升级会员可导出原分辨率。'
                            % int(max_height))
    try:
        aigc_mark.stamp(str(out_dir / video_name), producer=AIGC_PRODUCER, produce_id=project_id)
    except Exception as exc:  # noqa: BLE001
        raise ValueError('成片隐式标识写入失败，已停止导出：%s' % exc)

    cover_name = ''
    if cover and Path(str(cover.get('path') or '')).is_file():
        cover_name = '封面' + Path(cover['path']).suffix.lower()
        shutil.copy2(cover['path'], out_dir / cover_name)
    else:
        missing.append('封面')
        warnings.append('这条成片没有挂封面；素材包里不含封面图，发布时用视频首帧或剪映封面模板。')

    srt_text, srt_source, srt_warning = _subtitle_text(store, prepared, artifact)
    srt_name = ''
    if srt_text:
        srt_name = '字幕.srt'
        (out_dir / srt_name).write_text(srt_text + '\n', encoding='utf-8-sig')
        if srt_warning:
            warnings.append(srt_warning)
    else:
        missing.append('字幕')
        warnings.append(srt_warning or '没有字幕文件。')

    title = str(prepared.get('selected_title') or '').strip()
    caption = str(prepared.get('caption') or '').strip()
    tags = ' '.join('#' + str(tag).lstrip('#') for tag in prepared.get('tags') or [])
    if not title:
        missing.append('标题')
    if not caption:
        missing.append('正文')
    copy_text = ('标题\n%s\n\n正文\n%s\n\n话题\n%s\n' % (title or '（未填）', caption or '（未填）', tags or '（未填）'))
    (out_dir / '标题与文案.txt').write_text(copy_text, encoding='utf-8-sig')

    import datetime
    (out_dir / '使用说明.txt').write_text(
        GUIDE.format(video_name=video_name, srt_name=srt_name or '字幕.srt', cover_name=cover_name or '封面图',
                     missing='、'.join(missing) if missing else '无',
                     generated_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds')),
        encoding='utf-8')

    files = []
    for path in sorted(out_dir.iterdir()):
        if path.is_file() and path.suffix.lower() != '.zip':
            files.append({'name': path.name, 'size': path.stat().st_size})

    stem = _safe_name(branding.file_stem(store, title, '剪映素材包'), '剪映素材包')
    zip_path = out_dir / (project_id + '.zip')
    temporary = out_dir / (project_id + '.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.write(out_dir / item['name'], item['name'])
    try:
        temporary.replace(zip_path)
    except OSError:
        # 上一份 zip 正被下载时 Windows 不允许替换：另存一份带时间戳的，旧文件留着让对方下完。
        zip_path = out_dir / ('%s-%s.zip' % (project_id, datetime.datetime.now().strftime('%H%M%S%f')))
        temporary.replace(zip_path)

    return {'ok': True, 'project_id': project_id, 'dir': str(out_dir), 'zip': str(zip_path),
            'zip_name': stem + '.zip', 'zip_size': zip_path.stat().st_size,
            'files': files, 'missing': missing, 'warnings': warnings,
            'subtitle_source': srt_source, 'title': title,
            'resolution': {'max_height': max_height, 'downgraded': downgraded}}


# ---------------------------------------------------------------- 可编辑草稿
def _uid():
    import uuid
    return uuid.uuid4().hex


def _srt_cues(text):
    """把 SRT 文本解析成 [{start_ms,end_ms,text}]；解析不了就返回空表。"""
    cues = []
    for block in re.split(r'\n\s*\n', (text or '').replace('\r\n', '\n').strip()):
        lines = [line for line in block.split('\n') if line.strip()]
        if len(lines) < 2 or '-->' not in lines[0 if '-->' in lines[0] else 1]:
            continue
        index = 0 if '-->' in lines[0] else 1
        try:
            start, end = [part.strip() for part in lines[index].split('-->')[:2]]
            cues.append({'start_ms': _parse_srt_clock(start), 'end_ms': _parse_srt_clock(end),
                         'text': ' '.join(lines[index + 1:]).strip()})
        except ValueError:
            continue
    return [cue for cue in cues if cue['text']]


def _parse_srt_clock(value):
    match = re.match(r'^(\d+):(\d{2}):(\d{2})[.,](\d{1,3})$', value.strip())
    if not match:
        raise ValueError(value)
    hour, minute, second, milli = match.groups()
    return ((int(hour) * 60 + int(minute)) * 60 + int(second)) * 1000 + int(milli.ljust(3, '0'))


def _cues(store, prepared, artifact):
    """字幕时间轴：成片 cue → 自带 srt → 自带 ass。都没有就返回空表，不编时间。"""
    cues = [{'start_ms': int(c.get('start_ms') or 0), 'end_ms': int(c.get('end_ms') or 0),
             'text': str(c.get('text') or '').strip()}
            for c in (artifact.get('caption_cues') or [])]
    cues = [c for c in cues if c['text'] and c['end_ms'] > c['start_ms']]
    if cues:
        return cues, '成片字幕时间轴'
    subtitle = Path(str(artifact.get('subtitle_path') or ''))
    if subtitle.is_file():
        if subtitle.suffix.lower() == '.srt':
            body = subtitle.read_text(encoding='utf-8-sig', errors='replace')
        else:
            body = _srt_from_ass(subtitle)
        cues = _srt_cues(body)
        if cues:
            return cues, '成片自带字幕文件'
    return [], ''


def _timeline(artifact):
    """按成片 mapping 的输出去向拆片；没有 mapping 就整条一个片段。"""
    duration = int((artifact.get('info') or {}).get('duration_ms') or 0)
    clips, cursor = [], 0
    for item in artifact.get('mapping') or []:
        try:
            start = int(item.get('output_start_ms') or 0)
            end = int(item.get('output_end_ms') or 0)
        except (TypeError, ValueError):
            continue
        if end - start < 100:
            continue
        clips.append({'source_start_ms': start, 'duration_ms': end - start})
    if not clips:
        if duration <= 0:
            return []
        clips = [{'source_start_ms': 0, 'duration_ms': duration}]
    for clip in clips:
        clip['target_start_ms'] = cursor
        cursor += clip['duration_ms']
    return clips


def _text_content(text):
    return json.dumps({
        'styles': [{'fill': {'alpha': 1.0, 'content': {'render_type': 'solid',
                                                      'solid': {'alpha': 1.0, 'color': [1.0, 1.0, 1.0]}}},
                    'range': [0, len(text)], 'size': 8.0, 'bold': False, 'italic': False,
                    'underline': False, 'strokes': []}],
        'text': text}, ensure_ascii=False, separators=(',', ':'))


def _video_template(width, height):
    return {
        'canvas_config': {'width': width, 'height': height, 'ratio': 'original'},
        'color_space': 0,
        'config': {
            'adjust_max_index': 1, 'attachment_info': [], 'combination_max_index': 1,
            'export_range': None, 'extract_audio_last_index': 1, 'lyrics_recognition_id': '',
            'lyrics_sync': True, 'lyrics_taskinfo': [], 'maintrack_adsorb': True,
            'material_save_mode': 0, 'multi_language_current': 'none', 'multi_language_list': [],
            'multi_language_main': 'none', 'multi_language_mode': 'none',
            'original_sound_last_index': 1, 'record_audio_last_index': 1,
            'sticker_max_index': 1, 'subtitle_keywords_config': None, 'subtitle_recognition_id': '',
            'subtitle_sync': True, 'subtitle_taskinfo': [], 'system_font_list': [],
            'video_mute': False, 'zoom_info_params': None},
        'cover': None, 'create_time': 0, 'duration': 0, 'extra_info': None, 'fps': 30,
        'free_render_index_mode_on': False, 'group_container': None, 'id': _uid().upper(),
        'keyframe_graph_list': [],
        'keyframes': {'adjusts': [], 'audios': [], 'effects': [], 'filters': [], 'handwrites': [],
                      'stickers': [], 'texts': [], 'videos': []},
        'last_modified_platform': {'app_id': 3704, 'app_source': 'lv', 'app_version': '5.9.0',
                                   'os': 'windows'},
        'platform': {'app_id': 3704, 'app_source': 'lv', 'app_version': '5.9.0', 'os': 'windows'},
        'materials': {
            'ai_translates': [], 'audio_balances': [], 'audio_effects': [], 'audio_fades': [],
            'audio_track_indexes': [], 'audios': [], 'beats': [], 'canvases': [], 'chromas': [],
            'common_mask': [], 'color_curves': [], 'digital_humans': [], 'drafts': [],
            'effects': [], 'flowers': [], 'green_screens': [], 'handwrites': [], 'hsl': [],
            'images': [], 'log_color_wheels': [], 'loudnesses': [], 'manual_deformations': [],
            'masks': [], 'material_animations': [], 'material_colors': [], 'multi_language_refs': [],
            'placeholders': [], 'plugin_effects': [], 'primary_color_wheels': [],
            'realtime_denoises': [], 'shapes': [], 'smart_crops': [], 'smart_relights': [],
            'sound_channel_mappings': [], 'speeds': [], 'stickers': [], 'tail_leaders': [],
            'text_templates': [], 'texts': [], 'time_marks': [], 'transitions': [],
            'video_effects': [], 'video_trackings': [], 'videos': [], 'vocal_beautifys': [],
            'vocal_separations': []},
        'mutable_config': None, 'name': '', 'new_version': '110.0.0', 'relationships': [],
        'render_index_track_mode_on': False, 'retouch_cover': None, 'source': 'default',
        'static_cover_image_path': '', 'time_marks': None, 'tracks': [], 'update_time': 0,
        'version': 360000}


def _video_material(path, width, height, duration_ms):
    material_id = _uid()
    name = Path(path).name
    return {
        'audio_fade': None, 'category_id': '', 'category_name': 'local', 'check_flag': 63487,
        'crop': {'upper_left_x': 0.0, 'upper_left_y': 0.0, 'upper_right_x': 1.0,
                 'upper_right_y': 0.0, 'lower_left_x': 0.0, 'lower_left_y': 1.0,
                 'lower_right_x': 1.0, 'lower_right_y': 1.0},
        'crop_ratio': 'free', 'crop_scale': 1.0, 'duration': duration_ms * 1000, 'height': height,
        'id': material_id, 'local_material_id': '', 'material_id': material_id,
        'material_name': name, 'media_path': '', 'path': str(path), 'type': 'video', 'width': width}


def _video_segment(material_id, speed_id, clip, render_index=0):
    return {
        'enable_adjust': True, 'enable_color_correct_adjust': False, 'enable_color_curves': True,
        'enable_color_match_adjust': False, 'enable_color_wheels': True, 'enable_lut': True,
        'enable_smart_color_adjust': False, 'last_nonzero_volume': 1.0, 'reverse': False,
        'track_attribute': 0, 'track_render_index': 0, 'visible': True, 'id': _uid(),
        'material_id': material_id,
        'target_timerange': {'start': clip['target_start_ms'] * 1000,
                             'duration': clip['duration_ms'] * 1000},
        'common_keyframes': [], 'keyframe_refs': [],
        'source_timerange': {'start': clip['source_start_ms'] * 1000,
                             'duration': clip['duration_ms'] * 1000},
        'speed': 1.0, 'volume': 1.0, 'extra_material_refs': [speed_id], 'is_tone_modify': False,
        'clip': {'alpha': 1.0, 'flip': {'horizontal': False, 'vertical': False}, 'rotation': 0.0,
                 'scale': {'x': 1.0, 'y': 1.0}, 'transform': {'x': 0.0, 'y': 0.0}},
        'uniform_scale': {'on': True, 'value': 1.0},
        'hdr_settings': {'intensity': 1.0, 'mode': 1, 'nits': 1000}, 'render_index': render_index}


def _text_material(text):
    return {'id': _uid(), 'content': _text_content(text), 'typesetting': 0, 'alignment': 0,
            'letter_spacing': 0.0, 'line_spacing': 0.02, 'line_feed': 1, 'line_max_width': 0.82,
            'force_apply_line_max_width': False, 'check_flag': 7, 'type': 'text',
            'global_alpha': 1.0}


def _text_segment(material_id, cue, render_index=1):
    return {
        'enable_adjust': True, 'enable_color_correct_adjust': False, 'enable_color_curves': True,
        'enable_color_match_adjust': False, 'enable_color_wheels': True, 'enable_lut': True,
        'enable_smart_color_adjust': False, 'last_nonzero_volume': 1.0, 'reverse': False,
        'track_attribute': 0, 'track_render_index': 0, 'visible': True, 'id': _uid(),
        'material_id': material_id,
        'target_timerange': {'start': int(cue['start_ms']) * 1000,
                             'duration': int(cue['end_ms'] - cue['start_ms']) * 1000},
        'common_keyframes': [], 'keyframe_refs': [], 'source_timerange': None, 'speed': 1.0,
        'volume': 1.0, 'extra_material_refs': [], 'is_tone_modify': False,
        'clip': {'alpha': 1.0, 'flip': {'horizontal': False, 'vertical': False}, 'rotation': 0.0,
                 'scale': {'x': 1.0, 'y': 1.0}, 'transform': {'x': 0.0, 'y': 0.0}},
        'uniform_scale': {'on': True, 'value': 1.0}, 'render_index': render_index}


def _draft_meta(draft_name, fold_path, root_path, duration_ms):
    return {
        'cloud_package_completed_time': '', 'draft_cloud_capcut_purchase_info': '',
        'draft_cloud_last_action_download': False, 'draft_cloud_materials': [],
        'draft_cloud_purchase_info': '', 'draft_cloud_template_id': '',
        'draft_cloud_tutorial_info': '', 'draft_cloud_videocut_purchase_info': '',
        'draft_cover': '', 'draft_deeplink_url': '',
        'draft_enterprise_info': {'draft_enterprise_extra': '', 'draft_enterprise_id': '',
                                  'draft_enterprise_name': '', 'enterprise_material': []},
        'draft_fold_path': str(fold_path), 'draft_id': _uid().upper(),
        'draft_is_ai_packaging_used': False, 'draft_is_ai_shorts': False,
        'draft_is_ai_translate': False, 'draft_is_article_video_draft': False,
        'draft_is_from_deeplink': 'false', 'draft_is_invisible': False,
        'draft_materials': [{'type': t, 'value': []} for t in (0, 1, 2, 3, 6, 7, 8)],
        'draft_materials_copied_info': [], 'draft_name': draft_name, 'draft_new_version': '',
        'draft_removable_storage_device': '', 'draft_root_path': str(root_path),
        'draft_segment_extra_info': [], 'draft_type': '',
        'tm_draft_cloud_completed': '', 'tm_draft_cloud_modified': 0,
        'tm_draft_removed': 0, 'tm_duration': duration_ms * 1000}


def build_edit_project(store, data_dir, project_id):
    """生成一份能在剪映里继续改的明文草稿（按分段拆片 + 字幕文本轨），并打 zip。

    闸门与素材包一致：文稿定稿 + 成片已审阅，缺一项直接报人话错误。
    """
    prepared = release_service.prepare(store, project_id)
    if not prepared.get('ready'):
        raise ValueError('当前文稿还没有定稿，先确认版本再导出可编辑工程。')
    artifact_id = prepared.get('artifact_id')
    if not artifact_id:
        raise ValueError('这条作品还没有已审成片，先在「编辑成片」里审阅通过再导出。')
    artifact = store.get('artifact', artifact_id) or {}
    video_path = Path(str(artifact.get('path') or ''))
    if artifact.get('kind') not in VIDEO_KINDS or not video_path.is_file():
        raise ValueError('成片文件不存在或已被清理，请重新生成成片。')

    info = artifact.get('info') or {}
    width, height = int(info.get('width') or 1080), int(info.get('height') or 1920)
    duration_ms = int(info.get('duration_ms') or 0)
    clips = _timeline(artifact)
    if not clips:
        raise ValueError('这条成片没有可用的时长信息，暂时拆不出可编辑片段。')
    cues, cue_source = _cues(store, prepared, artifact)
    total_ms = sum(clip['duration_ms'] for clip in clips)
    cues = [cue for cue in cues if cue['start_ms'] < total_ms]
    for cue in cues:
        cue['end_ms'] = min(cue['end_ms'], total_ms)

    out_dir = Path(data_dir) / 'edit_projects' / project_id
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    draft_name = _safe_name(prepared.get('selected_title') or prepared.get('title') or '剪映工程', '剪映工程')
    draft_dir = out_dir / draft_name
    media_dir = draft_dir / 'materials'
    media_dir.mkdir(parents=True, exist_ok=True)
    media_path = media_dir / ('成片' + (video_path.suffix.lower() or '.mp4'))
    shutil.copy2(video_path, media_path)

    draft = _video_template(width, height)
    media = _video_material(media_path, width, height, duration_ms)
    speed = {'curve_speed': None, 'id': _uid(), 'mode': 0, 'speed': 1.0, 'type': 'speed'}
    draft['materials']['videos'] = [media]
    draft['materials']['speeds'] = [speed]
    draft['tracks'].append({
        'attribute': 0, 'flag': 0, 'id': _uid(), 'is_default_name': False, 'name': 'video',
        'segments': [_video_segment(media['id'], speed['id'], clip) for clip in clips],
        'type': 'video'})
    if cues:
        texts = [_text_material(cue['text']) for cue in cues]
        draft['materials']['texts'] = texts
        draft['tracks'].append({
            'attribute': 0, 'flag': 0, 'id': _uid(), 'is_default_name': False, 'name': 'text',
            'segments': [_text_segment(material['id'], cue) for material, cue in zip(texts, cues)],
            'type': 'text'})
    draft['duration'] = total_ms * 1000
    draft['name'] = draft_name

    payload = json.dumps(draft, ensure_ascii=False, indent=1)
    # 6.0+ 用 draft_info.json；部分版本仍按 draft_content.json 找，两个都写同样的明文。
    for filename in ('draft_info.json', 'draft_content.json'):
        (draft_dir / filename).write_text(payload, encoding='utf-8')
    (draft_dir / 'draft_meta_info.json').write_text(
        json.dumps(_draft_meta(draft_name, draft_dir, out_dir, total_ms), ensure_ascii=False, indent=1),
        encoding='utf-8')

    srt_text = _srt_from_cues(cues)
    if srt_text:
        (draft_dir / '字幕.srt').write_text(srt_text + '\n', encoding='utf-8-sig')

    state = detect()
    import datetime
    (draft_dir / '导入说明.txt').write_text(
        EDIT_GUIDE.format(clips=len(clips), texts=len(cues), draft_name=draft_name,
                          version=state.get('version') or '未知版本', note=_DRAFT_NOTE,
                          generated_at=datetime.datetime.now().astimezone().isoformat(timespec='seconds')),
        encoding='utf-8')

    files = []
    for path in sorted(draft_dir.rglob('*')):
        if path.is_file():
            files.append({'name': str(path.relative_to(draft_dir)).replace('\\', '/'),
                          'size': path.stat().st_size})

    stem = _safe_name(branding.file_stem(store, prepared.get('selected_title'), '剪映工程'), '剪映工程')
    zip_path = out_dir / (project_id + '.zip')
    temporary = out_dir / (project_id + '.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.write(draft_dir / item['name'], draft_name + '/' + item['name'])
    temporary.replace(zip_path)

    warnings = []
    if not cues:
        warnings.append('这条成片没有字幕时间轴，草稿里只有视频轨。')
    if len(clips) <= 1:
        warnings.append('这条成片没有分段信息，草稿里是整条一个片段，切点需要手动加。')
    warnings.append(_DRAFT_NOTE)
    return {'ok': True, 'project_id': project_id, 'draft_name': draft_name, 'draft_dir': str(draft_dir),
            'dir': str(out_dir), 'zip': str(zip_path), 'zip_name': stem + '.zip',
            'zip_size': zip_path.stat().st_size, 'files': files, 'clips': len(clips),
            'texts': len(cues), 'duration_ms': total_ms, 'subtitle_source': cue_source,
            'jianying_version': state.get('version'), 'warnings': warnings, 'note': _DRAFT_NOTE}


# ---------------------------------------------------------------- HTTP 路由
def route(handler, method, bits, query, store, data_dir):
    """挂在 app.py 的 route() 上；只处理 /api/clip/*，命中返回 True。"""
    if not bits or bits[:2] != ['api', 'clip']:
        return False
    path = '/' + '/'.join(bits)

    def q(name, default=''):
        return (query.get(name) or [default])[0]

    if path == '/api/clip/jianying' and method == 'GET':
        handler.json(detect())
        return True
    if path == '/api/clip/jianying/open' and method == 'POST':
        handler.json(open_app())
        return True
    if path == '/api/clip/pack' and method == 'POST':
        project_id = str(handler.body().get('project_id') or '').strip()
        if not project_id:
            raise ValueError('请先选择一条作品，再导出素材包。')
        blocked = _export_gate(project_id)
        if blocked:
            handler.json(blocked, 403)
            return True
        try:
            result = build_pack(store, data_dir, project_id, max_height=_export_max_height())
        except Exception:
            _export_cancel(project_id)
            raise
        handler.json(result, 201)
        return True
    if path == '/api/clip/pack' and method == 'GET':
        project_id = q('project_id').strip()
        if not project_id:
            raise ValueError('请先选择一条作品，再导出素材包。')
        blocked = _export_gate(project_id)
        if blocked:
            handler.json(blocked, 403)
            return True
        try:
            result = build_pack(store, data_dir, project_id, max_height=_export_max_height())
        except Exception:
            _export_cancel(project_id)
            raise
        if q('download') == '1':
            handler.send_file(Path(result['zip']), download=True, filename=result['zip_name'])
        else:
            handler.json(result)
        return True
    if path == '/api/clip/edit-project' and method == 'POST':
        project_id = str(handler.body().get('project_id') or '').strip()
        if not project_id:
            raise ValueError('请先选择一条作品，再导出可编辑工程。')
        handler.json(build_edit_project(store, data_dir, project_id), 201)
        return True
    if path == '/api/clip/edit-project' and method == 'GET':
        project_id = q('project_id').strip()
        if not project_id:
            raise ValueError('请先选择一条作品，再导出可编辑工程。')
        result = build_edit_project(store, data_dir, project_id)
        if q('download') == '1':
            handler.send_file(Path(result['zip']), download=True, filename=result['zip_name'])
        else:
            handler.json(result)
        return True
    raise ValueError('接口不存在。')
