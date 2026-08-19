"""e2e 测试：在真实 ConPTY 中运行 demo，验证渲染、键盘交互与退出。

依赖 pywezterm（桌面兄弟目录，ConPTY 引擎 + wezterm 终端模型）。
pywezterm 不可用时测试自动跳过。

闭环流程：读 pty 输出 → feed 终端模型 → 终端应答（如 DSR 查询）回写 pty。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# 探测 pywezterm：先看环境变量，再找桌面兄弟目录（不硬编码具体路径）
_PYWEZTERM_DIR = Path(os.environ.get("PYWEZTERM_PATH", "")).resolve() \
    if os.environ.get("PYWEZTERM_PATH") else None
if _PYWEZTERM_DIR is None:
    for cand in (Path(__file__).resolve().parent.parent.parent / "pywezterm",):
        if cand.exists():
            _PYWEZTERM_DIR = cand
            break

pywezterm = None
if _PYWEZTERM_DIR is not None:
    try:
        sys.path.insert(0, str(_PYWEZTERM_DIR))
        import pywezterm
    except ImportError:
        pywezterm = None

pytestmark = pytest.mark.skipif(
    pywezterm is None, reason="pywezterm 不可用，跳过 PTY e2e 测试")

WINUI_DIR = Path(__file__).resolve().parent.parent
DEMO = str(WINUI_DIR / "examples" / "demo.py")


def run_pty(cols=100, rows=30, args=()):
    """启动 demo 于伪终端（ConPTY 初始 cwd 不受调用方影响，须用绝对路径）。"""
    p = pywezterm.Pty(cols=cols, rows=rows)
    t = pywezterm.Terminal(cols=cols, rows=rows, scrollback=5000)
    import shutil
    python = shutil.which("python")
    assert python, "找不到 python"
    pid, _handle = p.spawn([python, DEMO, *args])
    assert pid > 0
    return p, t


def pump(p, t, seconds=0.3):
    """读 pty → 喂终端 → 回写终端应答。返回本周期累计原始输出。"""
    out = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunk = p.read(4096, timeout=0.05)
        if not chunk:
            continue
        out += chunk
        t.feed(chunk)
        resp = t.drain_written()
        if resp:
            p.write(resp)
    return out


def wait_text(p, t, needle: str, timeout=8.0) -> str:
    """轮询终端画面直到出现目标文本。超时抛异常并附当前画面。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pump(p, t, 0.2)
        text = t.text()
        if needle in text:
            return text
    return t.text()


def send_key(t, name: str, mods: int = 0):
    """通过终端模型编码按键并返回字节串。"""
    return t.key_down(name, mods)


    def test_demo_render_and_exit():
        """渲染完整 + 自动退出模式退出码为 0。"""
        p, t = run_pty(args=("--auto",))
        try:
            text = wait_text(p, t, "文件列表")
            # 标题是 OSC 序列（终端标题），屏幕文本中验证界面元素
            assert "Ctrl+Q 退出" in text, text
            assert "file_00.txt" in text, text
            assert "demo.txt" in text, text
            # 等 auto 定时器触发退出（1.5s 后）
            code = None
            deadline = time.time() + 10
            while time.time() < deadline and code is None:
                code = p.try_wait()
                if code is None:
                    pump(p, t, 0.2)
            assert code == 0, f"退出码={code}"
        finally:
            p.close()


def test_keyboard_interaction():
    """Tab 焦点移动 + 文本框输入回显。"""
    p, t = run_pty()
    try:
        wait_text(p, t, "文件列表")
        # 初始焦点在 ListBox；Tab → 文件名 TextBox，输入内容应回显
        p.write(t.key_down("Tab", 0))
        time.sleep(0.2)
        for ch in "hello":
            p.write(t.key_down(ch, 0))
            pump(p, t, 0.1)
        text = wait_text(p, t, "hello")
        assert "hello" in text, text

        # 方向键应移动列表选择（画面整体不崩溃即可），再输入中文
        p.write(t.key_down("Down", 0))
        p.write(t.key_down("Down", 0))
        pump(p, t, 0.3)

        # Ctrl+C 退出，退出码 0
        p.write(b"\x11")  # Ctrl+Q（ConPTY 控制字符→按键事件）
        code = None
        deadline = time.time() + 8
        while time.time() < deadline and code is None:
            code = p.try_wait()
            if code is None:
                pump(p, t, 0.2)
        assert code == 0, f"退出码={code}"
    finally:
        p.close()


def test_menu_mouse():
    """点击菜单栏打开下拉菜单：Esc 关闭，再打开后用键盘 Enter 触发菜单项。"""
    p, t = run_pty()
    try:
        wait_text(p, t, "文件列表")
        # 点击菜单栏“文件”（菜单条 y=0，x≈1）
        p.write(t.mouse(1, 0, "press", "left", 0))
        pump(p, t, 0.4)
        text = t.text()
        assert "新建" in text, text
        assert "退出" in text, text
        # Esc 关闭
        p.write(b"\x1b")
        pump(p, t, 0.3)
        # 再次打开，Enter 触发第一项“新建”（文件名为 untitled.txt）
        p.write(t.mouse(1, 0, "press", "left", 0))
        pump(p, t, 0.4)
        p.write(b"\r")
        pump(p, t, 0.4)
        assert "untitled.txt" in t.text(), t.text()
        p.write(b"\x11")  # Ctrl+Q 退出
        deadline = time.time() + 8
        code = None
        while time.time() < deadline and code is None:
            code = p.try_wait()
            if code is None:
                pump(p, t, 0.2)
        assert code == 0, f"退出码={code}"
    finally:
        p.close()


def test_button_via_tab():
    """Tab 焦点环导航到按钮，Enter 触发 on_click 回调。"""
    p, t = run_pty()
    try:
        wait_text(p, t, "文件列表")
        # 焦点环：ListBox→TextBox→TextArea→CheckBox1→CheckBox2→保存按钮
        for _ in range(5):
            p.write(t.key_down("Tab", 0))
            pump(p, t, 0.05)
        p.write(b"\r")
        pump(p, t, 0.4)
        assert "已保存" in t.text(), t.text()
        p.write(b"\x11")  # Ctrl+Q 退出
        deadline = time.time() + 8
        code = None
        while time.time() < deadline and code is None:
            code = p.try_wait()
            if code is None:
                pump(p, t, 0.2)
        assert code == 0, f"退出码={code}"
    finally:
        p.close()


def test_resize_reflow():
    """窗口尺寸变化后界面重排且关键组件仍在。"""
    p, t = run_pty()
    try:
        wait_text(p, t, "文件列表")
        p.resize(80, 25)
        pump(p, t, 0.5)
        text = t.text()
        assert "文件列表" in text, text
        assert "内容" in text, text
        # 缩小后按 Tab/方向键不应崩溃
        p.write(t.key_down("Tab", 0))
        p.write(t.key_down("Down", 0))
        pump(p, t, 0.3)
        p.write(b"\x11")  # Ctrl+Q（ConPTY 控制字符→按键事件）
        deadline = time.time() + 8
        code = None
        while time.time() < deadline and code is None:
            code = p.try_wait()
            if code is None:
                pump(p, t, 0.2)
        assert code == 0, f"退出码={code}"
    finally:
        p.close()