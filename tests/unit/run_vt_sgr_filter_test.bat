@echo off
REM Compile and run the isolated VtSgrFilter self-test (no CMake build needed).
REM Usage: double-click, or run from any command line.
REM Set VCVARS to point at vcvars64.bat; otherwise VS is located via vswhere.
setlocal
cd /d "%~dp0"

if defined VCVARS goto have_vcvars
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do set "VSDIR=%%i"
if not defined VSDIR (
    echo [FAIL] Visual Studio not found. Set VCVARS to vcvars64.bat.
    exit /b 1
)
set "VCVARS=%VSDIR%\VC\Auxiliary\Build\vcvars64.bat"

:have_vcvars
if not exist "%VCVARS%" (
    echo [FAIL] vcvars not found: %VCVARS%
    exit /b 1
)
call "%VCVARS%" >nul

set "SRC=..\..\src\dll\translator"
cl /nologo /EHsc /std:c++17 /W4 /utf-8 /I"%SRC%" /Fe:test_vt_sgr_filter.exe test_vt_sgr_filter.cpp "%SRC%\VtSgrFilter.cpp"
if errorlevel 1 (
    echo [FAIL] compile failed
    exit /b 1
)
test_vt_sgr_filter.exe
exit /b %errorlevel%
