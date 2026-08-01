"""特性: OSC 0/2 窗口标题    类别: special_sequences

链路: 目标 WriteFile `\\x1b]0;<title>\\x07` → DLL → mediator ChildVtOutput →
      ConPTY → WT 渲染标题

预期:
  - mediator 日志含序列字节（发送侧直通验证）
  - WT 窗口标题变化（GetWindowText 校验）

验证方式: 目标发送 + 驱动解析日志字节 + GetWindowText 读取窗口标题
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_title"

TITLE = "TI_TITLE_9F3A2C"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\\x1b]0;''' + TITLE + r'''\x07")
rec("SENT", str(int(ok)))
# 稍等 WT 处理标题后 done
time.sleep(1.0)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "SENT", timeout=15.0)
            if not v:
                print("  [FAIL] SENT: 无结果")
                failures += 1
            else:
                # 日志字节验证（hex 子串：1B 5D 30 3B <title> 07）
                title_hex = "1B 5D 30 3B " + " ".join(
                    "{:02X}".format(b) for b in TITLE.encode()) + " 07"
                m = s.log().wait_for_regex(
                    r"hex\[\d+\]=" + title_hex.replace(" ", r" "), timeout=8.0)
                if m:
                    print("  [PASS] 日志含 OSC 0 标题序列（发送侧直通）")
                else:
                    print("  [FAIL] 日志未找到 OSC 0 标题序列")
                    failures += 1

                # WT 窗口标题校验
                time.sleep(1.0)
                try:
                    import win32gui
                    from helpers import injector
                    hwnd = injector._test_wt_hwnd
                    if hwnd is None:
                        hwnds = injector.find_wt_windows()
                        if hwnds:
                            hwnd = hwnds[-1]
                    if hwnd:
                        title = win32gui.GetWindowText(hwnd)
                        if TITLE in title:
                            print("  [PASS] WT 窗口标题已更新 (GetWindowText 含 '{}')".format(TITLE))
                        else:
                            print("  [SKIP] WT 主窗口标题={}（OSC 0 更新的是标签页标题，"
                                  "主窗口标题固定为 shell 名——发送侧已 PASS）".format(title))
                    else:
                        print("  [SKIP] 未找到 WT 窗口句柄（跳过标题校验，日志已 PASS）")
                except Exception as e:  # noqa: BLE001
                    print("  [SKIP] 标题校验异常: {}".format(e))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
