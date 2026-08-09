# legacy PowerShell 调试脚本统一路径解析（环境变量优先，系统位置兜底）。
# 与 tests/legacy/paths.py 对齐：dot-source 后提供 $ProjectRoot / $BuildBin /
# $CdbTools / $CdbExe / $SymbolPath / $InjectedLogDir / $LegacyOutDir / $DumpDir /
# $MsSymbolSrv。
# 原则（工程规范：不硬编码路径）：
#   - 项目根：TI_PROJECT_ROOT，缺省按本文件位置相对解析（上三级）
#   - cdb 工具：TI_CDB_TOOLS，缺省 PROJECT_ROOT/.agents/skills/windows-debugging/<版本>
#   - 符号路径：TI_SYMBOL_PATH，缺省 srv*<TEMP>\symbols*<微软符号服务器>
#   - DLL 日志目录：TI_INJECTED_LOG_DIR，缺省 TEMP
#   - 输出目录：TI_LEGACY_OUT_DIR，缺省 <TEMP>\terminjector_legacy_out
#   - 崩溃转储：TI_DUMP_DIR，缺省 <TEMP>\terminjector_dumps
# 不回退到任何本机用户名路径。

$MsSymbolSrv = "http://msdl.blackint3.com:88/download/symbols"

$_DefaultCdbRel = ".agents\skills\windows-debugging\10.0.19041.5609"

# 项目根：TI_PROJECT_ROOT 或按 paths.ps1 位置相对解析（上三级）。
$ProjectRoot = $env:TI_PROJECT_ROOT
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}

# 构建产物目录（Release）。
$BuildBin = Join-Path $ProjectRoot "build\bin\Release"

# cdb 工具目录：TI_CDB_TOOLS 或项目 .agents 下默认版本。
$CdbTools = $env:TI_CDB_TOOLS
if (-not $CdbTools) {
    $CdbTools = Join-Path $ProjectRoot $_DefaultCdbRel
}

# cdb.exe 全路径。
$CdbExe = Join-Path $CdbTools "cdb.exe"

# 符号路径：TI_SYMBOL_PATH 或 srv*<TEMP>\symbols*<符号服务器>。
$SymbolPath = $env:TI_SYMBOL_PATH
if (-not $SymbolPath) {
    $SymbolPath = "srv*{0}*{1}" -f (Join-Path $env:TEMP "symbols"), $MsSymbolSrv
}

# DLL 注入日志目录：TI_INJECTED_LOG_DIR 或 TEMP。
$InjectedLogDir = $env:TI_INJECTED_LOG_DIR
if (-not $InjectedLogDir) {
    $InjectedLogDir = $env:TEMP
}

# legacy 输出目录：TI_LEGACY_OUT_DIR 或 <TEMP>\terminjector_legacy_out。
$LegacyOutDir = $env:TI_LEGACY_OUT_DIR
if (-not $LegacyOutDir) {
    $LegacyOutDir = Join-Path $env:TEMP "terminjector_legacy_out"
}
if (-not (Test-Path $LegacyOutDir)) {
    New-Item -ItemType Directory -Path $LegacyOutDir -Force | Out-Null
}

# 崩溃转储目录：TI_DUMP_DIR 或 <TEMP>\terminjector_dumps。
$DumpDir = $env:TI_DUMP_DIR
if (-not $DumpDir) {
    $DumpDir = Join-Path $env:TEMP "terminjector_dumps"
}
