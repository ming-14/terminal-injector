# -*- coding: utf-8 -*-
"""测量 python.exe 从进程创建到产出首行输出的宽限期(slack)。

slack = 首行输出时刻 - 进程创建时刻。
它决定"轮询式补注入"是否来得及: 若 slack 远大于(发现延迟+注入耗时), 才有戏。
"""
import os, subprocess, sys, time
import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "firstout.py")
STAMP = os.path.join(HERE, "_t0.txt")

TRIALS = 5
for trial in range(TRIALS):
    try:
        os.remove(STAMP)
    except OSError:
        pass
    p = subprocess.Popen(["py.exe", SCRIPT], creationflags=subprocess.CREATE_NEW_CONSOLE)

    py_pid = None
    deadline = time.time() + 10
    while time.time() < deadline and py_pid is None:
        for c in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cl = " ".join(c.info["cmdline"] or [])
                if "firstout.py" in cl and c.info["name"].lower() == "python.exe":
                    py_pid = c.info["pid"]
                    break
            except Exception:
                pass
        time.sleep(0.001)

    if py_pid is None:
        print("trial %d: 未捕获 python.exe" % trial); continue
    proc = psutil.Process(py_pid)
    t_create = proc.create_time()

    t_out = None
    d2 = time.time() + 10
    while time.time() < d2 and t_out is None:
        try:
            with open(STAMP, "r") as f:
                t_out = float(f.read())
        except (OSError, ValueError):
            time.sleep(0.0005)

    if t_out is None:
        print("trial %d: 未读到首行输出时间戳" % trial)
    else:
        print("trial %d: python.exe 创建→首行输出 = %.1f ms" % (trial, (t_out - t_create) * 1000))

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        p.terminate()
    except Exception:
        pass
    time.sleep(0.6)
