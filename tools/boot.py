# -*- coding: utf-8 -*-
"""Single-process launcher: venv fix + snapshot relocate + app server.

Combines the three former python invocations in start.bat into one process,
removing two interpreter startups from the cold-launch path. Order and logic
are unchanged: fix_venv.ensure_venvs() and relocate_snapshot's import-time
remap both run to completion before app.py starts serving.
"""
import json
import runpy
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _check_writable():
    """启动前先确认包目录可写；只读位置（Program Files、系统盘根目录等）直接给人话提示。"""
    probe = ROOT / '.write-probe.tmp'
    try:
        probe.write_text('ok', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


if not _check_writable():
    print('[ERROR] 当前文件夹不可写：' + str(ROOT), flush=True)
    print('安装包需要能写入自己的目录（语音模型、运行环境、日志都放在包内）。', flush=True)
    print('请把整个文件夹放到非系统盘（如 D 盘、E 盘）或用户目录（如你的用户文件夹）后再启动；')
    print('放在 Program Files、系统盘根目录或只读盘会导致启动失败。')
    sys.exit(1)

sys.path.insert(0, str(ROOT / 'tools'))
try:
    from upgrade_journal import guard as _upgrade_guard
except Exception:
    _upgrade_guard = None
if _upgrade_guard is not None:
    _can_start, _upgrade_msg, _ = _upgrade_guard(ROOT)
    if not _can_start:
        print('[ERROR] 升级未完成，拒绝启动。', flush=True)
        print(_upgrade_msg, flush=True)
        sys.exit(3)
# 2026-11-25 资料外置：先定好资料夹（默认「文档\人生重启系统数据」），老客户第一次启动把包内资料搬出去。
# 排在升级闸之后、所有会写数据的步骤之前。规则只有 tools/launch_root.py 一处；启动器那一步是同一个函数，
# 这里再调一次是给「不经启动器、直接跑 start.bat --backend」兜底（已做过时是瞬间返回的空操作）。
# 搬不动（磁盘不够等）不拦启动：照旧用包内那份，原因写进包内「资料未搬迁-原因.txt」，设置页读得到。
# 只有「会打开一个空库」的情况才停（本机记录的资料夹不见了 / 哪里都写不了）。
try:
    import launch_root
    _data_home = launch_root.prepare(ROOT)
    _data_lines = launch_root.describe(_data_home)
except Exception as _exc:
    _data_home = {'ok': False}
    _data_lines = ['资料夹定位出错：%s' % _exc]
for _line in _data_lines:
    print('[资料] ' + str(_line), flush=True)
if not _data_home.get('ok'):
    print('[ERROR] 没有可用的资料文件夹，已停止启动（你的资料没有被改动）。', flush=True)
    sys.exit(4)
try:
    import fix_venv
    fix_venv.ensure_venvs()  # 2026-11-06：显式调用，不再靠 import 的模块级副作用
except Exception:
    print('[ERROR] venv fix failed', flush=True)
    traceback.print_exc()
    sys.exit(1)
try:
    import relocate_snapshot  # noqa: F401  module-level side effect: remap + verify
except Exception:
    print('[ERROR] media verification failed', flush=True)
    traceback.print_exc()
    sys.exit(1)
sys.path.insert(0, str(ROOT / 'app'))
try:
    _version = json.loads((ROOT / 'app' / 'web' / 'version.json').read_text(encoding='utf-8')).get('version', '')
except Exception:
    _version = ''
if _version:
    print('[版本] ' + _version, flush=True)
try:
    _buyer = json.loads((ROOT / 'app' / 'buyer_id.json').read_text(encoding='utf-8')).get('id', '')
except Exception:
    _buyer = ''
if _buyer:
    print('[构建] %s · %s' % (_version or 'unknown', _buyer), flush=True)
try:
    import license as trial_license
    trial_state = trial_license.initialize()
    print(trial_license.status_text(trial_state), flush=True)
except Exception:
    # 试用记录异常不应阻止用户打开已有作品；闸门未接入时仅提示，接入后按只读处理。
    print('[试用] 授权状态初始化失败，按只读处理；详情见下。', flush=True)
    traceback.print_exc()
_clone_state = {'read_only': False, 'message': ''}
try:
    import clone_guard
    _clone_state = clone_guard.check(ROOT)
    print('[安装] ' + _clone_state.get('message', ''), flush=True)
except Exception:
    # 绑定检查失败不应阻止用户使用已有作品
    print('[安装] 绑定检查失败，按正常可用处理；详情见下。', flush=True)
    traceback.print_exc()

_app_path = str(ROOT / 'app' / 'app.py')
sys.argv = [_app_path] + sys.argv[1:]
_app = runpy.run_path(_app_path, run_name='ip_workbench_app')
_handler = _app.get('Handler')
if _handler is None or not callable(_app.get('main')):
    # 兼容旧结构：没有可 patch 的入口时退回原来的执行方式
    runpy.run_path(_app_path, run_name='__main__')
else:
    if _clone_state.get('read_only'):
        _original_dispatch = _handler.dispatch

        def _dispatch(self, method, _original=_original_dispatch):
            block = clone_guard.blocked(method, self.path, _clone_state)
            if block:
                return self.json(block, 403)
            return _original(self, method)

        _handler.dispatch = _dispatch
    _app['main']()
