# -*- coding: utf-8 -*-
"""t_screen.py — 光标与屏幕操作兼容性测试

本程序检验终端模拟器对光标定位、擦除、行操作、滚动区、模式开关与备用屏的支持:

  1. CUP 四角跳转 / CUU CUD CUF CUB (计数 0 1 5) / CHA nG / VPA nd / CUP 0 参数 / 越界钳制
  2. 保存恢复光标: ESC 7 / ESC 8 与 CSI s / CSI u, 并检验保存点是否连属性一起恢复
  3. 擦除: ED 0/1/2/3、EL 0/1/2、ECH nX、DCH nP、ICH n@
  4. 行操作: IL nL、DL nM、SU nS、SD nT (滚动区内 / 区外)
  5. 滚动区 DECSTBM CSI top;bottom r (区内滚动时区外必须不动)
  6. 原点模式 DECOM CSI ?6h/?6l (CUP 1;1 落在滚动区左上)
  7. 自动换行 CSI ?7h/?7l 与延迟换行
  8. 制表位: 默认 8 列 / HTS ESC H / TBC CSI 0g / CSI 3g
  9. 插入模式 IRM CSI 4h/?4l
 10. DECALN ESC # 8 整屏填 E
 11. 字符集: ESC ( 0 进入 DEC 特殊图形, SO/SI 切 G1
 12. 光标形状 DECSCUSR CSI n q (0-6) 与显隐 CSI ?25l/h
 13. 备用屏: 1049 / 47 / 1047
 14. 窗口缩放与 CSI 18 t 自报尺寸
 15. DECCOLM CSI ?3h/?3l (危险, 询问用户后执行)
 16. 每个模式切换后用 DECRQM CSI ?n$p 自动判定, 收尾复位所有用过的模式

非交互模式 (TERMTEST_NONINTERACTIVE=1): 等待自动跳过; 依赖终端回包 (CPR/DECRQM) 的判定记 SKIP。
"""

import re
import sys
import termlib as T


# ---------------------------------------------------------------- 通用助手
def _size():
    c, r = T.term_size()
    return (c if c > 0 else 80, r if r > 0 else 24)


def _clip(text, cols):
    out = ""
    for ch in text:
        if T.dwidth(out + ch) > cols:
            break
        out += ch
    return out


def _put(row, col, text):
    """在绝对位置写文本 (termlib.put 只返回字符串, 这里直接写出)。"""
    T.write(T.cup(row, col) + text)


def _stage(title):
    """清屏并在第 1 行写标题, 余下屏幕交给绝对定位绘图。

    标题行必须以换行收尾: 否则随后按顺序打印的判定行会接在标题行尾上。
    """
    cols, rows = _size()
    T.clear()
    T.write(T.sgr(1, 36) + _clip("▌ " + title, cols) + T.sgr(0) + "\n")


def _pause(msg):
    """把提示写在倒数第二行并等待按键。

    使用 wait_key_silent(): 不打印带换行的提示, 否则换行会滚动屏幕,
    把刚画好的参考标记 (如"区外不动"标记) 推走, 造成误判。
    """
    cols, rows = _size()
    line = "  " + msg + "    [按任意键继续, q 退出]"
    T.write(T.cup(max(1, rows - 1), 1) + T.el(2) + T.sgr(33) + _clip(line, cols) + T.sgr(0))
    T.wait_key_silent()
    T.write(T.cup(max(1, rows - 1), 1) + T.el(2))


def _verdict(name, status, detail=""):
    """判定行统一落在提示行 (倒数第二行): 不压正在观察的画面, 也不触发滚动。

    绝对定位类小节里光标常停在画面中间或末列, 直接 T.row 会把判定行接在被测
    内容后面、甚至把刚画好的证据覆盖掉; 先归位到提示行就没有这个问题。
    """
    _cols, rows = _size()
    T.park_cursor(max(1, rows - 1))
    T.row(name, status, detail)


def _decide(name, question, detail=""):
    """在屏幕底部提问并判定 (非交互 → SKIP)。

    同样避免换行提示, 保持画面不被滚动 —— 提问与判定都只在倒数第二行进行。
    """
    cols, rows = _size()
    line = "  " + question + "   [y=是 / n=否, q 退出]"
    T.write(T.cup(max(1, rows - 1), 1) + T.el(2) + T.sgr(33) + _clip(line, cols) + T.sgr(0))
    if T.NONINTERACTIVE:
        _verdict(name, T.SKIP, detail or "非交互模式 / 未作答")
        return
    k = T.wait_key_silent()
    c = k[:1].lower()
    if c == b"y":
        _verdict(name, T.PASS, detail or question)
    elif c == b"n":
        _verdict(name, T.FAIL, detail or question)
    else:
        _verdict(name, T.SKIP, detail or "未作答")


# ---------------------------------------------------------------- 终端回包解析
def _cpr(timeout=0.2):
    """DSR 6 → CSI row;col R, 返回 (row, col) 或 None。"""
    resp = T.query(T.CSI + "6n", timeout=timeout)
    if not resp:
        return None
    m = re.search(rb"\x1b\[(\d+);(\d+)R", resp)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _decrqm(n, private=True, timeout=0.2):
    """DECRQM → CSI ?n;Ps$y (私有) / CSI n;Ps$y (ANSI), 返回 Ps 或 None。"""
    q = T.CSI + ("?%d$p" % n if private else "%d$p" % n)
    resp = T.query(q, timeout=timeout)
    if not resp:
        return None
    pat = (rb"\x1b\[\?%d;(\d+)\$y" % n) if private else (rb"\x1b\[%d;(\d+)\$y" % n)
    m = re.search(pat, resp)
    if not m:
        return None
    return int(m.group(1))


_RQM_NAME = {0: "未识别", 1: "置位", 2: "复位", 3: "永久置位", 4: "永久复位"}


def _rqm_row(name, st, n, expect, extra="", private=True, park=False):
    """已知 DECRQM 状态后打印判定行。

    探测与打印分开是必要的: 插入模式 (IRM) 打开时, 打印会把文字"插入"而不是覆盖,
    上一行的旧内容被右推出来, 判定行看着就像接了一截别人的尾巴。
    """
    row = _verdict if park else T.row
    if st is None:
        row(name, T.SKIP, "DECRQM %s无回包%s"
            % ("?%d$p " % n if private else "%d$p " % n, (" " + extra) if extra else ""))
        return
    ok = (st == expect)
    row(name, T.PASS if ok else T.DIFF,
        "DECRQM %s%d$p → %d(%s) 期望 %d%s"
        % ("?" if private else "", n, st, _RQM_NAME.get(st, "?"), expect,
           ("  " + extra) if extra else ""))


def _mode_row(name, n, expect, extra="", private=True, park=False):
    """查一次 DECRQM 并立刻打印判定行 (park=True 见 _verdict)。"""
    _rqm_row(name, _decrqm(n, private=private), n, expect, extra, private, park)


def _check_pos(name, cases, extra=""):
    """cases: [(标签, 序列字符串, 期望(r,c)), ...]; 逐个发送并查 CPR。"""
    results = []
    any_resp = False
    ok = True
    for label, seq, want in cases:
        T.write(seq)
        p = _cpr()
        if p is not None:
            any_resp = True
            if p != tuple(want):
                ok = False
        results.append("%s→%s" % (label, p))
    tail = ("  " + extra) if extra else ""
    if not any_resp:
        _verdict(name, T.SKIP, "CPR 无回包; " + "; ".join(results) + tail)
    else:
        _verdict(name, T.PASS if ok else T.FAIL, "; ".join(results) + tail)


def _fill(rows, cols, upto=None):
    """用可辨认的编号纹样铺满屏幕 (便于看出擦除/滚动的影响范围)。"""
    upto = upto or (rows - 2)
    for r in range(1, upto + 1):
        body = "".join(str((r + c) % 10) for c in range(min(cols - 1, 100)))
        T.write(T.cup(r, 1) + T.sgr(90) + body + T.sgr(0))


def _fill_rows(rows, cols, r1, r2, label):
    for r in range(r1, r2 + 1):
        T.write(T.cup(r, 1) + T.sgr(36) + _clip("%s 行 %-3d " % (label, r), cols - 2) + T.sgr(0))


# ---------------------------------------------------------------- 1. CUP 四角
def t_cup_corners():
    cols, rows = _size()
    bot = rows - 2
    _stage("CUP 四角跳转  CSI r;c H")
    _put(1, 1, T.sgr(1, 33) + "◤A(1,1)" + T.sgr(0))
    _put(1, max(1, cols - 9), T.sgr(1, 33) + "B(1,%d)◥" % cols + T.sgr(0))
    _put(bot, 1, T.sgr(1, 33) + "◣C(%d,1)" % bot + T.sgr(0))
    _put(bot, max(1, cols - 11), T.sgr(1, 33) + "D(%d,%d)◢" % (bot, cols) + T.sgr(0))
    _pause("四角应各有一个角标, 位置与括号内坐标一致")
    _check_pos("CUP四角", [
        ("(1,1)   ", T.cup(1, 1), (1, 1)),
        ("(1,%d)" % cols, T.cup(1, cols), (1, cols)),
        ("(%d,%d)" % (rows, cols), T.cup(rows, cols), (rows, cols)),
        ("(%d,1) " % rows, T.cup(rows, 1), (rows, 1)),
    ])
    T.clear()


# ---------------------------------------------------------------- 2. 相对移动
def t_relative():
    cols, rows = _size()
    _stage("CUU / CUD / CUF / CUB 相对移动 (计数 0 / 1 / 5)")
    r0 = max(4, rows // 2)
    c0 = max(4, cols // 3)
    _put(r0, c0, T.sgr(1, 33) + "O 起点" + T.sgr(0))
    T.write(T.cup(r0, c0))
    T.write(T.cuf(5) + T.sgr(31) + "→C 右5" + T.sgr(0))
    T.write(T.cud(3) + T.sgr(32) + "↓B 下3" + T.sgr(0))
    T.write(T.cub(4) + T.sgr(34) + "←D 左4" + T.sgr(0))
    T.write(T.cuu(2) + T.sgr(35) + "↑A 上2" + T.sgr(0))
    _pause("轨迹应为 右→下→左→上, 每段位移与标注数字一致")
    br = max(6, min(10, rows - 9))
    bc = max(4, min(10, cols - 12))
    cases = [
        ("cuf(0)", T.cup(br, bc) + T.cuf(0), (br, bc + 1)),
        ("cuf(1)", T.cup(br, bc) + T.cuf(1), (br, bc + 1)),
        ("cud(0)", T.cup(br, bc) + T.cud(0), (br + 1, bc)),
        ("cud(1)", T.cup(br, bc) + T.cud(1), (br + 1, bc)),
    ]
    if bc + 5 <= cols - 1:
        cases.append(("cuf(5)", T.cup(br, bc) + T.cuf(5), (br, bc + 5)))
    if br + 5 <= rows - 1:
        cases.append(("cud(5)", T.cup(br, bc) + T.cud(5), (br + 5, bc)))
    if bc - 2 >= 1:
        cases.append(("cub(0)", T.cup(br, bc) + T.cub(0), (br, bc - 1)))
        cases.append(("cub(2)", T.cup(br, bc) + T.cub(2), (br, bc - 2)))
    if br - 1 >= 1:
        cases.append(("cuu(1)", T.cup(br, bc) + T.cuu(1), (br - 1, bc)))
    if br - 5 >= 1:
        cases.append(("cuu(5)", T.cup(br, bc) + T.cuu(5), (br - 5, bc)))
    _check_pos("相对移动计数", cases, "计数 0 应等同 1")
    T.clear()


# ---------------------------------------------------------------- 3. CHA / VPA
def t_cha_vpa():
    cols, rows = _size()
    _stage("CHA nG 绝对列 / VPA nd 绝对行")
    r = max(3, rows // 2)
    c = max(5, cols // 2)
    _put(1, 1, T.sgr(90) + "".join(str(i % 10) for i in range(1, min(cols, 81))) + T.sgr(0))
    for rr in range(2, rows - 1):
        _put(rr, 1, T.sgr(90) + "." + T.sgr(0))
    T.write(T.cup(r, c) + T.sgr(1, 31) + "◆" + T.sgr(0))
    T.write(T.cup(1, 1))
    T.write(T.cha(c) + T.sgr(1, 32) + "◆" + T.sgr(0))
    T.write(T.vpa(r) + T.sgr(1, 34) + "◆" + T.sgr(0))
    _pause("CHA 应只横向跳到第 %d 列; VPA 应只纵向跳到第 %d 行" % (c, r))
    _check_pos("CHA/VPA", [
        ("cha(%d)" % c, T.cup(r, 1) + T.cha(c), (r, c)),
        ("vpa(%d)" % r, T.cup(1, 1) + T.vpa(r), (r, 1)),
        ("cha(1)  ", T.cup(r, c) + T.cha(1), (r, 1)),
    ])
    T.clear()


# ---------------------------------------------------------------- 4. CUP 0 参数
def t_cup_zero():
    _stage("CUP 0 参数: CSI 0;0H / CSI ;5H 等价于 1")
    _check_pos("CUP0参数", [
        ("0;0H", T.CSI + "0;0H", (1, 1)),
        (";H  ", T.CSI + ";H", (1, 1)),
        (";5H ", T.CSI + ";5H", (1, 5)),
        ("3;H ", T.CSI + "3;H", (3, 1)),
    ])
    _pause("CSI 0;0H 与 CSI ;H 应回到 (1,1); 省略的行参数应视为 1")
    T.clear()


# ---------------------------------------------------------------- 5. 越界钳制
def t_clamp():
    cols, rows = _size()
    _stage("越界坐标钳制 (不应跑到屏幕外)")
    res = []
    any_resp = False
    inrange = True
    for label, seq in (("999;999H", T.CSI + "999;999H"),
                       ("%d;%dH" % (rows + 8, cols + 8), T.cup(rows + 8, cols + 8))):
        T.write(seq)
        p = _cpr()
        if p is not None:
            any_resp = True
            if not (1 <= p[0] <= rows and 1 <= p[1] <= cols):
                inrange = False
        res.append("%s→%s" % (label, p))
    if not any_resp:
        _verdict("越界钳制", T.SKIP, "CPR 无回包; " + "; ".join(res))
    else:
        _verdict("越界钳制", T.PASS if inrange else T.FAIL,
                 "屏幕 %dx%d, 结果 " % (cols, rows) + "; ".join(res) + " (应被钳制在屏内)")
    _pause("光标应停在屏幕右下角内, 未跑到屏幕外")
    T.clear()


# ---------------------------------------------------------------- 6. 保存/恢复
def t_save_restore():
    cols, rows = _size()
    _stage("保存/恢复光标: ESC 7 / ESC 8 与 CSI s / CSI u")
    T.write(T.cup(5, 5) + T.ESC + "7")
    T.write(T.cup(12, max(1, cols // 2)) + T.sgr(31) + "已移动到其他位置" + T.sgr(0))
    T.write(T.ESC + "8" + T.sgr(32) + "← ESC8 恢复点(应回到 5,5)" + T.sgr(0))
    T.write(T.cup(8, 8) + T.CSI + "s")
    T.write(T.cup(15, max(1, cols // 2)) + T.sgr(34) + "已移动到其他位置" + T.sgr(0))
    T.write(T.CSI + "u" + T.sgr(35) + "← CSIu 恢复点(应回到 8,8)" + T.sgr(0))
    _pause("两处「恢复点」应分别落在 (5,5) 与 (8,8)")
    _check_pos("保存恢复位置", [
        ("ESC7/8", T.cup(5, 5) + T.ESC + "7" + T.cup(12, 30) + T.ESC + "8", (5, 5)),
        ("CSIs/u", T.cup(8, 8) + T.CSI + "s" + T.cup(15, 40) + T.CSI + "u", (8, 8)),
    ])

    _stage("保存点是否连属性一起恢复 (xterm 会)")
    T.write(T.sgr(31) + T.cup(6, 4) + T.ESC + "7")
    T.write(T.sgr(0) + T.cup(9, 4) + "中途已复位颜色")
    T.write(T.ESC + "8" + "ESC8 恢复后这段: 仍为红色 → 属性随保存点恢复" + T.sgr(0))
    T.write(T.cup(14, 4) + T.sgr(34) + T.CSI + "s" + T.sgr(0) + T.CSI + "u"
            + "CSIu 恢复后这段: 仍为蓝色 → 属性随保存点恢复" + T.sgr(0))
    _pause("观察恢复后的文字颜色是否回到保存时的颜色")
    _decide("保存恢复属性", "ESC8 / CSIu 恢复后文字颜色是否也回到保存时的颜色?")
    T.clear()


# ---------------------------------------------------------------- 7. 擦除
def t_erase_ed():
    cols, rows = _size()
    for n, label in ((0, "ED 0: 光标 → 屏幕末尾"), (1, "ED 1: 屏幕开头 → 光标"), (2, "ED 2: 整屏")):
        _stage("擦除显示 " + label)
        _fill(rows, cols)
        pos = (max(2, rows // 2), max(2, cols // 3))
        T.write(T.cup(pos[0], pos[1]) + T.ed(n))
        _pause("观察被清空的范围; 光标本身不应移动")
        _check_pos("ED%d 光标不动" % n, [("ED%d" % n, T.cup(pos[0], pos[1]) + T.ed(n), pos)])
        T.clear()

    _stage("ED 3: 擦除滚动缓冲 (可见屏幕不变)")
    _fill(rows, cols)
    T.write(T.ed(3))
    _pause("可见屏幕应保持不变; 请回滚 (滚轮/Shift+PgUp) 查看历史是否已被清空")
    _decide("ED3 清滚动缓冲", "回滚后历史内容是否确实已被清空 (屏幕内容不变)?")
    T.clear()


def t_erase_el():
    cols, rows = _size()
    _stage("擦除行 EL: CSI 0K / 1K / 2K")
    for i, (n, label) in enumerate(((0, "EL 0 光标→行尾"), (1, "EL 1 行首→光标"), (2, "EL 2 整行"))):
        r = 3 + i * 3
        _put(r, 1, T.sgr(90)
              + "".join(str((r + c) % 10) for c in range(min(cols - 1, 70))) + T.sgr(0))
        c = max(2, cols // 3)
        T.write(T.cup(r, c) + T.el(n) + T.sgr(33) + "|光标在此" + T.sgr(0))
    _pause("分别观察 0K / 1K / 2K 的影响范围")
    _check_pos("EL 光标不动", [
        ("EL0", T.cup(6, 10) + T.el(0), (6, 10)),
        ("EL1", T.cup(6, 10) + T.el(1), (6, 10)),
        ("EL2", T.cup(6, 10) + T.el(2), (6, 10)),
    ])
    T.clear()


def t_erase_ech_dch_ich():
    cols, rows = _size()
    base = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    _stage("ECH nX 擦除字符 / DCH nP 删除字符 / ICH n@ 插入空格")
    rows3 = ((4, "ECH 5X:", 5), (7, "DCH 5P:", 5), (10, "ICH 5@:", 5))
    for r, label, n in rows3:
        _put(r, 1, T.sgr(33) + label + T.sgr(0))
        _put(r, 10, base)
    T.write(T.cup(4, 10) + T.CSI + "5X")
    T.write(T.cup(7, 10) + T.CSI + "5P")
    T.write(T.cup(10, 10) + T.CSI + "5@")
    T.wln("")
    _pause("ECH 应把 5 个字符变空白; DCH 应删 5 个并把后续左移; ICH 应插入 5 个空白并右推")
    _check_pos("ECH/DCH/ICH 光标不动", [
        ("ECH", T.cup(4, 10) + T.CSI + "5X", (4, 10)),
        ("DCH", T.cup(7, 10) + T.CSI + "5P", (7, 10)),
        ("ICH", T.cup(10, 10) + T.CSI + "5@", (10, 10)),
    ])
    _decide("ECH/DCH/ICH 范围", "ECH / DCH / ICH 三者对文本的影响范围是否都正确?")
    T.clear()


# ---------------------------------------------------------------- 8. 行操作
def t_line_ops():
    cols, rows = _size()
    top, bot = 4, rows - 3

    _stage("IL nL 插入行 / DL nM 删除行 (未设滚动区)")
    _fill_rows(rows, cols, 2, rows - 2, "参考")
    T.write(T.cup(6, 1) + T.CSI + "2L")
    _pause("在第 6 行插入 2 个空行, 其余内容下移 2 行 (顶部 2 行被顶出)")
    _fill_rows(rows, cols, 2, rows - 2, "参考")
    T.write(T.cup(6, 1) + T.CSI + "3M")
    _pause("从第 6 行删除 3 行, 下方内容上移 3 行, 底部补空行")
    T.clear()

    _stage("IL / DL 在滚动区内 (DECSTBM %d..%d)" % (top, bot))
    T.write(T.CSI + "%d;%dr" % (top, bot))
    _put(1, 1, T.sgr(1, 31) + "★ 区外上方 (不应动) ★" + T.sgr(0))
    _put(rows - 2, 1, T.sgr(1, 31) + "★ 区外下方 (不应动) ★" + T.sgr(0))
    _fill_rows(rows, cols, top, bot, "区内")
    T.write(T.cup(top + 2, 1) + T.CSI + "2L")
    _pause("只在区内插入 2 行, 顶部被顶出; 区外上下标记应原地不动")
    _fill_rows(rows, cols, top, bot, "区内")
    T.write(T.cup(top + 1, 1) + T.CSI + "2M")
    _pause("只在区内删除 2 行; 区外上下标记仍应原地不动")
    _decide("IL/DL 区外不动", "IL / DL 在滚动区内操作时, 区外文本是否保持不动?")
    T.clear()

    _stage("SU nS 上滚 / SD nT 下滚 (滚动区 %d..%d)" % (top, bot))
    T.write(T.CSI + "%d;%dr" % (top, bot))
    _put(1, 1, T.sgr(1, 31) + "★ 区外上方 (不应动) ★" + T.sgr(0))
    _fill_rows(rows, cols, top, bot, "区内")
    T.write(T.cup(top, 1) + T.CSI + "3S")
    _pause("SU 3S: 区内整体上滚 3 行 (内容上移, 底部补空)")
    _fill_rows(rows, cols, top, bot, "区内")
    T.write(T.cup(top, 1) + T.CSI + "3T")
    _pause("SD 3T: 区内整体下滚 3 行 (内容下移, 顶部补空)")
    _decide("SU/SD 区外不动", "SU / SD 滚动时区外文本是否保持不动?")
    T.write(T.CSI + "r")
    T.clear()


# ---------------------------------------------------------------- 9. 滚动区
def t_scroll_region():
    cols, rows = _size()
    top, bot = 3, rows - 3
    _stage("滚动区 DECSTBM  CSI top;bottom r  (关键: 区内滚动时区外不动)")
    T.write(T.CSI + "%d;%dr" % (top, bot))
    _put(1, 1, T.sgr(1, 31) + "★ 区外上方 顶部标记 (必须始终不动) ★" + T.sgr(0))
    _put(rows - 2, 1, T.sgr(1, 31) + "★ 区外下方 底部标记 (必须始终不动) ★" + T.sgr(0))
    for i, r in enumerate(range(top, bot + 1)):
        _put(r, 3, T.sgr(36) + "区内第 %2d 行" % (i + 1) + T.sgr(0))
    T.write(T.cup(bot, 3))
    _pause("区外上/下标记固定; 区内有编号行; 记住区外标记的位置")
    T.write(T.cup(bot, 3) + "\n")
    _pause("在区底 LF: 区内整体上滚 1 行(第1行被顶出), 区外标记必须原地不动")
    T.write(T.cup(top, 3) + T.ESC + "M")
    _pause("在区顶 RI (ESC M): 区内整体下滚 1 行, 区外标记仍须不动")
    _decide("滚动区-区外不动", "区内滚动时, 区外上方/下方标记是否保持原地不动?")
    T.write(T.CSI + "r")
    T.clear()


# ---------------------------------------------------------------- 10. DECOM
def t_decom():
    cols, rows = _size()
    top, bot = 4, rows - 3
    _stage("原点模式 DECOM  CSI ?6h / ?6l")
    T.write(T.CSI + "%d;%dr" % (top, bot))
    T.write(T.CSI + "?6h")
    _mode_row("DECOM 置位", 6, 1, "发送 CSI ?6h")
    _check_pos("DECOM 开启后原点", [
        ("CUP1;1", T.cup(1, 1), (top, 1)),
    ], "期望 (1,1) → 区内左上 (%d,1)" % top)
    T.write(T.cup(1, 1) + T.sgr(1, 31) + "● 原点(1,1)" + T.sgr(0))
    _pause("开启 DECOM 后 CUP 1;1 应落在滚动区左上角 (第 %d 行)" % top)
    T.write(T.CSI + "?6l")
    _mode_row("DECOM 复位", 6, 2, "发送 CSI ?6l")
    _check_pos("DECOM 关闭后", [("CUP1;1", T.cup(1, 1), (1, 1))], "应回到屏幕左上")
    T.write(T.CSI + "r")
    T.clear()


# ---------------------------------------------------------------- 11. 自动换行
def t_autowrap():
    cols, rows = _size()
    _stage("自动换行 DECAWM  CSI ?7h 开 / ?7l 关, 及延迟换行")
    T.write(T.CSI + "?7h")
    _mode_row("自动换行置位", 7, 1, "发送 CSI ?7h (默认)")

    T.write(T.cup(3, 1) + "X" * cols)
    p = _cpr()
    if p is None:
        _verdict("延迟换行-满行", T.SKIP, "CPR 无回包")
    else:
        ok = (p[0] == 3)
        _verdict("延迟换行-满行", T.PASS if ok else T.DIFF,
                 "写满一行后 CPR=%s (应在第 3 行末列, 即延迟换行)" % (p,))
    T.write("Y")
    p2 = _cpr()
    if p2 is None:
        _verdict("延迟换行-再写1字", T.SKIP, "CPR 无回包")
    else:
        ok = (p2[0] == 4)
        _verdict("延迟换行-再写1字", T.PASS if ok else T.DIFF,
                 "再写 1 字后 CPR=%s (应换到第 4 行)" % (p2,))
    _pause("第 3 行应被 X 写满; 光标停在末列; 再写 Y 才换到第 4 行 (延迟换行)")

    _stage("关闭自动换行 CSI ?7l: 最后一列应被覆写")
    _put(5, 1, T.sgr(33) + "★ 第 6 行是标记行, 若发生换行会被覆盖 ★" + T.sgr(0))
    _put(3, 1, T.sgr(1) + "关闭 ?7l 之前(默认换行):" + T.sgr(0))
    T.write(T.cup(3, 30) + "Z" * (cols - 28))
    T.write(T.CSI + "?7l")
    _mode_row("自动换行复位", 7, 2, "发送 CSI ?7l", park=True)
    _put(8, 1, T.sgr(1) + "关闭 ?7l 之后(不换行):" + T.sgr(0))
    T.write(T.cup(8, 30) + "W" * (cols - 28))
    p3 = _cpr()
    if p3 is None:
        _verdict("关闭换行-末列覆写", T.SKIP, "CPR 无回包")
    else:
        ok = (p3[0] == 8)
        _verdict("关闭换行-末列覆写", T.PASS if ok else T.FAIL,
                 "关闭 ?7h 后写溢出字 CPR=%s (应留在第 8 行)" % (p3,))
    _pause("上方「默认换行」应换成到第 4 行; 下方「不换行」应留在第 8 行覆写末列, 第 6 行标记完好")
    T.write(T.CSI + "?7h")
    T.clear()


# ---------------------------------------------------------------- 12. 制表位
def _ruler_row(row, cols):
    s = ""
    for c in range(1, min(cols, 81)):
        if c % 10 == 0:
            s += str((c // 10) % 10)
        elif c % 5 == 0:
            s += "+"
        else:
            s += "."
    T.write(T.cup(row, 1) + T.sgr(90) + s + T.sgr(0))


def _set_default_tabs(cols):
    """按每 8 列重建制表位, 尽量还原默认。"""
    T.write(T.CSI + "3g")
    for c in range(9, cols, 8):
        T.write(T.cup(1, c) + T.ESC + "H")


def t_tabs():
    cols, rows = _size()
    _stage("制表位: 默认 8 列 / HTS ESC H 设置 / TBC CSI 0g 清当前 / CSI 3g 清全部")
    _ruler_row(1, cols)
    T.write(T.cup(3, 1) + "默认 Tab: |" + "\t" + "|" + "\t" + "|")
    T.write(T.cup(4, 1) + "\t")
    _check_pos("默认制表位", [("Tab@1", T.cup(4, 1) + "\t", (4, 9))], "默认每 8 列 → 第 9 列")

    c = max(20, min(cols - 4, 40))
    T.write(T.cup(6, 1) + "\t")
    T.write(T.cup(6, c) + T.ESC + "H")
    T.write(T.cup(7, 1) + "\t")
    p = _cpr()
    if p is None:
        _verdict("HTS 自定义制表位", T.SKIP, "CPR 无回包")
    else:
        ok = p[1] in (c, c + 1)
        _verdict("HTS 自定义制表位", T.PASS if ok else T.DIFF,
                 "在第 %d 列 HTS 后, 从第 1 列 Tab 落在第 %d 列 (期望 %d/%d)" % (c, p[1], c, c + 1))

    T.write(T.cup(8, c) + T.CSI + "0g")
    T.write(T.cup(9, 1) + "\t")
    p = _cpr()
    if p is None:
        _verdict("TBC 0g 清当前", T.SKIP, "CPR 无回包")
    else:
        ok = p[1] != c
        _verdict("TBC 0g 清当前", T.PASS if ok else T.DIFF,
                 "清掉第 %d 列制表位后, Tab 落在第 %d 列 (应跳过已清除的位)" % (c, p[1]))

    T.write(T.CSI + "3g")
    T.write(T.cup(10, 1) + "\t")
    p = _cpr()
    if p is None:
        _verdict("TBC 3g 清全部", T.SKIP, "CPR 无回包")
    else:
        ok = p[1] >= cols - 1
        _verdict("TBC 3g 清全部", T.PASS if ok else T.DIFF,
                 "清全部制表位后 Tab 落在第 %d 列 (通常应到行尾第 %d 列)" % (p[1], cols))

    _pause("刻度尺每 10 列标数字; 观察 Tab 跳转位置是否与刻度一致")
    T.note("注意: 本项修改了制表位, 现按每 8 列重建以近似默认")
    _set_default_tabs(cols)
    T.clear()


# ---------------------------------------------------------------- 13. IRM
def t_irm():
    cols, rows = _size()
    _stage("插入模式 IRM  CSI 4h 开 / CSI 4l 关")
    T.write(T.cup(3, 3) + "第3行(未插入): " + T.sgr(36) + "ABCDEFGH" + T.sgr(0))
    T.write(T.cup(4, 3) + "第4行(插入XX后): " + T.sgr(36) + "ABCDEFGH" + T.sgr(0))
    T.write(T.cup(4, 17) + T.CSI + "4h")
    _mode_row("IRM 置位", 4, 1, "发送 CSI 4h", private=False, park=True)
    # 判定行打完光标就移开了, 重新定位回测试位置: XX 必须在 (4,17) 处插入才有意义
    T.write(T.cup(4, 17) + "XX" + T.CSI + "4l")
    _mode_row("IRM 复位", 4, 2, "发送 CSI 4l", private=False, park=True)
    _pause("第 4 行应变成 ABXXCDEFGH (XX 插入后 CDEFGH 被右推); 第 3 行保持不变")
    _decide("IRM 插入推动", "开启 IRM 后插入字符是否把后续内容向右推动?")
    T.clear()


# ---------------------------------------------------------------- 14. DECALN
def t_decaln():
    _stage("DECALN  ESC # 8  整屏填 E (VT100 对齐测试)")
    _pause("即将执行 DECALN: 整屏会被填满大写 E。准备好后按任意键")
    T.write(T.ESC + "#8")
    _decide("DECALN 对齐", "整屏是否为规整的大写 E (行列严格对齐, 无残留/错行)?")
    T.write(T.CSI + "2J" + T.CSI + "H" + T.sgr(0))
    T.wln(T.sgr(32) + "  已用 CSI 2J 恢复屏幕。" + T.sgr(0))
    T.wln("")


# ---------------------------------------------------------------- 15. 字符集
def t_charset():
    cols, rows = _size()
    _stage("字符集: DEC 特殊图形 ESC ( 0, 用 ASCII 字母画框线")
    T.hint("要观察: 下面应由字母映射成制表符, 画出完整无缝的框")
    mapping = ("映射表: q=─ x=│ l=┌ k=┐ m=└ j=┘ n=┼ t=├ u=┤ v=┴ w=┬")
    _put(3, 3, T.sgr(33) + mapping + T.sgr(0))
    T.write(T.cup(5, 6) + T.sgr(36))
    T.write(T.ESC + "(0")
    T.write("lqqqqqqqqqqqqk\r\n")
    for _ in range(3):
        T.write("x            x\r\n")
    T.write("mqqqqqqqqqqqqj")
    T.write(T.ESC + "(B" + T.sgr(0))
    _pause("应是一个 14 列宽、5 行高的无缝框; 若显示为乱码字母则不支持 DEC 特殊图形")
    _decide("DEC 特殊图形集", "ESC(0 后字母 q x l k m j 是否被绘成制表符并画出无缝框?")

    _stage("SO/SI 切 G1 (ESC ) 0 指定 G1 为图形集, \\x0e 切 G1, \\x0f 切回 G0)")
    T.hint("老程序兼容的关键点: 先 ESC ) 0 指定 G1, 再用 SO(0x0E) / SI(0x0F) 切换")
    _put(3, 3, T.sgr(33) + "ESC ) 0 指定 G1 = 图形集; SO=0x0E 切 G1; SI=0x0F 切 G0" + T.sgr(0))
    T.write(T.cup(5, 6) + T.sgr(35))
    T.write(T.ESC + ")0")          # 指定 G1 = DEC 图形集
    T.write("\x0e")                # SO: 切到 G1
    T.write("lqqqqqqqqqqqqk\r\n")
    for _ in range(3):
        T.write("x            x\r\n")
    T.write("mqqqqqqqqqqqqj")
    T.write("\x0f")                # SI: 切回 G0 (=ASCII)
    T.write(T.ESC + "(B")
    T.write("  ← SI 之后这里应是普通 ASCII 字母")
    T.write(T.sgr(0))
    _pause("上半应为框线, SO 之后应是框, SI 之后应恢复为普通 ASCII 文字")
    _decide("SO/SI 切 G1", "SO(0x0E) 是否切到 G1 图形集, SI(0x0F) 是否切回 ASCII?")
    T.write("\x0f" + T.ESC + "(B")
    T.clear()


# ---------------------------------------------------------------- 16. 光标形状
def t_cursor_style():
    cols, rows = _size()
    styles = ((0, "默认"), (1, "闪烁块"), (2, "稳定块"), (3, "闪烁下划线"),
              (4, "稳定下划线"), (5, "闪烁竖线"), (6, "稳定竖线"))
    for n, label in styles:
        _stage("光标形状 DECSCUSR  CSI %d q   (%s)" % (n, label))
        _put(max(1, rows // 2 - 1), max(1, cols // 2 - 12),
              T.sgr(33) + "← 光标形状应为「%s」" % label + T.sgr(0))
        T.write(T.cup(rows // 2, cols // 2))
        T.cursor_style(n)
        _pause("观察光标形状: 期望「%s」; 按任意键看下一种" % label)
    T.cursor_style(0)
    _stage("光标显隐 DECTCEM  CSI ?25l / ?25h")
    T.write(T.cup(rows // 2, cols // 2))
    T.show_cursor(False)
    _pause("已发送 CSI ?25l, 光标应不可见")
    T.show_cursor(True)
    _pause("已发送 CSI ?25h, 光标应重新可见")
    _decide("光标形状/显隐", "0-6 光标形状与 ?25l/h 显隐是否都符合标注?")
    T.clear()


# ---------------------------------------------------------------- 17. 备用屏
def t_alt_screen():
    T.section("备用屏: 1049 / 47 / 1047")
    T.wln("")
    T.wln(T.sgr(1, 33) + "  主屏标记行 (进入备用屏再退出后, 下面两行必须还在):" + T.sgr(0))
    T.wln("  >>> 主屏标记 MAIN-SCREEN-MARKER <<<")
    T.wln("  (退出备用屏后请回滚到此处, 检查本行与滚动缓冲是否完好)")
    T.wln("")
    T.hint("接下来进入备用屏 1049, 在里面画内容, 再退出")
    T.wait_key("按任意键进入备用屏 CSI ?1049h")

    T.write(T.CSI + "?1049h")
    _mode_row("1049 备用屏置位", 1049, 1, "发送 CSI ?1049h")
    T.clear()
    T.wln(T.sgr(1, 36) + "  [备用屏] 这是备用屏内容 (1049)" + T.sgr(0))
    T.wln("  备用屏标记: " + T.sgr(4, 31) + "ALT-MARKER" + T.sgr(0))
    T.wait_key("按任意键退出备用屏 CSI ?1049l, 退出后回滚查看主屏标记")
    T.write(T.CSI + "?1049l")
    _mode_row("1049 备用屏复位", 1049, 2, "发送 CSI ?1049l")
    _decide("1049 备用屏", "退出 1049 后主屏内容与滚动缓冲是否完好无损?")

    T.wln("")
    T.hint("老变体差异: 47 = 只切缓冲不清屏; 1047 = 切缓冲并清屏; 1049 = 切缓冲 + 清屏 + 保存/恢复光标")
    T.wln("")
    T.wait_key("按任意键对比 47: CSI ?47h (注意进入后是否保留主屏残留)")
    T.write(T.CSI + "?47h")
    T.wln("")
    T.wln(T.sgr(1, 36) + "  [47 缓冲] 进入时未主动清屏: 若仍看到上面的主屏内容则说明 47 不清屏" + T.sgr(0))
    T.wln("  47 标记: " + T.sgr(4, 32) + "ALT-47" + T.sgr(0))
    T.wait_key("按任意键 CSI ?47l 退出")
    T.write(T.CSI + "?47l")
    _decide("47 老变体", "47 切缓冲时是否保留了主屏内容 (与 1049 行为不同)?")

    T.wln("")
    T.wait_key("按任意键对比 1047: CSI ?1047h (应清屏)")
    T.write(T.CSI + "?1047h")
    T.clear()
    T.wln(T.sgr(1, 36) + "  [1047 缓冲] 进入时若为空白则说明 1047 会清屏" + T.sgr(0))
    T.wait_key("按任意键 CSI ?1047l 退出, 回到主屏")
    T.write(T.CSI + "?1047l" + T.sgr(0))
    _decide("1047 老变体", "1047 进入备用屏时是否清屏 (区别于 47)?")
    T.wln("")


# ---------------------------------------------------------------- 18. 缩放
def t_resize():
    cols, rows = _size()
    old = (cols, rows)
    T.section("窗口缩放 (当前 %dx%d)" % old)
    T.hint("请拖拽窗口边缘改变大小; 程序会等待尺寸变化")
    T.clear()
    T.write(T.cup(1, 1) + T.sgr(1, 36) + "▌ 旧尺寸边框 (整屏铺满刻度, 用于观察重排)" + T.sgr(0))
    # 按旧尺寸铺满刻度行与边框
    for r in range(2, rows - 1):
        body = "".join(str((r + c) % 10) for c in range(min(cols - 1, 100)))
        T.write(T.cup(r, 1) + T.sgr(90) + body + T.sgr(0))
    T.write(T.cup(rows - 1, 1) + T.sgr(33) + "  请拖拽改变窗口大小..." + T.sgr(0))

    new = T.wait_resize(old)
    if new is None:
        _verdict("窗口缩放", T.SKIP, "非交互模式 / 超时未检测到尺寸变化")
    else:
        _verdict("窗口缩放", T.PASS if new != old else T.WARN,
                 "旧尺寸 %s → 新尺寸 %s" % (old, new))

    cols2, rows2 = _size()
    T.clear()
    T.wln(T.sgr(1, 36) + "▌ 缩放后的重排观察" + T.sgr(0))
    T.wln("")
    T.wln("  当前尺寸: %dx%d (旧尺寸 %dx%d)" % (cols2, rows2, old[0], old[1]))
    T.wln("  若上面的刻度在缩放时出现错行/行数不一致/图标与内容分离, 说明重排实现有差异")
    T.wln("")

    resp = T.query(T.CSI + "18t", timeout=0.3)
    if not resp:
        T.row("窗口尺寸自报", T.SKIP, "CSI 18 t 无回包 (终端未实现)")
    else:
        m = re.search(rb"\x1b\[8;(\d+);(\d+)t", resp)
        if not m:
            T.row("窗口尺寸自报", T.WARN,
                  "回包格式异常: " + T.dump_bytes(resp))
        else:
            rr, cc = int(m.group(1)), int(m.group(2))
            ok = (cc, rr) == (cols2, rows2)
            T.row("窗口尺寸自报", T.PASS if ok else T.DIFF,
                  "终端自报 %dx%d, 实际 %dx%d | %s"
                  % (cc, rr, cols2, rows2, T.dump_bytes(resp)))
    T.wln("")


# ---------------------------------------------------------------- 19. DECCOLM
def t_deccolm():
    T.section("DECCOLM 132/80 列切换 (危险: 多数终端会清屏)")
    T.note("CSI ?3h 切 132 列 / CSI ?3l 切 80 列, 很多终端会同时清屏并复位滚动区")
    r = T.ask_yn("是否执行 DECCOLM 切换测试?")
    if r is None:
        T.row("DECCOLM", T.SKIP, "非交互模式 / 未作答: 未执行")
        return
    if not r:
        T.row("DECCOLM", T.SKIP, "用户选择不执行")
        return
    T.write(T.CSI + "?3h")
    _mode_row("DECCOLM 132 置位", 3, 1, "发送 CSI ?3h")
    T.wln("  已切到 132 列 (若支持); 请观察列数与屏幕是否被清空。")
    T.write(T.CSI + "?3l")
    _mode_row("DECCOLM 80 复位", 3, 2, "发送 CSI ?3l")
    T.wln("  已切回 80 列。")
    T.wln("")


# ---------------------------------------------------------------- 20. 模式状态汇总
def t_modes_report():
    T.section("模式状态查询汇总 (DECRQM CSI ?n$p): 置位/复位各查一次")
    T.hint("状态 0=未识别 1=置位 2=复位; 无回包记 SKIP")
    T.mode_reset()               # 从确定状态开始查, 否则查到的是别人留下的状态
    plan = [
        (1, "DECCKM 应用光标键", "?1h", "?1l", True),
        (6, "DECOM 原点模式", "?6h", "?6l", True),
        (7, "DECAWM 自动换行", "?7h", "?7l", True),
        (25, "DECTCEM 光标可见", "?25h", "?25l", True),
        (4, "IRM 插入模式", "4h", "4l", False),
    ]
    got = []
    # 探测会把光标挪走时也得把位置存回来: DECOM (CSI ?6h/?6l) 会把光标送回滚动区
    # 左上, 之后若不归位, 下面这张表就从第 1 行开始盖掉小节标题。
    with T.SavedCursor():
        for n, name, on, off, priv in plan:
            T.write(T.CSI + on)
            st_on = _decrqm(n, private=priv)
            T.write(T.CSI + off)
            st_off = _decrqm(n, private=priv)
            got.append(("%s 置位" % name, st_on, n, 1, "CSI %s" % on, priv))
            got.append(("%s 复位" % name, st_off, n, 2, "CSI %s" % off, priv))
    # 切换与查询全部做完再统一打印: 这些模式本身会动光标或改变打印方式 ——
    # DECOM 把光标送回左上, IRM (CSI 4h) 让打印变成"插入"。
    # 边切边打印的话, 这 10 行会互相覆盖 (实测: 前两行在第 8/9 行, 后面全被顶回 1~8 行)。
    for name, st, n, expect, extra, priv in got:
        _rqm_row(name, st, n, expect, extra, priv)
    T.wln("")


# ---------------------------------------------------------------- 21. 收尾复位
def t_reset_modes():
    T.section("收尾: 复位本程序用过的所有模式")
    # 用 termlib 的统一复位序列 (比手写一份更全: 还含鼠标上报 / 括号粘贴 / 焦点 / 同步输出 /
    # 键盘协议), 免得这里的清单和别处漂移; DECCOLM 单独补一刀 —— 它会改窗口宽度, 只在本程序
    # 真动过它的地方收尾
    T.mode_reset()
    T.write(T.CSI + "?3l")
    T.row("模式复位", T.PASS, "已发送 mode_reset(): 鼠标/粘贴/焦点/同步/DECCKM/DECOM/IRM/自动换行"
                             "/光标可见/滚动区/字符集/键盘协议 回默认, 另补 ?3l (DECCOLM 80 列)")
    _mode_row("复位后 ?6 原点", 6, 2, "应为复位")
    _mode_row("复位后 ?7 自动换行", 7, 1, "应为置位 (默认)")
    _mode_row("复位后 ?1 应用光标键", 1, 2, "应为复位")
    _mode_row("复位后 4 插入模式", 4, 2, "应为复位", private=False)
    T.wln("")


# ---------------------------------------------------------------- 入口
def main():
    T.start("t_screen.py",
            ["光标定位", "保存恢复", "擦除", "行操作", "滚动区", "DECOM", "自动换行",
             "制表位", "IRM", "DECALN", "字符集", "光标形状", "备用屏", "缩放", "DECCOLM"],
            note="依赖终端回包的项目 (CPR/DECRQM) 会自动判定, 无回包则记 SKIP; 其余由人工观察")
    t_cup_corners()
    t_relative()
    t_cha_vpa()
    t_cup_zero()
    t_clamp()
    t_save_restore()
    t_erase_ed()
    t_erase_el()
    t_erase_ech_dch_ich()
    t_line_ops()
    t_scroll_region()
    t_decom()
    t_autowrap()
    t_tabs()
    t_irm()
    t_decaln()
    t_charset()
    t_cursor_style()
    t_alt_screen()
    t_resize()
    t_deccolm()
    t_modes_report()
    t_reset_modes()
    T.clear()
    T.finish()
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))