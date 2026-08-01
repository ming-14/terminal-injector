"""Phase 10 任务6 WriteConsoleOutput diff 算法专项测试目标程序。

验证 diff 缓存正确性：
  1. 第1次 WriteConsoleOutputW：写 5x5 矩阵全 'A'（全量输出，缓存初始化）
  2. 第2次：只改 cell(0,0) 为 'B'（diff 应只输出 1 cell）
  3. 第3次：恢复 cell(0,0) 为 'A'（diff 应只输出 1 cell）
  4. 第4次：全改为 'C'（diff 输出 25 cell，但仍走 diff 路径）
  5. 第5次：FillConsoleOutputCharacterW 失效缓存后，再写全 'A'（全量输出）

每次调用间隔 50ms，确保 BatchSender（16ms flush）各自独立成包，
runner 可从 mediator 日志中按顺序提取每次的 VtOutput 字节数。

结果文件路径由环境变量 PHASE10_DIFF_RESULT_FILE 指定，默认 ./phase10_diff_result.txt
每行格式：TEST <name> ret=<0|1> err=<N>

依赖：仅 ctypes（Python 3.8+）
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

# ============================================================
# Win32 API 常量
# ============================================================
STD_OUTPUT_HANDLE = 0xFFFFFFF5
DEFAULT_ATTR = 0x0007  # 灰底黑字（WT 默认）

# ============================================================
# Win32 结构体
# ============================================================
class CHAR_INFO(ctypes.Structure):
    _fields_ = [
        ("Char", ctypes.c_wchar),
        ("Attributes", wintypes.WORD),
    ]


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


# ============================================================
# Win32 API 绑定
# ============================================================
_kernel32 = ctypes.windll.kernel32
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD
_kernel32.SetLastError.argtypes = [wintypes.DWORD]
_kernel32.SetLastError.restype = None

_kernel32.WriteConsoleOutputW.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(CHAR_INFO),
    COORD,  # bufferSize
    COORD,  # bufferCoord
    ctypes.POINTER(SMALL_RECT),  # writeRegion
]
_kernel32.WriteConsoleOutputW.restype = wintypes.BOOL

_kernel32.FillConsoleOutputCharacterW.argtypes = [
    wintypes.HANDLE, wintypes.WCHAR, wintypes.DWORD,
    wintypes.DWORD,  # COORD 打包为 DWORD
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.FillConsoleOutputCharacterW.restype = wintypes.BOOL

# SetConsoleCursorPosition：用于把光标移到矩阵外，避免 print("DONE") 覆盖矩阵
# WriteConsoleOutputW 不改光标位置（Windows 真实行为），但 ConsoleState 的光标
# 位置取决于 cmd/python banner 输出后的位置（可能落在矩阵区域 (0,0,4,4) 内，
# 如 Y=4），导致 print("DONE") 写到矩阵第4行覆盖 'A'。显式设置光标到 (0, 10)
# 确保 print("DONE") 写到矩阵之外。
_kernel32.SetConsoleCursorPosition.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,  # COORD 打包为 DWORD
]
_kernel32.SetConsoleCursorPosition.restype = wintypes.BOOL


def _coord_to_dword(x: int, y: int) -> int:
    """COORD 打包为 DWORD（低 16 位 X，高 16 位 Y）。"""
    return (y << 16) | (x & 0xFFFF)


def write_result(f, name: str, ret: int, err: int, **extra) -> None:
    parts = ["TEST", name, "ret={}".format(int(bool(ret))), "err={}".format(err)]
    for k, v in extra.items():
        parts.append("{}={}".format(k, v))
    f.write(" ".join(parts) + "\n")
    f.flush()


def _make_buffer(rows: int, cols: int, char: str, attr: int = DEFAULT_ATTR) -> list:
    """构造 rows×cols 的 CHAR_INFO 数组，所有 cell 填同字符。"""
    return [CHAR_INFO(char, attr) for _ in range(rows * cols)]


def _make_buffer_with_override(rows: int, cols: int, default_char: str,
                                overrides: dict, attr: int = DEFAULT_ATTR) -> list:
    """构造 rows×cols 的 CHAR_INFO 数组，overrides 指定 (row,col)->char。"""
    buf = [CHAR_INFO(default_char, attr) for _ in range(rows * cols)]
    for (r, c), ch in overrides.items():
        buf[r * cols + c] = CHAR_INFO(ch, attr)
    return buf


def _call_write_console_output(h_out, cells, rows, cols, region):
    """调 WriteConsoleOutputW，返回 (ret, err)。"""
    buf_type = CHAR_INFO * len(cells)
    buf = buf_type(*cells)
    bufferSize = COORD(cols, rows)
    bufferCoord = COORD(0, 0)
    writeRegion = SMALL_RECT(region[0], region[1], region[2], region[3])
    _kernel32.SetLastError(0)
    r = _kernel32.WriteConsoleOutputW(
        h_out, buf, bufferSize, bufferCoord, ctypes.byref(writeRegion)
    )
    err = _kernel32.GetLastError() if not r else 0
    return r, err


def _run_tests(f) -> None:
    # 写入本进程 pid，runner 据此定位 C:\temp\injected_<pid>.log 验证 diff 日志
    f.write("PID {}\n".format(os.getpid()))
    f.flush()

    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    rows, cols = 5, 5
    region = (0, 0, cols - 1, rows - 1)  # Left, Top, Right, Bottom

    # 把光标移到矩阵外（第10行），避免后续 print("DONE") 覆盖矩阵
    # 根因：WriteConsoleOutputW 不改 ConsoleState 光标缓存（Windows 真实行为），
    # 而 cmd/python banner 输出后 ConsoleState.dwCursorPosition.Y 可能落在矩阵
    # 区域 (0,0,4,4) 内（如 Y=4），导致 print("DONE") 从 (X, 4) 开始写，
    # 覆盖矩阵第4行前4个 'A' 形成行4='DONEA'。
    # 显式设置光标到 (0, 10)，让 savedCursor=(0, 10)，WriteConsoleOutputW 恢复
    # ConPTY 光标到 (0, 10)，print("DONE") 写到第10行不覆盖矩阵。
    _kernel32.SetConsoleCursorPosition(h_out, _coord_to_dword(0, 10))

    # ============================================================
    # 测试 1：全量输出（缓存初始化）—— 25 个 'A'
    # ============================================================
    cells = _make_buffer(rows, cols, 'A')
    r, err = _call_write_console_output(h_out, cells, rows, cols, region)
    write_result(f, "diff_full_init", 1 if r else 0, err)
    time.sleep(0.05)  # 等 BatchSender flush

    # ============================================================
    # 测试 2：diff 输出（只改 cell(0,0) 为 'B'）
    # ============================================================
    cells = _make_buffer_with_override(rows, cols, 'A', {(0, 0): 'B'})
    r, err = _call_write_console_output(h_out, cells, rows, cols, region)
    write_result(f, "diff_single_change", 1 if r else 0, err)
    time.sleep(0.05)

    # ============================================================
    # 测试 3：diff 输出（恢复 cell(0,0) 为 'A'）
    # ============================================================
    cells = _make_buffer(rows, cols, 'A')
    r, err = _call_write_console_output(h_out, cells, rows, cols, region)
    write_result(f, "diff_revert", 1 if r else 0, err)
    time.sleep(0.05)

    # ============================================================
    # 测试 4：diff 输出（全改为 'C'，25 cell 都变）
    # ============================================================
    cells = _make_buffer(rows, cols, 'C')
    r, err = _call_write_console_output(h_out, cells, rows, cols, region)
    write_result(f, "diff_all_change", 1 if r else 0, err)
    time.sleep(0.05)

    # ============================================================
    # 测试 5：FillConsoleOutputCharacterW 失效缓存后再全量
    # ============================================================
    # FillConsoleOutputCharacterW 会调 InvalidateOutputCache
    # 之后 WriteConsoleOutputW 应走全量路径（缓存已失效）
    written = wintypes.DWORD(0)
    _kernel32.FillConsoleOutputCharacterW(
        h_out, ' ', rows * cols,
        _coord_to_dword(region[0], region[1]),
        ctypes.byref(written),
    )
    time.sleep(0.05)

    cells = _make_buffer(rows, cols, 'A')
    r, err = _call_write_console_output(h_out, cells, rows, cols, region)
    write_result(f, "diff_after_invalidate", 1 if r else 0, err)
    time.sleep(0.05)


def main() -> int:
    result_file = os.environ.get(
        "PHASE10_DIFF_RESULT_FILE",
        os.path.join(os.getcwd(), "phase10_diff_result.txt"),
    )
    try:
        with open(result_file, "w", encoding="utf-8") as f:
            _run_tests(f)
        print("DONE")
    except Exception as e:
        try:
            with open(result_file, "w", encoding="utf-8") as f:
                f.write("EXCEPTION: {}\n".format(e))
        except OSError:
            pass
        print("EXCEPTION: {}".format(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
