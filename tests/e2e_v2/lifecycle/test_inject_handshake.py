"""特性: 注入 + 握手（v2 试点首个用例）    类别: lifecycle

链路: 目标 cmd（经典 ConHost，窗口隐藏）
        ←注入── mediator（跑在 pywezterm.Pty 里，扮演原 WT 的角色）
      输入: pty.write("echo TI_HS_95\\r") → mediator → DLL → 目标输入队列
      输出: 目标 → DLL → mediator → pty → Terminal 还原屏幕

预期:
  - 握手成功（Handshake OK）
  - 敲 echo 后**屏幕上出现输出行 TI_HS_95**（v1 只能靠 mediator 日志 hex 反推；
    这里直接断言屏幕，且用"整行相等"排除掉命令回显那一行的假命中）
  - mediator 日志含 `VtOutput` 转发记录（机制层）
  - DLL 日志 `conptyHosted=0` —— 自证拓扑：目标确实是经典 ConHost，
    走的是主进程那条分支（不是 ConPTY 托管的流式 shell 分支）

与 v1 的差别:
  v1 = 起 WT 窗口 + SendInput 打前台窗口 + 解析日志 hex；
  v2 = pywezterm.Pty + 直接写输入字节 + 直接断言屏幕。零焦点依赖、无 IME 干扰。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon.session import PwSession

NAME = "inject_handshake"
TEXT = "TI_HS_95"


def run() -> int:
    pyterm.require()

    failures = 0
    try:
        with PwSession() as s:   # host="conhost"（默认）
            print("  [INFO] {}".format(s.describe()))
            print("  [PASS] 握手成功（目标 pid={}）".format(s.target_pid))

            s.send_line("echo {}".format(TEXT))

            # 1. 屏幕断言（v2 的主通道）：输出行必须整行等于 TEXT
            if s.wait_line(TEXT, timeout=10.0):
                print("  [PASS] 屏幕上出现输出行 {}（输入→注入→执行→输出全链路）".format(TEXT))
            else:
                print("  [FAIL] 屏幕上没有输出行 {}；屏幕尾部={}".format(
                    TEXT, s.tail_lines(8)))
                failures += 1

            # 2. 机制层：mediator 确实在转发 VT 输出
            if s.wait_log("VtOutput", timeout=5.0, which="mediator"):
                print("  [PASS] mediator 日志含 VtOutput 转发记录")
            else:
                print("  [FAIL] mediator 日志无 VtOutput")
                failures += 1

            # 3. 拓扑自证：目标是经典 ConHost。**不做这一步，本用例可能静默跑到
            #    "WT 里开的 cmd" 那条分支上（conptyHosted=1）还照样是绿的**
            ok, actual = s.assert_topology("conhost", timeout=15.0)
            if ok:
                print("  [PASS] 拓扑自证 conptyHosted=0（经典 ConHost，与 v1 同路径）")
            else:
                print("  [FAIL] 拓扑不符：期望 conhost，实际 {}；LazyInit 记录={}".format(
                    actual, [l for l in s.dll_log().splitlines() if "LazyInit" in l][:2]))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
