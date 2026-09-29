# -*- coding: utf-8 -*-
"""探针：`CSI > 1 u`（Kitty 键盘协议 push）到底由哪一层兑现？

## 背景 / 要证伪的断言

BUG-022 与 `src/dll/Unloader.cpp` 的注释都写着：

  「`>1u` → WT 改用 Kitty 编码所有按键，目标 shell 收到的按键字节变成 Kitty 格式」
  「只有真 WT 才崩、ConPTY 不崩的原因是 **ConPTY 不支持 `>1u`**」

这是**上游文档/源码注释里的断言**，但没有实测证据。若它不成立，
以下两件事都站不住：
  a) 「v2 天然复现不出 BUG-022」这个结论
  b) Unloader 剔除 `>1u` 的必要性论证（虽然剔除本身可能是对的）

## 本探针怎么测

ConPTY 架构里按键编码发生在**宿主侧**（WT 的 TermControl / 终端的输入编码器），
不是 ConPTY 内核。所以要分开测两件事：

  T1  目标程序发 `CSI > 1 u` 后，ConPTY 是否把它**透传**给宿主？
      （若透传 ⇒ ConPTY 不"挡"这个序列，只是不自己兑现）
  T2  在 ConPTY 宿主侧（pywezterm.Terminal）应用 `>1u` 后，同一个按键
      编出来的字节是否变成 Kitty 格式？
      （这是问题的实质：编码器在终端一侧）

判据：
  - T1：目标写 `\x1b[>1u`，看宿主 read 到的字节里有没有它
  - T2：对 pywezterm.Terminal 喂 `\x1b[>1u`，再 key_down('a')，
        比较与未喂时的字节差异；同时对照 pyte 之类的第三方模型

用法：
    python kitty_kbd_layer_probe.py            # 全部跑
    python kitty_kbd_layer_probe.py --t2       # 只跑 T2（不需要项目产物）
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(
    PROJECT_ROOT, "tests", "vendor")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

try:
    import pywezterm
except ImportError as e:
    print("[SKIP] 无法 import pywezterm (PWTERM_DIR={!r}): {}".format(PWTERM_DIR, e))
    sys.exit(0)

COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")

CSI_GT_1U = b"\x1b[>1u"


def banner(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


# ---------------------------------------------------------------- T1
def t1_passthrough():
    """目标程序发 `>1u`，ConPTY 是否透传给宿主？"""
    banner("T1：目标发 `CSI > 1 u` → ConPTY 是否透传给宿主")

    cols, rows = 100, 24
    p = pywezterm.Pty(cols, rows)
    t = pywezterm.Terminal(cols, rows)

    # 目标：写一段带 >1u 的 VT，然后写一个可辨识标记
    script = (
        'import sys\n'
        'sys.stdout.write("\\x1b[>1u")\n'
        'sys.stdout.write("\\x1b[=1u")\n'
        'sys.stdout.write("MARK_AFTER_KITTY")\n'
        'sys.stdout.flush()\n'
        'import time; time.sleep(1.5)\n'
    )
    pid, _ = p.spawn([sys.executable, "-c", script])

    buf = b""
    deadline = time.time() + 8.0
    while time.time() < deadline:
        chunk = bytes(p.read(65536, timeout=0.2))
        if chunk:
            buf += chunk
            t.feed(chunk)
            resp = bytes(t.drain_written())
            if resp:
                p.write(resp)
        elif p.try_wait() is not None:
            while time.time() < deadline:
                c = bytes(p.read(65536, timeout=0.2))
                if not c:
                    break
                buf += c
                t.feed(c)
            break

    has_push = CSI_GT_1U in buf
    has_set = b"\x1b[=1u" in buf
    print("  宿主收到的总字节数      : {}".format(len(buf)))
    print("  含 `CSI > 1 u` (push)   : {}".format(has_push))
    print("  含 `CSI = 1 u` (set)    : {}".format(has_set))
    print("  含标记 MARK_AFTER_KITTY : {}".format(b"MARK_AFTER_KITTY" in buf))
    print("  字节样本(前120, repr)   : {!r}".format(buf[:120]))
    try:
        p.close()
    except Exception:
        pass

    print("\n  判读：")
    if has_push:
        print("  ⇒ ConPTY **透传**了 `>1u`（不拦截）。所以「ConPTY 不支持 >1u」")
        print("     这句话若成立，指的只能是「ConPTY 自己不兑现它」，而不是「挡住不让过」。")
    else:
        print("  ⇒ ConPTY **没有**把 `>1u` 交给宿主（被吞掉/拦截）。")
    return has_push


# ---------------------------------------------------------------- T2
def t2_encoder_layer():
    """按键编码在哪一层：终端侧应用 >1u 后，按键字节是否变成 Kitty 格式？"""
    banner("T2：按键编码发生在哪一层（ConPTY 内核 or 终端/宿主）")

    def probe(feed_kitty):
        t = pywezterm.Terminal(80, 24)
        if feed_kitty:
            t.feed(CSI_GT_1U)
        out = {}
        for key, mods in (("a", 0), ("Enter", 0), ("Up", 0), ("a", 8)):  # 8=CTRL
            try:
                d = bytes(t.key_down(key, mods))
                u = bytes(t.key_up(key, mods))
            except Exception as e:
                d = u = "ERR:{}".format(e).encode()
            out["{}|{}".format(key, mods)] = (d, u)
        try:
            bytes(t.drain_written())
        except Exception:
            pass
        return t, out

    t_plain, plain = probe(False)
    t_kitty, kitty = probe(True)

    print("  {:<12} {:<26} {:<26}".format("按键", "未启用 Kitty", "启用 `>1u` 之后"))
    changed = []
    for k in plain:
        a = plain[k][0]
        b = kitty[k][0]
        mark = "" if a == b else "   <== 变了"
        if a != b:
            changed.append(k)
        print("  {:<12} {!r:<26} {!r:<26}{}".format(k, a, b, mark))

    print("\n  终端侧是否自认进入了 Kitty 键盘模式：")
    for name, t in (("未启用", t_plain), ("启用后", t_kitty)):
        getters = [g for g in ("kitty_keyboard", "kitty_keyboard_flags",
                               "is_kitty_keyboard", "keyboard_encoding")
                   if hasattr(t, g)]
        vals = {}
        for g in getters:
            try:
                vals[g] = getattr(t, g)
                if callable(vals[g]):
                    vals[g] = vals[g]()
            except Exception as e:
                vals[g] = "ERR:{}".format(e)
        print("    {:<8} {}".format(name, vals or "(无该 getter)"))

    print("\n  判读：")
    if changed:
        print("  ⇒ 终端模型**自己**会根据 `>1u` 改变按键编码（变了 {} 项）。".format(len(changed)))
        print("     即：编码的责任在**终端/前端一侧**，不在 ConPTY 内核。")
    else:
        print("  ⇒ 这个终端模型**不因 `>1u` 改变按键编码**。")
    return changed


def main():
    only_t2 = "--t2" in sys.argv
    if not only_t2:
        t1_passthrough()
    t2_encoder_layer()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
