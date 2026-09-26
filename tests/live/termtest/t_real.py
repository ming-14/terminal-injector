# -*- coding: utf-8 -*-
"""t_real.py — 真实程序电池: 用真实存在的命令行程序检验终端兼容性

三部分:
  A. 环境清单 —— 用 shutil.which 探测常用 TUI/命令行工具是否安装 (附安装建议),
     并检查 Python 生态 (rich / textual / prompt_toolkit) 是否可导入。
  B. 对照运行 —— 逐个确认后用 subprocess 在同一终端里启动真实程序, 退出后回到本程序;
     记录 启动耗时 / 退出码 / 备注 并写入报告。内置项无需用户额外安装:
       * python -m rich            rich 官方 demo (表格/面板/进度条/真彩)
       * python -m textual[ demo]  textual 官方 demo (终端特性压力最大的真实程序)
       * 内嵌 mini prompt_toolkit 全屏应用 (输入框 + 状态栏 + 按键绑定 + 中文)
       * git log --graph 彩色输出 → less -R (测分页器与颜色直通; 无 less 则直接打印)
       * vim 打开示例文件 (含中文/长行/框线/制表符)
  C. 未安装程序的安装建议清单 (只打印, 不执行任何安装命令)

非交互模式 (TERMTEST_NONINTERACTIVE=1): 只做 A / C 两部分, 不启动任何交互程序。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

import termlib as T

# 名称, 用途 (检验点), 安装建议
PROGRAMS = [
    ("vim", "文本编辑器 (全屏 TUI: 光标定位/滚动/中文)",
     "winget install vim.vim    或  scoop install vim"),
    ("nvim", "Neovim (真彩/光标形状/鼠标)",
     "winget install Neovim.Neovim    或  scoop install neovim"),
    ("less", "分页器 (备用屏/滚动/搜索/颜色直通)",
     "winget install jftuga.less    或  scoop install less"),
    ("more", "分页器 (Windows 自带 more.com)",
     "系统自带, 未找到说明 PATH 异常"),
    ("htop", "进程监视 (需类 Unix 环境)",
     "仅 Linux/WSL: wsl --install 后 sudo apt install htop"),
    ("btop", "资源监视 (真彩图表/框线)",
     "winget install aristocratos.btop4win    或  scoop install btop"),
    ("fzf", "模糊查找 (交互选择器/alt-screen)",
     "winget install junegunn.fzf    或  scoop install fzf"),
    ("tig", "Git 的 TUI 前端",
     "scoop install tig"),
    ("ncdu", "磁盘占用 TUI",
     "scoop install ncdu"),
    ("tmux", "终端复用器 (需类 Unix 环境)",
     "仅 Linux/WSL; Windows 可用 WSL 或 psmux"),
    ("git", "版本控制 (彩色输出 + 分页器经典组合)",
     "winget install Git.Git    或  scoop install git"),
    ("python", "解释器",
     "winget install Python.Python.3.12"),
    ("pip", "包管理器 (随 Python 提供)",
     "python -m ensurepip --upgrade"),
    ("winget", "Windows 包管理器 (随 应用安装程序 提供)",
     "Microsoft Store 安装“应用安装程序”"),
    ("ssh", "OpenSSH 客户端 (连远端跑 TUI)",
     "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"),
    ("curl", "HTTP 客户端 (进度条/ANSI 输出)",
     "winget install curl.curl"),
]

WINDOWS_ONLY = [
    ("wt.exe", "Windows Terminal (被测终端本体/多标签)", "winget install Microsoft.WindowsTerminal"),
    ("cmd", "命令提示符 (ConHost 宿主)", "系统自带"),
    ("powershell", "PowerShell", "系统自带"),
]

PYPKGS = [
    ("rich", "富文本终端渲染 (表格/面板/进度条/真彩)", "pip install rich"),
    ("textual", "TUI 框架 (其 demo 是终端特性压力最大的真实程序)", "pip install textual"),
    ("prompt_toolkit", "交互式命令行库 (全屏应用/按键绑定/中文输入)", "pip install prompt_toolkit"),
]

# 内嵌的 mini prompt_toolkit 全屏应用 (作为被测对象交给独立进程运行)
PT_SCRIPT = r'''
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    from prompt_toolkit import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.widgets import Frame, TextArea
except Exception as exc:
    print("prompt_toolkit 不可用:", exc)
    raise SystemExit(3)

status = ["就绪 — 输入文字后按 Enter 提交; Ctrl+C 退出; 中文测试: 你好，世界！"]

kb = KeyBindings()


@kb.add("c-c")
@kb.add("c-q")
def _quit(event):
    event.app.exit()


def on_accept(buf):
    status[0] = "已提交: " + buf.text
    return False


edit = TextArea(multiline=False, accept_handler=on_accept, prompt="> ")

body = HSplit([
    Frame(edit, title="输入框 (支持中文/全角)"),
    Window(FormattedTextControl(lambda: status[0]), height=1),
    Window(FormattedTextControl(lambda: "状态栏: Ctrl+C 退出 | 框线: ┌─┬─┐ │ └─┴─┘"), height=1),
])

app = Application(layout=Layout(body), key_bindings=kb, full_screen=True, mouse_support=True)
app.run()
'''

VIM_SAMPLE = """\
termtest 示例文件 —— vim 里可试: j/k 上下, gg/G 首尾, /中文 搜索, :q 退出

一、中文与全角标点：中文测试，全角标点！？（），。；：“引号”《书名号》
二、框线字符：┌────────┬────────┐  │ 单元格 │  ├───┼───┤  └────────┴────────┘
三、制表符（以下两行行首是 TAB，检验 tabstop 对齐）：
\t第一列\t第二列\t第三列
\t数据 A\t数据 B\t数据 C
四、长行（超出窗口宽度，检验软换行与横向滚动）：
{longline}
五、组合字符与 emoji（宽度可能异常，属 t_unicode.py 职责，这里只看是否花屏）：
    🙂🙃🙄 é ě ñ ①②③ （这些就是上面几行的实际字符）
六、反色/粗体（vim 语法高亮）： # 注释 def f(x): return "字符串" 12345 True None
"""


# ------------------------------------------------------------------ 工具
def _cut(s, n):
    """按字符数截断 (纯文本, 不含转义序列)。"""
    return s if len(s) <= n else s[:max(0, n - 1)] + "…"


def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _inv_line(name, path, desc, tip):
    if path:
        # 只给文件名: shutil.which 的绝对路径含用户目录结构, 上屏/落盘都是身份指纹
        T.wln("  " + T.sgr(32) + "[已安装]" + T.sgr(0) + " " + T.pad(name, 12)
              + T.sgr(90) + _cut(os.path.basename(path), 40) + T.sgr(0) + "  " + _cut(desc, 30))
    else:
        T.wln("  " + T.sgr(90) + "[未安装]" + T.sgr(0) + " " + T.pad(name, 12)
              + T.sgr(33) + "建议: " + _cut(tip, 58) + T.sgr(0))


def _python_of():
    """返回可运行的 python 解释器命令 (用于启动被测 Python 程序)。"""
    return sys.executable or "python"


# ------------------------------------------------------------------ A 环境清单
def part_a(rep):
    T.section("A. 环境清单 — 命令行程序 (shutil.which 探测)")
    installed, missing = [], []
    for name, desc, tip in PROGRAMS:
        p = _which(name, name + ".exe")
        if p:
            installed.append(name)
        else:
            missing.append((name, desc, tip))
        _inv_line(name, p, desc, tip)

    T.wln()
    T.hint("Windows 专有命令:")
    win_installed = 0
    for name, desc, tip in WINDOWS_ONLY:
        p = _which(name)
        if p:
            win_installed += 1
        _inv_line(name, p, desc, tip)

    T.wln()
    T.section("A2. Python 生态可导入性")
    pkg_ok = {}
    for name, desc, tip in PYPKGS:
        try:
            ok = importlib.util.find_spec(name) is not None
        except Exception:
            ok = False
        pkg_ok[name] = ok
        if ok:
            T.wln("  " + T.sgr(32) + "[可导入]" + T.sgr(0) + " " + T.pad(name, 16) + _cut(desc, 34))
        else:
            T.wln("  " + T.sgr(90) + "[缺失  ]" + T.sgr(0) + " " + T.pad(name, 16)
                  + T.sgr(33) + "安装: " + tip + T.sgr(0))

    rep += ["# A. 环境清单",
            "已安装 (%d): %s" % (len(installed), ", ".join(installed)),
            "未安装 (%d): %s" % (len(missing), ", ".join(n for n, _, _ in missing)),
            "Windows 专有: %d/%d" % (win_installed, len(WINDOWS_ONLY)),
            "Python 包: " + ", ".join("%s=%s" % (k, "可导入" if v else "缺失")
                                    for k, v in pkg_ok.items()), ""]

    T.section("A 判定")
    total = len(PROGRAMS) + len(WINDOWS_ONLY)
    got = len(installed) + win_installed
    T.row("命令行程序覆盖", T.PASS if got >= 10 else T.WARN,
          "%d/%d 已安装, 未装的可按下方建议补齐" % (got, total))
    for name, desc, tip in PYPKGS:
        if pkg_ok[name]:
            T.row("Python 包 " + name, T.PASS, "可导入")
        else:
            T.row("Python 包 " + name, T.SKIP, "未安装: " + tip)
    return installed, missing, pkg_ok


# ------------------------------------------------------------------ B 对照运行
def _cmd_text(cmd):
    """命令行回显: 可执行文件与绝对路径参数都截到安全形式, 不回显本机目录结构。"""
    parts = [os.path.basename(cmd[0])]
    for a in cmd[1:]:
        parts.append(T.safe_path(a) if os.path.isabs(a) else a)
    return " ".join(parts)


def _run_external(title, cmd, note="", cwd=None, detail_ok=""):
    """确认后用 subprocess 在同一终端启动真实程序, 记录启动耗时/退出码。"""
    if T.NONINTERACTIVE:
        T.row(title, T.SKIP, "非交互模式: 不启动交互程序")
        return None
    T.section("B. 运行: " + title)
    if note:
        T.hint(note)
    T.wln("  命令: " + _cmd_text(cmd))
    with T.Raw():
        T.wait_key("按任意键启动 (q / Esc / Ctrl+C 退出本测试)")
    T.write(T.sgr(0))
    t0 = time.perf_counter()
    rc = -1
    launch_err = None
    try:
        rc = subprocess.call(cmd, cwd=cwd, env=T.safe_env())
    except OSError as exc:
        launch_err = exc
        T.note("启动失败: %s" % exc)
    dt = time.perf_counter() - t0
    T.restore_all()
    T.clear()
    if launch_err is not None:
        # 起不来的原因在程序/环境这一侧, 别写成"不被终端支持"去冤枉终端
        T.row(title, T.WARN, "启动失败 (%s), 未测到终端行为" % launch_err)
    elif rc == 0:
        T.row(title, T.PASS, "退出码 0, 用时 %.2f 秒%s" % (dt, (" — " + detail_ok) if detail_ok else ""))
    else:
        T.row(title, T.WARN, "退出码 %d, 用时 %.2f 秒 (程序自身报错或不被终端支持)" % (rc, dt))
    return {"title": title, "cmd": " ".join(cmd), "rc": rc, "sec": dt,
            "note": note + ((" | " + detail_ok) if detail_ok else "")}


def _prep_git_repo():
    """总是临时新建一个演示 git 仓库。

    不再用"当前目录所在的 git 仓库": 那会把开发者真实仓库的提交历史 (作者/邮箱/
    项目名) 打到屏幕上, 逻辑本身即隐私缺陷。
    """
    git = _which("git")
    if not git:
        return None, None, "git 未安装"
    d = tempfile.mkdtemp(prefix="termtest_git_")
    T.tempdir_register(d)
    base = [git, "-c", "user.email=termtest@example.com", "-c", "user.name=termtest",
            "commit", "--allow-empty", "-q", "-m"]
    T.hint("正在构造临时演示仓库 (30 条中文提交, 需数秒)…")
    try:
        subprocess.call([git, "init", "-q"], cwd=d,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        env=T.safe_env())
        for i in range(30):
            subprocess.call(base + ["提交 %02d: 中文提交信息 with English words" % i], cwd=d,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=T.safe_env())
    except OSError as exc:
        T.note("构造临时仓库失败: %s" % exc)
    return git, d, "临时仓库 (含 30 条中文提交)"


def part_b(rep, pkg_ok):
    T.section("B. 对照运行 — 真实程序 (每次启动前需按一次键)")
    if T.NONINTERACTIVE:
        T.hint("非交互模式: 跳过 B 部分全部交互程序")
    recs = []

    # 1. rich 官方 demo
    if pkg_ok.get("rich"):
        r = _run_external("rich 官方 demo", [_python_of(), "-m", "rich"],
                          note="rich 会输出表格/面板/真彩渐变条; 结束后按 q 或回车退出 (或 Ctrl+C)",
                          detail_ok="检验真彩渐变、框线、超链接")
        if r:
            recs.append(r)
    else:
        T.row("rich 官方 demo", T.SKIP, "rich 未安装: pip install rich")

    # 2. textual 官方 demo
    if pkg_ok.get("textual"):
        r = _run_external("textual 官方 demo", [_python_of(), "-m", "textual"],
                          note="textual demo 对终端要求最高 (鼠标/真彩/光标形状/重绘); Ctrl+Q 退出",
                          detail_ok="终端特性压力最大的真实程序")
        if r and r["rc"] != 0:
            tx = _which("textual")
            if tx:
                r2 = _run_external("textual demo (CLI 回退)", [tx, "demo"],
                                   note="python -m textual 失败后的回退: textual demo 控制台脚本",
                                   detail_ok="回退路径")
                if r2:
                    recs.append(r2)
    else:
        T.row("textual 官方 demo", T.SKIP, "textual 未安装: pip install textual")

    # 3. 内嵌 mini prompt_toolkit 全屏应用
    if pkg_ok.get("prompt_toolkit"):
        r = _run_external("prompt_toolkit 全屏 mini 应用",
                          [_python_of(), "-c", PT_SCRIPT],
                          note="输入框 + 状态栏 + 按键绑定 + 中文; Ctrl+C 退出",
                          detail_ok="检验 alt-screen/光标定位/中文输入回显")
        if r:
            recs.append(r)
    else:
        T.row("prompt_toolkit mini 应用", T.SKIP, "prompt_toolkit 未安装: pip install prompt_toolkit")

    # 4. git 彩色输出 → less -R
    git = _which("git")
    less = _which("less", "less.exe")
    if not git:
        T.row("git 彩色输出 → less", T.SKIP, "git 未安装: winget install Git.Git")
    elif T.NONINTERACTIVE:
        # 非交互模式不构造演示仓库 (需要几十次 git 进程, 纯属浪费时间)
        T.row("git 彩色输出 → less", T.SKIP, "非交互模式: 不启动交互程序")
    else:
        git, repo_dir, how = _prep_git_repo()
        gitcmd = [git, "-c", "color.ui=always", "log", "--oneline", "-n", "200", "--graph"]
        if less:
            T.section("B. 运行: git 彩色输出 → less -R (分页器 + 颜色直通)")
            T.hint(how + " — less 里可试: 空格翻页, /提交 搜索, q 退出")
            T.wln("  命令: %s | %s -R" % (_cmd_text(gitcmd), os.path.basename(less)))
            with T.Raw():
                T.wait_key("按任意键启动 (q / Esc / Ctrl+C 退出本测试)")
            T.write(T.sgr(0))
            t0 = time.perf_counter()
            rc = -1
            try:
                p1 = subprocess.Popen(gitcmd, cwd=repo_dir, stdout=subprocess.PIPE,
                                      env=T.safe_env())
                try:
                    rc = subprocess.call([less, "-R"], stdin=p1.stdout, env=T.safe_env())
                finally:
                    try:
                        p1.stdout.close()
                    except Exception:
                        pass
                    p1.wait()
            except OSError as exc:
                T.note("启动失败: %s" % exc)
            dt = time.perf_counter() - t0
            T.restore_all()
            T.clear()
            T.row("git log → less -R 分页", T.PASS if rc == 0 else T.WARN,
                  "退出码 %d, 用时 %.2f 秒 — 检验分页器与 ANSI 颜色直通" % (rc, dt))
            recs.append({"title": "git log → less -R", "cmd": " ".join(gitcmd) + " | less -R",
                         "rc": rc, "sec": dt, "note": how})
        else:
            r = _run_external("git log 彩色输出 (无 less, 直接打印)",
                              list(gitcmd), note=how + " — 颜色应正确直通到终端",
                              cwd=repo_dir, detail_ok="无 less, 未走分页器")
            if r:
                recs.append(r)

    # 5. vim 示例文件
    vim = _which("vim", "vim.exe", "nvim", "nvim.exe")
    if vim:
        d = tempfile.mkdtemp(prefix="termtest_vim_")
        T.tempdir_register(d)
        path = os.path.join(d, "sample.txt")
        try:
            longline = ("这是一行很长的中文：终端应当在窗口右边界软换行，而不是把内容截断掉；"
                        "The quick brown fox jumps over the lazy dog. ") * 4
            with open(path, "w", encoding="utf-8") as f:
                f.write(VIM_SAMPLE.format(longline=longline))
        except OSError as exc:
            T.note("写示例文件失败: %s" % exc)
        r = _run_external("vim 打开示例文件", [vim, path],
                          note="示例文件: <系统临时目录>/sample.txt — 可试 j/k 移动, /中文 搜索, :q 退出",
                          detail_ok="检验光标定位/软换行/TAB/宽字符渲染")
        if r:
            recs.append(r)
    else:
        T.row("vim 打开示例文件", T.SKIP, "vim 未安装: winget install vim.vim")
    return recs


# ------------------------------------------------------------------ C 建议清单
def part_c(missing, pkg_ok, rep):
    T.section("C. 安装建议清单 (只打印, 不执行)")
    not_installed = [(n, t) for n, _d, t in missing]
    if not_installed:
        T.hint("以下程序未安装, 补齐后本测试能覆盖更多真实场景:")
        for name, tip in not_installed:
            T.wln("  " + T.sgr(36) + T.pad(name, 12) + T.sgr(0) + T.sgr(90) + tip + T.sgr(0))
    else:
        T.hint("常用命令行程序已全部安装。")
    miss_pkg = [n for n, ok in pkg_ok.items() if not ok]
    if miss_pkg:
        T.wln()
        T.hint("Python 生态缺失项 (B 部分依赖它们):")
        for name, _desc, tip in PYPKGS:
            if not pkg_ok.get(name):
                T.wln("  " + T.sgr(36) + T.pad(name, 16) + T.sgr(0) + T.sgr(90) + tip + T.sgr(0))
    T.wln()
    T.hint("说明: 本程序不会自动安装任何软件, 请按需自行复制上面的命令执行。")
    rep += ["# C. 待安装建议",
            "\n".join("  %s  ->  %s" % (n, t) for n, t in not_installed) or "  (无)",
            "  Python 包缺失: " + (", ".join(miss_pkg) if miss_pkg else "无"), ""]


# ------------------------------------------------------------------ main
def main():
    T.start("t_real.py",
            ["A 环境清单 (which + pip 包)", "B rich/textual/prompt_toolkit 实测",
             "B git 彩色输出 → less 分页", "B vim 打开中文示例文件", "C 安装建议清单"],
            note="用真实程序检验终端: 分页器、全屏 TUI、真彩、中文与框线、软换行。")
    rep = ["t_real.py 真实程序电池报告 (窗口 %dx%d)" % T.term_size(), ""]
    installed, missing, pkg_ok = part_a(rep)
    recs = part_b(rep, pkg_ok)
    part_c(missing, pkg_ok, rep)
    if recs:
        rep.append("# B. 对照运行记录")
        for r in recs:
            rep.append("  %-36s 退出码=%-4s 用时=%.2fs  %s" % (r["title"], r["rc"], r["sec"], r["note"]))
    path = T.save_report("t_real", "\n".join(rep) + "\n")
    if path:
        T.hint("环境/运行记录: " + T.safe_path(path))
    T.finish("t_real 结束")
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))
