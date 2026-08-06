"""Phase 15 DSR/DA 终端属性查询 e2e 测试。

验证项：
  1. DSR CPR 查询发送：DLL 日志出现 "QueryWtCursorPos: DSR CPR query sent to WT"
  2. Primary DA 查询发送：DLL 日志出现 "QueryTerminalCaps: Primary DA query sent to WT"
  3. DA 响应解析：mediator 日志出现 "VtParser: DA response detected"
  4. WtStateReport DA 发送：mediator 日志出现 "WtStateReport DA sent"
  5. DA 报告接收：DLL 日志出现 "VirtualConsoleState::ApplyWtDaReport"

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 读 DLL 日志，验证 DSR CPR 和 DA 查询发送
  - 读 mediator 日志，验证 DA 响应解析和 WtStateReport 发送
  - 读 DLL 日志，验证 DA 报告接收
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

from helpers import injector
from helpers.vt_capture import MediatorLog


class TestContext:
    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None
        self.log = MediatorLog(injector.LOG_PATH)

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        injector.clear_log()
        print("[setup] 启动 WT + mediator...")
        self.mediator_proc = injector.start_wt_mediator(self.target_pid)
        print("[setup] 等待握手...")
        if not injector.wait_for_handshake(timeout=20.0):
            print("[setup] 握手失败")
            return False
        print("[setup] 握手成功")
        # 等待 LazyInit 完成（DSR CPR + DA 查询在初始化时发送）
        time.sleep(3.0)
        self.log.mark()
        return True

    def teardown(self) -> None:
        print("[teardown] 清理进程...")
        injector.cleanup(self.target_pid, self.mediator_proc)
        time.sleep(1.0)

    def get_dll_log_path(self) -> str:
        """返回 DLL 日志路径（该 pid 最新一份 injected_<pid>_*.log）。"""
        return paths.injected_log(self.target_pid)

    def read_all(self) -> str:
        return self.log.read_all()


# ============================================================
# 测试 1：DSR CPR 查询发送验证
# ============================================================

def test_dsr_cpr_query_sent(ctx: TestContext) -> bool:
    """测试：LazyInit 时发送 DSR CPR 查询（\x1b[6n）到 WT。

    验证方式：检查 DLL 日志中是否出现
    "QueryWtCursorPos: DSR CPR query sent to WT" 字符串。
    """
    print("\n[测试 1] DSR CPR 查询发送验证")

    dll_log_path = ctx.get_dll_log_path()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if os.path.exists(dll_log_path):
            try:
                with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if "DSR CPR query sent to WT" in content:
                        print("  [PASS] DSR CPR 查询已发送")
                        return True
            except OSError:
                pass
        time.sleep(0.5)

    print("  [FAIL] 未检测到 DSR CPR 查询发送日志")
    if os.path.exists(dll_log_path):
        with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print("  [DLL LOG] {}".format(line.strip()))
    return False


# ============================================================
# 测试 2：Primary DA 查询发送验证
# ============================================================

def test_da_query_sent(ctx: TestContext) -> bool:
    """测试：LazyInit 时发送 Primary DA 查询（\x1b[c）到 WT。

    验证方式：检查 DLL 日志中是否出现
    "QueryTerminalCaps: Primary DA query sent to WT" 字符串。
    """
    print("\n[测试 2] Primary DA 查询发送验证")

    dll_log_path = ctx.get_dll_log_path()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if os.path.exists(dll_log_path):
            try:
                with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if "Primary DA query sent to WT" in content:
                        print("  [PASS] Primary DA 查询已发送")
                        return True
            except OSError:
                pass
        time.sleep(0.5)

    print("  [FAIL] 未检测到 Primary DA 查询发送日志")
    if os.path.exists(dll_log_path):
        with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print("  [DLL LOG] {}".format(line.strip()))
    return False


# ============================================================
# 测试 3：DA 响应解析验证（mediator 侧）
# ============================================================

def test_da_response_parsed(ctx: TestContext) -> bool:
    """测试：mediator 的 VtParser 成功解析 WT 的 DA 响应。

    验证方式：检查 mediator 日志中是否出现
    "VtParser: DA response detected" 字符串。
    """
    print("\n[测试 3] DA 响应解析验证（mediator 侧）")

    deadline = time.time() + 15.0
    while time.time() < deadline:
        content = ctx.log.read_new()
        m = re.search(r"VtParser: DA response detected, caps=(\d+)", content)
        if m:
            caps = int(m.group(1))
            print("  [PASS] DA 响应解析成功，caps={}".format(caps))
            return True
        time.sleep(0.3)

    # 未找到，尝试读全部日志
    content = ctx.read_all()
    m = re.search(r"VtParser: DA response detected, caps=(\d+)", content)
    if m:
        caps = int(m.group(1))
        print("  [PASS] DA 响应解析成功（在历史日志中），caps={}".format(caps))
        return True

    print("  [FAIL] 未检测到 DA 响应解析日志")
    for line in content.split("\n")[-30:]:
        if "VtParser" in line or "DA" in line:
            print("  [LOG] {}".format(line))
    return False


# ============================================================
# 测试 4：WtStateReport DA 发送验证
# ============================================================

def test_da_report_sent(ctx: TestContext) -> bool:
    """测试：mediator 发送 WtStateReport(type=2) 给 DLL。

    验证方式：检查 mediator 日志中是否出现
    "WtStateReport DA sent" 字符串。
    """
    print("\n[测试 4] WtStateReport DA 发送验证")

    deadline = time.time() + 10.0
    while time.time() < deadline:
        content = ctx.log.read_new()
        if "WtStateReport DA sent" in content:
            print("  [PASS] WtStateReport DA 已发送")
            return True
        time.sleep(0.3)

    content = ctx.read_all()
    if "WtStateReport DA sent" in content:
        print("  [PASS] WtStateReport DA 已发送（在历史日志中）")
        return True

    print("  [FAIL] 未检测到 WtStateReport DA 发送日志")
    for line in content.split("\n")[-20:]:
        print("  [LOG] {}".format(line))
    return False


# ============================================================
# 测试 5：DA 报告 DLL 接收验证
# ============================================================

def test_da_report_received(ctx: TestContext) -> bool:
    """测试：DLL 收到 WtStateReport(type=2) 并更新 VirtualConsoleState。

    验证方式：检查 DLL 日志中是否出现
    "VirtualConsoleState::ApplyWtDaReport" 字符串。
    """
    print("\n[测试 5] DA 报告 DLL 接收验证")

    dll_log_path = ctx.get_dll_log_path()
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if os.path.exists(dll_log_path):
            try:
                with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    m = re.search(r"VirtualConsoleState::ApplyWtDaReport: terminal caps=(\d+)", content)
                    if m:
                        caps = int(m.group(1))
                        print("  [PASS] DA 报告已接收，terminal caps={}".format(caps))
                        return True
            except OSError:
                pass
        time.sleep(0.5)

    print("  [FAIL] 未检测到 DA 报告接收日志")
    if os.path.exists(dll_log_path):
        with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines[-20:]:
                print("  [DLL LOG] {}".format(line.strip()))
    return False


# ============================================================
# 主入口
# ============================================================

def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        # 测试 1：DSR CPR 查询发送
        if not test_dsr_cpr_query_sent(ctx):
            failures += 1

        # 测试 2：Primary DA 查询发送
        if not test_da_query_sent(ctx):
            failures += 1

        # 等待 DA 响应通过 WT 回传（需要 VT 往返）
        time.sleep(1.0)

        # 测试 3：DA 响应解析（mediator 侧）
        if not test_da_response_parsed(ctx):
            failures += 1

        # 测试 4：WtStateReport DA 发送
        if not test_da_report_sent(ctx):
            failures += 1

        # 测试 5：DA 报告 DLL 接收
        if not test_da_report_received(ctx):
            failures += 1

    finally:
        ctx.teardown()

    print("\n========== 结果 ==========")
    if failures == 0:
        print("全部通过")
    else:
        print("{} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())