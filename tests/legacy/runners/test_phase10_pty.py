"""Phase 10 屏幕渲染验证套件（WT + mediator 日志路径）。

本套件替代原 PTY-Agent 屏幕快照测试，使用 WT + mediator 路径验证
Phase 10 三个任务（HookWhitelist 重入防护、IPC 小包合并、WriteConsoleOutput diff）
的屏幕渲染结果是否正常。

验证方式（与 test_phase10.py / test_phase10_diff.py 互补）：
  - 前者偏重结果文件 + injected 日志验证（内部状态）
  - 本套件偏重 mediator 日志验证（VT 输出序列/子进程输出到达）
  - 两者侧重点不同，互不替代

覆盖 Phase 10 三个任务：
  - 任务4 HookWhitelist 重入防护：phase10_target 调大量 WriteConsoleA/W/WriteFile
    不死锁，输出到达 WT
  - 任务5 IPC 小包合并：phase10_target 高频小包合并后输出完整不丢失
  - 任务6 WriteConsoleOutput diff：phase10_diff_target 5 次 WriteConsoleOutputW
    后 diff 算法正确（canDiff 标志 + outBytes 减少）

测试 1：注入后 StateSnapshot 补发验证
  - mediator 日志中出现 StateSnapshot 补发 VT 序列（ChildVtOutput）
  - 证明 DLL 注入后初始屏幕内容被正确发送到 WT

测试 2：HookWhitelist 重入防护 + IPC 小包合并
  - 在 cmd 中运行 test_phase10_target.py
  - 验证结果文件 7 项 ret=1
  - 验证 mediator 日志中 ChildVtOutput 总字节 >= 阈值

测试 3：WriteConsoleOutput diff 算法
  - 在 cmd 中运行 test_phase10_diff_target.py
  - 验证结果文件 5 项 ret=1
  - 验证 injected 日志中 canDiff 标志和 outBytes 正确
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog

# 项目路径
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PHASE10_RESULT = os.path.join(PROJECT_ROOT, "phase10_result.txt")
PHASE10_DIFF_RESULT = os.path.join(PROJECT_ROOT, "phase10_diff_result.txt")
INJECTED_LOG_DIR = r"C:\temp"

# 目标程序相对 cmd cwd 的路径（cmd 启动时 cwd=PROJECT_ROOT）
TARGET_PHASE10 = os.path.join("tests", "targets", "test_phase10_target.py")
TARGET_PHASE10_DIFF = os.path.join("tests", "targets", "test_phase10_diff_target.py")

# 目标程序完成的最长等待时间（秒）
TARGET_TIMEOUT = 30.0

# 结果行正则
_RESULT_RE = re.compile(r"^TEST\s+(\S+)\s+ret=(\d+)\s+err=(\d+)(.*)$")
_PID_RE = re.compile(r"^PID\s+(\d+)")
# WriteConsoleOutput 日志正则
_WCO_LOG_RE = re.compile(
    r"WriteConsoleOutput:\s+canDiff=(\d+)\s+region=\((\-?\d+),(\-?\d+),(\-?\d+),(\-?\d+)\)\s+cells=(\d+)\s+outBytes=(\d+)"
)


def _parse_result_file(path: str) -> list:
    """解析结果文件，返回 TEST 行列表。"""
    if not os.path.exists(path):
        return []
    results = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("TEST "):
                results.append(line)
    return results


def _parse_results(path: str) -> dict:
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


def _extract_pid(path: str) -> int:
    """从结果文件中提取 python PID。"""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = _PID_RE.match(line)
            if m:
                return int(m.group(1))
    return 0


def _read_injected_log(pid: int) -> str:
    """读取 C:\\temp\\injected_<pid>.log 全部内容。"""
    if pid == 0:
        return ""
    path = os.path.join(INJECTED_LOG_DIR, "injected_{}.log".format(pid))
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _extract_wco_logs(log: str) -> list:
    """从 injected 日志提取所有 WriteConsoleOutput 日志行。"""
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


def _run_target_via_sendinput(script_rel: str, result_path: str) -> bool:
    """通过 SendInput 在 cmd 中运行目标 python 脚本，等待结果文件就绪。

    返回 True 表示结果文件已就绪（出现 DONE 或 EXCEPTION 标记）。
    超时则返回 False。
    """
    cmd_line = "python {}".format(script_rel.replace("/", "\\"))
    print("  [run] 输入命令: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.5)
    input_sim.type_enter()

    deadline = time.time() + TARGET_TIMEOUT
    while time.time() < deadline:
        if os.path.exists(result_path):
            try:
                with open(result_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                if "DONE" in content or "EXCEPTION" in content:
                    return True
            except OSError:
                pass
        time.sleep(0.3)
    return os.path.exists(result_path)


def _setup(label: str):
    """通用 setup：启动 cmd + 清日志 + 启动 WT mediator + 等握手。

    返回 (target_pid, mediator_proc, log) 或 (None, None, None) 表示失败。
    """
    print("\n[setup:{}] 启动目标 cmd...".format(label))
    target_pid = injector.start_target_cmd()
    print("[setup:{}] cmd PID={}".format(label, target_pid))

    injector.clear_log()

    print("[setup:{}] 启动 WT + mediator...".format(label))
    mediator_proc = injector.start_wt_mediator(target_pid)

    print("[setup:{}] 等待握手...".format(label))
    if not injector.wait_for_handshake(timeout=20.0):
        print("[setup:{}] 握手失败".format(label))
        injector.cleanup(target_pid, mediator_proc)
        return None, None, None

    print("[setup:{}] 握手成功".format(label))
    # 等待初始屏幕稳定（StateSnapshot 补发）
    time.sleep(2.0)
    injector.focus_wt()
    time.sleep(1.0)

    log = MediatorLog(injector.LOG_PATH)
    log.mark()
    return target_pid, mediator_proc, log


def test_phase10_state_snapshot() -> bool:
    """测试 1：注入后 StateSnapshot 补发验证。

    验证点：
      1. 握手后 mediator 日志中出现 pipe→stdout: VtOutput（DLL 补发初始屏幕 VT 序列）
      2. VtOutput 总字节数 >= 阈值（证明初始屏幕内容被正确发送到 WT）

    注意：目标 cmd 的初始屏幕输出走 pipe→stdout: VtOutput 路径（非 ChildVtOutput，
    ChildVtOutput 仅用于子进程如 python）。替代原 PTY-Agent 路径的初始屏幕快照验证。
    """
    print("\n" + "=" * 60)
    print("测试 1：注入后 StateSnapshot 补发验证")
    print("=" * 60)

    target_pid, mediator_proc, log = _setup("StateSnapshot")
    if target_pid is None:
        return False

    ok = False
    try:
        # 读取全部日志内容（StateSnapshot 补发在握手期间完成，已被 mark 跳过）
        # 使用 read_all 而非 read_new，因为 mark 在握手后才设置
        content = log.read_all()

        # 验证 1：日志中包含 pipe→stdout: VtOutput（目标进程 VT 输出）
        # 目标 cmd 的初始屏幕（横幅 + prompt）通过 DLL Hook 输出 VT 序列到 mediator，
        # mediator 通过 pipe→stdout: VtOutput 写到 WT（非 ChildVtOutput 路径）
        vt_count = len(re.findall(r"pipe.*stdout: VtOutput", content))
        print("  [info] pipe→stdout: VtOutput 出现 {} 次".format(vt_count))

        if vt_count == 0:
            print("  [FAIL] 日志中未出现 VtOutput（DLL 未发送初始屏幕 VT 序列）")
            return False
        print("  [PASS] 日志中出现 VtOutput（DLL 已发送初始屏幕 VT 序列）")

        # 验证 2：VtOutput 总字节数 >= 阈值
        # 行格式：pipe→stdout: VtOutput len=N written=...
        total_bytes = 0
        for m in re.finditer(r"pipe.*stdout: VtOutput len=(\d+)", content):
            total_bytes += int(m.group(1))

        threshold = 200  # 初始屏幕（cmd 横幅 + prompt）应至少 200 字节
        print("  [info] VtOutput 总字节={}（阈值 {}）".format(total_bytes, threshold))

        if total_bytes >= threshold:
            print("  [PASS] VtOutput 总字节 {} >= 阈值 {}".format(total_bytes, threshold))
            ok = True
        else:
            print("  [FAIL] VtOutput 总字节 {} < 阈值 {}".format(total_bytes, threshold))
    finally:
        injector.cleanup(target_pid, mediator_proc)
    return ok


def test_phase10_hookwhitelist_and_batch() -> bool:
    """测试 2：HookWhitelist 重入防护 + IPC 小包合并。

    验证点：
      1. 结果文件 7 项 ret=1（与 test_phase10.py 一致）
      2. Mediator 日志中 ChildVtOutput 总字节 >= 阈值（子进程输出到达 WT）

    替代原 PTY-Agent 路径的屏幕渲染验证（phase10_target_done 等标记）。
    """
    print("\n" + "=" * 60)
    print("测试 2：Phase 10 任务4+5 HookWhitelist + IPC 小包合并")
    print("=" * 60)

    target_pid, mediator_proc, log = _setup("HookWhitelist+Batch")
    if target_pid is None:
        return False

    ok = False
    try:
        # 清理旧结果文件
        if os.path.exists(PHASE10_RESULT):
            try:
                os.remove(PHASE10_RESULT)
            except OSError:
                pass

        # 运行目标程序
        if not _run_target_via_sendinput(TARGET_PHASE10, PHASE10_RESULT):
            print("  [FAIL] 目标程序超时未完成（疑似 Hook 重入死锁）")
            return False

        # 检查是否 EXCEPTION
        try:
            with open(PHASE10_RESULT, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "EXCEPTION" in content:
                print("  [FAIL] 目标程序异常退出（疑似 Hook 断言触发）")
                print(content)
                return False
        except OSError:
            pass

        print("  [info] 结果文件就绪：{}".format(PHASE10_RESULT))

        # 验证 1：结果文件 7 项 ret=1
        results = _parse_result_file(PHASE10_RESULT)
        print("  [info] 结果文件 {} 项结果".format(len(results)))
        ok1 = len(results) >= 7
        if ok1:
            for line in results:
                if "ret=0" in line:
                    ok1 = False
                    print("  [FAIL:结果] {}".format(line))
            if ok1:
                print("  [PASS:结果文件] {} 项全部 ret=1".format(len(results)))
        else:
            print("  [FAIL:结果文件] 期望 >=7 项，实际 {} 项".format(len(results)))

        # 验证 2：mediator 日志中 ChildVtOutput 总字节 >= 阈值
        content = log.read_new()
        total_bytes = 0
        for m in re.finditer(r"ChildVtOutput:\s+len=(\d+)\s+written=(\d+)\s+ok=1", content):
            total_bytes += int(m.group(1))

        threshold = 800  # 目标程序输出 ~1500 字节，取 800 留足余量
        print("  [info] ChildVtOutput 总字节={}（阈值 {}）".format(total_bytes, threshold))
        ok2 = total_bytes >= threshold
        if ok2:
            print("  [PASS] ChildVtOutput 总字节 {} >= 阈值 {}".format(total_bytes, threshold))
        else:
            print("  [FAIL] ChildVtOutput 总字节 {} < 阈值 {}".format(total_bytes, threshold))

        ok = ok1 and ok2
    finally:
        injector.cleanup(target_pid, mediator_proc)
    return ok


def test_phase10_diff_render() -> bool:
    """测试 3：WriteConsoleOutput diff 算法验证。

    验证点：
      1. 结果文件 5 项 ret=1（与 test_phase10_diff.py 一致）
      2. injected 日志中 canDiff 标志正确：[0, 1, 1, 1, 0]
      3. diff 路径输出字节量远小于全量路径

    替代原 PTY-Agent 路径的 5x5 'A' 矩阵屏幕渲染验证。
    """
    print("\n" + "=" * 60)
    print("测试 3：Phase 10 任务6 WriteConsoleOutput diff 算法")
    print("=" * 60)

    target_pid, mediator_proc, log = _setup("WriteConsoleOutput-diff")
    if target_pid is None:
        return False

    ok = False
    try:
        # 清理旧结果文件
        if os.path.exists(PHASE10_DIFF_RESULT):
            try:
                os.remove(PHASE10_DIFF_RESULT)
            except OSError:
                pass

        # 运行目标程序
        if not _run_target_via_sendinput(TARGET_PHASE10_DIFF, PHASE10_DIFF_RESULT):
            print("  [FAIL] 目标程序超时未完成")
            return False

        # 检查是否 EXCEPTION
        try:
            with open(PHASE10_DIFF_RESULT, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "EXCEPTION" in content:
                print("  [FAIL] 目标程序异常退出")
                print(content)
                return False
        except OSError:
            pass

        print("  [info] 结果文件就绪：{}".format(PHASE10_DIFF_RESULT))

        # 提取 python PID 和结果
        pid = _extract_pid(PHASE10_DIFF_RESULT)
        results = _parse_results(PHASE10_DIFF_RESULT)
        print("  [info] python pid={}, 解析到 {} 项结果".format(pid, len(results)))

        # 验证 1：结果文件 5 项 ret=1
        expected = [
            "diff_full_init",
            "diff_single_change",
            "diff_revert",
            "diff_all_change",
            "diff_after_invalidate",
        ]
        ok1 = True
        for name in expected:
            if name not in results:
                print("  [FAIL] {} 未在结果文件中找到".format(name))
                ok1 = False
                continue
            r = results[name]
            if r["ret"] == 1:
                print("  [PASS:结果] {} ret=1".format(name))
            else:
                print("  [FAIL:结果] {} ret={} err={}".format(name, r["ret"], r["err"]))
                ok1 = False

        # 验证 2 + 3：injected 日志中 canDiff 标志和 outBytes
        if pid == 0:
            print("  [FAIL] 未在结果文件中找到 python pid")
            injector.cleanup(target_pid, mediator_proc)
            return False

        # 等 injected 日志写入完成
        time.sleep(0.5)
        injected_log = _read_injected_log(pid)
        if not injected_log:
            print("  [FAIL] injected 日志为空：{}\\injected_{}.log".format(
                INJECTED_LOG_DIR, pid))
            return False

        wco_logs = _extract_wco_logs(injected_log)
        print("  [info] injected 日志中 WriteConsoleOutput 行数={}".format(len(wco_logs)))

        if len(wco_logs) < 5:
            print("  [FAIL] WriteConsoleOutput 日志仅 {} 行（预期 5 行）".format(len(wco_logs)))
            return False

        # 取最后 5 行（目标程序的 5 次调用）
        calls = wco_logs[-5:]

        # 验证 canDiff 标志
        expected_can_diff = [0, 1, 1, 1, 0]
        expected_names = [
            "全量初始化",
            "diff 1 cell 变化",
            "diff 1 cell 恢复",
            "diff 25 cell 变化",
            "失效缓存后全量",
        ]
        ok2 = True
        for i, (call, exp, name) in enumerate(zip(calls, expected_can_diff, expected_names)):
            actual = call["canDiff"]
            if actual == exp:
                print("  [PASS:canDiff] 调用{} {} canDiff={}".format(i + 1, name, actual))
            else:
                print("  [FAIL:canDiff] 调用{} {} 期望 canDiff={}，实际 canDiff={}".format(
                    i + 1, name, exp, actual))
                ok2 = False

        # 验证 outBytes 减少
        full_init_bytes = calls[0]["outBytes"]
        single_change_bytes = calls[1]["outBytes"]

        if full_init_bytes < 50:
            print("  [FAIL:outBytes] 全量初始化 outBytes={}（预期 >= 50）".format(full_init_bytes))
            ok3 = False
        else:
            threshold = full_init_bytes // 3
            if single_change_bytes < threshold:
                print("  [PASS:outBytes] diff 1cell outBytes={} < 全量 {}/3={}".format(
                    single_change_bytes, full_init_bytes, threshold))
                ok3 = True
            else:
                print("  [FAIL:outBytes] diff 1cell outBytes={}（预期 < {}/3={}）".format(
                    single_change_bytes, full_init_bytes, threshold))
                ok3 = False

        ok = ok1 and ok2 and ok3
    finally:
        injector.cleanup(target_pid, mediator_proc)
    return ok


def run() -> int:
    """主入口：运行 Phase 10 屏幕渲染验证套件（非 PTY-Agent 路径）。"""
    print("=" * 60)
    print("Phase 10 屏幕渲染验证套件（WT + mediator 日志路径）")
    print("=" * 60)
    print("本套件用 WT + mediator 替代 PTY-Agent，通过 mediator 日志")
    print("验证 VT 输出序列和子进程输出到达，验证屏幕渲染结果。")
    print("注意：测试期间不要手动操作 WT 窗口。")
    time.sleep(1)

    tests = [
        ("StateSnapshot 补发验证", test_phase10_state_snapshot),
        ("任务4+5 HookWhitelist + IPC 合并", test_phase10_hookwhitelist_and_batch),
        ("任务6 diff 算法验证", test_phase10_diff_render),
    ]

    results = {}
    failures = 0
    for name, func in tests:
        try:
            ok = func()
        except Exception as e:
            import traceback
            print("[ERROR] 测试 {} 异常: {}".format(name, e))
            traceback.print_exc()
            ok = False
        results[name] = ok
        if not ok:
            failures += 1
        # 测试间隔，确保进程清理完成
        time.sleep(2.0)

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for name, _ in tests:
        status = "通过" if results.get(name, False) else "失败"
        print("  {:30s} {}".format(name, status))
    print("-" * 60)
    if failures == 0:
        print("全部通过")
    else:
        print("共 {} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())