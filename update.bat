@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 用法：把补丁 zip 拖到 update.bat 上，或在命令行运行 update.bat 补丁路径
  pause
  exit /b 1
)
set PYEXE=%~dp0tools\python\python.exe
if not exist "%PYEXE%" set PYEXE=python
"%PYEXE%" "%~dp0tools\apply_patch.py" --zip "%~1"
if errorlevel 3 (
  echo.
  echo 更新未完成，已自动回滚或需要人工恢复；程序文件不一定保持原样。
  echo 请按上方提示处理，恢复完成前不要启动工作台。
) else if errorlevel 1 (
  echo.
  echo 更新未完成，程序文件可能已回滚或仍处于中间状态。
  echo 请按上方提示处理，恢复完成前不要启动工作台。
) else (
  echo.
  echo 更新完成，重新打开工作台即可。
)
pause
