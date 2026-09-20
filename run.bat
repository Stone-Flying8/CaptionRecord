@echo off
REM Live Captions -> Notepad launcher (ASCII only on purpose)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating virtual environment...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

".venv\Scripts\python.exe" live_captions_scribe.py %*
echo.
echo [done] press any key to close
pause >nul
exit /b 0

:fail
echo.
echo [error] setup failed. Run these manually:
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1
