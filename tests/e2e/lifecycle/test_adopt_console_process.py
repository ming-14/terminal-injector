"""特性: 接管同控制台上注入前已存在的后代进程    类别: lifecycle

链路: shell（cmd /k python 计时器，控制台由 ConPTY 托管）
        ←注入── mediator（跑在另一个 ConPTY 里，扮演 WT）

预期:
  - 注入时枚举同控制台进程，把「父链能回溯到注入目标」的后代一并接管
  - 已在运行的后代（本例的 python 计时器）输出改为经 DLL→mediator 转发
  - 注入目标终端持续收到该后代的**新**输出（不是只停在重放的一帧）
  - DLL 日志出现 Adopt 记录，mediator 日志出现对应 ChildProcessNotify

回归背景（2026-09-27 用户报告）:
  开 WT → 在 WT 里运行 taskboard.py（Textual TUI）→ 劫持承载它的 shell 到新 WT，
  新 WT 上 TUI 不刷新画面、无法输入。
  根因：注入只接管「注入目标本身」+「注入之后由它 CreateProcess 出的子进程」，
  注入前已在运行的后代永远不会被注入 —— 它的输出仍写旧 ConHost，新 WT 上只停在
  注入瞬间从共享 ConHost 快照重放的那一帧（DLL 日志 `screen content replayed`），
  之后再无任何字节；输入转发到了共享 ConHost，但没有输出通路，看着就是"没反应"。
  （docs/2026-09-23-child-injection-32bit-report.md §4.2 曾把"轮询补注入已存在
   子进程"判为不可行 —— 那说的是**时间窗**（子进程创建到首行输出的竞态）；
   同控制台枚举是注入完成后的定向补接管，不涉及该竞态，故可行。）

为什么这里用「计时器子进程」而不是真 TUI:
  判据是"注入后目标终端是否**持续**收到后代的新输出"。计时器每 250ms 打一行
  递增标记，既不依赖渲染语义，也能直接断言"注入后仍在新输出"；真 TUI 的
  可见帧还需要终端模型解读，不稳定。

为什么用 pywezterm 的 ConPTY 而不是真 WT:
  同样是 ConPTY（目标控制台窗口类名 = PseudoConsoleWindow，与 WT 一致），
  但能直接读字节断言、零焦点依赖、秒级完成。未装 pywezterm 时记 UNSUPPORTED。

验证方式: 目标终端字节（注入后递增标记）+ DLL 日志（Adopt）+ mediator 日志（子会话）
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.paths import PROJECT_ROOT, BUILD_BIN, RESULTS_DIR, ti_log_path
from common import childlog

NAME = "adopt_console_process"

# pywezterm 包所在目录（含 pywezterm/ 子包 + pywezterm.pyd）
PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")
MARK_FMT = "ADOPT_TICK_{}"


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _write_ticker_script() -> str:
    """写计时器脚本：每 250ms 打印一行递增标记（unbuffered 由 -u 保证）。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, "adopt_ticker.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "import sys, time\n"
            "n = 0\n"
            "while True:\n"
            "    n += 1\n"
            "    sys.stdout.write({!r}.format(n) + '\\r\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.25)\n".format(MARK_FMT)
        )
    return path


def _pump(pty, seconds):
    """读 pty 至多 seconds 秒，返回累计字节。"""
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            d = bytes(pty.read(65536, timeout=0.1))
        except Exception:
            break
        if d:
            buf += d
    return buf


def _ticks(data):
    return [int(m) for m in re.findall(rb"ADOPT_TICK_(\d+)", data)]


def _wait_ticks(pty, min_ticks, timeout, acc=b""):
    """持续读直到累计里出现 >= min_ticks 个标记，返回 (累计字节, 标记列表)。"""
    end = time.time() + timeout
    while time.time() < end and len(_ticks(acc)) < min_ticks:
        d = _pump(pty, 0.25)
        if d:
            acc += d
    return acc, _ticks(acc)


def _wait_new_ticks(pty, above, min_count, timeout, acc=b""):
    """持续读直到累计里出现 >= min_count 个「值 > above」的新标记。

    不能只看"有标记"：注入时会把源 ConHost 屏幕重放到目标终端，
    重放内容里就带着注入前的旧标记，只看数量会被重放帧直接满足。
    """
    end = time.time() + timeout
    while time.time() < end:
        fresh = [t for t in _ticks(acc) if t > above]
        if len(fresh) >= min_count:
            break
        d = _pump(pty, 0.25)
        if d:
            acc += d
    return acc, _ticks(acc)


def run() -> int:
    if PWTERM_DIR not in sys.path:
        sys.path.insert(0, PWTERM_DIR)
    try:
        import pywezterm
        import psutil
    except ImportError as e:
        print("  [SKIP] 无法 import pywezterm/psutil (PWTERM_DIR={!r}): {}".format(
            PWTERM_DIR, e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm/psutil 不可用)")
        return 0

    failures = 0
    src = dst = None
    target_pid = 0
    try:
        script = _write_ticker_script()

        # 1. 源：shell 里先跑起计时器子进程（注入**之前**就存在 —— 这正是回归条件）
        src = pywezterm.Pty(120, 30)
        target_pid, _ = src.spawn(
            [COMSPEC, "/k", sys.executable, "-u", script])
        print("  [INFO] 目标 shell pid={}（ConPTY 托管，子进程 = 计时器 python）"
              .format(target_pid))

        src_bytes, src_ticks = _wait_ticks(src, 3, 12.0)
        if len(src_ticks) < 3:
            print("  [FAIL] 注入前源终端未见计时器输出（用例前提不成立）：{!r}"
                  .format(src_bytes[-200:]))
            print("\nSUMMARY: FAIL ({} failures)".format(failures + 1))
            return failures + 1
        pre_max = max(src_ticks)
        print("  [PASS] 注入前子进程已在输出（{} 个标记，最大 {}）"
              .format(len(src_ticks), pre_max))

        # 2. 目标终端：mediator 跑在另一个 ConPTY 里
        dst = pywezterm.Pty(120, 30)
        dst.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])

        mlog = ti_log_path(target_pid)
        deadline = time.time() + 25
        while time.time() < deadline and "Handshake OK" not in _read(mlog):
            time.sleep(0.3)
        if "Handshake OK" in _read(mlog):
            print("  [PASS] 握手成功")
        else:
            print("  [FAIL] 握手失败（见 {}）".format(mlog))
            failures += 1

        # 3. 关键断言：注入后目标终端必须持续收到子进程的**新**输出
        dst_bytes, dst_ticks = _wait_new_ticks(dst, pre_max, 3, 12.0)
        new_ticks = [t for t in dst_ticks if t > pre_max]
        if len(new_ticks) >= 3:
            print("  [PASS] 注入后目标终端收到后代新输出（{} 个新标记，{}..{}）"
                  .format(len(new_ticks), min(new_ticks), max(new_ticks)))
        else:
            print("  [FAIL] 注入后目标终端未收到后代新输出（收到 {} 个标记，"
                  "注入前最大 {}）—— 后代未被接管，画面会停在重放帧"
                  .format(len(dst_ticks), pre_max))
            failures += 1

        # 4. DLL 侧：Adopt 记录（机制判据）
        dll_log = childlog.latest_injected_log(target_pid)
        text = _read(dll_log)
        if not text:
            print("  [FAIL] 未找到目标 DLL 日志（pid={}）".format(target_pid))
            failures += 1
        else:
            m = re.search(r"Adopt: console has (\d+) process\(es\), (\d+) descendant",
                          text)
            if m and int(m.group(2)) >= 1:
                print("  [PASS] 同控制台枚举到 {} 个进程，其中 {} 个后代待接管"
                      .format(m.group(1), m.group(2)))
            else:
                print("  [FAIL] DLL 日志未见后代接管记录: {}".format(
                    m.groups() if m else "无 Adopt 记录"))
                failures += 1

            # 本控制台只有「shell 自身 + 一个计时器后代」，故必须恰好接管 1 个、
            # 且不是 shell 本身（接管自身即 IsTargetProcess 分支，不可能出现）。
            # 若把祖先/无关进程也纳入，这里会 >1。
            adopted = re.findall(r"Adopt: pid=(\d+) depth=(\d+) injected=(\d+)", text)
            if adopted and len(adopted) == 1 and adopted[0][2] == "1" \
                    and adopted[0][0] != str(target_pid):
                print("  [PASS] 恰好接管 1 个后代 pid={} depth={} injected=1"
                      .format(adopted[0][0], adopted[0][1]))
            else:
                print("  [FAIL] 接管记录不符（期望 1 个后代且 injected=1，实际 {}）"
                      .format(adopted))
                failures += 1

        # 5. mediator 侧：为被接管的后代建了子会话
        mtext = _read(mlog)
        if "OnChildProcessNotify" in mtext and "ChildSession started" in mtext:
            print("  [PASS] mediator 为被接管的后代建立了子会话")
        else:
            print("  [FAIL] mediator 未见子会话建立记录")
            failures += 1
    finally:
        for p in (dst, src):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        try:
            import psutil
            if target_pid:
                try:
                    proc = psutil.Process(target_pid)
                    for child in proc.children(recursive=True):
                        try:
                            child.terminate()
                        except Exception:
                            pass
                    proc.terminate()
                except Exception:
                    pass
            for proc in psutil.process_iter(["name"]):
                try:
                    if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                        proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
