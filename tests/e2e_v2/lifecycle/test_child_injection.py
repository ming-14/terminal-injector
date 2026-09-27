"""特性: 子进程注入（v2 试点）    类别: lifecycle

链路: 目标 cmd（经典 ConHost）中跑起 python 子进程
        → ProcessHooks::OnChildProcessCreated 捕获并注入 injected.dll（子会话）
        → 子进程输出经 ChildSession → mediator → pty → Terminal

预期:
  - 子进程 WriteConsoleW 成功（结果文件 RESULT=ok written）
  - **子进程自己的模块表里有 injected.dll**（直接证据，v1 只能靠"输出字节到了"间接推断）
  - **屏幕上出现子进程的输出行 TI_SUB_96**（v2 的屏幕通道）
  - mediator 日志含子进程输出字节（机制层，保留 v1 的字节断言）

与 v1 的差别:
  - v1 断言 = 结果文件 + mediator 日志 hex；
  - v2 增加两条更直接的：活模块表（子进程真的被注入）+ 屏幕输出行。
  - 目标脚本在标记后补一个 CRLF，让"输出行"能在屏幕上成为独立一行（v1 只按字节断言，
    不加换行；这里为了让屏幕断言可用而加，不改变被测语义）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon import result as result_mod
from pwcommon import childlog
from pwcommon.session import PwSession

NAME = "child_injection"
TEXT = "TI_SUB_96"
# 目标实际写出的字符数（标记 + CRLF）；WriteConsoleW 的 written 按 wchar 计
OUT_LEN = len(TEXT) + 2

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
rec("PID", str(os.getpid()))
import ctypes
k32 = ctypes.WinDLL("kernel32")
k32.WriteConsoleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
                              ctypes.c_void_p]
k32.WriteConsoleW.restype = ctypes.c_int
h_out = get_std_out()
text = "TI_SUB_96\\r\\n"
wbuf = ctypes.create_unicode_buffer(text)
written = ctypes.c_ulong(0)
ok = k32.WriteConsoleW(h_out, wbuf, len(wbuf.value.encode("utf-16-le")) // 2,
                       ctypes.byref(written), None)
rec("RESULT", "{} {}".format(int(ok), written.value))
done()
time.sleep(2.0)  # 给驱动侧留出采样活模块表的时间窗
'''


def run() -> int:
    pyterm.require()

    failures = 0
    try:
        with PwSession() as s:
            print("  [INFO] {}".format(s.describe()))
            s.run_target(NAME, TARGET_BODY, ready_key="READY")

            v = result_mod.wait_result(NAME, "RESULT", timeout=20.0)
            if not v:
                print("  [FAIL] RESULT: 无结果（子进程注入失败或目标未执行）")
                failures += 1
            else:
                ok, written = (int(x) for x in v.split()[:2])
                if ok and written == OUT_LEN:
                    print("  [PASS] 子进程 WriteConsoleW 成功（ok={} written={}）".format(
                        ok, written))
                else:
                    print("  [FAIL] WriteConsoleW: ok={} written={}（期望 1/{}）".format(
                        ok, written, OUT_LEN))
                    failures += 1

            # 直接证据：子进程自己的模块表里有我们的 DLL
            child_pid_str = result_mod.read_result(NAME).get("PID", "")
            if not child_pid_str:
                print("  [FAIL] 结果文件无 PID（子进程未上报）")
                failures += 1
            else:
                child_pid = int(child_pid_str)
                if childlog.has_hook_dll(child_pid):
                    print("  [PASS] 子进程 pid={} 模块表含我们的 DLL（子会话注入生效）".format(
                        child_pid))
                else:
                    print("  [FAIL] 子进程 pid={} 模块表无 injected.dll/relay32.dll".format(
                        child_pid))
                    failures += 1

            # 屏幕通道（v2 主通道）
            if s.wait_line(TEXT, timeout=10.0):
                print("  [PASS] 屏幕上出现子进程输出行 {}".format(TEXT))
            else:
                print("  [FAIL] 屏幕上没有输出行 {}；屏幕尾部={}".format(
                    TEXT, s.tail_lines(8)))
                failures += 1

            # 机制层：字节确实经 mediator 转发
            hex_txt = " ".join("{:02X}".format(b) for b in TEXT.encode("utf-8"))
            if s.wait_log(hex_txt, timeout=10.0, which="mediator"):
                print("  [PASS] mediator 日志含子进程输出字节 {}".format(hex_txt))
            else:
                print("  [FAIL] mediator 日志未见 {} 字节".format(hex_txt))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
