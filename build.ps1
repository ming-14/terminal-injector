# Build script for terminal-injector
$vsPath = "C:\Program Files\Microsoft Visual Studio\18\Community"
$vsDevCmd = Join-Path $vsPath "Common7\Tools\VsDevCmd.bat"
$msbuild = Join-Path $vsPath "MSBuild\Current\Bin\MSBuild.exe"

# Initialize VS environment
& cmd /c "`"$vsDevCmd`" -arch=x64 -host_arch=x64 & set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

# Build
& $msbuild "c:\Users\rikka\Desktop\terminal-injector\build\ALL_BUILD.vcxproj" /p:Configuration=Release /p:Platform=x64 /t:Rebuild /m
exit $LASTEXITCODE