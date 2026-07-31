"""Phase 13 VT 模式测试脚本。

在注入的 cmd 中运行，执行以下操作：
1. 启用 ENABLE_VIRTUAL_TERMINAL_INPUT（触发 DLL 发 ModeSwitchNotify）
2. 启用 ENABLE_VIRTUAL_TERMINAL_PROCESSING（触发 WriteFile 输出直通）
3. 通过 WriteFile 写 VT 序列（验证 VT 输出直通路径）
4. 打印 marker 字符串供测试验证

注意：本脚本在被注入的 cmd 中运行，其 Console API 调用被 DLL Hook。
"""
import ctypes
import sys
import time

kernel32 = ctypes.windll.kernel32

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

# 1. 获取句柄
hIn = kernel32.GetStdHandle(STD_INPUT_HANDLE)
hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

# 2. 启用 VT 输入模式（触发 DLL ModeSwitchNotify）
mode = ctypes.c_uint32(0)
kernel32.GetConsoleMode(hIn, ctypes.byref(mode))
mode.value |= ENABLE_VIRTUAL_TERMINAL_INPUT
kernel32.SetConsoleMode(hIn, mode.value)
print("[VT_TEST] VT input mode enabled", flush=True)

# 3. 启用 VT 输出处理模式（触发 WriteFile 输出直通）
mode2 = ctypes.c_uint32(0)
kernel32.GetConsoleMode(hOut, ctypes.byref(mode2))
mode2.value |= ENABLE_VIRTUAL_TERMINAL_PROCESSING
kernel32.SetConsoleMode(hOut, mode2.value)
print("[VT_TEST] VT output mode enabled", flush=True)

# 4. 通过 WriteFile 写 VT 序列（验证 VT 输出直通，不经过 ANSI→W→VT 翻译）
vt_data = b'\x1b[31mPhase13_VT_Passthrough\x1b[0m\n'
written = ctypes.c_uint32(0)
kernel32.WriteFile(hOut, vt_data, len(vt_data), ctypes.byref(written), None)
print("[VT_TEST] VT output via WriteFile done, written={}".format(written.value), flush=True)

# 5. 等待 mediator 处理
time.sleep(1.0)
print("[VT_TEST] Phase13 test complete", flush=True)