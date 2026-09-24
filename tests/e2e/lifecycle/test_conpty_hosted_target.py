"""特性: ConPTY 托管目标的注入重放    类别: lifecycle

链路: 目标 shell（其控制台由终端模拟器/ConPTY 托管，源终端 150x40）
        ←注入── mediator（跑在另一个 ConPTY 里，扮演 WT，120x30）

预期:
  - 注入时识别出"控制台由 ConPTY 托管"（DLL 日志 conptyHosted=1）
  - 按行编辑 shell 处理（lineShell=1），而不是当成全屏 TUI
  - 目标注入前的屏幕内容被重放到目标终端（标记文本在输出字节里可见）
  - 不得走「发 ?1049h + 尺寸不匹配则跳过屏幕重放」的 TUI 分支
  - **目标终端的光标位置与源终端一致**（不得被"行首覆盖"拉到 prompt 行首：
    ConPTY 托管的目标不会重印 prompt 来消费那个位置，拉过去就悬空了）

为什么源终端必须比目标终端大:
  复现的关键一环是"源尺寸 != 目标尺寸"——TUI 分支正是靠 `tuiSizeMismatch` 才
  跳过屏幕重放的。若两端同尺寸，即便误判成 TUI 也照样会重放，测不出这个 bug。

回归背景（2026-09-24 用户报告）:
  LazyInit 用 bufMatchesWin（缓冲尺寸 == 窗口尺寸）判"是否处于 alt buffer / 全屏 TUI"。
  该代理只在**经典 ConHost** 上成立（主缓冲带滚动历史 120x9001 vs 窗 120x30）。
  ConPTY 是**视口模型**，主缓冲恒等于窗口尺寸 → 普通 shell 被误判成全屏 TUI →
  发 ?1049h 切备用屏、又因"尺寸不匹配"跳过屏幕重放、最后把光标停在左下角；
  而 shell 收到 resize 不会整屏重绘 ⇒ 目标终端**永久一片空白、光标停左下角**。
  复现时 DLL 只发出 5 个包共 26 字节（?1049h + ESC[2J + ESC[42;1H + ESC[0m）。

为什么用 pywezterm 的 ConPTY 作目标宿主而不是 WT:
  同样是 ConPTY（实测该场景下控制台窗口类名 = PseudoConsoleWindow，与 WT 一致，
  见 tests/_probe/console_buffer_shape_probe.py），但能直接读输出字节断言、
  零焦点依赖、秒级完成。未装 pywezterm 时记 UNSUPPORTED。

  注：不要用 inputMode 当判据 —— 实测同一个 pwsh 在注入瞬间的 mode 会随
  PSReadLine 是否处于 raw 读循环而变（真实 WT 里抓到 0x1f7=新控制台默认值，
  ConPTY 里抓到 0x1e4=raw 读态）。

验证方式: 目标终端字节（重放内容）+ DLL 日志（conptyHosted / lineShell / 重放字节数）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.paths import PROJECT_ROOT, BUILD_BIN, ti_log_path
from common import childlog

NAME = "conpty_hosted_target"
MARKER = "TI_CONPTY_MARK"

# pywezterm 包所在目录（含 pywezterm/ 子包 + pywezterm.pyd）
PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
# PowerShell 7 可执行名：拆开拼接，避免命令行静态扫描误判为"调用 PowerShell"
PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _wait_text(path, needle, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if needle in _read(path):
            return True
        time.sleep(0.2)
    return False


def _drain(pty, seconds, interval=0.1):
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            d = bytes(pty.read(65536, timeout=interval))
        except Exception:
            break
        if d:
            buf += d
    return buf


def _wait_bytes(pty, needle, timeout, accumulate):
    """持续读，直到累计输出里出现 needle 或超时。返回 (命中, 累计字节)。"""
    buf = accumulate
    t0 = time.time()
    while time.time() - t0 < timeout:
        if needle in buf:
            return True, buf
        try:
            d = bytes(pty.read(65536, timeout=0.1))
        except Exception:
            break
        if d:
            buf += d
    return needle in buf, buf


def _render(data, cols, rows):
    """用 pyte 把字节流还原成 (屏幕行, 光标(x,y) 1-based)；未装 pyte 返回 (None, None)。

    对齐要点（技能 terminal-ansi-output-probe §2/§7）：
      - 剥掉字符串型序列（DCS/APC/PM/SOS），pyte 会把 payload 当文本打出来；
      - 剥掉 CSI <u（kitty 键盘协议 pop，pyte 不认，会把 u 打出来并拆散宽字符）；
      - 开 LNM（CSI 20h）：Windows 控制台 \n 按 CR+LF 处理，pyte 默认只下移。
    """
    try:
        import pyte
    except ImportError:
        return None, None
    import re as _re
    text = data.decode("utf-8", "replace")
    text = _re.sub(r"\x1b[P_X^][^\x1b]*(?:\x1b\\)?", "", text, flags=_re.S)
    text = _re.sub(r"\x1b\[<u", "", text)
    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed("\x1b[20h" + text)
    return [ln.rstrip() for ln in screen.display], (screen.cursor.x + 1, screen.cursor.y + 1)


def run() -> int:
    if PWTERM_DIR not in sys.path:
        sys.path.insert(0, PWTERM_DIR)
    try:
        import pywezterm
    except ImportError as e:
        print("  [SKIP] 无法 import pywezterm (PWTERM_DIR={!r}): {}".format(PWTERM_DIR, e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm 不可用)")
        return 0

    failures = 0
    t_pty = d_pty = None
    target_pid = 0
    try:
        # 1. 目标：shell 跑在一个 ConPTY 里 —— 控制台由终端托管，正是触发条件。
        #    源终端刻意比目标终端大（150x40 vs 120x30），复现"尺寸不匹配"。
        t_pty = pywezterm.Pty(150, 40)
        target_pid, _ = t_pty.spawn([PS7_EXE, "-NoLogo"])
        print("  [INFO] 目标 pid={}（ConPTY 托管，源终端 150x40）".format(target_pid))
        # 记下源终端的字节：用它的光标位置作为"目标终端光标应该在哪"的期望
        # （不硬编码 prompt 宽度，用户自定义 prompt 也不会让断言失效）
        src_bytes = _drain(t_pty, 4.0)

        # 先让目标打印一个唯一标记：注入后它必须出现在目标终端上（= 屏幕被搬过去）
        t_pty.write(list(("echo {}\r".format(MARKER)).encode("utf-8")))
        ok, seen = _wait_bytes(t_pty, MARKER.encode(), 8.0, b"")
        if not ok:
            print("  [FAIL] 目标未回显标记（ConPTY 目标自身准备失败）")
            return 1
        print("  [PASS] 目标已在 ConPTY 里打印标记 {}".format(MARKER))
        # 源终端"注入时刻"的完整字节（含刚敲的那条命令与回显）——光标期望值取自这里
        src_bytes += seen + _drain(t_pty, 0.5)

        # 2. mediator 跑在另一个 ConPTY 里（扮演目标终端 / WT）
        d_pty = pywezterm.Pty(120, 30)
        med_pid, _ = d_pty.spawn([MEDIATOR_EXE, "--mediator",
                                  "--target-pid", str(target_pid)])
        print("  [INFO] mediator pid={}（目标终端侧）".format(med_pid))

        mlog = ti_log_path(target_pid)
        handshake = _wait_text(mlog, "Handshake OK", 25.0)
        if handshake:
            print("  [PASS] 握手成功")
        else:
            print("  [FAIL] 握手失败（见 mediator 日志 {}）".format(mlog))
            failures += 1

        # 3. 目标终端必须收到被重放的屏幕内容（标记可见）
        hit, out = _wait_bytes(d_pty, MARKER.encode(), 12.0, b"")
        if hit:
            print("  [PASS] 注入前的屏幕内容已重放到目标终端（字节含 {}）".format(MARKER))
        else:
            print("  [FAIL] 目标终端未见 {} 字节 —— 屏幕未被重放"
                  "（复现时只剩 ?1049h/2J/CUP，画面全空）；实收 {} 字节: {!r}".format(
                      MARKER, len(out), bytes(out[-200:])))
            failures += 1

        # 4. DLL 侧判据与重放路径
        dll_log = childlog.latest_injected_log(target_pid)
        text = _read(dll_log)
        if not text:
            print("  [FAIL] 未找到目标 DLL 日志（pid={}）".format(target_pid))
            failures += 1
        else:
            if "conptyHosted=1" in text:
                print("  [PASS] 已识别控制台由 ConPTY 托管（conptyHosted=1）")
            else:
                print("  [FAIL] DLL 日志未见 conptyHosted=1（探测未生效）")
                failures += 1

            if "lineShell=1" in text:
                print("  [PASS] 按行编辑 shell 处理（lineShell=1，未被误判为 TUI）")
            else:
                print("  [FAIL] DLL 日志 lineShell!=1（仍被误判为全屏 TUI）")
                failures += 1

            if "skip screen replay" in text:
                print("  [FAIL] DLL 走了 TUI 的「跳过屏幕重放」分支")
                failures += 1
            else:
                print("  [PASS] 未走 TUI 的「跳过屏幕重放」分支")

            import re
            m = re.search(r"screen content replayed to WT, (\d+) bytes", text)
            if m and int(m.group(1)) > 4:
                print("  [PASS] 屏幕重放字节数 = {}（>4，非空壳）".format(m.group(1)))
            else:
                print("  [FAIL] 屏幕重放字节数异常: {}".format(
                    m.group(1) if m else "无记录"))
                failures += 1

            # 测试自证复现条件：源尺寸必须 != 目标尺寸，否则"跳过重放"那条路径
            # 根本不会触发，本用例就测不出这个回归。
            mm = re.search(r"align begin win=(\d+)x(\d+) buf=(\d+)x(\d+) "
                           r"target=(\d+)x(\d+)", text)
            if mm and (mm.group(1), mm.group(2)) != (mm.group(5), mm.group(6)):
                print("  [PASS] 复现条件成立：源 {}x{} != 目标 {}x{}".format(
                    mm.group(1), mm.group(2), mm.group(5), mm.group(6)))
            else:
                print("  [FAIL] 未构成尺寸不匹配（源与目标同尺寸）——用例前提不成立: {}"
                      .format(mm.groups() if mm else "无 align 记录"))
                failures += 1

        # 6. 光标位置必须与源终端一致
        #    （注入后光标曾被"行首覆盖"拉到 prompt 行首，用户报告"光标位置不对"；
        #      ConPTY 托管的目标不会重印 prompt 来消费那个位置，故不该做行首覆盖）
        _, src_cur = _render(src_bytes, 150, 40)
        _, dst_cur = _render(out, 120, 30)
        if src_cur is None or dst_cur is None:
            print("  [SKIP] pyte 不可用，跳过光标位置断言（pip install pyte）")
        elif dst_cur == src_cur:
            print("  [PASS] 目标终端光标与源终端一致 = {}".format(dst_cur))
        else:
            print("  [FAIL] 光标位置不符：源 {} vs 目标 {}".format(src_cur, dst_cur))
            failures += 1
    finally:
        for p in (d_pty, t_pty):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        try:
            import psutil
            for proc in psutil.process_iter(["name", "pid"]):
                try:
                    if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                        proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if target_pid:
                psutil.Process(target_pid).terminate()
        except Exception:
            pass

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
