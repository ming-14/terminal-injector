# -*- coding: utf-8 -*-
"""t_keys_ext.py — 扩展键盘输入协议兼容性测试

检验范围:
  · kitty 键盘协议: 查询 CSI ? u, 打开 CSI > 1 u (disambiguate), 关闭 CSI < u
  · modifyOtherKeys: 打开 CSI > 4 ; 2 m, 查询 CSI ? 4 m, 关闭 CSI > 4 ; 0 m
  · win32-input-mode (Windows Terminal 扩展): CSI ? 9001 h / CSI ? 9001 l
  · 输出同一按键在 普通 / kitty / modifyOtherKeys 三种模式下的原始字节对照表并落盘

说明:
  · 每个模式结束都会恢复 (pop / 关闭), 退出前确保干净。
  · 符合规范记 PASS, 按键编码未变化记 DIFF (可能未实现该协议)。
  · TERMTEST_NONINTERACTIVE=1 时跳过全部按键采集与等待, 仅做能力查询并退出码 0。
"""

import os
import re
import sys
import time

import termlib as T

# 对照表默认只落盘按键名 (key_name 已能区分各协议的编码形式); 原始字节等价于用户击键
# 记录 —— 10 秒采集窗口内用户敲的第一个任意键也会被记进来。需要原始字节做逐字节分析时,
# 显式设 TERMTEST_RAW_KEYS=1 再跑。
RAW_KEYS = os.environ.get("TERMTEST_RAW_KEYS", "") not in ("", "0")

# 需要在各模式下采集的按键
KEYS = [
    ("Esc", "escape 键本身"),
    ("Ctrl+[", "与 Esc 同码, 看是否被区分"),
    ("Ctrl+I", "与 Tab 同码"),
    ("Ctrl+M", "与 Enter 同码"),
    ("Shift+Enter", ""),
    ("Ctrl+Enter", ""),
    ("Alt+x", ""),
    ("Shift+A", ""),
    ("Ctrl+Shift+A", ""),
]

# modifyOtherKeys / win32 只采集这几个键
MOK_KEYS = ["Alt+x", "Shift+Enter", "Ctrl+Shift+A"]
WIN_KEYS = ["Shift+Enter", "Ctrl+Enter", "Alt+x"]


_EOF_NOTED = [False]      # stdin 到 EOF 只提示一次, 免得每个键都刷一行


def _eof_give_up():
    """stdin 已经关了: 继续等下去每个键都要空耗 10 秒 (整段好几分钟), 直接收手。"""
    if not _EOF_NOTED[0]:
        _EOF_NOTED[0] = True
        T.note("stdin 已到 EOF (输入通道被关闭), 停止按键采集 —— "
               "对照表里的 '-' 是没采到, 不代表终端不支持这些键")
    return None


def _eof_skip(name):
    """采集阶段 stdin 被关了 → 记 SKIP, 别把"没采到"判成"协议没生效"。"""
    if T.input_alive():
        return False
    T.row(name, T.SKIP, "stdin 已到 EOF (输入通道被关闭), 采集未完成")
    return True


def _collect(label):
    """提示用户按下某个键, 返回这个键本身的字节 (None = 超时 / 无法读取)。

    一次读到的块里可能混着鼠标报文: 这里只取第一条真正的按键, 否则后面的
    re.fullmatch 字节对照会整条失配, 把"键没按对"误判成"协议没生效"。
    """
    T.hint("请按: %s" % label)
    if T.NONINTERACTIVE or not T.INPUT_OK:
        return None
    inp = T.InputReader()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        for unit in inp.read(timeout=0.5):
            ev = T.mouse_event(unit)
            if ev is not None:
                T.wln("    (忽略鼠标报文: %s)" % T.mouse_text(ev))
                continue
            T.wln("    %s → %s" % (label, T.key_name(unit)))
            T.wln("        HEX: %s" % T.hexdump(unit))
            return unit
        if not T.input_alive():
            return _eof_give_up()
    T.wln("    %s → (10 秒内无输入)" % label)
    return None


def _collect_set(keys, store):
    for label, _note in keys:
        store[label] = _collect(label)


def _collect_names(names, store):
    for name in names:
        store[name] = _collect(name)


def _any(store, pattern):
    """store 中是否存在匹配 pattern 的原始字节。"""
    for v in store.values():
        if v and re.fullmatch(pattern, v):
            return True
    return False


# ---------------------------------------------------------------- kitty
def _sec_kitty(store):
    T.section("kitty 键盘协议")
    T.hint("查询 CSI ? u 应回 CSI ? <flags> u; 打开用 CSI > 1 u, 关闭用 CSI < u。")
    T.mode_reset()          # 别的扩展键盘协议若还开着, 能力查询与编码对照都不作数
    r = T.query(T.CSI + "?u")
    if not r:
        T.row("kitty 能力查询", T.SKIP, "无回包, 该终端不实现该协议")
        return
    m = re.search(rb"\x1b\[\?([0-9;]*)u", r)
    if m:
        T.row("kitty 能力查询", T.PASS, "flags=%s  %s" % (m.group(1).decode(), T.dump_bytes(r)))
    else:
        T.row("kitty 能力查询", T.DIFF, "回包格式异常 " + T.dump_bytes(r))
    if T.NONINTERACTIVE or not T.INPUT_OK:
        T.row("kitty 按键编码", T.SKIP, "非交互模式 / 按键读取不可用")
        return
    T.hint("已打开 kitty 协议 (CSI > 1 u, disambiguate), 请依次按键:")
    # mode_scope: 进去先清空再开, 出来 (含异常) 清空 —— 关闭序列写在 termlib 里一处
    with T.mode_scope(on=[T.CSI + ">1u"]):
        _collect_set(KEYS, store)
    T.hint("已发送 CSI < u, 弹出扩展标志。")
    if _eof_skip("kitty 按键编码"):
        return
    hit = _any(store, rb"\x1b\[\d+;\d+(?::\d)?u")
    T.row("kitty 按键编码", T.PASS if hit else T.DIFF,
          "出现 CSI code;mods u 形式" if hit else "按键编码未变化, 可能未实现该协议")


# ---------------------------------------------------------------- modifyOtherKeys
def _sec_mok(store):
    T.section("modifyOtherKeys")
    T.hint("打开 CSI > 4 ; 2 m; 查询 CSI ? 4 m 应回 CSI > 4 ; n m; 关闭 CSI > 4 ; 0 m。")
    T.mode_reset()
    with T.mode_scope(on=[T.CSI + ">4;2m"]):     # 出块自动复位 (>4;0m 在 termlib 的复位序列里)
        q = T.query(T.CSI + "?4m")
        m = re.search(rb"\x1b\[>4;(\d+)m", q) if q else None
        if m:
            T.row("modifyOtherKeys 查询", T.PASS, "回包 CSI > 4 ; %s m" % m.group(1).decode())
        else:
            T.row("modifyOtherKeys 查询", T.SKIP,
                  "无回包, 该终端不实现" + (" " + T.dump_bytes(q) if q else ""))
        if T.NONINTERACTIVE or not T.INPUT_OK:
            T.row("modifyOtherKeys 按键编码", T.SKIP, "非交互模式 / 按键读取不可用")
            return
        T.hint("已打开 modifyOtherKeys, 请按: Alt+x / Shift+Enter / Ctrl+Shift+A")
        _collect_names(MOK_KEYS, store)
    T.hint("已发送 CSI > 4 ; 0 m 关闭。")
    if _eof_skip("modifyOtherKeys 按键编码"):
        return
    hit = _any(store, rb"\x1b\[27;\d+;\d+~")
    T.row("modifyOtherKeys 按键编码", T.PASS if hit else T.DIFF,
          "出现 CSI 27;mod;code~ 形式" if hit else "按键编码未变化, 可能未实现该协议")


# ---------------------------------------------------------------- win32-input-mode
def _sec_win32(store):
    T.section("win32-input-mode (Windows Terminal 扩展)")
    T.hint("CSI ? 9001 h 探测, 期待 CSI VK;Sc;Uc;Kd;Cs;Rc _ 形式; CSI ? 9001 l 恢复。")
    if T.NONINTERACTIVE or not T.INPUT_OK:
        T.row("win32-input-mode 按键编码", T.SKIP, "非交互模式 / 按键读取不可用")
        return
    T.mode_reset()
    with T.mode_scope(on=[9001]):        # 出块自动 ?9001l (在 termlib 的复位序列里)
        T.hint("已打开 win32-input-mode, 请按: Shift+Enter / Ctrl+Enter / Alt+x")
        _collect_names(WIN_KEYS, store)
    T.hint("已发送 CSI ? 9001 l 恢复。")
    if _eof_skip("win32-input-mode 按键编码"):
        return
    hit = _any(store, rb"\x1b\[\d+;\d+;\d+;\d+;\d+;\d+_")
    T.row("win32-input-mode 按键编码", T.PASS if hit else T.DIFF,
          "出现 win32-input 形式" if hit else "按键编码未变化, 可能未实现该协议")


# ---------------------------------------------------------------- 基线
def _sec_baseline(store):
    T.section("基线 · 普通模式按键编码")
    T.hint("未开启任何扩展协议, 采集 9 个按键的原始字节作为对照基线。")
    if T.NONINTERACTIVE or not T.INPUT_OK:
        T.row("普通模式基线", T.SKIP, "非交互模式 / 按键读取不可用")
        return
    _collect_set(KEYS, store)
    n = sum(1 for v in store.values() if v)
    if not T.input_alive():
        T.row("普通模式基线", T.SKIP, "stdin 已到 EOF, 采集未完成 (%d/%d)" % (n, len(KEYS)))
    else:
        T.row("普通模式基线", T.PASS if n else T.SKIP, "采集到 %d/%d 个按键" % (n, len(KEYS)))


# ---------------------------------------------------------------- 对照表
def _comparison(normal, kitty, mok, win32):
    T.section("对照表 · 同一按键在普通 / kitty / modifyOtherKeys 下的原始字节")
    T.hint("请与 Windows Terminal / 其他终端对照; 完整 (含 win32-input) 表格已写入报告。")

    cols = max(80, T.term_size()[0])
    kw = 14
    cw = max(12, (cols - kw - 12) // 3)
    head = "  " + T.pad("按键", kw) + " | " + T.pad("普通", cw) + " | " \
           + T.pad("kitty", cw) + " | " + T.pad("modifyOtherKeys", cw)
    T.wln(T.sgr(1) + head + T.sgr(0))
    modes = [("普通", normal), ("kitty", kitty), ("modifyOtherKeys", mok)]
    for label, _note in KEYS:
        cells = []
        for _name, st in modes:
            b = st.get(label)
            cells.append(T.hexdump(b)[:cw - 1] if b else "-")
        T.wln("  " + T.pad(label, kw) + " | "
              + " | ".join(T.pad(c, cw) for c in cells))

    # 落盘 (含 win32-input 列; 默认只记按键名, 原始字节需 TERMTEST_RAW_KEYS=1)
    body = ["t_keys_ext.py 按键编码对照表", "=" * 52,
            "(默认只记按键名, 原始字节不落盘; 设 TERMTEST_RAW_KEYS=1 记录原始字节)"
            if not RAW_KEYS else "(含原始字节: TERMTEST_RAW_KEYS=1)",
            ""]
    all_modes = modes + [("win32-input", win32)]
    for label, _note in KEYS:
        body.append("按键: %s" % label)
        for name, st in all_modes:
            b = st.get(label)
            if not b:
                body.append("    %-16s %s" % (name, "(无输入)"))
            elif RAW_KEYS:
                body.append("    %-16s %s" % (name, T.dump_bytes(b)))
            else:
                body.append("    %-16s %s" % (name, T.key_name(b)))
        body.append("")
    p = T.save_report("t_keys_ext", "\n".join(body) + "\n")
    if p:
        T.hint("对照表已写入报告: %s" % T.safe_path(p))
    else:
        T.note("报告写入失败")


def main():
    T.start("t_keys_ext.py",
            ["kitty 键盘协议", "modifyOtherKeys", "win32-input-mode", "三模式字节对照表"],
            note="检验扩展键盘协议; 每个模式结束都会恢复, 请按提示逐个按键")
    normal, kitty, mok, win32 = {}, {}, {}, {}
    try:
        _sec_baseline(normal)
        _sec_kitty(kitty)
        _sec_mok(mok)
        _sec_win32(win32)
    finally:
        # 中途 q/Esc 退出也把已经采到的字节写成对照表, 别让采集白做
        _comparison(normal, kitty, mok, win32)
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))