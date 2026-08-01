"""特性: SGR 1006 鼠标直通（VT 模式）    类别: vt_passthrough

链路: 目标请求 SGR 1006 鼠标报告 → mediator 写 ConPTY → WT 启用 SGR 鼠标 →
      SendInput 鼠标事件 → WT → ConPTY → mediator → DLL VtInput（raw 队列）→
      目标 os.read 读到原始 \\x1b[<b;x;yM 序列

预期:
  - 目标开启 VT_INPUT + 请求 SGR 1006（写 \\x1b[?1000h\\x1b[?1006h）
  - 驱动点击 WT 窗口内某坐标
  - 目标 os.read 读到 \\x1b[<0;x;yM 原始序列（坐标无转换）

验证方式: 目标 os.read 自检 hex（前缀 1b5b3c）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "vt_mouse_passthrough"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
# 请求 SGR 1006 鼠标报告（VT 输出直通写序列）
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\\x1b[?1000h\\x1b[?1006h")
rec("MOUSE_ON", str(int(ok)))
rec("READY_READ", "1")
import os as _os
b = _os.read(0, 32)
rec("GOT_RAW", b.hex())
# 关闭鼠标报告
write_bytes(h_out, b"\\x1b[?1000l\\x1b[?1006l")
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v0 = s.wait_result(NAME, "MOUSE_ON", timeout=15.0)
            if not v0:
                print("  [FAIL] MOUSE_ON: 无结果")
                failures += 1
            v1 = s.wait_result(NAME, "READY_READ", timeout=15.0)
            if not v1:
                print("  [FAIL] READY_READ: 无结果")
                failures += 1
            else:
                # 点击 WT 窗口中心附近
                time.sleep(0.5)  # 等鼠标报告开启生效
                cx, cy = s.wt_center()
                if cx is None:
                    print("  [FAIL] 驱动: 无法获取 WT 窗口中心")
                    failures += 1
                else:
                    s.mouse_click(cx, cy)
                    v = s.wait_result(NAME, "GOT_RAW", timeout=15.0)
                    if not v:
                        print("  [FAIL] GOT_RAW: 无结果（SGR 鼠标序列未到达）")
                        failures += 1
                    elif v.startswith("1b5b3c"):
                        print("  [PASS] os.read 读到原始 SGR 序列 (1B 5B 3C..M)")
                        print("  [INFO] 完整序列: {}".format(v))
                    else:
                        print("  [FAIL] GOT_RAW: {}（期望 1b5b3c 开头）".format(v))
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
