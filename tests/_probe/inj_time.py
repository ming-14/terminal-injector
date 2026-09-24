# -*- coding: utf-8 -*-
"""测量"注入一针"本身要多久(硬下限)。

LoadLibraryW 的远程线程退出 = DllMain 跑完(MH_Initialize + 全部 Hook 安装),
所以计时 = 完整注入 + Hook 就位耗时。这是"轮询补注入"能否赶上的硬下限。
"""
import ctypes, os, subprocess, sys, time
from ctypes import wintypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from direct_inject import inject, bitness, PROCESS_ALL_ACCESS

DLL = os.path.join(
    os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
    "build", "bin", "Release", "injected.dll")

# 起一个常驻 python.exe(x64), 反复测注入耗时
p = subprocess.Popen(["python.exe", "-c", "import time;time.sleep(60)"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
time.sleep(2)
print("目标 python.exe pid=%d" % p.pid)

for i in range(5):
    t0 = time.perf_counter()
    ok, why = inject(p.pid, DLL)
    dt = (time.perf_counter() - t0) * 1000
    print("  第%d次: %s  用时 %.1f ms  (%s)" % (
        i+1, "成功" if ok else "失败", dt, why))

try:
    p.terminate()
except Exception:
    pass
