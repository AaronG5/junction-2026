@echo off
rem Windows launcher: double-click this file, or run it from a terminal.
rem All the work happens in Python (local_ai/); this only finds Python 3.10+.
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
)

if not defined PY (
    echo Python 3.10 or newer is needed and was not found.
    where winget >nul 2>&1 && (
        choice /M "Install Python 3.12 now with winget"
        if not errorlevel 2 (
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            echo.
            echo Python was installed. Close this window and run launch.bat again.
            pause
            exit /b 0
        )
    )
    echo Download it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
)

%PY% -m local_ai %*
set "CODE=%ERRORLEVEL%"

rem Keep the window open when started by double-click, so messages can be read.
echo %CMDCMDLINE% | find /i "%~0" >nul && pause
exit /b %CODE%
