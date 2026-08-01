"""Phase 17 字符宽度审计 e2e 测试。

验证项：
  1. ASCII 单宽字符：写 "Hello" 后光标精确推进 5 列
  2. CJK 双宽字符：写 "测试" 后光标精确推进 4 列（每字符 2 列）
  3. Emoji 代理对：写 "😀" 后光标精确推进 2 列
  4. 混合宽度：写 "A中😀B" 后光标精确推进 6 列

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 Python 测试脚本（phase17_width_test.py）
  - 测试脚本将结果写入结果文件，runner 读取该文件避免 VT 输出 hex 截断
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim


# Python 测试脚本路径（相对于 PROJECT_ROOT）
WIDTH_TEST_SCRIPT = os.path.join("tests", "phase17_width_test.py")

# 结果文件路径（与测试脚本约定）
RESULT_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "terminal-injector", "logs", "width_test_result.txt")


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
# 测试 1：ASCII 单宽字符
# ============================================================

def test_ascii_width() -> bool:
    """测试：ASCII 字符写入后光标推进 1 列/字符。"""
    print("\n[测试 1] ASCII 单宽字符验证")

    result = wait_for_result("ASCII_WIDTH", timeout=20.0)
    if result == "PASS":
        print("  [PASS] ASCII 单宽字符宽度正确")
        return True
    elif result:
        print("  [FAIL] ASCII 单宽字符宽度验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 ASCII_WIDTH 结果")
        return False


# ============================================================
# 测试 2：CJK 双宽字符
# ============================================================

def test_cjk_width() -> bool:
    """测试：CJK 字符写入后光标推进 2 列/字符。"""
    print("\n[测试 2] CJK 双宽字符验证")

    result = wait_for_result("CJK_WIDTH", timeout=10.0)
    if result == "PASS":
        print("  [PASS] CJK 双宽字符宽度正确")
        return True
    elif result:
        print("  [FAIL] CJK 双宽字符宽度验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 CJK_WIDTH 结果")
        return False


# ============================================================
# 测试 3：Emoji 代理对
# ============================================================

def test_emoji_width() -> bool:
    """测试：Emoji 代理对写入后光标推进 2 列。"""
    print("\n[测试 3] Emoji 代理对验证")

    result = wait_for_result("EMOJI_WIDTH", timeout=10.0)
    if result == "PASS":
        print("  [PASS] Emoji 代理对宽度正确")
        return True
    elif result:
        print("  [FAIL] Emoji 代理对宽度验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 EMOJI_WIDTH 结果")
        return False


# ============================================================
# 测试 4：混合宽度
# ============================================================

def test_mixed_width() -> bool:
    """测试：混合 ASCII + CJK + Emoji 写入后光标精确推进。"""
    print("\n[测试 4] 混合宽度验证")

    result = wait_for_result("MIXED_WIDTH", timeout=15.0)
    if result == "PASS":
        print("  [PASS] 混合宽度正确")
        return True
    elif result:
        print("  [FAIL] 混合宽度验证失败: {}".format(result))
        return False
    else:
        print("  [FAIL] 未检测到 MIXED_WIDTH 结果")
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
    cmd = "python {}".format(WIDTH_TEST_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.4)
    input_sim.type_enter()

    # 依次检测各测试结果
    try:
        if not test_ascii_width():
            failures += 1

        time.sleep(1.0)

        if not test_cjk_width():
            failures += 1

        if not test_emoji_width():
            failures += 1

        if not test_mixed_width():
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
