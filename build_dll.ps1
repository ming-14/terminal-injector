$vsPath = "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\Tools\VsDevCmd.bat"
$msbuild = "C:\Program Files\Microsoft Visual Studio\18\Community\MSBuild\Current\Bin\MSBuild.exe"

# Initialize VS environment
cmd.exe /c "`"$vsPath`" -arch=x64 -host_arch=x64 > nul 2>&1 && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

# Run MSBuild
& $msbuild "c:\Users\rikka\Desktop\terminal-injector\build\src\dll\injected_dll.vcxproj" /p:Configuration=Release /p:Platform=x64 /t:Clean;Build /nologo /v:m