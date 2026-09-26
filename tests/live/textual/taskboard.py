#!/usr/bin/env python3
"""taskboard.py —— 单文件 Textual TUI：任务看板。

覆盖面（基础但尽量全面，全部只用 Textual 自带组件）：
  Header/Footer、TCSS 样式、ListView、DataTable、RichLog、ProgressBar、
  Sparkline、Input 过滤、TabbedContent、ModalScreen、notify 通知、
  reactive 响应式属性、自定义 Message、@work 线程 worker、定时器、鼠标。

运行： python taskboard.py
自测： python taskboard.py --check     # 无头（pilot）跑一遍，不需要真终端
"""
from __future__ import annotations

import argparse
import asyncio
import random
import time
from dataclasses import dataclass

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)

ICONS = {"待命": "○", "运行中": "▶", "完成": "●", "失败": "✗"}


@dataclass
class Task:
    """一个任务。"""

    name: str
    kind: str
    priority: str
    status: str = "待命"
    progress: float = 0.0
    note: str = ""

    @property
    def label(self) -> str:
        return f"{ICONS[self.status]} {self.name}  ·{self.priority}"


SEED = [
    Task("整理访问日志", "log", "高"),
    Task("备份数据库快照", "backup", "高"),
    Task("重建全文索引", "index", "中"),
    Task("跑回归测试", "test", "中"),
    Task("打包并发布镜像", "deploy", "高"),
    Task("生成周报数据", "report", "低"),
    Task("清理 7 天前临时文件", "cleanup", "低"),
    Task("刷新 CDN 缓存", "deploy", "中"),
]


class TaskProgress(Message):
    """worker 线程 → 主线程：任务进度。"""

    def __init__(self, index: int, progress: float, note: str) -> None:
        super().__init__()
        self.index = index
        self.progress = progress
        self.note = note


class TaskFinished(Message):
    """worker 线程 → 主线程：任务结束。"""

    def __init__(self, index: int, ok: bool = True) -> None:
        super().__init__()
        self.index = index
        self.ok = ok


class HelpScreen(ModalScreen[None]):
    """模态帮助屏（演示 ModalScreen + ModalScreen 自己的 BINDINGS）。"""

    BINDINGS = [Binding("escape,?,q", "close_help", "关闭", show=False)]

    TEXT = """[b]快捷键[/b]

  [b]a[/b]       新增一个随机任务
  [b]space[/b]   运行高亮任务（线程 worker，进度实时回传）
  [b]f[/b]       跳到过滤框，输入即过滤，清空即恢复
  [b]r[/b]       重新采样负载
  [b]d[/b]       清空日志
  [b]tab[/b]     切换焦点
  [b]?[/b]       打开 / 关闭本帮助
  [b]q[/b]       退出

鼠标：单击列表项选中，滚轮滚动列表与日志。
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(self.TEXT, id="help-text")
            yield Button("知道了", variant="primary", id="help-ok")

    def action_close_help(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
class TaskBoard(App):
    """主应用。"""

    TITLE = "TaskBoard"
    SUB_TITLE = "单文件 Textual 示例 · 任务看板"

    CSS = """
    Screen { layers: base overlay; }
    #body { height: 1fr; }
    #sidebar { width: 38; padding: 0 1; border-right: solid $panel; }
    #main { padding: 0 1; }
    #filter { margin-bottom: 1; }
    #tasks { height: 1fr; border: round $primary; }
    #tasks:focus { border: round $accent; }
    #stats, #load-title { height: 1; color: $text-muted; }
    #load { height: 3; }
    #metrics { height: 10; }
    #detail { margin-top: 1; color: $text-muted; }
    #log { height: 1fr; }
    #progress { margin-top: 1; }
    #status { height: 1; color: $text-muted; }
    HelpScreen { align: center middle; }
    #help-box {
        width: 64; height: auto; border: round $accent;
        background: $surface; padding: 1 2;
    }
    #help-ok { margin-top: 1; width: 100%; }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("a", "add_task", "新增"),
        Binding("space", "run_task", "运行"),
        Binding("f", "focus_filter", "过滤"),
        Binding("r", "resample", "刷新"),
        Binding("d", "clear_log", "清日志"),
        Binding("?", "help", "帮助"),
        Binding("tab", "focus_next", "切换焦点", show=False),
    ]

    running = reactive(False)
    """是否有任务在跑（演示 reactive + watch）。"""

    def __init__(self) -> None:
        super().__init__()
        self.tasks = [Task(t.name, t.kind, t.priority) for t in SEED]
        self.shown: list[int] = list(range(len(self.tasks)))
        self.load: list[float] = [0.0] * 40
        self.status_text = ""
        self._selected = 0
        self._rng = random.Random(7)

    # ------------------------------------------------------------- 构图
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="过滤任务（按 f 聚焦）…", id="filter")
                items = [
                    ListItem(Label(task.label), id=f"task-{i}")
                    for i, task in enumerate(self.tasks)
                ]
                yield ListView(*items, id="tasks")
                yield Label("", id="stats")
                yield Label("负载采样", id="load-title")
                yield Sparkline(list(self.load), id="load")
            with Vertical(id="main"):
                with TabbedContent(initial="tab-detail"):
                    with TabPane("详情", id="tab-detail"):
                        yield DataTable(id="metrics")
                        yield Static("", id="detail")
                    with TabPane("日志", id="tab-log"):
                        yield RichLog(highlight=True, markup=True, wrap=True, id="log")
                yield ProgressBar(total=100, show_eta=False, id="progress")
                yield Static("", id="status")
        yield Footer()

    # --------------------------------------------------------- 生命周期
    def on_mount(self) -> None:
        table = self.query_one("#metrics", DataTable)
        table.cursor_type = "none"
        # 列的 key 要显式给定：只写标签的话 Textual 会自动生成 column-0 这类 key，
        # 之后的 update_cell 就找不到这一列了。
        self.value_column = table.add_columns(("指标", "metric"), ("值", "value"))[1]
        self.metric_rows = {
            name: table.add_row(name, "-", key=name)
            for name in ("状态", "类型", "优先级", "进度", "备注")
        }
        self.write_log("[b green]TaskBoard[/] 已启动，按 [b]?[/b] 查看快捷键")
        self.update_stats()
        self.update_detail()
        self.set_interval(0.5, self.tick)
        self.set_interval(1.0, self.sample_load)
        self.set_status("就绪")

    def watch_running(self, running: bool) -> None:
        """running 变化时自动改副标题。"""
        self.sub_title = "运行中…" if running else "单文件 Textual 示例 · 任务看板"

    # ----------------------------------------------------------- 小工具
    def write_log(self, text: str) -> None:
        self.query_one("#log", RichLog).write(text)

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#status", Static).update(f" {time.strftime('%H:%M:%S')}  {text}")

    def task_at(self, position) -> Task | None:
        """列表位置 → 任务对象。"""
        if position is not None and 0 <= position < len(self.shown):
            return self.tasks[self.shown[position]]
        return None
    # ------------------------------------------------------- 列表 / 详情
    async def rebuild_list(self, keep: int | None = None) -> None:
        """按过滤词重建列表。clear()/extend() 返回可等待对象，必须 await。"""
        text = self.query_one("#filter", Input).value.strip().lower()
        list_view = self.query_one("#tasks", ListView)
        self.shown = [
            i
            for i, task in enumerate(self.tasks)
            if text in task.name.lower() or text in task.kind.lower()
        ]
        position = self.shown.index(keep) if keep in self.shown else 0
        await list_view.clear()
        if self.shown:
            await list_view.extend(
                ListItem(Label(self.tasks[i].label), id=f"task-{i}")
                for i in self.shown
            )
        list_view.index = position if self.shown else None
        self._selected = self.shown[position] if self.shown else -1
        self.update_stats()
        self.update_detail()

    def update_stats(self) -> None:
        done = sum(1 for task in self.tasks if task.status == "完成")
        self.query_one("#stats", Label).update(
            f"任务 {len(self.tasks)} · 显示 {len(self.shown)} · 完成 {done}"
        )

    def set_metric(self, name: str, value: str) -> None:
        """更新指标表里的某个单元格。"""
        self.query_one("#metrics", DataTable).update_cell(
            self.metric_rows[name], self.value_column, value
        )

    def update_detail(self) -> None:
        """把当前高亮任务的指标写进 DataTable / 进度条 / 详情文本。"""
        detail = self.query_one("#detail", Static)
        position = self.query_one("#tasks", ListView).index
        task = self.task_at(position)
        if task is None:
            for name in ("状态", "类型", "优先级", "进度", "备注"):
                self.set_metric(name, "-")
            self.query_one("#progress", ProgressBar).update(progress=0)
            detail.update("没有匹配的任务。")
            return
        index = self.shown[position]
        self.set_metric("状态", task.status)
        self.set_metric("类型", task.kind)
        self.set_metric("优先级", task.priority)
        self.set_metric("进度", f"{task.progress:.0%}")
        self.set_metric("备注", task.note or "-")
        self.query_one("#progress", ProgressBar).update(progress=task.progress * 100)
        detail.update(
            f"[b]{task.name}[/b]  （第 {index + 1} / {len(self.tasks)} 项）\n"
            "按 [b]space[/b] 运行；运行中进度由 worker 线程实时回传。"
        )

    def refresh_item(self, index: int) -> None:
        """刷新列表里某一项的图标（可能已被过滤掉，所以容错）。"""
        try:
            item = self.query_one(f"#task-{index}", ListItem)
        except Exception:
            return
        item.query_one(Label).update(self.tasks[index].label)

    # --------------------------------------------------------- 事件处理
    @on(ListView.Highlighted)
    def on_highlighted(self, event: ListView.Highlighted) -> None:
        list_view = self.query_one("#tasks", ListView)
        position = list_view.index
        self._selected = self.shown[position] if self.task_at(position) else -1
        self.update_detail()

    @on(ListView.Selected)
    def on_selected(self, event: ListView.Selected) -> None:
        task = self.task_at(event.index)
        if task is not None:
            self.set_status(f"已选中 {task.name}（按 space 运行）")

    @on(Input.Changed, "#filter")
    async def on_filter_changed(self, event: Input.Changed) -> None:
        await self.rebuild_list()

    @on(Input.Submitted, "#filter")
    def on_filter_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#tasks", ListView).focus()

    def on_task_progress(self, message: TaskProgress) -> None:
        task = self.tasks[message.index]
        task.progress = message.progress
        task.note = message.note
        if message.index == self._selected:
            self.update_detail()
        self.set_status(f"{task.name} · {message.note}")

    def on_task_finished(self, message: TaskFinished) -> None:
        task = self.tasks[message.index]
        task.status = "完成" if message.ok else "失败"
        task.progress = 1.0 if message.ok else task.progress
        task.note = "OK" if message.ok else "中断"
        self.running = any(t.status == "运行中" for t in self.tasks)
        self.refresh_item(message.index)
        self.update_stats()
        self.update_detail()
        icon = "[green]✔[/]" if message.ok else "[red]✘[/]"
        self.write_log(f"{icon} {task.name} 结束（{task.kind}）")
        self.notify(f"{task.name} 已完成", title="TaskBoard")

    # ------------------------------------------------------- 定时器
    def tick(self) -> None:
        self.set_status(self.status_text)      # 刷新时间戳

    def sample_load(self) -> None:
        value = self._rng.random() * (0.9 if self.running else 0.45)
        self.load = (self.load + [value])[-40:]
        self.query_one("#load", Sparkline).data = self.load
    # --------------------------------------------------------- 动作/绑定
    def action_run_task(self) -> None:
        position = self.query_one("#tasks", ListView).index
        task = self.task_at(position)
        if task is None:
            self.notify("没有可运行的任务", severity="warning")
            return
        if task.status == "运行中":
            self.notify("该任务正在运行", severity="warning")
            return
        task.status = "运行中"
        task.progress = 0.0
        task.note = "启动中"
        self.running = True
        self.refresh_item(self._selected)
        self.write_log(f"[yellow]▶[/] 开始运行 {task.name}")
        self.set_status(f"{task.name} · 启动中")
        self.update_detail()
        self.run_task_worker(self._selected)

    @work(thread=True, exclusive=True)
    def run_task_worker(self, index: int) -> None:
        """线程 worker：模拟耗时任务，用 post_message 把进度发回主线程。

        post_message 内部走 call_soon_threadsafe，所以从线程发消息是安全的。
        """
        total = 24
        for step in range(1, total + 1):
            time.sleep(0.04)
            self.post_message(TaskProgress(index, step / total, f"步骤 {step}/{total}"))
        self.post_message(TaskFinished(index, True))

    async def action_add_task(self) -> None:
        kinds = ["log", "backup", "index", "test", "deploy", "report", "cleanup"]
        name = f"新任务 #{len(self.tasks) + 1}"
        self.tasks.append(
            Task(name, self._rng.choice(kinds), self._rng.choice(["高", "中", "低"]))
        )
        self.write_log(f"[cyan]+[/] 新增 {name}")
        await self.rebuild_list()
        self.set_status(f"已新增 {name}")

    def action_resample(self) -> None:
        self.sample_load()
        self.update_detail()
        self.set_status("已重新采样")

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()
        self.set_status("日志已清空")

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


# ------------------------------------------------------------- 无头自测
async def self_check() -> None:
    """用 pilot 驱动一遍主要交互，不需要真终端。"""
    app = TaskBoard()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tasks_view = app.query_one("#tasks", ListView)
        assert len(tasks_view.children) == len(SEED)
        print(f"启动：{len(app.tasks)} 个任务，状态栏 = {app.status_text!r}")

        tasks_view.focus()
        await pilot.press("down", "down")
        await pilot.press("enter")
        assert tasks_view.index == 2, tasks_view.index
        print(f"导航：高亮 index={tasks_view.index} → {app.task_at(2).name}")

        await pilot.press("f")
        await pilot.press("l", "o", "g")
        await pilot.pause()
        print("过滤 log →", [app.tasks[i].name for i in app.shown])
        assert app.shown and all("log" in app.tasks[i].kind for i in app.shown)

        for _ in range(3):
            await pilot.press("backspace")
        await pilot.pause()
        assert len(app.shown) == len(app.tasks)

        tasks_view.focus()                 # 焦点从过滤框收回列表，否则空格会被输入框吃掉
        tasks_view.index = 0
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause(0.1)
        print(f"启动 worker：running={app.running}，{app.tasks[0].name} → {app.tasks[0].status}")
        assert app.tasks[0].status == "运行中"
        for _ in range(30):
            await pilot.pause(0.1)
            if not app.running:
                break
        done = app.tasks[0]
        print(f"运行：{done.name} → {done.status} {done.progress:.0%}")
        assert done.status == "完成" and done.progress == 1.0

        await pilot.press("a")
        await pilot.pause()
        assert len(app.tasks) == len(SEED) + 1
        print(f"新增任务：共 {len(app.tasks)} 个")

        await pilot.press("?")
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        print("帮助弹窗：打开 / 关闭正常")

        await pilot.press("r", "d")
        await pilot.pause()
        print(f"状态栏：{app.status_text}")
    print("self-check OK ✔")


def main() -> None:
    parser = argparse.ArgumentParser(description="单文件 Textual 任务看板")
    parser.add_argument("--check", action="store_true", help="无头自测后退出")
    args = parser.parse_args()
    if args.check:
        asyncio.run(self_check())
    else:
        TaskBoard().run()


if __name__ == "__main__":
    main()