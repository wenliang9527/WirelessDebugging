@echo off
REM Double-click launcher for the provisioning GUI.
REM Uses pythonw so no console window lingers behind the GUI.
setlocal
cd /d "%~dp0"

set "PYW="
where pythonw.exe >nul 2>&1 && set "PYW=pythonw.exe"
if not defined PYW (
    if exist "D:\python\pythonw.exe" set "PYW=D:\python\pythonw.exe"
)
if not defined PYW (
    echo Python not found. Install Python 3.8+ with tkinter, then retry.
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0dap_config.py" %*
endlocal
