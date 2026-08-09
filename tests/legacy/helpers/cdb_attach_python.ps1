Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\paths.ps1"

$pid_target = 4956
$cmd = "~* kB 30; q"
$logo = Join-Path $LegacyOutDir "cdb_python_$pid_target.log"

Write-Output "Attaching python PID=$pid_target, log to $logo"
& $CdbExe -p $pid_target -y $SymbolPath -pv -c $cmd -logo $logo 2>&1 | Select-Object -First 50
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 250
