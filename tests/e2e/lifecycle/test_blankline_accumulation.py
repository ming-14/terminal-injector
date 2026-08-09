"""特性: 同进程反复注入/卸载不累积空行    类别: lifecycle

回归测试（2026-08-10 修复 preReplayCur 记录时机）:
  同一 cmd 进程循环 注入→卸载（WT+mediator），卸载后 ConHost 屏幕中
  dir 统计行与 prompt 之间的空行逐轮累积（用户反馈：prompt 每轮下移一行，
  统计行与 prompt 间空行变多；偶发 `19    <DIR>  Videos` 前缀行错位）。

根因（Unloader.cpp 3.1 光标归位）:
  preReplayCur 在光标归位**前**记录（= KickStart 回车回显 `\r\n` 后的
  (0,N+1)，窗口外）；空会话重放仅 `ESC[0m`（SGR 不移动光标），重放后
  光标 (0,N) != preReplayCur (0,N+1) → 惰性重放分支（5.5 光标抬到擦除行
  上一行）永不触发 → cmd 回显 `\r\n` 后新 prompt 写到 N+1 行，快照
  prompt 行留空 → 每轮 prompt 下移一行、空行 +1。

修复: preReplayCur 在光标归位成功之后记录（归位后光标位置），
  空会话重放前后光标相同 → 惰性分支正确触发。

断言:
  - 卸载后 ConHost 屏幕：prompt 行（csbi 光标行）上方连续空行数
    必须等于 baseline（cmd 原生 dir 输出后空 1 行），不得随轮次增长
  - 每轮握手成功 + 10s 内 injected.dll 卸载

验证方式: 循环驱动 + AttachConsole 读全屏缓冲 + Toolhelp 模块枚举
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import injector

NAME = "blankline_accumulation"
ROUNDS = int(os.environ.get("ROUNDS", "8"))


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]


class _CSI(ctypes.Structure):
    _fields_ = [("dwSize", _COORD), ("dwCursorPosition", _COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD)]


_k = ctypes.windll.kernel32
_k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_CSI)]
_k.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
_k.ReadConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, ctypes.c_wchar_p,
                                           wintypes.DWORD, _COORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_k.ReadConsoleOutputCharacterW.restype = wintypes.BOOL

TH32CS_SNAPMODULE = 0x00000008
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD),
                ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


def has_module(pid: int, name: str) -> bool:
    """Toolhelp 枚举进程模块，判断 name 是否仍被加载。"""
    h = _k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
    if h in (INVALID_HANDLE_VALUE, 0):
        return False
    e = MODULEENTRY32()
    e.dwSize = ctypes.sizeof(MODULEENTRY32)
    ok = _k.Module32FirstW(h, ctypes.byref(e))
    found = False
    while ok:
        if e.szModule.lower() == name.lower():
            found = True
            break
        ok = _k.Module32NextW(h, ctypes.byref(e))
    _k.CloseHandle(h)
    return found


class _KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR),
                ("dwControlKeyState", wintypes.DWORD)]


class _INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD),
                ("Event", _KEY_EVENT_RECORD)]


def run_command(pid: int, text: str) -> bool:
    """AttachConsole 后 WriteConsoleInput 注入一行命令，触发 cmd 执行 dir。

    CONIN$ CreateFile 在部分宿主环境返回 PATH_NOT_FOUND，回退
    GetStdHandle(STD_INPUT_HANDLE)（AttachConsole 后有效）。
    """
    _k.FreeConsole()
    if not _k.AttachConsole(pid):
        print("  [DIAG] AttachConsole failed pid={} gle={}".format(pid, _k.GetLastError()))
        return False
    try:
        hIn = _k.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if hIn in (None, -1):
            print("  [DIAG] GetStdHandle failed gle={}".format(_k.GetLastError()))
            return False
        recs = []
        for ch in text + "\r":
            recs.append(_INPUT_RECORD(0x0001, _KEY_EVENT_RECORD(1, 1, 0, 0, ch, 0)))
        n = wintypes.DWORD(0)
        arr = (_INPUT_RECORD * len(recs))(*recs)
        wr = _k.WriteConsoleInputW(hIn, arr, len(recs), ctypes.byref(n))
        if not wr:
            print("  [DIAG] WriteConsoleInputW failed nWritten={} gle={}".format(
                n.value, _k.GetLastError()))
        return bool(wr)
    finally:
        _k.FreeConsole()


def read_screen(pid: int):
    """AttachConsole 读整个屏幕缓冲，返回 (rows_list, cursor_y)。失败返回 None。

    CONOUT$ CreateFile 在部分宿主环境返回 PATH_NOT_FOUND，回退
    GetStdHandle(STD_OUTPUT_HANDLE)（AttachConsole 后有效）。
    """
    _k.FreeConsole()
    if not _k.AttachConsole(pid):
        return None
    h = _k.CreateFileW("CONOUT$", 0x40000000 | 0x80000000, 2, None, 3, 0, None)
    if h in (None, -1):
        h = _k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    if h in (None, -1):
        _k.FreeConsole()
        return None
    try:
        info = _CSI()
        if not _k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return None
        cols, rows = info.dwSize.X, info.dwSize.Y
        result = []
        read = wintypes.DWORD(0)
        for r in range(rows):
            line = ctypes.create_unicode_buffer(cols)
            at = _COORD(0, r)
            if not _k.ReadConsoleOutputCharacterW(h, line, cols, at, ctypes.byref(read)):
                return None
            result.append(line.value)
        return result, info.dwCursorPosition.Y
    finally:
        _k.CloseHandle(h)
        _k.FreeConsole()


def analyze(pid: int):
    """返回 (last_content_row, blanks_above_prompt, prompt_row)。

    prompt 行 = csbi.dwCursorPosition.Y（AttachConsole 后即 cmd 当前光标行）。
    blanks = prompt 行上方连续空行数（从 prompt 上一行向上数到首个内容行）。
    cmd 原生行为：dir 统计行后空 1 行再 prompt（blanks=1）——
    修复后每轮 blanks 必须与 baseline 相同，不得随循环增长。
    """
    got = read_screen(pid)
    if got is None:
        return None
    rows, cur_y = got
    last_content = -1
    for i, r in enumerate(rows):
        if r.strip(" \x00"):
            last_content = i
    blanks = 0
    r = cur_y - 1
    while r >= 0 and not rows[r].strip(" \x00"):
        blanks += 1
        r -= 1
    return last_content, blanks, cur_y


def close_wt_and_wait_unload(pid: int) -> bool:
    """关闭测试 WT 窗口，等待 injected.dll 从目标进程卸载（10s 超时）。"""
    import win32gui
    import win32con
    if injector._test_wt_hwnd:
        win32gui.PostMessage(injector._test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not has_module(pid, "injected.dll"):
            return True
        time.sleep(0.3)
    return False


def run() -> int:
    failures = 0
    pid = injector.start_target_cmd()
    print("target cmd pid={}".format(pid))
    time.sleep(2.0)  # 等 cmd 渲染 banner+prompt

    # 先执行 dir 制造滚动历史（bug 触发条件：prompt 行进入滚动区、
    # 光标在窗口外，卸载重放必须走光标归位路径）
    if not run_command(pid, "dir"):
        print("  [FAIL] 无法向目标 cmd 注入 dir（WriteConsoleInput 失败）")
        injector.cleanup(pid)
        return 1
    time.sleep(3.0)

    base = analyze(pid)
    if base is None:
        print("  [FAIL] baseline 屏幕读取失败")
        injector.cleanup(pid)
        return 1
    print("baseline: last_content_row={} blanks={} prompt_row={}".format(*base))
    baseline_blanks = base[1]

    max_blanks = 0
    for i in range(1, ROUNDS + 1):
        mediator_proc = None
        try:
            mediator_proc = injector.start_wt_mediator(pid)
            if not injector.wait_for_handshake(pid, timeout=20.0):
                print("  轮 {}/{}: [FAIL] 握手失败".format(i, ROUNDS))
                failures += 1
                if mediator_proc:
                    mediator_proc.kill()
                time.sleep(2.0)
                continue
            time.sleep(1.0)
            if not close_wt_and_wait_unload(pid):
                print("  轮 {}/{}: [FAIL] 卸载超时".format(i, ROUNDS))
                failures += 1
                continue
            time.sleep(1.0)
            got = analyze(pid)
            if got is None:
                print("  轮 {}/{}: [FAIL] 屏幕读取失败".format(i, ROUNDS))
                failures += 1
                continue
            last_content, blanks, prompt_row = got
            max_blanks = max(max_blanks, blanks)
            ok = blanks == baseline_blanks
            print("  轮 {}/{}: last_content_row={} blanks={} prompt_row={} {}".format(
                i, ROUNDS, last_content, blanks, prompt_row,
                "[PASS]" if ok else "[FAIL] 空行累积（应为 {})".format(baseline_blanks)))
            if not ok:
                failures += 1
        except RuntimeError as e:
            print("  轮 {}/{}: [FAIL] {}".format(i, ROUNDS, e))
            failures += 1
        finally:
            if mediator_proc:
                try:
                    mediator_proc.kill()
                except Exception:
                    pass

    injector.cleanup(pid)
    print("\nSUMMARY: {} ({} failures), max blanks = {}".format(
        "PASS" if failures == 0 else "FAIL", failures, max_blanks))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
