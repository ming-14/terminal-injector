Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$cdb = "c:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"
$sympath = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols"

$pid_target = 4956
$cmd = "~* kB 30; q"
$logo = "c:\temp\cdb_python_4956.log"

Write-Output "Attaching python PID=$pid_target, log to $logo"
& $cdb -p $pid_target -y $sympath -pv -c $cmd -logo $logo 2>&1 | Select-Object -First 50
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 250
