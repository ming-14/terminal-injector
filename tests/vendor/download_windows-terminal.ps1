# 下载 Windows Terminal 便携版 zip 并解压到 tests\vendor\windows-terminal。
#
# 用法:  .\download_windows-terminal.ps1
#
# 要点:
#   - $urls 按序依次尝试：第 1 条是 GitHub release 直连，其余是「镜像前缀 + 直连 URL」。
#     每个 URL 全程限时 $TimeoutSec 秒，超时（没下载完）立刻换下一个。
#   - sha256 用 $expectedSha 硬编码 pin，每个 URL 下载完立刻比对，对不上就当该 URL 失败、换下一个
#     （镜像被限流时常回 HTML 错误页，字节数看着正常但内容不是原件）。
#     不去线上拉哈希附件：附件同样要过镜像，镜像若把 zip 和哈希一起篡改，校验就成了摆设。
#   - 全部失败 → 退出码 1，已有的 vendor\windows-terminal 原样保留（替换发生在下载+校验+解压都成功之后）。
#     若每个 URL 下到的都是同一份内容却都哈希不符，多半是 $zipUrl 升级了而 $expectedSha 没跟着改，
#     脚本会专门提示，免得误判成网络问题。
#   - zip 内是 terminal-<版本>\ 一层目录，解压后取「直接含 wt.exe 的那层目录」整体改名为
#     vendor\windows-terminal，与 release.yml 的 t\ 布局一致：vendor\windows-terminal\wt.exe。
param(
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = 'Stop'

$zipUrl = 'https://github.com/microsoft/terminal/releases/download/v1.24.11911.0/Microsoft.WindowsTerminal_1.24.11911.0_x64.zip'
# pin 来源：GitHub API release asset 的 digest 字段
#   gh api repos/microsoft/terminal/releases/tags/v1.24.11911.0 \
#     --jq '.assets[] | select(.name|test("x64.zip$")) | .digest'
# 这个 release 没有 .sha256 附件，digest 是唯一权威来源。换版本时用同一条命令取新值填这里。
$expectedSha = '7691efeb71c8dd0b95536c84e366fa4cf809a42c534912f9cefa1056534383bd'
$mirrorPrefixes = @(
    'https://v4.gh-proxy.org/'
    'https://gh-proxy.org/'
    'https://v6.gh-proxy.org/'
    'https://cdn.gh-proxy.org/'
    'https://axisnow.gh-proxy.org/'
)
$urls = @($zipUrl) + ($mirrorPrefixes | ForEach-Object { $_ + $zipUrl })

$targetDir = Join-Path $PSScriptRoot 'windows-terminal'
# 解压结果里必须齐全的文件：拿它校验（三个都在同一层，非子目录）
$required = @('wt.exe', 'WindowsTerminal.exe', 'OpenConsole.exe')

# .NET Framework 默认可能不含 TLS1.2，直连 GitHub 必须显式打开
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor `
    [Net.SecurityProtocolType]::Tls12

# ZipFile 必须在用到之前加载：Windows PowerShell 5.1 下不 Add-Type 就没有该类型。
# 旧版把它放在下载之后，验证函数一调用就抛「找不到类型」→ 被 catch 吞掉 → 合法 zip 恒判非法。
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
$workDir = Join-Path $env:TEMP ('wt_dl_{0}' -f ([Guid]::NewGuid().ToString('N').Substring(0, 8)))
$zipFile = Join-Path $workDir 'windows-terminal.zip'
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
        $err = Invoke-DownloadFile -Url $url -OutFile $zipFile
        if (-not $err) {
            $actual = (Get-FileHash -Path $zipFile -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expectedSha) {
                $err = "sha256 不匹配 (实际 $actual)"
                $shaFailCount++
                $actualSeen.Add($actual)
                Remove-Item $zipFile -Force -ErrorAction SilentlyContinue
            }
        }
        $sw.Stop()
        if (-not $err) {
            $size = (Get-Item $zipFile).Length
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
                "多半是 `$zipUrl 已升级而 `$expectedSha 没跟着改，请同步更新后重跑。已保留现有 $targetDir"
            Write-Error $msg -ErrorAction Continue
        } else {
            Write-Error "全部 $($urls.Count) 个 URL 均失败，已保留现有 $targetDir" -ErrorAction Continue
        }
        exit 1
    }

    # ---- 2) 解压（先解到临时目录，校验通过后才替换目标目录） ----
    $unpackDir = Join-Path $workDir 'unpack'
    New-Item -ItemType Directory -Path $unpackDir | Out-Null
    [IO.Compression.ZipFile]::ExtractToDirectory($zipFile, $unpackDir)

    # ---- 3) 取「直接含 wt.exe 的那层目录」（zip 里是 terminal-<版本>\），不写死目录名 ----
    $exe = Get-ChildItem $unpackDir -Recurse -File -Filter 'wt.exe' | Select-Object -First 1
    if (-not $exe) {
        Write-Error "解压结果里没有 wt.exe，已保留现有 $targetDir" -ErrorAction Continue
        exit 1
    }
    $innerDir = $exe.DirectoryName

    # ---- 4) 必备文件 ----
    foreach ($f in $required) {
        if (-not (Test-Path (Join-Path $innerDir $f))) {
            Write-Error "解压结果缺文件: $f，已保留现有 $targetDir" -ErrorAction Continue
            exit 1
        }
    }

    # ---- 5) 整目录替换 ----
    if (Test-Path $targetDir) {
        Remove-Item $targetDir -Recurse -Force
    }
    Move-Item $innerDir $targetDir

    Write-Host "已解压到 $targetDir"
    foreach ($f in $required) {
        Write-Host ("  {0,-16} {1}" -f $f, (Get-Item (Join-Path $targetDir $f)).Length)
    }
} finally {
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
}
