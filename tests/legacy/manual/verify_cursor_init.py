"""
verify_cursor_init.py — 验证注入后光标初始位置是否正确

测试流程：
  1. 启动一个 cmd 进程作为注入目标，等待 ConHost 输出版本横幅 + prompt
  2. 用 PTY-Agent exec 启动 terminal_injector.exe --mediator --target-pid <cmd_pid>
     --snapshot-mode 启用屏幕快照模式，PTY-Agent 用自己的 PTY 渲染 mediator 的 VT 输出
  3. mediator fork --inject 子进程注入 DLL，DLL 启动后通过管道连接 mediator
  4. DLL 把 ConHost 屏幕内容补发到 mediator，mediator 写到 PTY-Agent 的 PTY
  5. PTY-Agent 屏幕快照返回实际渲染内容，验证：
     - 屏幕上能看到 cmd 版本横幅（"Microsoft Windows"）
     - 屏幕上能看到 prompt（"C:\\Users\\rikka>"）
     - prompt 出现在最后一行（不是顶部第一行）
     - 光标位置应该在 prompt 末尾

验证项（与 Rikka 描述的 bug 对齐）：
  - 旧 bug：注入后 WT 显示在顶部第一行（C:\\Users\\rikka>），实际应在底部最后一行
  - 修复后：补发屏幕内容，让 WT 渲染出完整的 cmd 版本横幅 + prompt

用法:
  python tests/manual/verify_cursor_init.py
"""
import os
import re
import subprocess
import sys
import time
import json
import win32process
import win32api
import win32con

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

PROJECT_ROOT = paths.project_root()
MEDIATOR_EXE = os.path.join(paths.build_bin(), "terminal_injector.exe")
PTY_AGENT = paths.pty_agent()

# WT 终端尺寸（与默认 PTY-Agent 80x24 一致，确保不出现折行）
TERMINAL_SIZE = "120x40"


def log(msg):
    print(f"[verify] {msg}", flush=True)


def start_target_cmd():
    """启动一个 cmd 进程作为注入目标，返回 pid。

    使用 CREATE_NEW_CONSOLE 让 cmd 在自己的 ConHost 中启动并输出横幅。
    纯 cmd.exe 启动（不带 /K），确保输出版本横幅 + prompt（与用户场景一致）。
    工作目录通过 CreateProcess 的 lpCurrentDirectory 参数指定。
    """
    si = win32process.STARTUPINFO()
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_SHOW  # 显示原 cmd 窗口，让 ConHost 输出内容
    cmd_line = 'cmd.exe'
    handle, thread_handle, pid, tid = win32process.CreateProcess(
        None, cmd_line, None, None, False,
        win32con.CREATE_NEW_CONSOLE,
        None, PROJECT_ROOT, si)
    win32api.CloseHandle(handle)
    win32api.CloseHandle(thread_handle)
    log(f"target cmd started, pid={pid}")
    return pid


def wait_for_cmd_prompt(pid, timeout=5.0):
    """等待 cmd 输出 prompt（ConHost 渲染版本横幅 + prompt 需要时间）"""
    log(f"waiting {timeout}s for cmd to render banner+prompt...")
    time.sleep(timeout)


def run_pty_agent(args, timeout=30):
    """执行 PTY-Agent 命令，返回 stdout 文本"""
    cmd = ["python", PTY_AGENT] + args
    log(f"pty-agent cmd: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        if result.returncode != 0:
            log(f"pty-agent stderr: {result.stderr}")
        return result.stdout
    except subprocess.TimeoutExpired as e:
        log(f"pty-agent timeout: {e}")
        return e.stdout or ""


def start_mediator_session(pid, session_id):
    """启动 mediator 会话（不立即返回，等待后台注入完成）"""
    # 不用 --snapshot-mode，让 exec 命令快速返回（mediator 仍在运行）
    # 之后用 read --snapshot 获取屏幕快照
    # --idle-timeout 3 等待 3 秒无新输出后返回（注入 + 握手 + 补发屏幕）
    # --timeout 15 兜底超时
    # --keep-ansi 保留完整 VT 序列便于调试
    cmd_str = f'"{MEDIATOR_EXE}" --mediator --target-pid {pid}'
    args = [
        "exec", session_id,
        "-c", cmd_str,
        "--size", TERMINAL_SIZE,
        "--idle-timeout", "3",
        "--timeout", "15",
        "--keep-ansi",
    ]
    out = run_pty_agent(args, timeout=30)
    return out


def read_snapshot(session_id):
    """读取屏幕快照（不消费 offset）"""
    args = ["read", session_id, "--snapshot"]
    out = run_pty_agent(args, timeout=15)
    return out


def parse_snapshot(out):
    """从 PTY-Agent 输出解析屏幕快照内容，返回 (snapshot_text, debug_info)"""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None, {"raw": out[:500]}

    # PTY-Agent 不同模式返回字段不同：
    # - 增量模式：outputStream 是字节流
    # - 屏幕快照模式：outputStream 是屏幕内容（可能为多行字符串或列表）
    output = data.get("output", {})
    stream = data.get("outputStream")
    snapshot = None
    cursor = {}

    # 屏幕快照模式通常在 outputStream 里返回快照文本
    if stream is not None:
        if isinstance(stream, str):
            snapshot = stream
        elif isinstance(stream, list):
            snapshot = "\n".join(str(x) for x in stream)
        elif isinstance(stream, dict):
            snapshot = stream.get("snapshot") or stream.get("text")
            cursor = stream.get("cursor") or {}
    # 兼容 output 字段
    if not snapshot and isinstance(output, dict):
        snapshot = output.get("snapshot") or output.get("screen") or output.get("text")
        cursor = output.get("cursor") or cursor

    debug = {
        "raw_keys": list(data.keys()),
        "output_keys": list(output.keys()) if isinstance(output, dict) else None,
        "cursor": cursor,
        "triggerReturnReason": data.get("triggerReturnReason"),
        "program": data.get("program"),
        "hint": data.get("hint"),
    }
    return snapshot, debug


def verify_snapshot(snapshot, debug):
    """验证快照内容，返回 (all_pass, results)"""
    results = []

    if not snapshot:
        results.append(("snapshot存在", False, "snapshot 为空"))
        return False, results

    # snapshot 可能是字符串或多行
    if isinstance(snapshot, list):
        lines = [str(x) for x in snapshot]
        text = "\n".join(lines)
    elif isinstance(snapshot, str):
        lines = snapshot.splitlines()
        text = snapshot
    else:
        lines = str(snapshot).splitlines()
        text = str(snapshot)

    # 去掉行尾空格
    stripped_lines = [ln.rstrip() for ln in lines]
    # 非空行
    non_empty = [ln for ln in stripped_lines if ln.strip()]
    last_non_empty = non_empty[-1] if non_empty else ""

    # 1. 检查屏幕上能看到版本横幅
    has_banner = any("Microsoft Windows" in ln for ln in stripped_lines)
    results.append(("屏幕包含版本横幅(Microsoft Windows)", has_banner,
                    f"最后非空行: {last_non_empty!r}"))

    # 2. 检查屏幕上能看到 prompt
    # prompt 形如 C:\Users\rikka> 或 C:\Users\rikka\Desktop\terminal-injector>
    prompt_pattern = r"[A-Za-z]:\\Users\\rikka[^>]*>"
    has_prompt = any(re.search(prompt_pattern, ln) for ln in stripped_lines)
    results.append(("屏幕包含 prompt(盘符:\\Users\\rikka...>)", has_prompt,
                    f"non_empty_lines={len(non_empty)}"))

    # 3. 检查 prompt 在最后一行（不是顶部第一行）
    # 顶部第一行的 prompt 就是 bug 现象
    if has_prompt:
        prompt_line_idx = next(
            (i for i, ln in enumerate(stripped_lines) if re.search(prompt_pattern, ln)),
            -1)
        # 在非空行中的位置
        prompt_in_nonempty = -1
        for i, ln in enumerate(non_empty):
            if re.search(prompt_pattern, ln):
                prompt_in_nonempty = i
                break
        # 期望 prompt 是最后一个非空行
        is_last = (prompt_in_nonempty == len(non_empty) - 1)
        results.append((
            "prompt 位于最后一行（非顶部第一行）",
            is_last,
            f"prompt 行号(0-based)={prompt_line_idx}/{len(stripped_lines)}, "
            f"非空行位置={prompt_in_nonempty}/{len(non_empty)}"
        ))
    else:
        results.append(("prompt 位于最后一行", False, "无 prompt 无法判断"))

    # 4. 光标位置检查（如果 PTY-Agent 返回了 cursor）
    cursor = debug.get("cursor") or {}
    if cursor:
        cur_row = cursor.get("row")
        cur_col = cursor.get("col")
        # 期望光标行号接近屏幕底部（与 prompt 同行或之后）
        # PTY-Agent 的 row 是 1-based
        if cur_row is not None:
            # 屏幕总行数 = TERMINAL_SIZE 的 H
            total_rows = int(TERMINAL_SIZE.split("x")[1])
            is_near_bottom = (cur_row >= total_rows - 5)
            results.append((
                f"光标行号接近底部(row={cur_row}, total={total_rows})",
                is_near_bottom,
                f"cursor=(row={cur_row}, col={cur_col})"
            ))

    all_pass = all(r[1] for r in results)
    return all_pass, results


def cleanup_cmd(pid):
    """清理目标 cmd 进程"""
    try:
        import psutil
        p = psutil.Process(pid)
        p.kill()
        p.wait(timeout=3)
        log(f"target cmd killed, pid={pid}")
    except Exception as e:
        log(f"cleanup_cmd failed: {e}")


def kill_pty_session(session_id):
    """清理 PTY-Agent 会话"""
    run_pty_agent(["kill", session_id], timeout=10)


def main():
    if not os.path.exists(MEDIATOR_EXE):
        print(f"[FAIL] mediator exe not found: {MEDIATOR_EXE}")
        return 1

    print("=" * 70)
    print("验证目标：注入后 cmd 光标初始位置是否正确（应在底部最后一行 prompt 处）")
    print("=" * 70)

    session_id = "cursor_test"
    target_pid = None
    try:
        # 1. 启动目标 cmd
        target_pid = start_target_cmd()
        # 2. 等待 cmd 输出 banner + prompt
        wait_for_cmd_prompt(target_pid, timeout=5.0)

        # 3. 启动 mediator 会话（exec 命令在 idle-timeout 后返回）
        exec_out = start_mediator_session(target_pid, session_id)
        log(f"exec output: {exec_out[:500]}")

        # 3.5 等待 mediator 完成屏幕补发
        log("waiting 2s for mediator to finish screen replay...")
        time.sleep(2.0)

        # 4. 读取屏幕快照
        out = read_snapshot(session_id)
        log(f"read snapshot output length: {len(out)}")

        # 5. 解析屏幕快照
        snapshot, debug = parse_snapshot(out)
        log(f"debug info: {debug}")

        # 5. 验证
        all_pass, results = verify_snapshot(snapshot, debug)

        # 6. 输出结果
        print()
        print("-" * 70)
        print("验证结果：")
        print("-" * 70)
        for desc, ok, detail in results:
            mark = "[PASS]" if ok else "[FAIL]"
            print(f"{mark} {desc}")
            print(f"      详情: {detail}")
        print("-" * 70)
        if all_pass:
            print("总评：[PASS] 光标初始位置修复验证通过")
        else:
            print("总评：[FAIL] 光标初始位置仍有问题")

        # 7. 打印屏幕快照（便于人工核对）
        if snapshot:
            print()
            print("=" * 70)
            print("屏幕快照内容：")
            print("=" * 70)
            if isinstance(snapshot, list):
                for i, ln in enumerate(snapshot, 1):
                    print(f"{i:3d}| {ln}")
            else:
                print(snapshot)
            print("=" * 70)

        return 0 if all_pass else 1
    finally:
        # 清理
        if target_pid:
            cleanup_cmd(target_pid)
        kill_pty_session(session_id)


if __name__ == "__main__":
    sys.exit(main())
