# gui.py — terminal-injector 图形化管理界面
# 功能:
#   - 列出进程(调用 --list-targets --json --all 取全量,再按'仅显示可注入'过滤),
#     支持过滤与自动刷新
#   - 一键注入选中进程(--inject,可选管道名),远程卸载(--unload-remote)
#   - 卸载所需的 injected.dll 基址用 ctypes 查询目标进程模块,无需手工输入
#   - 实时日志面板(时间戳+着色)、状态栏(版本/路径/选中进程)
# 布局:terminal_injector.exe 与 injected.dll 须与本文件同目录
# 依赖:仅 Python 标准库(tkinter / ctypes / subprocess / json)
# i18n:界面文本按系统 UI 语言自动切换(中文/英文),不提供手动切换

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

EXE_NAME = "terminal_injector.exe"
DLL_NAME = "injected.dll"

# 进程访问权限(ctypes 查询模块基址用)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03

# ============================================================
# i18n:按系统 UI 主语言自动选择词典(中文/英文)
# ============================================================

def _detect_lang() -> str:
    """检测系统 UI 语言主语言:中文(简/繁) -> 'zh',其余 -> 'en'"""
    try:
        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        # LANGID 低 10 位为主语言;LANG_CHINESE = 0x04
        if (lang_id & 0x3FF) == 0x04:
            return "zh"
    except Exception:  # noqa: BLE001 - 检测失败退回英文
        pass
    return "en"


_LANG = _detect_lang()

_STR = {
    "zh": {
        # 窗口/通用
        "title": "terminal-injector 管理工具",
        "status_ready": "就绪",
        "status_busy": "任务进行中...",
        "task_running": "已有任务进行中,忽略新请求",
        "fail": "失败: {}",
        "op_failed": "操作失败",
        # 启动校验
        "missing_files": "缺少文件",
        "missing_files_msg": "未找到: {}\n请确保 {} 与 {} 与 gui.py 同目录。",
        # 菜单
        "m_file": "文件", "m_exit": "退出",
        "m_op": "操作", "m_refresh": "刷新列表",
        "m_inject_sel": "注入选中进程", "m_in_wt": "在 WT 中使用",
        "m_unload_sel": "卸载选中进程",
        "m_auto_refresh": "自动刷新(3 秒)",
        "m_help": "帮助", "m_about": "关于",
        "m_settings": "设置",
        "m_cols_all": "全选", "m_cols_none": "全不选",
        # 工具栏
        "btn_refresh": "刷新", "btn_inject": "注入选中",
        "btn_in_wt": "在 WT 中使用", "btn_unload": "卸载选中",
        "btn_unload_all": "卸载全部已注入",
        "chk_only_injectable": "仅显示可注入", "chk_auto_refresh": "自动刷新",
        # 列标题
        "col_pid": "PID", "col_name": "进程名", "col_status": "状态",
        "col_arch": "架构", "col_console": "类型", "col_injected": "已注入",
        "col_start_time": "启动时间", "col_cmd_line": "启动命令行",
        "col_reason": "说明",
        # 状态文本
        "st_injectable": "可注入", "st_injected": "已注入",
        "st_rejected": "不可注入",
        "reason_access_denied": "拒绝:无权限",
        "reason_not_x64": "拒绝:非 x64",
        "reason_not_console": "拒绝:非控制台程序",
        "yes": "是", "no": "否",
        # 日志区
        "log_label": "日志",
        # 列表
        "fetch_err": "--list-targets 退出码 {}: {}",
        "targets_refreshed": "进程列表已刷新: 共 {} 项",
        # 注入
        "injecting": "注入: pid={pid}",
        "inject_failed": "注入失败(退出码 {}): {}",
        "inject_ok": "注入成功: pid={pid}\n{out}",
        # 卸载
        "cannot_unload": "无法卸载",
        "not_marked_injected": "进程 {} 未标记为已注入",
        "unload_all_title": "卸载全部",
        "no_injected_procs": "当前没有已注入的进程",
        "unloading": "卸载: pid={pid} dllBase=0x{base:X}",
        "unload_failed": "卸载失败(退出码 {}): {}",
        "unload_ok": "卸载成功: pid={pid}\n{out}",
        "unload_one_failed": "pid={} 卸载失败: {}",
        # WT
        "cannot_in_wt": "无法在 WT 中使用",
        "not_injectable_msg": "进程 {} ({}) 不可注入:\n{}",
        "already_injected_title": "已注入",
        "already_injected_msg": "进程 {} ({}) 已被注入。\n请先「卸载选中」再在 WT 中使用。",
        "wt_not_found": "未找到 wt.exe(Windows Terminal)。请先安装 Windows Terminal 后重试。",
        "wt_retry_portable": "wt 启动失败,回退自带便携版: {}",
        "taking_over": "在 WT 中接管: {} (pid={})",
        "wt_launched": "已启动: {} (pid={})\nWT 新 tab 将打开中介器并自动注入接管该进程。\n关闭该 tab 即结束会话。",
        # 选择
        "no_selection": "未选择",
        "select_first": "请先在列表中选择一个进程",
        "sel_info": "选中: {} (PID {})",
        "no_sel_info": "未选中进程",
        # 详情
        "detail_title": "进程 {} 详情",
        "detail_name": "进程名: {}",
        "detail_arch": "架构: {}",
        "detail_type_cui": "控制台(CUI)",
        "detail_type_gui": "图形(GUI)",
        "detail_injectable": "可注入: {}",
        "detail_injected": "已注入: {}",
        "detail_start": "启动时间: {}",
        "detail_cmd": "启动命令行: {}",
        "detail_reason": "原因: {}",
        # 右键菜单
        "ctx_unload": "卸载", "ctx_inject": "注入",
        "ctx_in_wt": "在 WT 中使用", "ctx_detail": "查看详情",
        # 关于
        "about_title": "关于",
        "about_version": "版本: {}", "about_unknown": "未知",
        "about_text": "terminal-injector 管理工具\n\n"
                      "版本: {version}\n"
                      "exe: {exe}\n"
                      "dll: {dll}\n\n"
                      "功能:\n"
                      "  - 列出可注入进程(权限 + x64 + 控制台判定)\n"
                      "  - 一键注入,接管到 Windows Terminal\n"
                      "  - 远程卸载(自动查询 DLL 基址)\n\n"
                      "注入后目标进程的原控制台窗口会被隐藏,\n"
                      "输出经由 DLL 转发给中介/终端。",
        # find_dll_base 错误
        "openprocess_fail": "OpenProcess({}) 失败: err={}",
        "enummodules_fail": "EnumProcessModulesEx({}) 失败",
        "dll_not_loaded": "进程 {} 未加载 {}(可能已卸载)",
    },
    "en": {
        "title": "terminal-injector Manager",
        "status_ready": "Ready",
        "status_busy": "Working...",
        "task_running": "A task is already running; request ignored",
        "fail": "Failed: {}",
        "op_failed": "Operation Failed",
        "missing_files": "Missing Files",
        "missing_files_msg": "Not found: {}\nMake sure {} and {} are in the same directory as gui.py.",
        "m_file": "File", "m_exit": "Exit",
        "m_op": "Actions", "m_refresh": "Refresh List",
        "m_inject_sel": "Inject Selected", "m_in_wt": "Use in WT",
        "m_unload_sel": "Unload Selected",
        "m_auto_refresh": "Auto-refresh (3s)",
        "m_help": "Help", "m_about": "About",
        "m_settings": "Settings",
        "m_cols_all": "Select All", "m_cols_none": "Select None",
        "btn_refresh": "Refresh", "btn_inject": "Inject",
        "btn_in_wt": "Use in WT", "btn_unload": "Unload",
        "btn_unload_all": "Unload All Injected",
        "chk_only_injectable": "Injectables only", "chk_auto_refresh": "Auto-refresh",
        "col_pid": "PID", "col_name": "Name", "col_status": "Status",
        "col_arch": "Arch", "col_console": "Type", "col_injected": "Injected",
        "col_start_time": "Start Time", "col_cmd_line": "Command Line",
        "col_reason": "Reason",
        "st_injectable": "Injectable", "st_injected": "Injected",
        "st_rejected": "Rejected",
        "reason_access_denied": "Denied: no permission",
        "reason_not_x64": "Denied: not x64",
        "reason_not_console": "Denied: not a console app",
        "yes": "Yes", "no": "No",
        "log_label": "Log",
        "fetch_err": "--list-targets exit code {}: {}",
        "targets_refreshed": "Process list refreshed: {} entries",
        "injecting": "Injecting: pid={pid}",
        "inject_failed": "Injection failed (exit code {}): {}",
        "inject_ok": "Injection succeeded: pid={pid}\n{out}",
        "cannot_unload": "Cannot Unload",
        "not_marked_injected": "Process {} is not marked as injected",
        "unload_all_title": "Unload All",
        "no_injected_procs": "No injected processes found",
        "unloading": "Unloading: pid={pid} dllBase=0x{base:X}",
        "unload_failed": "Unload failed (exit code {}): {}",
        "unload_ok": "Unload succeeded: pid={pid}\n{out}",
        "unload_one_failed": "pid={} unload failed: {}",
        "cannot_in_wt": "Cannot Use in WT",
        "not_injectable_msg": "Process {} ({}) is not injectable:\n{}",
        "already_injected_title": "Already Injected",
        "already_injected_msg": "Process {} ({}) is already injected.\nUnload it first, then use in WT.",
        "wt_not_found": "wt.exe (Windows Terminal) not found. Install Windows Terminal and retry.",
        "wt_retry_portable": "wt failed to start, falling back to bundled portable: {}",
        "taking_over": "Taking over in WT: {} (pid={})",
        "wt_launched": "Launched: {} (pid={})\nA new WT tab will open the mediator and auto-inject the process.\nClosing the tab ends the session.",
        "no_selection": "No Selection",
        "select_first": "Select a process from the list first",
        "sel_info": "Selected: {} (PID {})",
        "no_sel_info": "No process selected",
        "detail_title": "Process {} Details",
        "detail_name": "Name: {}",
        "detail_arch": "Arch: {}",
        "detail_type_cui": "Console (CUI)",
        "detail_type_gui": "Graphical (GUI)",
        "detail_injectable": "Injectable: {}",
        "detail_injected": "Injected: {}",
        "detail_start": "Start time: {}",
        "detail_cmd": "Command line: {}",
        "detail_reason": "Reason: {}",
        "ctx_unload": "Unload", "ctx_inject": "Inject",
        "ctx_in_wt": "Use in WT", "ctx_detail": "Details",
        "about_title": "About",
        "about_version": "Version: {}", "about_unknown": "unknown",
        "about_text": "terminal-injector Manager\n\n"
                      "Version: {version}\n"
                      "exe: {exe}\n"
                      "dll: {dll}\n\n"
                      "Features:\n"
                      "  - List injectable processes (permission + x64 + console check)\n"
                      "  - One-click inject, take over into Windows Terminal\n"
                      "  - Remote unload (DLL base auto-located)\n\n"
                      "After injection the target's original console window is hidden;\n"
                      "output is forwarded to the mediator/terminal via the DLL.",
        "openprocess_fail": "OpenProcess({}) failed: err={}",
        "enummodules_fail": "EnumProcessModulesEx({}) failed",
        "dll_not_loaded": "Process {} has not loaded {} (possibly unloaded)",
    },
}


def _t(key: str) -> str:
    """取当前语言下界面文本(缺键返回键名本身)"""
    return _STR.get(_LANG, _STR["en"]).get(key, key)


REASON_TEXT = {
    "access_denied": _t("reason_access_denied"),
    "not_x64": _t("reason_not_x64"),
    "not_console": _t("reason_not_console"),
}

STATUS_TEXT = {
    "injectable": _t("st_injectable"),
    "injected": _t("st_injected"),
    "rejected": _t("st_rejected"),
}


def decode_output(data: bytes) -> str:
    """subprocess 原始字节解码:优先 UTF-8,失败回退 GBK,再兜底 replace"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def find_dll_base(pid: int) -> int:
    """查询目标进程内 injected.dll 的加载基址(HMODULE 值)

    供 --unload-remote <pid> <dllBase> 使用;进程未注入/已退出则抛异常。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    # 显式 argtypes,防止 64 位指针被截断
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.EnumProcessModulesEx.restype = ctypes.c_int
    psapi.EnumProcessModulesEx.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32]
    psapi.GetModuleFileNameExW.restype = ctypes.c_uint32
    psapi.GetModuleFileNameExW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]

    handle = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, 0, pid)
    if not handle:
        raise RuntimeError(
            _t("openprocess_fail").format(pid, ctypes.get_last_error()))
    try:
        cb = ctypes.c_uint32(0)
        psapi.EnumProcessModulesEx(handle, None, 0, ctypes.byref(cb),
                                   LIST_MODULES_ALL)
        count = cb.value // ctypes.sizeof(ctypes.c_void_p)
        buf = (ctypes.c_void_p * count)()
        if not psapi.EnumProcessModulesEx(handle, buf, cb.value,
                                          ctypes.byref(cb), LIST_MODULES_ALL):
            raise RuntimeError(_t("enummodules_fail").format(pid))
        for mod in buf:
            name = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleFileNameExW(handle, mod, name, 260):
                if Path(name.value).name.lower() == DLL_NAME.lower():
                    # 兼容 c_void_p 对象与 int:部分 Python 版本 ctypes
                    # 数组迭代直接解包为 int,此时 mod.value 会 AttributeError
                    return int(mod)
        raise RuntimeError(_t("dll_not_loaded").format(pid, DLL_NAME))
    finally:
        kernel32.CloseHandle(handle)


# ============================================================
# "在 WT 中使用"功能:对列表选中的已有进程,在 WT 新 tab 跑 mediator
# ============================================================

class InjectorGui(tk.Tk):
    """terminal-injector 图形化管理界面主窗口"""

    def __init__(self):
        super().__init__()
        self.title(_t("title"))
        self.geometry("960x640")
        self.minsize(800, 500)

        # exe/dll 定位:PyInstaller onefile 打包时随包解压到 _MEIPASS,
        # 源码运行时取脚本所在目录
        self.exe_dir = Path(getattr(sys, "_MEIPASS",
                                    Path(__file__).resolve().parent))
        self.exe_path = self.exe_dir / EXE_NAME
        self.dll_path = self.exe_dir / DLL_NAME

        self.queue = queue.Queue()      # 后台线程 -> 主线程消息队列
        self.busy = False               # 是否有任务在跑(防并发操作)
        self.targets = []               # 最近一次 --list-targets 结果
        self._version = ""

        self._check_binaries()
        self._build_ui()
        self.after(100, self._poll_queue)
        self.after(3000, self._auto_refresh_loop)
        self.after(1000, self._tick_clock)
        self._load_version()
        self.refresh()

    def _auto_refresh_loop(self):
        """自动刷新轮询:开启且空闲时每 3 秒刷新一次列表"""
        if self.auto_refresh_var.get() and not self.busy:
            self.refresh()
        self.after(3000, self._auto_refresh_loop)

    # ---------- 初始化 ----------

    def _check_binaries(self):
        """启动校验 exe/dll 是否就位,缺失则提示但允许启动(仅刷新会失败)"""
        missing = [p.name for p in (self.exe_path, self.dll_path)
                   if not p.exists()]
        if missing:
            messagebox.showwarning(
                _t("missing_files"),
                _t("missing_files_msg").format(
                    ", ".join(missing), EXE_NAME, DLL_NAME))

    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=22)

        self._build_toolbar()
        # 垂直 PanedWindow:上=表格,下=日志;分隔条可上下拖拽调高度
        self.vpaned = ttk.PanedWindow(self, orient="vertical")
        self.vpaned.pack(fill="both", expand=True, padx=0, pady=0)
        self._build_table()       # 先建表格与列定义(供『显示列』菜单使用)
        self._build_menu()
        self._build_log()
        self._build_statusbar()

        # 快捷键
        self.bind("<F5>", lambda e: self.refresh())
        self.bind("<Control-i>", lambda e: self.inject_selected())
        self.bind("<Control-u>", lambda e: self.unload_selected())

    def _build_menu(self):
        menubar = tk.Menu(self)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label=_t("m_exit"), accelerator="Alt+F4",
                           command=self.destroy)
        menubar.add_cascade(label=_t("m_file"), menu=m_file)

        m_op = tk.Menu(menubar, tearoff=0)
        m_op.add_command(label=_t("m_refresh"), accelerator="F5",
                         command=self.refresh)
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

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self._toolbar_buttons = []  # busy 时统一禁用
        for text_key, cmd, width in (
                ("btn_refresh", self.refresh, None),
                ("btn_inject", self.inject_selected, 10),
                ("btn_in_wt", self.launch_in_wt, 10),
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

    def _build_table(self):
        cols = ("pid", "name", "status", "arch", "console", "injected",
                "start_time", "cmd_line", "reason")
        wrap = ttk.Frame(self, padding=(6, 0))
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")
        # 列定义：cid -> (标题, 宽度, 对齐)；同时作为「显示列」菜单的数据源
        self.col_labels = {
            "pid": (_t("col_pid"), 70, "center"),
            "name": (_t("col_name"), 170, "w"),
            "status": (_t("col_status"), 90, "center"),
            "arch": (_t("col_arch"), 60, "center"),
            "console": (_t("col_console"), 70, "center"),
            "injected": (_t("col_injected"), 70, "center"),
            "start_time": (_t("col_start_time"), 160, "w"),
            "cmd_line": (_t("col_cmd_line"), 420, "w"),
            "reason": (_t("col_reason"), 320, "w")}
        for cid, (text, width, anchor) in self.col_labels.items():
            self.tree.heading(cid, text=text,
                              command=lambda c=cid: self._on_heading_click(c))
            self.tree.column(cid, width=width, anchor=anchor)
        # 排序状态:默认按启动时间降序(晚的在上);可注入始终在上(主排序键)
        self.sort_cid = "start_time"
        self.sort_reverse = True
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.vpaned.add(wrap, weight=1)  # 上 pane:表格(占满剩余空间)

        self._build_columns_menu()  # 表格列就绪后构建『显示列』菜单

        self.tree.tag_configure("injectable", foreground="#007a33")
        self.tree.tag_configure("injected", foreground="#1f4fd8")
        self.tree.tag_configure("rejected", foreground="#8a8a8a")

        self.tree.bind("<Double-1>", self._show_detail)
        self.tree.bind("<Button-3>", self._show_context_menu)

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

    # ---------- 后台任务框架 ----------

    def _run_task(self, fn, on_ok):
        """提交后台写任务:busy 期间拒绝新任务并禁用按钮;结果经队列回主线程"""
        if self.busy:
            self.log(_t("task_running"), "err")
            return
        self.busy = True
        self._set_busy_ui(True)
        threading.Thread(target=self._task_worker, args=(fn, on_ok),
                         daemon=True).start()

    def _run_refresh(self, fn, on_ok):
        """提交后台刷新任务:只读、不锁 UI、不阻塞写任务。
        刷新期间按钮保持可用,也不因 busy(写任务)被丢弃。"""
        threading.Thread(target=self._refresh_worker, args=(fn, on_ok),
                         daemon=True).start()

    def _task_worker(self, fn, on_ok):
        try:
            self.queue.put(("ok", on_ok, fn()))
        except Exception as exc:  # noqa: BLE001 - 统一上报给 UI
            self.queue.put(("err", on_ok, str(exc)))
        finally:
            self.queue.put(("done", None, None))

    def _refresh_worker(self, fn, on_ok):
        """刷新专用 worker:结果以 refresh_ok/refresh_err 标记,
        不触碰 busy/按钮,主线程照常处理。"""
        try:
            self.queue.put(("refresh_ok", on_ok, fn()))
        except Exception as exc:  # noqa: BLE001 - 统一上报给 UI
            self.queue.put(("refresh_err", on_ok, str(exc)))

    def _poll_queue(self):
        """主线程轮询后台结果队列(100ms 周期)"""
        try:
            while True:
                kind, cb, payload = self.queue.get_nowait()
                if kind in ("ok", "refresh_ok"):
                    cb(payload)
                elif kind in ("err", "refresh_err"):
                    self.log(_t("fail").format(payload), "err")
                    messagebox.showerror(_t("op_failed"), payload)
                else:
                    self.busy = False
                    self._set_busy_ui(False)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _set_busy_ui(self, busy):
        """忙碌时禁用操作按钮,防止并发注入/卸载"""
        state = "disabled" if busy else "normal"
        for child in self._toolbar_buttons:
            child.configure(state=state)
        self.status_var.set(_t("status_busy") if busy else _t("status_ready"))

    # ---------- 列表 ----------

    def _build_columns_menu(self):
        """构建『设置 -> 显示列』子菜单:每列一个勾选项 + 全选/全不选"""
        # col_visible: 列 cid -> BooleanVar;默认显示大部分列,
        # 启动时间/启动命令行默认隐藏(避免表格过宽,可在菜单中手动开启)
        hidden_by_default = {"start_time", "cmd_line", "reason"}
        self.col_visible = {
            cid: tk.BooleanVar(value=cid not in hidden_by_default)
            for cid in self.col_labels}
        self.m_columns = tk.Menu(self, tearoff=0)
        for cid, (label, _w, _a) in self.col_labels.items():
            self.m_columns.add_checkbutton(
                label=label, variable=self.col_visible[cid],
                command=self._apply_columns)
        self.m_columns.add_separator()
        self.m_columns.add_command(label=_t("m_cols_all"),
                                    command=lambda: self._set_all_columns(True))
        self.m_columns.add_command(label=_t("m_cols_none"),
                                    command=lambda: self._set_all_columns(False))
        self._apply_columns()  # 初次应用默认勾选(隐藏启动时间/命令行)

    def _set_all_columns(self, visible):
        for var in self.col_visible.values():
            var.set(visible)
        self._apply_columns()

    def _apply_columns(self):
        """按 col_visible 更新 tree 的 displaycolumns(保持原列顺序)"""
        shown = [cid for cid in self.col_labels if self.col_visible[cid].get()]
        # displaycolumns 为空会让表格无列;至少保留 pid 不至于空白
        if not shown:
            shown = ["pid"]
            self.col_visible["pid"].set(True)
        self.tree["displaycolumns"] = tuple(shown)

    def refresh(self):
        """刷新进程列表(后台执行 --list-targets --json),只读、不锁 UI"""
        self._run_refresh(self._fetch_targets, self._on_targets)

    def _fetch_targets(self):
        # --all 取全量（含不可注入及原因），'仅显示可注入'过滤在 _render_table 做
        res = subprocess.run(
            [str(self.exe_path), "--list-targets", "--json", "--all"],
            capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if res.returncode != 0:
            raise RuntimeError(
                _t("fetch_err").format(
                    res.returncode, decode_output(res.stderr).strip()))
        return json.loads(decode_output(res.stdout))

    def _on_targets(self, targets):
        self.targets = targets
        self._render_table()
        self.log(_t("targets_refreshed").format(len(targets)), "ok")

    def _render_table(self):
        """按过滤条件把 targets 渲染进表格;无过滤时按 PID 升序。
        重建前记录选中 PID,重建后恢复,避免刷新丢失选择。"""
        prev_sel = self.tree.selection()
        prev_pid = prev_sel[0] if prev_sel else None
        self.tree.delete(*self.tree.get_children())
        rows = self.targets
        if self.only_injectable_var.get():
            rows = [t for t in rows if t["injectable"]]
        # 排序:主键 injectable(可注入恒在上),次键为当前点击列(再按值)
        if self.sort_cid is None:
            rows = sorted(rows, key=lambda x: (not x["injectable"], x["pid"]))
        else:
            col_key = self._sort_value(self.sort_cid)
            rows = sorted(
                rows,
                key=lambda x: (not x["injectable"], col_key(x)),
                reverse=self.sort_reverse)
        for t in rows:
            injected = t["injectable"] and t["already_injected"]
            if injected:
                status, tag = STATUS_TEXT["injected"], "injected"
            elif t["injectable"]:
                status, tag = STATUS_TEXT["injectable"], "injectable"
            else:
                status, tag = STATUS_TEXT["rejected"], "rejected"
            reason = "" if t["injectable"] else REASON_TEXT.get(
                t["reason"], t["reason"] or "")
            self.tree.insert(
                "", "end", iid=str(t["pid"]),
                values=(t["pid"], t["name"], status,
                        "x64" if t["x64"] else "x86",
                        "CUI" if t["console"] else "GUI",
                        _t("yes") if injected else _t("no"),
                        t.get("start_time", ""),
                        t.get("cmd_line", ""), reason),
                tags=(tag,))
        # 恢复选中:仅当该 PID 仍在过滤后的结果中
        if prev_pid is not None and self.tree.exists(prev_pid):
            self.tree.selection_set(prev_pid)
            self.tree.see(prev_pid)
        self._update_headings()
        self._update_selection_info()

    def _sort_value(self, cid):
        """返回该列用于排序的取值函数(用底层数据,非显示文本)"""
        if cid == "pid":
            return lambda t: t["pid"]
        if cid == "name":
            return lambda t: t["name"].lower()
        if cid == "status":
            # 已注入 > 可注入 > 不可注入
            return lambda t: (not t["injectable"], not t["already_injected"])
        if cid == "arch":
            return lambda t: t["x64"]
        if cid == "console":
            return lambda t: t["console"]
        if cid == "injected":
            return lambda t: t["already_injected"]
        if cid == "start_time":
            return lambda t: t.get("start_time", "")
        if cid == "cmd_line":
            return lambda t: t.get("cmd_line", "")
        if cid == "reason":
            return lambda t: t.get("reason", "")
        return lambda t: t["pid"]

    def _update_headings(self):
        """刷新所有表头文本(固定标签,排序状态由点击行为体现,无需箭头)"""
        for cid in self.col_labels:
            self.tree.heading(cid, text=self.col_labels[cid][0])

    def _on_heading_click(self, cid):
        """点击表头:同列切换升降序,异列重置为升序;可注入始终在上"""
        if self.sort_cid == cid:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_cid = cid
            self.sort_reverse = False
        self._render_table()

    # ---------- 注入 / 卸载 ----------

    def inject_selected(self):
        pid = self._selected_pid()
        if pid is None:
            return
        self._run_task(lambda: self._do_inject(pid), self._on_inject_done)

    def _do_inject(self, pid):
        self.log(_t("injecting").format(pid=pid), "cmd")
        cmd = [str(self.exe_path), "--inject", str(pid)]
        if self.dll_path.exists():
            cmd += ["--dll", str(self.dll_path)]
        res = subprocess.run(cmd, capture_output=True, timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(
                _t("inject_failed").format(res.returncode, out))
        return _t("inject_ok").format(pid=pid, out=out)

    def _on_inject_done(self, text):
        self.log(text, "ok")
        self.refresh()

    def unload_selected(self):
        pid = self._selected_pid()
        if pid is None:
            return
        # 只允许卸载已注入进程;基址由 ctypes 查询,失败给原因
        target = next((t for t in self.targets if t["pid"] == pid), None)
        if not (target and target["injectable"] and target["already_injected"]):
            messagebox.showwarning(
                _t("cannot_unload"), _t("not_marked_injected").format(pid))
            return
        self._run_task(lambda: self._do_unload(pid), self._on_unload_done)

    def unload_all(self):
        """遍历列表卸载全部已注入进程;逐个失败仅记录不中断"""
        pids = [t["pid"] for t in self.targets
                if t["injectable"] and t["already_injected"]]
        if not pids:
            messagebox.showinfo(
                _t("unload_all_title"), _t("no_injected_procs"))
            return
        self._run_task(lambda: self._do_unload_all(pids), self._on_unload_done)

    def _do_unload(self, pid):
        base = find_dll_base(pid)   # 目标进程可能已退出 -> 抛异常
        self.log(_t("unloading").format(pid=pid, base=base), "cmd")
        cmd = [str(self.exe_path), "--unload-remote", str(pid),
               f"0x{base:X}"]
        res = subprocess.run(cmd, capture_output=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(
                _t("unload_failed").format(res.returncode, out))
        return _t("unload_ok").format(pid=pid, out=out)

    def _do_unload_all(self, pids):
        results = []
        for pid in pids:
            try:
                results.append(self._do_unload(pid))
            except Exception as exc:  # noqa: BLE001 - 单进程失败不中断
                results.append(_t("unload_one_failed").format(pid, exc))
        return "\n".join(results)

    def _on_unload_done(self, text):
        self.log(text, "ok")
        self.refresh()

    # ---------- 在 WT 中使用 ----------

    def launch_in_wt(self):
        """对列表选中的已有进程,在 Windows Terminal 新 tab 接管其会话"""
        pid = self._selected_pid()
        if pid is None:
            return
        target = next((t for t in self.targets if t["pid"] == pid), None)
        if not target:
            return
        if not target["injectable"]:
            reason = REASON_TEXT.get(target["reason"], target["reason"] or "-")
            messagebox.showwarning(
                _t("cannot_in_wt"),
                _t("not_injectable_msg").format(pid, target["name"], reason))
            return
        if target["already_injected"]:
            messagebox.showwarning(
                _t("already_injected_title"),
                _t("already_injected_msg").format(pid, target["name"]))
            return
        self._run_task(
            lambda: self._do_launch_in_wt(pid, target["name"]),
            self._on_wt_launch_done)

    def _find_wt(self):
        """定位 wt.exe(Windows Terminal);顺序:
        1. PATH 查找  2. App Execution Alias(%LOCALAPPDATA%\\Microsoft\\WindowsApps)
        3. 自带便携版(t\\wt.exe,源码运行时=仓库 t\\,打包时随 _MEIPASS 解压)
        全部缺失才报错。"""
        alias = (Path(os.environ.get("LOCALAPPDATA", ""))
                 / "Microsoft" / "WindowsApps" / "wt.exe")
        for cand in (shutil.which("wt.exe"),
                     str(alias) if alias.exists() else None,
                     str(self.exe_dir / "t" / "wt.exe")):
            if cand and Path(cand).exists():
                return cand
        raise RuntimeError(_t("wt_not_found"))

    def _do_launch_in_wt(self, pid, name):
        self.log(_t("taking_over").format(name, pid), "cmd")
        # 管道名必须为 \\\\.\\pipe\\ 完整形式(CLI 端 CreateNamedPipeW 依赖),
        # 随机后缀防多会话冲突;Python raw string 保证双反斜杠不被吞
        pipe = r"\\.\pipe\ti_wt_" + uuid.uuid4().hex[:8]
        # 参数元素不含内嵌引号:由 subprocess 按空格自动加引号,
        # 避免内嵌引号被转义成 \\" 污染 wt 的解析结果
        mediator = [str(self.exe_path), "--mediator", "--target-pid",
                    str(pid), "--pipe", pipe]
        if self.dll_path.exists():
            mediator += ["--dll", str(self.dll_path)]
        self._spawn_wt(self._find_wt(), mediator, name, pid)
        return _t("wt_launched").format(name, pid)

    def _spawn_wt(self, wt_path, mediator, name, pid):
        """启动 wt 新 tab;若所选 wt 启动失败(如商店别名未注册)且存在
        自带便携版 t\\wt.exe,则回退自带版本重试。"""
        try:
            subprocess.Popen(
                [wt_path, "new-tab", "--title", f"ti:{name} ({pid})"]
                + mediator,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError:
            portable = self.exe_dir / "t" / "wt.exe"
            if portable.exists() and str(portable.resolve()) != str(
                    Path(wt_path).resolve()):
                self.log(_t("wt_retry_portable").format(portable), "err")
                subprocess.Popen(
                    [str(portable), "new-tab", "--title",
                     f"ti:{name} ({pid})"] + mediator,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                raise

    def _on_wt_launch_done(self, text):
        self.log(text, "ok")
        self.refresh()

    # ---------- 辅助 ----------

    def _selected_pid(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(_t("no_selection"), _t("select_first"))
            return None
        return int(sel[0])

    def _show_detail(self, event=None):
        # 双击表头/空白区时(非数据行)忽略,避免误弹详情
        if event is not None:
            if not self.tree.identify_row(event.y):
                return
        pid = self._selected_pid()
        if pid is None:
            return
        target = next((t for t in self.targets if t["pid"] == pid), None)
        if target is None:
            return
        lines = [f"PID: {target['pid']}",
                 _t("detail_name").format(target['name']),
                 _t("detail_arch").format(
                     'x64' if target['x64'] else 'x86'),
                 _t("detail_type_" + ("cui" if target['console'] else "gui")),
                 _t("detail_injectable").format(
                     _t("yes") if target['injectable'] else _t("no")),
                 _t("detail_injected").format(
                     _t("yes") if target['already_injected'] else _t("no")),
                 _t("detail_start").format(target.get('start_time') or '-'),
                 _t("detail_cmd").format(target.get('cmd_line') or '-'),
                 _t("detail_reason").format(target['reason'] or '-')]
        messagebox.showinfo(
            _t("detail_title").format(pid), "\n".join(lines))

    def _show_context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        pid = int(iid)
        target = next((t for t in self.targets if t["pid"] == pid), None)

        menu = tk.Menu(self, tearoff=0)
        # 动态项:已注入显示『卸载』,可注入显示『注入』,二者互斥
        if target and target.get("already_injected"):
            menu.add_command(label=_t("ctx_unload"), command=self.unload_selected)
        elif target and target.get("injectable"):
            menu.add_command(label=_t("ctx_inject"), command=self.inject_selected)
            menu.add_command(label=_t("ctx_in_wt"), command=self.launch_in_wt)
        menu.add_separator()
        menu.add_command(label=_t("ctx_detail"), command=self._show_detail)
        menu.tk_popup(event.x_root, event.y_root)

    def _update_selection_info(self):
        sel = self.tree.selection()
        if sel:
            vals = self.tree.item(sel[0], "values")
            self.sel_var.set(_t("sel_info").format(vals[1], vals[0]))
        else:
            self.sel_var.set(_t("no_sel_info"))

    def _load_version(self):
        """启动时后台获取 --version,填充状态栏"""

        def fetch():
            res = subprocess.run([str(self.exe_path), "--version"],
                                 capture_output=True, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            return decode_output(res.stdout).strip()

        def done(ver):
            self._version = ver
            self.ver_var.set(ver)

        if self.exe_path.exists():
            threading.Thread(target=self._task_worker,
                             args=(fetch, done), daemon=True).start()

    def _show_about(self):
        messagebox.showinfo(
            _t("about_title"),
            _t("about_text").format(
                version=self._version or _t("about_unknown"),
                exe=self.exe_path, dll=self.dll_path))

    # ---------- 日志 ----------

    def log(self, text, tag="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] {text}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _tick_clock(self):
        self.clock_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.after(1000, self._tick_clock)


if __name__ == "__main__":
    app = InjectorGui()
    app.mainloop()