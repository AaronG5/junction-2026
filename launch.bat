@echo off
rem Windows launcher: double-click this file, or run it from a terminal.
rem All the work happens in Python (local_ai/), run through uv. uv also
rem downloads a suitable Python by itself, so nothing else needs installing.
setlocal
cd /d "%~dp0"

set "UV="
where uv >nul 2>&1 && set "UV=uv"
rem The uv installer puts uv here; a fresh install isn't on PATH until a new window.
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

if not defined UV (
    echo uv is needed to run Veritas Lens and was not found.
    echo It is a small tool that manages Python for this project: https://docs.astral.sh/uv/
    choice /M "Install uv now"
    if errorlevel 2 goto :nouv
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
)
if not defined UV goto :nouv

"%UV%" run --quiet python -m local_ai %*
set "CODE=%ERRORLEVEL%"
goto :end

:nouv
echo.
echo Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run launch.bat again.
set "CODE=1"

:end
rem Keep the window open when started by double-click, so messages can be read.
rem Explorer starts batch files as: cmd.exe /c ""C:\path\launch.bat" "  (note the
rem trailing quote-space-quote), which a terminal or another program never does.
echo(%CMDCMDLINE%| findstr /r /c:"\" \"$" >nul && pause
exit /b %CODE%
