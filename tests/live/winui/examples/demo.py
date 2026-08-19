"""winui TUI 演示程序。

布局：顶部菜单栏 / 左侧功能面板（列表）/ 右侧编辑区（文本框+多行编辑）
     + 底部状态栏。Ctrl+Q 退出，Tab 切换焦点。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

from winui import (
    Application,
    Button,
    CheckBox,
    Container,
    HorizontalLayout,
    Label,
    ListBox,
    Menu,
    MenuBar,
    MenuItem,
    ProgressBar,
    StatusBar,
    TextArea,
    TextBox,
    VerticalLayout,
)

# 默认静默：日志会写入 stderr（控制台），污染 TUI 画面；--debug 时输出
if "--debug" in sys.argv:
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
else:
    logging.getLogger().addHandler(logging.NullHandler())


class Demo:
    """演示应用：把所有控件组装进一个界面。"""

    def __init__(self) -> None:
        self.app = Application(title="winui 演示 - Ctrl+Q 退出")
        self.app.set_title("winui 演示")
        # 根布局：菜单栏 / 主体（自适应）/ 状态栏 垂直排布
        self.app.set_root(Container(layout=VerticalLayout(spacing=0, padding=0)))

        # ---- 菜单栏 ----
        menubar = MenuBar([
            Menu("&文件", [
                MenuItem("&新建", self.action_new),
                MenuItem("&打开", self.action_open),
                MenuItem("", separator=True),
                MenuItem("&退出", self.app.quit),
            ]),
            Menu("&帮助", [
                MenuItem("&关于", self.action_about),
            ]),
        ])
        self.app.add(menubar)

        # ---- 主体：左（列表）+ 右（表单） ----
        body = Container(layout=HorizontalLayout(spacing=1, padding=1))
        self.app.add(body)

        # 左侧：文件列表
        left = Container(layout=VerticalLayout(spacing=1))
        left.add(Label("文件列表", style_name="accent", height=1))
        self.listbox = ListBox(
            items=[f"file_{i:02d}.txt" for i in range(30)],
            on_select=self.on_select_file,
            height=12,
        )
        left.add(self.listbox)
        left.add(Label("↑↓ 选择  Enter 打开", style_name="dim", height=1))
        body.add(left)

        # 右侧：表单（宽度自适应，与左侧均分）
        right = Container(layout=VerticalLayout(spacing=1))
        right.add(Label("文件信息", style_name="accent", height=1))

        right.add(Label("文件名:", height=1))
        self.name_box = TextBox("demo.txt", width=38, on_submit=self.on_rename)
        right.add(self.name_box)

        right.add(Label("内容 (TextArea):", height=1))
        self.editor = TextArea(
            "第一行\n第二行 中文内容\nShift+方向键选择\nCtrl+C/V 复制粘贴",
            height=7,
        )
        right.add(self.editor)

        opts = Container(layout=HorizontalLayout(spacing=2))
        self.check1 = CheckBox("自动保存", checked=True)
        self.check2 = CheckBox("只读模式")
        opts.add(self.check1)
        opts.add(self.check2)
        right.add(opts)

        self.progress = ProgressBar(0.0, height=1)
        right.add(self.progress)

        btn_row = Container(layout=HorizontalLayout(spacing=2), height=1)
        btn_row.add(Button("保存", on_click=self.on_save, width=8))
        btn_row.add(Button("关于", on_click=lambda _btn: self.action_about(),
                           width=8))
        right.add(btn_row)
        body.add(right)

        # 底部状态栏
        self.status = StatusBar(left="就绪", right="Ctrl+Q 退出")
        self.app.add(self.status)

        # 定时器：进度条动画
        self.app.add_timer(100, self._tick_progress)

        # 全局快捷键：Ctrl+Q 退出（与状态栏提示一致）
        self.app.on_key = self._on_key

    # ---- 全局按键 ----
    def _on_key(self, ev) -> bool:
        if ev.name == "ctrl+q":
            self.app.quit()
            return True
        return False

    # ---- 动作 ----
    def action_new(self) -> None:
        self.name_box.set_value("untitled.txt")
        self.editor.set_text("")
        self.status.left = "已新建文件"

    def action_open(self) -> None:
        self.app.show_messagebox("打开", "请从左侧列表选择文件", ["确定"],
                                 self._on_open_result)

    def _on_open_result(self, index: int) -> None:
        if index >= 0:
            self.on_select_file(self.listbox, self.listbox.selected,
                                self.listbox.items[self.listbox.selected])

    def action_about(self) -> None:
        self.app.show_messagebox(
            "关于",
            "winui\n基于 Win32 Console API 的全屏 TUI 框架\n"
            "纯 ctypes 实现，无第三方依赖",
            ["确定"], self._on_about_result)

    def _on_about_result(self, index: int) -> None:
        self.status.left = f"关于对话框关闭 (result={index})"

    # ---- 控件回调 ----
    def on_select_file(self, _box: ListBox, index: int, name: str) -> None:
        self.name_box.set_value(name)
        self.editor.set_text(f"# {name}\n这是第 {index} 个文件的演示内容。\n"
                             "自由编辑：方向键/Home/End/PageUp/PageDown\n"
                             "选中后 Ctrl+C 复制、Ctrl+V 粘贴")
        self.status.left = f"已打开 {name}"

    def on_rename(self, _box: TextBox, value: str) -> None:
        self.status.left = f"文件名已改为 {value}"

    def on_save(self, _btn: Button) -> None:
        self.status.left = (
            f"已保存 {self.name_box.value} ({len(self.editor.text())} 字符)")

    def _tick_progress(self) -> bool:
        self.progress.value = (self.progress.value + 0.02) % 1.0
        return True


def main() -> None:
    app = Demo().app
    import sys
    if "--auto" in sys.argv:
        # e2e 模式：渲染若干帧后自动退出
        app.add_timer(1500, lambda: app.quit() or True)
    app.run()


if __name__ == "__main__":
    main()