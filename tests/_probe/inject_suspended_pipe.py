# -*- coding: utf-8 -*-
"""inject_suspended_pipe.py -- 判定"挂起子进程需要被冻结多久"

背景:
  inject_suspended.py 实测 --inject 对挂起进程要 5582ms。日志定位到耗时全在
  RemoteCallExport 上, 而 DLL 侧同一时刻报 "NamedPipe client connect timeout"。
  即: 5.5s = 【没有管道服务端】时 transport->Connect() 吃满超时; 期间
  g_initialized 仍为 false, RemoteCallExport 的远程线程进 detour 后被
  `while(!g_initialized) Sleep(1)` 自旋卡住 -> 整个注入被拖住。

本探针把管道服务端补上, 复现真实 mediator 的形态, 再测一次:
  - 若连接成功后注入很快 -> "挂起等注入" 的成本是可接受的 (几十 ms)
  - 若依然 5s        -> 挂起方案不可接受, 必须换策略

做法: Python 进程自己当管道服务端 (mediator 替身), 完成 Hello/HelloAck 握手,
      同时用 --mediator-pid 传入本 Python 进程 PID, 让 DLL 的服务端身份校验通过。
"""
import ctypes
import os
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import pywintypes
import win32file
import win32pipe

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
GC64 = os.path.join(P32, "gc64.exe")
BIN = os.path.join(os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", "..")), "build", "bin", "Release")
EXE = os.path.join(BIN, "terminal_injector.exe")
INJ = os.path.join(BIN, "injected.dll")
LOGDIR = os.path.join(BIN, "logs")
GCDONE = os.path.join(P32, "gc_done.txt")
VERDICT = os.path.join(P32, "gc_verdict.txt")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.c_void_p]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

CREATE_SUSPENDED = 0x00000004
MAGIC = 0x544A494E
VER = 1
T_HELLO = 0x0001
T_HELLOACK = 0x0002
results = []
timeline = []


def mark(msg):
    timeline.append((time.time(), msg))
    print("  t+%7.1f ms  %s" % ((time.time() - T0) * 1000, msg))


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail else ""))


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def enum_modules(pid):
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return [], -1
    try:
        need = wintypes.DWORD(0)
        r = psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), 0x02)
        err = ctypes.get_last_error()
        if not r or not need.value:
            return [], err
        buf = ctypes.create_string_buffer(need.value)
        psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), 0x02)
        out = []
        step = ctypes.sizeof(ctypes.c_void_p)
        for i in range(need.value // step):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            nm = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
                out.append(os.path.basename(nm.value).lower())
        return out, 0
    finally:
        k32.CloseHandle(h)


def read_exact(f, n, deadline):
    buf = b""
    while len(buf) < n:
        if time.time() > deadline:
            return None
        try:
            _, chunk = win32file.ReadFile(f, n - len(buf))
        except pywintypes.error as e:
            if e.winerror in (109, 232, 233):  # broken pipe / closing
                return None
            raise
        if not chunk:
            return None
        buf += chunk
    return buf


def server_main(name, ready, stop, stats):
    """假 mediator: 建管道 -> 等连接 -> 收 Hello -> 回 HelloAck -> 继续读"""
    h = win32pipe.CreateNamedPipe(
        name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 0, None)
    ready.set()
    try:
        win32pipe.ConnectNamedPipe(h, None)
        stats["connected_at"] = time.time()
        deadline = time.time() + 15
        hdr = read_exact(h, 16, deadline)
        if hdr is None:
            return
        magic, ver, _res, mtype, length = struct.unpack("<IHHII", hdr)
        stats["hello_at"] = time.time()
        stats["hello_type"] = mtype
        if length:
            read_exact(h, length, deadline)
        # HelloAck: 12 字节 payload
        ack = struct.pack("<HHHHHH", 120, 30, 0, 0, 0, 0)
        pkt = struct.pack("<IHHII", MAGIC, VER, 0, T_HELLOACK, len(ack)) + ack
        win32file.WriteFile(h, pkt)
        stats["ack_sent_at"] = time.time()
        # 之后持续读, 记录首个 VtOutput 时刻
        while not stop.is_set():
            h2 = read_exact(h, 16, time.time() + 0.5)
            if h2 is None:
                continue
            _m, _v, _r, mt, ln = struct.unpack("<IHHII", h2)
            if ln:
                read_exact(h, ln, time.time() + 5)
            if mt == 0x0010 and "first_vt_at" not in stats:
                stats["first_vt_at"] = time.time()
    except pywintypes.error as e:
        stats["server_err"] = str(e)
    finally:
        try:
            win32file.CloseHandle(h)
        except Exception:
            pass


def main():
    global T0
    for p in (GCDONE, VERDICT):
        try:
            os.remove(p)
        except OSError:
            pass

    mypid = os.getpid()
    name = r"\\.\pipe\terminjector_test_%d_%X" % (mypid, int(time.time() * 1000) & 0xFFFFFF)

    ready = threading.Event()
    stop = threading.Event()
    stats = {}
    th = threading.Thread(target=server_main, args=(name, ready, stop, stats),
                          daemon=True)
    th.start()
    ready.wait(5)
    print("假 mediator 已建管道: %s" % name)
    print("本进程 pid = %d (作为 --mediator-pid 传入, 供 DLL 服务端身份校验)" % mypid)

    print("\n=== 以 CREATE_SUSPENDED 启动 64 位目标 ===")
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    if not k32.CreateProcessW(ctypes.c_wchar_p(GC64), None, None, None, False,
                              CREATE_SUSPENDED, None, ctypes.c_wchar_p(P32),
                              ctypes.byref(si), ctypes.byref(pi)):
        print("CreateProcessW 失败 err=%d" % ctypes.get_last_error())
        return 1
    pid = pi.dwProcessId
    mods, err = enum_modules(pid)
    print("  目标 pid = %d, 挂起态模块数 = %d err = %d" % (pid, len(mods), err))
    check("目标处于挂起态(模块表为空)", len(mods) == 0, "err=%d" % err)

    before = set(os.listdir(LOGDIR)) if os.path.isdir(LOGDIR) else set()

    print("\n=== 对挂起目标跑 --inject (管道服务端已就绪) ===")
    T0 = time.time()
    cp = subprocess.run([EXE, "--inject", str(pid), "--dll", INJ,
                         "--pipe", name, "--mediator-pid", str(mypid)],
                        capture_output=True, timeout=60)
    dt = (time.time() - T0) * 1000
    out = (cp.stdout.decode("utf-8", "replace") +
           cp.stderr.decode("utf-8", "replace")).strip()
    print("  退出码 = %d, 总耗时 %.0f ms" % (cp.returncode, dt))
    if out:
        print("  输出: " + out.replace("\n", "\n        "))

    check("--inject 退出码为 0", cp.returncode == 0, "rc=%d" % cp.returncode)
    if "connected_at" in stats:
        mark("管道被连接 (DLL 连上假 mediator)")
    if "hello_at" in stats:
        mark("收到 Hello (type=0x%X)" % stats.get("hello_type", 0))
    if "ack_sent_at" in stats:
        mark("已回 HelloAck")

    mods2, _ = enum_modules(pid)
    has = any("injected.dll" in m for m in mods2)
    print("  注入后模块数 = %d, 含 injected.dll = %s" % (len(mods2), has))
    check("目标模块表含 injected.dll", has)

    print("\n=== 恢复目标 ===")
    k32.ResumeThread(pi.hThread)
    for _ in range(100):
        if os.path.exists(GCDONE):
            break
        time.sleep(0.1)
    check("恢复后目标正常跑完", os.path.exists(GCDONE))
    if "first_vt_at" in stats:
        mark("收到首个 VtOutput (目标输出已进 mediator)")

    # 关键判定
    print("\n=== 关键判定 ===")
    check("注入耗时远低于 5s (说明 5.5s 是无服务端时的连接超时, 非加载成本)",
          dt < 3000, "%.0f ms" % dt)

    # 读 DLL 侧日志
    after = set(os.listdir(LOGDIR)) if os.path.isdir(LOGDIR) else set()
    newl = sorted(after - before)
    if newl:
        print("\n=== DLL 侧日志 (%s) 关键行 ===" % newl[0])
        txt = open(os.path.join(LOGDIR, newl[0]), encoding="utf-8",
                   errors="replace").read()
        for line in txt.splitlines():
            if any(k in line for k in ("LazyInit starting", "using injected pipe",
                                      "pipe params not received", "connect timeout",
                                      "server identity", "Hello sent", "HelloAck",
                                      "LazyInit done", "LazyInit: ConnectToMediator")):
                print("  " + line[:170])

    stop.set()
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)

    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok in results if ok)
    for name_, ok in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name_))
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


T0 = time.time()
if __name__ == "__main__":
    sys.exit(main())
