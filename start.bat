@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
rem 2026-11-18: keep IP_DATA_DIR if already set outside (e.g. daily launcher for production data).
rem 2026-11-25 data-home: do NOT point IP_DATA_DIR into app\.runtime any more. When it is not set,
rem   tools/boot.py -> launch_root.prepare() decides (default: Documents\rensheng data folder; old installs
rem   are migrated once on first start). Hard-coding the package dir meant: delete the folder = lose all data.
rem   NOTE: keep the lines above pure ASCII. cmd.exe mis-parses UTF-8 Chinese in rem lines as commands.
set "IP_FFMPEG=%~dp0tools\ffmpeg\bin\ffmpeg.exe"
set "IP_FFPROBE=%~dp0tools\ffmpeg\bin\ffprobe.exe"
set "IP_NODE=%~dp0tools\node\bin\node.exe"
set "IP_MOTION_ADAPTER=%~dp0tools\motion-adapter\pack.py"
set "IP_MOTION_PYTHON=%~dp0tools\motion-python\Scripts\python.exe"
set "IP_LOCAL_ASR_ROOT=%~dp0app\.runtime\asr-workshop"
set "IP_LOCAL_ASR_PYTHON=%~dp0app\.runtime\ai-env\Scripts\python.exe"
set "IP_LOCAL_ASR_DEVICE=cpu"
set "IP_ASR_MODE=auto"
set "MOTIONGIT_PY=%~dp0tools\motion-python\Scripts\python.exe"
set "MOTIONGIT_FFMPEG=%~dp0tools\ffmpeg\bin"
set "MOTIONGIT_TEMPLATE=%~dp0tools\motion-template"
set "PYTHONIOENCODING=utf-8"
rem 2026-11-03: system TEMP may be unwritable (e.g. a read-only cache dir), which breaks Playwright browser resolve.
rem 2026-11-06: pick TEMP by actually writing a probe file (try-and-fallback), not by existence checks:
rem   IP_TMP_DIR -> app\.runtime\tmp -> %LOCALAPPDATA%\IPWorkbench\tmp -> %TEMP%\IPWorkbench\tmp
set "IPTMP="
set "IP_TMP_SYS=%TEMP%"
if defined IP_TMP_DIR call :pick_tmp "%IP_TMP_DIR%"
if not defined IPTMP call :pick_tmp "%~dp0app\.runtime\tmp"
if not defined IPTMP call :pick_tmp "%LOCALAPPDATA%\IPWorkbench\tmp"
if not defined IPTMP call :pick_tmp "%IP_TMP_SYS%\IPWorkbench\tmp"
if not defined IPTMP (
  echo [ERROR] no writable temp dir. Tried: IP_TMP_DIR, app\.runtime\tmp, %%LOCALAPPDATA%%\IPWorkbench\tmp, %%TEMP%%\IPWorkbench\tmp
  echo Move the folder to a writable disk, or set IP_TMP_DIR to a writable directory, then start again.
  pause
  exit /b 1
)
set "TEMP=%IPTMP%"
set "TMP=%IPTMP%"
rem 共用外壳的后端模式：只跑服务，不打印、不 pause，由 launcher 负责窗口与重试
if /i "%~1"=="--backend" (
  if "%~2"=="" (
    "%~dp0tools\python\python.exe" tools\boot.py --port 8791 --host 0.0.0.0
  ) else (
    if "%~3"=="" (
      "%~dp0tools\python\python.exe" tools\boot.py --port %~2 --host 0.0.0.0
    ) else (
      "%~dp0tools\python\python.exe" tools\boot.py --port %~2 --host %~3
    )
  )
  goto :backend_done
)
rem 默认入口：打开桌面窗口（pywebview），登录/注册都在窗口内完成。
rem 只有显式带 --backend 时才是“只跑服务、由外壳调起”的模式。
if "%~1"=="" (
  start "" "%~dp0tools\python\pythonw.exe" "%~dp0tools\launcher\launcher.py" --config "%~dp0tools\launcher\configs\rensheng.json"
  exit /b 0
)
echo [tmp] %IPTMP%
echo Workbench starting: http://127.0.0.1:8791
echo Phone access: http://(this PC LAN IP):8791  -- access code is printed below / saved to app\.runtime\lan-token.txt
echo Keep this window open. Ctrl+C stops the service.
"%~dp0tools\python\python.exe" tools\boot.py --port 8791 --host 0.0.0.0
if errorlevel 1 ( echo [ERROR] startup failed & pause & exit /b 1 )
endlocal
exit /b 0

:pick_tmp
rem 返回：%~1 可写则置 IPTMP=%~1，否则保持空。以「写探测文件成功」为准，不以存在性为准。
set "IPTMP="
if "%~1"=="" goto :eof
if not exist "%~1\" mkdir "%~1" >nul 2>nul
if not exist "%~1\" goto :eof
2>nul (>"%~1\.ipwrite.tmp" echo ipo-probe-writable)
if not exist "%~1\.ipwrite.tmp" goto :eof
del "%~1\.ipwrite.tmp" >nul 2>nul
set "IPTMP=%~1"
goto :eof

:backend_done
rem 2026-11-20: 传播 boot.py 真实退出码（此前裸 exit /b 吞码，launcher 误记 code=0）
exit /b %errorlevel%
