# -*- coding: utf-8 -*-
"""t_unicode.py — Unicode 宽度与渲染兼容性测试

本程序用带 `|` 边框的标尺模板逐行比对, 检验终端模拟器对 Unicode 的宽度计算与渲染:

  1. 宽字符: 纯 ASCII / 1-3 个 CJK / 中英混排 / 全角标点
  2. 边界: 屏幕最后一列的宽字符、宽字符 + 组合字符、覆盖宽字符 (不得半格残留)
  3. emoji: BMP 符号、非 BMP emoji、VS16、ZWJ 家族、旗帜、肤色、keycap
  4. 组合字符: NFC / NFD、多重音标、泰文 / 天城文、行首孤立组合字符、韩文 jamo
  5. 零宽字符: ZWJ / U+200B / LRM / RLM / BOM
  6. 框线与块元素: 单双线框无缝性、圆角、块渐变、盲文旋转、几何与数学符号
  7. powerline 私用区 (U+E0B0 等) 与 RTL 文本 (阿拉伯语 / 希伯来语)
  8. 收尾输出「宽度对照表」并列代表字符的 east_asian_width, 同时保存报告到 termtest/_reports/

判断依据: 每一行都套在 `| ... |` 里并与刻度尺对齐, 右侧边框是否对齐即可看出宽度是否正确。

非交互模式 (TERMTEST_NONINTERACTIVE=1): 所有等待自动跳过, 可安全冒烟运行。
"""

import sys
import unicodedata
import termlib as T

WIDTH = 30           # 标尺内容区宽度 (显示列)


# ---------------------------------------------------------------- 通用助手
def _size():
    c, r = T.term_size()
    return (c if c > 0 else 80, r if r > 0 else 24)


def _ruler(width=WIDTH):
    """带 | 边框的刻度尺: 每 10 列标数字, 每 5 列标 +。"""
    body = ""
    for i in range(1, width + 1):
        if i % 10 == 0:
            body += str((i // 10) % 10)
        elif i % 5 == 0:
            body += "+"
        else:
            body += "-"
    return "|" + body + "|"


def _line(text, width=WIDTH):
    """带 | 边框的内容行, 右侧按显示宽度补齐。"""
    return "|" + T.pad(text, width) + "|"


def _label(text, w=20):
    return T.pad(text, w)


def _sample(label, text, width=WIDTH, want=None):
    exp = T.dwidth(text)
    tail = "  码点累加 %d" % exp
    if want is not None:
        tail += " / 字素应为 %d" % want
    T.wln("  " + _label(label) + _line(text, width) + T.sgr(90) + tail + T.sgr(0))


def _head():
    T.wln("  " + _label("样本") + _ruler(WIDTH) + T.sgr(90) + "  宽度说明" + T.sgr(0))


def _ask_row(name, question, detail=""):
    r = T.ask_yn(question)
    if r is None:
        T.row(name, T.SKIP, detail or "非交互模式 / 未作答")
    else:
        T.row(name, T.PASS if r else T.FAIL, detail or question)


def _cue(text):
    T.wait_key(text + " — 按任意键继续")


# ---------------------------------------------------------------- 1. 宽字符
def part_width():
    T.section("宽字符宽度: ASCII / CJK / 中英混排 / 全角标点")
    T.hint("要观察: 每行右侧的 | 是否与刻度尺的 | 对齐")
    _head()
    _sample("纯 ASCII", "Hello, world! ASCII")
    _sample("1 个 CJK", "中")
    _sample("2 个 CJK", "中文")
    _sample("3 个 CJK", "中文字")
    _sample("中英混排", "中文abc混合XYZ测试")
    _sample("全角标点", "，。！？：；")
    _sample("全角括号书名号", "（）【】《》")
    _sample("半角标点对照", ",.!?:;")
    _sample("全角字母数字", "ＡＢＣ０１２")
    _sample("超出内容区(不截断)", "0123456789012345678901234567890123")
    T.wln("")
    _ask_row("宽字符对齐", "上面各行的右侧 | 是否都与刻度尺对齐 (CJK 按 2 列)?")
    _cue("观察完宽字符宽度")


# ---------------------------------------------------------------- 2. 宽字符边界
def part_boundary():
    cols, rows = _size()
    T.section("宽字符边界行为")
    T.clear()
    T.wln(T.sgr(1, 36) + "▌ 宽字符边界行为 (屏幕 %dx%d)" % (cols, rows) + T.sgr(0))
    T.hint("要观察: 在最右列附近写宽字符时, 是换行到下一行, 还是挤占/截断")
    T.wln("")

    # 在倒数第二列写宽字符 (正好能放下 2 列)
    T.wln("  " + _label("写在倒数第二列") + "光标置于 (%d,%d) 后写「你」" % (4, cols - 1))
    T.write(T.cup(4, max(1, cols - 1)) + T.sgr(1, 32) + "你" + T.sgr(0))
    T.write(T.cup(5, 1) + T.sgr(90) + "  ↑ 上一行右端; 下一行开头是普通文本 → 你 应占满最右两列" + T.sgr(0))

    # 在最后一列写宽字符 (放不下, 应换行)
    T.wln("")
    T.wln("  " + _label("写在最后一列") + "光标置于 (%d,%d) 后写「你」" % (7, cols))
    T.write(T.cup(7, cols) + T.sgr(1, 33) + "你" + T.sgr(0))
    T.write(T.cup(8, 1) + T.sgr(90) + "  ↑ 若「你」出现在本行行首则说明正确换行" + T.sgr(0))

    # 宽字符 + 组合字符
    T.wln("")
    T.wln("  " + _label("宽字符+组合字符") + "「你」+ U+0301 组合重音")
    T.wln("  " + _label("") + _line("你\u0301") + T.sgr(90) + "  应为 2 列 (组合符不占位)" + T.sgr(0))

    # 覆盖宽字符
    T.wln("")
    T.wln("  " + _label("覆盖宽字符") + "先写「你好」, 再 CUB 1 回到「好」的左半格, 写 X")
    T.write(T.cup(14, 6) + T.sgr(1, 36) + "你好" + T.sgr(0))
    T.write(T.cub(1) + T.sgr(1, 31) + "X" + T.sgr(0))
    T.write(T.cup(15, 6) + T.sgr(90) + "↑ 第 14 行应显示为「你X」+两个空格, 不得出现「好」的半格残留" + T.sgr(0))
    T.wln("")

    _ask_row("边界-末列宽字", "在最后一列写宽字符时, 是否换行而非挤成半格?")
    _ask_row("边界-覆盖宽字", "覆盖宽字符时整个宽字是否被清除 (无半格残留)?")
    _cue("观察完宽字符边界")


# ---------------------------------------------------------------- 3. emoji
def part_emoji():
    T.section("emoji 宽度对照")
    T.hint("要观察: 每行右侧 | 是否与刻度尺对齐; 多数终端把 emoji 渲染成 2 列")
    _head()
    _sample("BMP 符号", "★☆✓✗☺♥")
    _sample("BMP 单符号", "★")
    _sample("非 BMP emoji", "😀🎉🚀")
    _sample("单个 emoji", "😀")
    _sample("VS16 带变体", "❤️")
    _sample("VS16 不带变体", "❤")
    _sample("ZWJ 家庭", "👨‍👩‍👧‍👦", want=2)
    _sample("彩虹旗 ZWJ", "🏳️‍🌈", want=2)
    _sample("国旗 (区域指示符)", "🇨🇳", want=2)
    _sample("肤色修饰", "👍🏽", want=2)
    _sample("keycap 1️⃣", "1️⃣", want=2)
    _sample("普通数字 1", "1")
    _sample("emoji 混排", "a😀中🎉b")
    T.wln("")
    _ask_row("emoji 宽度", "emoji 是否按 2 列渲染且各右侧 | 对齐?")
    _ask_row("emoji-ZWJ", "ZWJ 家族 / 旗帜 / keycap 是否渲染为单个 2 列图形?")
    _cue("观察完 emoji")


# ---------------------------------------------------------------- 4. 组合字符
def part_combining():
    T.section("组合字符 (组合记号应占 0 列)")
    T.hint("要观察: 组合序列在视觉上应是一个字符, 且右侧 | 仍与刻度尺对齐")
    _head()
    nfc = "\u00e9"                    # é (NFC 单码点)
    nfd = "e\u0301"                   # e + 组合尖音符 (NFD)
    _sample("é NFC (U+00E9)", nfc)
    _sample("é NFD (e+U+0301)", nfd)
    _sample("多重音标堆叠", "a\u0301\u0302\u0303\u0308")
    _sample("泰文 ที่นี่", "ที่นี่")
    _sample("天城文 नमस्ते", "नमस्ते")
    _sample("韩文 한 (预组合)", "한")
    _sample("韩文 jamo 分解", "\u1112\u1161\u11ab")
    _sample("行首孤立组合符", "\u0301abc")
    T.wln("  " + _label("对照: 无组合符") + _line("e") + T.sgr(90) + "  应为 1 列" + T.sgr(0))
    T.wln("")
    _ask_row("组合字符-宽度", "组合序列是否视觉上合为一个字符且不额外占列?")
    _ask_row("组合字符-NFD", "NFD 形式 (e+U+0301) 与 NFC (é) 渲染是否一致?")
    _cue("观察完组合字符")


# ---------------------------------------------------------------- 5. 零宽字符
def part_zero_width():
    T.section("零宽字符 (应不占宽度, 不产生空格)")
    T.hint("要观察: 夹在 A 与 B 之间的零宽字符不应把它们推开")
    _head()
    _sample("无 (对照 AB)", "AB")
    _sample("ZWJ U+200D", "A\u200dB")
    _sample("U+200B 零宽空格", "A\u200bB")
    _sample("LRM U+200E", "A\u200eB")
    _sample("RLM U+200F", "A\u200fB")
    _sample("BOM U+FEFF", "A\ufeffB")
    _sample("孤立 ZWJ 开头", "\u200dAB")
    T.wln("")
    T.hint("上表中所有行都应看起来像「AB」紧邻; 若 A B 之间出现空隙即为宽度计算有误")
    _ask_row("零宽字符", "零宽字符是否都不产生可见空隙 (各行为 AB 紧邻)?")
    _cue("观察完零宽字符")


# ---------------------------------------------------------------- 6. 框线
def _box_lines(w, h, tl, tr, bl, br, hh, vv):
    lines = [tl + hh * (w - 2) + tr]
    for _ in range(h - 2):
        lines.append(vv + " " * (w - 2) + vv)
    lines.append(bl + hh * (w - 2) + br)
    return lines


def part_box():
    T.section("框线与块元素: 10x5 无缝框 / 渐变")
    T.hint("要观察: 框线是否连续无缝 (横竖交点无断裂、无错位)")
    T.wln("")
    sets = [
        ("单线", ("┌", "┐", "└", "┘", "─", "│")),
        ("双线", ("╔", "╗", "╚", "╝", "═", "║")),
        ("圆角", ("╭", "╮", "╰", "╯", "─", "│")),
        ("粗线", ("┏", "┓", "┗", "┛", "━", "┃")),
    ]
    for label, c in sets:
        T.wln("  " + T.sgr(36) + label + ":" + T.sgr(0))
        for ln in _box_lines(10, 5, *c):
            T.wln("    " + ln)
    T.wln("")
    T.hint("交点与连接符应彼此咬合:")
    T.wln("    " + "├┤┬┴┼╠╣╦╩╬" + "   ┌┬┐" + " " + "├┼┤" + " " + "└┴┘")
    T.wln("")
    T.hint("块元素渐变:")
    T.wln("    下八分块: " + "▁▂▃▄▅▆▇█")
    T.wln("    灰度块:   " + "░▒▓█" + "   反向: " + "█▓▒░")
    T.wln("    右八分块: " + "▏▎▍▌▋▊▉█")
    T.wln("")
    _ask_row("框线无缝", "单/双/圆角/粗线框是否都无缝 (交点无断裂错位)?")
    _ask_row("块元素渐变", "半块与灰度块渐变是否均匀无缝隙?")
    _cue("观察完框线块元素")


# ---------------------------------------------------------------- 7. 其它符号
def part_symbols():
    T.section("盲文 / 几何图形 / 数学符号 / powerline 私用区")
    T.hint("要观察: 符号是否为等宽的单列图形, 有无豆腐块或错位")
    T.wln("")
    T.wln("  盲文旋转 (应等宽): " + T.sgr(1, 36) + "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" + T.sgr(0))
    T.wln("  几何图形: " + "▲△▼▽◆◇○●■□▪▫◊")
    T.wln("  箭头: " + "←↑→↓↔↕⇐⇑⇒⇓⇔")
    T.wln("  数学符号: " + "∑∏∫√∞≈≠≤≥±×÷∂∇∈∉⊂⊃")
    T.wln("  制表块: " + "▔▕▖▗▘▙▚▛▜▝▞▟")
    T.wln("")
    T.wln("  powerline 私用区 (U+E0B0/E0B2 等):")
    T.wln("    " + "".join(chr(0xE0B0 + i) for i in (0, 2, 4, 6)) + "  " + T.sgr(90) + "显示为豆腐块属正常" + T.sgr(0))
    T.wln("    " + chr(0xE0B0) * 12 + T.sgr(90) + "  ← 连续三角形应拼成实心块 (若字体支持)" + T.sgr(0))
    T.wln("")
    _ask_row("盲文等宽", "盲文旋转字符是否为等宽单列且不跳动?")
    _cue("观察完其它符号")


# ---------------------------------------------------------------- 8. RTL
def part_rtl():
    T.section("RTL 文本 (只记录现象)")
    T.hint("要观察: 阿拉伯语与希伯来语的字符顺序与连接形态 (本项不作判定)")
    T.wln("")
    T.wln("  阿拉伯语: " + _line("مرحبا بالعالم"))
    T.wln("  希伯来语: " + _line("שלום עולם"))
    T.wln("  混排:     " + _line("abc مرحبا 123"))
    T.wln("")
    T.hint("现象记录: 若 RTL 被按 LTR 顺序逐个码点渲染, 单词顺序会反向")
    _cue("观察完 RTL")


# ---------------------------------------------------------------- 9. 宽度对照表
TABLE_CHARS = [
    "A", "a", "1", " ", "-",
    "中", "文", "，", "）", "Ａ",
    "★", "☆", "✓", "♥", "☺",
    "😀", "🎉", "❤", "❤️", "👍", "👍🏽", "1️⃣", "🇨🇳", "👨‍👩‍👧‍👦",
    "é", "e\u0301", "\u0301", "한", "\u1112\u1161\u11ab",
    "\u200d", "\u200b", "\u200e", "\u200f", "\ufeff",
    "⠋", "█", "▁", "░", "▒",
    "─", "│", "┌", "┼", "╬", "▔",
    "∑", "∞", "≈", "→", "\ue0b0",
]


def _table_lines():
    rows = []
    header = ("%-6s %-9s %-34s %-4s %-4s %-7s %s"
              % ("字符", "码点", "名称", "类别", "EAW", "dwidth", "码点数"))
    rows.append(header)
    rows.append("-" * len(header))
    for ch in TABLE_CHARS:
        cp = "U+%04X" % ord(ch[0])
        name = unicodedata.name(ch[0], "?")
        if len(name) > 32:
            name = name[:32]
        cat = unicodedata.category(ch[0])
        eaw = unicodedata.east_asian_width(ch[0])
        rows.append("%-6s %-9s %-34s %-4s %-4s %-7d %d"
                    % (ch, cp, name, cat, eaw, T.dwidth(ch), len(ch)))
    return rows


def part_table():
    T.section("宽度对照表 (字符 / 码点 / 类别 / east_asian_width / dwidth)")
    T.hint("把下表与终端里的实际表现对照; EAW: W/F=宽(2列) Na/N/H=窄(1列) A=依终端而定")
    T.wln("")
    lines = _table_lines()
    for ln in lines:
        T.wln("  " + ln)
    T.wln("")
    T.hint("说明: dwidth 为 termlib 按 east_asian_width 的近似计算; "
           "ZWJ / 旗帜 / 肤色等组合序列按码点累加会偏大, 实际应以终端渲染为准")

    report = []
    report.append("t_unicode.py 宽度对照表")
    report.append("终端尺寸: %dx%d" % _size())
    report.append("NONINTERACTIVE=%s" % T.NONINTERACTIVE)
    report.append("")
    report.append("列说明: 字符 / 码点 / 名称 / 类别 / east_asian_width / termlib.dwidth / 码点数")
    report.append("")
    report.extend(lines)
    report.append("")
    report.append("EAW 取值含义:")
    report.append("  W  (Wide)        宽, 通常 2 列")
    report.append("  F  (Fullwidth)   全角, 通常 2 列")
    report.append("  H  (Halfwidth)   半角, 通常 1 列")
    report.append("  Na (Narrow)      窄,  通常 1 列")
    report.append("  A  (Ambiguous)   依终端/区域设置, 1 或 2 列")
    report.append("")
    report.append("备注: ZWJ 序列 (家庭/旗帜)、肤色修饰、keycap 按码点累加得到的宽度会大于实际")
    report.append("      渲染宽度 (应为 2 列), 属正常现象, 需以终端实际渲染为准。")
    T.wln("")
    path = T.save_report("t_unicode", "\n".join(report))
    if path:
        T.row("宽度报告", T.PASS, "已保存: %s" % T.safe_path(path))
    else:
        T.row("宽度报告", T.WARN, "报告保存失败 (termtest/_reports 不可写)")


# ---------------------------------------------------------------- 入口
def main():
    T.start("t_unicode.py",
            ["宽字符宽度", "宽字符边界", "emoji", "组合字符", "零宽字符",
             "框线与块元素", "其它符号", "RTL", "宽度对照表"],
            note="每行都套在 | ... | 中并与刻度尺对齐, 右侧边框对齐即宽度正确")
    part_width()
    part_boundary()
    part_emoji()
    part_combining()
    part_zero_width()
    part_box()
    part_symbols()
    part_rtl()
    part_table()
    T.finish()
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))