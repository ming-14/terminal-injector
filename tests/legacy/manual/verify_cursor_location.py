"""
verify_cursor_location.py — 用 PTY-Agent mouse _get_cursor_location 验证注入后光标位置

测试流程：
  1. 启动一个 cmd 进程作为注入目标，等待 ConHost 输出版本横幅 + prompt
  2. 用 PTY-Agent exec 启动 terminal_injector.exe --mediator --target-pid <cmd_pid>
     --snapshot-mode 启用屏幕快照模式（mouse 命令要求 PTY + snapshot 模式）
  3. mediator fork --inject 子进程注入 DLL，DLL 启动后通过管道连接 mediator
  4. DLL 把 ConHost 屏幕内容补发到 mediator，mediator 写到 PTY-Agent 的 PTY
  5. 调用 `python app.py mouse test-cursor _get_cursor_location` 获取 PTY 终端光标位置
  6. 同时读取屏幕快照，验证光标位置与屏幕内容一致

验证项：
  - mouse _get_cursor_location 返回的光标坐标（col, row）
  - 光标行号接近屏幕底部（与 prompt 同行）
  - 屏幕快照中 prompt 位于最后一行

用法:
  python tests/manual/verify_cursor_location.py
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

# WT 终端尺寸（与 PTY-Agent 一致，确保不出现折行）
TERMINAL_SIZE = "120x40"
SESSION_ID = "test-cursor"


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
    """执行 PTY-Agent 命令，返回 (stdout, stderr) 文本"""
    cmd = ["python", PTY_AGENT] + args
    log(f"pty-agent cmd: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        if result.returncode != 0:
            log(f"pty-agent returncode={result.returncode}")
            log(f"pty-agent stderr: {result.stderr}")
        return result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        log(f"pty-agent timeout: {e}")
        return e.stdout or "", str(e)


def start_mediator_session(pid):
    """启动 mediator 会话（--snapshot-mode 让 mouse 命令可用）

    --snapshot-mode 禁用 trigger/idle-timeout，exec 立即返回首个屏幕快照
    会话在 daemon 后台继续运行，后续可用 mouse/read 命令交互
    """
    cmd_str = f'"{MEDIATOR_EXE}" --mediator --target-pid {pid}'
    args = [
        "exec", SESSION_ID,
        "-c", cmd_str,
        "--size", TERMINAL_SIZE,
        "--snapshot-mode",
        "--keep-ansi",
    ]
    out, err = run_pty_agent(args, timeout=30)
    return out, err


def get_cursor_location():
    """调用 mouse _get_cursor_location 获取 PTY 终端光标位置"""
    args = ["mouse", SESSION_ID, "_get_cursor_location"]
    out, err = run_pty_agent(args, timeout=15)
    return out, err


def read_snapshot():
    """读取屏幕快照（不消费 offset）"""
    args = ["read", SESSION_ID, "--snapshot"]
    out, err = run_pty_agent(args, timeout=15)
    return out, err


def parse_json(out):
    """安全解析 JSON，失败返回 None"""
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def parse_snapshot_text(data):
    """从 PTY-Agent 响应解析屏幕快照文本"""
    if not data:
        return None
    stream = data.get("outputStream")
    output = data.get("output", {})
    if isinstance(stream, str):
        return stream
    if isinstance(stream, list):
        return "\n".join(str(x) for x in stream)
    if isinstance(stream, dict):
        return stream.get("snapshot") or stream.get("text")
    if isinstance(output, dict):
        return output.get("snapshot") or output.get("screen") or output.get("text")
    return None


def verify(cursor_data, snapshot_text):
    """验证光标位置与屏幕内容，返回 (all_pass, results)"""
    results = []
    total_cols = int(TERMINAL_SIZE.split("x")[0])

    # 1. mouse _get_cursor_location 返回了光标
    cursor = cursor_data.get("cursor") if cursor_data else None
    if not cursor:
        results.append(("mouse _get_cursor_location 返回光标", False,
                        f"response={cursor_data}"))
        return False, results

    cur_col = cursor.get("col")
    cur_row = cursor.get("row")
    results.append(("mouse _get_cursor_location 返回光标", True,
                    f"cursor=(col={cur_col}, row={cur_row})"))

    # 2. 光标所在行就是 prompt 行（mouse 响应的 line 字段）
    # cmd 启动只输出横幅(2行)+空行(1行)+prompt(1行)，光标应在 prompt 行
    # PTY-Agent cursor.line 是光标所在行的文本内容
    cursor_line = cursor.get("line", "")
    cursor_on_prompt = bool(cursor_line) and re.search(
        r"[A-Za-z]:\\Users\\rikka[^>]*>", cursor_line)
    results.append((
        f"光标所在行是 prompt 行(row={cur_row})",
        bool(cursor_on_prompt),
        f"cursor_line={cursor_line!r}"
    ))

    # 3. 光标列号在合理范围（prompt 末尾，不超总列数）
    col_in_range = (cur_col is not None and 1 <= cur_col <= total_cols)
    results.append((
        f"光标列号在合理范围(col={cur_col}, total={total_cols})",
        col_in_range,
        f"期望 1 <= col <= {total_cols}"
    ))

    # 4. 屏幕快照验证（如果有的话）
    if snapshot_text:
        lines = snapshot_text.splitlines() if isinstance(snapshot_text, str) else [str(snapshot_text)]
        stripped_lines = [ln.rstrip() for ln in lines]
        non_empty = [ln for ln in stripped_lines if ln.strip()]

        # 4a. 屏幕包含版本横幅
        has_banner = any("Microsoft Windows" in ln for ln in stripped_lines)
        results.append(("屏幕包含版本横幅(Microsoft Windows)", has_banner,
                        f"non_empty_lines={len(non_empty)}"))

        # 4b. 屏幕包含 prompt
        prompt_pattern = r"[A-Za-z]:\\Users\\rikka[^>]*>"
        has_prompt = any(re.search(prompt_pattern, ln) for ln in stripped_lines)
        results.append(("屏幕包含 prompt", has_prompt, ""))

        # 4c. prompt 位于最后一行
        if has_prompt:
            prompt_in_nonempty = -1
            for i, ln in enumerate(non_empty):
                if re.search(prompt_pattern, ln):
                    prompt_in_nonempty = i
                    break
            is_last = (prompt_in_nonempty == len(non_empty) - 1)
            results.append((
                "prompt 位于最后一行",
                is_last,
                f"非空行位置={prompt_in_nonempty}/{len(non_empty)}"
            ))

            # 4d. 光标列号与 prompt 末尾对齐
            # prompt 形如 "C:\Users\rikka\Desktop\terminal-injector>"
            # 光标应在 prompt 末尾的 > 之后
            last_prompt_line = non_empty[prompt_in_nonempty] if prompt_in_nonempty >= 0 else ""
            # 计算显示宽度（ASCII 字符按 1 计）
            try:
                from wcwidth import wcswidth
                prompt_width = wcswidth(last_prompt_line)
            except ImportError:
                prompt_width = len(last_prompt_line)
            # 光标列号应 = prompt 宽度 + 1（> 之后）
            col_matches_prompt = (cur_col is not None and abs(cur_col - (prompt_width + 1)) <= 1)
            results.append((
                f"光标列号与 prompt 末尾对齐(col={cur_col}, prompt_width={prompt_width})",
                col_matches_prompt,
                f"prompt_line={last_prompt_line!r}"
            ))
    else:
        results.append(("屏幕快照验证", False, "snapshot 为空"))

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


def kill_pty_session():
    """清理 PTY-Agent 会话"""
    run_pty_agent(["kill", SESSION_ID], timeout=10)


def main():
    if not os.path.exists(MEDIATOR_EXE):
        print(f"[FAIL] mediator exe not found: {MEDIATOR_EXE}")
        return 1

    print("=" * 70)
    print(f"验证目标：用 mouse _get_cursor_location 获取注入后光标位置")
    print(f"会话 ID：{SESSION_ID}    终端尺寸：{TERMINAL_SIZE}")
    print("=" * 70)

    target_pid = None
    try:
        # 1. 启动目标 cmd
        target_pid = start_target_cmd()
        wait_for_cmd_prompt(target_pid, timeout=5.0)

        # 2. 启动 mediator 会话（snapshot-mode）
        exec_out, exec_err = start_mediator_session(target_pid)
        log(f"exec output (first 500): {exec_out[:500]}")

        # 3. 等待握手 + 屏幕补发完成
        log("waiting 3s for mediator to finish screen replay...")
        time.sleep(3.0)

        # 4. 调用 mouse _get_cursor_location
        mouse_out, mouse_err = get_cursor_location()
        log(f"mouse _get_cursor_location output:")
        print(mouse_out)
        if mouse_err:
            log(f"mouse stderr: {mouse_err}")

        cursor_data = parse_json(mouse_out)

        # 5. 读取屏幕快照（用于交叉验证）
        snap_out, _ = read_snapshot()
        snap_data = parse_json(snap_out)
        snapshot_text = parse_snapshot_text(snap_data)

        # 6. 验证
        all_pass, results = verify(cursor_data, snapshot_text)

        # 7. 输出结果
        print()
        print("-" * 70)
        print("验证结果：")
        print("-" * 70)
        for desc, ok, detail in results:
            mark = "[PASS]" if ok else "[FAIL]"
            print(f"{mark} {desc}")
            if detail:
                print(f"      详情: {detail}")
        print("-" * 70)
        if all_pass:
            print("总评：[PASS] 光标位置验证通过")
        else:
            print("总评：[FAIL] 光标位置仍有问题")

        # 8. 打印屏幕快照
        if snapshot_text:
            print()
            print("=" * 70)
            print("屏幕快照内容：")
            print("=" * 70)
            if isinstance(snapshot_text, str):
                lines = snapshot_text.splitlines()
                for i, ln in enumerate(lines, 1):
                    print(f"{i:3d}| {ln}")
            else:
                print(snapshot_text)
            print("=" * 70)

        return 0 if all_pass else 1
    finally:
        kill_pty_session()
        if target_pid:
            cleanup_cmd(target_pid)


if __name__ == "__main__":
    sys.exit(main())
