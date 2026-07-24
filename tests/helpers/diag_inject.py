"""诊断脚本：手动复现注入流程，捕获 injector 的 stdout/stderr/exit code。

流程：
  1. 启动 cmd（CREATE_NEW_CONSOLE），拿 PID
  2. 直接跑 terminal_injector.exe --inject <pid> --dll <path>
  3. 捕获 injector 输出
  4. 检查 cmd 是否加载 injected.dll（看 C:\\temp\\injected_<pid>.log）
  5. 退出前不清理，保留现场供进一步调试
"""
import os
import subprocess
import time

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")
LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")


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

    # 2. 清空旧日志
    for p in [LOG_PATH]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    # 3. 直接跑 injector（不通过 mediator）
    inject_cmd = [MEDIATOR_EXE, "--inject", str(pid), "--dll", INJECTED_DLL]
    print("[2] 跑 injector: {}".format(" ".join(inject_cmd)))
    try:
        result = subprocess.run(
            inject_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        print("    exit code: {}".format(result.returncode))
        if result.stdout:
            print("    --- stdout ---")
            print(result.stdout)
        if result.stderr:
            print("    --- stderr ---")
            print(result.stderr)
    except subprocess.TimeoutExpired:
        print("    [TIMEOUT] injector 30s 未退出")

    # 4. 检查 injector 日志
    print("[3] injector 日志 (terminal-injector.log):")
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        print(content if content.strip() else "(空文件)")
    else:
        print("    日志文件未生成")

    # 5. 检查 cmd 注入日志
    dll_log = r"C:\temp\injected_{}.log".format(pid)
    print("[4] DLL 日志 {}:".format(dll_log))
    if os.path.exists(dll_log):
        with open(dll_log, "r", encoding="utf-8", errors="ignore") as f:
            print(f.read())
    else:
        print("    DLL 日志未生成（DLL 未加载或 DllMain 未跑到写日志）")

    # 6. 检查 cmd 进程是否还活着
    print("[5] cmd 进程状态:")
    try:
        p = __import__("psutil").Process(pid)
        print("    cmd 还活着, status={}".format(p.status()))
        # 列出 cmd 加载的模块，看有没有 injected.dll
        try:
            dlls = [m.path for m in p.memory_maps() if "injected" in m.path.lower()]
            if dlls:
                print("    cmd 已加载: {}".format(dlls))
            else:
                print("    cmd 未加载 injected.dll")
        except Exception as e:
            print("    memory_maps 查询失败: {}".format(e))
    except __import__("psutil").NoSuchProcess:
        print("    cmd 已退出")

    print("\n[done] 保留现场，cmd PID={} 请手动关闭或用任务管理器".format(pid))
    print("如需进一步调试，可用 cdb -p {} 附加".format(pid))


if __name__ == "__main__":
    main()
