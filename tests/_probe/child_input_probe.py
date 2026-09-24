# -*- coding: utf-8 -*-
r"""child_input_probe.py —— 逐层量「孙进程交互输入」：按键进得去吗、回显出得来吗

背景（2026-09-24 用户报告）
--------------------------
注入 pwsh 到 WT 后运行 `C:\...\termtest\run.py`（.py 关联 → py.exe → python.exe），
菜单渲染正常，但停在 `选择 >` 处**无法输入**。

链路：目标(64, 终端托管) → py.exe(32, 中继) → python.exe(64, 注入 injected.dll)

用户现场日志里看到的现象是矛盾的，所以本探针把每一段单独量出来：
  mediator：RouteInput 到底路由到哪个会话（父/哪个子）
  子进程 DLL：VtInput 收到没有、ReadConsoleW 返回了什么字符
  目标终端：按键有没有被回显（= 用户肉眼能不能看到自己打的字）

脚本走 **.py 文件关联**（和用户一样敲路径），而不是直接 `py xxx.py`，
因为关联链路上多一层 CreateProcess，是真实路径。

    python child_input_probe.py
"""
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "e2e"))

from common.paths import PROJECT_ROOT, BUILD_BIN, ti_log_path
from common import childlog

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_childinput")
SCRIPT = os.path.join(WORK, "ask.py")
RESULT = os.path.join(WORK, "ask_result.txt")

SCRIPT_SRC = '''# -*- coding: utf-8 -*-
# 先做不自阻塞的诊断记录（即使后面卡住，也能读到这些）
import sys, ctypes
RAW_FIRST = "--rawfirst" in sys.argv
out = open(r"__RESULT__", "w", encoding="utf-8")
def rec(k, v):
    out.write("{}={}\\n".format(k, v)); out.flush()

rec("STDIN_ISATTY", sys.stdin.isatty())
rec("STDOUT_ISATTY", sys.stdout.isatty())
try:
    import msvcrt
    k = ctypes.windll.kernel32
    k.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    k.GetConsoleMode.restype = ctypes.c_int
    k.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    k.SetConsoleMode.restype = ctypes.c_int
    k.GetFileType.argtypes = [ctypes.c_void_p]; k.GetFileType.restype = ctypes.c_ulong
    for name, fobj in (("STDIN", sys.stdin), ("STDOUT", sys.stdout)):
        try:
            h = msvcrt.get_osfhandle(fobj.fileno())
            m = ctypes.c_ulong(0)
            ok = k.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(m))
            rec(name + "_MODE", "ok={} mode=0x{:x} filetype={}".format(ok, m.value, k.GetFileType(ctypes.c_void_p(h))))
        except Exception as e:
            rec(name + "_MODE", "EXC:" + repr(e))
    # 真实 ConHost 模式：新建一个 CONIN$ 句柄（DLL 按句柄识别输入句柄，很可能
    # 不认这个新句柄 → GetConsoleMode 透传到真 API）→ 与 DLL 缓存值对比
    try:
        k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                                  ctypes.c_void_p]
        k.CreateFileW.restype = ctypes.c_void_p
        hc = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        m3 = ctypes.c_ulong(0)
        ok3 = k.GetConsoleMode(ctypes.c_void_p(hc), ctypes.byref(m3))
        rec("REAL_CONIN_MODE", "handle={} ok={} mode=0x{:x}".format(hc, ok3, m3.value))
    except Exception as e:
        rec("REAL_CONIN_MODE", "EXC:" + repr(e))

    if RAW_FIRST:
        h = msvcrt.get_osfhandle(sys.stdin.fileno())
        m = ctypes.c_ulong(0)
        k.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(m))
        newmode = (m.value & ~(0x1 | 0x2 | 0x10))  # 清 PROCESSED/LINE/MOUSE，保留 ECHO → 0x1e4
        ok = k.SetConsoleMode(ctypes.c_void_p(h), newmode)
        m2 = ctypes.c_ulong(0)
        k.GetConsoleMode(ctypes.c_void_p(h), ctypes.byref(m2))
        rec("RAW_FIRST_APPLIED", "ok={} want=0x{:x} now=0x{:x}".format(ok, newmode, m2.value))
except Exception as e:
    rec("DIAG_EXC", repr(e))
rec("READY", "1")
print("PROBE_READY", flush=True)

# 单字符读（不经过行语义）：按键应该让它立刻返回
try:
    import msvcrt
    ch = msvcrt.getwch()
    rec("GETWCH", repr(ch))
    print("GETWCH_GOT=" + repr(ch), flush=True)
except Exception as e:
    rec("GETWCH", "EXC:" + repr(e))

# 行读（= run.py 菜单用的 input()）
try:
    line = input("choice > ")
    rec("INPUT_RESULT", repr(line))
    print("GOT=" + repr(line), flush=True)
except Exception as e:
    rec("INPUT_RESULT", "EXC:" + repr(e))
rec("DONE", "1")
'''


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


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


def _one(variant, pywezterm):
    """跑一个变体，返回 (是否收到按键回显, input() 是否返回, 说明)。

    variant="plain"  ：**不注入**，脚本直接跑在一个 ConPTY 里（对照组，定义"正确行为"）
    variant="direct" ：在目标里直接敲 `python <脚本>` → python.exe 是**直接子进程**
    variant="assoc"  ：敲脚本路径（.py 关联）→ py.exe(32) → python.exe（**孙进程**）
    隔离这三者，才能判断问题属于"中继链路特有"还是"所有子会话共有"，
    并拿到正常终端的基线（回显从哪来、行读靠什么结束）。
    """
    try:
        os.remove(RESULT)
    except OSError:
        pass

    t_pty = d_pty = None
    target_pid = 0
    try:
        if variant in ("plain", "plain_raw"):
            # 对照组：不注入，直接把脚本跑在 ConPTY 里
            # plain_raw：脚本自己先把行模式关掉（模拟"继承了父进程 raw 模式"），
            #            用来判断"注入态挂住"是不是单纯由行模式关引起
            argv = [sys.executable, SCRIPT]
            if variant == "plain_raw":
                argv.append("--rawfirst")
            t_pty = pywezterm.Pty(120, 30)
            t_pty.spawn(argv)
            _drain(t_pty, 3.0)
            t_pty.write(list(b"1"))
            time.sleep(0.6)
            t_pty.write(list(b"\r"))
            out2 = _drain(t_pty, 6.0)
            echoed = b"1" in out2
            returned = "INPUT_RESULT" in _read(RESULT)
            print("  variant={:<9} （无注入，对照组）".format(variant))
            print("    键入后：回显={} input()返回={} 收到{}字节".format(
                echoed, returned, len(out2)))
            print("    键入后字节: {!r}".format(bytes(out2[:200])))
            print("    结果文件: {!r}".format(_read(RESULT).replace("\n", " ")))
            return echoed, returned, {}

        t_pty = pywezterm.Pty(120, 30)
        target_pid, _ = t_pty.spawn([PS7_EXE, "-NoLogo"])
        _drain(t_pty, 3.5)

        d_pty = pywezterm.Pty(120, 30)
        mlog = ti_log_path(target_pid)
        # variant 前缀 native_ = **不注入**，直接把目标 shell 的 ConPTY 当终端用。
        # 与注入组用同一个父进程（同一个控制台、同一个模式），这才是能定修法归属的对照。
        inject = not variant.startswith("native")
        if inject:
            med_pid, _ = d_pty.spawn([MEDIATOR_EXE, "--mediator",
                                      "--target-pid", str(target_pid)])
            t0 = time.time()
            while time.time() - t0 < 25 and "Handshake OK" not in _read(mlog):
                time.sleep(0.2)
            _drain(d_pty, 1.0)
        else:
            # 不注入：承载 shell 的那个 pty 本身就是"目标终端"，直接在它上面敲与读
            d_pty = t_pty

        cmd = SCRIPT if variant.endswith("assoc") else 'python "{}"'.format(SCRIPT)
        d_pty.write(list((cmd + "\r").encode("utf-8")))
        out = _drain(d_pty, 12.0)
        ready = b"PROBE_READY" in out
        prompt_seen = b"choice >" in out

        try:
            import psutil
            names = {}
            for c in psutil.Process(target_pid).children(recursive=True):
                names.setdefault(c.name().lower(), []).append(c.pid)
            chain = {k: v for k, v in sorted(names.items())}
        except Exception:
            chain = {}

        med = _read(mlog) if inject else ""
        relay = "RelayHello" in med
        child_routes = [l for l in med.splitlines() if "routed to child" in l]

        # 从"目标终端"键入
        d_pty.write(list(b"1"))
        time.sleep(0.6)
        d_pty.write(list(b"\r"))
        out2 = _drain(d_pty, 6.0)
        echoed = b"1" in out2
        returned = "INPUT_RESULT" in _read(RESULT)
        print("  variant={:<14} inject={} 后代={}".format(variant, inject, chain))
        print("    PROBE_READY={} 提示符出现={} 用了中继={} 路由到子={} 条".format(
            ready, prompt_seen, relay, len(child_routes)))
        print("    键入后：回显={} input()返回={} 收到{}字节 结果文件={!r}".format(
            echoed, returned, len(out2), _read(RESULT).replace("\n", " ")))
        if out2:
            print("    键入后字节: {!r}".format(bytes(out2[:120])))

        # 从**探针自己**（进程里没有 DLL，不会被 GetConsoleMode 的 Hook 骗）
        # AttachConsole 到目标控制台读真实输入模式 —— 判定 0x1e4 是真值还是 DLL 缓存
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.FreeConsole()
            att = k.AttachConsole(target_pid)
            hc = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
            mm = ctypes.c_ulong(0)
            ok = k.GetConsoleMode(ctypes.c_void_p(hc), ctypes.byref(mm))
            print("    [探针 AttachConsole 读真实模式] attach={} ok={} mode=0x{:x}".format(
                att, ok, mm.value))
            k.FreeConsole()
        except Exception as e:
            print("    [探针 AttachConsole] 失败: {!r}".format(e))

        return echoed, returned, chain
    finally:
        for p in (d_pty, t_pty):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                try:
                    if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                        proc.terminate()
                except Exception:
                    pass
            if target_pid:
                psutil.Process(target_pid).terminate()
        except Exception:
            pass


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("无法 import pywezterm: {}".format(e))
        return 1

    os.makedirs(WORK, exist_ok=True)
    with open(SCRIPT, "w", encoding="utf-8") as f:
        f.write(SCRIPT_SRC.replace("__RESULT__", RESULT))

    print("=" * 78)
    print("子进程交互输入：无注入对照组 / 直接子进程 / 经 py.exe 的孙进程")
    print("=" * 78)
    for v in ("direct", "assoc"):
        print("\n[variant {}]".format(v))
        _one(v, pywezterm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
