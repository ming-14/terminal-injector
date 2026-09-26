# -*- coding: utf-8 -*-
"""t_osc.py — 终端 OSC (Operating System Command) 扩展序列兼容性测试

检验范围:
  · OSC 0/1/2      窗口标题与图标名 (无法读回, 只做视觉判定)
  · OSC 8          可点击超链接 (含多链接 / id 参数 / 空 URI 结束)
  · OSC 52         剪贴板读写 (会改动剪贴板, 先征得同意)
  · OSC 7          上报当前工作目录 file://host/path (仅提示)
  · OSC 9 / 777    桌面通知 (会弹系统通知, 先征得同意)
  · OSC 9;4        任务栏 / 标签进度条
  · OSC 133        shell 集成标记 A/B/C/D
  · OSC 4          256 色调色板查询与单项改写
  · OSC 10/11/12   前景 / 背景 / 光标颜色查询与设置
  · OSC 104/110/111/112  调色板与各颜色重置 (谨慎使用)

本文件仅使用标准库, 不修改 termlib.py。
设置 TERMTEST_NONINTERACTIVE=1 时可无人值守跑完 (跳过等待与一切改动类操作)。
"""

import base64
import hashlib
import random
import re
import sys
import time

import termlib as T


# ---------------------------------------------------------------- 小工具
def _req(payload):
    """构造 OSC 请求: ESC ] payload ST。"""
    return T.OSC + payload + T.ST


def _dd_color(data):
    """颜色类回包的落盘摘要: 颜色三元组以 sha256 前 8 位代替原值。

    终端配色方案 (16 色调色板 + 前景/背景/光标色) 是可反向锁定终端与主题的
    稳定指纹, 报告只保留"同一颜色跨运行可比对"的哈希, 不保留原值。
    """
    if not data:
        return "(无响应)"
    parts = []
    for p in _osc_payloads(data):
        head = p.split(";", 2)
        col = _color_in(p) if len(head) >= 2 else None
        if col:
            h = hashlib.sha256(("%d,%d,%d" % col).encode("ascii")).hexdigest()[:8]
            parts.append("%s;%s;rgb=<哈希 %s>" % (head[0], head[1] if len(head) > 1 else "", h))
        else:
            parts.append("<载荷 %d 字节>" % len(p))
    if not parts:
        return "%d 字节 (sha256[:8]=%s)" % (len(data), hashlib.sha256(data).hexdigest()[:8])
    return " | ".join(parts) + "  (颜色值已哈希, 原值不落盘)"


def _osc_payloads(data):
    """拆出回包中的所有 OSC 有效载荷字符串 (去掉 ESC ] 与 ST/BEL)。"""
    if not data:
        return []
    s = data.decode("utf-8", "replace")
    out = []
    for part in re.split(r"\x1b\\|\x07", s):
        i = part.find("\x1b]")
        if i >= 0:
            out.append(part[i + 2:])
    return out


def _scale(hexstr):
    """把 1~4 位十六进制分量归一到 0~255。"""
    n = int(hexstr, 16)
    mx = (16 ** len(hexstr)) - 1
    return int(round(n * 255.0 / mx)) if mx else 0


def _color_in(payload):
    """从 OSC 载荷解析颜色: 支持 rgb:RRRR/GGGG/BBBB 与 #RRGGBB。"""
    m = re.search(r"rgb:([0-9A-Fa-f]{1,4})/([0-9A-Fa-f]{1,4})/([0-9A-Fa-f]{1,4})", payload)
    if m:
        return (_scale(m.group(1)), _scale(m.group(2)), _scale(m.group(3)))
    m = re.search(r"#([0-9A-Fa-f]{6})", payload)
    if m:
        v = m.group(1)
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    return None


def _rgb16(c):
    """(r,g,b) -> 'rgb:rr/gg/bb' (写入用)。"""
    return "rgb:%02x/%02x/%02x" % c


def _near(a, b, tol=10):
    """颜色近似比较 (终端可能量化)。"""
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _b64dec(s):
    s = re.sub(r"\s+", "", s)
    return base64.b64decode(s + "=" * ((-len(s)) % 4))


def _judge(name, a, ok_text, bad_text):
    """把 ask_yn 的 True/False/None 映射为 PASS/DIFF/SKIP。"""
    if a is True:
        T.row(name, T.PASS, ok_text)
    elif a is False:
        T.row(name, T.DIFF, bad_text)
    else:
        T.row(name, T.SKIP, "未作答 / 非交互模式")


# ---------------------------------------------------------------- OSC 0/1/2 标题
def _sec_titles():
    T.section("OSC 0 / 1 / 2 · 窗口标题与图标名")
    T.hint("标题无法被程序读回, 只能由你肉眼判断标题栏 / 标签 / 任务栏。")
    cases = [
        ("OSC 2 窗口标题", "2", "termtest: OSC2 窗口标题"),
        ("OSC 0 标题+图标名", "0", "termtest: OSC0 标题与图标名"),
        ("OSC 1 图标名", "1", "termtest-icon"),
    ]
    for name, code, text in cases:
        T.write(_req("%s;%s" % (code, text)))
        T.wln("  已发送 OSC %s -> “%s”" % (code, text))
        a = T.ask_yn("标题栏 / 标签是否已变为 “%s” ?" % text)
        _judge(name, a, "标题已更新", "未观察到变化, 该终端可能不支持该 OSC")


# ---------------------------------------------------------------- OSC 8 超链接
def _sec_links():
    T.section("OSC 8 · 超链接")
    T.hint("多数终端支持 悬停看 URL / Ctrl+点击 打开; 提示符中也应能正常显示。")

    def _l(params, url, text):
        return T.OSC + "8;" + params + ";" + url + T.ST + text + T.OSC + "8;;" + T.ST

    T.wln("  单个链接: " + _l("", "https://example.com/termtest", "example.com 示例链接"))
    T.wln("  多链接文本: " + _l("", "https://example.com/a", "链接A")
          + "  |  " + _l("", "https://example.com/b", "链接B")
          + "  |  " + _l("", "https://example.com/c", "链接C"))
    T.wln("  带 id 参数的链接: " + _l("id=termtest-42", "https://example.com/id", "带 id 的链接")
          + "  (同一 id 的多段链接会被合并)")
    T.wln("  空 URI 结束链接: "
          + T.OSC + "8;;https://example.com/d" + T.ST + "正常链接" + T.OSC + "8;;" + T.ST
          + " 之后应不再有链接")
    a = T.ask_yn("悬停是否显示 URL / Ctrl+点击 是否能打开?")
    _judge("OSC 8 超链接", a, "链接可识别", "未被识别为链接 (可能不实现)")


# ---------------------------------------------------------------- OSC 52 剪贴板
def _sec_clipboard(rep):
    T.section("OSC 52 · 剪贴板")
    T.hint("写入: OSC 52;c;BASE64 ST   读取: OSC 52;c;? ST (部分终端要求授权)。")
    agree = T.ask_yn("OSC 52 测试会读取并临时改写系统剪贴板, 是否继续?")
    if agree is not True:
        T.row("OSC 52 剪贴板读写", T.SKIP, "未同意 / 非交互模式: 不改动剪贴板")
        rep.append("OSC 52: 跳过 (未征得同意或非交互模式)")
        return

    def _read():
        r = T.query(_req("52;c;?"))
        if r:
            # 剪贴板里可能有用户复制的口令/密钥/验证码: 只记长度与哈希前 8 位
            # (同一内容跨运行可比对, 但无法还原), 原始回包不落盘。
            rep.append("OSC 52 读取回包: %d 字节, sha256[:8]=%s (内容不落盘)"
                       % (len(r), hashlib.sha256(r).hexdigest()[:8]))
        else:
            rep.append("OSC 52 读取回包: (无响应)")
        for p in _osc_payloads(r):
            parts = p.split(";", 2)
            if len(parts) == 3 and parts[0] == "52":
                try:
                    return _b64dec(parts[2]).decode("utf-8", "replace")
                except Exception:
                    return None
        return None

    original = _read()
    if original is not None:
        T.hint("已读到剪贴板原内容 (%d 字符), 结束时将尝试恢复。" % len(original))

    token = "termtest-%06d" % random.randint(0, 999999)
    T.write(_req("52;c;" + base64.b64encode(token.encode("utf-8")).decode("ascii")))
    T.hint("已写入剪贴板: %s" % token)
    time.sleep(0.2)
    got = _read()
    if got == token:
        T.row("OSC 52 剪贴板读写", T.PASS, "读回内容与写入一致")
    elif got is None:
        T.row("OSC 52 剪贴板读写", T.SKIP, "无有效回包, 该终端不实现 / 未授权读取")
    else:
        T.row("OSC 52 剪贴板读写", T.FAIL,
              "读回 %d 字符 (期望 %d 字符, 内容不落盘)" % (len(got), len(token)))

    # 恢复
    if original is not None:
        T.write(_req("52;c;" + base64.b64encode(original.encode("utf-8")).decode("ascii")))
        T.hint("已尝试恢复剪贴板原内容。")
    else:
        T.hint("未能读到原有内容, 剪贴板保留测试文本 (可手动覆盖)。")


# ---------------------------------------------------------------- OSC 7 cwd
def _sec_cwd():
    T.section("OSC 7 · 上报工作目录")
    # 主机名与路径用固定测试值: 真实的 file://<主机名>/<本机路径> 会被 Windows
    # Terminal / VSCode 等持久化到终端自身配置里, 属于跨机器可关联的身份信息。
    # 序列格式本身 (OSC 7;file://host/path ST) 不变, 终端照样能测。
    uri = "file://termtest-host/termtest/cwd"
    T.write(_req("7;" + uri))
    T.hint("已上报: %s  (主机与路径为固定测试值, 不含本机信息)" % uri)
    T.hint("用途: 支持时新建标签页 / 分屏会继承当前目录 (无回包, 无法自动判定)。")
    T.row("OSC 7 上报工作目录", T.SKIP, "仅提示: 无回包, 无法自动判定")


# ---------------------------------------------------------------- OSC 9/777 通知
def _sec_notify():
    T.section("OSC 9 / 777 · 桌面通知")
    T.hint("OSC 9;<文本> 为常见通知; OSC 777;notify;<标题>;<正文> 为 urxvt 风格。")
    a = T.ask_yn("将发送一条系统桌面通知 (会弹窗), 是否继续?")
    if a is not True:
        T.row("OSC 9 桌面通知", T.SKIP, "未同意 / 非交互模式: 不发送")
        T.row("OSC 777 urxvt 通知", T.SKIP, "未同意 / 非交互模式: 不发送")
        return
    T.write(_req("9;termtest 桌面通知测试"))
    b = T.ask_yn("是否弹出系统通知?")
    _judge("OSC 9 桌面通知", b, "已弹出通知", "未观察到通知 (可能不实现)")
    T.write(_req("777;notify;termtest;OSC 777 通知测试"))
    c = T.ask_yn("是否弹出 urxvt 风格通知?")
    _judge("OSC 777 urxvt 通知", c, "已弹出通知", "未观察到通知 (可能不实现)")


# ---------------------------------------------------------------- OSC 9;4 进度
def _sec_progress():
    T.section("OSC 9;4 · 任务栏 / 标签进度条")
    T.hint("state: 1 正常 / 2 错误 / 3 不确定 / 4 暂停, 最后用 0 清除。")
    if T.NONINTERACTIVE:
        T.row("OSC 9;4 进度条", T.SKIP, "非交互模式: 跳过动画与视觉判定")
        return
    for pct in range(0, 101, 5):
        T.write(T.progress_osc(1, pct))
        time.sleep(0.05)
    T.write(T.progress_osc(2, 70)); T.hint("state=2 错误 (通常显示红色)"); time.sleep(0.6)
    T.write(T.progress_osc(4, 40)); T.hint("state=4 暂停 (通常显示黄色)"); time.sleep(0.6)
    T.write(T.progress_osc(3, 0)); T.hint("state=3 不确定 (来回滚动)"); time.sleep(0.9)
    T.write(T.progress_osc(0, 0))
    T.hint("state=0 已清除进度条。")
    a = T.ask_yn("任务栏 / 标签是否出现进度条并正常变化?")
    _judge("OSC 9;4 进度条", a, "进度条正常", "未观察到进度条 (可能不实现)")


# ---------------------------------------------------------------- OSC 133 shell 集成
def _sec_133():
    T.section("OSC 133 · shell 集成标记")
    T.hint("A=提示符开始 B=提示符结束 C=命令输出开始 D=命令结束; 支持时提示符可跳转 / 高亮。")
    T.write(_req("133;A") + "user@termtest:~$ " + _req("133;B") + "ls -l" + _req("133;C"))
    T.wln("")
    T.wln("  (以上一行由 OSC 133 A/B/C 标记包裹)")
    T.write(_req("133;D;0"))
    T.wln("  (OSC 133;D;0 命令结束, 退出码 0)")
    T.row("OSC 133 shell 集成标记", T.SKIP, "仅提示: 无回包, 不做自动判定")


# ---------------------------------------------------------------- OSC 4 调色板
def _sec_palette(rep):
    T.section("OSC 4 · 256 色调色板查询与改写")
    T.hint("查询 OSC 4;n;? ST, 回包形如 OSC 4;n;rgb:RRRR/GGGG/BBBB ST。")
    indices = [0, 1, 2, 7, 15, 16, 231, 255]
    got = {}
    any_resp = False
    for i in indices:
        r = T.query(_req("4;%d;?" % i))
        rep.append("OSC 4 %d 查询 -> %s" % (i, _dd_color(r)))
        if r:
            any_resp = True
        col = None
        for p in _osc_payloads(r):
            if p.startswith("4;"):
                col = _color_in(p)
                if col:
                    break
        got[i] = col
        if col:
            T.wln("  索引 %3d = #%02x%02x%02x  %s" % (
                i, col[0], col[1], col[2], T.bg_rgb(*col) + "        " + T.sgr(0)))
        else:
            T.wln("  索引 %3d = (无回包)" % i)
    if not any_resp:
        T.row("OSC 4 调色板查询", T.SKIP, "该终端不实现此序列")
    else:
        n = sum(1 for v in got.values() if v)
        T.row("OSC 4 调色板查询", T.PASS if n == len(indices) else T.DIFF,
              "读到 %d/%d 个索引颜色" % (n, len(indices)))

    if T.NONINTERACTIVE:
        T.row("OSC 4 调色板改写", T.SKIP, "非交互模式: 不改动终端调色板")
        return

    orig = got.get(16)
    test = (255, 0, 160)
    try:
        T.write(_req("4;16;" + _rgb16(test)))
        T.wln("  已将索引 16 设为 %s: %s  <- 应显示为亮洋红" % (
            _rgb16(test), T.bg_rgb(*test) + "  16 色  " + T.sgr(0)))
        r = T.query(_req("4;16;?"))
        rep.append("OSC 4 16 验证 -> " + _dd_color(r))
        cur = None
        for p in _osc_payloads(r):
            if p.startswith("4;"):
                cur = _color_in(p)
        if _near(cur, test):
            T.row("OSC 4 调色板改写", T.PASS, "查询验证一致 (%s)" % _rgb16(cur))
        elif cur is None:
            T.row("OSC 4 调色板改写", T.SKIP, "改写后无回包, 该终端不实现 / 无法验证")
        else:
            T.row("OSC 4 调色板改写", T.DIFF, "回包 %s 与设置不一致" % _rgb16(cur))
    finally:
        # 必须放在 finally: 调色板改动会留在终端里, 进程退出也没人替你恢复
        # (restore_all 只复位模式, 不含 OSC 4), 中途按 q 退出也一样
        if orig:
            T.write(_req("4;16;" + _rgb16(orig)))
            T.hint("已写回索引 16 原值 %s" % _rgb16(orig))
        else:
            T.write(_req("104;16"))
            T.hint("已用 OSC 104;16 重置索引 16")


# ---------------------------------------------------------------- OSC 10/11/12 颜色
def _sec_colors(rep):
    T.section("OSC 10 / 11 / 12 · 前景 / 背景 / 光标颜色")
    T.hint("查询 OSC n;? ST, 设置 OSC n;rgb:r/g/b ST, 重置 OSC 1nn ST。")
    specs = [(10, "前景色", 110), (11, "背景色", 111), (12, "光标色", 112)]
    saved = {}
    for code, label, _rst in specs:
        r = T.query(_req("%d;?" % code))
        rep.append("OSC %d 查询 -> %s" % (code, _dd_color(r)))
        col = None
        for p in _osc_payloads(r):
            if p.startswith("%d;" % code):
                col = _color_in(p)
        saved[code] = col
        T.wln("  %s: %s" % (label, ("#%02x%02x%02x" % col) if col else "(无回包)"))

    if T.NONINTERACTIVE:
        for code, label, _rst in specs:
            T.row("OSC %d 设置%s" % (code, label), T.SKIP, "非交互模式: 不改动颜色")
        return

    tests = {10: (255, 200, 0), 11: (16, 16, 48), 12: (0, 255, 128)}
    # 只动"能读到原值"的那几个: 读不到就无从恢复, 而把颜色重置成标准色等于把用户自己
    # 的配色顶掉 —— 那比少测一项糟得多。
    targets = [sp for sp in specs if saved.get(sp[0])]
    for code, label, _rst in specs:
        if not saved.get(code):
            T.row("OSC %d 设置%s" % (code, label), T.SKIP,
                  "查询不到原值 → 不改动 (改了无法恢复, 会顶掉用户自己的配色)")
    if not targets:
        T.wln(T.sgr(90) + "  三个颜色都读不到原值, 本节不改动终端配色。" + T.sgr(0))
        return
    try:
        for code, label, _rst in targets:
            test = tests[code]
            T.write(_req("%d;%s" % (code, _rgb16(test))))
            time.sleep(0.15)
            r = T.query(_req("%d;?" % code))
            rep.append("OSC %d 验证 -> %s" % (code, _dd_color(r)))
            cur = None
            for p in _osc_payloads(r):
                if p.startswith("%d;" % code):
                    cur = _color_in(p)
            if _near(cur, test):
                T.row("OSC %d 设置%s" % (code, label), T.PASS, "查询验证一致 (%s)" % _rgb16(cur))
            elif cur is None:
                T.row("OSC %d 设置%s" % (code, label), T.SKIP, "无回包, 该终端不实现 / 无法验证")
            else:
                T.row("OSC %d 设置%s" % (code, label), T.DIFF, "回包 %s 与设置不一致" % _rgb16(cur))
    finally:
        for code, _label, _rst in targets:
            T.write(_req("%d;%s" % (code, _rgb16(saved[code]))))
        T.hint("已在 finally 中把改过的颜色写回原值。")


# ---------------------------------------------------------------- OSC 104/110/111/112 重置
def _sec_resets():
    T.section("OSC 104 / 110 / 111 / 112 · 重置")
    T.hint("104 重置调色板 (104;n 单个, 104 全部); 110/111/112 重置前景 / 背景 / 光标色。")
    T.note("这些序列会改动终端外观, 请谨慎使用; 本测试仅按索引做单项重置, 不做全量重置。")
    T.row("OSC 104/110/111/112 重置", T.SKIP, "仅说明: 避免全量重置影响终端外观")


def _write_report(rep):
    body = "t_osc.py OSC 原始回包记录\n" + "=" * 48 + "\n" + "\n".join(rep) + "\n"
    p = T.save_report("t_osc", body)
    if p:
        T.hint("原始回包已写入报告: %s" % T.safe_path(p))
    else:
        T.note("报告写入失败")


def main():
    T.start("t_osc.py",
            ["OSC 0/1/2 标题", "OSC 8 超链接", "OSC 52 剪贴板", "OSC 7 cwd",
             "OSC 9/777 通知", "OSC 9;4 进度", "OSC 133 shell", "OSC 4 调色板",
             "OSC 10/11/12 颜色", "OSC 104/110/111/112"],
            note="逐项检验 OSC 扩展序列; 改动类操作先征得同意, 非交互模式自动跳过")
    rep = []
    _sec_titles()
    _sec_links()
    _sec_clipboard(rep)
    _sec_cwd()
    _sec_notify()
    _sec_progress()
    _sec_133()
    _sec_palette(rep)
    _sec_colors(rep)
    _sec_resets()
    T.set_title("termtest: 完成")
    T.hint("标题已设为 “termtest: 完成”")
    _write_report(rep)
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))