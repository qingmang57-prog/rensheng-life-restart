# -*- coding: utf-8 -*-
"""Portable package: rewrite venv homes to this machine's package root.

2026-11-06：从「import 时的模块级副作用」改成可显式调用的一次性自愈函数
ensure_venvs()。start.bat -> boot.py 与「直接 python app.py」两条入口都会调用，
因此绕开 boot.py 直启时，venv 也不会再指向打包机的绝对路径。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENVS = [
    ('app/.runtime/ai-env', 'tools/python'),
    ('app/.runtime/browser-env', 'tools/python'),
    ('tools/motion-python', 'tools/motion-base-python'),
]


def ensure_venvs(root=None, verbose=True):
    """把包内 3 个 venv 的 home/executable 改指当前包根。

    返回 [(venv_rel, ok, message)]；ok=None 表示这个 venv 不在（跳过），
    False 表示目录不可写。调用方不因单个失败而崩溃，但应把结果打进启动日志。
    """
    base = Path(root).resolve() if root else ROOT
    results = []
    for venv_rel, home_rel in VENVS:
        cfg = base / venv_rel / 'pyvenv.cfg'
        if not cfg.is_file():
            results.append((venv_rel, None, 'pyvenv.cfg 不存在，跳过'))
            continue
        home = (base / home_rel).resolve()
        if not (home / 'python.exe').is_file():
            # 链接安装（仓库+组件包）可能没带 home 解释器：退到当前解释器目录，
            # 组件包里的 venv 与随包 Python 同小版本，site-packages 仍可用。
            home = Path(sys.executable).resolve().parent
        out = []
        for line in cfg.read_text(encoding='utf-8').splitlines():
            if line.startswith('home ='):
                out.append('home = %s' % home)
            elif line.startswith('executable ='):
                out.append('executable = %s' % (home / 'python.exe'))
            else:
                out.append(line)
        text = '\n'.join(out) + '\n'
        try:
            # 内容没变就不写：包放在只读位置时，只要路径已正确就不该因一次空写而崩溃。
            if cfg.read_text(encoding='utf-8') != text:
                cfg.write_text(text, encoding='utf-8')
            results.append((venv_rel, True, str(home)))
            if verbose:
                print('fixed %s -> %s' % (venv_rel, home), flush=True)
        except OSError as exc:
            results.append((venv_rel, False, str(exc)))
            if verbose:
                print('[WARN] 无法改写 %s（%s）。安装包目录不可写时，请把整个文件夹移到非系统盘或用户目录。'
                      % (venv_rel, exc), flush=True)
    if verbose:
        print('venv homes fixed', flush=True)
    results.append(ensure_patchright_no_window(base, verbose=verbose))
    return results


def ensure_patchright_no_window(root=None, verbose=True):
    """幂等自愈：patchright 驱动子进程补 CREATE_NO_WINDOW（2026-11-26 黑窗修复）。

    browser-env 重装/重建会还原成未补丁版，故每次启动检查补打；
    已含标志则不写（与 ensure_venvs 同原则：只读目录不因空写崩溃）。
    """
    base = Path(root).resolve() if root else ROOT
    target = base / 'app/.runtime/browser-env/Lib/site-packages/patchright/_impl/_transport.py'
    if not target.is_file():
        return ('patchright-no-window', None, 'patchright 不在，跳过')
    try:
        text = target.read_text(encoding='utf-8')
    except OSError as exc:
        return ('patchright-no-window', False, str(exc))
    if 'creationflags' in text:
        return ('patchright-no-window', True, '已无窗')
    anchor = '                startupinfo=startupinfo,\n            )'
    if anchor not in text:
        return ('patchright-no-window', False, 'spawn 锚点变化，需人工确认')
    patched = text.replace(
        anchor,
        '                startupinfo=startupinfo,\n'
        '                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,\n'
        '            )', 1)
    try:
        target.write_text(patched, encoding='utf-8')
    except OSError as exc:
        return ('patchright-no-window', False, str(exc))
    if verbose:
        print('patched patchright driver spawn: CREATE_NO_WINDOW', flush=True)
    return ('patchright-no-window', True, '已补 CREATE_NO_WINDOW')


def main():
    results = ensure_venvs()
    return 0 if all(ok is not False for _, ok, _ in results) else 1


if __name__ == '__main__':
    sys.exit(main())
