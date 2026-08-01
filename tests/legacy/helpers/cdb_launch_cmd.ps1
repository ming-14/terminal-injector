Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$cdb = "c:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe"
$sympath = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols;c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
$logo = "c:\temp\cdb_launch_cmd.log"

# Launch cmd under cdb, read global variables, then detach
$cmd = "dd injected!g_probe_wf L1; dd injected!g_probe_dllmain L1; dd injected!g_probe_wcw L1; q"
Write-Output "Launching cmd under cdb..."
& $cdb -G -g -o -y $sympath -logo $logo -c $cmd "cmd.exe" 2>&1 | Select-Object -First 5
Write-Output "---log content---"
Get-Content $logo -ErrorAction SilentlyContinue | Select-Object -First 100
