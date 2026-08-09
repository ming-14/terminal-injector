@echo off
setlocal
set "BATDIR=%~dp0"
rem 项目根：TI_PROJECT_ROOT 或按脚本位置相对解析（上三级）
if defined TI_PROJECT_ROOT (
    set "ROOT=%TI_PROJECT_ROOT%"
) else (
    for %%I in ("%BATDIR%..\..\..") do set "ROOT=%%~fI"
)
rem cdb 工具目录：TI_CDB_TOOLS 或项目 .agents 下默认版本
if defined TI_CDB_TOOLS (
    set "CDBTOOLS=%TI_CDB_TOOLS%"
) else (
    set "CDBTOOLS=%ROOT%\.agents\skills\windows-debugging\10.0.19041.5609"
)
set "CDB=%CDBTOOLS%\cdb.exe"
rem 构建产物
set "I=%ROOT%\build\bin\Release"
rem 符号路径：TI_SYMBOL_PATH 或 srv*<TEMP>\symbols*<符号服务器>
if defined TI_SYMBOL_PATH (
    set "Y=%TI_SYMBOL_PATH%;%I%"
) else (
    set "Y=srv*%TEMP%\symbols*http://msdl.blackint3.com:88/download/symbols;%I%"
)
rem 日志目录：TI_LEGACY_OUT_DIR 或 <TEMP>\terminjector_legacy_out
if defined TI_LEGACY_OUT_DIR (
    set "OUTDIR=%TI_LEGACY_OUT_DIR%"
) else (
    set "OUTDIR=%TEMP%\terminjector_legacy_out"
)
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
set "LOG=%OUTDIR%\cdb_ddag.log"
set "PID=%1"
set "DDAG=%2"

if exist "%LOG%" del "%LOG%"

"%CDB%" -p %PID% -y "%Y%" -i "%I%" -logo "%LOG%" -c ".reload /f injected.dll; !dlls -c injected.dll; dt ntdll!_LDR_DDAG_NODE %DDAG%; qd"

endlocal
