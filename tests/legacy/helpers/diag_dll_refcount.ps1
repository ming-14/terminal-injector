$TOOLS = "c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
$SYMSRV = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols"
$IMGPATH = "c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
$OUT = "c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_dlls_out.txt"
$cdb = Join-Path $TOOLS "cdb.exe"
$cmdArgs = @(
    "-p", "7360",
    "-y", "$SYMSRV;$IMGPATH",
    "-i", $IMGPATH,
    "-logo", $OUT,
    "-c", ".symfix srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols; .reload /f ntdll.dll; !dlls -c:injected.dll; qd"
)
Write-Host "Running: $cdb $cmdArgs"
$ret = & $cdb @cmdArgs
Write-Host "exit: $LASTEXITCODE"
$ret | Select-Object -Last 30
