# 统计 injected.dll 在目标进程中的引用计数（cdb !dlls）。
# 路径解析见 paths.ps1（环境变量优先，不硬编码）。
. "$PSScriptRoot\paths.ps1"

$pid_target = 7360
$logo = Join-Path $LegacyOutDir "cdb_dlls_out.txt"
$cmdArgs = @(
    "-p", "$pid_target",
    "-y", "$SymbolPath;$BuildBin",
    "-i", $BuildBin,
    "-logo", $logo,
    "-c", ".symfix $SymbolPath; .reload /f ntdll.dll; !dlls -c:injected.dll; qd"
)
Write-Host "Running: $CdbExe $cmdArgs"
$ret = & $CdbExe @cmdArgs
Write-Host "exit: $LASTEXITCODE"
$ret | Select-Object -Last 30
