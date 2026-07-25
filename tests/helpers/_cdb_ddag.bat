@echo off
setlocal
set CDB=C:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609\cdb.exe
set Y=srv*C:\symbols*http://msdl.blackint3.com:88/download/symbols;C:\Users\rikka\Desktop\terminal-injector\build\bin\Release
set I=C:\Users\rikka\Desktop\terminal-injector\build\bin\Release
set LOG=C:\temp\cdb_ddag.log
set PID=%1
set DDAG=%2

if exist "%LOG%" del "%LOG%"

"%CDB%" -p %PID% -y "%Y%" -i "%I%" -logo "%LOG%" -c ".reload /f injected.dll; !dlls -c injected.dll; dt ntdll!_LDR_DDAG_NODE %DDAG%; qd"

endlocal
