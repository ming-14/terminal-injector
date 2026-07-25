"""用 cdb 查 _LDR_DDAG_NODE 和 _LDR_DATA_TABLE_ENTRY 的准确结构定义。

attach 到指定 PID，查类型后分离（不杀进程）。
用于 Phase 11 卸载诊断：确认 +0x18 字段的真实含义。

用法：python tests\helpers\cdb_dump_ddag.py <pid>
"""
import os
import subprocess
import sys

TOOLS = r"c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
# 符号缓存目录用 C:\symbols（e:\Symbol 盘不存在）
SYMSRV = "srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols"
IMGPATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
OUT = r"c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_ddag_struct.txt"

if len(sys.argv) < 2:
    print("用法: python cdb_dump_ddag.py <pid>")
    sys.exit(1)
pid = int(sys.argv[1])

# cdb 命令：
#   -p <pid>               attach 到进程
#   -y <sympath>           符号路径
#   -i <imgpath>           镜像路径
#   -logo <file>           日志输出到文件
#   -c "<commands>"        执行命令后退出
# 命令：
#   .reload /f ntdll.dll   强制重新加载 ntdll 符号（从 symbol server 下载）
#   dt ntdll!_LDR_DDAG_NODE          查 DDAG 结构
#   dt ntdll!_LDR_DATA_TABLE_ENTRY   查 Entry 结构
#   dt ntdll!_PEB_LDR_DATA           查 Ldr 结构
#   qd                     分离（不杀进程）
cmd_str = ('"{}" -p {} -y "{};{}" -i "{}" -logo "{}" '
           '-c ".reload /f ntdll.dll; dt ntdll!_LDR_DDAG_NODE; '
           'dt ntdll!_LDR_DATA_TABLE_ENTRY; dt ntdll!_PEB_LDR_DATA; qd"')
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
    if ret.stdout:
        print("\n[cdb stdout tail]", flush=True)
        print(ret.stdout[-2000:], flush=True)
    if ret.stderr:
        print("\n[cdb stderr]", flush=True)
        print(ret.stderr[-1000:], flush=True)
except subprocess.TimeoutExpired:
    print("cdb 超时", flush=True)

# 读输出，提取 dt 部分
if os.path.exists(OUT):
    print("\n[cdb 输出文件 {}]".format(OUT), flush=True)
    with open(OUT, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    # 找 dt 输出部分（ntdll! 开头的行 + 后续的 +0x 字段行）
    in_dt = False
    for line in content.splitlines():
        if line.startswith("ntdll!"):
            in_dt = True
            print(line, flush=True)
        elif in_dt and line.startswith("   +0x"):
            print(line, flush=True)
        elif in_dt and not line.strip():
            in_dt = False
            print(line, flush=True)
        elif "Unable" in line or "error" in line.lower():
            print(line, flush=True)
