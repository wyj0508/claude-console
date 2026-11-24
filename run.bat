@echo off
rem Claude Console launcher for Windows.
rem Keep this file ASCII-only: cmd.exe parses batch files with the OEM codepage.
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PYW=.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
    set "PY=python"
    set "PYW=pythonw"
)

rem run.bat --background : start with pythonw (no console window)
if /I "%~1"=="--background" goto :background

"%PY%" "%~dp0main.py" %*
if errorlevel 1 (
    echo.
    echo The app exited with an error. See the output above.
    pause
)
exit /b 0

:background
rem %* is not affected by shift, so do not forward it here
start "" "%PYW%" "%~dp0main.py"
exit /b 0
