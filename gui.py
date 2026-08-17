# gui.py — terminal-injector 图形化管理界面
# 功能:
#   - 列出进程(调用 --list-targets --json --all 取全量,再按'仅显示可注入'过滤),
#     支持过滤与自动刷新
#   - 一键注入选中进程(--inject,可选管道名),远程卸载(--unload-remote)
#   - 卸载所需的 injected.dll 基址用 ctypes 查询目标进程模块,无需手工输入
#   - 实时日志面板(时间戳+着色)、状态栏(版本/路径/选中进程)
# 布局:terminal_injector.exe 与 injected.dll 须与本文件同目录
# 依赖:仅 Python 标准库(tkinter / ctypes / subprocess / json)

import ctypes
import json
import queue
import shutil
import subprocess
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

# 拒绝原因 -> 界面显示文本
REASON_TEXT = {
    "access_denied": "拒绝:无权限",
    "not_x64": "拒绝:非 x64",
    "not_console": "拒绝:非控制台程序",
}

STATUS_TEXT = {
    "injectable": "可注入",
    "injected": "已注入",
    "rejected": "不可注入",
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
        raise RuntimeError(f"OpenProcess({pid}) 失败: err={ctypes.get_last_error()}")
    try:
        cb = ctypes.c_uint32(0)
        psapi.EnumProcessModulesEx(handle, None, 0, ctypes.byref(cb),
                                   LIST_MODULES_ALL)
        count = cb.value // ctypes.sizeof(ctypes.c_void_p)
        buf = (ctypes.c_void_p * count)()
        if not psapi.EnumProcessModulesEx(handle, buf, cb.value,
                                          ctypes.byref(cb), LIST_MODULES_ALL):
            raise RuntimeError(f"EnumProcessModulesEx({pid}) 失败")
        for mod in buf:
            name = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleFileNameExW(handle, mod, name, 260):
                if Path(name.value).name.lower() == DLL_NAME.lower():
                    # 兼容 c_void_p 对象与 int:部分 Python 版本 ctypes
                    # 数组迭代直接解包为 int,此时 mod.value 会 AttributeError
                    return int(mod)
        raise RuntimeError(f"进程 {pid} 未加载 {DLL_NAME}(可能已卸载)")
    finally:
        kernel32.CloseHandle(handle)


# ============================================================
# "在 WT 中使用"功能:对列表选中的已有进程,在 WT 新 tab 跑 mediator
# ============================================================

class InjectorGui(tk.Tk):
    """terminal-injector 图形化管理界面主窗口"""

    def __init__(self):
        super().__init__()
        self.title("terminal-injector 管理工具")
        self.geometry("960x640")
        self.minsize(800, 500)

        self.exe_dir = Path(__file__).resolve().parent
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
                "缺少文件",
                f"未找到: {', '.join(missing)}\n"
                f"请确保 {EXE_NAME} 与 {DLL_NAME} 与 gui.py 同目录。")

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
        m_file.add_command(label="退出", accelerator="Alt+F4",
                           command=self.destroy)
        menubar.add_cascade(label="文件", menu=m_file)

        m_op = tk.Menu(menubar, tearoff=0)
        m_op.add_command(label="刷新列表", accelerator="F5",
                         command=self.refresh)
        m_op.add_command(label="注入选中进程", accelerator="Ctrl+I",
                         command=self.inject_selected)
        m_op.add_command(label="在 WT 中使用", command=self.launch_in_wt)
        m_op.add_command(label="卸载选中进程", accelerator="Ctrl+U",
                         command=self.unload_selected)
        m_op.add_separator()
        m_op.add_checkbutton(label="自动刷新(3 秒)",
                             variable=self.auto_refresh_var)
        menubar.add_cascade(label="操作", menu=m_op)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self._show_about)
        menubar.add_cascade(label="帮助", menu=m_help)

        # 设置 -> 显示列：勾选表格显示哪些列(菜单在 _build_table 内已构建)
        menubar.add_cascade(label="设置", menu=self.m_columns)
        self.config(menu=menubar)

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(6, 4))
        bar.pack(fill="x")
        self._toolbar_buttons = []  # busy 时统一禁用
        for text, cmd, width in (
                ("刷新", self.refresh, None),
                ("注入选中", self.inject_selected, 10),
                ("在 WT 中使用", self.launch_in_wt, 10),
                ("卸载选中", self.unload_selected, 10),
                ("卸载全部已注入", self.unload_all, 14)):
            btn = ttk.Button(bar, text=text, command=cmd, width=width)
            btn.pack(side="left", padx=(6, 0) if self._toolbar_buttons else 0)
            self._toolbar_buttons.append(btn)

        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.only_injectable_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="仅显示可注入",
                        variable=self.only_injectable_var,
                        command=self._render_table).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(bar, text="自动刷新",
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
        # 列定义：cid -> (中文标题, 宽度, 对齐)；同时作为「显示列」菜单的数据源
        self.col_labels = {
            "pid": ("PID", 70, "center"),
            "name": ("进程名", 170, "w"),
            "status": ("状态", 90, "center"),
            "arch": ("架构", 60, "center"),
            "console": ("类型", 70, "center"),
            "injected": ("已注入", 70, "center"),
            "start_time": ("启动时间", 160, "w"),
            "cmd_line": ("启动命令行", 420, "w"),
            "reason": ("说明", 320, "w")}
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
        wrap = ttk.LabelFrame(self, text="日志", padding=(6, 4))
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
        self.status_var = tk.StringVar(value="就绪")
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
            self.log("已有任务进行中,忽略新请求", "err")
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
                    self.log(f"失败: {payload}", "err")
                    messagebox.showerror("操作失败", payload)
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
        self.status_var.set("任务进行中..." if busy else "就绪")

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
        self.m_columns.add_command(label="全选",
                                    command=lambda: self._set_all_columns(True))
        self.m_columns.add_command(label="全不选",
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
            raise RuntimeError(f"--list-targets 退出码 {res.returncode}: "
                               f"{decode_output(res.stderr).strip()}")
        return json.loads(decode_output(res.stdout))

    def _on_targets(self, targets):
        self.targets = targets
        self._render_table()
        self.log(f"进程列表已刷新: 共 {len(targets)} 项", "ok")

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
                        "是" if injected else "否",
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
        self.log(f"注入: pid={pid}", "cmd")
        cmd = [str(self.exe_path), "--inject", str(pid)]
        if self.dll_path.exists():
            cmd += ["--dll", str(self.dll_path)]
        res = subprocess.run(cmd, capture_output=True, timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(f"注入失败(退出码 {res.returncode}): {out}")
        return f"注入成功: pid={pid}\n{out}"

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
            messagebox.showwarning("无法卸载", f"进程 {pid} 未标记为已注入")
            return
        self._run_task(lambda: self._do_unload(pid), self._on_unload_done)

    def unload_all(self):
        """遍历列表卸载全部已注入进程;逐个失败仅记录不中断"""
        pids = [t["pid"] for t in self.targets
                if t["injectable"] and t["already_injected"]]
        if not pids:
            messagebox.showinfo("卸载全部", "当前没有已注入的进程")
            return
        self._run_task(lambda: self._do_unload_all(pids), self._on_unload_done)

    def _do_unload(self, pid):
        base = find_dll_base(pid)   # 目标进程可能已退出 -> 抛异常
        self.log(f"卸载: pid={pid} dllBase=0x{base:X}", "cmd")
        cmd = [str(self.exe_path), "--unload-remote", str(pid),
               f"0x{base:X}"]
        res = subprocess.run(cmd, capture_output=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(f"卸载失败(退出码 {res.returncode}): {out}")
        return f"卸载成功: pid={pid}\n{out}"

    def _do_unload_all(self, pids):
        results = []
        for pid in pids:
            try:
                results.append(self._do_unload(pid))
            except Exception as exc:  # noqa: BLE001 - 单进程失败不中断
                results.append(f"pid={pid} 卸载失败: {exc}")
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
                "无法在 WT 中使用",
                f"进程 {pid} ({target['name']}) 不可注入:\n{reason}")
            return
        if target["already_injected"]:
            messagebox.showwarning(
                "已注入",
                f"进程 {pid} ({target['name']}) 已被注入。\n"
                f"请先「卸载选中」再在 WT 中使用。")
            return
        self._run_task(
            lambda: self._do_launch_in_wt(pid, target["name"]),
            self._on_wt_launch_done)

    def _find_wt(self):
        """定位 wt.exe(Windows Terminal);缺失时报错"""
        wt = shutil.which("wt.exe")
        if not wt:
            raise RuntimeError("未找到 wt.exe(Windows Terminal)。"
                               "请先安装 Windows Terminal 后重试。")
        return wt

    def _do_launch_in_wt(self, pid, name):
        self.log(f"在 WT 中接管: {name} (pid={pid})", "cmd")
        # 管道名必须为 \\\\.\\pipe\\ 完整形式(CLI 端 CreateNamedPipeW 依赖),
        # 随机后缀防多会话冲突;Python raw string 保证双反斜杠不被吞
        pipe = r"\\.\pipe\ti_wt_" + uuid.uuid4().hex[:8]
        # 参数元素不含内嵌引号:由 subprocess 按空格自动加引号,
        # 避免内嵌引号被转义成 \\" 污染 wt 的解析结果
        mediator = [str(self.exe_path), "--mediator", "--target-pid",
                    str(pid), "--pipe", pipe]
        if self.dll_path.exists():
            mediator += ["--dll", str(self.dll_path)]
        wt_cmd = [self._find_wt(), "new-tab",
                  "--title", f"ti:{name} ({pid})"] + mediator
        subprocess.Popen(wt_cmd, creationflags=subprocess.CREATE_NO_WINDOW)
        return (f"已启动: {name} (pid={pid})\n"
                f"WT 新 tab 将打开中介器并自动注入接管该进程。\n"
                f"关闭该 tab 即结束会话。")

    def _on_wt_launch_done(self, text):
        self.log(text, "ok")
        self.refresh()

    # ---------- 辅助 ----------

    def _selected_pid(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("未选择", "请先在列表中选择一个进程")
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
                 f"进程名: {target['name']}",
                 f"架构: {'x64' if target['x64'] else 'x86'}",
                 f"类型: {'控制台(CUI)' if target['console'] else '图形(GUI)'}",
                 f"可注入: {'是' if target['injectable'] else '否'}",
                 f"已注入: {'是' if target['already_injected'] else '否'}",
                 f"启动时间: {target.get('start_time') or '-'}",
                 f"启动命令行: {target.get('cmd_line') or '-'}",
                 f"原因: {target['reason'] or '-'}"]
        messagebox.showinfo(f"进程 {pid} 详情", "\n".join(lines))

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
            menu.add_command(label="卸载", command=self.unload_selected)
        elif target and target.get("injectable"):
            menu.add_command(label="注入", command=self.inject_selected)
            menu.add_command(label="在 WT 中使用", command=self.launch_in_wt)
        menu.add_separator()
        menu.add_command(label="查看详情", command=self._show_detail)
        menu.tk_popup(event.x_root, event.y_root)

    def _update_selection_info(self):
        sel = self.tree.selection()
        if sel:
            vals = self.tree.item(sel[0], "values")
            self.sel_var.set(f"选中: {vals[1]} (PID {vals[0]})")
        else:
            self.sel_var.set("未选中进程")

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
            "关于",
            f"terminal-injector 管理工具\n\n"
            f"版本: {self._version or '未知'}\n"
            f"exe: {self.exe_path}\n"
            f"dll: {self.dll_path}\n\n"
            f"功能:\n"
            f"  - 列出可注入进程(权限 + x64 + 控制台判定)\n"
            f"  - 一键注入,接管到 Windows Terminal\n"
            f"  - 远程卸载(自动查询 DLL 基址)\n\n"
            f"注入后目标进程的原控制台窗口会被隐藏,\n"
            f"输出经由 DLL 转发给中介/终端。")

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
