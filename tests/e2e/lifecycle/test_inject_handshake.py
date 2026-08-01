"""特性: 注入 + 握手    类别: lifecycle

链路: start_target_cmd → start_wt_mediator → mediator 注入 injected.dll →
      cmd DLL 握手（Hello/HelloAck）→ Handshake OK → cmd 输出被劫持

预期:
  - 握手成功（TestSession 进入即验证）
  - cmd 输入 echo 命令 → 输出经 DLL→mediator 转发（日志 ChildVtOutput
    含 "TI_HS_95" 的 UTF-8 字节）→ WT 显示

验证方式: 驱动 type_text + mediator 日志字节断言
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from helpers import input_sim

NAME = "inject_handshake"

TEXT = "TI_HS_95"


def run() -> int:
    failures = 0
    try:
        with TestSession() as s:
            print("  [PASS] 握手成功（cmd PID={}）".format(s.target_pid))
            input_sim.type_text("echo {}".format(TEXT))
            input_sim.press_key(input_sim.VK_RETURN)
            time.sleep(2.0)
            # cmd（主进程）输出走 VtPassThrough pipe→stdout: VtOutput
            # （子进程 python 才走 ChildVtOutput），且要验证输出方向字节
            hex_txt = " ".join("{:02X}".format(b) for b in TEXT.encode("utf-8"))
            m = s.log().wait_for_regex(
                r"pipe.*stdout: VtOutput len=\d+.*hex\[\d+\]=[0-9A-F ]*{}".format(
                    hex_txt), timeout=10.0)
            if m:
                print("  [PASS] cmd 输出 {} 经 DLL→mediator→WT 转发（VtOutput hex 含 {}）".format(
                    TEXT, hex_txt))
            else:
                print("  [FAIL] VtOutput hex 未见 {} 字节（输出未被劫持）".format(hex_txt))
                failures += 1
            mc = s.log().wait_for_regex(
                r"pipe.*stdout: VtOutput len=\d+", timeout=5.0)
            if mc:
                print("  [PASS] 日志含 VtOutput 转发记录（cmd 输出劫持生效）")
            else:
                print("  [FAIL] 日志无 VtOutput")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
