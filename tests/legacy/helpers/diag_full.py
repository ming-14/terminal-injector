"""综合诊断：启动 cmd + 注入 + 立即 cdb 附加 + 全面反汇编。

定位 DllMain 期间 CloseHandle 调用链：
  - 线程 3 的真实 RIP
  - 栈上返回地址对应的符号（ln 命令）
  - 调用 KERNELBASE!CloseHandle 的 call 指令位置
  - CloseHandle_orig 指针值（trampoline 地址）
  - trampoline 内容
  - InstallAll 反汇编
"""
import os
import subprocess
import time

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")
LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")
CDB = os.path.join(
    PROJECT_ROOT,
    ".agents", "skills", "windows-debugging", "10.0.19041.5609", "cdb.exe",
)
SYM = "srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols;" + BUILD_BIN

# cdb 脚本：切到线程 3，抓栈，反汇编关键点
CDB_SCRIPT = r"""
~3s
.echo === [thread 3 rip] ===
r rip
.echo === [stack kb] ===
kb
.echo === [ln retaddr from stack top] ===
ln poi(@rsp)
.echo === [disasm caller of CloseHandle - 10 bytes] ===
u poi(@rsp)-10 L12
.echo === [CloseHandle_orig pointer value] ===
dp injected!terminjector::hooks::CloseHandle_orig L1
.echo === [disasm trampoline (CloseHandle_orig target)] ===
u poi(injected!terminjector::hooks::CloseHandle_orig) L12
.echo === [KERNELBASE!CloseHandle first bytes] ===
u KERNELBASE!CloseHandle L5
.echo === [InstallAll disasm] ===
u injected!terminjector::HookManager::InstallAll L50
.echo === [DllMain disasm around +0x7e] ===
u injected!DllMain+0x60 L20
qd
"""


def main():
    # 1. 启动 cmd
    print("[1] 启动 cmd...")
    cmd_proc = subprocess.Popen(
        ["cmd.exe"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=PROJECT_ROOT,
    )
    pid = cmd_proc.pid
    print("    cmd PID={}".format(pid))
    time.sleep(1.0)

    # 2. 清旧日志
    for p in [LOG_PATH]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    # 3. 跑 injector（异步，不等待，因为 DllMain 会卡住）
    inject_cmd = [MEDIATOR_EXE, "--inject", str(pid), "--dll", INJECTED_DLL]
    print("[2] 跑 injector (async): {}".format(" ".join(inject_cmd)))
    inj_proc = subprocess.Popen(inject_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # 等 injector 跑一会（DllMain 应该已卡住）
    time.sleep(3.0)

    # 4. cdb 附加 + 跑脚本
    out = os.path.join(BUILD_BIN, "cdb_full_diag_{}.txt".format(pid))
    cdb_args = [CDB, "-p", str(pid), "-y", SYM, "-i", BUILD_BIN, "-logo", out]
    # 写脚本文件
    script_file = os.path.join(BUILD_BIN, "cdb_script.txt")
    with open(script_file, "w", encoding="ascii") as f:
        f.write(CDB_SCRIPT)
    cdb_args += ["-cf", script_file]
    print("[3] cdb 附加 + 反汇编...")
    try:
        subprocess.run(cdb_args, timeout=60, capture_output=True)
    except subprocess.TimeoutExpired:
        print("    cdb 超时")

    # 5. 打印结果
    print("[4] === cdb 输出 ===")
    if os.path.exists(out):
        with open(out, "r", encoding="utf-8", errors="ignore") as f:
            print(f.read())
    else:
        print("    cdb 日志未生成")

    # 6. 终止 injector（如果还活着）
    try:
        inj_proc.terminate()
    except Exception:
        pass

    # 7. 不杀 cmd，保留现场
    print("\n[done] cmd PID={} 保留现场".format(pid))
    print("如需清理: Stop-Process -Id {} -Force".format(pid))


if __name__ == "__main__":
    main()
