# toolbar.py — ToolbarMixin:工具栏(窗口探测准星 + 操作按钮 + 过滤/刷新勾选 + 时钟)
# self._toolbar_buttons 供 app 在 busy 时统一禁用;
# 勾选变量 only_injectable_var / auto_refresh_var 在此创建,故 _build_toolbar
# 必须早于 _build_menu(菜单里有对应勾选项)与 _build_table(渲染时读过滤).

import tkinter as tk
from tkinter import ttk

from ..i18n import _t
from .spy import ToolTip, WindowFinderTool


class ToolbarMixin:
    """工具栏构建"""

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self._toolbar_buttons = []  # busy 时统一禁用

        # 窗口探测准星(Spy++ 风格):拖到目标窗口松开即定位关联进程
        self.finder = WindowFinderTool(
            bar, on_select=self._on_window_selected,
            on_status=self._spy_status)
        self.finder.pack(side="left", padx=(0, 8))
        self._finder_tip = ToolTip(self.finder, _t("spy_tooltip"))

        for text_key, cmd, width in (
                ("btn_refresh", self.refresh, None),
                ("btn_inject", self.inject_selected, 10),
                ("btn_in_wt", self.launch_in_wt, 12),
                ("btn_unload", self.unload_selected, 10),
                ("btn_unload_all", self.unload_all, 14)):
            btn = ttk.Button(bar, text=_t(text_key), command=cmd, width=width)
            btn.pack(side="left", padx=(6, 0) if self._toolbar_buttons else 0)
            self._toolbar_buttons.append(btn)

        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.only_injectable_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=_t("chk_only_injectable"),
                        variable=self.only_injectable_var,
                        command=self._render_table).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(bar, text=_t("chk_auto_refresh"),
                        variable=self.auto_refresh_var).pack(side="left", padx=(6, 0))

        self.clock_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.clock_var,
                  foreground="#666666").pack(side="right")
