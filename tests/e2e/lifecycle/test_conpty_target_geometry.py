"""特性: ConPTY 场景（目标跑在 WT 内）几何不被 DLL 触碰（BUG-013 回归）  类别: lifecycle

链路: TARGET 在 WT 窗口 A（ConPTY）里运行 → 注入（开 WT 窗口 B=mediator）
      → 拖动 A → 卸载（关 B）。

BUG-013 根因（2026-08-19 实测复现）：
  - 注入时 LazyInit align 用 B 的尺寸（wtCols）压缩 A 的 ConPTY（156x43→120x30），
    画面被裁 + WT 渲染错位 + 滚动条（用户报告"卸载后画面错乱+滚动条"）；
  - 卸载时 Unloader 对 ConPTY 做几何恢复，与 WT 实际渲染竞态 → 叠画。
修复：ConPTY 场景（GetConsoleWindow 类名 PseudoConsoleWindow）跳过
  align/隐藏窗口/卸载重放与几何恢复；虚拟状态跟随真实 ConPTY 尺寸，
  StatePoller 持续轮询真实 ConPTY 尺寸变化 → ApplyWtResize + EnqueueResizeEvent。

断言：
  1. 注入后 TARGET 的 ConPTY 尺寸 == 注入前（不被压缩）
  2. 拖动 A 后 TARGET 因 ConPTY 自感知重排（LAYOUT 键出现新尺寸）
  3. 卸载后 TARGET 的 ConPTY 尺寸 == 卸载前最后一刻（不跳回注入时几何）
"""
import os
import sys
import time
import ctypes
import ctypes.wintypes as wt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import injector
from common import result as result_mod
from common import paths

NAME = "conpty_target_geometry"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
h_in = get_std_in()
set_mode(h_in, ENABLE_WINDOW_INPUT | ENABLE_EXTENDED_FLAGS)
last_w = last_h = 0
lay_idx = 0

def paint(w, h):
    cells = (CHAR_INFO * (w * h))()
    feat = chr(65 + (w % 26))
    for y in range(h):
        for x in range(w):
            ch = " "
            if x == w // 2 or x == w - 1:
                ch = "|"
            elif x == w // 2 + 1:
                ch = feat
            cells[y * w + x].Char = ch
            cells[y * w + x].Attributes = 0x07
    rect = SMALL_RECT(0, 0, w - 1, h - 1)
    _k.WriteConsoleOutputW(h_out, cells, COORD(w, h), COORD(0, 0), ctypes.byref(rect))

deadline = time.time() + 40.0
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


def find_target_in_wt(marker: str, script_path: str):
    """在 WT 进程树里找 _targets 下的目标 python 进程（限定路径，排除自身）。"""
    import psutil

    # 目标脚本在 _targets 目录（唯一区分测试进程的标志：
    # 测试进程自身命令行也含 marker，其祖先链同样含 WindowsTerminal(OC 窗口)
    # ——2026-08-19 实测注入到测试进程自身导致卸载时 DLL 在测试进程里
    # 执行远程 FreeLibrary → access violation）
    norm = "/" + marker + ".py"
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() != "python.exe":
                continue
            if p.info["pid"] == os.getpid():
                continue
            cl = " ".join(p.info["cmdline"] or [])
            if "_targets" not in cl or norm not in cl.replace("\\", "/"):
                continue
            cur = p
            for _ in range(6):
                try:
                    cur = cur.parent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    break
                if cur is None:
                    break
                if (cur.name() or "").lower() in ("wt.exe", "windowsterminal.exe",
                                                   "openconsole.exe"):
                    return p.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def read_conpty_geom(pid: int):
    """子进程 AttachConsole 读目标控制台几何，返回 (cols, rows, (bufX, bufY))。

    独立子进程（helpers/dump_geom.py）执行 FreeConsole->AttachConsole->GCSBI：
    直接在本测试进程内 FreeConsole/AttachConsole 在 run_all 管道环境会失败
    （gle=6，进程控制台归属问题，2026-08-19 实测）；独立进程与 dump_console.py
    同路径，实测可靠。
    """
    import subprocess
    dump = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "helpers", "dump_geom.py")
    r = subprocess.run([sys.executable, dump, str(pid)],
                       capture_output=True, text=True, timeout=30)
    line = (r.stdout or "").strip()
    if line.startswith("FAIL"):
        print("  [DIAG] dump_geom: {}".format(line))
        return None
    parts = line.split()
    if len(parts) != 4:
        print("  [DIAG] dump_geom 输出异常: {!r}".format(line))
        return None
    return (int(parts[0]), int(parts[1]), (int(parts[2]), int(parts[3])))


def run() -> int:
    import win32gui
    import win32con

    failures = 0
    result_mod.clear_result(NAME)
    # 写目标脚本（复用 TARGET_PREAMBLE）
    script_path = os.path.join(paths.TARGETS_DIR, NAME + ".py")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        from common.target import TARGET_PREAMBLE
        f.write(TARGET_PREAMBLE)
        f.write("\n")
        f.write(TARGET_BODY)
    result_file = result_mod.result_file(NAME)

    # 1. 启动 WT 窗口 A 跑 TARGET（快照差分定位 A）
    existing = set(injector.find_wt_windows())
    win_name = "conpty_e2e_{}".format(int(time.time() * 1000))
    wt_cmd = [injector.find_wt_exe(), "-w", win_name, "--", "cmd", "/k",
              'python "{}" "{}"'.format(script_path, result_file)]
    print("[1] 启动 WT 窗口 A: {}".format(" ".join(wt_cmd)))
    wt_proc = __import__("subprocess").Popen(wt_cmd)

    a_hwnd = None
    dl = time.time() + 12.0
    while time.time() < dl:
        now = set(injector.find_wt_windows())
        new = now - existing
        if new:
            a_hwnd = sorted(new)[0]
            break
        time.sleep(0.3)
    if a_hwnd is None:
        print("  [FAIL] WT 窗口 A 未出现")
        return 1
    print("[OK] A hwnd={}".format(a_hwnd))

    # 2. 找 TARGET 进程
    target_pid = None
    dl = time.time() + 15.0
    while time.time() < dl and target_pid is None:
        target_pid = find_target_in_wt(NAME, script_path)
        if target_pid is None:
            time.sleep(0.5)
    if target_pid is None:
        injector._test_wt_hwnd = a_hwnd
        injector.cleanup(target_pid or 0)
        print("  [FAIL] TARGET 进程未找到")
        return 1
    v = result_mod.wait_result(NAME, "READY", timeout=15.0)
    if not v:
        print("  [FAIL] TARGET READY 超时")
        return 1
    print("[2] TARGET pid={}".format(target_pid))
    time.sleep(1.5)

    # 3. 注入前几何
    pre = read_conpty_geom(target_pid)
    print("[INFO] 注入前 ConPTY 几何: {}".format(pre))
    if pre is None:
        print("  [FAIL] 注入前读取 ConPTY 几何失败")
        return 1

    # 4. 注入（B 窗口）
    injector.clear_log(target_pid)
    mediator_proc = injector.start_wt_mediator(target_pid)
    if not injector.wait_for_handshake(target_pid, timeout=20.0):
        injector.cleanup(target_pid, mediator_proc)
        print("  [FAIL] 握手失败")
        return 1
    print("[OK] 握手成功")
    time.sleep(3.0)
    post_inject = read_conpty_geom(target_pid)
    print("[INFO] 注入后 ConPTY 几何: {}".format(post_inject))
    if post_inject is None:
        print("  [FAIL] 注入后读取 ConPTY 几何失败")
        failures += 1
    elif post_inject[:2] == pre[:2]:
        print("  [PASS] 注入后 ConPTY 未被压缩（{}）".format(post_inject[:2]))
    else:
        print("  [FAIL] 注入后 ConPTY 被改动 {} -> {}（align 未跳过）".format(
            pre[:2], post_inject[:2]))
        failures += 1

    # 5. 拖动 A（0.65x）→ ConPTY 自感知重排
    r2 = win32gui.GetWindowRect(a_hwnd)
    win32gui.SetWindowPos(a_hwnd, None, r2[0], r2[1],
                          int((r2[2] - r2[0]) * 0.65),
                          int((r2[3] - r2[1]) * 0.65), 0x0004)
    time.sleep(5.0)
    res = result_mod.read_result(NAME)
    lay_keys = sorted(k for k in res if k.startswith("LAYOUT"))
    print("[INFO] 布局序列: {}".format([res[k] for k in lay_keys]))
    grown = read_conpty_geom(target_pid)
    print("[INFO] 拖动 A 后 ConPTY 几何: {}".format(grown))
    if grown is None:
        print("  [FAIL] 拖动后读取 ConPTY 几何失败")
        failures += 1
    elif grown[:2] != pre[:2]:
        print("  [PASS] 拖动 A 后 ConPTY 尺寸变化 -> {}".format(grown[:2]))
    else:
        print("  [FAIL] 拖动 A 后 ConPTY 尺寸未变（StatePoller 自感知失效）")
        failures += 1
    if len(lay_keys) >= 2:
        print("  [PASS] TARGET 感知尺寸变化并重绘（{} 次布局）".format(len(lay_keys)))
    else:
        print("  [FAIL] TARGET 布局重绘次数 {} < 2".format(len(lay_keys)))
        failures += 1

    # 6. 卸载（关 B）
    b = injector._test_wt_hwnd
    if b:
        win32gui.PostMessage(b, win32con.WM_CLOSE, 0, 0)
        time.sleep(8.0)
    post_unload = read_conpty_geom(target_pid)
    print("[INFO] 卸载后 ConPTY 几何: {}".format(post_unload))
    if post_unload is None:
        print("  [FAIL] 卸载后读取 ConPTY 几何失败")
        failures += 1
    elif grown is not None and post_unload[:2] == grown[:2]:
        print("  [PASS] 卸载后 ConPTY 保持会话末几何（无回跳/无恢复竞态）")
    else:
        print("  [FAIL] 卸载后 ConPTY 几何回跳/变化 {} -> {}".format(
            grown[:2] if grown else "?", post_unload[:2] if post_unload else "?"))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    try:
        wt_proc.kill()
    except Exception:
        pass
    if target_pid:
        try:
            import psutil
            psutil.Process(target_pid).kill()
        except Exception:
            pass
    time.sleep(1.0)
    injector.cleanup(target_pid, None)
    return failures


if __name__ == "__main__":
    sys.exit(run())