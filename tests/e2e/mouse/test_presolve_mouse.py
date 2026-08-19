"""特性: 注入前已启用鼠标模式的 TUI（winui demo 场景）  类别: mouse

链路: 目标(TUI)在真实 ConHost 中早已 SetConsoleMode(0x98, 含 ENABLE_MOUSE_INPUT)
      → 注入目标进程（运行中的 python 全屏程序，此后不再 SetConsoleMode）
      → mediator 握手时须按 Hello 初始 inputMode 补发 \\x1b[?1002h\\x1b[?1006h
      → WT 启用鼠标报告 → 点击/拖拽转 SGR 1006 → DLL 翻译 MOUSE_EVENT → 目标收到

回归背景（BUG-010/011）:
  注入目标在注入前已启用鼠标模式时，ModeChange 永不发出（SetConsoleMode 只调一次），
  mediator 无线索启用 WT 鼠标报告 → WT 把点击当默认行为、拖拽当选择文本，
  目标进程收不到任何 MOUSE_EVENT（TextBox 点击放置光标失效、拖动变行选择）。
  本测试锁定"握手时按初始模式初始化鼠标报告"这一修复。

预期:
  - 点击: 收到 down(0x1)/up(0x0) 事件对，坐标正确
  - 拖拽: down → 拖动持按下(0x1) → up(0x0)，按下期间位置移动
"""
import os
import sys
import time
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import injector
from helpers import input_sim
from common import target as target_mod
from common import result as result_mod

NAME = "presolve_mouse"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
# 注入前就启用鼠标（等价 winui console driver init 的 0x98 组合：
# ENABLE_WINDOW_INPUT|ENABLE_MOUSE_INPUT|ENABLE_EXTENDED_FLAGS，无 QuickEdit）
mode = ENABLE_WINDOW_INPUT | ENABLE_MOUSE_INPUT | ENABLE_EXTENDED_FLAGS
set_mode(h_in, mode)
rec("MODE_SET", str(get_mode(h_in)))
deadline = time.time() + 40.0
evs = []
while time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs.append((m.dwButtonState, m.dwEventFlags,
                            m.dwMousePosition.X, m.dwMousePosition.Y))
                rec("EV" + str(len(evs)), "%08x,%04x,%d,%d" % (
                    m.dwButtonState, m.dwEventFlags,
                    m.dwMousePosition.X, m.dwMousePosition.Y))
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
done()
'''


def find_console_window(pid: int):
    """按进程 pid 找原生控制台窗口（ConsoleWindowClass）。"""
    import win32gui
    import win32process

    result = []

    def cb(hwnd, _):
        if win32gui.GetClassName(hwnd) != "ConsoleWindowClass":
            return
        if not win32gui.IsWindowVisible(hwnd):
            return
        wpid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if wpid == pid:
            result.append(hwnd)

    win32gui.EnumWindows(cb, None)
    return result[0] if result else None


def run() -> int:
    import win32gui
    import win32con
    import psutil

    failures = 0
    result_mod.clear_result(NAME)
    script_path = target_mod.write_target(NAME, TARGET_BODY)
    result_file = result_mod.result_file(NAME)

    print("[setup] 启动宿主 cmd...")
    host_pid = injector.start_target_cmd()
    time.sleep(0.8)

    # 前置失败统一清理
    def fail(msg):
        print("  [FAIL] " + msg)
        injector.cleanup(host_pid)
        return 1

    print("[setup] 前台定位宿主控制台窗口...")
    hwnd = None
    deadline = time.time() + 5.0
    while time.time() < deadline and hwnd is None:
        hwnd = find_console_window(host_pid)
        if hwnd is None:
            time.sleep(0.3)
    if hwnd is None:
        return fail("找不到宿主控制台窗口")
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    fg_deadline = time.time() + 3.0
    while time.time() < fg_deadline:
        win32gui.SetForegroundWindow(hwnd)
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.2)

    print("[setup] 运行目标 TUI（注入前启用鼠标模式）...")
    cmd = 'python "{}" "{}"'.format(script_path, result_file)
    input_sim.type_text(cmd)
    time.sleep(0.3)
    input_sim.type_enter()
    v = result_mod.wait_result(NAME, "READY", timeout=15.0)
    if not v:
        return fail("目标 READY 超时")
    mode_v = result_mod.wait_result(NAME, "MODE_SET", timeout=8.0)
    print("[INFO] 目标模式 = {}".format(mode_v))

    # 找目标 python 进程（宿主 cmd 的直接子进程）
    probe_pid = None
    deadline = time.time() + 8.0
    while time.time() < deadline and probe_pid is None:
        host = psutil.Process(host_pid)
        for child in host.children(recursive=False):
            if child.name().lower() == "python.exe":
                probe_pid = child.pid
                break
        if probe_pid is None:
            time.sleep(0.3)
    if probe_pid is None:
        return fail("找不到目标 python 进程")
    print("[INFO] 目标 python pid={}".format(probe_pid))

    print("[setup] 注入目标（模式早已启用，之后不再 SetConsoleMode）...")
    injector.clear_log(probe_pid)
    mediator_proc = injector.start_wt_mediator(probe_pid)
    if not injector.wait_for_handshake(probe_pid, timeout=20.0):
        return fail("握手失败")
    print("[OK] 握手成功")
    time.sleep(1.0)
    injector.focus_wt()

    # 日志验证：握手时补发鼠标报告启用序列（修复核心）
    log_ok = False
    log_path = injector.log_path(probe_pid)
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            log = f.read()
        log_ok = "1002h" in log and "sent" in log
        if log_ok:
            print("  [PASS] mediator 握手时发送 \\x1b[?1002h\\x1b[?1006h")
        else:
            print("  [FAIL] mediator 日志无鼠标报告启用序列（握手初始化缺失）")
            failures += 1

    if injector._test_wt_hwnd is None:
        injector.cleanup(host_pid, mediator_proc)
        return fail("无 WT 窗口句柄")
    r = win32gui.GetWindowRect(injector._test_wt_hwnd)
    cx, cy = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2

    print("[test] 模拟点击（左键 ×2）...")
    for _ in range(2):
        input_sim.mouse_click(cx, cy, "left")
        time.sleep(0.6)

    print("[test] 模拟拖拽（左键按住移动）...")
    input_sim.mouse_drag(cx - 120, cy, cx + 120, cy, steps=4, step_sleep=0.2)
    time.sleep(1.0)

    v = result_mod.wait_result(NAME, "COUNT", timeout=45.0)
    if not v:
        print("  [FAIL] COUNT 无结果（目标未写完，输入未达）")
        failures += 1
    else:
        res = result_mod.read_result(NAME)
        evs = []
        i = 1
        while "EV{}".format(i) in res:
            part = res["EV{}".format(i)].split(",")
            evs.append((int(part[0], 16), int(part[1], 16),
                        int(part[2]), int(part[3])))
            i += 1
        print("  [INFO] 收到 {} 个鼠标事件".format(len(evs)))

        clicks = [e for e in evs if e[0] == 0x1 and e[1] == 0x0]   # 按下（非移动）
        ups = [e for e in evs if e[0] == 0x0 and e[1] == 0x0]      # 释放
        if len(clicks) >= 1 and len(ups) >= 1:
            print("  [PASS] 点击事件到达: down={} up={}".format(len(clicks), len(ups)))
        else:
            print("  [FAIL] 点击事件缺失: down={} up={}".format(len(clicks), len(ups)))
            failures += 1

        # 拖拽验证：按下定位与释放位置不同（位置移动）
        drag = [e for e in evs if e[0] == 0x1]
        if len(drag) >= 2:
            moved = (drag[0][2], drag[0][3]) != (drag[-1][2], drag[-1][3])
            if moved:
                print("  [PASS] 拖拽按下期间位置移动 ({},{}) -> ({},{})".format(
                    drag[0][2], drag[0][3], drag[-1][2], drag[-1][3]))
            else:
                print("  [FAIL] 拖拽无位置移动")
                failures += 1
        else:
            print("  [FAIL] 拖拽事件缺失（down 按下事件 <2）")
            failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    injector.cleanup(host_pid, mediator_proc)
    return failures


if __name__ == "__main__":
    sys.exit(run())