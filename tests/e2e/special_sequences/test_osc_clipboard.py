"""特性: OSC 52 剪贴板写入    类别: special_sequences

链路: 目标 WriteFile `\\x1b]52;c;<base64>\\x07` → WT → WT 写入系统剪贴板

预期:
  - 写入后 GetClipboardData 内容 == 目标内容 → PASS
  - 无效果 → UNSUPPORTED（不允许 FAIL；若目标能读回自己写的也算）

验证方式: 驱动读取系统剪贴板比对（win32clipboard 或 ctypes）
"""
import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_clipboard"

CLIP_TEXT = "TI_CLIPBOARD_51B7E8"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
import base64
b64 = base64.b64encode(b"''' + CLIP_TEXT + r'''").decode()
ok, _ = write_bytes(h_out, b"\x1b]52;c;" + b64.encode() + b"\x07")
rec("SENT", str(int(ok)))
time.sleep(1.5)
done()
'''


def _read_clipboard() -> str:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.OpenClipboard(None)
            try:
                h = user32.GetClipboardData(13)  # CF_UNICODETEXT
                if h:
                    ptr = ctypes.windll.kernel32.GlobalLock(h)
                    if ptr:
                        try:
                            text = ctypes.wstring_at(ptr)
                            return text
                        finally:
                            ctypes.windll.kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
        except Exception:
            pass
    return ""


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
                v_done = s.wait_result(NAME, "DONE", timeout=15.0)
                clip = _read_clipboard()
                if clip is None:
                    clip = ""
                print("  [INFO] 剪贴板内容: {}".format(repr(clip[:60])))
                if CLIP_TEXT in clip:
                    print("  [PASS] OSC 52 写入剪贴板成功（WT 支持）")
                else:
                    print("  [UNSUPPORTED] 剪贴板无目标内容（WT 不支持 OSC 52 或"
                          "写入被拒绝）")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
