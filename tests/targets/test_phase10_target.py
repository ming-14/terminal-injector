"""Phase 10 HookWhitelist 重入防护专项测试目标程序。

验证 HookReentryGuard + ASSERT_IN_HOOK 的重入防护机制工作正常：
  1. WriteConsoleA 高频压测：触发 A→W 复用路径（深度=2），不死锁
  2. WriteFile(CONOUT$) 压测：WriteFile_Detour→WriteConsoleW_Detour 复用路径
  3. FillConsoleOutputCharacterA：A→W 复用路径
  4. 混合 A/W 调用：验证 HookReentryGuard 深度计数正确归零
  5. Logger worker 重入：大量输出触发 Logger 高频 WriteFile，验证 pass-through

DLL 已通过子进程注入（ProcessHooks）注入到本 python 进程，故以下 API
调用都会经过 Detour，触发 HookReentryGuard 计数。

链路：
  runner 启动注入 cmd + WT(mediator) → SendInput 输入 python 命令
  → cmd CreateProcess python（DLL ProcessHooks 拦截 + 注入 DLL 到 python）
  → python 调用 Console API → DLL Hook 拦截，HookReentryGuard 计数
  → python 拿到返回值 → 写结果文件
  → runner 读结果文件 + mediator 日志验证

结果文件路径由环境变量 PHASE10_RESULT_FILE 指定，默认 ./phase10_result.txt
每行格式：
  TEST <name> ret=<0|1> err=<N> [key=value ...]

依赖：仅 ctypes（Python 3.8+，无需第三方包）
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

# ============================================================
# Win32 API 常量
# ============================================================
STD_INPUT_HANDLE = 0xFFFFFFF6
STD_OUTPUT_HANDLE = 0xFFFFFFF5
STD_ERROR_HANDLE = 0xFFFFFFF4

# CreateFile 用
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_SHARE_WRITE = 0x00000004
FILE_SHARE_READ = 0x00000001
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


# ============================================================
# Win32 API 绑定（ctypes）
# ============================================================
_kernel32 = ctypes.windll.kernel32
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD
_kernel32.SetLastError.argtypes = [wintypes.DWORD]
_kernel32.SetLastError.restype = None
_kernel32.GetCurrentProcessId.argtypes = []
_kernel32.GetCurrentProcessId.restype = wintypes.DWORD

# WriteConsoleA/W
_kernel32.WriteConsoleA.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.WriteConsoleA.restype = wintypes.BOOL
_kernel32.WriteConsoleW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.WriteConsoleW.restype = wintypes.BOOL

# WriteFile
_kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_kernel32.WriteFile.restype = wintypes.BOOL

# CreateFileW（用于打开 CONOUT$ 测试 WriteFile Console 路径）
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE

# CloseHandle（清理 CreateFile 句柄）
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

# FillConsoleOutputCharacterA/W
_kernel32.FillConsoleOutputCharacterA.argtypes = [
    wintypes.HANDLE, ctypes.c_char, wintypes.DWORD,
    wintypes.DWORD,  # COORD 打包为 DWORD
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.FillConsoleOutputCharacterA.restype = wintypes.BOOL
_kernel32.FillConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE, wintypes.WCHAR, wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.FillConsoleOutputCharacterW.restype = wintypes.BOOL

# GetConsoleScreenBufferInfo（取光标位置作为填充起点）
class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes._COORD),
        ("dwCursorPosition", wintypes._COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", wintypes._COORD),
    ]


_kernel32.GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO),
]
_kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL


def _coord_to_dword(x: int, y: int) -> int:
    """COORD 打包为 DWORD（低 16 位 X，高 16 位 Y）。"""
    return (y << 16) | (x & 0xFFFF)


def write_result(f, name: str, ret: int, err: int, **extra) -> None:
    """写一行结果到结果文件。

    格式：TEST <name> ret=<0|1> err=<N> [key=value ...]
    """
    parts = ["TEST", name, "ret={}".format(int(bool(ret))), "err={}".format(err)]
    for k, v in extra.items():
        parts.append("{}={}".format(k, v))
    f.write(" ".join(parts) + "\n")
    f.flush()


def _run_tests(f, own_pid: int) -> None:
    """执行所有 Phase 10 验证项，结果写入 f。异常由调用方捕获。"""
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    csbi = _CONSOLE_SCREEN_BUFFER_INFO()
    _kernel32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(csbi))
    start_coord = _coord_to_dword(csbi.dwCursorPosition.X, csbi.dwCursorPosition.Y)

    # ============================================================
    # 测试 1：WriteConsoleA 高频压测（A→W 复用路径，深度=2）
    # ============================================================
    # DLL WriteConsoleA_Detour → MultiByteToWideChar → WriteConsoleW_Detour
    # HookReentryGuard 深度：1（A 版）→ 2（W 版），合法路径，不应触发断言
    # 预期：N 次调用全部 ret=1，程序不死锁
    N_A = 50
    msg_a = b"Phase10A\n"
    written = wintypes.DWORD(0)
    ok_count = 0
    fail_first_err = 0
    t_start = time.time()
    for _ in range(N_A):
        _kernel32.SetLastError(0)
        r = _kernel32.WriteConsoleA(
            h_out, msg_a, len(msg_a), ctypes.byref(written), None
        )
        if r:
            ok_count += 1
        elif fail_first_err == 0:
            fail_first_err = _kernel32.GetLastError()
    elapsed_ms = int((time.time() - t_start) * 1000)
    write_result(
        f, "write_console_a_batch", 1 if ok_count == N_A else 0, fail_first_err,
        ok=ok_count, total=N_A, elapsed_ms=elapsed_ms,
    )

    # ============================================================
    # 测试 2：WriteFile(CONOUT$) 压测（WriteFile_Detour A→W 复用路径）
    # ============================================================
    # WriteFile_Detour 检测到 Console 句柄 → MultiByteToWideChar
    # → WriteConsoleW_Detour（HookReentryGuard 深度 1→2，合法）
    # 用 GetStdHandle(STD_OUTPUT_HANDLE) 拿到的就是 Console 句柄
    N_WF = 50
    msg_f = b"Phase10File\n"
    ok_count = 0
    fail_first_err = 0
    t_start = time.time()
    for _ in range(N_WF):
        _kernel32.SetLastError(0)
        r = _kernel32.WriteFile(
            h_out, msg_f, len(msg_f), ctypes.byref(written), None
        )
        if r:
            ok_count += 1
        elif fail_first_err == 0:
            fail_first_err = _kernel32.GetLastError()
    elapsed_ms = int((time.time() - t_start) * 1000)
    write_result(
        f, "write_file_console_batch", 1 if ok_count == N_WF else 0, fail_first_err,
        ok=ok_count, total=N_WF, elapsed_ms=elapsed_ms,
    )

    # ============================================================
    # 测试 3：FillConsoleOutputCharacterA（A→W 复用路径）
    # ============================================================
    # FillConsoleOutputCharacterA_Detour → FillConsoleOutputCharacterW_Detour
    # 验证 A→W 复用不死锁，ret=1
    _kernel32.SetLastError(0)
    r = _kernel32.FillConsoleOutputCharacterA(
        h_out, b"X", 10, start_coord, ctypes.byref(written)
    )
    err = _kernel32.GetLastError()
    write_result(
        f, "fill_output_a", r, err, filled=written.value if r else 0,
    )

    # ============================================================
    # 测试 4：混合 A/W 交替调用（验证深度归零）
    # ============================================================
    # 交替调 WriteConsoleA / WriteConsoleW N 次
    # 若 HookReentryGuard 深度未归零，累积后会触发断言或死锁
    # 预期：全部 ret=1，程序不死锁
    N_MIX = 100
    msg_a2 = b"MIX_A\n"
    msg_w2 = "MIX_W\n"
    ok_count = 0
    fail_first_err = 0
    t_start = time.time()
    for i in range(N_MIX):
        _kernel32.SetLastError(0)
        if i % 2 == 0:
            r = _kernel32.WriteConsoleA(
                h_out, msg_a2, len(msg_a2), ctypes.byref(written), None
            )
        else:
            r = _kernel32.WriteConsoleW(
                h_out, msg_w2, len(msg_w2), ctypes.byref(written), None
            )
        if r:
            ok_count += 1
        elif fail_first_err == 0:
            fail_first_err = _kernel32.GetLastError()
    elapsed_ms = int((time.time() - t_start) * 1000)
    write_result(
        f, "mixed_a_w_batch", 1 if ok_count == N_MIX else 0, fail_first_err,
        ok=ok_count, total=N_MIX, elapsed_ms=elapsed_ms,
    )

    # ============================================================
    # 测试 5：Logger worker 重入压测
    # ============================================================
    # 大量 WriteConsoleW 触发 DLL 内 LOG_INFO，Logger worker 线程
    # 异步调 WriteFile(日志文件句柄) → WriteFile_Detour
    # WriteFile_Detour 检测到非 Console 句柄 → pass-through（直接调 _orig）
    # 此路径 HookReentryGuard 计入深度，但 IsConsoleHandle 快速返回 false
    # 不进入 A→W 复用路径，应不死锁
    #
    # 预期：所有调用 ret=1，程序不死锁，时间合理
    N_LOG = 200
    msg_log = "L" * 64 + "\n"  # 较长字符串触发更多日志
    ok_count = 0
    fail_first_err = 0
    t_start = time.time()
    for _ in range(N_LOG):
        _kernel32.SetLastError(0)
        r = _kernel32.WriteConsoleW(
            h_out, msg_log, len(msg_log), ctypes.byref(written), None
        )
        if r:
            ok_count += 1
        elif fail_first_err == 0:
            fail_first_err = _kernel32.GetLastError()
    elapsed_ms = int((time.time() - t_start) * 1000)
    write_result(
        f, "logger_worker_stress", 1 if ok_count == N_LOG else 0, fail_first_err,
        ok=ok_count, total=N_LOG, elapsed_ms=elapsed_ms,
    )

    # ============================================================
    # 测试 6：通过 CreateFileW 打开 CONOUT$ 后 WriteFile
    # ============================================================
    # 验证非 GetStdHandle 路径的 Console 句柄也被 Hook 拦截
    # CreateFileW("CONOUT$") 返回新 Console 句柄，IsConsoleHandle 应识别
    # WriteFile_Detour 拦截后转 WriteConsoleW_Detour
    h_conout = _kernel32.CreateFileW(
        "CONOUT$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None,
    )
    if h_conout == INVALID_HANDLE_VALUE or not h_conout:
        write_result(f, "create_conout", 0, _kernel32.GetLastError())
    else:
        write_result(f, "create_conout", 1, 0, handle="{:#x}".format(int(h_conout)))
        # 用此句柄 WriteFile 输出
        _kernel32.SetLastError(0)
        msg_co = b"Phase10Conout\n"
        r = _kernel32.WriteFile(
            h_conout, msg_co, len(msg_co), ctypes.byref(written), None
        )
        err = _kernel32.GetLastError()
        write_result(f, "write_file_conout_handle", r, err,
                     written=written.value if r else 0)
        _kernel32.CloseHandle(h_conout)

    # 写入完成标记
    f.write("DONE pid={}\n".format(own_pid))
    f.flush()


def main() -> int:
    result_file = os.environ.get("PHASE10_RESULT_FILE", "phase10_result.txt")
    own_pid = _kernel32.GetCurrentProcessId()

    # 启动标记
    with open(result_file, "w", encoding="utf-8") as f:
        f.write("# phase10 hook-reentry result, pid={}\n".format(own_pid))
        f.write("STARTED pid={}\n".format(own_pid))
        f.flush()

        try:
            _run_tests(f, own_pid)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            f.write("EXCEPTION {}\n".format(repr(e)))
            f.write(tb + "\n")
            f.flush()
            return 1

    # 输出完成标记到 Console（mediator 日志能观察到）
    done_msg = "phase10_target_done pid={}\r\n".format(own_pid)
    written = wintypes.DWORD(0)
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    _kernel32.WriteConsoleW(h_out, done_msg, len(done_msg),
                            ctypes.byref(written), None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
