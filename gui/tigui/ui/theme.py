# theme.py — ttk 外观
# vista 为 Windows 原生主题,缺失时(非 Windows / 精简运行库)静默退回默认。

import tkinter as tk
from tkinter import ttk


def apply_theme(root):
    """应用窗口主题与表行高"""
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass
    style.configure("Treeview", rowheight=22)
