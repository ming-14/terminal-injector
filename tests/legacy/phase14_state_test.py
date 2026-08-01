"""Phase 14 虚拟 Console 状态测试脚本。

在注入的 cmd 中运行，执行以下操作：
1. 查询当前 Console 状态（光标位置、缓冲区尺寸、窗口区域）
2. 用 SetConsoleCursorPosition 设置光标位置，再查询验证
3. 写一行文本，查询光标位置是否推进
4. 设置文本属性，查询验证
5. 输出 marker 字符串供测试验证

注意：本脚本在被注入的 cmd 中运行，其 Console API 调用被 DLL Hook，
Get* API 走 VirtualConsoleState 返回，Set* API 更新 VirtualConsoleState。
"""
import ctypes
import sys
import time
import struct

kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11


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


def get_csbi() -> CONSOLE_SCREEN_BUFFER_INFO:
    """获取当前 Console 屏幕缓冲区信息。"""
    hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    csbi = CONSOLE_SCREEN_BUFFER_INFO()
    ok = kernel32.GetConsoleScreenBufferInfo(hOut, ctypes.byref(csbi))
    if not ok:
        print("[STATE_TEST] GetConsoleScreenBufferInfo failed, err={}".format(
            kernel32.GetLastError()), flush=True)
        return None
    return csbi


def print_csbi(label: str, csbi: CONSOLE_SCREEN_BUFFER_INFO):
    """打印 CSBI 状态。"""
    if csbi is None:
        print("[STATE_TEST] {}: None".format(label), flush=True)
        return
    print("[STATE_TEST] {}: cursor=({},{}) size=({},{}) "
          "win=({},{})-({},{}) attr=0x{:04x} max=({},{})".format(
        label,
        csbi.dwCursorPosition.X, csbi.dwCursorPosition.Y,
        csbi.dwSize.X, csbi.dwSize.Y,
        csbi.srWindow.Left, csbi.srWindow.Top,
        csbi.srWindow.Right, csbi.srWindow.Bottom,
        csbi.wAttributes,
        csbi.dwMaximumWindowSize.X, csbi.dwMaximumWindowSize.Y,
    ), flush=True)


# ============================================================
# 测试 1：初始状态查询
# ============================================================
print("[STATE_TEST] === Phase 14 State Test Start ===", flush=True)
time.sleep(0.5)

csbi1 = get_csbi()
print_csbi("initial", csbi1)

# ============================================================
# 测试 2：SetConsoleCursorPosition + 查询验证
# ============================================================
hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
test_pos = COORD(10, 5)
ok = kernel32.SetConsoleCursorPosition(hOut, test_pos)
# 注意：必须先查询光标位置再 print，否则 print 会推进光标
csbi2 = get_csbi()
print("[STATE_TEST] SetConsoleCursorPosition(10,5) -> ok={}".format(ok), flush=True)
print_csbi("after_set_cursor", csbi2)
time.sleep(0.3)

# 验证光标位置
if csbi2 is not None:
    if csbi2.dwCursorPosition.X == 10 and csbi2.dwCursorPosition.Y == 5:
        print("[STATE_TEST] CURSOR_SET: PASS", flush=True)
    else:
        print("[STATE_TEST] CURSOR_SET: FAIL (got {},{} expected 10,5)".format(
            csbi2.dwCursorPosition.X, csbi2.dwCursorPosition.Y), flush=True)

# ============================================================
# 测试 3：WriteConsole 后光标位置推进
# ============================================================
# 写一段文本，验证光标推进
test_text = "HelloPhase14"
written = ctypes.c_uint32(0)
kernel32.WriteConsoleW(hOut, test_text, len(test_text), ctypes.byref(written), None)
print("[STATE_TEST] WriteConsoleW('{}') -> written={}".format(test_text, written.value), flush=True)
time.sleep(0.3)

csbi3 = get_csbi()
print_csbi("after_write", csbi3)

# 光标应在 (10+len("HelloPhase14"), 5) = (10+13, 5) = (23, 5)
# 但注意行末回绕和缓冲区宽度，暂不精确验证，只验证光标已推进
if csbi3 is not None:
    if csbi3.dwCursorPosition.X > 10 or csbi3.dwCursorPosition.Y > 5:
        print("[STATE_TEST] CURSOR_ADVANCE: PASS", flush=True)
    else:
        print("[STATE_TEST] CURSOR_ADVANCE: FAIL (cursor didn't advance: {}, {})".format(
            csbi3.dwCursorPosition.X, csbi3.dwCursorPosition.Y), flush=True)

# ============================================================
# 测试 4：SetConsoleTextAttribute + 查询验证
# ============================================================
# 设置属性为红色前景+蓝色背景
test_attr = 0x1C  # 红色前景(4) | 蓝色背景(1<<4) | 亮色(8)
ok = kernel32.SetConsoleTextAttribute(hOut, test_attr)
print("[STATE_TEST] SetConsoleTextAttribute(0x{:04x}) -> ok={}".format(test_attr, ok), flush=True)
time.sleep(0.3)

csbi4 = get_csbi()
print_csbi("after_set_attr", csbi4)

if csbi4 is not None:
    if csbi4.wAttributes == test_attr:
        print("[STATE_TEST] ATTR_SET: PASS", flush=True)
    else:
        print("[STATE_TEST] ATTR_SET: FAIL (got 0x{:04x} expected 0x{:04x})".format(
            csbi4.wAttributes, test_attr), flush=True)

# 恢复默认属性
kernel32.SetConsoleTextAttribute(hOut, 0x07)

# ============================================================
# 测试 5：缓冲区大小查询
# ============================================================
if csbi1 is not None:
    print("[STATE_TEST] BUFFER_SIZE: ({},{})".format(
        csbi1.dwSize.X, csbi1.dwSize.Y), flush=True)
    # 缓冲区应在合理范围（>0 且 < 10000）
    if csbi1.dwSize.X > 0 and csbi1.dwSize.Y > 0 and csbi1.dwSize.Y < 10000:
        print("[STATE_TEST] BUFFER_SIZE: PASS", flush=True)
    else:
        print("[STATE_TEST] BUFFER_SIZE: FAIL (unreasonable size)", flush=True)

# ============================================================
# 完成
# ============================================================
print("[STATE_TEST] === Phase 14 State Test Complete ===", flush=True)
time.sleep(0.5)