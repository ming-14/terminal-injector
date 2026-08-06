"""Phase 10 任务6 WriteConsoleOutput diff 算法专项测试 runner。

验证项：
  1. 5 次 WriteConsoleOutputW 调用全部成功（ret=1）
  2. diff 决策正确：
     - 第1次 canDiff=0（全量，缓存初始化）
     - 第2次 canDiff=1, outBytes 远小于第1次（diff，仅 1 cell 变化）
     - 第3次 canDiff=1, outBytes 远小于第1次（diff，仅 1 cell 变化）
     - 第4次 canDiff=1（diff，25 cell 变化，但仍走 diff 路径）
     - 第5次 canDiff=0（FillConsoleOutputCharacterW 失效缓存后全量）

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - SendInput 在 cmd 中运行目标程序
  - 目标程序把 python pid 写到结果文件
  - runner 读结果文件拿 pid，读该 pid 的 injected 日志
  - 提取 WriteConsoleOutput LOG_INFO 行，验证 canDiff 和 outBytes

链路：
  cmd → CreateProcess(python) → DLL 注入到 python
  → python 调 WriteConsoleOutputW → DLL Hook 拦截 → diff 算法
  → DLL 写 LOG_INFO 到 injected 日志
  → runner 读 injected 日志验证
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

from helpers import injector
from helpers import input_sim

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
TARGET_SCRIPT_REL = os.path.join("tests", "targets", "test_phase10_diff_target.py")
RESULT_FILE = os.path.join(PROJECT_ROOT, "phase10_diff_result.txt")

TARGET_TIMEOUT = 30.0

# 结果行正则：TEST <name> ret=<0|1> err=<N>
_RESULT_RE = re.compile(r"^TEST\s+(\S+)\s+ret=(\d+)\s+err=(\d+)")
# PID 行正则：PID <N>
_PID_RE = re.compile(r"^PID\s+(\d+)")
# WriteConsoleOutput 日志正则：
# WriteConsoleOutput: canDiff=N region=(L,T,R,B) cells=N outBytes=N
_WCO_LOG_RE = re.compile(
    r"WriteConsoleOutput:\s+canDiff=(\d+)\s+region=\((\-?\d+),(\-?\d+),(\-?\d+),(\-?\d+)\)\s+cells=(\d+)\s+outBytes=(\d+)"
)


class TestContext:
    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
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
    cmd_line = "python {}".format(TARGET_SCRIPT_REL.replace("/", "\\"))
    print("  [run] 输入命令: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.5)
    input_sim.type_enter()

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


def parse_results(path: str):
    """解析结果文件，返回 (pid, {test_name: {ret, err}})。"""
    pid = 0
    results = {}
    if not os.path.exists(path):
        return pid, results
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = _PID_RE.match(line)
            if m:
                pid = int(m.group(1))
                continue
            m = _RESULT_RE.match(line)
            if m:
                name = m.group(1)
                ret = int(m.group(2))
                err = int(m.group(3))
                results[name] = {"ret": ret, "err": err}
    return pid, results


def read_injected_log(pid: int) -> str:
    """读取该 pid 最新一份 injected_<pid>_*.log 全部内容。"""
    if pid == 0:
        return ""
    path = paths.injected_log(pid)
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def extract_wco_logs(log: str):
    """从 injected 日志提取所有 WriteConsoleOutput 日志行。

    返回 list[dict]，每个 dict 含 canDiff/region/cells/outBytes。
    """
    result = []
    for m in _WCO_LOG_RE.finditer(log):
        result.append({
            "canDiff": int(m.group(1)),
            "region": (int(m.group(2)), int(m.group(3)),
                       int(m.group(4)), int(m.group(5))),
            "cells": int(m.group(6)),
            "outBytes": int(m.group(7)),
        })
    return result


def test_all_calls_succeeded(results: dict) -> bool:
    """验证 5 次 WriteConsoleOutputW 调用全部 ret=1。"""
    print("\n[测试 1] 5 次 WriteConsoleOutputW 调用全部成功")
    expected = [
        "diff_full_init",
        "diff_single_change",
        "diff_revert",
        "diff_all_change",
        "diff_after_invalidate",
    ]
    all_ok = True
    for name in expected:
        if name not in results:
            print("  [FAIL] {} 未在结果文件中找到".format(name))
            all_ok = False
            continue
        r = results[name]
        if r["ret"] == 1:
            print("  [PASS] {} ret=1".format(name))
        else:
            print("  [FAIL] {} ret={} err={}".format(name, r["ret"], r["err"]))
            all_ok = False
    return all_ok


def test_diff_decision(wco_logs: list) -> bool:
    """验证 diff 决策正确：canDiff 标志与预期一致。"""
    print("\n[测试 2] diff 决策正确性（canDiff 标志）")
    if len(wco_logs) < 5:
        print("  [FAIL] WriteConsoleOutput 日志仅 {} 行（预期 5 行）".format(len(wco_logs)))
        return False

    # 取最后 5 行（目标程序的 5 次调用）
    # 之前可能还有其他 WriteConsoleOutput 调用（如 LazyInit 补发屏幕内容）
    calls = wco_logs[-5:]
    expected_can_diff = [0, 1, 1, 1, 0]
    expected_names = [
        "全量初始化",
        "diff 1 cell 变化",
        "diff 1 cell 恢复",
        "diff 25 cell 变化",
        "失效缓存后全量",
    ]
    all_ok = True
    for i, (call, exp, name) in enumerate(zip(calls, expected_can_diff, expected_names)):
        actual = call["canDiff"]
        if actual == exp:
            print("  [PASS] 调用{} {} canDiff={}".format(i + 1, name, actual))
        else:
            print("  [FAIL] 调用{} {} 期望 canDiff={}，实际 canDiff={}".format(
                i + 1, name, exp, actual))
            all_ok = False
    return all_ok


def test_diff_byte_reduction(wco_logs: list) -> bool:
    """验证 diff 路径输出字节数远小于全量路径。"""
    print("\n[测试 3] diff 输出字节量减少")
    if len(wco_logs) < 5:
        print("  [FAIL] 日志行数不足")
        return False

    calls = wco_logs[-5:]
    full_init_bytes = calls[0]["outBytes"]       # 全量初始化
    single_change_bytes = calls[1]["outBytes"]    # diff 1 cell
    revert_bytes = calls[2]["outBytes"]           # diff 1 cell 恢复
    all_change_bytes = calls[3]["outBytes"]       # diff 25 cell
    after_invalidate_bytes = calls[4]["outBytes"]  # 失效后全量

    print("  [info] 全量初始化 outBytes={}".format(full_init_bytes))
    print("  [info] diff 1cell 变化 outBytes={}".format(single_change_bytes))
    print("  [info] diff 1cell 恢复 outBytes={}".format(revert_bytes))
    print("  [info] diff 25cell 变化 outBytes={}".format(all_change_bytes))
    print("  [info] 失效后全量 outBytes={}".format(after_invalidate_bytes))

    # 全量初始化应有显著输出
    if full_init_bytes < 50:
        print("  [FAIL] 全量初始化 outBytes={}（预期 >= 50）".format(full_init_bytes))
        return False

    # diff 1 cell 变化应远小于全量
    # 全量 25 cell 输出 ~25*15=375 字节，diff 1 cell 输出 ~15 字节
    # 阈值：diff 输出 < 全量的 1/3
    threshold = full_init_bytes // 3
    if single_change_bytes >= threshold:
        print("  [FAIL] diff 1cell 变化 outBytes={}（预期 < {}）".format(
            single_change_bytes, threshold))
        return False
    if revert_bytes >= threshold:
        print("  [FAIL] diff 1cell 恢复 outBytes={}（预期 < {}）".format(
            revert_bytes, threshold))
        return False

    # 失效缓存后应走全量路径，outBytes 与首次全量相近
    if after_invalidate_bytes < threshold:
        print("  [FAIL] 失效后全量 outBytes={}（预期 >= {}）".format(
            after_invalidate_bytes, threshold))
        return False

    print("  [PASS] diff 输出字节量 {} < 全量 {} 的 1/3 = {}".format(
        single_change_bytes, full_init_bytes, threshold))
    return True


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not run_target_program():
            print("[FATAL] 目标程序超时未完成")
            failures += 1
        else:
            try:
                with open(RESULT_FILE, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "EXCEPTION" in content:
                    print("[FATAL] 目标程序异常退出")
                    print(content)
                    failures += 1
                    ctx.teardown()
                    return failures
            except OSError:
                pass

            print("  [run] 结果文件就绪：{}".format(RESULT_FILE))
            pid, results = parse_results(RESULT_FILE)
            print("  [run] python pid={}, 解析到 {} 项结果".format(pid, len(results)))

            if pid == 0:
                print("[FATAL] 未在结果文件中找到 python pid")
                failures += 1
            else:
                # 等 injected 日志写入完成
                time.sleep(0.5)
                log = read_injected_log(pid)
                if not log:
                    print("[FATAL] injected 日志为空：{}".format(paths.injected_log(pid)))
                    failures += 1
                else:
                    wco_logs = extract_wco_logs(log)
                    print("  [run] injected 日志中 WriteConsoleOutput 行数={}".format(
                        len(wco_logs)))

                    if not test_all_calls_succeeded(results):
                        failures += 1
                    if not test_diff_decision(wco_logs):
                        failures += 1
                    if not test_diff_byte_reduction(wco_logs):
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
