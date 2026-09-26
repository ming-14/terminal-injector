# menu.py — MenuMixin:菜单栏(文件 / 操作 / 帮助 / 设置)
# 『设置 -> 显示列』的菜单对象 self.m_columns 由 TableMixin._build_columns_menu
# 建立,故 _build_menu 必须在其之后调用(顺序见 app.InjectorGui._build_ui)。

import tkinter as tk

from ..i18n import _t


class MenuMixin:
    """菜单栏构建"""

    def _build_menu(self):
        menubar = tk.Menu(self)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label=_t("m_exit"), accelerator="Alt+F4",
                           command=self.destroy)
        menubar.add_cascade(label=_t("m_file"), menu=m_file)

        m_op = tk.Menu(menubar, tearoff=0)
        m_op.add_command(label=_t("m_refresh"), accelerator="F5",
                         command=self.refresh)
        m_op.add_command(label=_t("m_rediscover"), command=self.rediscover)
        m_op.add_command(label=_t("m_inject_sel"), accelerator="Ctrl+I",
                         command=self.inject_selected)
        m_op.add_command(label=_t("m_in_wt"), command=self.launch_in_wt)
        m_op.add_command(label=_t("m_unload_sel"), accelerator="Ctrl+U",
                         command=self.unload_selected)
        m_op.add_separator()
        m_op.add_checkbutton(label=_t("m_auto_refresh"),
                             variable=self.auto_refresh_var)
        menubar.add_cascade(label=_t("m_op"), menu=m_op)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label=_t("m_about"), command=self._show_about)
        menubar.add_cascade(label=_t("m_help"), menu=m_help)

        # 设置 -> 显示列：勾选表格显示哪些列(菜单在 _build_table 内已构建)
        menubar.add_cascade(label=_t("m_settings"), menu=self.m_columns)
        self.config(menu=menubar)
