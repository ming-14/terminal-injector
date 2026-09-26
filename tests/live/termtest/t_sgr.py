# -*- coding: utf-8 -*-
"""t_sgr.py — 文本样式与颜色兼容性测试

本程序检验终端模拟器对 SGR (CSI ... m, Select Graphic Rendition) 的支持范围:

  1. 16 色 ANSI: 前景 30-37 / 亮色 90-97; 背景 40-47 / 亮色 100-107; 每格标注 SGR 编号
  2. 256 色: 系统色 0-15 / 6x6x6 彩色立方 16-231 / 24 级灰阶 232-255
  3. 真彩色 24bit 三种写法: 38;2;r;g;b、38;2;r:g:b、38:2::r:g:b (背景 48 同理), 各画渐变条
  4. 全部文本属性: 1 2 3 4 5 6 7 8 9 21 51 52 53 73 74 (默认 / 应用后 / 恢复 三连对照)
  5. 下划线变体 4:2 / 4:3 / 4:4 / 4:5 与下划线颜色 58;5;n / 58;2;r;g;b / 取消 59
  6. 多属性叠加与逐个关闭 22 23 24 27 39 49
  7. 一次发送多属性 CSI 1;3;4;31;44 m 与逐个单发的效果一致性
  8. SGR 0 复位是否彻底

非交互模式 (TERMTEST_NONINTERACTIVE=1): 所有等待自动跳过, 可安全冒烟运行。
"""

import sys
import termlib as T

SAMPLE = "样例Ab"


# ---------------------------------------------------------------- 通用助手
def _cols():
    c = T.term_size()[0]
    return c if c > 0 else 80


def _cue(text):
    """小节之间的观察确认 (非交互模式自动跳过)。"""
    T.wait_key(text + " — 按任意键继续")


def _ask_row(name, question, detail=""):
    """提出 y/n 问题并判定; 非交互模式记 SKIP。"""
    r = T.ask_yn(question)
    if r is None:
        T.row(name, T.SKIP, detail or "非交互模式 / 未作答")
    else:
        T.row(name, T.PASS if r else T.FAIL, detail or question)


def _row_codes(mk, codes, fmt):
    line = "  "
    for c in codes:
        line += mk(c) + (fmt % c) + " " + T.sgr(0) + " "
    T.wln(line)


# ---------------------------------------------------------------- 16 色
def part_16():
    T.section("16 色 ANSI: 前景 30-37 / 90-97, 背景 40-47 / 100-107")
    T.hint("要观察: 每个色块的实际颜色, 以及色块内标注的 SGR 编号是否与之对应")
    T.wln("")
    T.wln(T.sgr(1) + "前景 标准色 30-37:" + T.sgr(0))
    _row_codes(lambda c: T.sgr(c), range(30, 38), "██ %d")
    T.wln(T.sgr(1) + "前景 亮色   90-97:" + T.sgr(0))
    _row_codes(lambda c: T.sgr(c), range(90, 98), "██ %d")
    T.wln("")
    T.wln(T.sgr(1) + "背景 标准色 40-47:" + T.sgr(0))
    _row_codes(lambda c: T.sgr(c), range(40, 48), " %d ")
    T.wln(T.sgr(1) + "背景 亮色   100-107:" + T.sgr(0))
    _row_codes(lambda c: T.sgr(c), range(100, 108), " %d ")
    T.wln("")
    T.hint("前景 + 背景 同时使用 (前景 30+i / 背景 40+i 对角线):")
    line = "  "
    for i in range(4):
        line += T.sgr(37 - i, 40 + i) + " %d/%d " % (37 - i, 40 + i) + T.sgr(0) + " "
    T.wln(line)
    T.wln("")
    _ask_row("16色可区分", "16 色 (标准 8 + 亮 8) 是否都能正确区分?")
    _cue("观察完 16 色")


# ---------------------------------------------------------------- 256 色
def part_256():
    T.section("256 色: 系统色 0-15 / 6x6x6 立方 16-231 / 灰阶 232-255")
    T.hint("要观察: 0-15 通常随主题变化; 16-231 应形成平滑彩色立方; 232-255 应为 24 级灰")
    T.wln("")
    for start in (0, 8):
        ln = "  系统色 %2d-%2d: " % (start, start + 7) if start == 0 else "             "
        for i in range(start, start + 8):
            ln += T.bg(i) + " %2d " % i + T.sgr(0) + " "
        T.wln(ln)
    T.wln("")
    T.wln(T.sgr(1) + "  6x6x6 彩色立方 (每行 6 格 = b 分量 0-5; 行首 [r g] 为立方下标):" + T.sgr(0))
    for r in range(6):
        for g in range(6):
            base = 16 + 36 * r + 6 * g
            ln = "  %3d [%d%d] " % (base, r, g)
            for b in range(6):
                ln += T.bg(base + b) + "  " + T.sgr(0)
            T.wln(ln)
    T.wln("")
    T.wln(T.sgr(1) + "  灰阶 232-255:" + T.sgr(0))
    ln = "  "
    cnt = 0
    for i in range(232, 256):
        ln += T.bg(i) + " %d " % i + T.sgr(0)
        cnt += 1
        if cnt % 12 == 0:
            T.wln(ln)
            ln = "  "
    if ln.strip():
        T.wln(ln)
    T.wln("")
    T.hint("前景 256 色示例 38;5;n: "
           + T.fg(196) + "196 红" + T.sgr(0) + "  "
           + T.fg(46) + "46 绿" + T.sgr(0) + "  "
           + T.fg(226) + "226 黄" + T.sgr(0) + "  "
           + T.fg(21) + "21 蓝" + T.sgr(0) + "  "
           + T.fg(208) + "208 橙" + T.sgr(0) + "  "
           + T.fg(93) + "93 紫" + T.sgr(0))
    T.wln("")
    _ask_row("256色可区分", "256 色块是否都能区分且无错位 (立方与灰阶过渡自然)?")
    _cue("观察完 256 色")


# ---------------------------------------------------------------- 真彩色
def _rgb_at(t):
    r = int(round(255 * t))
    g = int(round(255 * (1 - abs(2 * t - 1))))
    b = int(round(255 * (1 - t)))
    return (r, g, b)


def _bar_width():
    return min(64, max(16, _cols() - 8))


def _gradient_fg(mk, label):
    w = _bar_width()
    bar = ""
    for x in range(w):
        t = x / float(w - 1) if w > 1 else 0.0
        r, g, b = _rgb_at(t)
        bar += mk(r, g, b) + "█"
    T.wln("  " + T.pad(label, 24) + bar + T.sgr(0))


def _gradient_bg(mk, label):
    w = _bar_width()
    bar = ""
    for x in range(w):
        t = x / float(w - 1) if w > 1 else 0.0
        r, g, b = _rgb_at(t)
        bar += mk(r, g, b) + " "
    T.wln("  " + T.pad(label, 24) + bar + T.sgr(0))


def part_truecolor():
    T.section("真彩色 24bit: 分号式 / 冒号式 / 冒号带空保留位")
    T.hint("要观察: 三条前景渐变条应完全一致; 某写法被忽略时该条会变成默认色或错色")
    T.wln("")
    _gradient_fg(lambda r, g, b: T.CSI + "38;2;%d;%d;%dm" % (r, g, b), "CSI 38;2;r;g;b m")
    _gradient_fg(lambda r, g, b: T.CSI + "38;2;%d:%d:%dm" % (r, g, b), "CSI 38;2;r:g:b m")
    _gradient_fg(lambda r, g, b: T.CSI + "38:2::%d:%d:%dm" % (r, g, b), "CSI 38:2::r:g:b m")
    T.wln("")
    T.hint("背景 48 同理, 三条背景渐变条应一致:")
    _gradient_bg(lambda r, g, b: T.CSI + "48;2;%d;%d;%dm" % (r, g, b), "CSI 48;2;r;g;b m")
    _gradient_bg(lambda r, g, b: T.CSI + "48;2;%d:%d:%dm" % (r, g, b), "CSI 48;2;r:g:b m")
    _gradient_bg(lambda r, g, b: T.CSI + "48:2::%d:%d:%dm" % (r, g, b), "CSI 48:2::r:g:b m")
    T.wln("")
    _ask_row("真彩色渐变", "前景/背景各三种真彩写法是否颜色平滑且相互一致?")
    _cue("观察完真彩色")


# ---------------------------------------------------------------- 文本属性
ATTRS = [
    (1, "加粗", ""),
    (2, "弱化", ""),
    (3, "斜体", ""),
    (4, "下划线", ""),
    (5, "慢闪", "部分终端忽略闪烁"),
    (6, "快闪", "部分终端忽略闪烁"),
    (7, "反显", ""),
    (8, "隐藏", "应用后应看不到文字"),
    (9, "删除线", ""),
    (21, "双下划线", "支持度较低"),
    (51, "边框", "支持度较低"),
    (52, "环绕", "支持度较低"),
    (53, "上划线", "支持度较低"),
    (73, "上标", "位置应上移"),
    (74, "下标", "位置应下移"),
]


def part_attrs():
    T.section("文本属性逐个演示 (默认 / 应用后 / 恢复 三连对照)")
    T.hint("要观察: 「应用后」相对「默认」的变化; 「恢复」应与「默认」完全一致 (已 SGR 0 复位)")
    T.wln("")
    for code, name, extra in ATTRS:
        head = T.sgr(1) + T.pad("SGR %d" % code, 7) + T.sgr(0) + T.pad(name, 10)
        line = ("  " + head
                + "默认:" + SAMPLE
                + "  " + T.sgr(90) + "应用后:" + T.sgr(0) + T.sgr(code) + SAMPLE + T.sgr(0)
                + "  " + T.sgr(90) + "恢复:" + T.sgr(0) + SAMPLE)
        T.wln(line)
        if extra:
            T.wln("      " + T.sgr(33) + "! " + extra + T.sgr(0))
    T.wln("")
    _ask_row("属性-隐藏", "SGR 8 隐藏项的「应用后」文字是否确实不可见?")
    _ask_row("属性-反显", "SGR 7 反显项的前景色与背景色是否确实互换?")
    _ask_row("属性-删除线", "SGR 9 删除线是否可见 (文字中间横线)?")
    _cue("观察完文本属性")


# ---------------------------------------------------------------- 下划线变体
def part_underline():
    T.section("下划线变体 (4:2 / 4:3 / 4:4 / 4:5) 与下划线颜色 (58 / 59)")
    T.hint("要观察: 线型差异 (双线 / 波浪 / 点线 / 虚线); 不支持时通常降级为普通实线")
    T.wln("")
    for suf, label in (("4:2", "双线"), ("4:3", "波浪"), ("4:4", "点线"), ("4:5", "虚线")):
        seq = T.CSI + suf + "m"
        T.wln("  " + T.pad("CSI %s m" % suf, 14) + T.pad(label, 8)
              + "默认:" + "下划线样例文本"
              + "   应用后:" + seq + "下划线样例文本" + T.sgr(0))
    T.wln("")
    T.hint("下划线颜色 58;5;n (需先开启 4 下划线):")
    T.wln("  " + T.sgr(4) + T.CSI + "58;5;196m" + "红下划线" + T.sgr(0)
          + "   " + T.sgr(4) + T.CSI + "58;5;46m" + "绿下划线" + T.sgr(0)
          + "   " + T.sgr(4) + T.CSI + "58;5;226m" + "黄下划线" + T.sgr(0))
    T.wln("  " + T.sgr(4) + T.CSI + "58;2;214;39;40m" + "58;2;r;g;b 真彩下划线" + T.sgr(0))
    T.wln("")
    T.hint("取消 59: 先彩色下划线, 再用 59 取消颜色, 下划线本身应保留 (变回默认色)")
    T.wln("  " + T.sgr(4) + T.CSI + "58;5;196m" + "彩色下划线" + T.CSI + "59m"
          + "→ 59 之后(应为默认色的下划线)" + T.sgr(0))
    T.wln("")
    _ask_row("下划线-波浪", "SGR 4:3 波浪下划线是否可见 (且与普通实线不同)?")
    _ask_row("下划线-颜色", "58;5;n / 58;2;r;g;b 是否让下划线变色, 且 59 后回到默认色?")
    _cue("观察完下划线变体")


# ---------------------------------------------------------------- 叠加
def part_combine():
    T.section("多属性叠加与逐个关闭 (22 23 24 27 39 49)")
    T.hint("要观察: 起始为「加粗+斜体+下划线+反显+红字蓝底」, 随后每关一项, 对应效果消失且其余保留")
    T.wln("")
    T.wln("  全部开启: " + T.sgr(1, 3, 4, 7, 31, 44) + "加粗+斜体+下划线+反显+红字蓝底" + T.sgr(0))
    T.wln("")
    chain = (T.sgr(1, 3, 4, 7, 31, 44) + "起始"
             + T.sgr(22) + " 关22"
             + T.sgr(23) + " 关23"
             + T.sgr(24) + " 关24"
             + T.sgr(27) + " 关27"
             + T.sgr(39) + " 关39"
             + T.sgr(49) + " 关49(应完全回默认)")
    T.wln("  逐步关闭: " + chain + T.sgr(0))
    T.wln("")
    T.hint("关闭码含义: 22=去加粗/弱化  23=去斜体  24=去下划线  27=去反显  39=去前景  49=去背景")
    T.wln("")
    _ask_row("叠加-逐个关闭", "逐个关闭时是否每次都只去掉对应一项而其余保留?")
    _cue("观察完叠加")


def part_multi_send():
    T.section("一次发送多属性 vs 逐个单发 (视觉一致性)")
    T.hint("要观察: 下面两行文本外观应完全相同")
    T.wln("")
    a = (T.sgr(1) + T.sgr(3) + T.sgr(4) + T.sgr(31) + T.sgr(44)
         + "逐个单发: 加粗/斜体/下划线/红字/蓝底" + T.sgr(0))
    b = T.sgr(1, 3, 4, 31, 44) + "一次多属性: 加粗/斜体/下划线/红字/蓝底" + T.sgr(0)
    T.wln("  " + a)
    T.wln("  " + b)
    T.wln("")
    T.hint("原始序列: 单发 = ESC[1m ESC[3m ESC[4m ESC[31m ESC[44m   |   合并 = ESC[1;3;4;31;44m")
    T.wln("")
    _ask_row("多属性一致", "两行文本的样式是否完全一致?")
    _cue("观察完多属性发送")


def part_reset():
    T.section("SGR 0 复位验证")
    T.hint("要观察: 应用一堆样式后发送 CSI 0 m, 之后文本应完全回到默认")
    T.wln("")
    T.write(T.sgr(1, 3, 4, 7, 9, 31, 44) + "带样式的文本" + T.sgr(0))
    T.wln("  ← 紧跟复位后的这段: " + SAMPLE + "  (应与普通文本无异)")
    T.wln("")
    T.hint("若复位后仍残留颜色 / 下划线 / 反显, 说明 SGR 0 未彻底复位")
    T.wln("")
    _ask_row("SGR0复位", "CSI 0 m 之后文本是否完全恢复默认样式?")


# ---------------------------------------------------------------- 入口
def main():
    T.start("t_sgr.py",
            ["16色", "256色", "真彩色", "文本属性", "下划线变体", "属性叠加", "SGR复位"],
            note="只检验 SGR 样式与颜色; 每小节之间按任意键继续")
    part_16()
    part_256()
    part_truecolor()
    part_attrs()
    part_underline()
    part_combine()
    part_multi_send()
    part_reset()
    T.finish()
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))