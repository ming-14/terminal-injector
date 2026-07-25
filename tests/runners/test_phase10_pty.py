"""Phase 10 PTY-Agent 屏幕快照重测套件。

用 PTY-Agent 替代 WT 作为 mediator 的 PTY 父端，通过 pyte 终端模拟器
渲染 mediator 输出的 VT 序列，验证屏幕最终渲染内容（而非仅 VT 字节流）。

覆盖 Phase 10 三个任务：
  - 任务4 HookWhitelist 重入防护：phase10_target 调大量 WriteConsoleA/W/WriteFile
    不死锁，输出到达屏幕
  - 任务5 IPC 小包合并：phase10_target 高频小包合并后屏幕内容完整不丢失
  - 任务6 WriteConsoleOutput diff：phase10_diff_target 5 次 WriteConsoleOutputW
    后屏幕 5x5 矩阵渲染为 'A'（diff 不破坏渲染）

与 test_phase10.py / test_phase10_diff.py 的差异：
  - 后者走 WT + SendInput + mediator 日志验证（字节流）
  - 本套件走 PTY-Agent + send + 屏幕快照验证（渲染结果）
  - 两者互补，不替代

依赖：
  - tests/helpers/pty_agent.py（PTY-Agent CLI 封装）
  - PTY-Agent 项目（c:\\Users\\rikka\\Desktop\\PTY-Agent\\PTY-Agent\\app.py 或 PTY_AGENT_PATH 环境变量）
  - psutil
"""
import os
import re
import sys
import time

# 添加 helpers 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helpers.pty_agent import (  # noqa: E402
    PtyAgentSession,
    cleanup,
    clear_log,
    parse_snapshot_text,
    start_target_cmd,
    wait_for_handshake,
)

# 项目路径
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
PHASE10_RESULT = os.path.join(PROJECT_ROOT, "phase10_result.txt")
PHASE10_DIFF_RESULT = os.path.join(PROJECT_ROOT, "phase10_diff_result.txt")
TARGET_PHASE10 = r"python tests\targets\test_phase10_target.py"
TARGET_PHASE10_DIFF = r"python tests\targets\test_phase10_diff_target.py"

# 终端尺寸（与 verify_cursor_init.py 一致，避免折行）
TERMINAL_SIZE = "120x40"


def _parse_result_file(path: str) -> list:
    """解析结果文件，返回 TEST 行列表。

    每行格式：TEST <name> ret=<0|1> err=<N> [key=value ...]
    """
    if not os.path.exists(path):
        return []
    results = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("TEST "):
                results.append(line)
    return results


def _setup_session(session_id: str, label: str):
    """通用 setup：启动 cmd + 清日志 + 启动 PTY-Agent mediator + 等握手。

    返回 (target_pid, session) 或 (None, None) 表示失败。
    """
    print("\n[setup:{}] 启动目标 cmd...".format(label))
    target_pid = start_target_cmd()
    print("[setup:{}] cmd PID={}".format(label, target_pid))

    clear_log()

    print("[setup:{}] 启动 PTY-Agent mediator 会话...".format(label))
    session = PtyAgentSession(session_id, size=TERMINAL_SIZE)
    if not session.start_mediator(target_pid, snapshot_mode=True, timeout=30):
        print("[setup:{}] PTY-Agent 启动 mediator 失败".format(label))
        cleanup(target_pid, session)
        return None, None

    print("[setup:{}] 等待握手...".format(label))
    if not wait_for_handshake(timeout=15):
        print("[setup:{}] 握手失败".format(label))
        cleanup(target_pid, session)
        return None, None
    print("[setup:{}] 握手成功".format(label))

    # 等待屏幕稳定（StateSnapshot 补发 cmd banner + prompt）
    time.sleep(2.0)
    return target_pid, session


def _verify_snapshot_contains(snapshot: str, patterns: list, label: str) -> bool:
    """验证屏幕快照包含所有指定模式，返回是否全部命中。

    Args:
        snapshot: 屏幕快照文本
        patterns: 期望出现的字符串列表
        label: 测试标签（用于日志）
    """
    all_ok = True
    for pat in patterns:
        if pat in snapshot:
            print("  [PASS:{}] 屏幕包含 {!r}".format(label, pat))
        else:
            print("  [FAIL:{}] 屏幕未包含 {!r}".format(label, pat))
            all_ok = False
    return all_ok


def _verify_snapshot_not_contains(snapshot: str, patterns: list, label: str) -> bool:
    """验证屏幕快照不包含指定模式（用于检测死锁/卡死/异常输出）。"""
    all_ok = True
    for pat in patterns:
        if pat in snapshot:
            print("  [FAIL:{}] 屏幕误包含 {!r}".format(label, pat))
            all_ok = False
        else:
            print("  [PASS:{}] 屏幕未包含 {!r}".format(label, pat))
    return all_ok


def _print_snapshot_summary(snapshot: str, max_lines: int = 15) -> None:
    """打印屏幕快照摘要（最后 N 行），便于人工核对。"""
    if not snapshot:
        print("  [warn] 屏幕快照为空")
        return
    lines = snapshot.splitlines()
    if len(lines) <= max_lines:
        tail = lines
    else:
        tail = lines[-max_lines:]
    print("  [屏幕最后 {} 行]".format(len(tail)))
    for i, ln in enumerate(tail, start=max(1, len(lines) - max_lines + 1)):
        # 截断过长的行
        display = ln if len(ln) <= 100 else ln[:100] + "..."
        print("    {:3d}| {}".format(i, display))


def test_phase10_hookwhitelist_and_batch() -> bool:
    """任务4 + 任务5：phase10_target 调大量 Console API，验证屏幕渲染。

    验证点：
      1. 屏幕上能看到 phase10_target_done 标记（程序不死锁、完成执行）
      2. 屏幕上能看到 Phase10Conout 输出（最后的输出，未被滚动出去）
      3. 屏幕上能看到 MIX_A 或 MIX_W 输出（A→W 复用路径工作）
      4. 屏幕上能看到 L 字符（Logger worker 重入压测输出）
      5. 结果文件 7 项 ret=1（与原 phase10 测试一致，字节流层验证）
    """
    print("\n" + "=" * 60)
    print("测试 1：Phase 10 任务4+5 屏幕渲染验证（PTY-Agent 路径）")
    print("=" * 60)

    target_pid, session = _setup_session("p10hw", "HookWhitelist+Batch")
    if target_pid is None:
        return False

    ok = False
    try:
        # 发送运行目标程序命令
        # trigger 等待 "phase10_target_done" 出现，确认程序跑完
        print("\n[run] 发送命令: {}".format(TARGET_PHASE10))
        resp = session.send(
            TARGET_PHASE10,
            trigger=r"phase10_target_done",
            snapshot=True,
            timeout=60,
        )
        if "error" in resp:
            print("  [FAIL] send 返回错误: {}".format(resp.get("error")))
            return False

        reason = resp.get("triggerReturnReason")
        print("  [info] triggerReturnReason={!r}".format(reason))

        # 取屏幕快照
        snapshot = parse_snapshot_text(resp)
        if not snapshot:
            # send 没拿到快照，单独读一次
            print("  [warn] send 未返回快照，单独读取...")
            snap_resp = session.read_snapshot(timeout=15)
            snapshot = parse_snapshot_text(snap_resp)

        _print_snapshot_summary(snapshot)

        # 验证 1：屏幕包含 phase10_target_done（程序完成）
        # 验证 2：屏幕包含 Phase10Conout（最后输出）
        # 验证 3：屏幕包含 MIX_A 或 MIX_W（A→W 复用）
        # 验证 4：屏幕包含 L*64 字符串（Logger 压测）
        #
        # 注意：phase10_target 总输出 ~400 行，远超 40 行屏幕，
        # 早期 Phase10A/File 已滚出屏幕，只能验证最后的输出
        patterns_required = ["phase10_target_done"]
        patterns_optional_any = ["MIX_A", "MIX_W", "Phase10Conout", "LLLL"]

        ok1 = _verify_snapshot_contains(snapshot, patterns_required, "完成标记")
        # 至少命中一个可选模式
        hit_optional = [p for p in patterns_optional_any if p in snapshot]
        if hit_optional:
            print("  [PASS:Hook 输出] 屏幕包含 {}".format(hit_optional))
            ok2 = True
        else:
            print("  [FAIL:Hook 输出] 屏幕未包含任何 {}（输出可能被滚动）".format(
                patterns_optional_any))
            ok2 = False

        # 验证结果文件 7 项 ret=1（与原 phase10 测试一致）
        results = _parse_result_file(PHASE10_RESULT)
        print("\n  [info] 结果文件 {} 项结果".format(len(results)))
        ok3 = len(results) >= 7
        if ok3:
            for line in results:
                if "ret=0" in line:
                    ok3 = False
                    print("  [FAIL:结果] {}".format(line))
            if ok3:
                print("  [PASS:结果文件] {} 项全部 ret=1".format(len(results)))
        else:
            print("  [FAIL:结果文件] 期望 >=7 项，实际 {} 项".format(len(results)))

        ok = ok1 and ok2 and ok3
    finally:
        cleanup(target_pid, session)
    return ok


def test_phase10_diff_render() -> bool:
    """任务6：phase10_diff_target 5 次 WriteConsoleOutputW，验证屏幕渲染。

    验证点：
      1. 屏幕左上角 5x5 区域渲染为 'A'（diff 不破坏最终渲染）
      2. 屏幕底部包含 DONE 标记（程序正常完成）
      3. 结果文件 5 项 ret=1（与原 phase10_diff 测试一致）

    屏幕预期：
      - WriteConsoleOutputW 写到屏幕 (0,0)-(4,4) 区域
      - 5 次调用后最终内容是 5x5 'A'（第5次失效缓存后全量写 'A'）
      - print("DONE") 写到屏幕底部光标位置
    """
    print("\n" + "=" * 60)
    print("测试 2：Phase 10 任务6 diff 算法屏幕渲染验证（PTY-Agent 路径）")
    print("=" * 60)

    target_pid, session = _setup_session("p10diff", "WriteConsoleOutput-diff")
    if target_pid is None:
        return False

    ok = False
    try:
        # 清理旧结果文件（避免读到上次结果）
        if os.path.exists(PHASE10_DIFF_RESULT):
            os.remove(PHASE10_DIFF_RESULT)

        print("\n[run] 发送命令: {}".format(TARGET_PHASE10_DIFF))
        resp = session.send(
            TARGET_PHASE10_DIFF,
            trigger=r"DONE|EXCEPTION",
            snapshot=True,
            timeout=60,
        )
        if "error" in resp:
            print("  [FAIL] send 返回错误: {}".format(resp.get("error")))
            return False

        reason = resp.get("triggerReturnReason")
        print("  [info] triggerReturnReason={!r}".format(reason))

        snapshot = parse_snapshot_text(resp)
        if not snapshot:
            print("  [warn] send 未返回快照，单独读取...")
            snap_resp = session.read_snapshot(timeout=15)
            snapshot = parse_snapshot_text(snap_resp)

        _print_snapshot_summary(snapshot)

        # 验证 1：屏幕包含 DONE（程序完成）
        ok1 = _verify_snapshot_contains(snapshot, ["DONE"], "完成标记")
        # 验证 2：屏幕不包含 EXCEPTION（无异常）
        ok2 = _verify_snapshot_not_contains(snapshot, ["EXCEPTION"], "无异常")

        # 验证 3：左上角 5x5 'A' 矩阵
        # WriteConsoleOutputW 写到 (0,0)-(4,4)，每行前 5 个字符应为 'A'
        # 屏幕快照按行分割，取前 5 行，每行前 5 个字符
        lines = snapshot.splitlines()
        matrix_ok = True
        if len(lines) < 5:
            print("  [FAIL:5x5 矩阵] 屏幕行数 {} < 5".format(len(lines)))
            matrix_ok = False
        else:
            for r in range(5):
                line = lines[r]
                # 取前 5 个字符（去除 ANSI 码后）
                # 注意：snapshot 可能包含 VT 序列残留，先粗略清理
                clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line)
                prefix = clean[:5]
                if prefix == "AAAAA":
                    print("  [PASS:5x5 矩阵] 行{} = 'AAAAA'".format(r))
                else:
                    print("  [FAIL:5x5 矩阵] 行{} = {!r}（期望 'AAAAA'）".format(
                        r, prefix))
                    matrix_ok = False
        ok3 = matrix_ok

        # 验证 4：结果文件 5 项 ret=1
        results = _parse_result_file(PHASE10_DIFF_RESULT)
        print("\n  [info] 结果文件 {} 项结果".format(len(results)))
        ok4 = len(results) >= 5
        if ok4:
            for line in results:
                if "ret=0" in line:
                    ok4 = False
                    print("  [FAIL:结果] {}".format(line))
            if ok4:
                print("  [PASS:结果文件] {} 项全部 ret=1".format(len(results)))
        else:
            print("  [FAIL:结果文件] 期望 >=5 项，实际 {} 项".format(len(results)))

        ok = ok1 and ok2 and ok3 and ok4
    finally:
        cleanup(target_pid, session)
    return ok


def test_phase10_initial_screen() -> bool:
    """前置验证：注入后初始屏幕应包含 cmd 版本横幅 + prompt。

    这是 PTY-Agent 路径独有的验证（WT 路径无法读取屏幕渲染结果）。
    用于确认 PTY-Agent + mediator 链路工作正常，再进入正式测试。

    验证点：
      1. 屏幕包含 "Microsoft Windows"（cmd 版本横幅）
      2. 屏幕包含 prompt（盘符:\\...>）
      3. prompt 在最后一行（不是顶部第一行）
    """
    print("\n" + "=" * 60)
    print("测试 0：PTY-Agent 路径前置验证（注入后初始屏幕）")
    print("=" * 60)

    target_pid, session = _setup_session("p10init", "InitialScreen")
    if target_pid is None:
        return False

    ok = False
    try:
        # 不发送任何命令，仅读取初始屏幕快照
        resp = session.read_snapshot(timeout=15)
        if "error" in resp:
            print("  [FAIL] read_snapshot 返回错误: {}".format(resp.get("error")))
            return False

        snapshot = parse_snapshot_text(resp)
        _print_snapshot_summary(snapshot)

        # 验证 1：屏幕包含版本横幅
        ok1 = _verify_snapshot_contains(snapshot, ["Microsoft Windows"], "版本横幅")

        # 验证 2：屏幕包含 prompt（C:\...>）
        prompt_pattern = r"[A-Za-z]:\\[^>]*>"
        has_prompt = bool(re.search(prompt_pattern, snapshot))
        if has_prompt:
            print("  [PASS:prompt] 屏幕包含 prompt")
        else:
            print("  [FAIL:prompt] 屏幕未包含 prompt")
        ok2 = has_prompt

        # 验证 3：prompt 在最后一行
        ok3 = False
        if has_prompt:
            lines = [ln.rstrip() for ln in snapshot.splitlines()]
            non_empty = [ln for ln in lines if ln.strip()]
            if non_empty and re.search(prompt_pattern, non_empty[-1]):
                print("  [PASS:prompt 位置] prompt 在最后一行")
                ok3 = True
            else:
                print("  [FAIL:prompt 位置] prompt 不在最后一行（最后非空行: {!r}）".format(
                    non_empty[-1] if non_empty else "<空>"))

        ok = ok1 and ok2 and ok3
    finally:
        cleanup(target_pid, session)
    return ok


def run() -> int:
    """主入口：运行 Phase 10 PTY-Agent 屏幕快照重测套件。"""
    print("=" * 60)
    print("Phase 10 PTY-Agent 屏幕快照重测套件")
    print("=" * 60)
    print("本套件用 PTY-Agent 替代 WT 作为 mediator 的 PTY 父端，")
    print("通过 pyte 终端模拟器渲染 VT 序列，验证屏幕最终渲染内容。")
    print("注意：测试期间不要手动操作 cmd/PTY-Agent 窗口。")
    time.sleep(1)

    tests = [
        ("初始屏幕验证", test_phase10_initial_screen),
        ("任务4+5 屏幕渲染", test_phase10_hookwhitelist_and_batch),
        ("任务6 diff 屏幕渲染", test_phase10_diff_render),
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
        print("  {:20s} {}".format(name, status))
    print("-" * 60)
    if failures == 0:
        print("全部通过")
    else:
        print("共 {} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
