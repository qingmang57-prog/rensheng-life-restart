@echo off
cd /d "%~dp0"
"%~dp0tools\python\python.exe" "tools\create_desktop_shortcut.py" --force
echo.
pause
