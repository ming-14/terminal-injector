# -*- coding: utf-8 -*-
"""t_query.py — 自动一致性查询测试

向终端发送查询序列并读取回包, 自动判定 PASS / FAIL / DIFF / SKIP。
这是不依赖人眼的"回归测试", 终端每改一版都可以重跑一遍。

覆盖:
  * DA1 / DA2 设备属性 (CSI c / CSI > c)
  * DSR 状态与光标位置 (CSI 5n / 6n / ?6n)
  * DECRQM 模式查询 (批量) + 模式置位/复位的状态回读 (往返验证)
  * DECRQSS 设置查询 (m / r / " q" / s / "q")
  * XTGETTCAP 终端能力查询 (TN / Co / RGB / Ms / Su ...)
  * OSC 4 调色板查询/设置回读, OSC 10/11/12 前景/背景/光标色
  * OSC 52 剪贴板查询与写入回读
  * XTWINOPS 窗口/像素/单元格尺寸查询 (CSI 18t / 14t / 16t / 13t)
  * 未知序列健壮性 (未知 SGR/CSI/OSC/DCS/APC 不应破坏状态)
  * 延迟换行状态机 (末列不回绕 / 触发回绕 / DECAWM 关闭)

用法:
    python t_query.py [--timeout 0.35] [--no-save]
"""

import argparse
import re
import sys
import time

import termlib as T

TQ = {"timeout": 0.35}
RAW_LOG = []          # (说明, 原始回包 bytes|None)


def ask(seq, desc="", log=True):
    """发送查询并记录原始回包。

    log=False: 只查询不记账。用于回包可能携带用户数据的查询 (OSC 52 剪贴板) ——
    剪贴板里可能有口令/密钥/令牌, 原始回包不进 RAW_LOG、不落盘。
    """
    r = T.query(seq, TQ["timeout"])
    if log:
        RAW_LOG.append((desc or T.vis(seq), r))
    return r


def S(b):
    """回包 bytes -> latin-1 字符串, 便于正则解析。"""
    return b.decode("latin-1") if b else ""


def show(desc, r, indent="      "):
    """把回包以可读 + 十六进制形式打印出来 (排查用)。"""
    if r is None:
        T.wln(indent + T.sgr(90) + desc + ": (无回包)" + T.sgr(0))
    else:
        T.wln(indent + T.sgr(90) + desc + ": " + T.vis(r) + T.sgr(0))
        T.wln(indent + T.sgr(90) + "      " + T.hexdump(r)[:150] + T.sgr(0))


def parse_rgb(s):
    """解析 rgb:RRRR/GGGG/BBBB -> (r,g,b) 0-255。"""
    m = re.search(r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})", s or "")
    if not m:
        return None
    return tuple(int(v, 16) * 255 // 65535 for v in m.groups())


def rgb_seq(r, g, b):
    """把 0-255 的颜色转成 OSC 用的 rgb:RRRR/GGGG/BBBB 部分。"""
    return "rgb:%04x/%04x/%04x" % (r * 257, g * 257, b * 257)


def swatch(rgb, width=3):
    if not rgb:
        return ""
    return T.bg_rgb(*rgb) + " " * width + T.sgr(0) + " rgb(%d,%d,%d)" % rgb


DA1_NAMES = {
    1: "132 列模式", 2: "打印机", 3: "ReGIS 图形", 4: "Sixel 图形",
    6: "选择性擦除", 8: "UDK 用户定义键", 9: "NRCS 字符集", 12: "南斯拉夫字符集",
    15: "技术字符集", 16: "定位器", 17: "终端状态询问", 18: "窗口",
    21: "水平滚动", 22: "ANSI 颜色", 23: "希腊字符集", 24: "土耳其字符集",
    28: "矩形操作", 29: "ANSI 文本定位器",
    42: "NVRAM", 44: "边界响铃", 45: "反向回绕(REVWRAP)", 46: "logos",
    47: "备用屏", 52: "剪贴板(OSC 52)",
}
# 其余参数多为厂商扩展 (例: Windows Terminal 报 61/7/14/32 等未收录值), 一律显示为"未知"

DECRQM_STATUS = {0: "未识别", 1: "已置位", 2: "已复位", 3: "永久置位", 4: "永久复位"}


# ---------------------------------------------------------------- 各检查项
def sec_da():
    T.section("DA 设备属性 — CSI c / CSI > c")
    r = ask(T.CSI + "c", "DA1 (CSI c)")
    show("回包", r)
    m = re.match(r"\x1b\[\?([0-9;]*)c", S(r))
    if m:
        params = [int(x) for x in m.group(1).split(";") if x]
        desc = ", ".join("%d=%s" % (p, DA1_NAMES.get(p, "未知")) for p in params)
        T.row("DA1 主设备属性", T.PASS, "参数: " + desc)
    else:
        T.row("DA1 主设备属性", T.SKIP, "无回包 (终端未实现 DA1)")

    r = ask(T.CSI + ">c", "DA2 (CSI > c)")
    show("回包", r)
    m = re.match(r"\x1b\[>([0-9;]*)c", S(r))
    if m:
        T.row("DA2 次设备属性", T.PASS, "参数: " + m.group(1))
    else:
        T.row("DA2 次设备属性", T.SKIP, "无回包")


def sec_dsr():
    T.section("DSR 状态与光标位置 — CSI 5n / CSI 6n / CSI ?6n")
    cols, rows = T.term_size()

    # 探测要把光标 CUP 到 (3,5) 与右下角, 期间全程不打印 ——
    # 就地打印会得到"短行盖长行"的叠字画面。
    # 探测完由 SavedCursor 把光标放回原处, 后面的 show() 就能接着顺序追加;
    # 换成 park_cursor() 甩到屏幕末行的话, 标题与末行之间那几十行从未写过,
    # 屏幕上会留下一大段空白。
    got = []
    with T.SavedCursor():
        got.append(("DSR 5n 设备状态", ask(T.CSI + "5n", "DSR 5n (设备状态)")))
        T.write(T.cup(3, 5))
        got.append(("DSR 6n (CUP 3;5 后)", ask(T.CSI + "6n", "DSR 6n (CUP 3;5 后)")))
        got.append(("DSR ?6n (DECXCPR)", ask(T.CSI + "?6n", "DSR ?6n (DECXCPR)")))
        T.write(T.cup(rows, cols))
        got.append(("DSR 6n 右下角", ask(T.CSI + "6n", "DSR 6n (右下角)")))

    for desc, r in got:
        show(desc, r)

    # ---- 判定 ----
    r = dict(got).get("DSR 5n 设备状态")
    if S(r).startswith("\x1b[0n"):
        T.row("DSR 5n 状态报告", T.PASS, "回包 CSI 0n (正常工作)")
    else:
        T.row("DSR 5n 状态报告", T.SKIP, "无回包")

    r = dict(got).get("DSR 6n (CUP 3;5 后)")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got_pos = (int(m.group(1)), int(m.group(2)))
        ok = got_pos == (3, 5)
        T.row("DSR 6n 光标位置", T.PASS if ok else T.FAIL,
              "CUP 3;5 后报告 %d;%d%s" % (got_pos[0], got_pos[1], "" if ok else " (期望 3;5)"))
    else:
        T.row("DSR 6n 光标位置", T.SKIP, "无回包")

    r = dict(got).get("DSR ?6n (DECXCPR)")
    m = re.match(r"\x1b\[\?(\d+);(\d+)R", S(r))
    if m:
        T.row("DSR ?6n 扩展位置", T.PASS, "回包 %s;%s (1-based)" % (m.group(1), m.group(2)))
    else:
        T.row("DSR ?6n 扩展位置", T.SKIP, "无回包")

    r = dict(got).get("DSR 6n 右下角")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got_pos = (int(m.group(1)), int(m.group(2)))
        ok = got_pos == (rows, cols)
        T.row("DSR 6n 右下角边界", T.PASS if ok else T.DIFF,
              "期望 %d;%d, 得到 %d;%d" % (rows, cols, got_pos[0], got_pos[1]))
    else:
        T.row("DSR 6n 右下角边界", T.SKIP, "无回包")


def sec_decrqm():
    T.section("DECRQM 模式查询 — CSI n $ p / CSI ? n $ p")
    modes = [
        (4, False, "IRM 插入模式"),
        (1, True, "DECCKM 应用光标键"),
        (6, True, "DECOM 原点模式"),
        (7, True, "DECAWM 自动换行"),
        (25, True, "DECTCEM 光标可见"),
        (1004, True, "焦点事件"),
        (1006, True, "SGR 鼠标编码"),
        (1005, True, "UTF-8 鼠标编码"),
        (1049, True, "备用屏"),
        (2004, True, "括号粘贴"),
        (2026, True, "同步输出"),
        (2027, True, "字形簇(grapheme)处理"),
    ]
    recognized = 0
    for mode, private, name in modes:
        seq = T.CSI + ("?" if private else "") + "%d$p" % mode
        r = ask(seq, "DECRQM %s%d" % ("?" if private else "", mode))
        m = re.match(r"\x1b\[(\?)?(\d+);(\d+)\$y", S(r))
        if m:
            st = int(m.group(3))
            if st == 0:
                # 回包本身说明终端实现了 DECRQM, 但它不识别这个模式 → 不能算 PASS
                T.row("DECRQM %s" % name, T.SKIP, "终端回复 status=0: 不识别该模式")
            else:
                recognized += 1
                T.row("DECRQM %s" % name, T.PASS,
                      "%s (status=%d)" % (DECRQM_STATUS.get(st, "未知"), st))
        else:
            T.row("DECRQM %s" % name, T.SKIP, "无回包 (未实现 DECRQM)")
    T.hint("DECRQM 可识别的模式数: %d / %d" % (recognized, len(modes)))

    # ---- 往返验证: 置位 -> 查询应为"已置位"; 复位 -> 查询应为"已复位" ----
    # 注意: 这里固定"先 h 后 l", 所以期望恒为 1 与 2;
    #       对默认就是置位的模式 (25 光标可见 / 7 自动换行), 结束后要恢复默认, 否则会留下副作用。
    T.section("模式状态往返 — 置位/复位后回读状态")
    cases = [
        (2004, "括号粘贴", 2),
        (2026, "同步输出", 2),
        (25, "光标可见", 1),
        (7, "自动换行", 1),
    ]
    for mode, name, default in cases:
        q = T.CSI + "?%d$p" % mode
        r0 = ask(q, "DECRQM ?%d (初始)" % mode)
        m0 = re.search(r"(\d+)\$y", S(r0))
        if not m0:
            T.row("模式往返 %s" % name, T.SKIP, "终端不支持该模式的 DECRQM 查询")
            continue
        if int(m0.group(1)) == 0:
            T.row("模式往返 %s" % name, T.SKIP, "终端回复 status=0: 不识别该模式, 无法验证往返")
            continue
        T.write(T.CSI + "?%dh" % mode)          # 置位 → 期望 status=1
        r1 = ask(q, "DECRQM ?%d (置位后)" % mode)
        T.write(T.CSI + "?%dl" % mode)          # 复位 → 期望 status=2
        r2 = ask(q, "DECRQM ?%d (复位后)" % mode)
        T.write(T.CSI + ("?%dh" % mode if default == 1 else "?%dl" % mode))   # 恢复默认状态
        s1 = re.search(r"(\d+)\$y", S(r1))
        s2 = re.search(r"(\d+)\$y", S(r2))
        v1 = int(s1.group(1)) if s1 else -1
        v2 = int(s2.group(1)) if s2 else -1
        ok = (v1 == 1 and v2 == 2)
        T.row("模式往返 %s" % name, T.PASS if ok else T.DIFF,
              "置位后 status=%s, 复位后 status=%s%s (默认=%s, 已恢复)"
              % (DECRQM_STATUS.get(v1, v1), DECRQM_STATUS.get(v2, v2),
                 "" if ok else " (期望 1 与 2)", DECRQM_STATUS.get(default, default)))


def sec_decrqss():
    T.section("DECRQSS 设置查询 — DCS $ q <P> ST")
    items = [
        ("m", "SGR 当前属性"),
        ("r", "DECSTBM 滚动区"),
        (" q", "DECSCUSR 光标形状"),
        ("s", "DECSLRM 左右边距"),
        ('"q', "DECSCL 兼容级别"),
    ]
    for pt, name in items:
        r = ask(T.DCS + "$q" + pt + T.ST, "DECRQSS %r" % pt)
        show("回包", r)
        m = re.match(r"\x1bP([01])\$r(.*)\x1b\\", S(r), re.S)
        if m:
            ok = m.group(1) == "1"
            val = m.group(2)
            T.row("DECRQSS %s" % name, T.PASS if ok else T.SKIP,
                  "值: %r" % val if ok else "返回 0 (该设置项不支持)")
        else:
            T.row("DECRQSS %s" % name, T.SKIP, "无回包")


def sec_termcap():
    T.section("XTGETTCAP 终端能力查询 — DCS + q <hex> ST")
    names = [
        ("TN", "终端名"),
        ("Co", "颜色数"),
        ("RGB", "truecolor (规范名)"),
        ("Tc", "truecolor (旧名)"),
        ("Ms", "modifyOtherKeys"),
        ("Su", "Sixel 支持"),
        ("Setulc", "下划线颜色"),
        ("Smulx", "下划线样式"),
        ("Cs", "光标样式"),
    ]
    for nm, desc in names:
        hx = nm.encode("ascii").hex()
        r = ask(T.DCS + "+q" + hx + T.ST, "XTGETTCAP %s" % nm)
        m = re.match(r"\x1bP([01])\+r([0-9a-fA-F]+)(?:=([0-9a-fA-F]+))?\x1b\\", S(r), re.S)
        if not m:
            T.row("XTGETTCAP %s (%s)" % (nm, desc), T.SKIP, "无回包")
            continue
        found = m.group(1) == "1"
        val = ""
        if m.group(3):
            try:
                val = bytes.fromhex(m.group(3)).decode("utf-8", "replace")
            except Exception:
                val = "?"
        if found:
            if nm == "TN":
                # xterm/vte 惯例下 TN 是终端名, 常等于主机名: 只记长度不记值
                T.row("XTGETTCAP %s (%s)" % (nm, desc), T.PASS,
                      "值已掩码 (长度 %d 字符)" % len(val))
            else:
                T.row("XTGETTCAP %s (%s)" % (nm, desc), T.PASS, "值: %r" % val)
        else:
            T.row("XTGETTCAP %s (%s)" % (nm, desc), T.SKIP, "终端明确回复不支持 (0+r)")


def sec_osc_colors():
    T.section("OSC 4 调色板 — 查询 / 设置回读 / 恢复")
    idxs = [0, 1, 2, 7, 15, 16, 231, 255]
    got = {}
    for n in idxs:
        r = ask(T.OSC + "4;%d;?" % n + T.ST, "OSC 4;%d;?" % n)
        rgb = parse_rgb(S(r))
        if rgb:
            got[n] = rgb
            T.row("OSC 4 查询索引 %d" % n, T.PASS, swatch(rgb))
        else:
            T.row("OSC 4 查询索引 %d" % n, T.SKIP, "无回包")
    if 16 in got:
        orig = got[16]
        test = (255, 64, 128)
        try:
            T.write(T.OSC + "4;16;" + rgb_seq(*test) + T.ST)
            time.sleep(0.05)
            r = ask(T.OSC + "4;16;?" + T.ST, "OSC 4;16;? (设置后)")
            back = parse_rgb(S(r))
            ok = back is not None and all(abs(a - b) <= 4 for a, b in zip(back, test))
            back_s = ("rgb(%d,%d,%d)" % back) if back else "无回包"
            T.row("OSC 4 设置后回读", T.PASS if ok else T.FAIL,
                  "写入 rgb(%d,%d,%d), 回读 %s" % (test[0], test[1], test[2], back_s))
        finally:
            T.write(T.OSC + "4;16;" + rgb_seq(*orig) + T.ST)   # 恢复原色
    else:
        T.row("OSC 4 设置后回读", T.SKIP, "读不到索引 16 的原值, 跳过写入测试")

    T.section("OSC 10/11/12 前景 / 背景 / 光标色")
    orig = {}
    for code, name in ((10, "前景色"), (11, "背景色"), (12, "光标色")):
        r = ask(T.OSC + "%d;?" % code + T.ST, "OSC %d;?" % code)
        rgb = parse_rgb(S(r))
        if rgb:
            orig[code] = rgb
            T.row("OSC %d 查询%s" % (code, name), T.PASS, swatch(rgb))
        else:
            T.row("OSC %d 查询%s" % (code, name), T.SKIP, "无回包")
    if 12 in orig:
        test = (0, 255, 128)
        try:
            T.write(T.OSC + "12;" + rgb_seq(*test) + T.ST)
            time.sleep(0.05)
            r = ask(T.OSC + "12;?" + T.ST, "OSC 12;? (设置后)")
            back = parse_rgb(S(r))
            ok = back is not None and all(abs(a - b) <= 4 for a, b in zip(back, test))
            back_s = ("rgb(%d,%d,%d)" % back) if back else "无回包"
            T.row("OSC 12 设置后回读", T.PASS if ok else T.FAIL,
                  "写入 rgb(%d,%d,%d), 回读 %s" % (test[0], test[1], test[2], back_s))
        finally:
            T.write(T.OSC + "12;" + rgb_seq(*orig[12]) + T.ST)  # 恢复原色
    else:
        T.row("OSC 12 设置后回读", T.SKIP, "读不到原值, 跳过写入测试")


def sec_osc52():
    T.section("OSC 52 剪贴板 — 查询 / 写入回读")
    if T.NONINTERACTIVE:
        T.row("OSC 52 剪贴板回读", T.SKIP, "非交互模式: 跳过 (会改动剪贴板)")
        return
    import base64
    # 剪贴板回包可能含用户复制的口令/密钥/验证码: 不进 RAW_LOG、不 hexdump 上屏,
    # 只用于判定, 报告里最多出现长度。
    r = ask(T.OSC + "52;c;?" + T.ST, "OSC 52;c;? (查询)", log=False)
    m = re.match(r"\x1b\]52;c;([A-Za-z0-9+/=]*)\x1b\\", S(r))
    original = m.group(1) if m else None
    if original is None:
        T.row("OSC 52 查询剪贴板", T.SKIP, "无回包 (多数终端为安全起见不支持读剪贴板)")
    else:
        try:
            txt = base64.b64decode(original).decode("utf-8", "replace") if original else "(空)"
        except Exception:
            txt = "(无法解码)"
        T.row("OSC 52 查询剪贴板", T.PASS, "长度 %d 字符" % len(txt))

    marker = "termtest-%d" % (time.time() % 100000)
    T.note("即将向剪贴板写入测试文本以验证回读, 随后会尝试恢复原内容")
    T.write(T.OSC + "52;c;" + base64.b64encode(marker.encode()).decode() + T.ST)
    time.sleep(0.1)
    r = ask(T.OSC + "52;c;?" + T.ST, "OSC 52;c;? (写入后)", log=False)
    m = re.match(r"\x1b\]52;c;([A-Za-z0-9+/=]*)\x1b\\", S(r))
    back = ""
    if m and m.group(1):
        try:
            back = base64.b64decode(m.group(1)).decode("utf-8", "replace")
        except Exception:
            back = "(解码失败)"
    if back == marker:
        T.row("OSC 52 写入后回读", T.PASS, "剪贴板往返一致")
    elif back:
        T.row("OSC 52 写入后回读", T.DIFF,
              "写入 %d 字符, 回读 %d 字符 (内容不落盘)" % (len(marker), len(back)))
    else:
        T.row("OSC 52 写入后回读", T.SKIP, "不支持读回 (写入是否生效请手动粘贴验证)")
    if original:
        T.write(T.OSC + "52;c;" + original + T.ST)   # 恢复剪贴板


def sec_winops():
    T.section("XTWINOPS 窗口信息 — CSI 18t / 14t / 16t / 13t")
    cols, rows = T.term_size()
    res = {}
    r = ask(T.CSI + "18t", "CSI 18t (字符尺寸)")
    show("回包", r)
    m = re.match(r"\x1b\[8;(\d+);(\d+)t", S(r))
    if m:
        got = (int(m.group(2)), int(m.group(1)))   # 回包是 height;width → 存成 (cols, rows)
        res["chars"] = got
        ok = got == (cols, rows)
        T.row("CSI 18t 字符尺寸", T.PASS if ok else T.DIFF,
              "终端自报 %dx%d, 程序测得 %dx%d (列x行)" % (got[0], got[1], cols, rows))
    else:
        T.row("CSI 18t 字符尺寸", T.SKIP, "无回包")

    r = ask(T.CSI + "14t", "CSI 14t (像素尺寸)")
    show("回包", r)
    m = re.match(r"\x1b\[4;(\d+);(\d+)t", S(r))
    if m:
        res["px"] = (int(m.group(2)), int(m.group(1)))
        T.row("CSI 14t 像素尺寸", T.PASS, "窗口 %dx%d 像素" % res["px"])
    else:
        T.row("CSI 14t 像素尺寸", T.SKIP, "无回包")

    r = ask(T.CSI + "16t", "CSI 16t (单元格像素)")
    show("回包", r)
    m = re.match(r"\x1b\[6;(\d+);(\d+)t", S(r))
    if m:
        res["cell"] = (int(m.group(2)), int(m.group(1)))
        T.row("CSI 16t 单元格尺寸", T.PASS, "单元格 %dx%d 像素" % res["cell"])
    else:
        T.row("CSI 16t 单元格尺寸", T.SKIP, "无回包")

    r = ask(T.CSI + "13t", "CSI 13t (窗口位置)")
    show("回包", r)
    m = re.match(r"\x1b\[3;(\d+);(\d+)t", S(r))
    if m:
        T.row("CSI 13t 窗口位置", T.PASS, "位置 x=%s y=%s" % (m.group(1), m.group(2)))
    else:
        T.row("CSI 13t 窗口位置", T.SKIP, "无回包")

    # 一致性: 像素尺寸 ≈ 单元格尺寸 × 字符行列数
    if "px" in res and "cell" in res and "chars" in res:
        cw, ch = res["cell"]
        pw, ph = res["px"]
        cc, cr = res["chars"]
        ew, eh = cw * cc, ch * cr
        ok = abs(pw - ew) <= cw and abs(ph - eh) <= ch
        T.row("像素/单元格一致性", T.PASS if ok else T.DIFF,
              "单元格 %dx%d × %dx%d = %dx%d, 自报窗口 %dx%d" %
              (cw, ch, cc, cr, ew, eh, pw, ph))


def sec_junk():
    rows = T.term_size()[1]
    T.clear()                # 先清屏: 垃圾序列若留下可见残留, 在空白屏上一眼就能看出来
    T.section("未知序列健壮性 — 不应破坏光标位置/SGR 状态/画面")
    junk = [
        T.CSI + "999;88;77;12345m",                                  # 未知 SGR 参数
        T.CSI + "1;2;3;4;5;6;7;8;9;10;11;12;13;14;15;16;17;18;19;20z",  # 未知 final + 20 参数
        T.CSI + "?9999h",                                            # 未知私有模式
        T.CSI + "?9999l",
        T.CSI + ">20q",                                              # 非法光标形状参数
        T.OSC + "7777;some;unknown;payload" + T.ST,                   # 未知 OSC
        T.OSC + "99999;data" + T.ST,
        T.DCS + "1;2;3x" + "junkpayload" + T.ST,                      # 未知 DCS
        T.APC + "junkpayload" + T.ST,                                 # 未知 APC
        T.PM + "junkpayload" + T.ST,                                  # 未知 PM
        T.SOS + "junkpayload" + T.ST,                                 # 未知 SOS
    ]
    T.wln("  已清屏; 在 (5,10) 处连续发送 %d 条未知/畸形序列, 判定与标记行统一打在屏幕下方"
          " —— 本行与下方判定行之间整片留空, 那就是残留检测区。" % len(junk))
    T.write(T.cup(5, 10))
    for j in junk:
        T.write(j)

    # 用 CPR 验证光标位置没被垃圾序列带跑 (行与列都应停在 5;10)
    r = ask(T.CSI + "6n", "垃圾序列后 DSR 6n")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got = (int(m.group(1)), int(m.group(2)))
        ok = got == (5, 10)
    else:
        got, ok = None, False

    # 用 DECRQSS 验证 SGR 状态没被污染
    r = ask(T.DCS + "$qm" + T.ST, "垃圾序列后 DECRQSS m")
    m = re.match(r"\x1bP([01])\$r(.*)\x1b\\", S(r), re.S)

    # 两条判定行、标记行、提问与结论共 5 行, 统一打到屏幕下方:
    #   * 不能就地打印 —— 光标正停在 (5,10), 也就是垃圾序列的写入点, 就地打印
    #     会直接把残留盖掉, 检测区里什么也看不出来;
    #   * 也不能停到末行收尾 —— 末行每次换行都触发滚动, 会把上方那片空白检测区
    #     连同标题一起推走, 残留就跟着滚出可视区了。
    #     所以按"后面还要写几行"留出余量, 整块落位且不滚动。
    T.park_cursor(rows - 6)

    if got:
        T.row("光标位置未被带跑", T.PASS if ok else T.FAIL,
              "发送 11 条未知序列后光标 %d;%d (期望 5;10)" % got)
    else:
        T.row("光标位置未被带跑", T.SKIP, "无 CPR 回包, 请目视检查")

    if m:
        val = m.group(2)
        ok = val in ("0m", "m", "")
        T.row("SGR 状态未被污染", T.PASS if ok else T.FAIL, "当前 SGR: %r" % val)
    else:
        T.row("SGR 状态未被污染", T.SKIP, "无 DECRQSS 回包, 请目视检查")

    T.wln(T.sgr(1, 33) + "垃圾序列测试结束: 这一行应完整清晰, 屏幕其它位置不应出现乱码或 junkpayload。" + T.sgr(0))
    y = T.ask_yn("画面是否干净 (无乱码 / 无多余的 junkpayload)?")
    if y is None:
        T.row("垃圾序列后画面完整", T.SKIP, "未回答")
    else:
        T.row("垃圾序列后画面完整", T.PASS if y else T.FAIL, "")


def sec_wrap():
    T.section("延迟换行状态机 — 末列不回绕 / 触发回绕 / DECAWM 关闭")
    cols, rows = T.term_size()
    T.write(T.CSI + "2J" + T.CSI + "H")

    def verdict(name, status, detail):
        """判定行落到倒数第二行。

        探测时, 光标正停在末列/延迟换行这种"待测状态"上 (就那样打印会先触发一次
        回绕并滚动整屏, 把待测状态和刚写满的末行一起踩掉), 所以先归位再打印。
        """
        T.park_cursor(max(1, rows - 1))
        T.row(name, status, detail)

    # a) 写满最后一行的 cols 个字符, 光标应停在末列 (延迟换行)
    T.write(T.cup(rows, 1) + "X" * max(1, cols))
    r = ask(T.CSI + "6n", "写满末行后 DSR 6n")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got = (int(m.group(1)), int(m.group(2)))
        ok = got == (rows, cols)
        verdict("末列延迟换行 (不提前回绕)", T.PASS if ok else T.FAIL,
                "期望 %d;%d, 得到 %d;%d" % (rows, cols, got[0], got[1]))
    else:
        verdict("末列延迟换行 (不提前回绕)", T.SKIP, "无回包")

    # b) 再写 1 个字符才触发回绕 (末行会滚动一行)
    #    上一条判定行已经把光标挪走了, 这里重新写满末行, 把"末列延迟换行"的状态
    #    复原成与 a) 相同, 否则测到的是判定行留下的位置, 不是终端的换行行为。
    T.write(T.cup(rows, 1) + "X" * max(1, cols))
    T.write("Y")
    r = ask(T.CSI + "6n", "补写 1 字符后 DSR 6n")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got = (int(m.group(1)), int(m.group(2)))
        ok = got == (rows, 2)
        verdict("补写后触发回绕", T.PASS if ok else T.FAIL,
                "期望 %d;2, 得到 %d;%d" % (rows, got[0], got[1]))
    else:
        verdict("补写后触发回绕", T.SKIP, "无回包")

    # c) DECAWM 关闭: 超出内容应覆写末列, 光标不越界
    T.write(T.CSI + "?7l" + T.CSI + "2J" + T.CSI + "H")
    T.write(T.cup(rows, 1) + "X" * max(1, cols) + "ZZZ")
    r = ask(T.CSI + "6n", "DECAWM 关闭后 DSR 6n")
    m = re.match(r"\x1b\[(\d+);(\d+)R", S(r))
    if m:
        got = (int(m.group(1)), int(m.group(2)))
        ok = got[1] <= cols and got[0] <= rows
        verdict("DECAWM 关闭时覆写末列", T.PASS if ok else T.FAIL,
                "光标 %d;%d (不应超过 %d;%d)" % (got[0], got[1], rows, cols))
    else:
        verdict("DECAWM 关闭时覆写末列", T.SKIP, "无回包")
    T.write(T.CSI + "?7h" + T.CSI + "2J" + T.CSI + "H")


# ---------------------------------------------------------------- 主流程
def build_report():
    lines = ["t_query.py 报告",
             "时间: " + time.strftime("%Y-%m-%d"),
             "终端: %s" % "  ".join(T.term_info()["objs"]),
             "窗口: %dx%d" % T.term_size(),
             "超时: %.2fs" % TQ["timeout"],
             "",
             "== 判定 =="]
    for name, status, detail in T.rows():
        lines.append("[%-4s] %-34s %s" % (status, name, detail))
    lines.append("")
    lines.append("== 原始回包 ==")
    for desc, raw in RAW_LOG:
        lines.append("%-40s %s" % (desc, T.dump_bytes(raw) if raw else "(无回包)"))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="自动一致性查询测试")
    ap.add_argument("--timeout", type=float, default=0.35, help="每次查询等待回包秒数")
    ap.add_argument("--no-save", action="store_true", help="不写报告文件")
    args = ap.parse_args()
    TQ["timeout"] = args.timeout

    T.start("t_query.py", ["DA1/DA2", "DSR 5/6n", "DECRQM", "DECRQSS",
                           "XTGETTCAP", "OSC 4/10/11/12/52", "XTWINOPS",
                           "未知序列健壮性", "延迟换行"],
            note="发查询序列读回包自动判定; 终端不回包的项目记为 SKIP (该终端未实现该查询)")

    if not T.INPUT_OK:
        T.note("stdin 不是可读终端: 无法收到回包, 所有项目会记 SKIP。")
        T.note("请在模拟器/终端窗口内直接运行本程序。")

    try:
        for fn in (sec_da, sec_dsr, sec_decrqm, sec_decrqss, sec_termcap,
                   sec_osc_colors, sec_osc52, sec_winops, sec_junk, sec_wrap):
            try:
                fn()
            except T.QuitTest:
                raise
            except Exception as e:
                T.row(fn.__name__, T.FAIL, "内部异常: %r" % (e,))
    finally:
        # 中途 q/Esc/EOF 退出也把已经判出来的项目落盘, 别让前面几十条查询白跑
        if not args.no_save:
            p = T.save_report("query", build_report())
            if p:
                T.hint("完整报告(含原始回包十六进制)已写入: %s" % T.safe_path(p))

    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))