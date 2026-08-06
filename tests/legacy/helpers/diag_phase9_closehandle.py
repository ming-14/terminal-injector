"""诊断 Phase 9 CloseHandle Hook 是否在 python 子进程中生效。

流程：
  1. 启动 cmd + WT(mediator) + 注入
  2. 在 cmd 中运行 python sleep 脚本（python 被 ProcessHooks 自动注入）
  3. 找到 python 子进程 pid
  4. 用 cdb 附加到 python，检查 kernelbase!CloseHandle 字节
  5. 退出 cdb，清理

检查项：
  - kernelbase!CloseHandle 前 16 字节是否有 jmp 指令（Hook 标志）
  - injected!CloseHandle_Detour 地址
  - 对比 cmd 进程与 python 进程的 Hook 状态
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

BUILD_BIN = paths.build_bin()
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")
CDB_EXE = paths.cdb_exe()

# cdb 脚本：检查 kernelbase!CloseHandle 是否被 Hook
#
# 关键检查项：
#   1. kernelbase!CloseHandle 反汇编：首指令是否为 E9 jmp（Hook 标志）
#   2. CloseHandle_Detour 地址（因 ASLR 每次不同）
#   3. CloseHandle_orig 指针值（trampoline 地址）
#   4. trampoline 反汇编（应包含原始字节 + jmp 回原 API +N）
#   5. g_initialized（LazyInit 是否完成）
CDB_SCRIPT = """
.echo === reload symbols ===
.reload /f injected.dll
.echo

.echo === kernelbase!CloseHandle disassembly (Hook installed?) ===
u kernelbase!CloseHandle L5
.echo

.echo === kernelbase!CloseHandle raw bytes (first 16) ===
db kernelbase!CloseHandle L10
.echo

.echo === CloseHandle_Detour address ===
x injected!terminjector::hooks::CloseHandle_Detour
.echo

.echo === CloseHandle_Detour disassembly (magic check logic) ===
u injected!terminjector::hooks::CloseHandle_Detour L25
.echo

.echo === CloseHandle_orig pointer value (trampoline addr) ===
dq injected!terminjector::hooks::CloseHandle_orig L1
.echo

.echo === trampoline disassembly ===
u poi(injected!terminjector::hooks::CloseHandle_orig) L5
.echo

.echo === relay content (sign-extended disp32) ===
r $t0 = dwo(kernelbase!CloseHandle + 1)
.if ($t0 & 0x80000000) { r $t0 = $t0 - 0x100000000 }
r $t1 = kernelbase!CloseHandle + 5 + $t0
db $t1 L10
.echo

.echo === relay target (qword at relay+6, should == CloseHandle_Detour) ===
dq $t1 + 6 L1
.echo

.echo === CloseHandle_Detour address (compare) ===
? injected!terminjector::hooks::CloseHandle_Detour
.echo

.echo === g_initialized (LazyInit done?) ===
db injected!terminjector::g_initialized L1
.echo

.echo === AllocConsole_Detour (compare: is it hooked too?) ===
u kernelbase!AllocConsole L3
.echo

.echo === FreeConsole_Detour (compare: is it hooked too?) ===
u kernelbase!FreeConsole L3
.echo

q
"""


def find_python_child(cmd_pid: int) -> int:
    """找到 cmd 的 python 子进程 pid。"""
    import psutil
    try:
        p = psutil.Process(cmd_pid)
        for child in p.children(recursive=True):
            if child.name().lower() == "python.exe":
                return child.pid
    except psutil.NoSuchProcess:
        pass
    return 0


def run_cdb(pid: int, script: str, output_path: str) -> None:
    """用 cdb 附加到进程，执行脚本，输出到文件。"""
    script_path = os.path.join(PROJECT_ROOT, "diag_cdb_script.txt")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    # cdb -p <pid> -cf <script> -y <sympath>
    # 符号路径：含 injected.pdb 所在目录 + 微软符号服务器
    sympath = "srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols;{}".format(BUILD_BIN)
    cmd = [CDB_EXE, "-p", str(pid), "-cf", script_path, "-y", sympath]
    print("  [cdb] 命令: {}".format(" ".join(cmd)))
    with open(output_path, "w", encoding="utf-8") as out:
        proc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                              timeout=30)
    print("  [cdb] 退出码: {}".format(proc.returncode))


def main() -> int:
    print("=== Phase 9 CloseHandle Hook 诊断 ===")
    print()

    # 1. 启动测试环境
    print("[1] 启动 cmd + WT(mediator) + 注入...")
    cmd_pid = injector.start_target_cmd()
    print("  cmd PID={}".format(cmd_pid))
    injector.clear_log()
    mediator_proc = injector.start_wt_mediator(cmd_pid)
    print("  等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[FATAL] 握手失败")
        injector.cleanup(cmd_pid, mediator_proc)
        return 1
    print("  握手成功")
    time.sleep(2.0)
    injector.focus_wt()
    time.sleep(1.0)

    # 2. 在 cmd 中运行 python sleep 脚本
    print()
    print("[2] 在 cmd 中运行 python sleep 脚本...")
    cmd_line = "python -c \"import time; time.sleep(300)\""
    print("  输入: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.5)
    input_sim.type_enter()

    # 等待 python 启动
    print("  等待 python 启动...")
    py_pid = 0
    deadline = time.time() + 15.0
    while time.time() < deadline:
        py_pid = find_python_child(cmd_pid)
        if py_pid:
            break
        time.sleep(0.5)

    if not py_pid:
        print("[FATAL] 未找到 python 子进程")
        injector.cleanup(cmd_pid, mediator_proc)
        return 1
    print("  python PID={}".format(py_pid))

    # 等待 python 的 DLL 注入完成（worker 线程 Sleep 100ms + LazyInit）
    print("  等待 python DLL 注入与 Hook 安装（2秒）...")
    time.sleep(2.0)

    # 3. 用 cdb 附加到 python，检查 Hook 状态
    print()
    print("[3] cdb 附加到 python 检查 Hook 状态...")
    py_output = os.path.join(BUILD_BIN, "diag_cdb_python.txt")
    try:
        run_cdb(py_pid, CDB_SCRIPT, py_output)
        print("  输出: {}".format(py_output))
    except Exception as e:
        print("  [ERROR] cdb 失败: {}".format(e))

    # 4. 也用 cdb 附加到 cmd，对比 Hook 状态
    print()
    print("[4] cdb 附加到 cmd 对比 Hook 状态...")
    cmd_output = os.path.join(BUILD_BIN, "diag_cdb_cmd.txt")
    try:
        run_cdb(cmd_pid, CDB_SCRIPT, cmd_output)
        print("  输出: {}".format(cmd_output))
    except Exception as e:
        print("  [ERROR] cdb 失败: {}".format(e))

    # 5. 清理
    print()
    print("[5] 清理...")
    injector.cleanup(cmd_pid, mediator_proc)

    print()
    print("=== 诊断完成 ===")
    print("查看输出文件：")
    print("  python: {}".format(py_output))
    print("  cmd:    {}".format(cmd_output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
