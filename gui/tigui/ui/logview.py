# logview.py — LogMixin:日志面板(时间戳 + 着色)与状态栏时钟

import tkinter as tk
from datetime import datetime
from tkinter import ttk

from ..i18n import _t


class LogMixin:
    """日志面板与时钟"""

    def _build_log(self):
        wrap = ttk.LabelFrame(self, text=_t("log_label"), padding=(6, 4))
        self.log_text = tk.Text(wrap, height=7, state="disabled",
                                font=("Consolas", 9), wrap="word",
                                background="#f7f7f7")
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.log_text.tag_configure("info", foreground="#333333")
        self.log_text.tag_configure("cmd", foreground="#5b5bd6")
        self.log_text.tag_configure("ok", foreground="#007a33")
        self.log_text.tag_configure("err", foreground="#c00000")

        self.vpaned.add(wrap, weight=0)  # 下 pane:日志(默认取自然高度,可拖拽调高)

    def log(self, text, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {text}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tick_clock(self):
        self.clock_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._tick_clock)
