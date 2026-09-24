"""特性: 32 位启动器链路（py.exe 中继）    类别: lifecycle

链路: cmd(64) [已注入 injected.dll]
        └─ CreateProcess ─► py.exe(32)     ← 跨位数：x64 的 injected.dll 注不进去，
        │                                      由 32 位的 relay32inject.exe 代劳注入 relay32.dll
        └─ 中继捕获 ─► python.exe(64)      ← 被冻住，由 mediator 注入 x64 injected.dll
              → 输出经 ChildSession → mediator → WT

预期（Phase 23）:
  - mediator 判定该子进程为 32 位：OnChildProcessNotify 带 peerIsRelay=1
  - 中继握手走 RelayHello（不是 injected.dll 的 Hello）
  - 中继把 64 位孙进程上报：OnRelayChildNotify（修复前就断在这一环，见下）
  - 孙进程 WriteConsoleW 的输出经 DLL→mediator 到达链路（日志含其 UTF-8 字节）
  - 目标脚本结果文件写出 RESULT（证明孙进程真的跑起来，没被一直冻着）
  - 中继会话正常收尾（ChildSession: Run exit）—— 证明 detour 没卡死

回归背景:
  中继的接收循环若用阻塞 RecvPacket(ReadFile) 常驻，会占住同步命名管道的 I/O 锁，
  使同句柄上的 Send(WriteFile)（即上报孙进程的那一帧）永远发不出去 →
  子进程一直冻结、cmd 卡住不出提示符（现象与"空白屏"一致）。
  本测试的 OnRelayChildNotify + Run exit 两条断言就是钉住这个回归点。

环境前提: C:\\Windows\\py.exe 必须是 32 位，且 `py -3` 解析到 64 位 python；
          否则本机不存在这条跨位数链路 → 记 UNSUPPORTED（不算失败）。

验证方式: 目标脚本结果文件 + mediator 日志（中继会话 / RelayHello / 日志 hex 字节）
"""
import ctypes
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "launcher_chain"

TEXT = "TI_PY_CHAIN"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
k32.WriteConsoleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
                              ctypes.c_void_p]
k32.WriteConsoleW.restype = ctypes.c_int
h_out = get_std_out()
text = "TI_PY_CHAIN"
wbuf = ctypes.create_unicode_buffer(text)
written = ctypes.c_ulong(0)
ok = k32.WriteConsoleW(h_out, wbuf, len(wbuf.value.encode("utf-16-le")) // 2,
                       ctypes.byref(written), None)
rec("RESULT", "{} {}".format(int(ok), written.value))
done()
'''


def _py_launcher_bitness():
    """C:\\Windows\\py.exe 的位数；用 GetBinaryTypeW（OS 权威判定，不手工解析 PE）。

    返回 32 / 64 / None（不存在或不可判定）。
    """
    path = r"C:\Windows\py.exe"
    if not os.path.exists(path):
        return None
    bt = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetBinaryTypeW(
        ctypes.c_wchar_p(path), ctypes.byref(bt))
    if not ok:
        return None
    return {0: 32, 6: 64}.get(bt.value, bt.value)


def _py_resolved_python_bitness():
    """`py -3` 实际解析到的 python 的位数（32 / 64），失败返回 None。"""
    try:
        out = subprocess.run(
            ["py", "-3", "-c", "import struct;print(struct.calcsize('P')*8)"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    v = (out.stdout or "").strip()
    return int(v) if v in ("32", "64") else None


def run() -> int:
    # ---- 环境前提：本机必须真的存在"64 → 32 → 64"这条链路，否则无从测起
    launcher_bits = _py_launcher_bitness()
    python_bits = _py_resolved_python_bitness()
    print("  [前提] py.exe 位数={}  py -3 -> python 位数={}".format(
        launcher_bits, python_bits))
    if launcher_bits != 32 or python_bits != 64:
        print("  [SKIP] 本机无 32 位启动器链路（需 py.exe=32 且 py -3 -> python=64）")
        print("\nSUMMARY: UNSUPPORTED (py.exe 位数={} python 位数={})".format(
            launcher_bits, python_bits))
        return 0

    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", launcher="py -3")

            # 1. 孙进程（python.exe）真的跑起来了：结果文件由它写出
            v = s.wait_result(NAME, "RESULT", timeout=25.0)
            if not v:
                print("  [FAIL] RESULT: 无结果（孙进程未执行 —— 可能被冻死或未注入）")
                s.log_tail(20)
                failures += 1
            else:
                ok, written = (int(x) for x in v.split()[:2])
                if ok and written == len(TEXT):
                    print("  [PASS] 孙进程 WriteConsoleW 成功（64 位孙进程已接管）")
                else:
                    print("  [FAIL] WriteConsoleW: ok={} written={}".format(ok, written))
                    failures += 1

            # 2. mediator 把该子进程判为 32 位（走中继），而非普通注入
            m = s.log().wait_for_regex(
                r"OnChildProcessNotify: childPid=(\d+) parentPid=(\d+) .*peerIsRelay=1",
                timeout=10.0)
            if m:
                relay_pid = int(m.group(1))
                print("  [PASS] 子进程被判为 32 位走中继（pid={} peerIsRelay=1）".format(
                    relay_pid))
            else:
                relay_pid = 0
                print("  [FAIL] 日志无 peerIsRelay=1（未走中继路径）")
                failures += 1

            # 3. 中继握手是 RelayHello（区别于 injected.dll 的 Hello）
            m = s.log().wait_for_regex(
                r"Handshake: RelayHello pid=(\d+) bitness=32", timeout=10.0)
            if m:
                print("  [PASS] 中继握手 RelayHello（pid={} bitness=32）".format(
                    m.group(1)))
            else:
                print("  [FAIL] 日志无 RelayHello（中继未与 mediator 建连）")
                failures += 1

            # 4. 中继把 64 位孙进程上报 mediator —— 修复前断掉的正是这一环
            m = s.log().wait_for_regex(
                r"OnRelayChildNotify: childPid=(\d+) parentPid=(\d+)", timeout=10.0)
            if m:
                print("  [PASS] 中继上报 64 位孙进程（child={} parent={}）".format(
                    m.group(1), m.group(2)))
            else:
                print("  [FAIL] 日志无 OnRelayChildNotify（中继的 Send 未送达 —— "
                      "检查接收循环是否占住了管道 I/O）")
                failures += 1

            # 5. 孙进程输出经 DLL→mediator 到达链路
            hex_txt = " ".join("{:02X}".format(b) for b in TEXT.encode("utf-8"))
            m = s.log().wait_for_regex(hex_txt, timeout=10.0)
            if m:
                print("  [PASS] 孙进程输出经 DLL→mediator 转发（日志含 {}）".format(hex_txt))
            else:
                print("  [FAIL] 日志未见 {} 字节（孙进程输出未劫持）".format(hex_txt))
                failures += 1

            # 6. 中继会话正常收尾 —— 证明 detour 未卡死（回归点）
            if relay_pid:
                m = s.log().wait_for_regex(
                    r"ChildSession: Run exit, pid={}".format(relay_pid), timeout=15.0)
                if m:
                    print("  [PASS] 中继会话正常收尾（detour 未卡死）")
                else:
                    print("  [FAIL] 中继会话未收尾（pid={} 的 detour 可能被堵）".format(
                        relay_pid))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
