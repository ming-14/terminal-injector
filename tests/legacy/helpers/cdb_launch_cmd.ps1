Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
. "$PSScriptRoot\paths.ps1"

$sympath = "$SymbolPath;$BuildBin"
$logo = Join-Path $LegacyOutDir "cdb_launch_cmd.log"

# Launch cmd under cdb, read global variables, then detach
$cmd = "dd injected!g_probe_wf L1; dd injected!g_probe_dllmain L1; dd injected!g_probe_wcw L1; q"
Write-Output "Launching cmd under cdb..."
& $CdbExe -G -g -o -y $sympath -logo $logo -c $cmd "cmd.exe" 2>&1 | Select-Object -First 5
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 100
