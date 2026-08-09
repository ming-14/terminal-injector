Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
. "$PSScriptRoot\paths.ps1"

$sympath = "$SymbolPath;$BuildBin"
$pid_target = 4016
$logo = Join-Path $LegacyOutDir "cdb_cmd_$pid_target.log"
$scriptFile = Join-Path $ProjectRoot "tests\helpers\cdb_script.txt"

Write-Output "Attaching cmd PID=$pid_target"
& $CdbExe -p $pid_target -y $sympath -pb -cf $scriptFile -logo $logo -G 2>&1 | Select-Object -First 5
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 200
