"""Phase 10 任务1 验证脚本：注入后检查 StatePoller 线程日志。

验证项：
  1. injected_<pid>.log 出现 "StatePoller started"（Start 成功）
  2. 约 3 秒后出现 "StatePoller loop done"（PollLoop 正常退出）
  3. 出现 "StatePoller stopped" 或线程自然结束（无卡死）

流程：
  1. 启动 cmd（注入目标）
  2. 启动 WT mediator
  3. 等待握手
  4. 等 5 秒（StatePoller 跑 3 秒 + 余量）
  5. 读 injected 日志验证 StatePoller 关键字
  6. cleanup
"""
import os
import sys
import time

# 让 tests/helpers 可导入
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "helpers")))
import injector  # noqa: E402

# injected DLL 日志路径（与 LazyInit.cpp 一致：C:\temp\injected_<pid>.log）
INJECTED_LOG_DIR = r"C:\temp"


def get_injected_log_path(pid: int) -> str:
    return os.path.join(INJECTED_LOG_DIR, f"injected_{pid}.log")


def main() -> int:
    print("=== Phase 10 任务1 验证：StatePoller ===")

    # 1. 启动注入目标 cmd
    target_pid = injector.start_target_cmd()
    print(f"[+] target cmd started, pid={target_pid}")

    # 2. 清空 mediator 日志
    injector.clear_log()

    # 3. 启动 WT mediator
    mediator_proc = injector.start_wt_mediator(target_pid)
    print(f"[+] WT mediator started, mediator_pid={mediator_proc.pid}")

    # 4. 等待握手
    if not injector.wait_for_handshake(timeout=15.0):
        print("[-] handshake failed")
        injector.cleanup(target_pid, mediator_proc)
        return 1
    print("[+] handshake OK")

    # 5. 等 5 秒让 StatePoller 跑完 3 秒轮询 + 余量
    print("[*] waiting 5s for StatePoller to complete 3s poll loop...")
    time.sleep(5)

    # 6. 读 injected 日志验证
    log_path = get_injected_log_path(target_pid)
    if not os.path.exists(log_path):
        print(f"[-] injected log not found: {log_path}")
        injector.cleanup(target_pid, mediator_proc)
        return 1

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        log_content = f.read()

    # 7. 检查关键字
    checks = [
        ("StatePoller started", "Start 成功"),
        ("StatePoller loop done", "PollLoop 正常退出"),
    ]
    all_pass = True
    for keyword, desc in checks:
        if keyword in log_content:
            # 提取该行用于展示
            line = next((l for l in log_content.splitlines() if keyword in l), "")
            print(f"[PASS] {desc}: {line.strip()}")
        else:
            print(f"[FAIL] {desc}: 关键字 '{keyword}' 未找到")
            all_pass = False

    # 8. 额外检查：是否有 sync 日志（可选，无并发输出时可能为 0 syncs）
    sync_lines = [l for l in log_content.splitlines() if "cursor synced" in l]
    if sync_lines:
        print(f"[*] cursor sync 事件 {len(sync_lines)} 次（LazyInit 期间有并发输出）")
    else:
        print(f"[*] 无 cursor sync 事件（LazyInit 期间无并发输出，属正常）")

    # 9. 检查 loop done 行的 syncs 计数
    done_line = next((l for l in log_content.splitlines() if "loop done" in l), "")
    if done_line:
        print(f"[*] {done_line.strip()}")

    # 10. cleanup
    injector.cleanup(target_pid, mediator_proc)
    print("[+] cleanup done")

    if all_pass:
        print("\n=== RESULT: PASS ===")
        return 0
    else:
        print("\n=== RESULT: FAIL ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
