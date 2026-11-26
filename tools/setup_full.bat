@echo off
rem Full-feature installer entry. Any AI agent: run this, read exit code + tools\install-report.json
rem Exit 0 = all features ready; 1 = self-check missing; 2 = python/manifest; 3 = download failed
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required. Install it first (winget install -e --id Python.Python.3.12), then rerun.
  exit /b 2
)
python "%~dp0setup_full.py" %*
exit /b %errorlevel%