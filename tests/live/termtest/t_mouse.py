# -*- coding: utf-8 -*-
"""t_mouse.py — 鼠标协议检查

逐个开启鼠标上报模式, 把终端送来的鼠标事件解码显示, 并自动做结构与坐标判定。

覆盖:
  * CSI ?1000h  X10 兼容 (按下/释放, 无移动)
  * CSI ?1002h  拖拽上报 (button-event tracking)
  * CSI ?1003h  任意移动上报 (any-event tracking)
  * CSI ?1006h  SGR 编码 (现代标准, 坐标不受 223 限制)
  * CSI ?1005h  UTF-8 编码  /  CSI ?1015h urxvt 编码
  * 按钮 (左/中/右)、滚轮上下、修饰键 (Shift/Alt/Ctrl)、边界坐标 (1,1) 与 (cols,rows)

操作: 每个模式下点击/拖拽/滚轮; 按 Enter 进入下一个模式, 按 q 结束本节。
      收到的事件会在被点击的格子上打一个红 X, 便于目视核对坐标映射。

用法:
    python t_mouse.py
"""

import sys
import time

import termlib as T

PROTOS = [
    (["1000"], "1000 X10 兼容", "按下/释放 (无移动), 传统 X10 编码"),
    (["1002", "1006"], "1002+1006 拖拽 + SGR", "多数 TUI 程序使用的组合"),
    (["1003", "1006"], "1003+1006 任意移动 + SGR", "鼠标跟随/悬停类程序"),
    (["1002"], "1002 拖拽 + 传统编码", "对比 X10 编码的坐标上限与格式"),
    (["1002", "1005"], "1002+1005 UTF-8 编码", "老式宽坐标编码"),
    (["1002", "1015"], "1002+1015 urxvt 编码", "另一种十进制坐标编码"),
]


def set_proto(modes, on=True):
    """开关一组鼠标上报模式。

    开之前先 t.mode_reset(): 六种上报/编码模式是互斥的, 上一节没关掉的残留
    (或终端忽略了关闭序列) 会让本节的坐标与编码判定失真。

    注意 mode_reset 含 ?6l (DECOM 复位), 真机与终端模型都会把光标送回屏幕左上角
    (t_screen 的 t_decom 正是测这条)。所以调用方要在它之后再摆版面 —— 固定内容
    (引导、标尺) 都用 CUP 显式定位, 不要依赖"当前光标在哪"。
    """
    T.mode_reset()
    if on:
        T.write("".join(T.CSI + "?%sh" % m for m in modes))


def park_log_bottom():
    """把光标停到日志区底部的定位行 (只定位, 不清行)。

    set_proto() 会发 mode_reset (?6l), 把光标甩回屏幕左上角 —— 固定位置的内容都得自己
    定位。这里停在末行"上一行": 跟着写的判定行还要发一个换行, 落在末行刚好不触发滚动;
    直接停末行再换行会把标尺/引导整屏推走。也不用 T.park_cursor(): 它会顺手擦掉那一行,
    而那一行往往刚打印过一条事件, 擦了就少一条。
    """
    _cols, rows = T.term_size()
    T.write(T.cup(max(1, rows - 1), 1))


def draw_ruler():
    """画列标尺和行标尺, 便于目视核对坐标。"""
    cols, rows = T.term_size()
    T.write(T.CSI + "2J" + T.CSI + "H")
    T.write(T.sgr(90))
    line = "".join(("%-10d" % n)[:10] for n in range(1, cols, 10))
    T.write(line[:cols])
    for r in range(2, rows):
        if r % 5 == 0:
            T.write(T.cup(r, 1) + "%-4d" % r)
    T.write(T.sgr(0))
    T.write(T.cup(1, 1))


def mark(x, y, ch="X"):
    cols, rows = T.term_size()
    if 1 <= x <= cols and 1 <= y <= rows:
        T.write(T.cup(y, x) + T.sgr(1, 31) + ch + T.sgr(0))


def summary_of(events):
    """统计事件: 按钮/动作计数。"""
    cnt = {}
    for ev, _ in events:
        key = "%s%s" % (ev["name"], ev["action"])
        cnt[key] = cnt.get(key, 0) + 1
    return cnt


def free_play():
    T.section("各协议自由测试 — 点击 / 拖拽 / 滚轮, Enter 下一个模式, q 结束")
    all_stats = []
    for modes, title, desc in PROTOS:
        T.write(T.CSI + "2J" + T.CSI + "H")
        draw_ruler()
        T.wln(T.cup(2, 1) + T.sgr(1, 36) + "模式: %s" % title + T.sgr(0))
        T.wln(T.cup(3, 1) + T.sgr(90) + desc + "   |   现在请: 左键点击 / 拖动 / 滚轮; Enter=下一个, q=结束" + T.sgr(0))
        T.wln(T.cup(5, 1) + T.sgr(90) + "事件流:" + T.sgr(0))

        events = []
        cnt = {}
        T.set_title("t_mouse: " + title)
        set_proto(modes, True)
        try:
            with T.Raw():
                T.drain(0.05)
                inp = T.InputReader()
                row = 6
                deadline = time.monotonic() + 120
                stop = False
                while time.monotonic() < deadline and not stop:
                    # 按序逐条解析: 一次读到的块里往往粘着十几条报文 (拖拽/滚轮都是高频),
                    # 整块当一条解析必然失配, 那些事件就全丢了。
                    for unit in inp.read(timeout=0.3):
                        if unit in (b"\r", b"\n"):
                            stop = True
                            break
                        if unit == b"q":
                            return all_stats
                        ev = T.mouse_event(unit)
                        if ev is None:
                            continue
                        events.append((ev, unit))
                        mark(ev["x"], ev["y"])
                        row = row + 1
                        # 事件环 6..末行上一行, 末行上一行留给判定行 (版面固定, 互不侵占)
                        if row > T.term_size()[1] - 2:
                            row = 6
                        # 用 wln: 事件行必须以换行收尾, 否则下一条事件会接在这一行尾部
                        T.wln(T.cup(row, 1) + T.sgr(0) + T.pad(T.mouse_text(ev), 70)
                              + T.sgr(90) + T.hexdump(unit) + T.sgr(0))
                    if not T.input_alive():
                        break
            # 判定行固定落在日志区末行: 显式定位 —— 中途 set_proto 的复位已经把光标甩走了,
            # 没事件时更是停在左上角, 靠光标就会压在标尺和模式行上。
            park_log_bottom()
            cnt = summary_of(events)
            if events:
                detail = ", ".join("%s×%d" % (k, v)
                                   for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))
                T.row("鼠标 %s" % title, T.PASS,
                      "收到 %d 个事件: %s" % (len(events), detail[:90]))
            else:
                T.row("鼠标 %s" % title, T.SKIP, "未收到事件 (没有操作或终端不实现该模式)")
        finally:
            set_proto(modes, False)      # 复位鼠标模式 (含滚动区)
            T.set_title("t_mouse")
            park_log_bottom()            # 收尾也把光标放在底部, 别留在左上角
        all_stats.append((title, len(events), cnt))
    return all_stats


def guided_check(title, modes, instruction, match, expect_desc, timeout=20,
                 forbid=None, forbid_desc=""):
    """引导式单次操作 + 自动判定。match(ev) -> bool。

    版式: 第 1 行是引导、第 2 行是时限提示, 第 3 行起是事件区 (自己一个滚动区),
    判定行接在事件流后面。事件再多也只在事件区里滚, 压不到引导。

    顺序要紧: 先 set_proto() 再排版。set_proto 里的 mode_reset 含 ?6l (DECOM 复位),
    真机和终端模型都会把光标送回屏幕左上角 (t_screen 的 t_decom 就是测这条);
    引导若先写、事件行又是顺序追加, 复位之后第一批事件就正好落在引导那两行上。

    给了 forbid 时, 命中 match 之后还会多收 1.5 秒: 有些模式下"多出来的事件"是
    在正确事件之后才出现的 (例: 1000 模式不该有移动报文, 但拖拽时的移动紧跟按下)。
    """
    _cols, rows = T.term_size()
    log_top = 3 if rows > 3 else rows     # 事件区第一行; 窗口矮到放不下分区时只能挤在末行
    got = None
    bad = None
    set_proto(modes, True)
    if rows > log_top:
        # 事件区自成一个滚动区: 区内写满后只在区内上滚, 引导与提示固定不动。
        T.write(T.CSI + "%d;%dr" % (log_top, rows))
    T.write(T.CSI + "2J" + T.CSI + "H" + T.cup(1, 1))
    T.wln(T.sgr(1, 36) + instruction + T.sgr(0))
    T.wln(T.sgr(90) + "  (20 秒内操作; Enter 跳过, q 结束)" + T.sgr(0))
    T.write(T.cup(log_top, 1))
    try:
        with T.Raw():
            T.drain(0.05)
            inp = T.InputReader()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                units = inp.read(timeout=0.3)
                if not units:
                    if not T.input_alive():
                        break
                    continue
                done = False
                for unit in units:
                    if unit == b"q":
                        raise T.QuitTest()
                    if unit in (b"\r", b"\n"):
                        done = True
                        break
                    ev = T.mouse_event(unit)
                    if ev is None:
                        continue
                    T.wln("  " + T.sgr(90) + "收到: " + T.mouse_text(ev)
                          + "  " + T.hexdump(unit) + T.sgr(0))
                    if bad is None and forbid is not None and forbid(ev):
                        bad = ev
                    if got is None and match(ev):
                        got = ev
                        if forbid is None:
                            done = True
                            break
                        deadline = min(deadline, time.monotonic() + 1.5)
                if done:
                    break
        # 判定行也接在事件区里 (写在 finally 之前: 复位会把光标甩回左上角,
        # 那之后打判定行就压在引导上了)。
        if got is None:
            T.row(title, T.SKIP, "未收到符合预期的操作 (期望: %s)" % expect_desc)
        else:
            T.row(title, T.PASS, "收到: " + T.mouse_text(got))
        if bad is not None:
            T.row(title + " 多余事件", T.DIFF,
                  "不该出现: %s  (%s)" % (T.mouse_text(bad), forbid_desc))
    finally:
        set_proto(modes, False)      # 复位鼠标模式与事件区滚动区
        park_log_bottom()
    return got


def sec_guided():
    T.section("精确坐标 / 修饰键 / 滚轮 (SGR 1006)")
    modes = ["1002", "1006"]
    cols, rows = T.term_size()

    guided_check("鼠标 左上角坐标", modes,
                 "请点击窗口的「左上角」第一个字符格",
                 lambda ev: ev["x"] == 1 and ev["y"] == 1 and ev["name"] == "左键",
                 "(1,1) 左键按下")

    guided_check("鼠标 右下角坐标", modes,
                 "请点击窗口的「右下角」最后一个字符格",
                 lambda ev: ev["x"] == cols and ev["y"] == rows and ev["name"] == "左键",
                 "(%d,%d) 左键按下" % (cols, rows))

    guided_check("鼠标 中键", modes,
                 "请用「中键」(按下滚轮) 点击任意位置",
                 lambda ev: ev["name"] == "中键",
                 "中键按下")

    guided_check("鼠标 右键", modes,
                 "请用「右键」点击任意位置",
                 lambda ev: ev["name"] == "右键",
                 "右键按下")

    guided_check("鼠标 滚轮上", modes,
                 "请把滚轮「向上」滚动一格",
                 lambda ev: ev["name"] == "滚轮上",
                 "滚轮上事件")

    guided_check("鼠标 滚轮下", modes,
                 "请把滚轮「向下」滚动一格",
                 lambda ev: ev["name"] == "滚轮下",
                 "滚轮下事件")

    guided_check("鼠标 Ctrl+点击", modes,
                 "请按住 Ctrl 键再左键点击任意位置",
                 lambda ev: "Ctrl" in ev["mods"],
                 "mods 含 Ctrl (b & 16)")

    guided_check("鼠标 拖拽移动", ["1002", "1006"],
                 "请按住左键拖动一段距离 (应有「移动」事件)",
                 lambda ev: ev["motion"],
                 "带 32 位标记的移动事件")

    guided_check("鼠标 按键限制 (1000)", ["1000"],
                 "现在只开 1000 (X10 兼容): 请按住左键拖动, 期间不应有移动事件",
                 lambda ev: ev["name"] == "左键",
                 "1000 模式下只有按下/释放, 无移动",
                 forbid=lambda ev: ev["motion"],
                 forbid_desc="1000 是 X10 兼容模式, 只报按下/释放; 裸移动属于 1003")


def build_report(stats):
    lines = ["t_mouse.py 报告",
             "时间: " + time.strftime("%Y-%m-%d"),
             "终端: %s" % "  ".join(T.term_info()["objs"]),
             "窗口: %dx%d" % T.term_size(),
             "",
             "== 各协议事件统计 =="]
    for title, n, cnt in stats:
        lines.append("%-28s 事件 %-6d %s" % (title, n, cnt))
    lines.append("")
    lines.append("== 判定 ==")
    for name, status, detail in T.rows():
        lines.append("[%-4s] %-26s %s" % (status, name, detail))
    return "\n".join(lines) + "\n"


def main():
    T.start("t_mouse.py", ["1000/1002/1003", "1005/1006/1015 编码",
                           "按钮/滚轮/修饰键", "边界坐标"],
            note="开启鼠标上报后, 终端会把点击事件以字节形式送给程序; 本程序解码并核对坐标")

    if not T.INPUT_OK or T.NONINTERACTIVE:
        if T.NONINTERACTIVE:
            T.note("非交互模式: 只打印各协议说明后结束 (不做实际鼠标测试)")
        else:
            T.note("当前 stdin 不是可读终端, 无法接收鼠标事件。请在终端窗口里直接运行本程序。")
        for modes, title, desc in PROTOS:
            T.wln("  " + T.pad(title, 26) + T.sgr(90)
                  + "开启: " + " ".join("CSI ?%sh" % m for m in modes) + "  " + desc + T.sgr(0))
            T.row("鼠标 %s" % title, T.SKIP, "未做交互测试")
        T.finish()
        return 0

    T.wln(T.sgr(90) + "  说明: 开启 1002/1003 后, 正常的选择复制会暂时失效, 退出本程序即恢复。" + T.sgr(0))
    T.wait_key("按任意键开始鼠标测试")

    stats = []
    try:
        stats = free_play()
        T.wait_key("自由测试结束, 按任意键进入引导式精确检查")
        sec_guided()
    finally:
        # 中途 q/Esc 退出也把已经收到的事件统计留下, 别让刚才的点击/拖拽白做
        p = T.save_report("mouse", build_report(stats))
        if p:
            T.hint("鼠标测试报告已写入: %s" % T.safe_path(p))
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))