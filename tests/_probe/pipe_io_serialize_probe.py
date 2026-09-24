# -*- coding: utf-8 -*-
"""pipe_io_serialize_probe.py —— 隔离验证「同步命名管道上挂起的 ReadFile 会阻塞同一句柄的 WriteFile」

背景
----
relay32.dll 的接收循环（ConnectWorkerProc）用**阻塞** RecvPacket(ReadFile) 常驻等待，
之后同一句柄上由 detour 线程发起 Send(WriteFile) 去送 RelayChildNotify —— 实测该
WriteFile 卡住 6.9s 直到 mediator 进程被杀、管道断开才以 err=232 返回。

本探针只回答一个问题，**不涉及任何项目源码**：
    同一个同步命名管道句柄上，一个挂起未完成的 ReadFile，
    会不会把同一句柄上的 WriteFile 堵住？改用 Peek 轮询后是否就不堵？

对照设计（唯一变量 = 接收侧是否挂起阻塞 ReadFile）
  A 组：接收线程挂起阻塞 ReadFile(1B)，主线程 WriteFile(12B)
        → 判据 1: WriteFile 在 1s 观察窗内不返回（被堵）
        → 判据 2: 服务端写入 1B 满足那次 ReadFile 后，WriteFile 随即返回（堵因解除）
  B 组：接收线程改为 PeekNamedPipe 轮询（不挂 ReadFile），主线程 WriteFile(12B)
        → 判据 3: WriteFile 立即返回（<100ms）
        → 判据 4: 服务端能读到 12B 且内容一致（写确实生效）

A 判据全中 + B 判据全中 ⇒ 「阻塞 RecvPacket 占住管道 I/O，使同一句柄的 Send 自阻塞」
成立，且「Peek 轮询」是可用的修法方向。

    python pipe_io_serialize_probe.py
"""
import ctypes
import sys
import threading
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PIPE_ACCESS_DUPLEX = 0x00000003
# PIPE_TYPE_BYTE(0) | PIPE_READMODE_BYTE(0) | PIPE_WAIT(0) → dwPipeMode = 0
# 与被测代码 CreateNamedPipeW 的实际参数一致（NamedPipeTransport::Create）
PIPE_MODE = 0x00000000
PIPE_UNLIMITED_INSTANCES = 255
BUF_SIZE = 65536
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

k32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.c_void_p]
k32.CreateNamedPipeW.restype = wintypes.HANDLE
k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
k32.ConnectNamedPipe.restype = wintypes.BOOL
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
k32.CreateFileW.restype = wintypes.HANDLE
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.ReadFile.restype = wintypes.BOOL
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wintypes.BOOL
k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD),
                              ctypes.POINTER(wintypes.DWORD),
                              ctypes.POINTER(wintypes.DWORD)]
k32.PeekNamedPipe.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL


def _make_pipe(tag):
    name = r"\\.\pipe\ti_pipe_serialize_{}_{}".format(tag, int(time.time() * 1000) % 1000000)
    h = k32.CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX, PIPE_MODE,
                             PIPE_UNLIMITED_INSTANCES, BUF_SIZE, BUF_SIZE, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError("CreateNamedPipeW failed err={}".format(ctypes.get_last_error()))
    srv = {"name": name, "h": h, "connected": False}
    threading.Thread(target=lambda: (
        k32.ConnectNamedPipe(h, None),
        srv.__setitem__("connected", True)), daemon=True).start()
    hc = INVALID_HANDLE_VALUE
    deadline = time.time() + 5
    while time.time() < deadline:
        hc = k32.CreateFileW(name, 0x80000000 | 0x40000000, 0, None, 3, 0, None)
        if hc != INVALID_HANDLE_VALUE:
            break
        time.sleep(0.02)
    if hc == INVALID_HANDLE_VALUE:
        raise OSError("client CreateFileW failed err={}".format(ctypes.get_last_error()))
    return srv, hc


def _write_async(h, data, result):
    """在后台线程发 WriteFile，把 (是否完成, 耗时ms, 错误码, 写入字节数) 记到 result。"""
    def run():
        buf = ctypes.create_string_buffer(data, len(data))
        n = wintypes.DWORD(0)
        t0 = time.time()
        ok = k32.WriteFile(h, buf, len(data), ctypes.byref(n), None)
        err = 0 if ok else ctypes.get_last_error()
        result["done"] = True
        result["ms"] = (time.time() - t0) * 1000.0
        result["err"] = err
        result["written"] = n.value
    threading.Thread(target=run, daemon=True).start()


def case_a():
    """接收侧挂起阻塞 ReadFile —— 观察同句柄 WriteFile 是否被堵。"""
    print("\n" + "=" * 70)
    print("[A 组] 接收线程挂起阻塞 ReadFile(1B)，主线程对同句柄 WriteFile(12B)")
    print("=" * 70)
    srv, hc = _make_pipe("a")
    h_srv = srv["h"]
    fails = 0

    # 接收线程：阻塞 ReadFile（模拟 relay32 的阻塞 RecvPacket）
    read_state = {"done": False}
    stop = threading.Event()

    def blocking_reader():
        buf = ctypes.create_string_buffer(1)
        n = wintypes.DWORD(0)
        k32.ReadFile(hc, buf, 1, ctypes.byref(n), None)
        read_state["done"] = True
        print("   [接收线程] 挂起的 ReadFile 返回了（被满足），读到 {}B".format(n.value))

    threading.Thread(target=blocking_reader, daemon=True).start()
    time.sleep(0.3)  # 让它挂上去

    res = {"done": False, "ms": None, "err": None, "written": 0}
    _write_async(hc, b"RELAY_NOTIFY", res)
    time.sleep(1.0)
    if res["done"]:
        print("   ✗ 判据1 未中: WriteFile 在挂起 ReadFile 存在时就返回了"
              "（{:.1f}ms) —— 说明不存在 I/O 串行化".format(res["ms"]))
        fails += 1
    else:
        print("   ✓ 判据1 命中: WriteFile 1s 内未返回 —— 同一句柄的写被挂起的读堵住")

    # 服务端写 1B，满足接收线程那次挂起的 ReadFile
    nb = wintypes.DWORD(0)
    sbuf = ctypes.create_string_buffer(b"X", 1)
    k32.WriteFile(h_srv, sbuf, 1, ctypes.byref(nb), None)
    for _ in range(50):
        if res["done"] and read_state["done"]:
            break
        time.sleep(0.05)
    if res["done"] and read_state["done"]:
        print("   ✓ 判据2 命中: 挂起的读被满足后，WriteFile 随即返回"
              "（阻塞累计 {:.1f}ms, err={}, written={}）".format(res["ms"], res["err"], res["written"]))
    else:
        print("   ✗ 判据2 未中: 读被满足后 WriteFile 仍未返回 —— 现象另有原因")
        fails += 1

    k32.CloseHandle(hc)
    k32.CloseHandle(h_srv)
    return fails


def case_b():
    """接收侧改 Peek 轮询 —— 同样场景下 WriteFile 应立即返回。"""
    print("\n" + "=" * 70)
    print("[B 组] 接收线程改 PeekNamedPipe 轮询，主线程对同句柄 WriteFile(12B)")
    print("=" * 70)
    srv, hc = _make_pipe("b")
    h_srv = srv["h"]
    fails = 0

    stop = threading.Event()
    peek_hits = {"n": 0}

    def peek_reader():
        b = ctypes.create_string_buffer(1)
        read = wintypes.DWORD(0)
        avail = wintypes.DWORD(0)
        while not stop.is_set():
            ok = k32.PeekNamedPipe(hc, b, 1, ctypes.byref(read),
                                   ctypes.byref(avail), None)
            if not ok:
                break
            if avail.value > 0:
                peek_hits["n"] += 1
                break
            time.sleep(0.01)

    threading.Thread(target=peek_reader, daemon=True).start()
    time.sleep(0.3)

    res = {"done": False, "ms": None, "err": None, "written": 0}
    _write_async(hc, b"RELAY_NOTIFY", res)
    for _ in range(20):
        if res["done"]:
            break
        time.sleep(0.05)
    if res["done"] and res["ms"] < 100:
        print("   ✓ 判据3 命中: 无挂起读时 WriteFile 立即返回（{:.2f}ms）".format(res["ms"]))
    else:
        print("   ✗ 判据3 未中: WriteFile done={} ms={} err={}".format(
            res["done"], res["ms"], res["err"]))
        fails += 1

    # 服务端读出 12B 校验内容
    rbuf = ctypes.create_string_buffer(64)
    n = wintypes.DWORD(0)
    ok = k32.ReadFile(h_srv, rbuf, 12, ctypes.byref(n), None)
    got = rbuf.raw[:n.value] if ok else b""
    if got == b"RELAY_NOTIFY":
        print("   ✓ 判据4 命中: 服务端读到 {}B 内容一致".format(n.value))
    else:
        print("   ✗ 判据4 未中: 服务端读到 {!r} (ok={} err={})".format(
            got, ok, ctypes.get_last_error()))
        fails += 1

    stop.set()
    k32.CloseHandle(hc)
    k32.CloseHandle(h_srv)
    return fails


def case_c():
    """客户端句柄上 PeekNamedPipe 能否察觉「服务端已关闭」——决定 Peek 轮询能否退出。

    修法依赖这条：relay32 用 Peek 轮询取代阻塞 RecvPacket 后，必须仍能在对端
    断开时及时退出循环，否则会退化成"每 10ms 空转"的僵尸线程。
    """
    print("\n" + "=" * 70)
    print("[C 组] 服务端关闭后，客户端句柄上 PeekNamedPipe 的返回")
    print("=" * 70)
    srv, hc = _make_pipe("c")
    h_srv = srv["h"]
    fails = 0

    # 关掉服务端
    k32.CloseHandle(h_srv)
    time.sleep(0.2)

    b = ctypes.create_string_buffer(1)
    read = wintypes.DWORD(0)
    avail = wintypes.DWORD(0)
    ctypes.set_last_error(0)
    ok = k32.PeekNamedPipe(hc, b, 1, ctypes.byref(read),
                           ctypes.byref(avail), None)
    err = ctypes.get_last_error()
    print("   Peek ok={} err={} avail={}".format(ok, err, avail.value))
    # ERROR_BROKEN_PIPE = 109 / ERROR_PIPE_NOT_CONNECTED = 233
    if (not ok and err in (109, 233)) or (ok and avail.value == 0):
        # 用 GetLastError 的调用方（NamedPipeTransport::Peek 正是）会拿到 109
        print("   ✓ 判据5 命中: 对端关闭可被察觉（ok={} err={}）"
              "→ Peek 轮询能退出，不会空转".format(ok, err))
    else:
        print("   ✗ 判据5 未中: Peek 未反映对端关闭，Peek 轮询会空转")
        fails += 1

    k32.CloseHandle(hc)
    return fails


def main():
    print("隔离验证：同步命名管道句柄上「挂起的 ReadFile」是否阻塞同句柄的 WriteFile")
    fails = case_a() + case_b() + case_c()
    print("\n" + "=" * 70)
    print("SUMMARY: {} ({} failures)".format("PASS" if fails == 0 else "FAIL", fails))
    print("=" * 70)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
