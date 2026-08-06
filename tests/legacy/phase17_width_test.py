"""Phase 17 字符宽度审计测试脚本。

在注入的 cmd 中运行，验证 AdvanceCursor 对 CJK 双宽字符和 emoji 代理对的正确处理。

测试项：
1. ASCII 单宽字符：写 "Hello"（5 字符），光标应推进 5 列
2. CJK 双宽字符：写 "测试"（2 字符），光标应推进 4 列
3. Emoji 代理对：写 "😀"（U+1F600），光标应推进 2 列
4. 混合宽度：写 "A中😀B"，光标应推进 6 列

结果写入文件 WIDTH_TEST_RESULT 以避免 VT 输出截断导致的检测问题。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

kernel32 = ctypes.windll.kernel32

STD_OUTPUT_HANDLE = -11

# 结果文件路径（与 test runner 约定）
RESULT_FILE = os.environ.get("WIDTH_RESULT_FILE") or os.path.join(paths.out_dir(), "width_test_result.txt")


def write_result(key, value):
    """将测试结果写入文件。"""
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write("{}={}\n".format(key, value))


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


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
    """设置光标位置（走 Hook → VirtualConsoleState）。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    pos = COORD(x, y)
    kernel32.SetConsoleCursorPosition(hOut, pos)


def wchar_len(text):
    """返回字符串的 wchar_t 单位数量（代理对计为 2）。"""
    return len(text.encode('utf-16-le')) // 2


def write_console(text):
    """直接调用 WriteConsoleW 写入文本（走 Hook → AdvanceCursor 推进光标）。
    
    注意：WriteConsoleW 的 nNumberOfCharsToWrite 参数是 wchar_t 单位数量，
    不是 Python 代码点数量。对于 emoji 等 BMP 外字符（代理对），
    长度必须为 2，否则只写入高代理而丢失低代理。
    """
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    written = ctypes.c_uint32(0)
    kernel32.WriteConsoleW(hOut, text, wchar_len(text), ctypes.byref(written), None)
    return written.value


# ============================================================
# 开始测试
# ============================================================
# 清空结果文件
try:
    os.remove(RESULT_FILE)
except OSError:
    pass

print("[WIDTH_TEST] === Phase 17 Width Test Start ===", flush=True)
time.sleep(0.5)

csbi = get_csbi()
if csbi is None:
    print("[WIDTH_TEST] FATAL: GetConsoleScreenBufferInfo failed", flush=True)
    write_result("FATAL", "GetConsoleScreenBufferInfo failed")
    sys.exit(1)

print("[WIDTH_TEST] initial cursor=({},{}) buffer=({},{})".format(
    csbi.dwCursorPosition.X, csbi.dwCursorPosition.Y,
    csbi.dwSize.X, csbi.dwSize.Y), flush=True)

# ============================================================
# 测试 1：ASCII 单宽字符
# 写 "Hello"（5 字符，每字符宽度 1），光标应从 X=0 推进到 X=5
# ============================================================
row = csbi.dwCursorPosition.Y + 1
set_cursor(0, row)
write_console("Hello")          # 5 chars × 1 = 5
csbi = get_csbi()
if csbi is not None and csbi.dwCursorPosition.X == 5 and csbi.dwCursorPosition.Y == row:
    print("[WIDTH_TEST] ASCII_WIDTH: PASS (X={})".format(csbi.dwCursorPosition.X), flush=True)
    write_result("ASCII_WIDTH", "PASS")
else:
    msg = "FAIL (expected X=5,Y={}, got X={},Y={})".format(
        row, csbi.dwCursorPosition.X if csbi else -1,
        csbi.dwCursorPosition.Y if csbi else -1)
    print("[WIDTH_TEST] ASCII_WIDTH: {}".format(msg), flush=True)
    write_result("ASCII_WIDTH", msg)

# ============================================================
# 测试 2：CJK 双宽字符
# 写 "测试"（2 字符，每字符宽度 2），光标应从 X=0 推进到 X=4
# ============================================================
row = csbi.dwCursorPosition.Y + 1
set_cursor(0, row)
write_console("测试")           # 2 chars × 2 = 4
csbi = get_csbi()
if csbi is not None and csbi.dwCursorPosition.X == 4 and csbi.dwCursorPosition.Y == row:
    print("[WIDTH_TEST] CJK_WIDTH: PASS (X={})".format(csbi.dwCursorPosition.X), flush=True)
    write_result("CJK_WIDTH", "PASS")
else:
    msg = "FAIL (expected X=4,Y={}, got X={},Y={})".format(
        row, csbi.dwCursorPosition.X if csbi else -1,
        csbi.dwCursorPosition.Y if csbi else -1)
    print("[WIDTH_TEST] CJK_WIDTH: {}".format(msg), flush=True)
    write_result("CJK_WIDTH", msg)

# ============================================================
# 测试 3：Emoji 代理对
# 写 "😀"（U+1F600，UTF-16 代理对 D83D DE00，宽度 2），光标应从 X=0 推进到 X=2
# ============================================================
row = csbi.dwCursorPosition.Y + 1
set_cursor(0, row)
write_console("😀")             # 1 surrogate pair × 2 = 2
csbi = get_csbi()
if csbi is not None and csbi.dwCursorPosition.X == 2 and csbi.dwCursorPosition.Y == row:
    print("[WIDTH_TEST] EMOJI_WIDTH: PASS (X={})".format(csbi.dwCursorPosition.X), flush=True)
    write_result("EMOJI_WIDTH", "PASS")
else:
    msg = "FAIL (expected X=2,Y={}, got X={},Y={})".format(
        row, csbi.dwCursorPosition.X if csbi else -1,
        csbi.dwCursorPosition.Y if csbi else -1)
    print("[WIDTH_TEST] EMOJI_WIDTH: {}".format(msg), flush=True)
    write_result("EMOJI_WIDTH", msg)

# ============================================================
# 测试 4：混合宽度
# 写 "A中😀B"（1+2+2+1=6），光标应从 X=0 推进到 X=6
# ============================================================
row = csbi.dwCursorPosition.Y + 1
set_cursor(0, row)
write_console("A中😀B")         # 1 + 2 + 2 + 1 = 6
csbi = get_csbi()
if csbi is not None and csbi.dwCursorPosition.X == 6 and csbi.dwCursorPosition.Y == row:
    print("[WIDTH_TEST] MIXED_WIDTH: PASS (X={})".format(csbi.dwCursorPosition.X), flush=True)
    write_result("MIXED_WIDTH", "PASS")
else:
    msg = "FAIL (expected X=6,Y={}, got X={},Y={})".format(
        row, csbi.dwCursorPosition.X if csbi else -1,
        csbi.dwCursorPosition.Y if csbi else -1)
    print("[WIDTH_TEST] MIXED_WIDTH: {}".format(msg), flush=True)
    write_result("MIXED_WIDTH", msg)

# ============================================================
# 完成
# ============================================================
print("[WIDTH_TEST] === Phase 17 Width Test Complete ===", flush=True)
time.sleep(0.5)