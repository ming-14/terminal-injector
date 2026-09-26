# app.py — InjectorGui:主窗口装配与动作编排
#
# 分层:
#   ui/*      界面构件(Mixin):只依赖 i18n 与 self 约定
#   backend   terminal_injector.exe 调用(无 tkinter 依赖,可单独测试)
#   tasks     后台线程 / 队列 / busy
#   i18n      界面文本(中英自动切换)
#   paths     exe/dll 定位(terminal_injector.exe 与 injected.dll 须与 gui.py 同目录)
#
# 构建顺序不可调整:
#   _build_toolbar(建勾选变量与按钮)-> vpaned -> _build_table(建列定义并生成
#   『显示列』菜单)-> _build_menu(把该菜单挂到『设置』)-> _build_log
#   -> _build_statusbar

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from .backend import InjectorBackend
from .discover import DiscoveryResult, current_root, describe_limits
from .i18n import REASON_TEXT, _t
from .paths import DLL_NAME, EXE_NAME
from .tasks import TaskRunner
from .ui.logview import LogMixin
from .ui.menu import MenuMixin
from .ui.statusbar import StatusBarMixin
from .ui.table import TableMixin
from .ui.theme import apply_theme
from .ui.toolbar import ToolbarMixin
from .winapi import find_associated_processes


class InjectorGui(MenuMixin, ToolbarMixin, TableMixin, LogMixin,
                  StatusBarMixin, tk.Tk):
    """terminal-injector 图形化管理界面主窗口"""

    def __init__(self):
        super().__init__()
        self.title(_t("title"))
        self.geometry("960x640")
        self.minsize(800, 500)

        # exe/dll 定位统一走 paths.resolve_base_dir(包内 __file__ 不可用)
        self.backend = InjectorBackend()
        self.tasks = TaskRunner(self, log=self.log,
                                on_error=self._on_task_error,
                                on_busy=self._set_busy_ui)
        self.backend.set_log_sink(self.tasks.post_log)   # 后台日志只入队
        self.targets = []               # 最近一次 list_targets 结果
        self.by_pid = {}                # targets 的 pid 索引(见 _on_targets)
        self._version = ""
        # 行着色基准时刻(见 table.py):早于它的进程不算『新起』
        self.app_start = datetime.now().replace(microsecond=0)
        self._discovering = False   # 后台探测进行中(见 rediscover)

        self._build_ui()
        self.after(100, self.tasks.poll)
        self.after(3000, self._auto_refresh_loop)
        self.after(1000, self._tick_clock)
        self._load_version()
        if self.backend.missing_binaries():
            self.rediscover()   # 缺失即自动后台探测一次(不阻塞启动)
        self.refresh()

    def _auto_refresh_loop(self):
        """自动刷新轮询:开启且空闲时每 3 秒刷新一次列表

        exe 缺失时静默跳过:refresh() 会记一条 error,这里每轮都调就成了
        每 3 秒刷一条同类日志;且缺 exe 时列表本就刷不出来。
        """
        if self.auto_refresh_var.get() and not self.tasks.busy \
                and self.backend.exe_path.exists():
            self.refresh()
        self.after(3000, self._auto_refresh_loop)

    # ---------- exe/dll 自动探测 ----------

    def rediscover(self):
        """后台递归搜索缺失的 exe/dll,找到即回填 backend 路径

        走 run_readonly:独立守护线程执行,不占 busy、不锁 UI,可与刷新
        并发。_discovering 挡住重复触发(只读任务没有内置互斥)。
        """
        if self._discovering:
            self.log(_t("disc_busy"), "err")
            return
        missing = self.backend.missing_binaries()
        if not missing:
            self.log(_t("disc_all_present"), "ok")
            return
        self._discovering = True
        self.log(_t("disc_start").format(
            ", ".join(missing), current_root(),
            describe_limits()), "info")
        self.tasks.run_readonly(self._discovery_job, self._on_discovered)

    def _discovery_job(self):
        """探测载荷(工作线程):异常折叠成 error 字段

        这样 run_readonly 永远走 ok 分支 -> _on_discovered 一定执行,
        _discovering 不会因异常卡死在 True。
        """
        try:
            return self.backend.discover()
        except Exception as exc:  # noqa: BLE001 - 兜底,统一在 UI 层报
            return DiscoveryResult({}, current_root(), error=str(exc))

    def _on_discovered(self, res):
        """探测完成:逐个报出新找到的文件,仍有缺失则报原因(上探过就把
        两个搜索根都写进报告),否则刷新"""
        self._discovering = False
        if res.error:
            self.log(_t("disc_error").format(res.error), "err")
            return
        for name, path in res.found.items():
            self.log(_t("disc_found").format(name, path), "ok")

        left = self.backend.missing_binaries()
        if left:
            limits = describe_limits()
            missing = ", ".join(left)
            if res.parent_round:
                # 上探过:报两个搜索根,扫描数是两轮之和
                key = "disc_timeout_2" if res.timed_out else "disc_not_found_2"
                self.log(_t(key).format(res.root, res.parent_round.root,
                                        limits, res.scanned, missing), "err")
            elif res.timed_out:
                self.log(_t("disc_timeout").format(
                    limits, res.scanned, missing), "err")
            else:
                self.log(_t("disc_not_found").format(
                    limits, res.scanned, missing), "err")
            # 本目录 + 上探父目录两轮(或第一轮就扫完)仍缺失才弹窗;
            # res.error 分支在上面已 return,不在此列
            messagebox.showwarning(
                _t("missing_files"),
                _t("missing_files_msg").format(
                    missing, EXE_NAME, DLL_NAME))
            return
        # 已齐:补上的可能有 exe,版本号与列表都要重取
        if self.backend.exe_path.exists():
            self._load_version()
        if res.found:
            self.refresh()

    def _build_ui(self):
        apply_theme(self)

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

    # ---------- 后台任务框架 ----------

    def _on_task_error(self, payload):
        """后台任务失败:写日志并弹窗提示"""
        self.log(_t("fail").format(payload), "err")
        messagebox.showerror(_t("op_failed"), payload)

    def _on_op_done(self, text):
        """注入 / 卸载 / WT 接管三类写任务的统一收尾:记日志并刷新列表"""
        self.log(text, "ok")
        self.refresh()

    def _set_busy_ui(self, busy):
        """忙碌时禁用操作按钮,防止并发注入/卸载"""
        state = "disabled" if busy else "normal"
        for child in self._toolbar_buttons:
            child.configure(state=state)
        self.status_var.set(_t("status_busy") if busy else _t("status_ready"))

    # ---------- 窗口探测 (Spy++) ----------

    def _spy_status(self, text):
        """拖动准星时实时显示探测到的目标窗口信息"""
        self.status_var.set(text)

    def _on_window_selected(self, hwnd, window_pid, title):
        """准星松开:找出窗口关联的全部进程(含全部子进程)并一次性全部标记选中"""
        matched = find_associated_processes(window_pid, self.targets)
        if not matched:
            self.log(_t("spy_not_found").format(window_pid), "err")
            self.status_var.set(_t("status_ready"))
            return
        if not self.select_pids(matched):
            return
        if len(matched) == 1:
            target = self.by_pid.get(matched[0])
            name = target["name"] if target else str(matched[0])
            self.log(_t("spy_matched_one").format(name, matched[0]), "ok")
        else:
            self.log(_t("spy_matched_multi").format(len(matched), window_pid), "ok")

    # ---------- 列表 ----------

    def refresh(self):
        """刷新进程列表(后台执行 --list-targets --json),只读、不锁 UI

        exe 缺失时不派发子进程:subprocess 必抛 FileNotFoundError,一路走
        refresh_err 回到 _on_task_error 弹 showerror —— 启动 + 3 秒自动
        刷新会连环弹窗。改为只记一条 error(用户决策:不弹窗,日志即可);
        路径补齐后由 _on_discovered 回填并刷新。
        """
        if not self.backend.exe_path.exists():
            self.log(_t("refresh_no_exe").format(EXE_NAME), "err")
            return
        self.tasks.run_readonly(self.backend.list_targets,
                                self._on_targets)

    def _on_targets(self, targets):
        self.targets = targets
        self.by_pid = {t["pid"]: t for t in targets}
        self._render_table()
        self.log(_t("targets_refreshed").format(len(targets)), "ok")

    # ---------- 注入 / 卸载 ----------

    def inject_selected(self):
        pid = self._selected_pid()
        if pid is None:
            return
        self.tasks.run(lambda: self.backend.inject(pid), self._on_op_done)

    def unload_selected(self):
        pid = self._selected_pid()
        if pid is None:
            return
        # 只允许卸载已注入进程;基址由 ctypes 查询,失败给原因
        target = self.by_pid.get(pid)
        if not (target and target["injectable"] and target["already_injected"]):
            messagebox.showwarning(
                _t("cannot_unload"), _t("not_marked_injected").format(pid))
            return
        self.tasks.run(lambda: self.backend.unload(pid), self._on_op_done)

    def unload_all(self):
        """遍历列表卸载全部已注入进程;逐个失败仅记录不中断"""
        pids = [t["pid"] for t in self.targets
                if t["injectable"] and t["already_injected"]]
        if not pids:
            messagebox.showinfo(
                _t("unload_all_title"), _t("no_injected_procs"))
            return
        self.tasks.run(lambda: self.backend.unload_many(pids), self._on_op_done)

    # ---------- 在 WT 中使用 ----------

    def launch_in_wt(self):
        """对列表选中的已有进程,在 Windows Terminal 新 tab 接管其会话"""
        pid = self._selected_pid()
        if pid is None:
            return
        target = self.by_pid.get(pid)
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
        self.tasks.run(
            lambda: self.backend.launch_in_wt(pid, target["name"]),
            self._on_op_done)

    # ---------- 辅助 ----------

    def _load_version(self):
        """启动时后台获取 --version,填充状态栏"""

        def done(ver):
            self._version = ver
            self.ver_var.set(ver)

        if self.backend.exe_path.exists():
            self.tasks.run_oneshot(self.backend.version, done)

    def _show_about(self):
        messagebox.showinfo(
            _t("about_title"),
            _t("about_text").format(
                version=self._version or _t("about_unknown"),
                exe=self.backend.exe_path, dll=self.backend.dll_path))


def main():
    """入口:创建主窗口并进入事件循环(由 gui.py 调用)"""
    app = InjectorGui()
    app.mainloop()
