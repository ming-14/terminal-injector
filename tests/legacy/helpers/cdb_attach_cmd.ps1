Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$cdb = "c:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"
$sympath = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols;c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
$pid_target = 4016
$logo = "c:\temp\cdb_cmd_4016.log"
$scriptFile = "c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_script.txt"

Write-Output "Attaching cmd PID=$pid_target"
& $cdb -p $pid_target -y $sympath -pb -cf $scriptFile -logo $logo -G 2>&1 | Select-Object -First 5
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 200
