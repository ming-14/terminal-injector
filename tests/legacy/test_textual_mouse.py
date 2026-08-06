"""测试 Textual 鼠标和滚动问题 - 简化版。

在 WT 窗口中输入命令启动 textual_demo.py，mediator 转发输入到目标 cmd。
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "helpers"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from injector import (
    start_target_cmd, start_wt_mediator, clear_log, wait_for_handshake,
    focus_wt, cleanup, LOG_PATH, BUILD_BIN
)
import input_sim as sim
import paths  # noqa: E402


def get_log() -> str:
    if not os.path.exists(LOG_PATH):
        return ""
    with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def main():
    # 1. 启动目标 cmd
    print("=" * 50)
    print("[1] 启动目标 cmd")
    target_pid = start_target_cmd()
    print(f"    PID: {target_pid}")

    # 2. 清空日志
    clear_log()

    # 3. 启动 mediator (WT)
    print("[2] 启动 mediator (WT)")
    mediator_proc = start_wt_mediator(target_pid)

    # 4. 等待握手
    print("[3] 等待握手...", end="", flush=True)
    if wait_for_handshake(timeout=15):
        print(" 成功!")
    else:
        print(" 失败!")
        log = get_log()
        print(log[-2000:] if log else "(空)")
        cleanup(target_pid, mediator_proc)
        return

    # 5. 聚焦 WT 窗口，键入命令启动 Textual
    print("[4] 聚焦 WT 窗口...")
    wt_hwnd = focus_wt()
    if wt_hwnd:
        print(f"    WT 窗口句柄: 0x{wt_hwnd:08x}")
    else:
        print("    WARNING: 无法聚焦 WT 窗口!")

    # 在 WT 窗口中输入命令（mediator 转发到目标 cmd）
    print("    在 WT 中运行 textual_demo.py")
    # 先输入 cd 到项目目录
    sim.type_text("cd {}".format(paths.project_root()))
    sim.type_enter()
    time.sleep(1)
    # 启动 textual_demo.py
    sim.type_text("python ..\\textual_demo.py")
    sim.type_enter()
    print("    等待 Textual 启动 (15秒)...")
    time.sleep(15)

    # 6. 读取日志
    log = get_log()
    print(f"[5] 日志大小: {len(log)} 字节")

    # 分析关键信息
    print("\n--- 分析 ---")
    for line in log.split("\n"):
        l = line.strip()
        if not l: continue
        # 模式切换
        if "ModeSwitchNotify" in l or "ModeChange" in l or "output mode" in l:
            print(f"  MODE: {l}")
        # VT 模式
        if "VT_INPUT" in l or "VT_PROCESSING" in l:
            print(f"  VT: {l}")
        # 鼠标
        if "mouse" in l.lower() or "MOUSE" in l or "Mouse" in l:
            print(f"  MOUSE: {l}")
        # 错误
        if "ERROR" in l or "fail" in l.lower() or "exception" in l.lower():
            print(f"  ERR: {l}")
        # WriteFile/WriteConsoleW
        if "WriteFile_Detour" in l or "WriteConsoleW_Detour" in l:
            print(f"  HOOK: {l}")

    # 7. 等待用户检查
    print("\n" + "=" * 50)
    print("Textual 已启动，请检查 WT 窗口。")
    print("观察鼠标点击和滚动行为。")
    print("按 Ctrl+C 退出。")
    print("=" * 50)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n清理...")
        cleanup(target_pid, mediator_proc)
        print("完成。")


if __name__ == "__main__":
    main()