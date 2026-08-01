"""Phase 10 HookWhitelist 重入防护专项测试。

验证项（对应 docs/phases/10-state-sync-stability.md 4.4 死锁防护白名单）：
  1. WriteConsoleA 高频压测：A→W 复用路径（深度=2）不死锁
  2. WriteFile(CONOUT$) 压测：WriteFile_Detour→WriteConsoleW_Detour 复用路径不死锁
  3. FillConsoleOutputCharacterA：A→W 复用路径不死锁
  4. 混合 A/W 交替调用：HookReentryGuard 深度计数正确归零
  5. Logger worker 重入压测：WriteFile(日志文件) pass-through 不死锁
  6. CreateFileW("CONOUT$") + WriteFile：非 GetStdHandle 路径也被 Hook 拦截

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - SendInput 在 cmd 中运行 `python tests\\targets\\test_phase10_target.py`
  - 子进程注入（ProcessHooks）将 DLL 注入到 python
  - python 调用各项 API，DLL Hook 拦截，HookReentryGuard 计数
  - python 把结果写入结果文件（默认 ./phase10_result.txt）
  - runner 读结果文件 + mediator 日志，验证：
      * 程序未死锁（DONE 标记在超时内出现）
      * 各项 ret=1, ok=total
      * mediator 收到对应 UTF-8 字节序列

链路：
  cmd → CreateProcess(python) → DLL ProcessHooks 注入 DLL 到 python
  → python 调 WriteConsoleA/WriteFile/FillConsoleOutputCharacterA/...
  → DLL 各 Hook 拦截，HookReentryGuard 深度计数
  → python 写结果文件 → runner 读验证
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog

# 项目根目录（与 injector.PROJECT_ROOT 一致）
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
# 目标程序相对 cmd cwd 的路径（cmd 启动时 cwd=PROJECT_ROOT）
TARGET_SCRIPT_REL = os.path.join("tests", "targets", "test_phase10_target.py")
# 结果文件路径（目标程序默认写到 cwd，cmd cwd=PROJECT_ROOT）
RESULT_FILE = os.path.join(PROJECT_ROOT, "phase10_result.txt")

# 结果行正则：TEST <name> ret=<0|1> err=<N> [key=value ...]
_RESULT_RE = re.compile(
    r"^TEST\s+(\S+)\s+ret=(\d+)\s+err=(\d+)(.*)$"
)

# 目标程序完成的最长等待时间（秒）
# 6 项测试包含 50+50+1+100+200+1 次调用，正常应 < 10 秒
# 死锁时会卡住，给 30 秒余量
TARGET_TIMEOUT = 30.0


class TestContext:
    """测试上下文：管理 cmd + WT(mediator) + 注入生命周期。"""

    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None
        self.log = MediatorLog(injector.LOG_PATH)

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
        self.log.mark()
        return True

    def teardown(self) -> None:
        print("[teardown] 清理进程...")
        injector.cleanup(self.target_pid, self.mediator_proc)
        time.sleep(1.0)


def run_target_program() -> bool:
    """在注入的 cmd 中运行目标 python 程序，等待结果文件就绪。

    返回 True 表示结果文件已就绪可读（出现 DONE 或 EXCEPTION 标记）。
    超时则视为死锁，返回 False。
    """
    cmd_line = "python {}".format(TARGET_SCRIPT_REL.replace("/", "\\"))
    print("  [run] 输入命令: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.5)
    input_sim.type_enter()

    # 等待结果文件出现且写完
    # 目标程序写完后会输出 DONE 标记，异常时输出 EXCEPTION
    deadline = time.time() + TARGET_TIMEOUT
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
            for kv in extra_str.split():
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    extra[k] = v
            results[name] = {"ret": ret, "err": err, "extra": extra}
    return results


def _expect_full(name: str, results: dict) -> bool:
    """验证某项测试结果，要求 ret=1 且 ok=total（针对批量调用）。"""
    if name not in results:
        print("  [FAIL] {} 未在结果文件中找到".format(name))
        return False
    r = results[name]
    ok = int(r.get("extra", {}).get("ok", "0"))
    total = int(r.get("extra", {}).get("total", "0"))
    elapsed = r.get("extra", {}).get("elapsed_ms", "?")
    if r["ret"] == 1 and ok == total and total > 0:
        print("  [PASS] {} ok={}/total={} elapsed_ms={}".format(
            name, ok, total, elapsed))
        return True
    print("  [FAIL] {} ret={} ok={}/total={} err={} elapsed_ms={}".format(
        name, r["ret"], ok, total, r["err"], elapsed))
    return False


def _expect_simple(name: str, results: dict, expect_ret: int = 1) -> bool:
    """验证某项测试结果只看 ret。"""
    if name not in results:
        print("  [FAIL] {} 未在结果文件中找到".format(name))
        return False
    r = results[name]
    if r["ret"] == expect_ret:
        print("  [PASS] {} ret={} err={} {}".format(
            name, r["ret"], r["err"], r.get("extra", "")))
        return True
    print("  [FAIL] {} 期望 ret={}，实际 ret={} err={} {}".format(
        name, expect_ret, r["ret"], r["err"], r.get("extra", "")))
    return False


def test_write_console_a_batch(results: dict) -> bool:
    """测试 1：WriteConsoleA 高频压测（A→W 复用路径，深度=2）。"""
    print("\n[测试 1] WriteConsoleA 高频压测（A→W 复用路径）")
    return _expect_full("write_console_a_batch", results)


def test_write_file_console_batch(results: dict) -> bool:
    """测试 2：WriteFile(Console 句柄) 压测（WriteFile_Detour→WriteConsoleW_Detour）。"""
    print("\n[测试 2] WriteFile(Console 句柄) 压测（WriteFile→WriteConsoleW 复用）")
    return _expect_full("write_file_console_batch", results)


def test_fill_output_a(results: dict) -> bool:
    """测试 3：FillConsoleOutputCharacterA（A→W 复用路径）。"""
    print("\n[测试 3] FillConsoleOutputCharacterA（A→W 复用路径）")
    return _expect_simple("fill_output_a", results)


def test_mixed_a_w_batch(results: dict) -> bool:
    """测试 4：混合 A/W 交替调用（验证 HookReentryGuard 深度归零）。"""
    print("\n[测试 4] 混合 A/W 交替调用（验证深度归零）")
    return _expect_full("mixed_a_w_batch", results)


def test_logger_worker_stress(results: dict) -> bool:
    """测试 5：Logger worker 重入压测（WriteFile 日志文件 pass-through）。"""
    print("\n[测试 5] Logger worker 重入压测（大量输出触发 Logger 写日志）")
    return _expect_full("logger_worker_stress", results)


def test_create_conout(results: dict) -> bool:
    """测试 6a：CreateFileW("CONOUT$") 成功打开 Console 句柄。"""
    print("\n[测试 6a] CreateFileW(CONOUT$) 打开 Console 句柄")
    return _expect_simple("create_conout", results)


def test_write_file_conout_handle(results: dict) -> bool:
    """测试 6b：用 CONOUT$ 句柄 WriteFile，验证 Hook 拦截。"""
    print("\n[测试 6b] WriteFile(CONOUT$ 句柄) Hook 拦截")
    return _expect_simple("write_file_conout_handle", results)


def test_mediator_received_child_output(ctx: TestContext) -> bool:
    """验证 mediator 收到子进程（python）的 VT 输出（ChildVtOutput 行）。

    子进程的输出经过 DLL Hook 转 VT 后发给 mediator，mediator 用
    WriteChildVtOutput 转发到 WT stdout，日志记录为 ChildVtOutput 行
    （无 hex 字段，只有 len）。

    Phase 10 任务5 起 VtOutput 走 BatchSender 攒批合并路径：
    多个小包被合并为少数大包发送，因此 ChildVtOutput 行数大幅减少，
    但每条 len 较大。本测试改为按总字节数验证：合并前后总字节不变，
    阈值取 800（远低于预期 ~1500 字节，留足余量）。

    注意：子进程输出走 ChildVtOutput 路径（无 hex 字段），父进程输出
    才走 pipe→stdout: VtOutput（有 hex 字段）。本测试目标是子进程 python，
    故匹配 ChildVtOutput。
    """
    print("\n[测试 7] mediator 收到子进程 VT 输出（ChildVtOutput 总字节）")
    content = ctx.log.read_new()
    # 解析所有 ChildVtOutput 行的 len 字段并累加
    # 行格式：ChildVtOutput: len=%zu written=%lu ok=%d err=%lu
    total_bytes = 0
    packet_count = 0
    for m in re.finditer(r"ChildVtOutput:\s+len=(\d+)\s+written=(\d+)\s+ok=1", content):
        total_bytes += int(m.group(1))
        packet_count += 1
    threshold = 800
    print("  [info] ChildVtOutput 包数={} 总字节={}（阈值 {}）".format(
        packet_count, total_bytes, threshold))
    if total_bytes >= threshold:
        print("  [PASS] ChildVtOutput 总字节 {}（>= 阈值 {}，合并生效）".format(
            total_bytes, threshold))
        return True
    print("  [FAIL] ChildVtOutput 总字节 {}（< 阈值 {}）".format(
        total_bytes, threshold))
    return False


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not run_target_program():
            print("[FATAL] 目标程序超时未完成（疑似 Hook 重入死锁）")
            failures += 1
        else:
            # 检查是否 EXCEPTION
            try:
                with open(RESULT_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "EXCEPTION" in content:
                    print("[FATAL] 目标程序异常退出（疑似 Hook 断言触发）")
                    print(content)
                    failures += 1
                    ctx.teardown()
                    return failures
            except OSError:
                pass

            print("  [run] 结果文件就绪：{}".format(RESULT_FILE))
            results = parse_results(RESULT_FILE)
            print("  [run] 解析到 {} 项结果".format(len(results)))

            if not test_write_console_a_batch(results):
                failures += 1
            if not test_write_file_console_batch(results):
                failures += 1
            if not test_fill_output_a(results):
                failures += 1
            if not test_mixed_a_w_batch(results):
                failures += 1
            if not test_logger_worker_stress(results):
                failures += 1
            if not test_create_conout(results):
                failures += 1
            if not test_write_file_conout_handle(results):
                failures += 1
            if not test_mediator_received_child_output(ctx):
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
