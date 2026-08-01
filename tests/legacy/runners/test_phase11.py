"""Phase 11 卸载清理测试。

验证项（对应 docs/phases/11-unload-testing.md 5.4 卸载干净度）：
  1. DLL 从目标进程模块列表消失（卸载完成）
  2. 原 cmd 窗口恢复可交互（echo 命令有响应）
  3. Hook 字节已恢复为原 API 字节（CloseHandle 首字节非 E9）
  4. 反复注入/卸载 10 次无内存/句柄泄漏

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 关闭 WT 窗口触发卸载（mediator stdin EOF → 发 Shutdown 给 DLL
    → DLL Unloader::RequestUnload → FreeLibraryAndExitThread）
  - 在测试进程中用 EnumProcessModules 验证 DLL 模块消失
  - 用 OpenProcess + ReadProcessMemory 读 CloseHandle 首字节
  - 用 SendInput 给 cmd 发命令 + AttachConsole + ReadConsoleOutputW 验证响应
  - 循环 10 次记录内存/句柄数比较

链路：
  runner 启动 cmd + WT(mediator) + 注入
  → 关闭 WT 窗口 → mediator stdin EOF → 发 Shutdown 给 DLL
  → DLL Unloader::RequestUnload → HookManager::UninstallAll
  → FreeLibraryAndExitThread → DLL 模块从 cmd 进程消失
  → runner 验证各项验收点
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

import psutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim

# 项目根目录（与 injector.PROJECT_ROOT 一致）
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Win32 常量
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
HOOK_JMP_BYTE = 0xE9  # MinHook x64 jmp disp32 首字节
STD_OUTPUT_HANDLE = 0xFFFFFFF5
STD_INPUT_HANDLE = 0xFFFFFFF6
SW_RESTORE = 9


# ============================================================
# CHAR_INFO 结构（ReadConsoleOutputW 用，wintypes 未提供）
# CONSOLE_SCREEN_BUFFER_INFO 同样未在 wintypes 中导出，需自定义
# COORD/SMALL_RECT 复用 wintypes._COORD / wintypes.SMALL_RECT
# ============================================================
class CHAR_INFO(ctypes.Structure):
    _fields_ = [
        ("UnicodeChar", wintypes.WCHAR),
        ("Attributes", wintypes.WORD),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes._COORD),
        ("dwCursorPosition", wintypes._COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", wintypes.SMALL_RECT),
        ("dwMaximumWindowSize", wintypes._COORD),
    ]


class MODULEINFO(ctypes.Structure):
    _fields_ = [
        ("lpBaseOfDll", ctypes.c_void_p),
        ("SizeOfImage", wintypes.DWORD),
        ("EntryPoint", ctypes.c_void_p),
    ]


# ============================================================
# Win32 API 绑定（统一在 ctypes.windll 共享实例上设置 argtypes）
# ============================================================
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetCurrentProcessId.argtypes = []
_kernel32.GetCurrentProcessId.restype = wintypes.DWORD
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.AttachConsole.argtypes = [wintypes.DWORD]
_kernel32.AttachConsole.restype = wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = wintypes.BOOL
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)
]
_kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
# 注意：ReadConsoleOutputW 第三参数 dwBufferSize 是 COORD 类型（X=列数, Y=行数），
# 不是 DWORD！若误用 DWORD 传 buf 元素总数（如 24000=0x5DC0）会被解释为
# COORD(X=24000, Y=0)，函数认为 buffer 行数为 0 → err=1 ERROR_INVALID_FUNCTION
_kernel32.ReadConsoleOutputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(CHAR_INFO), wintypes._COORD,
    wintypes._COORD, ctypes.POINTER(wintypes.SMALL_RECT)
]
_kernel32.ReadConsoleOutputW.restype = wintypes.BOOL
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD
_kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
_kernel32.ReadProcessMemory.restype = wintypes.BOOL

_psapi = ctypes.windll.psapi
_psapi.EnumProcessModules.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)
]
_psapi.EnumProcessModules.restype = wintypes.BOOL
_psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD
]
_psapi.GetModuleFileNameExW.restype = wintypes.DWORD

_user32 = ctypes.windll.user32
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL


# ============================================================
# 进程枚举与内存读取辅助
# ============================================================
def enum_process_modules(pid: int):
    """枚举进程加载的所有模块，返回 [(hmodule, full_path)] 列表。

    hmodule 即模块在目标进程中的基址（HMODULE == base address）。
    """
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return []
    try:
        cbNeeded = wintypes.DWORD(0)
        # 第一次调用获取所需字节数
        if not _psapi.EnumProcessModules(hProc, None, 0, ctypes.byref(cbNeeded)):
            return []
        count = cbNeeded.value // ctypes.sizeof(wintypes.HMODULE)
        hMods = (wintypes.HMODULE * count)()
        if not _psapi.EnumProcessModules(
                hProc, hMods, cbNeeded.value, ctypes.byref(cbNeeded)):
            return []
        result = []
        name_buf = ctypes.create_unicode_buffer(260)
        for i in range(count):
            if _psapi.GetModuleFileNameExW(hProc, hMods[i], name_buf, 260) > 0:
                # hMods[i] 是 c_void_p，int() 取其数值（模块基址）
                result.append((int(hMods[i]) or 0, name_buf.value))
        return result
    finally:
        _kernel32.CloseHandle(hProc)


def find_module_by_name(pid: int, name_lower: str):
    """查找指定进程中的模块，返回 (hmodule, full_path) 或 None。"""
    for hmod, path in enum_process_modules(pid):
        if name_lower in path.lower():
            return hmod, path
    return None


def read_process_memory(pid: int, address: int, size: int) -> bytes:
    """读取指定进程的内存。失败返回空 bytes。"""
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return b""
    try:
        buf = (ctypes.c_ubyte * size)()
        bytesRead = ctypes.c_size_t(0)
        ok = _kernel32.ReadProcessMemory(
            hProc, ctypes.c_void_p(address), buf, size,
            ctypes.byref(bytesRead))
        if not ok:
            return b""
        return bytes(buf[:bytesRead.value])
    finally:
        _kernel32.CloseHandle(hProc)


def find_console_window_by_pid(pid: int) -> int:
    """查找指定 pid 进程的 Console 窗口句柄（类名 ConsoleWindowClass）。"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        class_buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, class_buf, 256)
        if class_buf.value != "ConsoleWindowClass":
            return True
        proc_pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))
        if proc_pid.value == pid:
            found.append(hwnd)
        return True

    _user32.EnumWindows(callback, 0)
    return found[0] if found else 0


def _close_wt_window() -> bool:
    """关闭测试启动的 WT 窗口（触发卸载），返回是否成功 PostMessage。

    通过 injector 模块记录的 _test_wt_hwnd 精确定位测试窗口，
    避免误关其他 WT 窗口。PostMessage(WM_CLOSE) 异步通知 WT 退出。
    """
    try:
        import win32gui
        import win32con
    except ImportError:
        return False
    hwnd = injector._test_wt_hwnd
    if hwnd is None:
        return False
    if not win32gui.IsWindow(hwnd):
        return False
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    # 重置状态，避免后续 cleanup 重复关闭
    injector._test_wt_hwnd = None
    return True


# ============================================================
# 卸载触发
# ============================================================
def _cmd_alive(cmd_pid: int) -> bool:
    """检查 cmd 进程是否还活着（psutil 比 OpenProcess 更可靠）。"""
    try:
        return psutil.Process(cmd_pid).is_running()
    except psutil.NoSuchProcess:
        return False


def _cmd_exitcode(cmd_pid: int):
    """获取 cmd 进程退出码（进程仍存活返回 None，已退出返回退出码）。"""
    try:
        p = psutil.Process(cmd_pid)
        if p.is_running():
            return None
        return p.returncode()
    except psutil.NoSuchProcess:
        # 进程已退出且被 OS 回收，无法获取退出码
        return "exited(code unavailable)"
    except Exception as e:
        return "error({})".format(e)


def _print_cmd_status(cmd_pid: int, tag: str) -> None:
    """打印 cmd 进程状态用于诊断（验收点前后调用）。"""
    try:
        p = psutil.Process(cmd_pid)
        alive = p.is_running()
        status = p.status()
        mem = p.memory_info().rss
        handles = p.num_handles()
        print("  [diag:{}] cmd pid={} alive={} status={} rss={:,} handles={}".format(
            tag, cmd_pid, alive, status, mem, handles))
    except psutil.NoSuchProcess:
        # 进程已退出，尝试获取退出码
        code = _cmd_exitcode(cmd_pid)
        print("  [diag:{}] cmd pid={} DEAD exitcode={}".format(tag, cmd_pid, code))
    except Exception as e:
        print("  [diag:{}] cmd pid={} check error: {}".format(tag, cmd_pid, e))


def trigger_unload(cmd_pid: int, mediator_proc: subprocess.Popen,
                   timeout: float = 20.0) -> bool:
    """触发卸载：关闭 WT 窗口 → mediator 退出 → 管道断开 → DLL Unloader。

    链路：
      1. PostMessage(WM_CLOSE) 通知 WT 窗口关闭
      2. WT 退出 → mediator 的 stdin 读到 EOF / 被 WT 连带终止
      3. DLL 检测 pipe 断开调 Unloader::RequestUnload
      4. Unloader 在独立线程执行 DoUnload（HookManager::UninstallAll 等）
         末尾启动助手进程 terminal_injector.exe --unload-remote
      5. 助手进程 Sleep(2s) 等 DoUnload + Logger 线程退出 → 远程 FreeLibrary
         → LoadCount 归 0 触发 DETACH → 必要时触发 LDR flush
      6. DLL 模块从 cmd 进程消失

    timeout=20s：助手进程 Sleep(2s) + FreeLibrary(5s) + LDR flush(5s) + 重试余量

    返回 True 表示 DLL 已从 cmd 进程卸载且 cmd 仍存活；
    返回 False 表示卸载失败 或 cmd 已退出（trigger 误判成功）。
    """
    print("  [unload] 关闭 WT 窗口...")
    _close_wt_window()
    _print_cmd_status(cmd_pid, "after-close-wt")

    # 等 mediator 进程退出（WT 关闭后 mediator 跟随退出）
    try:
        mediator_proc.wait(timeout=5.0)
        print("  [unload] mediator 已退出")
    except subprocess.TimeoutExpired:
        print("  [unload] mediator 超时未退出，kill")
        mediator_proc.kill()
        try:
            mediator_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
    _print_cmd_status(cmd_pid, "after-mediator-exit")

    # 等 DLL 卸载完成（Unloader 在独立线程执行，需等 FreeLibraryAndExitThread）
    print("  [unload] 等待 DLL 卸载...")
    start = time.time()
    deadline = start + timeout
    last_alive_check = 0.0
    last_module_check_time = start
    while time.time() < deadline:
        now = time.time()
        # 周期性检查 cmd 是否还活着（高频，0.3s 一次，尽早捕获退出码）
        if now - last_alive_check > 0.3:
            if not _cmd_alive(cmd_pid):
                code = _cmd_exitcode(cmd_pid)
                print("  [unload] cmd 进程在卸载等待期间退出！elapsed={:.1f}s exitcode={}".format(
                    now - start, code))
                return False
            last_alive_check = now
        # 较低频检查 DLL 模块（避免 OpenProcess 开销）
        if now - last_module_check_time > 0.5:
            if find_module_by_name(cmd_pid, "injected.dll") is None:
                # 关键诊断：find_module_by_name 返回 None 可能是
                #   (a) injected.dll 真的卸载了
                #   (b) cmd 进程已退出（OpenProcess 失败也返回 []）
                # 用 psutil 区分两种情况
                if not _cmd_alive(cmd_pid):
                    code = _cmd_exitcode(cmd_pid)
                    print("  [unload] cmd 进程已退出！find_module_by_name 返回 None 是误判（OpenProcess 失败）"
                          " exitcode={}".format(code))
                    return False
                print("  [unload] DLL 已卸载（耗时 {:.1f}s）".format(now - start))
                _print_cmd_status(cmd_pid, "after-dll-unloaded")
                return True
            last_module_check_time = now
        time.sleep(0.1)
    return False


# ============================================================
# 验收点验证函数
# ============================================================
def test_dll_unloaded(cmd_pid: int) -> bool:
    """验收点 1：injected.dll 已从 cmd 进程模块列表消失。"""
    print("\n[验收 1] DLL 模块列表检查")
    mod = find_module_by_name(cmd_pid, "injected.dll")
    if mod is None:
        print("  [PASS] injected.dll 已从 cmd 进程卸载")
        return True
    print("  [FAIL] injected.dll 仍在 cmd 进程模块列表中: {}".format(mod[1]))
    return False


def test_cmd_responsive(cmd_pid: int, marker: str) -> bool:
    """验收点 2：原 cmd 窗口恢复可交互。

    向 cmd 发送 `echo <marker>` 命令，通过 AttachConsole + ReadConsoleOutputW
    读取 cmd 控制台 buffer，验证 marker 出现在输出中。

    链路：
      SetForegroundWindow(cmd_hwnd) → SendInput 输入 echo 命令
      → cmd 处理并 WriteConsoleW 输出（Hook 已卸载，直接写原 Console）
      → AttachConsole(cmd_pid) + ReadConsoleOutputW 读 buffer 验证
    """
    print("\n[验收 2] cmd 恢复可交互（echo 命令响应）")
    hwnd = find_console_window_by_pid(cmd_pid)
    if not hwnd:
        print("  [FAIL] 未找到 cmd 的 Console 窗口")
        return False
    print("  [info] cmd Console 窗口 hwnd={:#x}".format(hwnd))

    # 聚焦 cmd 窗口（SendInput 需要前台窗口）
    _user32.ShowWindow(hwnd, SW_RESTORE)
    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    # 输入命令
    cmd_line = "echo {}".format(marker)
    print("  [info] 输入命令: {}".format(cmd_line))
    input_sim.type_text(cmd_line)
    time.sleep(0.3)
    input_sim.type_enter()
    time.sleep(1.0)  # 等 cmd 处理并输出

    # 读取 cmd 控制台内容
    # 先 FreeConsole 当前进程（若有），再 AttachConsole 到 cmd
    _kernel32.FreeConsole()
    try:
        if not _kernel32.AttachConsole(cmd_pid):
            err = _kernel32.GetLastError()
            print("  [FAIL] AttachConsole 失败 err={}".format(err))
            return False

        # AttachConsole 后 GetStdHandle 返回的句柄可能不是 cmd console 的有效句柄
        # (Windows 已知行为：AttachConsole 不总是更新 STD_OUTPUT_HANDLE)
        # 用 CreateFileW("CONOUT$") 重新打开 cmd console 的 output handle
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        OPEN_EXISTING = 3
        hOut = _kernel32.CreateFileW(
            wintypes.LPCWSTR("CONOUT$"),
            wintypes.DWORD(GENERIC_READ | GENERIC_WRITE),
            wintypes.DWORD(FILE_SHARE_READ | FILE_SHARE_WRITE),
            None,
            wintypes.DWORD(OPEN_EXISTING),
            0, None)
        print("  [info] CreateFileW(CONOUT$)={:#x}".format(hOut if hOut else 0))
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not _kernel32.GetConsoleScreenBufferInfo(hOut, ctypes.byref(info)):
            err = _kernel32.GetLastError()
            print("  [FAIL] GetConsoleScreenBufferInfo 失败 err={}".format(err))
            _kernel32.CloseHandle(hOut)
            return False

        print("  [info] console buf=({},{}) cursor=({},{}) win=({},{},{},{})".format(
            info.dwSize.X, info.dwSize.Y,
            info.dwCursorPosition.X, info.dwCursorPosition.Y,
            info.srWindow.Left, info.srWindow.Top,
            info.srWindow.Right, info.srWindow.Bottom))

        cols = info.dwSize.X
        rows = info.dwSize.Y
        # 限制读取行数避免大 buffer 拖慢测试（cmd 默认 buf 高度可达 9001）
        max_rows = min(rows, 200)

        # buf 数组：max_rows * cols 个 CHAR_INFO
        buf_cells = max_rows * cols
        buf = (CHAR_INFO * buf_cells)()
        # buf_coord：buf 数组的左上角坐标（缓冲区内部坐标）
        buf_coord = wintypes._COORD(0, 0)
        # buf_size_coord：传给 ReadConsoleOutputW 的 dwBufferSize（COORD！）
        # X=列数, Y=行数。Win32 API 期望 COORD 而非元素总数
        buf_size_coord = wintypes._COORD(cols, max_rows)
        # read_region：要读取的控制台屏幕缓冲区区域
        # 注意：必须完全在 srWindow 内？不，必须在屏幕缓冲区（dwSize）内即可
        read_region = wintypes.SMALL_RECT(0, 0, cols - 1, max_rows - 1)

        if not _kernel32.ReadConsoleOutputW(
                hOut, buf, buf_size_coord, buf_coord, ctypes.byref(read_region)):
            err = _kernel32.GetLastError()
            print("  [FAIL] ReadConsoleOutputW 失败 err={}".format(err))
            _kernel32.CloseHandle(hOut)
            return False

        # 提取所有字符
        text = "".join(buf[i].UnicodeChar for i in range(buf_cells))
        _kernel32.CloseHandle(hOut)
        if marker in text:
            print("  [PASS] cmd 输出中找到 marker {!r}".format(marker))
            return True
        print("  [FAIL] cmd 输出中未找到 marker {!r}".format(marker))
        # 打印末尾非空行帮助调试
        lines = [text[i * cols:(i + 1) * cols].rstrip() for i in range(max_rows)]
        last_lines = [l for l in lines if l][-10:]
        for i, line in enumerate(last_lines):
            print("    tail {}: {}".format(i, line))
        return False
    finally:
        # 必须释放，否则后续测试无法再次 Attach
        _kernel32.FreeConsole()


def test_hook_bytes_restored(cmd_pid: int) -> bool:
    """验收点 3：Hook 字节恢复为原 API 字节。

    检查 cmd 进程中 kernelbase!CloseHandle 首字节，应为非 E9（jmp disp32）。
    MinHook 安装 Hook 时把首字节改为 E9 + disp32，卸载后恢复原字节。

    系统级 ASLR 下 kernelbase.dll 在所有进程中基址相同，但为严谨起见
    仍通过 EnumProcessModules 获取 cmd 进程的 kernelbase 基址。
    """
    print("\n[验收 3] Hook 字节检查（CloseHandle 首字节）")

    # 在测试进程中计算 CloseHandle 在 kernelbase 中的偏移
    test_kbase = ctypes.WinDLL("kernelbase.dll")
    test_base = test_kbase._handle  # 模块句柄即基址
    test_close_addr = ctypes.cast(
        test_kbase.CloseHandle, ctypes.c_void_p).value or 0
    offset = test_close_addr - test_base
    print("  [info] kernelbase 基址(测试)={:#x} CloseHandle={:#x} 偏移={:#x}".format(
        test_base, test_close_addr, offset))

    # 找 cmd 进程中 kernelbase 的基址（HMODULE 即基址）
    cmd_mod = find_module_by_name(cmd_pid, "kernelbase.dll")
    if cmd_mod is None:
        print("  [FAIL] 未找到 cmd 进程的 kernelbase.dll")
        return False
    cmd_kb_base, _ = cmd_mod
    cmd_close_addr = cmd_kb_base + offset
    print("  [info] kernelbase 基址(cmd)={:#x} CloseHandle={:#x}".format(
        cmd_kb_base, cmd_close_addr))

    # 读 cmd 进程中 CloseHandle 字节
    bytes_ = read_process_memory(cmd_pid, cmd_close_addr, 16)
    if not bytes_:
        print("  [FAIL] 无法读取 cmd 进程的 CloseHandle 字节")
        return False
    hex_str = " ".join("{:02x}".format(b) for b in bytes_)
    print("  [info] CloseHandle bytes: {}".format(hex_str))

    if bytes_[0] == HOOK_JMP_BYTE:
        print("  [FAIL] 首字节仍是 E9，Hook 未卸载")
        return False
    print("  [PASS] 首字节非 E9，Hook 已卸载")
    return True


def test_repeat_no_leak(cycles: int = 10) -> bool:
    """验收点 4：反复注入/卸载 N 次无内存/句柄泄漏。

    流程：
      - 启动 cmd 一次
      - 循环 N 次：注入 → 记录内存/句柄 → 卸载 → 记录内存/句柄
      - 比较第 1 次和第 N 次的卸载后值，内存增长 < 2MB，句柄增长 ±5
    """
    print("\n[验收 4] 反复注入/卸载 {} 次无泄漏".format(cycles))

    print("  [setup] 启动 cmd...")
    cmd_pid = injector.start_target_cmd()
    print("  [setup] cmd PID={}".format(cmd_pid))

    try:
        records = []
        for i in range(cycles):
            print("\n  --- 循环 {}/{} ---".format(i + 1, cycles))
            injector.clear_log()
            mediator_proc = injector.start_wt_mediator(cmd_pid)
            if not injector.wait_for_handshake(timeout=15.0):
                print("  [FAIL] 循环 {} 握手失败".format(i + 1))
                return False
            print("  [info] 循环 {} 握手成功".format(i + 1))
            time.sleep(0.5)

            # 记录注入后内存/句柄
            try:
                p = psutil.Process(cmd_pid)
                mem_before = p.memory_info().rss
                handles_before = p.num_handles()
            except psutil.NoSuchProcess:
                print("  [FAIL] cmd 进程消失")
                return False

            # 触发卸载
            if not trigger_unload(cmd_pid, mediator_proc, timeout=20.0):
                print("  [FAIL] 循环 {} DLL 卸载超时".format(i + 1))
                return False

            # 记录卸载后内存/句柄
            try:
                p = psutil.Process(cmd_pid)
                mem_after = p.memory_info().rss
                handles_after = p.num_handles()
            except psutil.NoSuchProcess:
                print("  [FAIL] cmd 进程消失")
                return False

            print("  [info] mem: before={:,} after={:,} "
                  "handles: before={} after={}".format(
                      mem_before, mem_after, handles_before, handles_after))
            records.append((i + 1, mem_before, mem_after,
                            handles_before, handles_after))

        # 比较第 1 次和最后一次的卸载后值
        first = records[0]
        last = records[-1]
        mem_growth = last[2] - first[2]
        handle_growth = last[4] - first[4]
        print("\n  [汇总] 第 1 次卸载后: mem={:,} handles={}".format(
            first[2], first[4]))
        print("  [汇总] 第 {} 次卸载后: mem={:,} handles={}".format(
            last[0], last[2], last[4]))
        print("  [汇总] 内存增长 {:+,} 字节，句柄增长 {:+}".format(
            mem_growth, handle_growth))

        # 容差：内存 2MB（含碎片波动），句柄 ±5
        mem_ok = mem_growth < 2 * 1024 * 1024
        handle_ok = -5 <= handle_growth <= 5

        if mem_ok and handle_ok:
            print("  [PASS] 内存和句柄无显著泄漏")
            return True
        if not mem_ok:
            print("  [FAIL] 内存泄漏：增长 {:+,} 字节（容差 2MB）".format(
                mem_growth))
        if not handle_ok:
            print("  [FAIL] 句柄泄漏：增长 {}（容差 ±5）".format(handle_growth))
        return False
    finally:
        # 清理 cmd 进程及其子进程
        try:
            p = psutil.Process(cmd_pid)
            for child in p.children(recursive=True):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            p.terminate()
            p.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass
        # 清理可能残留的 terminal_injector 进程
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"] or ""
                if name.lower() == "terminal_injector.exe":
                    proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass


def _cleanup_cmd(cmd_pid: int) -> None:
    """清理 cmd 进程及其子进程（不依赖 injector.cleanup 避免误杀 WT）。

    子进程可能是 conhost 或权限提升的进程（如 Administrator 启动的 cmd
    父进程是 explorer），terminate 会抛 AccessDenied，需单独捕获避免
    影响整体 teardown。
    """
    try:
        p = psutil.Process(cmd_pid)
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass
    # 清理可能残留的 terminal_injector 进程
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info["name"] or ""
            if name.lower() == "terminal_injector.exe":
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def run() -> int:
    """主测试入口：单次卸载验证 + 反复注入/卸载验证。"""
    failures = 0

    # === 单次卸载测试 ===
    print("=" * 60)
    print("Phase 11 单次卸载测试")
    print("=" * 60)

    print("[setup] 启动 cmd...")
    cmd_pid = injector.start_target_cmd()
    print("[setup] cmd PID={}".format(cmd_pid))

    injector.clear_log()
    print("[setup] 启动 WT + mediator...")
    mediator_proc = injector.start_wt_mediator(cmd_pid)
    print("[setup] 等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[FATAL] 握手失败")
        injector.cleanup(cmd_pid, mediator_proc)
        return 1
    print("[setup] 握手成功")
    time.sleep(1.0)

    # 触发卸载
    print("\n[unload] 触发卸载（关闭 WT 窗口）...")
    if not trigger_unload(cmd_pid, mediator_proc, timeout=20.0):
        print("[FAIL] DLL 卸载超时 或 cmd 已退出")
        failures += 1

    # 验收点 1: DLL 模块消失
    _print_cmd_status(cmd_pid, "before-验收1")
    if not test_dll_unloaded(cmd_pid):
        failures += 1

    # 验收点 2: cmd 恢复可交互
    _print_cmd_status(cmd_pid, "before-验收2")
    marker = "phase11_marker_{}".format(int(time.time()) % 100000)
    if not test_cmd_responsive(cmd_pid, marker):
        failures += 1

    # 验收点 3: Hook 字节恢复
    _print_cmd_status(cmd_pid, "before-验收3")
    if not test_hook_bytes_restored(cmd_pid):
        failures += 1

    # 清理 cmd
    print("\n[teardown] 终止 cmd...")
    _cleanup_cmd(cmd_pid)
    time.sleep(1.0)

    # === 反复注入/卸载测试 ===
    print("\n" + "=" * 60)
    print("Phase 11 反复注入/卸载测试")
    print("=" * 60)

    if not test_repeat_no_leak(cycles=10):
        failures += 1

    # === 汇总 ===
    print("\n" + "=" * 60)
    print("Phase 11 测试汇总")
    print("=" * 60)
    if failures == 0:
        print("全部通过")
    else:
        print("{} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
