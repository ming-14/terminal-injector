# statusbar.py — StatusBarMixin:状态栏(状态 / 选中进程 / 版本)

import tkinter as tk
from tkinter import ttk

from ..i18n import _t


class StatusBarMixin:
    """状态栏构建"""

    def _build_statusbar(self):
        bar = ttk.Frame(self, padding=(6, 2))
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value=_t("status_ready"))
        ttk.Label(bar, textvariable=self.status_var,
                  width=60, anchor="w").pack(side="left")
        self.sel_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.sel_var,
                  width=32, anchor="w").pack(side="left")
        self.ver_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.ver_var,
                  foreground="#666666").pack(side="right")
