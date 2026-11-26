# -*- coding: utf-8 -*-
"""人生重启系统 · 全功能一键安装器（任何 AI 助手可按 INSTALL.md 契约直接执行）。

回答两个问题：缺什么（自检清单）、从哪里补（三级来源）：
  1 本机已有 / 旧安装目录 / 网盘完整包解压目录  --from-package DIR（零下载）
  2 本地组件包 zip                            --offline-dir DIR
  3 镜像下载：官网 → GitHub Release（断点续传 + sha256 校验）
不在客户机跑 pip / npm：venv、node_modules、浏览器、模型全部随组件包交付。

用法：
  python tools/setup_full.py                     # 检测→补齐→自愈→自检
  python tools/setup_full.py --check             # 只自检，报告缺什么、从哪里补
  python tools/setup_full.py --from-package <旧安装目录>
  python tools/setup_full.py --migrate-from <旧安装目录>   # 老客户答题信息+认知卡承接
退出码：0 全齐 / 1 自检有缺 / 2 用法或 Python 版本 / 3 下载或校验失败
全程无交互提问；日志 tools/setup_full.log；子进程一律隐藏窗口。
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0
SOURCE_EXTS = {'.py', '.js', '.mjs', '.cjs', '.json', '.md', '.html', '.css', '.txt',
               '.ts', '.svg', '.yaml', '.yml', '.lock', '.cfg', '.bat', '.ps1', '.vbs', '.gitignore'}
SOURCE_MAX = 2 * 1024 * 1024
SKIP_DIRS = {'node_modules', '__pycache__', '.git'}
# node_modules is legitimate payload for overlay packs (playwright-core etc.); only junk dirs are skipped when copying from old packages
COPY_SKIP_DIRS = {'__pycache__', '.git'}
LOG = []


def say(msg):
    line = str(msg)
    LOG.append(line)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode('gbk', 'replace').decode('gbk'), flush=True)


def run(args, timeout=60):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace').strip()
    except Exception as exc:
        return 1, str(exc)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def is_source(rel, size):
    parts = rel.replace('\\', '/').split('/')
    if any(p in SKIP_DIRS for p in parts):
        return False
    return Path(rel).suffix.lower() in SOURCE_EXTS and size <= SOURCE_MAX


def load_manifest(root):
    path = root / 'tools' / 'components.json'
    if not path.is_file():
        say('缺组件清单 tools/components.json：仓库不完整，请重新克隆完整仓库。')
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def marker_path(root, pack_id):
    return root / 'tools' / '.installed' / (pack_id + '.json')


def installed(root, pack):
    mp = marker_path(root, pack['id'])
    if not mp.is_file():
        return False
    try:
        mark = json.loads(mp.read_text(encoding='utf-8'))
    except ValueError:
        return False
    return mark.get('sha256') == pack.get('sha256') or (
        mark.get('source') == 'package' and mark.get('version') == pack.get('version', ''))


def write_marker(root, pack, source, sha):
    mp = marker_path(root, pack['id'])
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({'id': pack['id'], 'version': pack.get('version', ''),
                              'source': source, 'sha256': sha,
                              'at': time.strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False), encoding='utf-8')


def safe_extract(zip_path, dest):
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest) + os.sep) and target != dest:
                raise ValueError('组件包内含越界路径，拒绝解压：%s' % info.filename)
        zf.extractall(dest)


def copy_from_package(pack, src_root, root):
    src = Path(src_root) / pack['rel']
    if not src.is_dir():
        return False, '旧包/完整包里没有 %s' % pack['rel']
    dst = root / pack['rel']
    n = 0
    for path in sorted(src.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(src).as_posix()
        if pack['kind'] == 'overlay' and is_source(rel, path.stat().st_size):
            continue
        if any(p in COPY_SKIP_DIRS for p in path.relative_to(src).parts[:-1]) and pack['kind'] != 'full':
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(target))
        n += 1
    return True, '%d 个文件' % n


def download(url, part, timeout=120):
    start = part.stat().st_size if part.is_file() else 0
    req = urllib.request.Request(url, headers={'Range': 'bytes=%d-' % start} if start else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        mode = 'ab' if start and resp.status == 206 else 'wb'
        if mode == 'wb':
            start = 0
        with open(part, mode) as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
    return part.stat().st_size


def fetch_pack(pack, root, offline_dir, mirrors):
    zip_name = pack['id'] + '.zip'
    if offline_dir:
        local = Path(offline_dir) / zip_name
        if not local.is_file():
            return False, '离线目录缺 %s' % zip_name
        if sha256_file(local) != pack['sha256']:
            return False, '%s sha256 不符（包损坏或版本不对）' % zip_name
        try:
            safe_extract(local, root / pack['rel'])
        except Exception as exc:
            return False, '解压失败：%s' % exc
        write_marker(root, pack, 'offline', pack['sha256'])
        return True, '离线包'
    part = root / 'tools' / '.download' / zip_name
    part.parent.mkdir(parents=True, exist_ok=True)
    urls = mirrors or pack.get('mirrors', [])
    last = '没有可用镜像'
    for url in urls:
        for attempt in (1, 2):
            try:
                size = download(url, part)
                if size != pack['zip_bytes']:
                    last = '%s 大小不符（%d != %d）' % (url, size, pack['zip_bytes'])
                    part.unlink(missing_ok=True)
                    continue
                if sha256_file(part) != pack['sha256']:
                    last = '%s sha256 不符' % url
                    part.unlink(missing_ok=True)
                    continue
                safe_extract(part, root / pack['rel'])
                write_marker(root, pack, url, pack['sha256'])
                part.unlink(missing_ok=True)
                return True, url
            except Exception as exc:
                last = '%s 第%d次：%s' % (url, attempt, exc)
                say('  重试：%s' % last)
                time.sleep(2 * attempt)
    return False, last


def heal(root):
    spec = importlib.util.spec_from_file_location('fix_venv_setup', root / 'tools' / 'fix_venv.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for rel, ok, msg in mod.ensure_venvs(root, verbose=False):
        say('  自愈 %s: %s' % (rel, msg if ok is not False else '失败 ' + msg))


def ensure_pip_steps(root, manifest):
    """唯一 pip 步骤：torch 等超体积依赖走国内镜像补进 venv（幂等：import 成功即跳过）。"""
    for step in manifest.get('pip_steps', []):
        vpy = root / step['venv'] / 'Scripts' / 'python.exe'
        if not vpy.is_file():
            say('  pip 步骤跳过：%s 不在（先补对应组件包）' % step['venv'])
            continue
        probe = 'import ' + step['packages'][0].split('==')[0].replace('-', '_')
        code, _ = run([str(vpy), '-c', probe], timeout=120)
        if code == 0:
            continue
        say('  补 %s（%s，国内镜像）' % (step['venv'], ', '.join(step['packages'])))
        done = False
        for index in step['indexes']:
            code, out = run([str(vpy), '-m', 'pip', 'install', '--disable-pip-version-check',
                             '--index-url', index] + step['packages'], timeout=3600)
            if code == 0:
                done = True
                say('    完成：%s' % index)
                break
            say('    镜像失败 %s：%s' % (index, out[-200:]))
        if not done:
            say('    torch 没补上：重跑本脚本会续试；或 --from-package 旧包（旧包自带 torch，零下载）。')


def selfcheck(root, manifest):
    checks = []

    def add(name, ok, detail, where):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail, 'where': where})

    ff = root / 'tools/ffmpeg/bin/ffmpeg.exe'
    fp = root / 'tools/ffmpeg/bin/ffprobe.exe'
    code, out = run([str(ff), '-version']) if ff.is_file() else (1, '不在')
    add('ffmpeg', ff.is_file() and code == 0, out.splitlines()[0] if code == 0 else out, 'tools/ffmpeg 包')
    code, out = run([str(fp), '-version']) if fp.is_file() else (1, '不在')
    add('ffprobe', fp.is_file() and code == 0, out.splitlines()[0] if code == 0 else out, 'tools/ffmpeg 包')
    node = root / 'tools/node/bin/node.exe'
    code, out = run([str(node), '-v']) if node.is_file() else (1, '不在')
    add('node', node.is_file() and code == 0, out, 'tools/node 包')
    chrome = sorted((root / 'tools/chromium').glob('*/chrome.exe')) if (root / 'tools/chromium').is_dir() else []
    add('chromium', bool(chrome), str(chrome[0]) if chrome else '不在', 'tools/chromium 包')
    ytdlp = root / 'tools/yt-dlp/yt-dlp.exe'
    code, out = run([str(ytdlp), '--version']) if ytdlp.is_file() else (1, '不在')
    add('yt-dlp', ytdlp.is_file() and code == 0, out, 'tools/yt-dlp 包')
    u2net = root / 'tools/avatar-model/models/u2net/u2net.onnx'
    add('抠图模型 u2net', u2net.is_file(), str(u2net) if u2net.is_file() else '不在', 'tools/avatar-model 包')
    aipy = root / 'app/.runtime/ai-env/Scripts/python.exe'
    asr_ok = aipy.is_file() and (root / 'app/.runtime/ai-env/Lib/site-packages').is_dir()
    torch_detail = '不在'
    torch_ok = False
    if asr_ok:
        code, out = run([str(aipy), '-c', 'import torch; print(torch.__version__)'], timeout=180)
        torch_ok = code == 0
        torch_detail = out if torch_ok else 'torch 缺（安装器镜像步骤会补）'
    add('本地ASR环境', asr_ok and torch_ok, torch_detail, 'app/.runtime/ai-env 包 + torch 镜像步骤')
    bipy = root / 'app/.runtime/browser-env/Scripts/python.exe'
    code, out = run([str(bipy), '-c', 'import patchright; print(patchright.__version__ if hasattr(patchright,"__version__") else "ok")']) if bipy.is_file() else (1, '不在')
    add('发布执行器 patchright', bipy.is_file() and code == 0, out, 'app/.runtime/browser-env 包')
    mpy = root / 'tools/motion-python/Scripts/python.exe'
    add('动效环境', mpy.is_file(), str(mpy) if mpy.is_file() else '不在', 'tools/motion-python 包')
    pw = root / 'tools/smart-packaging/node_modules/playwright-core'
    add('包装渲染依赖', pw.is_dir(), str(pw) if pw.is_dir() else '不在', 'tools/smart-packaging 包')
    eva = root / 'tools/eva'
    tpl = [p for p in eva.glob('模板-*') if p.is_dir()] if eva.is_dir() else []
    add('口播模板', len(tpl) >= 4, '%d 套' % len(tpl), 'tools/eva 包')
    lib = root / 'tools/bgm/library.json'
    tracks = 0
    if lib.is_file():
        try:
            tracks = len(json.loads(lib.read_text(encoding='utf-8')).get('tracks', []))
        except ValueError:
            tracks = 0
    add('配乐曲库', tracks > 0, '%d 首' % tracks, 'tools/bgm 包')
    cs = root / 'app/caption_style.py'
    font_dir = root / 'app/assets/fonts'
    if cs.is_file():
        names = re.findall(r"'file':\s*'([^']+)'", cs.read_text(encoding='utf-8'))
        have = [n for n in names if (font_dir / n).is_file()]
        add('字幕字体', bool(names) and len(have) == len(names),
            '%d/%d' % (len(have), len(names)), 'app/assets/fonts 包')
    else:
        add('字幕字体', False, 'caption_style.py 不在', 'app/assets/fonts 包')
    missing = [c for c in checks if not c['ok']]
    say('---- 自检 ----')
    for c in checks:
        say('  [%s] %s | %s' % ('齐' if c['ok'] else '缺', c['name'], c['detail']))
    if missing:
        say('缺 %d 项。补齐来源（按顺序试）：' % len(missing))
        say('  1 旧安装目录或网盘完整包解压目录：python tools/setup_full.py --from-package <目录>')
        say('  2 官网镜像：%s<版本>/<组件>.zip' % 'https://www.jidixh.com/components/')
        say('  3 GitHub Release：components-<版本> 标签下同名 zip')
        for c in missing:
            say('  缺 %s → %s' % (c['name'], c['where']))
    report = {'at': time.strftime('%Y-%m-%d %H:%M:%S'), 'checks': checks,
              'missing': [c['name'] for c in missing]}
    (root / 'tools').mkdir(parents=True, exist_ok=True)
    (root / 'tools' / 'install-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return missing


def migrate(root, old):
    spec = importlib.util.spec_from_file_location('launch_root_setup', root / 'tools' / 'launch_root.py')
    lr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lr)
    src = Path(old).resolve() / 'app' / '.runtime'
    if not src.is_dir():
        say('承接失败：%s 里没有 app/.runtime（不是本软件的安装目录）' % old)
        return False
    dest = lr.default_data_dir()
    if dest.exists() and any(dest.iterdir()):
        say('资料夹 %s 已有资料，不覆盖。如需合并：用旧版本打开旧包→资产与备份→导出，再在新版导入。' % dest)
        return False
    info = lr.adopt(root, source=src, dest=dest)
    say('已承接旧包资料（答题信息+认知卡+资产）到 %s；旧包原样未动。' % dest)
    return bool(info.get('ok'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=str(ROOT))
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--from-package', default='')
    ap.add_argument('--offline-dir', default='')
    ap.add_argument('--migrate-from', default='')
    ap.add_argument('--mirror', action='append', default=[])
    ap.add_argument('--only', action='append', default=[])
    ap.add_argument('--skip-selfcheck', action='store_true')
    args = ap.parse_args()
    if sys.version_info < (3, 10):
        say('需要 Python 3.10+（当前 %s）。先装 Python 再跑本脚本。' % sys.version.split()[0])
        return 2
    root = Path(args.root).resolve()
    manifest = load_manifest(root)
    if manifest is None:
        return 2
    for pack in manifest['packs']:
        pack.setdefault('version', manifest.get('version', ''))
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if args.check:
        return 1 if selfcheck(root, manifest) else 0
    todo = [p for p in manifest['packs'] if not installed(root, p)
            and (not args.only or p['id'] in args.only)]
    say('组件清单 %s：%d 个包，待补 %d 个' % (manifest.get('version'), len(manifest['packs']), len(todo)))
    failed = []
    for pack in todo:
        say('补 %s (%s, %.1f MB)' % (pack['rel'], pack['kind'], pack.get('zip_bytes', 0) / 1e6))
        if args.from_package:
            ok, detail = copy_from_package(pack, args.from_package, root)
            if ok:
                write_marker(root, pack, 'package', '')
                say('  来自旧包/完整包：%s' % detail)
                continue
            say('  旧包来源不可用（%s），转镜像' % detail)
        ok, detail = fetch_pack(pack, root, args.offline_dir, args.mirror)
        if ok:
            say('  完成：%s' % detail)
        else:
            failed.append((pack['rel'], detail))
            say('  失败：%s' % detail)
    heal(root)
    ensure_pip_steps(root, manifest)
    if args.migrate_from:
        migrate(root, args.migrate_from)
    if failed:
        say('有 %d 个包没补上：%s' % (len(failed), '; '.join('%s(%s)' % f for f in failed)))
        say('出路：--from-package 旧安装目录/网盘完整包解压目录；或 --offline-dir 本地 zip；或检查网络后重跑（已补的不会重复下载）。')
    if args.skip_selfcheck:
        return 3 if failed else 0
    missing = selfcheck(root, manifest)
    try:
        (root / 'tools' / 'setup_full.log').write_text('\n'.join(LOG), encoding='utf-8')
    except OSError:
        pass
    if failed or missing:
        say('安装未全齐：按上面「缺什么/从哪里补」处理后再重跑本脚本。')
        return 3 if failed else 1
    say('全功能就绪。下一步：双击 start.bat 启动工作台。')
    return 0


if __name__ == '__main__':
    sys.exit(main())