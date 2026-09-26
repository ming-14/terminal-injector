# -*- coding: utf-8 -*-
"""run.py — termtest 终端兼容性测试套件总入口

用法:
    python run.py                交互菜单 (输入编号 / 范围 / 名字片段, 如: 1 3 / 1-5 / query / all)
    python run.py all            顺序跑完所有测试
    python run.py 1 3 5          跑指定编号
    python run.py query          按名字片段匹配运行
    python run.py --list         只列出清单

说明:
    每个测试都是独立进程, 在自己的 T.guard() 里恢复终端状态;
    菜单只是调度器, 不会残留任何终端模式。
"""

import os
import subprocess
import sys

import termlib as T

HERE = os.path.dirname(os.path.abspath(__file__))

_last = {"name": "", "rc": 0}     # 上一个跑完的测试 (菜单里回显, 免得清屏后丢失信息)

# 编号顺序 = 推荐运行顺序 (先自动判定, 再视觉/交互, 最后压力与真实程序)
TESTS = [
    ("t_query.py",    "自动一致性查询",   "DA1/DA2、DSR、DECRQM、DECRQSS、XTGETTCAP、OSC 颜色/剪贴板、窗口尺寸、未知序列健壮性、延迟换行 (自动 PASS/FAIL)"),
    ("t_sgr.py",      "文本样式与颜色",   "16/256/truecolor 三种写法、全部文本属性、下划线变体、重置语义"),
    ("t_screen.py",   "光标与屏幕操作",   "光标移动、擦除/插入/删除、滚动区、原点模式、换行、制表位、字符集、备用屏、窗口缩放"),
    ("t_unicode.py",  "Unicode 宽度",     "CJK/emoji/ZWJ/组合字符宽度、框线无缝、宽字符边界与覆盖"),
    ("t_input.py",    "按键输入",         "逐个按键的字节流与 xterm 规范对照、DECCKM、括号粘贴、焦点事件、未请求的鼠标上报"),
    ("t_mouse.py",    "鼠标协议",         "1000/1002/1003/1005/1006/1015、按钮/滚轮/修饰键、边界坐标"),
    ("t_tui.py",      "交互 TUI (贪吃蛇)", "备用屏 + 高频重绘 + 输入延迟 + 增量/全量渲染对比"),
    ("t_osc.py",      "OSC 扩展",         "标题、OSC 8 链接、OSC 52 剪贴板、OSC 7、通知、OSC 9;4 进度、OSC 133、调色板读写"),
    ("t_sync.py",     "同步输出 2026",    "同步刷新 vs 撕裂对照、整屏混色压力"),
    ("t_images.py",   "图像协议",         "sixel / kitty graphics / iTerm2 内联图, 含滚动与叠加"),
    ("t_keys_ext.py", "扩展键盘协议",     "kitty 键盘协议、CSI u、modifyOtherKeys、win32-input, 三模式字节对照表"),
    ("t_perf.py",     "性能与压力",       "往返延迟、fps、百万行吞吐、超长行、属性风暴、滚动 (自动出数字)"),
    ("t_real.py",     "真实程序电池",     "vim/less/fzf/git + rich/textual/prompt_toolkit 官方 demo 逐个检验"),
]


def probe():
    """快速探测终端是否响应查询序列, 用于给用户一个直观预期。"""
    try:
        r = T.query(T.CSI + "c", 0.25)
    except Exception:
        r = None
    return r


def print_menu():
    # 先清可见屏: 上一个测试的画面还留在屏幕上, 直接在它上面画菜单会出现
    # "短行盖住长行、旧行尾巴从右边露出来"的叠字现象。
    # 用 T.clear() (2J+H) 而不是 clear_all(): 保留滚动缓冲, 方便回滚翻看刚才的测试输出。
    T.clear()
    cols, rows = T.term_size()
    T.divider(ch="━")
    T.wln(T.sgr(1, 37) + " termtest — 终端模拟器兼容性测试套件" + T.sgr(0)
           + T.sgr(90) + "   (%d 个测试)" % len(TESTS) + T.sgr(0))
    info = T.term_info()
    T.hint("窗口 %dx%d  |  %s" % (info["size"][0], info["size"][1],
                                "  ".join(info["objs"]) or "TERM 未设置"))
    if _last["name"]:
        T.hint("上一个测试: %s  退出码 %d  (输出已保留在滚动缓冲, 可回滚查看; 明细见 _reports/)"
               % (_last["name"], _last["rc"]))
    r = probe()
    if r:
        T.hint("查询探测: 终端已响应 %s  (自动判定类测试可以正常出 PASS/FAIL)" % T.vis(r[:40]))
    else:
        T.hint("查询探测: 无响应  (自动判定类测试会记 SKIP; 若你的模拟器应支持查询, 这是个线索)")
    T.divider(ch="━")
    T.wln("")
    for i, (fname, title, desc) in enumerate(TESTS, 1):
        exists = os.path.isfile(os.path.join(HERE, fname))
        mark = "" if exists else T.sgr(31) + " [文件缺失]" + T.sgr(0)
        T.wln("  " + T.sgr(1, 36) + "%2d" % i + T.sgr(0) + "  "
              + T.pad(title, 20) + T.sgr(90) + fname + T.sgr(0) + mark)
        T.wln("      " + T.sgr(90) + desc[:cols - 8] + T.sgr(0))
    T.wln("")
    T.hint("输入: 编号 (如 1 3) / 范围 (1-5) / 名字片段 (query) / all=全部 / q=退出")
    T.wln("")
    T.wln(T.sgr(90) + "  这几项属于终端界面本身的功能, 无法用终端内程序自动测, 建议顺手人工核对:" + T.sgr(0))
    for item in ("文本选择: 拖选高亮、双击选词、复制内容是否与屏幕一致 (t_mouse 退出后测)",
                 "粘贴: Ctrl+V / 右键粘贴、多行粘贴是否完整、括号粘贴是否被 2004 包裹 (见 t_input)",
                 "滚轮与滚动条: 滚轮翻页、滚动条位置、回滚到顶部后新输出是否正常",
                 "窗口: 缩放时内容重排 (见 t_screen)、失焦/获焦边框高亮、Ctrl+滚轮 调整显示大小",
                 "触摸 (移动端): 单指滑动滚动、双指缩放、长按选择"):
        T.wln(T.sgr(90) + "    - " + item + T.sgr(0))


def resolve(tokens):
    """把用户输入解析为测试序号列表。"""
    picked = []
    for tk in tokens:
        tk = tk.strip().lower()
        if not tk:
            continue
        if tk in ("all", "a", "*"):
            picked = list(range(1, len(TESTS) + 1))
            continue
        if tk.isdigit():
            n = int(tk)
            if 1 <= n <= len(TESTS):
                picked.append(n)
            continue
        if "-" in tk:
            a, _, b = tk.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                picked.extend(range(int(a), int(b) + 1))
                continue
        for i, (fname, title, desc) in enumerate(TESTS, 1):
            if tk in fname.lower() or tk in title.lower() or tk in desc.lower():
                picked.append(i)
    seen = []
    for n in picked:
        if n not in seen and 1 <= n <= len(TESTS):
            seen.append(n)
    return seen


def run_tests(numbers, pause=False):
    rc_all = 0
    for n in numbers:
        fname, title = TESTS[n - 1][0], TESTS[n - 1][1]
        path = os.path.join(HERE, fname)
        if not os.path.isfile(path):
            T.note("跳过 %s: 文件不存在 (%s)" % (title, T.safe_path(path)))
            rc_all = 1
            continue
        T.wln("")
        T.divider("运行 %d/%d: %s" % (n, len(TESTS), title))
        T.clear_below()          # 启动前先把下方残留清掉, 避免运行提示叠字
        try:
            rc = subprocess.call([sys.executable, path], cwd=HERE, env=T.safe_env())
        except KeyboardInterrupt:
            rc = 130
        _last["name"], _last["rc"] = fname, rc
        # 测试程序退出时已用 ED 0 擦掉光标以下的旧内容, 因此这里追加的提示行是干净的;
        # 保留判定汇总给用户看, 等回到菜单时 (print_menu) 才清屏。
        T.divider("已结束: %s" % title)
        T.wln(T.sgr(90) + "  %s 退出码 %d   (输出保留在滚动缓冲, 可回滚查看)"
              % (fname, rc) + T.sgr(0))
        if rc != 0:
            rc_all = rc
        if pause:
            try:
                input(T.sgr(90) + "  按 Enter 返回菜单..." + T.sgr(0))
            except (EOFError, KeyboardInterrupt):
                break
    return rc_all


def main():
    args = sys.argv[1:]
    if "--list" in args:
        for i, (fname, title, desc) in enumerate(TESTS, 1):
            print("%2d  %-16s %-20s %s" % (i, fname, title, desc))
        return 0

    if args:
        nums = resolve(args)
        if not nums:
            print("没有匹配的测试: %s" % " ".join(args))
            return 1
        return run_tests(nums)

    # ---- 交互菜单 ----
    T.start("run.py", ["测试套件总入口"],
            note="所有测试都可在你的模拟器里直接运行; q/Esc/Ctrl+C 可随时退出单个测试")
    while True:
        print_menu()
        try:
            line = input(T.sgr(1, 33) + " 选择 > " + T.sgr(0))
        except (EOFError, KeyboardInterrupt):
            T.wln("")
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("q", "quit", "exit", "0"):
            break
        nums = resolve(line.replace(",", " ").split())
        if not nums:
            T.note("没看懂: %r — 输入编号/范围/名字片段, 或 all / q" % line)
            continue
        run_tests(nums, pause=True)
    T.wln(T.sgr(90) + "  已退出 termtest。" + T.sgr(0))
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))