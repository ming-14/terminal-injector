# 构建 32 位中继 DLL（relay32.dll），并把产物拷到 x64 输出目录
#
# 为什么单独一棵构建树：
#   relay32 必须与它要注入的 32 位进程（如 C:\Windows\py.exe）同位数，
#   而主产物是 x64。一个 CMake 构建树的平台固定，无法同时产出两种位数，
#   故单独配置 build32（-A Win32，根 CMakeLists 在该配置下只构建 relay32）。
#
# 为什么拷到 x64 输出目录：
#   injected.dll 的 ProcessHooks 要按自身所在目录定位 relay32.dll，
#   才能决定给 32 位子进程注入哪个 DLL。
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot

$cmakeCmd = Get-Command cmake -ErrorAction SilentlyContinue
if (-not $cmakeCmd) {
    Write-Error 'cmake not found in PATH (needed to build relay32).'
    exit 1
}
$cmake = $cmakeCmd.Source

$build32 = Join-Path $projectRoot 'build32'
$cache = Join-Path $build32 'CMakeCache.txt'
if (-not (Test-Path $cache)) {
    Write-Host '[relay32] configuring build32 (-A Win32) ...'
    & $cmake -S $projectRoot -B $build32 -A Win32
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} elseif (-not (Select-String -Path $cache -Pattern 'CMAKE_GENERATOR_PLATFORM:INTERNAL=Win32' -Quiet)) {
    Write-Error "build32 不是 Win32 配置。请删除 build32 目录后重试。"
    exit 1
}

Write-Host '[relay32] building ...'
& $cmake --build $build32 --config Release --target relay32 relay32inject
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# relay32inject.exe 也要拷：injected.dll 的 ProcessHooks 捕获到 32 位子进程时，
# 按自身所在目录 fork 它来完成同位数注入（跨位数注入不可行）。
$srcDir  = Join-Path $build32 'bin\Release'
$dstDir  = Join-Path $projectRoot 'build\bin\Release'
if (-not (Test-Path $dstDir)) {
    New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
}
foreach ($f in @('relay32.dll', 'relay32.pdb', 'relay32inject.exe', 'relay32inject.pdb')) {
    $p = Join-Path $srcDir $f
    if (Test-Path $p) {
        Copy-Item $p (Join-Path $dstDir $f) -Force
    }
}
Write-Host "[relay32] -> $dstDir\relay32.dll + relay32inject.exe"
exit 0
