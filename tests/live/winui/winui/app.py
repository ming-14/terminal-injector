"""Application 用例层：事件循环、焦点管理、模态栈、定时器与渲染编排。

依赖规则：本层只调用实体层（控件/布局/事件）与适配层（CharBuffer），
具体控制台操作由注入的驱动（框架层）完成。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from winui.adapters.buffer import CharBuffer
from winui.drivers.console import ConsoleDriver
from winui.entities.events import KeyEvent, MouseEvent, ResizeEvent, from_raw
from winui.entities.messagebox import MessageBox
from winui.entities.theme import DEFAULT_THEME, Theme
from winui.entities.widgets import Container, Widget

logger = logging.getLogger("winui.app")


class Application:
    """TUI 应用宿主：持有根容器、焦点与模态，驱动事件循环。"""

    def __init__(self, *, title: str = "winui", theme: Theme = DEFAULT_THEME,
                 driver: ConsoleDriver | None = None) -> None:
        self.title = title
        self.theme = theme
        self.driver = driver or ConsoleDriver()
        self.root = Container()
        self._buffer: CharBuffer | None = None
        self._modals: list[Widget] = []
        self._focus_widget: Widget | None = None
        self._running = False
        self._timers: list[list] = []  # [deadline, interval_ms, cb]
        self._exit_callbacks: list[Callable[[], None]] = []
        self.on_resize: Callable[[int, int], None] | None = None
        self.on_quit: Callable[[], None] | None = None
        self.on_key: Callable[[KeyEvent], bool] | None = None  # 未消费按键的全局钩子

    @property
    def width(self) -> int:
        return self._buffer.w if self._buffer else 0

    @property
    def height(self) -> int:
        return self._buffer.h if self._buffer else 0

    # ---- 装配 ----
    def add(self, widget: Widget) -> Application:
        self.root.add(widget)
        return self

    def set_root(self, container: Container) -> Application:
        """替换根容器。需要对根做自定义布局时使用。"""
        self.root = container
        return self

    def set_title(self, title: str) -> Application:
        self.title = title
        return self

    def on_exit(self, cb: Callable[[], None]) -> Application:
        self._exit_callbacks.append(cb)
        return self

    # ---- 尺寸 ----
    def _resize(self, w: int, h: int) -> None:
        """接受新的窗口尺寸并重建渲染状态。

        ConPTY/终端语义：尺寸由宿主决定（拖窗口/伪终端 resize），
        客户端只响应。仅当窗口超出缓冲区大小时才扩缓冲区（真实 conhost
        场景），避免在 ConPTY 下 SetConsoleScreenBufferSize 引发的
        尺寸同步死循环。
        """
        w, h = max(1, w), max(1, h)
        if (w, h) == (self.width, self.height) and self._buffer is not None:
            return
        bw, bh = self.driver.buffer_size()
        if w > bw or h > bh:
            self.driver.set_window_size(w, h)
        self._buffer = CharBuffer(w, h)
        self.root.arrange(0, 0, w, h)
        for modal in self._modals:
            if hasattr(modal, "center_on"):
                modal.center_on(w, h)
        logger.info("布局尺寸 %sx%s", w, h)
        if self.on_resize:
            self.on_resize(w, h)

    # ---- 焦点管理 ----
    def focus(self, widget: Widget | None) -> None:
        if widget is self._focus_widget:
            return
        old = self._focus_widget
        if old is not None:
            old.on_blur(self)
        # 清除整棵焦点标记，再沿新焦点路径标记（容器节点也需要 focused 状态，
        # 事件分派依赖父链上每一层的 focused 标记来定位活动子级）
        self._clear_focus(self.root)
        node = widget
        while node is not None:
            node.focused = True
            node = node.parent
        self._focus_widget = widget
        if widget is not None:
            widget.on_focus(self)
            logger.debug("焦点 -> %r", widget)

    def _clear_focus(self, container: Container) -> None:
        for child in container.children:
            if isinstance(child, Container):
                child.focused = False
                self._clear_focus(child)
            else:
                child.focused = False

    def focus_next(self, backward: bool = False) -> None:
        """在根容器的可聚焦控件环上移动焦点。"""
        focusables = self.root.focusables()
        if not focusables:
            return
        current = self._focus_widget
        if current is None or current not in focusables:
            self.focus(focusables[0 if not backward else -1])
            return
        idx = focusables.index(current)
        idx = (idx + (-1 if backward else 1)) % len(focusables)
        self.focus(focusables[idx])

    def _focus_initial(self) -> None:
        if self._focus_widget is None:
            focusables = self.root.focusables()
            if focusables:
                self.focus(focusables[0])

    # ---- 模态 ----
    def push_modal(self, widget: Widget) -> None:
        """压入模态：打开期间独占输入，绘制在根容器之上。"""
        if isinstance(widget, MessageBox):
            widget._attach(self)
        if hasattr(widget, "center_on") and self._buffer:
            widget.center_on(self.width, self.height)
        self._modals.append(widget)
        logger.info("模态打开 %r", widget)

    def pop_modal(self) -> None:
        if self._modals:
            closed = self._modals.pop()
            logger.info("模态关闭 %r", closed)

    def show_messagebox(self, title: str, message: str,
                        buttons: list[str] | None = None,
                        on_close: Callable[[int], None] | None = None) -> None:
        """弹出模态消息框。按钮点击回调按钮序号，Esc 回调 -1。"""
        box = MessageBox(title, message, buttons, on_close)
        self.push_modal(box)
        if box._buttons:
            self.focus(box._buttons[0])

    # ---- 定时器 ----
    def add_timer(self, interval_ms: int, cb: Callable[[], bool | None]) -> None:
        """注册周期定时器。cb 返回 False 后停止。"""
        self._timers.append([time.monotonic() * 1000 + interval_ms, interval_ms, cb])

    def _next_timer_ms(self) -> int | None:
        if not self._timers:
            return None
        now = time.monotonic() * 1000
        return max(0, int(min(d for d, *_ in self._timers) - now))

    def _fire_timers(self) -> bool:
        now = time.monotonic() * 1000
        alive: list[list] = []
        fired = False
        for timer in self._timers:
            if timer[0] > now:
                alive.append(timer)
                continue
            fired = True
            keep = timer[2]()
            if keep is not False:
                timer[0] = now + timer[1]
                alive.append(timer)
        self._timers = alive
        return fired

    # ---- 事件分发 ----
    def _dispatch(self, ev) -> None:
        if isinstance(ev, ResizeEvent):
            self._resize(ev.width, ev.height)
            return
        if isinstance(ev, KeyEvent):
            self._dispatch_key(ev)
        elif isinstance(ev, MouseEvent):
            self._dispatch_mouse(ev)

    def _dispatch_key(self, ev: KeyEvent) -> None:
        logger.debug("键盘事件: %s (焦点=%r)", ev, self._focus_widget)
        if self._modals:
            if self._modals[-1].handle_key(ev, self):
                return
        elif self.root.handle_key(ev, self):
            return
        # 应用级钩子（如绑定退出快捷键）
        if self.on_key and self.on_key(ev):
            return
        # 控件未消费的系统级按键
        if ev.name == "tab":
            self.focus_next(ev.shift)
        elif ev.name == "ctrl+c":
            logger.info("Ctrl+C 退出")
            self.quit()

    def _dispatch_mouse(self, ev: MouseEvent) -> None:
        if self._modals:
            self._modals[-1].handle_mouse(ev, self)
        else:
            self.root.handle_mouse(ev, self)

    # ---- 渲染 ----
    def _paint(self) -> None:
        assert self._buffer is not None
        self.root.draw(self._buffer, self.theme)
        for modal in self._modals:
            modal.draw(self._buffer, self.theme)
        for x, y, w, h, matrix in self._buffer.render_diff():
            self.driver.write((x, y, w, h), matrix)
        self._sync_cursor()

    def _sync_cursor(self) -> None:
        """把系统光标定位到当前编辑控件的光标处（隐藏状态下位置仍随焦点走，
        避免终端隐藏失效时光标滞留左上角）。"""
        pos = None
        focused = self._focus_widget
        cursor_pos = getattr(focused, "cursor_pos", None)
        if cursor_pos is not None:
            pos = cursor_pos()
        if pos is not None:
            self.driver.set_cursor(False, pos)
        else:
            self.driver.set_cursor(False)

    # ---- 生命周期 ----
    def quit(self) -> None:
        self._running = False

    def run(self) -> None:
        """进入事件循环。退出（quit/Ctrl+C/异常）后自动恢复控制台。"""
        assert self._buffer is None
        self.driver.init()
        try:
            self.driver.set_title(self.title)
            w, h = self.driver.window_size()
            self._resize(w, h)
            self._focus_initial()
            # 轮询窗口尺寸：真实 conhost 窗口拖大不产生 buffer 事件
            self.add_timer(500, self._poll_window_size)
            self._running = True
            while self._running:
                timeout = self._next_timer_ms()
                raws = self.driver.read_input(timeout)
                for raw in raws:
                    ev = from_raw(raw)
                    if ev is not None:
                        self._dispatch(ev)
                timers_fired = self._fire_timers()
                if raws or timers_fired:
                    self._paint()
        finally:
            self.driver.restore()
            if self.on_quit:
                self.on_quit()
            for cb in self._exit_callbacks:
                cb()

    def _poll_window_size(self) -> bool:
        """定时检查可见窗口尺寸，变化时触发重排（返回 True 持续轮询）。"""
        w, h = self.driver.window_size()
        if (w, h) != (self.width, self.height):
            logger.debug("轮询发现窗口尺寸变化 %sx%s -> %sx%s",
                         self.width, self.height, w, h)
            self._resize(w, h)
        return True