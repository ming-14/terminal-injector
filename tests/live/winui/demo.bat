@echo off
REM Run the winui TUI demo (fullscreen console app; Ctrl+Q to quit).
REM Usage: double-click, or run from any command line. Extra args are passed through (e.g. demo.bat --debug).
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [FAIL] python not found in PATH.
    pause
    exit /b 1
)

python examples\demo.py %*
set "RC=%errorlevel%"
if not "%RC%"=="0" pause
exit /b %RC%
