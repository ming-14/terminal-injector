@echo off
setlocal
set VSCMD_START_DIR=C:\Users\rikka\Desktop\terminal-injector
call C:\PROGRA~1\MIB055~1\18\COMMUN~1\Common7\Tools\VsDevCmd.bat -arch=x64 -host_arch=x64
echo VS_ENV_INIT_DONE
msbuild "C:\Users\rikka\Desktop\terminal-injector\build\src\dll\injected_dll.vcxproj" /p:Configuration=Release /p:Platform=x64 /t:Clean;Build /nologo /v:m
echo BUILD_EXIT_CODE=%ERRORLEVEL%
endlocal