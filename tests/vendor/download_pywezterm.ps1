# 下载 pywezterm wheel 并解包到 tests\vendor\pywezterm。
#
# 用法:  .\download_pywezterm.ps1
#
# 要点:
#   - $urls 按序依次尝试：第 1 条是 GitHub release 直连，其余是「镜像前缀 + 直连 URL」。
#     每个 URL 全程限时 $TimeoutSec 秒，超时（没下载完）立刻换下一个。
#   - sha256 用 $expectedSha 硬编码 pin，每个 URL 下载完立刻比对，对不上就当该 URL 失败、换下一个
#     （镜像被限流时常回 HTML 错误页，字节数看着正常但内容不是 wheel）。
#     不去线上拉 SHA256SUMS.txt：附件同样要过镜像，镜像若把 wheel 和哈希一起篡改，校验就成了摆设。
#   - 全部失败 → 退出码 1，已有的 vendor\pywezterm 原样保留（替换发生在下载+校验+解包都成功之后）。
#     若每个 URL 下到的都是同一份内容却都哈希不符，多半是 $wheelUrl 换了平台/版本而 $expectedSha
#     没跟着改，脚本会专门提示，免得误判成网络问题。
#   - wheel 是 zip，包目录 pywezterm/ 是内层；解包后取「直接含 pywezterm.pyd 的那层目录」整体改名为
#     vendor\pywezterm —— paths.pywezterm_dir() 取的是它的父目录 tests\vendor，布局不能变。
#     整目录替换也顺带清掉旧版残留的 conpty\ 子目录和 __pycache__。
param(
    [int]$TimeoutSec = 60
)

$ErrorActionPreference = 'Stop'

$wheelUrl = 'https://github.com/ming-14/pywezterm/releases/download/v1.1/pywezterm-0.1.0-cp38-abi3-win_amd64.whl'
# pin 来源：GitHub API digest 与 release 的 SHA256SUMS.txt 原文两处一致
#   gh api repos/ming-14/pywezterm/releases/tags/v1.1 \
#     --jq '.assets[] | select(.name|test("win_amd64.whl$")) | .digest'
# 换平台/版本时用同一条命令取新值填这里。
$expectedSha = '079fd731af27581debd6f94f39fdae20bbbb4a0d33c70030407be58d57228515'
$mirrorPrefixes = @(
    'https://v4.gh-proxy.org/'
    'https://gh-proxy.org/'
    'https://v6.gh-proxy.org/'
    'https://cdn.gh-proxy.org/'
    'https://axisnow.gh-proxy.org/'
)
$urls = @($wheelUrl) + ($mirrorPrefixes | ForEach-Object { $_ + $wheelUrl })

$targetDir = Join-Path $PSScriptRoot 'pywezterm'
# 包目录下必须齐全的文件：拿它校验（v1.1 布局是 conpty.dll / OpenConsole.exe 与 pywezterm.pyd 同级）
$required = @('__init__.py', 'pywezterm.pyd', 'conpty.dll', 'OpenConsole.exe')

# .NET Framework 默认可能不含 TLS1.2，直连 GitHub 必须显式打开
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor `
    [Net.SecurityProtocolType]::Tls12

# ZipFile 必须在用到之前加载：Windows PowerShell 5.1 下不 Add-Type 就没有该类型。
# 旧版把它放在下载之后，Test-WheelFile 一调用就抛「找不到类型」→ 被 catch 吞掉 → 恒返回 false，
# 于是 6 个 URL 全判「内容非法」，5.1 下 100% 失败（pwsh 下类型自带，才一直没暴露）。
Add-Type -AssemblyName System.IO.Compression.FileSystem

# 输出被重定向（日志捕获）时按 UTF-8 写出，否则中文会按控制台 GBK 解码成乱码；
# 交互式终端保持宿主自己的编码，不动调用方状态
if ([Console]::IsOutputRedirected) {
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
}

function Invoke-DownloadFile {
    # 单个 URL 下载，$TimeoutSec 全程限时。返回 $null=成功，否则是失败原因。
    # 只负责取到字节，校验交给调用方（哈希比对）。
    param([string]$Url, [string]$OutFile)

    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $true
    $handler.MaxAutomaticRedirections = 10   # release 直连会 302 到 objects.githubusercontent.com
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('terminal-injector/vendordl')
    # 总时钟：从发起请求开始算，覆盖「连上了但一直下不完」的慢速拉取
    $cts = New-Object System.Threading.CancellationTokenSource([TimeSpan]::FromSeconds($TimeoutSec))

    try {
        # 不能跨行续接 .GetAwaiter()：PowerShell 只把「换行」当作语句结束，前导点号会另起一条语句
        $respTask = $client.GetAsync($Url, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cts.Token)
        $resp = $respTask.GetAwaiter().GetResult()
        if (-not $resp.IsSuccessStatusCode) {
            return "HTTP $([int]$resp.StatusCode) $($resp.ReasonPhrase)"
        }
        $src = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $dst = [IO.File]::Create($OutFile)
        try {
            # 非泛型 Task 的 GetResult() 会回一个 VoidTaskResult，不赋值就会混进函数输出 → 误判为失败
            $null = $src.CopyToAsync($dst, 1MB, $cts.Token).GetAwaiter().GetResult()
        } finally {
            $dst.Dispose()
            $src.Dispose()
        }
        return   # 成功：不产出任何值，$err 才会是 $null
    } catch {
        if ($cts.IsCancellationRequested) { return "超过 ${TimeoutSec}s 未完成" }
        # 逐层下钻：HttpClient 的异常通常套着 AggregateException/WebException，根因在最里层
        $e = $_.Exception
        while ($e.InnerException) { $e = $e.InnerException }
        return ('{0}: {1}' -f $e.GetType().Name, $e.Message)
    } finally {
        $cts.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

# ---- 工作目录 ----
$workDir = Join-Path $env:TEMP ('pywezterm_dl_{0}' -f ([Guid]::NewGuid().ToString('N').Substring(0, 8)))
$wheelFile = Join-Path $workDir 'pywezterm.whl'
New-Item -ItemType Directory -Path $workDir | Out-Null

try {
    Write-Host ("期望 sha256 (pin): {0}" -f $expectedSha)

    # ---- 1) 下载 + 哈希校验：依次尝试每个 URL ----
    $got = $false
    $shaFailCount = 0
    $actualSeen = New-Object 'System.Collections.Generic.List[string]'
    for ($i = 0; $i -lt $urls.Count; $i++) {
        $url = $urls[$i]
        Write-Host ("[{0}/{1}] {2}" -f ($i + 1), $urls.Count, $url)
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $err = Invoke-DownloadFile -Url $url -OutFile $wheelFile
        if (-not $err) {
            $actual = (Get-FileHash -Path $wheelFile -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expectedSha) {
                $err = "sha256 不匹配 (实际 $actual)"
                $shaFailCount++
                $actualSeen.Add($actual)
                Remove-Item $wheelFile -Force -ErrorAction SilentlyContinue
            }
        }
        $sw.Stop()
        if (-not $err) {
            $size = (Get-Item $wheelFile).Length
            Write-Host ("          OK: {0:N1} MB, {1:N1}s" -f ($size / 1MB), $sw.Elapsed.TotalSeconds)
            $got = $true
            break
        }
        Write-Host ("          失败 ({0:N1}s): {1}" -f $sw.Elapsed.TotalSeconds, $err)
    }

    if (-not $got) {
        # -ErrorAction Continue：$ErrorActionPreference=Stop 会让 Write-Error 变成终止性错误，
        # 下面的 exit 1 永远走不到（退出码靠异常隐式给，语义不明确）
        if ($shaFailCount -eq $urls.Count -and ($actualSeen | Select-Object -Unique).Count -eq 1) {
            # 所有 URL 都下到了同一份内容却都不匹配 pin → 不是网络问题，是版本和哈希没同步
            $msg = "全部 $($urls.Count) 个 URL 下到的内容 sha256 一致但与 pin 不符（实际 $($actualSeen[0])）。`n" +
                "多半是 `$wheelUrl 已换平台/版本而 `$expectedSha 没跟着改，请同步更新后重跑。已保留现有 $targetDir"
            Write-Error $msg -ErrorAction Continue
        } else {
            Write-Error "全部 $($urls.Count) 个 URL 均失败，已保留现有 $targetDir" -ErrorAction Continue
        }
        exit 1
    }

    # ---- 2) 解包（先解到临时目录，校验通过后才替换目标目录） ----
    $unpackDir = Join-Path $workDir 'unpack'
    New-Item -ItemType Directory -Path $unpackDir | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($wheelFile, $unpackDir)

    # ---- 3) 取「直接含 pywezterm.pyd 的那层目录」（wheel 里是 pywezterm/），不写死目录名 ----
    $pyd = Get-ChildItem $unpackDir -Recurse -File -Filter 'pywezterm.pyd' | Select-Object -First 1
    if (-not $pyd) {
        Write-Error "解包结果里没有 pywezterm.pyd，已保留现有 $targetDir" -ErrorAction Continue
        exit 1
    }
    $innerDir = $pyd.DirectoryName

    # ---- 4) 必备文件 ----
    foreach ($f in $required) {
        if (-not (Test-Path (Join-Path $innerDir $f))) {
            Write-Error "解包结果缺文件: $f，已保留现有 $targetDir" -ErrorAction Continue
            exit 1
        }
    }

    # ---- 5) 整目录替换 ----
    if (Test-Path $targetDir) {
        Remove-Item $targetDir -Recurse -Force
    }
    Move-Item $innerDir $targetDir

    Write-Host "已解包到 $targetDir"
    foreach ($f in $required) {
        Write-Host ("  {0,-16} {1}" -f $f, (Get-Item (Join-Path $targetDir $f)).Length)
    }
} finally {
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
}
