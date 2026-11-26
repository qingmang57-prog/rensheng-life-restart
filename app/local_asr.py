"""Read-only bridge to the owner's 分段工坊 models and transcript parser."""
import atexit
import ctypes
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

# 2026-11-19（t168）跨平台：venv 内解释器路径 Windows=Scripts/python.exe，mac/Linux=bin/python3
VENV_PYTHON = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python3'
ROOT=Path(__file__).resolve().parent
LOCK=threading.Lock()
DEFAULT_ROOT=ROOT/'.runtime'/'asr-workshop'
MODEL_DIRS=(('speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch','model.pt'),
            ('speech_fsmn_vad_zh-cn-16k-common-pytorch','model.pt'),
            ('speech_campplus_sv_zh-cn_16k-common','campplus_cn_common.bin'))
OPTIONAL_MODEL_DIRS=(('punc_ct-transformer_zh-cn-common-vocab272727-pytorch','model.pt'),)
STARTUP_TIMEOUT=int(os.getenv('IP_ASR_STARTUP_TIMEOUT','600'))
LOAD_TIMEOUT=int(os.getenv('IP_ASR_LOAD_TIMEOUT','300'))
STALL_TIMEOUT=int(os.getenv('IP_ASR_STALL_TIMEOUT','900'))
# 2026-11-06：依据本机冷加载实测峰值 3188 MB（paraformer-large 952MB+VAD 2MB+SPK 27MB
# 载入时权重会短暂占双份，再加 torch CPU 运行时），取保守下限 3000 MB；旧占位值 999999 已废弃。
MIN_FREE_MB=int(os.getenv('IP_ASR_MIN_FREE_MB','3000'))
CLOUD_HINT='把设置里的语音识别通道改为 auto（本地失败自动切云端）或 cloud（只用云端），并在本机数据目录 app/.runtime/.env.local 填你自己的火山 API Key（IP_VOLC_ASR_KEY）。'
READY_FILE=ROOT/'.runtime/local-asr-ready.json'
# 2026-11-16：常驻进程。用户拍板「不用的时候把内存收一下，用的时候打开」。
# 默认空闲 10 分钟后整棵 worker 进程杀掉，模型内存归还系统；下次转写重新拉起并预热。
IDLE_UNLOAD_SEC=int(os.getenv('IP_ASR_IDLE_UNLOAD_SEC','600'))
RESIDENT_LOG=ROOT/'.runtime/asr-cache/resident.log'
_WORKER=None
_IDLE_STARTED=False


def _rel_to_root(path):
    """记录只写相对 app/ 的路径；包整体搬盘/换机后仍能解析。"""
    try:
        return Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def mark_ready(evidence, **extra):
    """自检/预热/真实转写成功后落盘；只写能证明的那一级，不虚报转写通过。

    evidence: env_check（文件与内存齐全）| warmup（模型真加载过）| real_transcription（真出过转写结果）
    返回是否写盘成功。写失败不抛异常——记录不该把转写链路卡死。
    """
    workshop,python=paths()
    try:
        old=json.loads(READY_FILE.read_text(encoding='utf-8')) if READY_FILE.is_file() else {}
    except (OSError,ValueError):
        old={}
    if not isinstance(old,dict):old={}
    record={
        'python':_rel_to_root(python),
        'workshop':_rel_to_root(workshop),
        'checked_at':time.strftime('%Y-%m-%d %H:%M:%S'),
        'evidence':evidence,
        'env_check_passed':True,
        'warmup_passed':bool(old.get('warmup_passed')) or evidence in ('warmup','real_transcription'),
        'real_transcription_passed':bool(old.get('real_transcription_passed')) or evidence=='real_transcription',
        'note':'relative paths resolve against app/',
    }
    # 2026-11-07：旧版随包记录里的实测字段（real_sample_seconds / method /
    # elapsed_seconds 等）是已有证据，升级证据级时必须保留，不能一起抹掉。
    for key,value in old.items():
        if key not in record:
            record[key]=value
    record.update(extra)
    try:
        READY_FILE.parent.mkdir(parents=True,exist_ok=True)
        tmp=READY_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        os.replace(tmp,READY_FILE)
        return True
    except OSError:
        return False


def paths():
    # 换机/换盘后 .env.local 里的绝对路径会失效：配置路径不存在时回落到包内
    # .runtime/asr-workshop 与 .runtime/ai-env，保证便携包拿到就能用。
    bundled=ROOT/'.runtime/asr-workshop'
    default=str(bundled) if bundled.is_dir() else str(DEFAULT_ROOT)
    workshop=Path(os.getenv('IP_LOCAL_ASR_ROOT',default))
    if not workshop.is_dir() and bundled.is_dir():
        workshop=bundled
    python=Path(os.getenv('IP_LOCAL_ASR_PYTHON',str(ROOT/'.runtime/ai-env'/VENV_PYTHON)))
    bundled_python=ROOT/'.runtime/ai-env'/VENV_PYTHON
    if not python.is_file() and bundled_python.is_file():
        python=bundled_python
    return workshop,python


def _torch_flavor(python):
    """不启动解释器，直接读 torch/version.py 判断是 CPU 还是 CUDA 轮子。

    GPU 加速包约定放在 .runtime/ai-env-gpu（可用 IP_LOCAL_ASR_GPU_PYTHON 覆盖）；
    没有加速包、或包里是 CPU 版 torch 时安静回落 CPU，绝不因为缺驱动报错。
    """
    try:
        text=(Path(python).parent.parent/'Lib/site-packages/torch/version.py')
        raw=text.read_text(encoding='utf-8',errors='replace')
    except OSError:
        return 'unknown'
    # torch/version.py 里的 cuda = None 只是字段；只看 __version__ 的构建标签。
    match=re.search(r"__version__\s*=\s*['\"]([^'\"]+)",raw)
    version=match.group(1) if match else ''
    if '+cu' in version:
        return 'cuda'
    if '+cpu' in version:
        return 'cpu'
    return 'unknown'


def gpu_python():
    explicit=os.getenv('IP_LOCAL_ASR_GPU_PYTHON','').strip()
    path=Path(explicit) if explicit else ROOT/'.runtime/ai-env-gpu'/VENV_PYTHON
    return path if path.is_file() else None


def device_status():
    """设备能力与「此刻真正在用哪套」。

    2026-11-16：effective 只报实际在用（常驻进程回报的 device），不再因为装了 CUDA 版
    torch 就宣称在用 GPU——加速包坏了会安静回落 CPU，界面必须跟着回落，不能说谎。
    """
    _workshop,base=paths();gpu=gpu_python();requested=os.getenv('IP_LOCAL_ASR_DEVICE','auto')
    base_torch=_torch_flavor(base);gpu_torch=_torch_flavor(gpu) if gpu else ''
    gpu_ready=bool(gpu and gpu_torch=='cuda') or base_torch=='cuda'
    worker=_WORKER
    effective=(worker.device or 'cpu') if worker and worker.alive else 'cpu'
    return {'requested':requested,'base_python':str(base),'base_torch':base_torch,
            'gpu_python':str(gpu or ''),'gpu_torch':gpu_torch,'gpu_pack':bool(gpu),
            'gpu_ready':bool(gpu_ready and requested!='cpu'),'effective':effective}


def diarize_enabled():
    return os.getenv('IP_ASR_DIARIZE','0')=='1'


_MEM_CACHE=[None]


def total_ram_gb():
    """物理内存总量（GB）；读不到返回 0，由调用方按保守策略处理。"""
    if _MEM_CACHE[0] is not None:return _MEM_CACHE[0]
    value=0.0
    if os.name!='nt':
        try:
            value=round(os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')/1073741824,1)
        except (ValueError,OSError,AttributeError):
            value=0.0
        _MEM_CACHE[0]=value
        return value
    if os.name=='nt':
        from ctypes import wintypes
        class Status(ctypes.Structure):
            _fields_=[('dwLength',wintypes.DWORD),('dwMemoryLoad',wintypes.DWORD),
                      ('ullTotalPhys',ctypes.c_ulonglong),('ullAvailPhys',ctypes.c_ulonglong),
                      ('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),
                      ('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),
                      ('ullAvailExtendedVirtual',ctypes.c_ulonglong)]
        try:
            k=_kernel32()
            k.GlobalMemoryStatusEx.restype=wintypes.BOOL
            k.GlobalMemoryStatusEx.argtypes=(ctypes.c_void_p,)
            status=Status();status.dwLength=ctypes.sizeof(status)
            if k.GlobalMemoryStatusEx(ctypes.byref(status)):
                value=round(status.ullTotalPhys/1073741824,1)
        except (AttributeError,OSError):
            value=0.0
    _MEM_CACHE[0]=value
    return value


def prewarm_decision(total_gb=None):
    """启动预热是否执行：内存 ≥ 阈值（默认 16 GB）才自动预热。

    2026-11-16 用户拍板：常驻要占 2.5 GB，8 GB 机器一开机就吃这么多，开几个软件就顶不住；
    16 GB 以上几乎无感。低于阈值改成首次用到时再加载（会多等一次加载时间）。
    IP_ASR_PRELOAD=0 强制关；IP_ASR_PRELOAD_MIN_RAM_GB 覆盖阈值。
    """
    min_gb=float(os.getenv('IP_ASR_PRELOAD_MIN_RAM_GB','16'))
    ram=total_ram_gb() if total_gb is None else float(total_gb)
    if os.getenv('IP_ASR_PRELOAD','')=='0':
        return {'enabled':False,'total_ram_gb':ram,'min_ram_gb':min_gb,
                'reason':'IP_ASR_PRELOAD=0 已强制关闭启动预热，首次转写时再加载'}
    if ram<=0:
        return {'enabled':False,'total_ram_gb':ram,'min_ram_gb':min_gb,
                'reason':'读不到物理内存，保守起见不预热，首次转写时再加载'}
    if ram<min_gb:
        return {'enabled':False,'total_ram_gb':ram,'min_ram_gb':min_gb,
                'reason':('物理内存 %.1f GB，低于 %.0f GB 阈值：不预热，先把内存留给其他软件，'
                          '首次转写时再加载（这一次会多等约 10 秒到几分钟）')%(ram,min_gb)}
    return {'enabled':True,'total_ram_gb':ram,'min_ram_gb':min_gb,
            'reason':'物理内存 %.1f GB ≥ %.0f GB，启动后后台预热，首次转写不用等'%(ram,min_gb)}


def set_diarize(enabled):
    """运行中切换说话人分离；模型组件不同，切换后释放常驻进程，下次转写按新设置重载。"""
    value='1' if enabled else '0'
    changed=os.getenv('IP_ASR_DIARIZE','0')!=value
    os.environ['IP_ASR_DIARIZE']=value
    released=False
    if changed:released=shutdown('diarize_changed')
    return {'diarize':enabled,'restarting':bool(changed and released)}


def cache_tag():
    """缓存参数标签：模型变了、说话人开关变了就必须重转。"""
    workshop,_python=paths()
    stamp=''
    try:
        model=workshop/'models/hub/iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch/model.pt'
        st=model.stat();stamp='%d:%d'%(st.st_size,int(st.st_mtime))
    except OSError:
        pass
    return 'funasr-paraformer-large-v1|diarize=%d|model=%s'%(int(diarize_enabled()),stamp)


def _free_mb():
    if os.name!='nt':return None
    class Mem(ctypes.Structure):
        _fields_=[('dwLength',ctypes.c_ulong),('dwMemoryLoad',ctypes.c_ulong),
                  ('ullTotalPhys',ctypes.c_ulonglong),('ullAvailPhys',ctypes.c_ulonglong),
                  ('ullTotalPageFile',ctypes.c_ulonglong),('ullAvailPageFile',ctypes.c_ulonglong),
                  ('ullTotalVirtual',ctypes.c_ulonglong),('ullAvailVirtual',ctypes.c_ulonglong),
                  ('ullAvailExtendedVirtual',ctypes.c_ulonglong)]
    try:
        stat=Mem();stat.dwLength=ctypes.sizeof(stat)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):return None
        return round(stat.ullAvailPhys/1048576)
    except (AttributeError,OSError):return None


class LocalAsrTimeout(RuntimeError):
    """本地加载或转写超时（已杀进程树）；调用方据此自动降级云端。"""


def _tail(path,lines=6):
    try:return '\n'.join([x for x in path.read_text(encoding='utf-8',errors='replace').splitlines() if x.strip()][-lines:])
    except OSError:return ''


EVIDENCE_LEVELS=('env_check','warmup','real_transcription')


def _upgrade_legacy_ready(ready,warnings):
    """2026-11-07：把人工旧格式的随包记录升级成带三级证据的记录。

    背景：随包 local-asr-ready.json 只有 python/workshop/real_transcription_passed/
    method/note，没有 evidence；_ready_problems() 只看 xxx_passed，所以旧记录会
    永远停在旧格式，和工作流 R4 断言冲突。这里在环境硬检查全过时按「现有证据」
    补写，绝不虚报更高级；旧记录里的实测字段由 mark_ready() 原样保留。
    """
    if ready.get('evidence') in EVIDENCE_LEVELS:
        return False
    if ready.get('real_transcription_passed') is True:
        level='real_transcription'
    elif ready.get('warmup_passed') is True:
        level='warmup'
    else:
        level='env_check'
    fixed={'python','workshop','checked_at','evidence','env_check_passed',
           'warmup_passed','real_transcription_passed','note'}
    extras={k:v for k,v in ready.items() if k not in fixed}
    ok=mark_ready(level,**extras)
    if ok:
        warnings.append('已把旧格式语音自检记录升级为 %s 级证据（按现有证据，不虚报）。'%level)
    return ok


def _ready_problems(python,workshop,warnings,allow_autowrite):
    """校验 .runtime/local-asr-ready.json；返回要追加的 problems 列表。"""
    try:
        ready=json.loads(READY_FILE.read_text(encoding='utf-8'))
        rp=Path(ready['python']);rw=Path(ready['workshop'])
        if not rp.is_absolute():rp=ROOT/rp
        if not rw.is_absolute():rw=ROOT/rw
        if rp.resolve()!=python.resolve() or rw.resolve()!=workshop.resolve():
            status='stale'
        elif ready.get('real_transcription_passed') is True:
            if allow_autowrite:_upgrade_legacy_ready(ready,warnings)
            return []
        elif ready.get('warmup_passed') is True or ready.get('env_check_passed') is True:
            warnings.append('本地语音环境已通过自检，但还没跑过真实转写；建议点一次「预热语音模型」。')
            if allow_autowrite:_upgrade_legacy_ready(ready,warnings)
            return []
        else:
            status='unverified'
    except (OSError,ValueError,KeyError,TypeError):
        status='missing'
    if allow_autowrite and mark_ready('env_check'):
        warnings.append('已自动重建语音识别自检记录；建议点一次「预热语音模型」做真实加载验证。')
        return []
    return [{'missing':'缺少语音识别自检记录 .runtime/local-asr-ready.json',
             'stale':'环境自检记录与当前模型目录不一致，请重新做一次“语音识别环境检查”',
             'unverified':'环境自检尚未通过（未完成一次真实转写验证）'}[status]]


def preflight():
    """Cheap checks before spawning the worker; never downloads models."""
    workshop,python=paths();problems=[];warnings=[]
    if not python.is_file():problems.append('找不到本地 ASR 解释器 '+str(python))
    if not (workshop/'backend/app/local_asr.py').is_file():problems.append('找不到分段工坊转写代码 '+str(workshop))
    for name,weight in MODEL_DIRS:
        if not (workshop/'models/hub/iic'/name/weight).is_file():problems.append('模型不完整：'+name)
    for name,weight in OPTIONAL_MODEL_DIRS:
        if not (workshop/'models/hub/iic'/name/weight).is_file():warnings.append('可选模型缺失：'+name)
    free=_free_mb();memory_low=False
    if free is not None and free<MIN_FREE_MB:
        memory_low=True
        problems.append('内存不足：可用 %d MB，本地转写至少需要 %d MB'%(free,MIN_FREE_MB))
    # 2026-11-06：旧版把「缺记录」当硬失败，而全工程没有任何写盘点 → 记录一旦被清掉，
    # 本地转写永久关死且用户无路径恢复。现在按证据分级：真实转写 > 预热 > 环境自检，
    # 硬检查全过时，缺记录/路径过期会自动重建一份 env_check 记录（不虚报转写通过）。
    hard_ok=not problems
    problems.extend(_ready_problems(python,workshop,warnings,allow_autowrite=hard_ok))
    return {'ok':not problems,'problems':problems,'warnings':warnings,'free_mb':free,
            'need_mb':MIN_FREE_MB,'memory_low':memory_low,'python':str(python),'workshop':str(workshop)}


def available():
    if os.getenv('IP_DISABLE_LOCAL_ASR')=='1':return False
    return preflight()['ok']


def _kill_tree(proc):
    """Stop the worker and any ffmpeg children; never leave an orphan."""
    if proc.poll() is not None:return
    try:
        if os.name=='nt':
            subprocess.run(['taskkill','/F','/T','/PID',str(proc.pid)],capture_output=True,timeout=30,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        else:
            # POSIX（mac/Linux）：先按进程组温柔收（TERM），等不到再 KILL；有 psutil 时连子孙一起收。
            import signal
            killed=False
            try:
                import psutil  # 可选依赖：装了就能收整棵树
                parent=psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    try: child.terminate()
                    except Exception: pass
                parent.terminate(); killed=True
            except Exception:
                killed=False
            if not killed:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (OSError, AttributeError):
                    try: proc.terminate()
                    except OSError: pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, AttributeError):
                    try: proc.kill()
                    except OSError: pass
    except (OSError,subprocess.SubprocessError):pass
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:proc.kill()
        except OSError:pass


def _worker_env():
    import media
    env=media.runtime_environment();env.update(PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',
        PYTHONDONTWRITEBYTECODE='1',HF_HUB_OFFLINE='1',MODELSCOPE_OFFLINE='1',
        OMP_NUM_THREADS='4',MKL_NUM_THREADS='4')
    cache=ROOT/'.runtime/asr-cache';cache.mkdir(exist_ok=True)
    env.update(HF_HOME=str(cache/'hf'),MODELSCOPE_CACHE=str(cache/'modelscope'),
               TORCH_HOME=str(cache/'torch'),NUMBA_CACHE_DIR=str(cache/'numba'))
    for key in ('IP_LLM_KEY','IP_ASR_KEY','IP_RESOLVER_KEY'):env.pop(key,None)
    return env


MARKERS=(('ASR worker: starting','正在启动本地语音进程'),
         ('ASR worker: importing torch','正在加载语音运行时（首次冷启动可能要 1–2 分钟）'),
         ('ASR worker: CPU runtime loaded','运行时已加载'),
         ('ASR worker: workshop parser loaded','转写脚本已加载'),
         ('ASR worker: components importing','正在注册语音组件'),
         ('ASR worker: components registered','转写组件已注册'),
         ('ASR worker: model loading','正在加载语音模型'),
         ('ASR worker: model loaded','模型已加载，开始转写'),
         ('ASR worker: transcribing','正在转写音频'))


def _run_worker(extra,log_path,stage=None,load_timeout=None,stall_timeout=None):
    load_timeout=load_timeout or LOAD_TIMEOUT
    stall_timeout=stall_timeout or STALL_TIMEOUT
    workshop,python=paths()
    command=[str(python),'-B',str(ROOT/'local_asr_worker.py'),'--workshop',str(workshop),
             '--device',os.getenv('IP_LOCAL_ASR_DEVICE','cpu'),*extra]
    env=_worker_env()
    with log_path.open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(command,env=env,stdout=log,stderr=log,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        started=time.monotonic();last=started;seen=set();startup_done=False;model_loading=False;model_loaded=False
        stage_text='准备本地语音识别'
        if stage:stage(stage_text)
        while True:
            code=proc.poll()
            if code is not None:
                if code:
                    _kill_tree(proc)  # 非 0 退出也要收尸，避免残留进程占住产物/内存
                break
            now=time.monotonic()
            try:text=log_path.read_text(encoding='utf-8',errors='replace')
            except OSError:text=''
            for marker,label in MARKERS:
                if marker in text and marker not in seen:
                    seen.add(marker);last=now;stage_text=label
                    if marker=='ASR worker: workshop parser loaded':startup_done=True
                    if marker=='ASR worker: model loading':model_loading=True
                    if marker=='ASR worker: model loaded':model_loaded=True
                    if stage:stage(label)
            if not startup_done and now-started>STARTUP_TIMEOUT:
                _kill_tree(proc)
                free=_free_mb()
                raise LocalAsrTimeout('本地语音运行时启动超过 %d 秒没有完成，已停止本次转写并清理后台进程。'
                    '首次冷启动需要读大量模型运行库，正常也要一两分钟；当前可用内存 %s MB。'
                    '请先在剪辑页点「预热语音模型」再试；若仍失败，%s 运行日志：%s'
                    %(STARTUP_TIMEOUT,free if free is not None else '未知',CLOUD_HINT,log_path))
            # 2026-11-03：卡在「转写脚本已加载」之后（funasr 组件注册）时，旧逻辑因未见到
            # 「model loading」标记而不设超时，结果挂到上层 900 秒。只要启动完成且模型未就绪，
            # 任何标记超过 load_timeout 都判定为加载卡死并杀树。
            if startup_done and not model_loaded and now-last>load_timeout:
                _kill_tree(proc)
                free=_free_mb()
                tail=_tail(log_path)
                raise LocalAsrTimeout('本地语音模型加载超过 %d 秒没有进展（卡在“%s”），已停止本次转写并清理后台进程。'
                    '可能原因：funasr 组件导入死锁、内存不足（当前可用 %s MB，建议至少 %d MB）或模型文件不完整。'
                    '请关闭占用内存的程序后重试；若仍失败，%s 运行日志：%s%s'
                    %(load_timeout,stage_text,free if free is not None else '未知',MIN_FREE_MB,CLOUD_HINT,log_path,
                      ('\n运行日志末尾：'+tail) if tail else ''))
            if model_loaded and now-last>stall_timeout:
                _kill_tree(proc)
                raise LocalAsrTimeout('本地转写超过 %d 秒没有新进展，已停止并清理后台进程。'
                    '音频可能过长或机器负载过高；请缩短片段后重试，或%s 运行日志：%s'
                    %(stall_timeout,CLOUD_HINT,log_path))
            time.sleep(0.5)
    return proc.returncode


_K32=[None]
_INVALID_HANDLE=ctypes.c_void_p(-1).value


def _kernel32():
    """统一给 Win32 调用设 restype/argtypes：64 位句柄不设会被截成 32 位，读内存直接读错。"""
    if _K32[0] is None:
        from ctypes import wintypes
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes=(wintypes.DWORD,wintypes.DWORD)
        k.Process32FirstW.restype=wintypes.BOOL
        k.Process32FirstW.argtypes=(wintypes.HANDLE,ctypes.c_void_p)
        k.Process32NextW.restype=wintypes.BOOL
        k.Process32NextW.argtypes=(wintypes.HANDLE,ctypes.c_void_p)
        k.OpenProcess.restype=wintypes.HANDLE
        k.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
        k.CloseHandle.restype=wintypes.BOOL
        k.CloseHandle.argtypes=(wintypes.HANDLE,)
        _K32[0]=k
    return _K32[0]


def _process_tree(pid):
    """pid 本身加它的全部后代。ai-env/Scripts/python.exe 只是启动器（实测 3 MB），
    真正吃模型内存的是它派生的 tools/python/python.exe，因此必须按树统计。"""
    if not pid:return []
    if os.name!='nt':
        # POSIX（mac/Linux）：有 psutil 就按树取，没装就只认 pid 本身（不假装知道树）
        try:
            import psutil
            proc=psutil.Process(int(pid))
            return [proc.pid]+[c.pid for c in proc.children(recursive=True)]
        except Exception:
            return [int(pid)]
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
                  ('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),
                  ('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
                  ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),
                  ('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    k=_kernel32()
    snap=k.CreateToolhelp32Snapshot(0x00000002,0)  # TH32CS_SNAPPROCESS
    if not snap or snap==_INVALID_HANDLE:return [int(pid)]
    children={}
    try:
        entry=Entry();entry.dwSize=ctypes.sizeof(entry)
        ok=k.Process32FirstW(snap,ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID),[]).append(int(entry.th32ProcessID))
            ok=k.Process32NextW(snap,ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    tree=[];stack=[int(pid)]
    while stack:
        current=stack.pop()
        if current in tree:continue
        tree.append(current);stack.extend(children.get(current,()))
    return tree


def _proc_memory_mb(pid):
    """整棵进程树的 WorkingSet 合计（MB），用来证明空闲卸载后内存真的回落。"""
    if not pid:return None
    if os.name!='nt':
        try:
            import psutil
            total=0
            for proc_id in _process_tree(pid):
                try: total+=psutil.Process(proc_id).memory_info().rss
                except Exception: continue
            return round(total/1048576) if total else None
        except Exception:
            return None
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD),
                  ('PeakWorkingSetSize',ctypes.c_size_t),('WorkingSetSize',ctypes.c_size_t),
                  ('QuotaPeakPagedPoolUsage',ctypes.c_size_t),('QuotaPagedPoolUsage',ctypes.c_size_t),
                  ('QuotaPeakNonPagedPoolUsage',ctypes.c_size_t),('QuotaNonPagedPoolUsage',ctypes.c_size_t),
                  ('PagefileUsage',ctypes.c_size_t),('PeakPagefileUsage',ctypes.c_size_t)]
    k=_kernel32()
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    psapi.GetProcessMemoryInfo.restype=wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes=(wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD)
    total=0;seen=False
    for proc_id in _process_tree(pid):
        handle=k.OpenProcess(0x0400|0x1000,False,proc_id)  # QUERY_INFORMATION|QUERY_LIMITED
        if not handle:continue
        try:
            counters=Counters();counters.cb=ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),ctypes.sizeof(counters)):
                total+=counters.WorkingSetSize;seen=True
        finally:
            k.CloseHandle(handle)
    return round(total/1048576) if seen else None


def _arm_job(proc):
    """把 worker 关进 KILL_ON_JOB_CLOSE 作业对象。

    启动器先退出时 taskkill /T 找不到父子关系，会漏杀真正在跑模型的子进程
    （实测残留过一个吃满 CPU 的 tools\\python\\python.exe）。作业对象关句柄即全灭，
    连 worker 再派生的 ffmpeg 一起收走。
    """
    if os.name!='nt' or not proc:return None
    from ctypes import wintypes
    class IO(ctypes.Structure):
        _fields_=[('ReadOperationCount',ctypes.c_ulonglong),('WriteOperationCount',ctypes.c_ulonglong),
                  ('OtherOperationCount',ctypes.c_ulonglong),('ReadTransferCount',ctypes.c_ulonglong),
                  ('WriteTransferCount',ctypes.c_ulonglong),('OtherTransferCount',ctypes.c_ulonglong)]
    class Basic(ctypes.Structure):
        _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),
                  ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),
                  ('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),
                  ('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),
                  ('SchedulingClass',wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),
                  ('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                  ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    k=_kernel32()
    k.CreateJobObjectW.restype=wintypes.HANDLE
    k.CreateJobObjectW.argtypes=(ctypes.c_void_p,wintypes.LPCWSTR)
    k.SetInformationJobObject.restype=wintypes.BOOL
    k.SetInformationJobObject.argtypes=(wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD)
    k.AssignProcessToJobObject.restype=wintypes.BOOL
    k.AssignProcessToJobObject.argtypes=(wintypes.HANDLE,wintypes.HANDLE)
    job=k.CreateJobObjectW(None,None)
    if not job:return None
    info=Extended()
    info.BasicLimitInformation.LimitFlags=0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(job,9,ctypes.byref(info),ctypes.sizeof(info)):  # ExtendedLimitInformation
        k.CloseHandle(job);return None
    handle=k.OpenProcess(0x0001|0x0100|0x0400,False,int(proc.pid))  # TERMINATE|SET_QUOTA|QUERY
    if not handle:
        k.CloseHandle(job);return None
    try:
        ok=k.AssignProcessToJobObject(job,handle)
    finally:
        k.CloseHandle(handle)
    if not ok:
        k.CloseHandle(job);return None
    return job


def _close_job(job):
    if not job:return
    try:_kernel32().CloseHandle(job)
    except (AttributeError,OSError):pass


class _Resident:
    """One long-lived worker process; model stays loaded until idle unload."""

    def __init__(self,python,device,diarize):
        self.python=Path(python);self.requested=device;self.diarize=bool(diarize)
        self.proc=None;self.log=None;self.queue=None;self.reader=None;self.job=None
        self.device='';self.gpu='';self.model_seconds=0.0;self.ready_seconds=0.0
        self.started_at=0.0;self.last_used=time.monotonic()
        self.requests=0;self.errors=0;self.gpu_fallback=''

    @property
    def alive(self):
        return bool(self.proc is not None and self.proc.poll() is None)

    def start(self,stage=None):
        workshop,_python=paths()
        RESIDENT_LOG.parent.mkdir(parents=True,exist_ok=True)
        self.log=RESIDENT_LOG.open('a',encoding='utf-8',buffering=1)
        self.log.write('\n===== resident start %s =====\n'%time.strftime('%Y-%m-%d %H:%M:%S'))
        command=[str(self.python),'-B',str(ROOT/'local_asr_worker.py'),'--workshop',str(workshop),
                 '--serve','--device',self.requested,'--diarize','1' if self.diarize else '0']
        self.proc=subprocess.Popen(command,env=_worker_env(),stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',bufsize=1,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.job=_arm_job(self.proc)
        self.queue=queue.Queue()

        def reader():
            try:
                for line in self.proc.stdout:
                    line=(line or '').strip()
                    if not line:continue
                    try:self.queue.put(json.loads(line))
                    except ValueError:self.queue.put({'type':'stage','text':line[:200]})
            except (OSError,ValueError):
                pass
            finally:self.queue.put({'type':'eof'})

        self.reader=threading.Thread(target=reader,daemon=True,name='ip-asr-reader')
        self.reader.start()
        started=time.monotonic()
        if stage:stage('正在拉起本地语音进程并加载模型')
        while True:
            if self.proc.poll() is not None:
                tail=_tail(RESIDENT_LOG,10)
                raise RuntimeError('本地语音进程启动失败（退出码 %s）。%s'%(self.proc.returncode,tail[-300:]))
            try:item=self.queue.get(timeout=1)
            except queue.Empty:item=None
            if item:
                kind=item.get('type')
                if kind=='stage':
                    if stage:stage(str(item.get('text') or '')[:120])
                    continue
                if kind=='ready':
                    self.device=str(item.get('device') or 'cpu')
                    self.gpu=str(item.get('gpu') or '')
                    self.model_seconds=float(item.get('model_seconds') or 0)
                    self.ready_seconds=round(time.monotonic()-started,2)
                    self.started_at=time.monotonic();self.last_used=self.started_at
                    if stage:stage('本地语音模型已就绪（%s）'%('GPU '+self.gpu if self.device=='cuda' else 'CPU'))
                    return self
                if kind=='error':
                    raise RuntimeError(str(item.get('message') or '本地语音进程启动失败'))
            if time.monotonic()-started>STARTUP_TIMEOUT:
                self.stop('startup_timeout')
                raise LocalAsrTimeout('本地语音运行时启动超过 %d 秒没有完成，已停止并释放后台进程。'
                    '首次冷启动需要读大量运行库和模型，慢盘上可能要几分钟；'
                    '请先点「预热语音模型」，等就绪后再转写。' %STARTUP_TIMEOUT)

    def request(self,payload,stage=None):
        if not self.alive:raise RuntimeError('本地语音进程已退出')
        self.last_used=time.monotonic()
        try:
            self.proc.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n')
            self.proc.stdin.flush()
        except (OSError,ValueError) as exc:
            raise RuntimeError('本地语音进程写入失败：%s'%exc)
        started=last=time.monotonic()
        hard_cap=float(os.getenv('IP_ASR_REQUEST_MAX','14400'))
        while True:
            if self.proc.poll() is not None:
                tail=_tail(RESIDENT_LOG,12)
                raise RuntimeError('本地语音进程异常退出（退出码 %s）。%s'
                    %(self.proc.returncode,tail[-400:]))
            try:item=self.queue.get(timeout=1)
            except queue.Empty:item=None
            if item:
                kind=item.get('type')
                if kind in ('stage','eof'):
                    if kind=='stage' and stage:
                        stage(str(item.get('text') or '')[:120])
                    last=time.monotonic()
                    if kind=='eof':raise RuntimeError('本地语音进程输出已关闭')
                    continue
                last=time.monotonic()
                if kind=='error':
                    self.errors+=1
                    raise RuntimeError(str(item.get('message') or '本地转写失败'))
                if kind=='done':
                    self.requests+=1;self.last_used=time.monotonic()
                    return item
            now=time.monotonic()
            if now-last>STALL_TIMEOUT:
                self.stop('stall_timeout')
                raise LocalAsrTimeout('本地转写超过 %d 秒没有新进展，已停止并释放后台进程。'
                    '音频可能过长或机器负载过高；请缩短片段后重试。'%STALL_TIMEOUT)
            if now-started>hard_cap:
                self.stop('request_max')
                raise LocalAsrTimeout('本地转写超过 %.0f 小时上限，已停止并释放后台进程。'% (hard_cap/3600))

    def stop(self,reason=''):
        proc=self.proc
        if proc is None:return
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(json.dumps({'cmd':'shutdown'})+'\n');proc.stdin.flush()
                    proc.stdin.close()
                proc.wait(timeout=3)
            except (OSError,ValueError,subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:_kill_tree(proc)
        # 作业对象兜底：启动器先退、真正跑模型的子进程还在时，关句柄即全灭。
        _close_job(self.job);self.job=None
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:_kill_tree(proc)
        self.proc=None
        try:
            if self.log:self.log.write('===== resident stop (%s) =====\n'%reason);self.log.close()
        except OSError:pass
        self.log=None


def _idle_watch():
    while True:
        time.sleep(10)
        worker=_WORKER
        if not worker or not worker.alive:continue
        idle=time.monotonic()-worker.last_used
        if idle<IDLE_UNLOAD_SEC:continue
        if not LOCK.acquire(blocking=False):continue
        try:
            current=_WORKER
            if current is worker and current.alive and time.monotonic()-current.last_used>=IDLE_UNLOAD_SEC:
                pid=worker.proc.pid if worker.proc else 0
                before=_proc_memory_mb(pid)
                worker.stop('idle_unload')
                globals()['_WORKER']=None
                try:
                    import applog
                    applog.info('asr.resident','本地语音常驻进程空闲卸载',
                        idle_seconds=round(idle,1),pid=pid,memory_mb_before=before)
                except Exception:
                    pass
        finally:
            LOCK.release()


def _start_idle_watch():
    global _IDLE_STARTED
    if _IDLE_STARTED:return
    _IDLE_STARTED=True
    threading.Thread(target=_idle_watch,daemon=True,name='ip-asr-idle').start()


def _ensure_worker(stage=None):
    global _WORKER
    worker=_WORKER
    if worker and worker.alive:
        worker.last_used=time.monotonic()
        return worker
    if worker:
        worker.stop('replace_dead')
        _WORKER=None
    _workshop,base=paths();gpu=gpu_python()
    requested=os.getenv('IP_LOCAL_ASR_DEVICE','auto')
    candidates=[]
    if gpu and requested!='cpu' and _torch_flavor(gpu)=='cuda':
        candidates.append((gpu,'gpu'))
    candidates.append((base,'base'))
    last=None
    for python,kind in candidates:
        candidate=_Resident(python,requested if requested=='cpu' else 'auto',diarize_enabled())
        try:
            candidate.start(stage=stage)
            if kind=='base' and candidates[0][1]=='gpu':
                candidate.gpu_fallback='GPU 加速包启动失败，已回落 CPU'
            _WORKER=candidate
            _start_idle_watch()
            return candidate
        except Exception as exc:
            last=exc
            candidate.stop('start_failed')
            try:
                import applog
                applog.write('warn','asr.resident','本地语音进程启动失败，尝试下一套运行时',
                    runtime=kind,error=str(exc)[:300])
            except Exception:
                pass
    raise last or RuntimeError('本地语音运行时不可用')


SLOW_FACTOR=float(os.getenv('IP_ASR_SLOW_FACTOR','2'))


def _annotate_speed(data,elapsed):
    """速度告警：耗时超过音频时长的 SLOW_FACTOR 倍就记一条，界面上说清原因与办法。"""
    try:
        words=data.get('words') or []
        audio=round(max(float(w.get('end_ms') or 0) for w in words)/1000,2) if words else 0.0
    except (TypeError,ValueError):
        audio=0.0
    if audio<=0:return data
    data['audio_seconds']=audio
    data['speed_ratio']=round(audio/max(elapsed,0.01),2)
    if elapsed>audio*SLOW_FACTOR:
        data['speed_warning']=('本地转写用了 %.0f 秒，音频时长 %.0f 秒（慢于 %.1f 倍实时）。'
            '长素材会明显更慢：建议先切段、关掉说话人分离，或装 GPU 加速包。'
            %(elapsed,audio,SLOW_FACTOR))
        try:
            import applog
            applog.write('warn','asr.speed','本地转写速度偏慢',elapsed_seconds=round(elapsed,1),
                         audio_seconds=audio,ratio=data['speed_ratio'],device=data.get('device'))
        except Exception:
            pass
    return data


def _profile_hotwords():
    # 2026-11-17（t15）：LLM 纠错热词白名单，源=主播档案 profile/owner.hotwords
    # （sqlite 单条只读直查，不 import app；加载失败一律空表降级，不阻断转写）。
    try:
        root=Path(os.getenv('IP_DATA_DIR',str(ROOT/'.runtime')))
        import sqlite3 as _sq
        con=_sq.connect(str(root/'workbench.sqlite3'),timeout=5)
        try:
            row=con.execute("SELECT data FROM records WHERE kind='profile' AND id='owner'").fetchone()
        finally:
            con.close()
        if not row:return ()
        raw=str((json.loads(row[0]) or {}).get('hotwords') or '')
        words=[]
        for w in raw.replace(',',' ').replace('，',' ').replace('、',' ').split():
            w=w.strip()
            if w and w not in words:words.append(w)
        return tuple(words)
    except Exception:
        return ()


def transcribe(audio_path,stage=None):
    if os.getenv('IP_DISABLE_LOCAL_ASR')=='1':raise ValueError('本地转写已被 IP_DISABLE_LOCAL_ASR 关闭。'+CLOUD_HINT)
    info=preflight()
    if not info['ok']:
        raise ValueError('本地语音识别环境不可用：'+'；'.join(info['problems'])+'。'+CLOUD_HINT)
    audio_path=Path(audio_path).resolve()
    result_path=audio_path.with_suffix('.asr.json');log_path=audio_path.with_suffix('.asr.log')
    with LOCK:
        try:result_path.unlink()
        except OSError:pass
        worker=_ensure_worker(stage=stage)
        started=time.monotonic()
        try:
            response=worker.request({'cmd':'transcribe','audio':str(audio_path),
                'output':str(result_path),'ffmpeg':media_binary()},stage=stage)
        except Exception as exc:
            try:
                log_path.write_text('本地常驻进程转写失败：%s\n\n%s'%(exc,_tail(RESIDENT_LOG,30)),
                    encoding='utf-8')
            except OSError:pass
            if not worker.alive:
                worker.stop('request_failed')
                globals()['_WORKER']=None
            raise
        total=round(time.monotonic()-started,2)
    if result_path.is_file():
        try:
            data=json.loads(result_path.read_text(encoding='utf-8'))
            data['worker_seconds']=data.get('elapsed_seconds')
            data['elapsed_seconds']=total
            data['device']=worker.device
            data['diarize']=worker.diarize
            data['resident_reused']=worker.requests>1
            if worker.gpu_fallback:data['gpu_fallback']=worker.gpu_fallback
            _annotate_speed(data,total)
            # 2026-11-17（t60）：LLM 纠错 + 等长回写 words + 单字合并成词。
            try:
                import asr_postprocess
                data,_pp=asr_postprocess.apply_to_transcript(data, hotwords=_profile_hotwords())
            except Exception as exc:
                import applog
                applog.exception('asr.postprocess.failed',exc)
            mark_ready('real_transcription',last_seconds=total,device=worker.device,
                       diarize=worker.diarize)
            return data
        except ValueError:
            pass
    raise RuntimeError('本地语音识别没有产出可用结果（%s），原素材保留。%s'
        %(response.get('error') or '结果文件无法解析',CLOUD_HINT))


def media_binary():
    import media
    return media.binary('ffmpeg')


def warmup(stage=None):
    """Load the resident worker once; it stays warm until the idle timeout."""
    info=preflight()
    if not info['ok']:
        return {'ok':False,'seconds':0,'error':'本地语音识别环境不可用：'+'；'.join(info['problems'])+'。'+CLOUD_HINT}
    with LOCK:
        started=time.monotonic()
        try:
            worker=_ensure_worker(stage=stage)
            seconds=round(time.monotonic()-started,1)
            mark_ready('warmup',warmup_seconds=seconds,device=worker.device)
            return {'ok':True,'seconds':seconds,'error':'','device':worker.device,
                    'resident':True,'memory_mb':_proc_memory_mb(worker.proc.pid)}
        except Exception as exc:
            return {'ok':False,'seconds':round(time.monotonic()-started,1),'error':str(exc)[:600]}


def shutdown(reason='app_exit'):
    global _WORKER
    if not LOCK.acquire(blocking=False):return False
    try:
        worker=_WORKER;_WORKER=None
        if worker:worker.stop(reason)
        return True
    finally:
        LOCK.release()


def resident_status():
    worker=_WORKER
    if not worker or not worker.alive:
        return {'running':False,'idle_unload_sec':IDLE_UNLOAD_SEC,'diarize':diarize_enabled(),
                'device':device_status()}
    now=time.monotonic()
    return {'running':True,'pid':worker.proc.pid,'device':worker.device,'gpu':worker.gpu,
            'diarize':worker.diarize,'model_seconds':worker.model_seconds,
            'ready_seconds':worker.ready_seconds,'idle_seconds':round(now-worker.last_used,1),
            'idle_unload_sec':IDLE_UNLOAD_SEC,'requests':worker.requests,'errors':worker.errors,
            'memory_mb':_proc_memory_mb(worker.proc.pid),'gpu_fallback':worker.gpu_fallback,
            'python':str(worker.python)}


def status():
    import asr_cache
    return {'preflight':preflight(),'resident':resident_status(),'devices':device_status(),
            'settings':{'diarize':diarize_enabled(),'idle_unload_sec':IDLE_UNLOAD_SEC,
                        'slow_factor':SLOW_FACTOR},
            'cache':asr_cache.stats()}


atexit.register(shutdown)
