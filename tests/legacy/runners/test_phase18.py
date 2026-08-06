"""Phase 18 滚动缓冲区一致性 e2e 测试。

验证项：
  1. SCROLLBACK_COUNT：输出多行触发换行滚动后，bufferSize.Y 包含滚动计数
  2. USER_BUFFER_HEIGHT：SetConsoleScreenBufferSize 设置高度后，高度被保留
  3. MODE_SWITCH_RESET：模式切换后，scrollback 被重置

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 Python 测试脚本（phase18_scrollback_test.py）
  - 测试脚本将结果写入结果文件，runner 读取该文件避免 VT 输出 hex 截断
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

from helpers import injector
from helpers import input_sim


# Python 测试脚本路径（相对于 PROJECT_ROOT）
SCROLLBACK_TEST_SCRIPT = os.path.join("tests", "phase18_scrollback_test.py")

# 结果文件路径（与测试脚本约定）
RESULT_FILE = os.path.join(paths.out_dir(), "scrollback_test_result.txt")


def wait_for_result(key: str, timeout: float = 20.0) -> str:
    """等待结果文件中出现指定 key 的结果，返回 value。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(RESULT_FILE):
            time.sleep(0.3)
            continue
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(key + "="):
                        return line[len(key) + 1:]
        except (OSError, IOError):
            pass
        time.sleep(0.3)
    return ""


# ============================================================
# 测试 1：滚动计数跟踪
# ============================================================

def test_scrollback_count() -> bool:
    """测试：输出多行触发换行滚动后，bufferSize.Y 增加。"""
    print("\n[测试 1] 滚动计数跟踪")

    result = wait_for_result("SCROLLBACK_COUNT", timeout=25.0)
    if result == "PASS":
        print("  [PASS] 滚动计数正确，bufferSize.Y 包含滚动行数")
        return True
    elif result:
        print("  [FAIL] 滚动计数验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 SCROLLBACK_COUNT 结果")
        return False


# ============================================================
# 测试 2：用户缓冲区高度保留
# ============================================================

def test_user_buffer_height() -> bool:
    """测试：SetConsoleScreenBufferSize 后缓冲区高度被保留。"""
    print("\n[测试 2] 用户缓冲区高度保留")

    result = wait_for_result("USER_BUFFER_HEIGHT", timeout=15.0)
    if result == "PASS":
        print("  [PASS] 用户设置的缓冲区高度正确保留")
        return True
    elif result:
        print("  [FAIL] 用户缓冲区高度验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 USER_BUFFER_HEIGHT 结果")
        return False


# ============================================================
# 测试 3：模式切换重置滚动计数
# ============================================================

def test_mode_switch_reset() -> bool:
    """测试：模式切换后 scrollback 被重置。"""
    print("\n[测试 3] 模式切换重置滚动计数")

    result = wait_for_result("MODE_SWITCH_RESET", timeout=15.0)
    if result == "PASS":
        print("  [PASS] 模式切换后滚动计数正确重置")
        return True
    elif result:
        print("  [FAIL] 模式切换重置验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 MODE_SWITCH_RESET 结果")
        return False


# ============================================================
# 主入口
# ============================================================

def run() -> int:
    # 清理旧结果文件
    try:
        os.remove(RESULT_FILE)
    except OSError:
        pass

    failures = 0

    # 启动 cmd + WT
    print("[setup] 启动目标 cmd...")
    target_pid = injector.start_target_cmd()
    print("[setup] cmd PID={}".format(target_pid))
    injector.clear_log()
    print("[setup] 启动 WT + mediator...")
    mediator_proc = injector.start_wt_mediator(target_pid)
    print("[setup] 等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[setup] 握手失败")
        injector.cleanup(target_pid, mediator_proc)
        return 1
    print("[setup] 握手成功")
    time.sleep(2.0)
    injector.focus_wt()
    time.sleep(1.0)

    # 运行测试脚本
    cmd = "python {}".format(SCROLLBACK_TEST_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.4)
    input_sim.type_enter()

    # 依次检测各测试结果
    try:
        if not test_scrollback_count():
            failures += 1

        time.sleep(1.0)

        if not test_user_buffer_height():
            failures += 1

        time.sleep(1.0)

        if not test_mode_switch_reset():
            failures += 1

    finally:
        # 清理
        print("[teardown] 清理进程...")
        injector.cleanup(target_pid, mediator_proc)
        time.sleep(1.0)

    print("\n========== 结果 ==========")
    if failures == 0:
        print("全部通过")
    else:
        print("{} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())