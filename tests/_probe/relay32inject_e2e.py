# -*- coding: utf-8 -*-
"""relay32inject_e2e.py -- 验证 32 位引导注入器 + relay32.dll 的完整落地链路

链路（全部为可判定事实）：
  [1] relay32.dll / relay32inject.exe 确实是 32 位产物
  [2] 目标以 CREATE_SUSPENDED 启动时模块表为空（loader 未初始化 —— 前置条件）
  [3] relay32inject.exe 能向这个【挂起】的 32 位进程注入 relay32.dll，退出码 0
  [4] 注入后目标模块表出现 relay32.dll（顺带证明远程线程触发了 loader 初始化）
  [5] relay32.dll 的 RemotePipeSetup 收到管道参数（证明导出名/参数下发都对）
  [6] relay32.dll 连上假 mediator 并发出 RelayHello（type=0x63，pid/bitness 正确）

为什么 [5][6] 必须验：
  relay32.dll 的 RemotePipeSetup 在 x86 下曾被导出为 _RemotePipeSetup@4，
  而注入方按裸名 GetProcAddress —— 不验就会得到"注入成功但中继永远不连管道"
  的静默失败。

Python 在这里扮演真实实现里的 mediator（只建管道 + 收 RelayHello）。

用法: python relay32inject_e2e.py
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
sys.path.insert(0, HERE)
import pe_info  # noqa: E402  （复用 PE 解析，判位数）

P32 = os.path.join(HERE, "probe32")
TARGET32 = os.path.join(P32, "target32.exe")
RELEASE = os.path.join(
    os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
    "build", "bin", "Release")
RELAY32 = os.path.join(RELEASE, "relay32.dll")
INJECT32 = os.path.join(RELEASE, "relay32inject.exe")
LOGDIR = os.path.join(RELEASE, "logs")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.c_void_p]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.ResumeThread.argtypes = [wintypes.HANDLE]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

CREATE_SUSPENDED = 0x00000004
MAGIC = 0x544A494E
VER = 1
T_RELAY_HELLO = 0x0063
LIST_MODULES_32BIT = 0x01

results = []
stats = {}


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


def enum_modules_32bit(pid):
    """返回 (包含的模块名小写列表, 模块数, err)"""
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return [], 0, ctypes.get_last_error()
    try:
        need = wintypes.DWORD(0)
        k32.SetLastError(0)
        psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), LIST_MODULES_32BIT)
        err = ctypes.get_last_error()
        if not need.value:
            return [], 0, err
        buf = ctypes.create_string_buffer(need.value)
        if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need),
                                          LIST_MODULES_32BIT):
            return [], 0, ctypes.get_last_error()
        out = []
        step = ctypes.sizeof(ctypes.c_void_p)
        for i in range(need.value // step):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            nm = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
                out.append(os.path.basename(nm.value).lower())
        return out, len(out), 0
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
            if e.winerror in (109, 232, 233):
                return None
            raise
        if not chunk:
            return None
        buf += chunk
    return buf


def server_main(name, ready, stop, stats):
    """假 mediator: 建管道 -> 等连接 -> 读首帧（应为 RelayHello）"""
    h = win32pipe.CreateNamedPipe(
        name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 0, None)
    ready.set()
    try:
        win32pipe.ConnectNamedPipe(h, None)
        stats["connected_at"] = time.time()
        hdr = read_exact(h, 16, time.time() + 20)
        if hdr is None:
            stats["err"] = "首帧超时"
            return
        magic, ver, _res, mtype, length = struct.unpack("<IHHII", hdr)
        stats["first_frame_at"] = time.time()
        stats["magic"] = magic
        stats["ver"] = ver
        stats["type"] = mtype
        stats["length"] = length
        if length:
            payload = read_exact(h, length, time.time() + 5)
            stats["payload"] = payload
        # 保持连接直到被测方收尾
        while not stop.is_set():
            time.sleep(0.05)
    except pywintypes.error as e:
        stats["err"] = str(e)
    finally:
        try:
            win32file.CloseHandle(h)
        except Exception:
            pass


def main():
    print("=== [1] 产物位数 ===")
    for p in (RELAY32, INJECT32, TARGET32):
        info = pe_info.parse(p) if os.path.exists(p) else None
        mach = info["machine"] if info else "缺失"
        print("  %-18s %s" % (os.path.basename(p), mach))
    check("relay32.dll 是 32 位", os.path.exists(RELAY32) and
          pe_info.parse(RELAY32)["machine"].startswith("I386"))
    check("relay32inject.exe 是 32 位", os.path.exists(INJECT32) and
          pe_info.parse(INJECT32)["machine"].startswith("I386"))

    mypid = os.getpid()
    name = r"\\.\pipe\terminjector_relay_probe_%d_%X" % (
        mypid, int(time.time() * 1000) & 0xFFFFFF)
    ready = threading.Event()
    stop = threading.Event()
    th = threading.Thread(target=server_main, args=(name, ready, stop, stats),
                          daemon=True)
    th.start()
    ready.wait(5)
    print("\n假 mediator 管道: %s (本进程 pid=%d)" % (name, mypid))

    print("\n=== [2] 以 CREATE_SUSPENDED 启动 32 位目标 ===")
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    if not k32.CreateProcessW(ctypes.c_wchar_p(TARGET32), None, None, None, False,
                              CREATE_SUSPENDED, None, ctypes.c_wchar_p(P32),
                              ctypes.byref(si), ctypes.byref(pi)):
        print("  CreateProcessW 失败 err=%d" % ctypes.get_last_error())
        return 1
    pid = pi.dwProcessId
    mods, n, err = enum_modules_32bit(pid)
    print("  目标 pid=%d 挂起态模块数=%d err=%d" % (pid, n, err))
    check("挂起态模块表为空（loader 未初始化）", n == 0, "err=%d" % err)

    print("\n=== [3] 运行 relay32inject.exe 注入挂起目标 ===")
    before = set(os.listdir(LOGDIR)) if os.path.isdir(LOGDIR) else set()
    t0 = time.time()
    cp = subprocess.run([INJECT32, "--child", str(pid), "--dll", RELAY32,
                         "--pipe", name, "--mediator-pid", str(mypid)],
                        capture_output=True, text=True, timeout=60)
    dt = (time.time() - t0) * 1000
    out = ((cp.stdout or "") + (cp.stderr or "")).strip()
    print("  退出码=%d 耗时=%.0f ms" % (cp.returncode, dt))
    if out:
        print("  输出: " + out.replace("\n", "\n        "))
    check("relay32inject.exe 退出码为 0", cp.returncode == 0, "rc=%d" % cp.returncode)

    print("\n=== [4] 目标模块表 ===")
    mods2, n2, err2 = enum_modules_32bit(pid)
    has_relay = any("relay32" in m for m in mods2)
    print("  注入后模块数=%d err=%d" % (n2, err2))
    for m in sorted(mods2):
        print("    %s" % m)
    check("注入后模块表含 relay32.dll", has_relay)
    check("注入后模块表非空（远程线程触发了 loader 初始化）", n2 > 0)

    print("\n=== [5][6] relay32 是否把参数收下并连上 mediator ===")
    ok, _ = (False, None)
    t1 = time.time()
    while time.time() - t1 < 20:
        if "first_frame_at" in stats or stats.get("err"):
            break
        time.sleep(0.05)
    if "connected_at" in stats:
        print("  管道被连接 t+%.0f ms（相对注入启动）"
              % ((stats["connected_at"] - t0) * 1000))
    if stats.get("err"):
        print("  服务端错误: %s" % stats["err"])
    check("relay32.dll 连上了管道（说明 RemotePipeSetup 收到参数）",
          "connected_at" in stats)
    check("首帧类型为 RelayHello(0x63)", stats.get("type") == T_RELAY_HELLO,
          "type=0x%X" % stats.get("type", 0))
    if stats.get("payload") and len(stats["payload"]) >= 8:
        hpid, hbits = struct.unpack("<II", stats["payload"][:8])
        print("  RelayHello payload: pid=%d bitness=%d" % (hpid, hbits))
        check("RelayHello.pid == 目标 pid", hpid == pid, "%d vs %d" % (hpid, pid))
        check("RelayHello.bitness == 32", hbits == 32, str(hbits))
    else:
        check("RelayHello payload 至少 8 字节", False, str(stats.get("payload")))

    print("\n=== 清理 ===")
    k32.ResumeThread(pi.hThread)
    time.sleep(0.2)
    k32.TerminateProcess(pi.hProcess, 0)
    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)
    stop.set()

    after = set(os.listdir(LOGDIR)) if os.path.isdir(LOGDIR) else set()
    newl = sorted(after - before)
    if newl:
        print("\n=== 本次新增日志 (%s) ===" % newl)
        for f in newl:
            p = os.path.join(LOGDIR, f)
            if os.path.getsize(p) == 0:
                continue
            print("  --- %s ---" % f)
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh.read().splitlines():
                    print("    " + line[:170])

    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok in results if ok)
    for nm, ok in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", nm))
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
