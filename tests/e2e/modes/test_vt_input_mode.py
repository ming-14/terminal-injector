"""特性: ENABLE_VIRTUAL_TERMINAL_INPUT    类别: modes

链路: 目标 SetConsoleMode/GetConsoleMode → DLL ModeHooks → mediator ModeSwitchNotify

预期（工程实际语义）:
  - SetConsoleMode(输入句柄, VT_INPUT) 成功，GetConsoleMode 返回一致
  - 模式切换后 mediator 收到 ModeSwitchNotify（日志 OnModeSwitchNotify VT input mode=1）

与原生语义的差异（LIM-004 已清理）:
  - 原生 ConHost：VT_INPUT 开启后 ReadConsoleInputW 返回的键盘事件以 VT 序列
    表示（如方向键 → ESC [ A），行编辑/按键翻译进入直通模式
  - 工程：VT 输入直通在 DLL 侧实现（DllRecvLoop VtInput 分支按
    ENABLE_VIRTUAL_TERMINAL_INPUT 选择 EnqueueRaw 原始字节直通 或
    INPUT_RECORD 翻译双队列入队；ReadFile/ReadConsoleInputW 按模式消费）；
    mediator 仅转发（LIM-004 清理：删除无使用方的 m_vtInputMode 状态，
    模式通知仅记录日志）

验证方式: 目标自检 set/get + mediator 日志 ModeSwitchNotify
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "vt_input_mode"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
rec("BEFORE_VT", "1")
time.sleep(0.8)  # 给驱动 mark 时间（ModeSwitchNotify 在 set 时同步发出）
ok1 = set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
g1 = get_mode(h_in)
rec("SET_GET", str(int(ok1)) + " " + hex(g1))
time.sleep(0.5)  # 给 mediator 处理 ModeSwitchNotify 的时间
ok2 = set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
g2 = get_mode(h_in)
rec("CLEAR_GET", str(int(ok2)) + " " + hex(g2))
time.sleep(0.5)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            log = s.log()
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # 顺序关键：mark 前只等 BEFORE_VT（rec 在 set 之前，早于通知日志）；
            # 若先等 SET_GET（set 完成后 rec），通知已落盘，mark 会漏掉
            v0 = s.wait_result(NAME, "BEFORE_VT", timeout=15.0)
            if not v0:
                print("  [FAIL] BEFORE_VT: 无结果")
                failures += 1
            log.mark()

            v1 = s.wait_result(NAME, "SET_GET", timeout=15.0)
            if not v1:
                print("  [FAIL] SET_GET: 无结果")
                failures += 1
            else:
                parts = v1.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x200:
                    print("  [PASS] SET_GET Set(0x200) 后 Get 一致 (0x200)")
                else:
                    print("  [FAIL] SET_GET: {}（期望 1 0x200）".format(v1))
                    failures += 1

            # mediator 收到 ModeSwitchNotify（VT input mode=1）
            m = log.wait_for_regex(r"OnModeSwitchNotify: VT input mode=1", timeout=8.0)
            if m:
                print("  [PASS] ModeSwitchNotify 通知 mediator (VT input mode=1)")
            else:
                print("  [FAIL] ModeSwitchNotify: 日志未见通知（8s 超时）")
                failures += 1

            v2 = s.wait_result(NAME, "CLEAR_GET", timeout=15.0)
            if not v2:
                print("  [FAIL] CLEAR_GET: 无结果")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x7:
                    print("  [PASS] CLEAR_GET 清除 VT_INPUT 后 Get=0x7")
                else:
                    print("  [FAIL] CLEAR_GET: {}（期望 1 0x7）".format(v2))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
