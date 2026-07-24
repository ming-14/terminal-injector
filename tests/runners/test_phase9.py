"""Phase 9 自保护专项测试。

验证项（对应 docs/phases/09-self-protection.md 第 5 节）：
  1. AllocConsole 被拦：返回 FALSE + ERROR_NOT_ENOUGH_MEMORY(8)
  2. AttachConsole 被拦：返回 FALSE + ERROR_ACCESS_DENIED(5)
  3. FreeConsole 静默成功：返回 TRUE，且后续 WriteConsoleW 仍可写（未真断）
  4. GetStdHandle 多次返回一致
  5. CloseHandle 假句柄（魔数 0xABCDE123）静默成功
  6. 原 Console 窗口被 LazyInit 隐藏（IsWindowVisible=0）

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - SendInput 在 cmd 中运行 `python tests\\targets\\test_phase9_target.py`
  - 子进程注入（ProcessHooks）将 DLL 注入到 python
  - python 调用各项 API，DLL ProtectionHooks 拦截，python 拿到 Hook 后的返回值
  - python 把结果写入结果文件（默认 ./phase9_result.txt）
  - runner 读结果文件，按 TEST <name> ret=<0|1> err=<N> 格式解析验证

链路：
  cmd → CreateProcess(python) → DLL ProcessHooks 注入 DLL 到 python
  → python 调 AllocConsole/AttachConsole/FreeConsole/CloseHandle
  → DLL ProtectionHooks 拦截 → python 写结果文件 → runner 读验证
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim

# 项目根目录（与 injector.PROJECT_ROOT 一致）
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
# 目标程序相对 cmd cwd 的路径（cmd 启动时 cwd=PROJECT_ROOT）
TARGET_SCRIPT_REL = os.path.join("tests", "targets", "test_phase9_target.py")
# 结果文件路径（目标程序默认写到 cwd，cmd cwd=PROJECT_ROOT）
RESULT_FILE = os.path.join(PROJECT_ROOT, "phase9_result.txt")

# 错误码常量
ERROR_NOT_ENOUGH_MEMORY = 8
ERROR_ACCESS_DENIED = 5

# 结果行正则：TEST <name> ret=<0|1> err=<N> [key=value ...]
_RESULT_RE = re.compile(
    r"^TEST\s+(\S+)\s+ret=(\d+)\s+err=(\d+)(.*)$"
)


class TestContext:
    """测试上下文：管理 cmd + WT(mediator) + 注入生命周期。"""

    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        # 清理上次残留的结果文件
        if os.path.exists(RESULT_FILE):
            try:
                os.remove(RESULT_FILE)
            except OSError:
                pass
        injector.clear_log()
        print("[setup] 启动 WT + mediator...")
        self.mediator_proc = injector.start_wt_mediator(self.target_pid)
        print("[setup] 等待握手...")
        if not injector.wait_for_handshake(timeout=20.0):
            print("[setup] 握手失败")
            return False
        print("[setup] 握手成功")
        time.sleep(2.0)
        injector.focus_wt()
        time.sleep(1.0)
        return True

    def teardown(self) -> None:
        print("[teardown] 清理进程...")
        injector.cleanup(self.target_pid, self.mediator_proc)
        time.sleep(1.0)


def run_target_program() -> bool:
    """在注入的 cmd 中运行目标 python 程序，等待结果文件就绪。

    返回 True 表示结果文件已就绪可读。
    """
    # 用反斜杠路径（cmd 习惯），避免转义
    cmd_line = "python {}".format(TARGET_SCRIPT_REL.replace("/", "\\"))
    print("  [run] 输入命令: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.5)
    input_sim.type_enter()

    # 等待结果文件出现且写完
    # 目标程序写完后会输出 DONE 标记，异常时输出 EXCEPTION
    deadline = time.time() + 20.0
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "DONE" in content or "EXCEPTION" in content:
                    return True
            except OSError:
                pass
        time.sleep(0.3)
    return os.path.exists(RESULT_FILE)


def parse_results(path: str) -> dict:
    """解析结果文件，返回 {test_name: {ret, err, extra}}。"""
    results = {}
    if not os.path.exists(path):
        return results
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = _RESULT_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            ret = int(m.group(2))
            err = int(m.group(3))
            extra_str = m.group(4).strip()
            extra = {}
            # 解析 key=value 对
            for kv in extra_str.split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    extra[k] = v
            results[name] = {"ret": ret, "err": err, "extra": extra}
    return results


def _expect(name: str, results: dict, expect_ret: int, expect_err: int = None) -> bool:
    """验证某项测试结果，打印 PASS/FAIL。"""
    if name not in results:
        print("  [FAIL] {} 未在结果文件中找到".format(name))
        return False
    r = results[name]
    ok = (r["ret"] == expect_ret)
    if expect_err is not None:
        ok = ok and (r["err"] == expect_err)
    if ok:
        print("  [PASS] {} ret={} err={} {}".format(
            name, r["ret"], r["err"], r.get("extra", "")))
        return True
    print("  [FAIL] {} 期望 ret={} err={}，实际 ret={} err={} {}".format(
        name, expect_ret, expect_err if expect_err is not None else "*",
        r["ret"], r["err"], r.get("extra", "")))
    return False


def test_alloc_console_blocked(results: dict) -> bool:
    """AllocConsole 应被拦：ret=0, err=ERROR_NOT_ENOUGH_MEMORY(8)。"""
    print("\n[测试] AllocConsole 被拦截")
    return _expect("alloc_console", results, 0, ERROR_NOT_ENOUGH_MEMORY)


def test_attach_console_blocked(results: dict) -> bool:
    """AttachConsole 应被拦：ret=0, err=ERROR_ACCESS_DENIED(5)。"""
    print("\n[测试] AttachConsole 被拦截")
    return _expect("attach_console", results, 0, ERROR_ACCESS_DENIED)


def test_free_console_silent(results: dict) -> bool:
    """FreeConsole 应静默成功：ret=1, err=0。"""
    print("\n[测试] FreeConsole 静默成功（不真断）")
    return _expect("free_console", results, 1, 0)


def test_write_after_free(results: dict) -> bool:
    """FreeConsole 后 WriteConsoleW 应仍可写：ret=1, written=len(msg)=14。"""
    print("\n[测试] FreeConsole 后 WriteConsoleW 仍可写（未真断）")
    if "write_after_free" not in results:
        print("  [FAIL] write_after_free 未在结果文件中找到")
        return False
    r = results["write_after_free"]
    written = int(r.get("extra", {}).get("written", "0"))
    if r["ret"] == 1 and written == len("after_free_ok"):
        print("  [PASS] write_after_free ret={} written={}".format(r["ret"], written))
        return True
    print("  [FAIL] write_after_free ret={} written={}（期望 ret=1 written={}）".format(
        r["ret"], written, len("after_free_ok")))
    return False


def test_std_handle_consistent(results: dict) -> bool:
    """多次 GetStdHandle(STD_OUTPUT_HANDLE) 应返回一致：ret=1。"""
    print("\n[测试] GetStdHandle 多次返回一致")
    return _expect("std_handle_consistent", results, 1, 0)


def test_close_fake_handle_silent(results: dict) -> bool:
    """CloseHandle 假句柄（魔数 0xABCDE123）应静默成功：ret=1, err=0。"""
    print("\n[测试] CloseHandle 假句柄静默成功")
    return _expect("close_fake_handle", results, 1, 0)


def test_console_window_hidden(results: dict) -> bool:
    """原 Console 窗口应被 LazyInit 隐藏：ret=1（visible=0 表示已隐藏）。"""
    print("\n[测试] 原 Console 窗口已隐藏")
    if "console_window_hidden" not in results:
        print("  [FAIL] console_window_hidden 未在结果文件中找到")
        return False
    r = results["console_window_hidden"]
    visible = int(r.get("extra", {}).get("visible", "1"))
    if r["ret"] == 1 and visible == 0:
        print("  [PASS] console_window_hidden visible=0（已隐藏）")
        return True
    print("  [FAIL] console_window_hidden ret={} visible={}（期望 visible=0）".format(
        r["ret"], visible))
    return False


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not run_target_program():
            print("[FATAL] 目标程序结果文件未就绪")
            failures += 1
        else:
            print("  [run] 结果文件就绪：{}".format(RESULT_FILE))
            results = parse_results(RESULT_FILE)
            print("  [run] 解析到 {} 项结果".format(len(results)))

            if not test_alloc_console_blocked(results):
                failures += 1
            if not test_attach_console_blocked(results):
                failures += 1
            if not test_free_console_silent(results):
                failures += 1
            if not test_write_after_free(results):
                failures += 1
            if not test_std_handle_consistent(results):
                failures += 1
            if not test_close_fake_handle_silent(results):
                failures += 1
            if not test_console_window_hidden(results):
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
