# -*- coding: utf-8 -*-
"""测量 py.exe -> python.exe 的时间窗, 判断"轮询式补注入"是否来得及。
同时测: 从 python.exe 出现到它产出第一行输出, 有多久(可用的注入窗口)。
"""
import os, subprocess, sys, time
import psutil

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hello.py")

p = subprocess.Popen(["py.exe", SCRIPT], creationflags=subprocess.CREATE_NEW_CONSOLE)
t0 = time.perf_counter()

py_seen = None
pyth_seen = None
deadline = t0 + 15
while time.perf_counter() < deadline:
    try:
        for c in psutil.Process(p.pid).children(recursive=True):
            n = c.name().lower()
            if n == "py.exe" and py_seen is None:
                py_seen = time.perf_counter() - t0
            if n == "python.exe" and pyth_seen is None:
                pyth_seen = time.perf_counter() - t0
    except psutil.NoSuchProcess:
        pass
    if py_seen is not None and pyth_seen is not None:
        break
    time.sleep(0.002)

print("py.exe      首次出现: {} ms".format(
    "N/A" if py_seen is None else "%.1f" % (py_seen*1000)))
print("python.exe  首次出现: {} ms".format(
    "N/A" if pyth_seen is None else "%.1f" % (pyth_seen*1000)))
if py_seen is not None and pyth_seen is not None:
    print("=> py.exe 存活窗口: {:.1f} ms".format((pyth_seen-py_seen)*1000))

tb = time.perf_counter()
try:
    psutil.Process(p.pid).terminate()
except Exception:
    pass
time.sleep(0.5)
for c in psutil.process_iter(["pid","name","cmdline"]):
    try:
        cl = " ".join(c.info["cmdline"] or [])
        if "hello.py" in cl:
            psutil.Process(c.info["pid"]).terminate()
    except Exception:
        pass
