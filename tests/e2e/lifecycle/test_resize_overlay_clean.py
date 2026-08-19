"""特性: 注入后多次 resize 画面无叠画（BUG-012 回归）  类别: lifecycle

链路: 目标 TUI 按 GCSBI 尺寸整屏重绘（WriteConsoleOutputW 全屏矩阵）
      → DLL 全量路径翻译 VT → WT。resize 后新布局必须覆盖旧帧，
      不得残留（历史缺陷：全量路径跳过"默认空格"，注入后 WT 已有
      旧帧（补发快照/上一尺寸帧），跳过空格导致旧帧永不覆盖 →
      连续 resize 后多帧叠画（实测 120→69→99→141 四帧叠画）。

预期: 连续 0.6x/1.4x/1.4x resize 后，WT 画面 = 单一最新布局帧：
  - 每行 "┌"（TextArea 顶框角）出现次数 ≤ 1（无并列双框）
  - 画面非空行数 == 当前视口行数（无残留多帧垂直堆叠）
"""
import os
import sys
import time
import ctypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import injector
from helpers import input_sim
from common import result as result_mod

import uiautomation as auto

NAME = "resize_overlay_clean"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
h_in = get_std_in()
set_mode(h_in, ENABLE_WINDOW_INPUT | ENABLE_EXTENDED_FLAGS)
last_w = last_h = 0
lay_idx = 0

def paint(w, h):
    # 整屏确定性矩阵：每行 x==w//2 与 x==w-1 画 '|'（右区框线），
    # x==w//2+1 画布局特征字符（取决于宽度）→ 宽度不同布局不同，
    # 旧帧未被覆盖时同一行会出现两组框线（'|' 计数 4+，叠画）
    cells = (CHAR_INFO * (w * h))()
    feat = chr(65 + (w % 26))  # 宽度特征：120→'R'? 统一 chr(65+w%26)
    for y in range(h):
        for x in range(w):
            ch = " "
            attr = 0x07
            if x == w // 2 or x == w - 1:
                ch = "|"
            elif x == w // 2 + 1:
                ch = feat
            cells[y * w + x].Char = ch
            cells[y * w + x].Attributes = attr
    rect = SMALL_RECT(0, 0, w - 1, h - 1)
    _k.WriteConsoleOutputW(h_out, cells, COORD(w, h), COORD(0, 0), ctypes.byref(rect))

deadline = time.time() + 30.0
while time.time() < deadline:
    info = get_csbi(h_out)
    if info is None:
        time.sleep(0.05)
        continue
    w = info.srWindow.Right - info.srWindow.Left + 1
    h = info.srWindow.Bottom - info.srWindow.Top + 1
    if (w, h) != (last_w, last_h):
        paint(w, h)
        lay_idx += 1
        rec("LAYOUT{}".format(lay_idx), "{}x{}".format(w, h))
        last_w, last_h = w, h
    time.sleep(0.05)
rec("DONE2", "1")
done()
'''


def find_console_window(pid: int):
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


def uia_read_wt(hwnd):
    win = auto.ControlFromHandle(hwnd)
    stack = [win]
    term = None
    while stack:
        cur = stack.pop()
        try:
            children = cur.GetChildren()
        except Exception:
            children = []
        for ch in children:
            try:
                clsn = ch.ClassName or ""
            except Exception:
                clsn = ""
            if "TermControl" in clsn:
                term = ch
                stack.clear()
                break
            stack.append(ch)
    if term is None:
        return None
    tp = term.GetTextPattern()
    if tp is None:
        return None
    return tp.DocumentRange.GetText(-1)


def run() -> int:
    import win32gui
    import win32con
    import psutil

    failures = 0
    result_mod.clear_result(NAME)
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "_targets")
    from common import paths
    script_path = os.path.join(paths.TARGETS_DIR, NAME + ".py")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        from common.target import TARGET_PREAMBLE
        f.write(TARGET_PREAMBLE)
        f.write("\n")
        f.write(TARGET_BODY)
    result_file = result_mod.result_file(NAME)

    host_pid = injector.start_target_cmd()
    time.sleep(0.8)
    hwnd = None
    dl = time.time() + 5.0
    while time.time() < dl and hwnd is None:
        hwnd = find_console_window(host_pid)
        if hwnd is None:
            time.sleep(0.3)
    if hwnd is None:
        injector.cleanup(host_pid)
        print("  [FAIL] 宿主控制台窗口未找到")
        return 1
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    fdl = time.time() + 3.0
    while time.time() < fdl:
        win32gui.SetForegroundWindow(hwnd)
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            break
        time.sleep(0.2)

    cmd = 'python "{}" "{}"'.format(script_path, result_file)
    input_sim.type_text(cmd)
    time.sleep(0.3)
    input_sim.type_enter()

    target_pid = None
    dl = time.time() + 10.0
    while time.time() < dl and target_pid is None:
        try:
            host = psutil.Process(host_pid)
            for child in host.children(recursive=False):
                if child.name().lower() == "python.exe":
                    target_pid = child.pid
                    break
        except psutil.NoSuchProcess:
            pass
        if target_pid is None:
            time.sleep(0.3)
    if target_pid is None:
        injector.cleanup(host_pid)
        print("  [FAIL] 目标 python 进程未找到")
        return 1
    v = result_mod.wait_result(NAME, "READY", timeout=15.0)
    if not v:
        injector.cleanup(host_pid)
        print("  [FAIL] TARGET READY 超时")
        return 1
    print("[INFO] 目标 pid={}".format(target_pid))

    injector.clear_log(target_pid)
    mediator_proc = injector.start_wt_mediator(target_pid)
    if not injector.wait_for_handshake(target_pid, timeout=20.0):
        injector.cleanup(host_pid, mediator_proc)
        print("  [FAIL] 握手失败")
        return 1
    print("[OK] 握手成功")
    time.sleep(2.0)
    injector.focus_wt()
    wt_hwnd = injector._test_wt_hwnd
    if not wt_hwnd:
        injector.cleanup(host_pid, mediator_proc)
        print("  [FAIL] 无 WT 窗口句柄")
        return 1

    time.sleep(1.0)
    for k in (0.6, 1.4, 1.4):
        r2 = win32gui.GetWindowRect(wt_hwnd)
        win32gui.SetWindowPos(wt_hwnd, None, r2[0], r2[1],
                              int((r2[2] - r2[0]) * k),
                              int((r2[3] - r2[1]) * k), 0x0004)
        time.sleep(2.5)

    time.sleep(2.0)
    text = uia_read_wt(wt_hwnd) or ""
    if not text:
        print("  [FAIL] UIA 读 WT 文本为空")
        failures += 1
    else:
        # 叠画特征：同一行出现多组右区框线（单帧每行 2 个 '|'，叠画 4+）
        max_bars = 0
        bars_at = ""
        for i, line in enumerate(text.splitlines()):
            n = line.count("|")
            if n > max_bars:
                max_bars = n
                bars_at = "line[{}]={!r}".format(i, line[:90])
        if max_bars <= 2:
            print("  [PASS] 画面无叠画（每行框线 ≤2）")
        else:
            print("  [FAIL] 画面叠画：{} 行最大 {} 个框线".format(bars_at, max_bars))
            failures += 1

        lays = result_mod.read_result(NAME)
        lay_keys = sorted(k for k in lays if k.startswith("LAYOUT"))
        if len(lay_keys) >= 3:
            print("  [PASS] resize 触发 {} 次布局重绘".format(len(lay_keys)))
        else:
            print("  [FAIL] 布局重绘次数 {} < 3（resize 未驱动重绘）".format(len(lay_keys)))
            failures += 1
        print("  [INFO] 布局序列: {}".format(
            ", ".join(lays[k] for k in lay_keys)))

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    injector.cleanup(host_pid, mediator_proc)
    return failures


if __name__ == "__main__":
    sys.exit(run())