# spy.py — 窗口探测器(Spy++ 风格瞄准镜与目标窗口高亮层)

import ctypes
import tkinter as tk

from ..i18n import _t
from ..winapi import get_window_at_cursor

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080

# 配色(与 Windows 强调色一致,集中定义便于统一调整)
ACCENT = "#0078d7"        # 准星/高亮边框
ACCENT_ACTIVE = "#005a9e"  # 拖动中的高亮态
ACCENT_BG = "#e5f1fb"     # 拖动中的底色
TOOLTIP_BG = "#ffffe1"     # 悬停提示底色


class HighlightOverlay:
    """目标窗口外围矩形高亮边框(由 4 条细长无边框置顶透明穿透窗口组成)"""

    def __init__(self, master, color=ACCENT, thickness=3):
        self.master = master
        self.thickness = thickness
        self.color = color
        self._lines = []
        user32 = ctypes.windll.user32

        for _ in range(4):
            w = tk.Toplevel(master)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            w.config(bg=self.color)
            w.withdraw()
            self._lines.append(w)

        # 挂入事件循环后设置鼠标穿透样式，不阻挡鼠标事件与窗口嗅探
        self.master.update_idletasks()
        for w in self._lines:
            hwnd = w.winfo_id()
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    def show(self, left, top, right, bottom):
        """按目标矩形显示 4 边边框;矩形无效(最小化/零尺寸)时不显示"""
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            self.hide()
            return
        t = self.thickness

        # 上、下、左、右
        self._lines[0].geometry(f"{width}x{t}+{left}+{top}")
        self._lines[1].geometry(f"{width}x{t}+{left}+{top + height - t}")
        self._lines[2].geometry(f"{t}x{height}+{left}+{top}")
        self._lines[3].geometry(f"{t}x{height}+{left + width - t}+{top}")

        for line in self._lines:
            line.deiconify()
            line.lift()

    def hide(self):
        """隐藏全部边框"""
        for line in self._lines:
            line.withdraw()

    def destroy(self):
        for line in self._lines:
            try:
                line.destroy()
            except Exception:
                pass
        self._lines.clear()


class WindowFinderTool(tk.Canvas):
    """Spy++ 风格的瞄准镜探测图标控件"""

    def __init__(self, master, on_select=None, on_status=None, **kwargs):
        super().__init__(master, width=24, height=24, highlightthickness=0,
                         cursor="crosshair", **kwargs)
        self.on_select = on_select
        self.on_status = on_status
        self._dragging = False
        self._overlay = None
        self._last_hwnd = None
        self._last_pid = 0
        self._last_title = ""

        self._draw_bullseye()
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _draw_bullseye(self, active=False):
        """绘制准星图形"""
        self.delete("all")
        color = ACCENT_ACTIVE if active else ACCENT
        if active:
            self.create_rectangle(0, 0, 24, 24, fill=ACCENT_BG, outline="")

        # 外圈与十字线
        self.create_oval(3, 3, 21, 21, outline=color, width=2)
        self.create_oval(7, 7, 17, 17, outline=color, width=1)
        self.create_line(12, 1, 12, 23, fill=color, width=2)
        self.create_line(1, 12, 23, 12, fill=color, width=2)
        self.create_oval(10, 10, 14, 14, fill=color, outline="")

    def _on_press(self, event):
        """按下:抓取鼠标并进入拖拽态(准星只能靠真实拖拽触发,单击不定位)"""
        ctypes.windll.user32.SetCapture(self.winfo_id())
        self._dragging = True
        self._last_hwnd = None
        self._last_pid = 0
        self._last_title = ""
        self._draw_bullseye(active=True)
        if not self._overlay:
            self._overlay = HighlightOverlay(self.winfo_toplevel())
        self._notify(_t("spy_drag_active"))

    def _on_motion(self, event):
        if not self._dragging:
            return
        try:
            hwnd, pid, title, (left, top, right, bottom) = get_window_at_cursor()
            if not hwnd or hwnd == self.winfo_toplevel().winfo_id():
                self._last_hwnd = None
                self._hide_overlay()
                return
            if self._overlay:
                self._overlay.show(left, top, right, bottom)
            self._last_hwnd = hwnd
            self._last_pid = pid
            self._last_title = title
            name = title[:24] + "..." if len(title) > 24 else (title or "-")
            self._notify(_t("spy_dragging").format(name, pid))
        except Exception:  # 嗅探异常不得把控件卡在拖拽态
            self._abort_drag()

    def _on_release(self, event):
        if not self._dragging:
            return
        ctypes.windll.user32.ReleaseCapture()
        self._dragging = False
        self._draw_bullseye(active=False)
        self._hide_overlay()

        hwnd, pid, title = self._last_hwnd, self._last_pid, self._last_title
        self._last_hwnd = None
        if hwnd and pid and self.on_select:
            self.on_select(hwnd, pid, title)
        else:
            self._notify(_t("status_ready"))

    def _hide_overlay(self):
        if self._overlay:
            self._overlay.hide()

    def _abort_drag(self):
        """异常路径:复位拖拽态与鼠标抓取"""
        self._dragging = False
        ctypes.windll.user32.ReleaseCapture()
        self._draw_bullseye(active=False)
        self._hide_overlay()
        self._notify(_t("status_ready"))

    def _notify(self, text):
        if self.on_status:
            self.on_status(text)


class ToolTip:
    """简单悬停提示气泡(悬停约 0.4 秒后在控件下方弹出文本)"""

    def __init__(self, widget, text, delay=400):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, event=None):
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 4
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify="left",
                         background=TOOLTIP_BG, relief="solid",
                         borderwidth=1, font=("Segoe UI", 9))
        label.pack(ipadx=4, ipady=2)

    def _hide(self, event=None):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._tip:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
