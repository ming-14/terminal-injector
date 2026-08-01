"""Phase 18 滚动缓冲区一致性测试脚本。

在注入的 cmd 中运行，验证 AdvanceCursor 对滚动计数的正确跟踪，
以及 SetConsoleScreenBufferSize 后缓冲区高度保留。

测试项：
1. SCROLLBACK_COUNT：输出多行触发换行滚动，验证 bufferSize.Y 包含滚动计数
2. USER_BUFFER_HEIGHT：SetConsoleScreenBufferSize 设置高度后，验证高度被保留
3. MODE_SWITCH_RESET：切换模式后，验证 scrollback 被重置

结果写入文件 SCROLLBACK_RESULT_FILE 以避免 VT 输出截断导致的检测问题。
"""
import ctypes
import os
import sys
import time

kernel32 = ctypes.windll.kernel32

STD_OUTPUT_HANDLE = -11
STD_INPUT_HANDLE = -10

# 常量
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200

# 结果文件路径
RESULT_FILE = os.environ.get(
    "SCROLLBACK_RESULT_FILE",
    "C:\\Users\\rikka\\Desktop\\terminal-injector\\logs\\scrollback_test_result.txt",
)


def write_result(key, value):
    """将测试结果写入文件。"""
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write("{}={}\n".format(key, value))


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


def get_csbi():
    """获取当前 Console 屏幕缓冲区信息（走 Hook → VirtualConsoleState）。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    csbi = CONSOLE_SCREEN_BUFFER_INFO()
    ok = kernel32.GetConsoleScreenBufferInfo(hOut, ctypes.byref(csbi))
    if not ok:
        return None
    return csbi


def set_cursor(x, y):
    """设置光标位置。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    pos = COORD(x, y)
    kernel32.SetConsoleCursorPosition(hOut, pos)


def write_console(text):
    """调用 WriteConsoleW 写入文本。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    written = ctypes.c_uint32(0)
    kernel32.WriteConsoleW(
        hOut, text, len(text), ctypes.byref(written), None
    )
    return written.value


def set_console_buffer_size(cols, rows):
    """调用 SetConsoleScreenBufferSize 设置缓冲区尺寸。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    size = COORD(cols, rows)
    ok = kernel32.SetConsoleScreenBufferSize(hOut, size)
    return ok


def get_console_mode(h_in):
    """获取输入模式。"""
    mode = ctypes.c_uint32(0)
    ok = kernel32.GetConsoleMode(h_in, ctypes.byref(mode))
    if not ok:
        return None
    return mode.value


def set_console_mode(h_in, mode):
    """设置输入模式。"""
    return kernel32.SetConsoleMode(h_in, mode)


# ============================================================
# 开始测试
# ============================================================
# 清空结果文件
try:
    os.remove(RESULT_FILE)
except OSError:
    pass

print("[SCROLLBACK_TEST] === Phase 18 Scrollback Test Start ===", flush=True)
time.sleep(0.5)

csbi = get_csbi()
if csbi is None:
    print("[SCROLLBACK_TEST] FATAL: GetConsoleScreenBufferInfo failed", flush=True)
    write_result("FATAL", "GetConsoleScreenBufferInfo failed")
    sys.exit(1)

# 记录初始状态
init_cols = csbi.dwSize.X
init_rows = csbi.dwSize.Y
init_win_rows = csbi.srWindow.Bottom - csbi.srWindow.Top + 1
print(
    "[SCROLLBACK_TEST] initial buffer=({},{}) window=({} rows)".format(
        init_cols, init_rows, init_win_rows
    ),
    flush=True,
)

# ============================================================
# 测试 1：滚动计数跟踪
# 输出足够多的行数触发换行滚动，然后请求一个很小的缓冲区高度，
# 验证 scrollback 机制阻止了缓冲区缩小（buffer.Y >= 视口高度）
# ============================================================
print("\n[SCROLLBACK_TEST] 测试 1: 滚动计数跟踪", flush=True)

# 先移动到视口最后一行
set_cursor(0, init_win_rows - 1)

# 输出足够多的行触发换行滚动（至少 init_win_rows + 1 行溢出）
for i in range(init_win_rows + 1):
    # 每行写入 cols-1 个字符加回车，确保每行都触发换行
    line = "x" * (init_cols - 1) + "\n"
    write_console(line)

# 稍等让 Hook 处理
time.sleep(0.5)

# 请求一个很小的缓冲区高度（1 行），验证 scrollback 不为 0
# 此时 m_scrollbackLines > 0，SetUserBufferHeight 应确保 buffer.Y >= 视口高度
set_console_buffer_size(init_cols, 1)
time.sleep(0.5)

csbi = get_csbi()
if csbi is not None:
    print(
        "[SCROLLBACK_TEST] after scroll+set size 1: buffer=({},{})".format(
            csbi.dwSize.X, csbi.dwSize.Y
        ),
        flush=True,
    )
    if csbi.dwSize.Y >= init_win_rows:
        print(
            "[SCROLLBACK_TEST] SCROLLBACK_COUNT: PASS (buffer.Y={} >= win_rows={})".format(
                csbi.dwSize.Y, init_win_rows
            ),
            flush=True,
        )
        write_result("SCROLLBACK_COUNT", "PASS")
    else:
        msg = "FAIL (buffer.Y={} < win_rows={}, scrollback not preserved)".format(
            csbi.dwSize.Y, init_win_rows
        )
        print("[SCROLLBACK_TEST] SCROLLBACK_COUNT: {}".format(msg), flush=True)
        write_result("SCROLLBACK_COUNT", msg)
else:
    print("[SCROLLBACK_TEST] SCROLLBACK_COUNT: FAIL (GetConsoleScreenBufferInfo failed)", flush=True)
    write_result("SCROLLBACK_COUNT", "FAIL (GetConsoleScreenBufferInfo failed)")

# ============================================================
# 测试 2：用户缓冲区高度保留
# 调用 SetConsoleScreenBufferSize 设置较大高度，验证高度被保留
# ============================================================
print("\n[SCROLLBACK_TEST] 测试 2: 用户缓冲区高度保留", flush=True)

# 在测试 1 的基础上再设置一个更大的缓冲区高度
# 当前 buffer.Y 已包含滚动计数，再设置一个更大的值
new_height = init_rows + 10
ok = set_console_buffer_size(init_cols, new_height)
if not ok:
    print("[SCROLLBACK_TEST] SetConsoleScreenBufferSize failed", flush=True)
    write_result("USER_BUFFER_HEIGHT", "FAIL (SetConsoleScreenBufferSize failed)")
else:
    time.sleep(0.5)
    csbi = get_csbi()
    if csbi is not None:
        print(
            "[SCROLLBACK_TEST] after SetBufferSize({},{}): buffer=({},{})".format(
                init_cols, new_height, csbi.dwSize.X, csbi.dwSize.Y
            ),
            flush=True,
        )
        if csbi.dwSize.Y >= new_height:
            print(
                "[SCROLLBACK_TEST] USER_BUFFER_HEIGHT: PASS (buffer.Y={} >= requested={})".format(
                    csbi.dwSize.Y, new_height
                ),
                flush=True,
            )
            write_result("USER_BUFFER_HEIGHT", "PASS")
        else:
            msg = "FAIL (buffer.Y={} < requested={})".format(
                csbi.dwSize.Y, new_height
            )
            print("[SCROLLBACK_TEST] USER_BUFFER_HEIGHT: {}".format(msg), flush=True)
            write_result("USER_BUFFER_HEIGHT", msg)
    else:
        print("[SCROLLBACK_TEST] USER_BUFFER_HEIGHT: FAIL (GetConsoleScreenBufferInfo failed)", flush=True)
        write_result("USER_BUFFER_HEIGHT", "FAIL")

# ============================================================
# 测试 3：模式切换重置滚动计数
# 切换输入模式触发 ResetScrollback，验证 scrollback 被重置为 0
# ============================================================
print("\n[SCROLLBACK_TEST] 测试 3: 模式切换重置滚动计数", flush=True)

h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)

# 先获取当前模式
old_mode = get_console_mode(h_in)
if old_mode is None:
    print("[SCROLLBACK_TEST] GetConsoleMode failed", flush=True)
    write_result("MODE_SWITCH_RESET", "FAIL (GetConsoleMode failed)")
else:
    # 先输出一些行产生滚动，然后切换模式
    set_cursor(0, init_win_rows - 1)
    for i in range(init_win_rows + 2):
        line = "y" * (init_cols - 1) + "\n"
        write_console(line)
    time.sleep(0.5)

    # 记录切换前的 buffer.Y
    csbi_before = get_csbi()
    before_y = csbi_before.dwSize.Y if csbi_before else 0
    print(
        "[SCROLLBACK_TEST] before mode switch: buffer.Y={}".format(before_y),
        flush=True,
    )

    # 切换模式（翻转 VT_INPUT 标志）
    new_mode = old_mode ^ ENABLE_VIRTUAL_TERMINAL_INPUT
    ok = set_console_mode(h_in, new_mode)
    if not ok:
        print("[SCROLLBACK_TEST] SetConsoleMode failed", flush=True)
        write_result("MODE_SWITCH_RESET", "FAIL (SetConsoleMode failed)")
    else:
        time.sleep(1.0)
        # 恢复原始模式
        set_console_mode(h_in, old_mode)
        time.sleep(0.5)

        csbi_after = get_csbi()
        if csbi_after is not None:
            after_y = csbi_after.dwSize.Y
            win_rows = (
                csbi_after.srWindow.Bottom - csbi_after.srWindow.Top + 1
            )
            print(
                "[SCROLLBACK_TEST] after mode switch: buffer.Y={} win_rows={}".format(
                    after_y, win_rows
                ),
                flush=True,
            )
            # 模式切换后 scrollback 应重置，buffer.Y 应等于视口高度
            # 注意：不一定完全等于 win_rows，因为可能有微小差异
            if after_y <= win_rows + 1:
                print(
                    "[SCROLLBACK_TEST] MODE_SWITCH_RESET: PASS (buffer.Y={} <= win_rows+1={})".format(
                        after_y, win_rows + 1
                    ),
                    flush=True,
                )
                write_result("MODE_SWITCH_RESET", "PASS")
            else:
                msg = "FAIL (buffer.Y={} > win_rows+1={}, scrollback not reset)".format(
                    after_y, win_rows + 1
                )
                print(
                    "[SCROLLBACK_TEST] MODE_SWITCH_RESET: {}".format(msg),
                    flush=True,
                )
                write_result("MODE_SWITCH_RESET", msg)
        else:
            print(
                "[SCROLLBACK_TEST] MODE_SWITCH_RESET: FAIL (GetConsoleScreenBufferInfo failed)",
                flush=True,
            )
            write_result("MODE_SWITCH_RESET", "FAIL")

# ============================================================
# 完成
# ============================================================
print("\n[SCROLLBACK_TEST] === Phase 18 Scrollback Test Complete ===", flush=True)
time.sleep(0.5)