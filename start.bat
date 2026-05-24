@echo off
cd /d "%~dp0"

echo [1/3] Stopping old usps app.py processes...
wmic process where "CommandLine like '%%usps%%app.py%%'" call terminate >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] Starting server (port 5050)...
if exist "venv\Scripts\python.exe" (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

echo [3/3] Open: http://127.0.0.1:5050/
echo       Health: http://127.0.0.1:5050/health
echo.
"%PY%" app.py
pause
