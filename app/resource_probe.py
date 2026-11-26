"""Read-only Windows resource sampling for the benchmark's own process tree."""
import ctypes
import os
import platform
import subprocess
import time
from ctypes import wintypes as W
from pathlib import Path

MIB=1024*1024


class MemoryStatus(ctypes.Structure):
    _fields_=[('length',W.DWORD),('load',W.DWORD)]+[(n,ctypes.c_ulonglong) for n in ('total','available','page_total','page_available','virtual_total','virtual_available','extended')]


class ProcessEntry(ctypes.Structure):
    _fields_=[('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',ctypes.c_size_t),('module',W.DWORD),('threads',W.DWORD),('ppid',W.DWORD),('priority',W.LONG),('flags',W.DWORD),('name',W.WCHAR*260)]


class ProcessMemory(ctypes.Structure):
    _fields_=[('size',W.DWORD),('faults',W.DWORD)]+[(n,ctypes.c_size_t) for n in ('peak_rss','rss','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]


def gpu_binary():
    options=[Path(os.environ.get('SystemRoot',r'C:/Windows'))/'System32/nvidia-smi.exe',Path(r'C:/Program Files/NVIDIA Corporation/NVSMI/nvidia-smi.exe')]
    return next((str(p) for p in options if p.is_file()),None)


def gpu_snapshot():
    exe=gpu_binary()
    if not exe:return None
    try:
        r=subprocess.run([exe,'--query-gpu=name,memory.total,memory.used','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=4,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if r.returncode:return None
        rows=[]
        for line in r.stdout.strip().splitlines():
            name,total,used=[x.strip() for x in line.split(',')]
            rows.append({'name':name,'total_mib':float(total),'device_used_mib':float(used)})
        return rows
    except (OSError,ValueError,subprocess.TimeoutExpired):return None


def hardware():
    result={'os':platform.system(),'architecture':platform.machine(),'logical_cpus':os.cpu_count(),'cpu_description':platform.processor(),'physical_ram_mib':None,'available_ram_mib':None,'gpu':gpu_snapshot()}
    if os.name=='nt':
        status=MemoryStatus();status.length=ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            result.update(physical_ram_mib=round(status.total/MIB),available_ram_mib=round(status.available/MIB))
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as key:
                result['cpu_description']=winreg.QueryValueEx(key,'ProcessorNameString')[0].strip()
        except OSError:pass
    return result


def process_tree(root_pid):
    if os.name!='nt':return {root_pid}
    kernel=ctypes.windll.kernel32
    kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
    kernel.Process32FirstW.argtypes=[W.HANDLE,ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes=[W.HANDLE,ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle in (None,ctypes.c_void_p(-1).value):return {root_pid}
    edges=[]
    try:
        row=ProcessEntry();row.size=ctypes.sizeof(row)
        ok=kernel.Process32FirstW(handle,ctypes.byref(row))
        while ok:
            edges.append((row.pid,row.ppid));ok=kernel.Process32NextW(handle,ctypes.byref(row))
    finally:kernel.CloseHandle(handle)
    found={root_pid}
    for _ in range(30):
        added={pid for pid,parent in edges if parent in found}-found
        if not added:break
        found.update(added)
    return found


def process_sample(pid):
    if os.name!='nt':return None
    kernel=ctypes.windll.kernel32;psapi=ctypes.windll.psapi
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.GetProcessTimes.argtypes=[W.HANDLE,ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME),ctypes.POINTER(W.FILETIME)]
    psapi.GetProcessMemoryInfo.argtypes=[W.HANDLE,ctypes.POINTER(ProcessMemory),W.DWORD]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.OpenProcess(0x1000|0x10,False,pid)
    if not handle:return None
    try:
        created,ended,kernel_time,user_time=(W.FILETIME() for _ in range(4))
        cpu_ok=kernel.GetProcessTimes(handle,*[ctypes.byref(x) for x in (created,ended,kernel_time,user_time)])
        mem=ProcessMemory();mem.size=ctypes.sizeof(mem)
        mem_ok=psapi.GetProcessMemoryInfo(handle,ctypes.byref(mem),mem.size)
        if not cpu_ok and not mem_ok:return None
        ticks=lambda x:(x.dwHighDateTime<<32)+x.dwLowDateTime
        return {'pid':pid,'created':ticks(created),'cpu_seconds':(ticks(kernel_time)+ticks(user_time))/10000000 if cpu_ok else None,
                'rss_mib':mem.rss/MIB if mem_ok else None,'private_mib':mem.private/MIB if mem_ok else None}
    finally:kernel.CloseHandle(handle)


def monitored_run(command,directory,label,timeout=900,interval=.25,env=None):
    """Logs only this launched command's stdout/stderr, never unrelated process details."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    log_path=directory/(label+'.log')
    samples=[];cpu_latest={};rss_peak=None;private_peak=None;known=set();start=time.perf_counter();timed_out=False
    with log_path.open('w',encoding='utf-8') as log:
        proc=subprocess.Popen([str(x) for x in command],stdout=log,stderr=subprocess.STDOUT,env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        while True:
            pids=process_tree(proc.pid);known.update(pids);rss=private=0;mem_count=0
            for pid in pids:
                value=process_sample(pid)
                if value:
                    if value['cpu_seconds'] is not None:cpu_latest[(pid,value['created'])]=value['cpu_seconds']
                    if value['rss_mib'] is not None:rss+=value['rss_mib'];private+=value['private_mib'];mem_count+=1
            if mem_count:
                rss_peak=max(rss_peak or 0,rss);private_peak=max(private_peak or 0,private)
            elapsed=time.perf_counter()-start
            samples.append({'elapsed_seconds':round(elapsed,3),'rss_mib':round(rss,2) if mem_count else None,'private_mib':round(private,2) if mem_count else None,'process_count':len(pids)})
            if proc.poll() is not None:break
            if elapsed>timeout:
                timed_out=True
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
                else:proc.kill()
                proc.wait(timeout=15);break
            time.sleep(interval)
    elapsed=time.perf_counter()-start
    return {'label':label,'status':'timeout' if timed_out else 'succeeded' if proc.returncode==0 else 'failed',
        'wall_seconds':round(elapsed,3),'observed_cpu_seconds':round(sum(cpu_latest.values()),3) if cpu_latest else None,
        'observed_tree_rss_peak_mib':round(rss_peak,2) if rss_peak is not None else None,
        'observed_tree_private_peak_mib':round(private_peak,2) if private_peak is not None else None,
        'observed_processes':len(known),'sample_interval_seconds':interval,'exit_code':proc.returncode,'log':log_path.name,'samples':samples,
        'measurement_note':'采样值可能漏掉极短子进程和瞬时峰值；RSS相加可能重复统计共享页。CPU为已观察进程累计值下界；不能直接视为云服务器规格。'}
