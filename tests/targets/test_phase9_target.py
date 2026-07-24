"""Phase 9 防越狱自检目标程序（运行在被注入的 cmd 中作为 python 子进程）。

调用各项 Console 控制 API，验证 Phase 9 的 ProtectionHooks 是否生效。
DLL 已通过子进程注入（ProcessHooks）注入到本 python 进程，故以下 API
调用都会经过 Detour：
  - AllocConsole        → 应被拦返回 FALSE，ERROR_NOT_ENOUGH_MEMORY
  - AttachConsole       → 应被拦返回 FALSE，ERROR_ACCESS_DENIED
  - FreeConsole         → 应静默成功（返回 TRUE 但不真断）
  - CloseHandle(假句柄) → 应静默返回 TRUE，不真关
  - WriteConsoleW       → FreeConsole 后仍能写入（验证未真断）
  - GetStdHandle        → 多次返回一致
  - GetConsoleWindow    → 原 Console 窗口应被 LazyInit 隐藏（IsWindowVisible=0）

链路：
  runner 启动注入 cmd + WT(mediator) → SendInput 输入 python 命令
  → cmd CreateProcess python（DLL ProcessHooks 拦截 + 注入 DLL 到 python）
  → python 调用 Console API → DLL ProtectionHooks 拦截
  → python 拿到 Hook 后的返回值 → 写结果文件
  → runner 读结果文件验证

结果文件路径由环境变量 PHASE9_RESULT_FILE 指定，默认 ./phase9_result.txt
每行格式：
  TEST <name> ret=<0|1> err=<N> [key=value ...]

依赖：仅 ctypes（Python 3.8+，无需第三方包）
"""
import ctypes
import os
import sys
from ctypes import wintypes

# Console API 常量
ERROR_NOT_ENOUGH_MEMORY = 8
ERROR_ACCESS_DENIED = 5

STD_INPUT_HANDLE = 0xFFFFFFF6
STD_OUTPUT_HANDLE = 0xFFFFFFF5
STD_ERROR_HANDLE = 0xFFFFFFF4

# AttachConsole 专用值：附加到父进程控制台
ATTACH_PARENT_PROCESS = 0xFFFFFFFF

# DLL 内 Alt Buffer sentinel 用的魔数假句柄（见 HandleRegistry.h / BufferHooks.cpp）
# 位 16-31 为 0xABCD，IsFakeHandleFast O(1) 命中
# 注意：必须满足 (h & 0xFFFF0000) == 0xABCD0000
FAKE_HANDLE_MAGIC_EXAMPLE = 0xABCDE123


# ============================================================
# Win32 API 绑定（ctypes）
# ============================================================
_kernel32 = ctypes.windll.kernel32
_kernel32.AllocConsole.argtypes = []
_kernel32.AllocConsole.restype = wintypes.BOOL
_kernel32.AttachConsole.argtypes = [wintypes.DWORD]
_kernel32.AttachConsole.restype = wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD
_kernel32.SetLastError.argtypes = [wintypes.DWORD]
_kernel32.SetLastError.restype = None
_kernel32.GetCurrentProcessId.argtypes = []
_kernel32.GetCurrentProcessId.restype = wintypes.DWORD
# GetCurrentProcess 必须设置 restype=HANDLE，否则默认 int(32位) 返回 -1 被截断为
# 0x00000000FFFFFFFF，导致 64 位 GetModuleInformation 拿到错误的进程伪句柄而失败
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.WriteConsoleW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.WriteConsoleW.restype = wintypes.BOOL

_user32 = ctypes.windll.user32
# GetConsoleWindow 在 kernel32.dll（不在 user32）
_kernel32.GetConsoleWindow.argtypes = []
_kernel32.GetConsoleWindow.restype = wintypes.HWND
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL


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
    """执行所有 Phase 9 验证项，结果写入 f。异常由调用方捕获。"""
    # ---- 1. AllocConsole 应被拦截 ----
    # 预期：ret=0, err=ERROR_NOT_ENOUGH_MEMORY(8)
    _kernel32.SetLastError(0)
    ret = _kernel32.AllocConsole()
    err = _kernel32.GetLastError()
    write_result(f, "alloc_console", ret, err)

    # ---- 2. AttachConsole(parent) 应被拦截 ----
    # 预期：ret=0, err=ERROR_ACCESS_DENIED(5)
    _kernel32.SetLastError(0)
    ret = _kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
    err = _kernel32.GetLastError()
    write_result(f, "attach_console", ret, err)

    # ---- 3. FreeConsole 应静默成功（返回 TRUE 但不真断）----
    # 预期：ret=1, err=0
    _kernel32.SetLastError(0)
    ret = _kernel32.FreeConsole()
    err = _kernel32.GetLastError()
    write_result(f, "free_console", ret, err)

    # ---- 4. FreeConsole 后 WriteConsoleW 应仍可写 ----
    # 验证 FreeConsole 未真断 ConHost，Console 句柄仍有效
    # 预期：ret=1, written=len(msg)
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    msg = "after_free_ok"
    written = wintypes.DWORD(0)
    _kernel32.SetLastError(0)
    write_ok = _kernel32.WriteConsoleW(
        h_out, msg, len(msg), ctypes.byref(written), None
    )
    write_err = _kernel32.GetLastError()
    write_result(f, "write_after_free", write_ok, write_err,
                 written=written.value)

    # ---- 5. 多次 GetStdHandle(STD_OUTPUT_HANDLE) 应返回一致 ----
    # 预期：ret=1, h1==h2==h3 且非 0
    h1 = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    h2 = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    h3 = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    consistent = int(h1 != 0 and h1 == h2 == h3)
    write_result(f, "std_handle_consistent", consistent, 0,
                 h="{:#x}".format(int(h1) if h1 else 0))

    # ---- 6. CloseHandle 假句柄（魔数 0xABCDE123）应静默成功 ----
    # DLL CloseHandle_Detour 用 IsFakeHandleFast 命中魔数，返回 TRUE 不真关
    # 预期：ret=1, err=0

    # 诊断：读取 CloseHandle 函数地址处的首字节，确认 Hook 是否安装
    _kb_mod = ctypes.windll.kernelbase
    _k32_mod = ctypes.windll.kernel32
    kb_addr = ctypes.cast(_kb_mod.CloseHandle, ctypes.c_void_p).value
    k32_addr = ctypes.cast(_k32_mod.CloseHandle, ctypes.c_void_p).value
    _kb_buf = (ctypes.c_ubyte * 8)()
    ctypes.memmove(_kb_buf, kb_addr, 8)
    kb_hex = " ".join("{:02x}".format(b) for b in _kb_buf)
    _k32_buf = (ctypes.c_ubyte * 8)()
    ctypes.memmove(_k32_buf, k32_addr, 8)
    k32_hex = " ".join("{:02x}".format(b) for b in _k32_buf)
    write_result(f, "diag_closehandle_bytes", 1, 0,
                 kb_addr="{:#x}".format(kb_addr), kb_bytes=kb_hex,
                 k32_addr="{:#x}".format(k32_addr), k32_bytes=k32_hex)

    # 诊断：读取 relay（Hook jmp 目标）的内容和间接跳转目标
    # relay = kernelbase!CloseHandle + 5 + disp32（disp32 在 E9 后 4 字节）
    # relay 结构：FF 25 00 00 00 00 <8 bytes target ptr>
    import struct
    disp32 = struct.unpack_from("<i", _kb_buf, 1)[0]  # 从 kb 字节读取 disp32
    relay_addr = kb_addr + 5 + disp32
    relay_buf = (ctypes.c_ubyte * 16)()
    ctypes.memmove(relay_buf, relay_addr, 16)
    relay_hex = " ".join("{:02x}".format(b) for b in relay_buf)
    relay_target = struct.unpack_from("<Q", relay_buf, 6)[0]
    write_result(f, "diag_relay", 1, 0,
                 relay_addr="{:#x}".format(relay_addr),
                 relay_bytes=relay_hex,
                 relay_target="{:#x}".format(relay_target))

    # 诊断：获取 injected.dll 模块基址和大小，判断 relay_target 是否在范围内
    # 若 relay_target 不在 injected.dll 范围内，说明 relay 指向错误地址，
    # CloseHandle_Detour 根本没被调用，这就是 ret=0 err=6 的根因
    import ctypes.wintypes as _wt

    class _MODULEINFO(ctypes.Structure):
        _fields_ = [
            ("lpBaseOfDll", ctypes.c_void_p),
            ("SizeOfImage", _wt.DWORD),
            ("EntryPoint", ctypes.c_void_p),
        ]

    _psapi = ctypes.windll.psapi
    _psapi.GetModuleInformation.argtypes = [
        _wt.HANDLE, _wt.HMODULE, ctypes.POINTER(_MODULEINFO), _wt.DWORD
    ]
    _psapi.GetModuleInformation.restype = _wt.BOOL

    _h_injected = _kernel32.GetModuleHandleW("injected.dll")
    if _h_injected:
        _mi = _MODULEINFO()
        if _psapi.GetModuleInformation(
            _kernel32.GetCurrentProcess(), _h_injected,
            ctypes.byref(_mi), ctypes.sizeof(_mi)
        ):
            _inj_base = _mi.lpBaseOfDll or 0
            _inj_size = _mi.SizeOfImage
            _inj_end = _inj_base + _inj_size
            _relay_in_range = int(_inj_base <= relay_target < _inj_end)
            write_result(f, "diag_injected_module", 1, 0,
                         inj_base="{:#x}".format(_inj_base),
                         inj_size="{:#x}".format(_inj_size),
                         inj_end="{:#x}".format(_inj_end),
                         relay_target="{:#x}".format(relay_target),
                         relay_in_range=_relay_in_range)
        else:
            _kernel32.SetLastError(0)
            write_result(f, "diag_injected_module", 0, _kernel32.GetLastError(),
                         msg="GetModuleInformation failed")
    else:
        write_result(f, "diag_injected_module", 0, 0,
                     msg="injected.dll not loaded")

    # python 位数与可执行路径（诊断注入目标是否匹配 DLL 位数）
    py_bits = struct.calcsize("P") * 8
    write_result(f, "diag_python_info", 1, 0,
                 bits=py_bits, exe=sys.executable)

    fake_h = ctypes.c_void_p(FAKE_HANDLE_MAGIC_EXAMPLE)

    # 诊断：直接调用 relay_target（CloseHandle_Detour），绕过 Hook 链路
    # 如果返回 1，说明 Detour 逻辑正确，问题在 Hook 链路（E9 jmp 没生效）
    # 如果返回 0，说明 Detour 逻辑有问题
    _detour_func = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE)(relay_target)
    _kernel32.SetLastError(0)
    ret_detour = _detour_func(fake_h)
    err_detour = _kernel32.GetLastError()
    write_result(f, "diag_call_detour_direct", ret_detour, err_detour,
                 detour_addr="{:#x}".format(relay_target),
                 fake_h="{:#x}".format(FAKE_HANDLE_MAGIC_EXAMPLE))

    # 诊断：直接通过 kernelbase!CloseHandle 调用（绕过 kernel32 thunk）
    _kb_closehandle = _kb_mod.CloseHandle
    _kb_closehandle.argtypes = [wintypes.HANDLE]
    _kb_closehandle.restype = wintypes.BOOL
    _kernel32.SetLastError(0)
    ret_direct = _kb_closehandle(fake_h)
    err_direct = _kernel32.GetLastError()
    write_result(f, "diag_closehandle_direct_kb", ret_direct, err_direct,
                 fake_h="{:#x}".format(FAKE_HANDLE_MAGIC_EXAMPLE))

    # 原测试：通过 kernel32!CloseHandle 调用（经 thunk → kernelbase）
    _kernel32.SetLastError(0)
    ret = _kernel32.CloseHandle(fake_h)
    err = _kernel32.GetLastError()
    write_result(f, "close_fake_handle", ret, err,
                 fake_h="{:#x}".format(FAKE_HANDLE_MAGIC_EXAMPLE))

    # ---- 7. 原 Console 窗口应被 LazyInit 隐藏 ----
    # LazyInit 末尾 ShowWindow(SW_HIDE)，IsWindowVisible 应返回 0
    # 注意：GetConsoleWindow 不 Hook 仍返回真实 HWND，但窗口已隐藏
    # 预期：ret=1（visible=0 表示已隐藏）
    hwnd = _kernel32.GetConsoleWindow()
    visible = _user32.IsWindowVisible(hwnd) if hwnd else 0
    write_result(f, "console_window_hidden", int(not visible), 0,
                 hwnd="{:#x}".format(int(hwnd) if hwnd else 0),
                 visible=int(visible))

    # 写入完成标记
    f.write("DONE pid={}\n".format(own_pid))
    f.flush()


def main() -> int:
    result_file = os.environ.get("PHASE9_RESULT_FILE", "phase9_result.txt")
    own_pid = _kernel32.GetCurrentProcessId()

    # 先写入启动标记，让 runner 知道 python 进程已进入 main
    # （若仅此行无后续 TEST 行，说明 main 内部异常）
    with open(result_file, "w", encoding="utf-8") as f:
        f.write("# phase9 self-protection result, pid={}\n".format(own_pid))
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
    done_msg = "phase9_target_done pid={}\r\n".format(own_pid)
    written = wintypes.DWORD(0)
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    _kernel32.WriteConsoleW(h_out, done_msg, len(done_msg),
                            ctypes.byref(written), None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
