"""用 cdb 查 injected.dll 的依赖关系和反向引用。

attach 到指定 PID，查依赖树后分离（不杀进程）。
用于 Phase 11 卸载诊断：定位 LoadCount=5 的引用来源。

用法：python tests\helpers\cdb_dump_deps.py <pid>
"""
import os
import subprocess
import sys

TOOLS = r"c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
# 符号缓存目录用 C:\symbols（e:\Symbol 盘不存在）
SYMSRV = "srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols"
IMGPATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
OUT = r"c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_deps_out.txt"

if len(sys.argv) < 2:
    print("用法: python cdb_dump_deps.py <pid>")
    sys.exit(1)
pid = int(sys.argv[1])

# cdb 命令：
#   !dlls -d               显示所有 DLL 的依赖关系（forward）
#   !dlls -c injected      显示谁引用了 injected.dll（backward）
#   !dlls -i               显示初始化顺序
#   !dlls                  显示所有 DLL 概要
cmd_str = ('"{}" -p {} -y "{};{}" -i "{}" -logo "{}" '
           '-c ".reload /f ntdll.dll; !dlls -c injected; qd"')
cmd_str = cmd_str.format(
    os.path.join(TOOLS, "cdb.exe"),
    pid,
    SYMSRV, IMGPATH,
    IMGPATH,
    OUT,
)
print("运行 cdb: {}".format(cmd_str), flush=True)
try:
    ret = subprocess.run(cmd_str, shell=True, capture_output=True,
                         text=True, timeout=120)
    print("cdb exit code: {}".format(ret.returncode), flush=True)
except subprocess.TimeoutExpired:
    print("cdb 超时", flush=True)

# 读输出
if os.path.exists(OUT):
    print("\n[cdb 输出文件 {}]".format(OUT), flush=True)
    with open(OUT, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    # 找 !dlls 输出部分（通常以 "0x" 地址开头）
    in_dlls = False
    for line in content.splitlines():
        # !dlls -c 的输出包含 "refs:" 和 "dependent" 等关键字
        if ("dlls" in line.lower() or "refs" in line.lower()
                or "dependent" in line.lower()
                or "injected" in line.lower()
                or line.strip().startswith("0x")
                or "LoadCount" in line
                or "State" in line):
            print(line, flush=True)
