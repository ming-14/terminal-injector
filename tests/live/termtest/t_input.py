# -*- coding: utf-8 -*-
"""t_input.py — 按键输入路径检查

在原始模式下逐个按键, 显示终端实际送出的字节序列, 并与 xterm 规范自动对照。

覆盖:
  * 方向键 / Home / End / Insert / Delete / PageUp / PageDown / F1-F12 / Shift+Tab
  * 修饰组合: Ctrl / Shift / Alt 与方向键、字符键
  * Backspace(0x7F) 与 Ctrl+H(0x08) 的区分、Enter(CR) / Tab / Esc
  * DECCKM 应用光标键模式 (CSI ?1h)
  * 括号粘贴 (CSI ?2004h): 粘贴内容应被 ESC[200~ ... ESC[201~ 包裹
  * 焦点事件 (CSI ?1004h): 切走/切回窗口应收到 CSI O / CSI I

用法:
    python t_input.py            逐个按键
    python t_input.py --quick    只测基础键 (跳过粘贴/焦点)
"""

import argparse
import os
import sys
import time

import termlib as T

# 未登记按键/粘贴内容的原始字节默认不落盘不回显 —— 否则本程序等价于旁路键盘记录器
# (用户随手敲的任何键、误粘的口令都会被原样记走)。需要原始字节做协议分析时,
# 显式设 TERMTEST_RAW_KEYS=1 再跑。
RAW_KEYS = os.environ.get("TERMTEST_RAW_KEYS", "") not in ("", "0")

# (名称, 允许的规范编码, 名称前缀[用于识别"按了这个键但编码不同"], 说明)
CHECKS = [
    ("Up 上", [b"\x1b[A"], ["Up"], ""),
    ("Down 下", [b"\x1b[B"], ["Down"], ""),
    ("Right 右", [b"\x1b[C"], ["Right"], ""),
    ("Left 左", [b"\x1b[D"], ["Left"], ""),
    ("Home", [b"\x1b[H", b"\x1bOH", b"\x1b[1~"], ["Home"], "CSI H / SS3 H / CSI 1~ 都算规范"),
    ("End", [b"\x1b[F", b"\x1bOF", b"\x1b[4~"], ["End"], "CSI F / SS3 F / CSI 4~ 都算规范"),
    ("Insert", [b"\x1b[2~"], ["Insert"], ""),
    ("Delete", [b"\x1b[3~"], ["Delete"], ""),
    ("PageUp", [b"\x1b[5~"], ["PageUp"], ""),
    ("PageDown", [b"\x1b[6~"], ["PageDown"], ""),
    ("F1", [b"\x1bOP", b"\x1b[11~"], ["F1"], ""),
    ("F2", [b"\x1bOQ", b"\x1b[12~"], ["F2"], ""),
    ("F3", [b"\x1bOR", b"\x1b[13~"], ["F3"], ""),
    ("F4", [b"\x1bOS", b"\x1b[14~"], ["F4"], ""),
    ("F5", [b"\x1b[15~"], ["F5"], ""),
    ("F12", [b"\x1b[24~"], ["F12"], ""),
    ("Shift+Tab", [b"\x1b[Z"], ["Shift+Tab"], ""),
    ("Backspace", [b"\x7f", b"\x08"], ["Backspace", "Ctrl+H"], "0x7F 是现代规范, 0x08 是传统写法"),
    ("Enter", [b"\r"], ["Enter"], "应收到 CR (0x0D)"),
    ("Tab", [b"\t"], ["Tab"], ""),
    ("Esc(单独)", [b"\x1b"], ["Esc"], "单独按 Esc, 终端不应把它卡住或吞掉"),
    ("Ctrl+A", [b"\x01"], ["Ctrl+A"], ""),
    ("Ctrl+C", [b"\x03"], ["Ctrl+C"], "原始模式下应能作为字节收到 (不是中断程序)"),
    ("Alt+x", [b"\x1bx", b"\x1b[120;3u"], ["Alt+x"], "传统为 ESC 前缀; kitty 协议为 CSI u 形式"),
    ("Ctrl+Left", [b"\x1b[1;5D"], ["Ctrl+Left"], ""),
    ("Ctrl+Shift+Left", [b"\x1b[1;6D"], ["Ctrl+Shift+Left"], ""),
    ("Alt+Left", [b"\x1b[1;3D"], ["Alt+Left"], ""),
]

# 本程序从不"开启"任何鼠标上报模式, 而且开篇与每节开始都会先发一遍关闭序列
# (termlib.mode_reset), 因此这里收到的每一条鼠标报文都是终端多送的 —— 它不只是一屏噪声:
# 报文和按键粘在一块时会拖累按键判定。记账在 termlib 里统一做, 收尾单列一行判定。


def mouse_run_text(evs):
    """把一批鼠标报文压成一行。逐条打印会在鼠标一动就刷屏, 把有用的行冲走。"""
    cnt = {}
    for ev in evs:
        k = "%s %s" % (ev["name"], ev["action"])
        cnt[k] = cnt.get(k, 0) + 1
    detail = " / ".join("%s×%d" % (k, v)
                       for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))
    if len(evs) == 1:
        pos = "@ (%d, %d)" % (evs[0]["x"], evs[0]["y"])
    else:
        pos = "@ (%d, %d)→(%d, %d)" % (evs[0]["x"], evs[0]["y"], evs[-1]["x"], evs[-1]["y"])
    return "鼠标 %d 条 [%s] %s  %s" % (len(evs), detail, pos, evs[0].get("enc", ""))


def take_noise(units):
    """把一批 unit 分成 (鼠标报文, 其它未登记输入), 鼠标的记账交给 termlib。"""
    mouse, other = [], []
    for u in units:
        ev = T.noise_mouse(u)
        if ev is not None:
            mouse.append(ev)
        else:
            other.append((T.key_name(u), u))
    return mouse, other


def find_entry(chunk, entries):
    """把收到的字节块对应到清单项。返回 (index, kind): kind 为 'ok' / 'diff'。"""
    for i, e in enumerate(entries):
        if chunk in e["accepted"]:
            return i, "ok"
    nm = T.key_name(chunk)
    for i, e in enumerate(entries):
        for alias in e["aliases"]:
            if nm.startswith(alias):
                return i, "diff"
    return None, None


def sec_basic(args):
    T.section("普通模式按键 — 请按下列键 (顺序随意, 按 q 提前结束)")
    entries = []
    for name, accepted, aliases, note in CHECKS:
        if args.quick and name in ("F12", "Ctrl+Shift+Left"):
            continue
        entries.append({"name": name, "accepted": accepted, "aliases": aliases,
                        "note": note, "got": None, "kind": None})

    cols = T.term_size()[0]
    T.wln(T.sgr(90) + "  清单: " + " / ".join(e["name"] for e in entries) + T.sgr(0))
    T.wln(T.sgr(90) + "  提示: 请逐个按上面的键; 也可以先把想测的按完再按 q 结束。" + T.sgr(0))
    T.wln("")

    events = []
    T.mode_reset()       # 本节不需要任何模式: 先清空, 收到的鼠标报文才一定算"未被请求"
    with T.Raw():
        T.drain(0.05)
        inp = T.InputReader()
        done = 0
        stop = False
        while not stop:
            remain = [e["name"] for e in entries if e["got"] is None]
            if not remain:
                T.wln(T.sgr(32) + "  清单已全部收集完成。" + T.sgr(0))
                break
            units = inp.read(timeout=0.3)
            if not units:
                if not T.input_alive():
                    break
                continue
            # 一块里可能挤着好几个按键 + 一堆鼠标报文: 按键逐条判 (粘在一起也不会漏),
            # 鼠标报文压成一行。
            hits, noise = [], []
            for unit in units:
                if unit == b"q":
                    stop = True
                    break
                i, kind = find_entry(unit, entries)
                if i is None:
                    noise.append(unit)
                else:
                    hits.append((i, kind, unit))
            mouse, other = take_noise(noise)
            for i, kind, unit in hits:
                e = entries[i]
                nm = T.key_name(unit)
                if e["got"] is None:
                    e["got"] = unit
                    e["kind"] = kind
                    done += 1
                    color = "32" if kind == "ok" else "33"
                    T.wln("  " + T.sgr(color) + ("✓ %-16s" % e["name"]) + T.sgr(0)
                          + T.pad(nm, 26) + T.sgr(90) + T.hexdump(unit) + T.sgr(0)
                          + ("   [非规范编码]" if kind == "diff" else ""))
                else:
                    T.wln("  重复: " + T.pad(nm, 26) + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
                T.wln(T.sgr(90) + "     进度 %d/%d" % (done, len(entries)) + T.sgr(0))
            if mouse:
                T.wln("  其他: " + mouse_run_text(mouse))
            for nm, unit in other:
                T.wln("  其他: " + T.pad(nm, 30) + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
            events.extend(other)
            if stop:
                break

    # ---- 结算 ----
    T.wln("")
    T.divider("收集结果")
    ok_cnt = diff_cnt = miss_cnt = 0
    for e in entries:
        if e["got"] is None:
            miss_cnt += 1
            T.wln("  " + T.sgr(90) + "[--  ]" + T.sgr(0) + " " + T.pad(e["name"], 16)
                  + T.sgr(90) + "未按到, 跳过" + T.sgr(0))
        elif e["kind"] == "ok":
            ok_cnt += 1
            T.wln("  " + T.sgr(32) + "[ OK ]" + T.sgr(0) + " " + T.pad(e["name"], 16)
                  + T.hexdump(e["got"]))
        else:
            diff_cnt += 1
            T.wln("  " + T.sgr(33) + "[差异]" + T.sgr(0) + " " + T.pad(e["name"], 16)
                  + T.hexdump(e["got"]) + T.sgr(90) + "  期望 " +
                  " 或 ".join(T.hexdump(a) for a in e["accepted"]) + T.sgr(0))

    detail = "规范 %d, 非规范 %d, 未测 %d" % (ok_cnt, diff_cnt, miss_cnt)
    T.row("基础按键编码", T.PASS if diff_cnt == 0 else T.DIFF, detail)
    for e in entries:
        if e["got"] is not None and e["kind"] == "diff":
            T.row("按键差异 %s" % e["name"], T.DIFF,
                  "收到 %s, 期望 %s" % (T.hexdump(e["got"]),
                                      " / ".join(T.hexdump(a) for a in e["accepted"])))
    if events:
        T.hint("另有 %d 个未登记的按键被记录到报告: %s"
               % (len(events), ", ".join(n for n, _ in events[:6])))
    return entries, events


def sec_decckm():
    T.section("DECCKM 应用光标键 — CSI ?1h 后方向键应变成 SS3 形式")
    got = {}
    T.wln(T.sgr(90) + "  已进入应用光标键模式, 请按 ↑ ↓ ← → 四个方向键。" + T.sgr(0))
    # mode_scope: 进入先清空全部模式再开 ?1h, 退出 (含异常) 再清空
    with T.mode_scope(on=[1]):
        with T.Raw():
            T.drain(0.05)
            inp = T.InputReader()
            deadline = time.monotonic() + 20
            while len(got) < 4 and time.monotonic() < deadline:
                units = inp.read(timeout=0.3)
                if not units:
                    if not T.input_alive():
                        break
                    continue
                noise = []
                stop = False
                for unit in units:
                    if unit == b"q":
                        stop = True
                        break
                    if unit in (b"\x1bOA", b"\x1bOB", b"\x1bOC", b"\x1bOD"):
                        got[unit] = T.key_name(unit)
                        T.wln("  " + T.pad(T.key_name(unit), 28)
                              + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
                    else:
                        noise.append(unit)
                mouse, other = take_noise(noise)
                if mouse:
                    T.wln("  " + mouse_run_text(mouse))
                for nm, unit in other:
                    T.wln("  " + T.pad(nm, 28) + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
                if stop:
                    break

    if len(got) == 4:
        T.row("DECCKM 应用光标键", T.PASS, "四个方向键均为 SS3 形式 (ESC O A/B/C/D)")
    elif got:
        T.row("DECCKM 应用光标键", T.DIFF,
              "只收到 %d/4 个 SS3 编码; 收到: %s" % (len(got), ", ".join(T.hexdump(k) for k in got)))
    else:
        T.row("DECCKM 应用光标键", T.SKIP, "未收到 SS3 编码 (终端可能忽略 CSI ?1h)")


def sec_paste():
    T.section("括号粘贴 — CSI ?2004h 后粘贴内容应被包裹")
    T.wln(T.sgr(90) + "  请复制下面这 3 行并粘贴进来 (Ctrl+V 或右键粘贴):" + T.sgr(0))
    T.wln(T.bg(236) + "行1\t带制表符" + T.sgr(0))
    T.wln(T.bg(236) + "行2 中文与 emoji 🎉" + T.sgr(0))
    T.wln(T.bg(236) + "行3 结束" + T.sgr(0))
    T.wln("")

    data = None
    typed = bytearray()          # 没有包裹就直接送来的内容 (用来区分"没粘"和"粘了但没包")
    inp = T.InputReader()
    # mode_scope 负责 ?2004h 与退出时的清空 (含异常路径)
    with T.mode_scope(on=[2004]):
        with T.Raw():
            T.drain(0.05)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and data is None:
                units = inp.read(timeout=0.5)
                if not units:
                    if not T.input_alive():
                        break
                    continue
                noise, stop = [], False
                for unit in units:
                    if unit == b"q":
                        stop = True
                        break
                    if unit.startswith(b"\x1b[200~"):
                        data = unit          # 切分器保证这一条同时带 200~ 和 201~
                        break
                    if unit == b"\x1b[201~":
                        continue             # 只收到结束标记, 不算内容
                    noise.append(unit)
                mouse, other = take_noise(noise)
                for nm, unit in other:
                    typed += unit            # 可能是"终端直接把内容送来, 没做包裹"
                if mouse:
                    T.wln("  (收到鼠标报文 %d 条, 与本项无关, 请继续粘贴)" % len(mouse))
                elif other:
                    T.wln("  (收到非粘贴按键: %s, 请继续粘贴)" % other[0][0])
                if stop:
                    break

    if data is None:
        rest = inp.incomplete
        if rest.startswith(b"\x1b[200~"):
            T.row("括号粘贴包裹", T.DIFF,
                  "只有开始标记 ESC[200~: 之后 %d 字节都没有结束标记 ESC[201~"
                  % (len(rest) - len(b"\x1b[200~")))
        elif "行1".encode("utf-8") in bytes(typed):
            T.row("括号粘贴包裹", T.DIFF,
                  "收到 %d 字节粘贴内容, 但完全没有 ESC[200~ / ESC[201~ 包裹"
                  % len(typed))
        else:
            T.row("括号粘贴包裹", T.SKIP, "未检测到粘贴 (未粘贴或终端不实现 2004)")
        return
    inner = data[len(b"\x1b[200~"):]
    if inner.endswith(b"\x1b[201~"):
        inner = inner[:-len(b"\x1b[201~")]
    wrapped = data.startswith(b"\x1b[200~") and data.endswith(b"\x1b[201~")
    # 内容不回显: 用户可能误粘口令。默认只记字节数, 是否与示例一致用字节匹配判定;
    # 设 TERMTEST_RAW_KEYS=1 才在判定行里带前 60 字符预览。
    sample_hit = "行1".encode("utf-8") in inner
    if wrapped:
        detail = "首尾标记完整, 内容 %d 字节" % len(inner)
        if RAW_KEYS:
            detail += ": %r" % inner.decode("utf-8", "replace")[:60].replace("\n", "\\n").replace("\t", "\\t")
        T.row("括号粘贴包裹", T.PASS, detail)
        if not sample_hit:
            T.row("括号粘贴内容", T.WARN, "内容与提示的示例不同 (可能粘贴了别的内容)")
        else:
            T.row("括号粘贴内容", T.PASS, "内容与示例一致 (制表符/换行未被拆分)")
    else:
        T.row("括号粘贴包裹", T.DIFF,
              "收到 %d 字节但首尾标记不全 (内容不回显)" % len(data))


def sec_focus():
    T.section("焦点事件 — CSI ?1004h 后窗口获得/失去焦点应上报")
    seen = {"in": None, "out": None}
    T.wln(T.sgr(90) + "  请在 10 秒内: 点击其它窗口 (失焦) 再点回本窗口 (获焦)。" + T.sgr(0))
    inp = T.InputReader()
    with T.mode_scope(on=[1004]):        # ?1004h 与退出时的清空都由它负责
        with T.Raw():
            T.drain(0.05)
            deadline = time.monotonic() + 10
            stop = False
            while time.monotonic() < deadline and not (seen["in"] and seen["out"]):
                units = inp.read(timeout=0.3)
                if not units:
                    if not T.input_alive():
                        break
                    continue
                noise = []
                for unit in units:
                    if unit == b"\x1b[O":
                        seen["out"] = unit
                        T.wln("  (焦点离开)" + T.sgr(90) + " " + T.hexdump(unit) + T.sgr(0))
                    elif unit == b"\x1b[I":
                        seen["in"] = unit
                        T.wln("  (焦点进入)" + T.sgr(90) + " " + T.hexdump(unit) + T.sgr(0))
                    elif unit == b"q":
                        stop = True
                        break
                    else:
                        noise.append(unit)
                mouse, other = take_noise(noise)
                if mouse:
                    T.wln("  " + mouse_run_text(mouse))
                for nm, unit in other:
                    T.wln("  " + T.pad(nm, 28) + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
                if stop:
                    break
    if seen["in"] and seen["out"]:
        T.row("焦点事件 (1004)", T.PASS, "收到焦点进入 ESC[I 与离开 ESC[O")
    elif seen["in"] or seen["out"]:
        T.row("焦点事件 (1004)", T.DIFF, "只收到一种: %s" % seen)
    else:
        T.row("焦点事件 (1004)", T.SKIP, "未收到焦点事件 (终端可能不实现 1004)")


def build_report(entries, events, extra):
    lines = ["t_input.py 报告",
             "时间: " + time.strftime("%Y-%m-%d"),
             "终端: %s" % "  ".join(T.term_info()["objs"]),
             "",
             "== 按键对照表 ==",
             "%-20s %-26s %s" % ("按键", "收到", "判定")]
    for e in entries:
        got = T.hexdump(e["got"]) if e["got"] else "-"
        st = "-" if e["got"] is None else ("规范" if e["kind"] == "ok" else "非规范")
        exp = " / ".join(T.hexdump(a) for a in e["accepted"])
        lines.append("%-20s %-26s %s   期望: %s" % (e["name"], got, st, exp))
    if events:
        lines.append("")
        lines.append("== 未登记按键 ==")
        if RAW_KEYS:
            for nm, chunk in events:
                lines.append("%-30s %s" % (nm, T.hexdump(chunk)))
        else:
            # 默认只记按键名: 原始字节等价于用户击键记录, 不落盘
            lines.append("(默认只记按键名, 原始字节不落盘; 设 TERMTEST_RAW_KEYS=1 开启)")
            for nm, _chunk in events:
                lines.append("  %s" % nm)
    lines.append("")
    lines.append("== 未被请求的鼠标报文 ==")
    ns = T.noise_stats()
    if ns["mouse"]:
        lines.append("共 %d 条 / %d 字节 — 本程序从不开启鼠标上报, 且开篇与每节开始都先发过一遍"
                     "关闭序列 (?1000l ?1002l ?1003l ?1005l ?1006l ?1015l ?1016l), 终端不应上报"
                     % (ns["mouse"], ns["bytes"]))
        for k, v in sorted(ns["kinds"].items(), key=lambda kv: -kv[1]):
            lines.append("  %-30s ×%d" % (k, v))
        lines.append("  编码: %s" % ", ".join(sorted(ns["encs"])))
        lines.append("  首条: %s" % ns["first"])
        lines.append("  末条: %s" % ns["last"])
    else:
        lines.append("(无)")
    lines.append("")
    lines.append("== 其它原始记录 ==")
    for k, v in extra:
        lines.append("%-30s %s" % (k, T.dump_bytes(v) if v else "(无)"))
    return "\n".join(lines) + "\n"


def _finish(entries, events):
    """收尾: 记"未请求的鼠标上报"判定, 并把按键对照报告落盘。

    由 main 放在 finally 里调用 —— 中途按 q/Esc/Ctrl+C 退出时, 已经采到的按键
    也要留下报告, 不然整轮按键白按。
    """
    # 本程序从不开启鼠标上报 (开篇与每节开始还各发过一遍关闭序列), 收到的都算终端多送的。
    T.noise_row("鼠标上报未被请求")

    p = T.save_report("input", build_report(entries, events, []))
    if p:
        T.hint("按键对照报告已写入: %s" % T.safe_path(p))


def main():
    ap = argparse.ArgumentParser(description="按键输入路径检查")
    ap.add_argument("--quick", action="store_true", help="少测几个键")
    args = ap.parse_args()

    T.start("t_input.py", ["方向/功能键", "修饰组合", "DECCKM", "括号粘贴 2004",
                           "焦点事件 1004", "未请求的鼠标上报"],
            note="逐个按键, 记录终端实际送出的字节并与 xterm 规范对照; 报告可保存用于和 WT 对照")

    if not T.INPUT_OK or T.NONINTERACTIVE:
        if T.NONINTERACTIVE:
            T.note("非交互模式: 打印按键编码对照表后结束 (不做实际按键测试)")
        else:
            T.note("当前 stdin 不是可读终端, 无法检查按键。请在终端窗口里直接运行本程序。")
        entries = []
        for name, accepted, aliases, note in CHECKS:
            entries.append({"name": name, "accepted": accepted, "aliases": aliases,
                            "note": note, "got": None, "kind": None})
            T.wln("  " + T.pad(name, 18) + T.sgr(90) + "期望编码: "
                  + " / ".join(T.hexdump(a) for a in accepted) + T.sgr(0))
        for nm in ("基础按键编码", "DECCKM 应用光标键", "括号粘贴", "焦点事件"):
            T.row(nm, T.SKIP, "未做交互测试")
        p = T.save_report("input", build_report(entries, [], []))
        if p:
            T.hint("按键对照表已写入: %s" % T.safe_path(p))
        T.finish()
        return 0

    entries, events = [], []
    try:
        entries, events = sec_basic(args)
        T.wait_key("基础按键测完了, 按任意键继续测 DECCKM")
        sec_decckm()
        T.wait_key("按任意键继续测括号粘贴")
        sec_paste()
        T.wait_key("按任意键继续测焦点事件")
        sec_focus()
    finally:
        _finish(entries, events)
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))