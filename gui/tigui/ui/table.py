# table.py — TableMixin:进程表格
#   列定义(同时是『显示列』菜单的数据源)/ 显示列 / 排序 / 渲染 / 选中继承 /
#   右键菜单 / 详情弹窗 / 选中信息 / 行着色
# 依赖 app.InjectorGui 提供:self.vpaned(垂直分栏)、self.targets(最近一次结果)、
# self.by_pid(targets 的 pid 索引)、self.only_injectable_var(『仅显示可注入』勾选)、
# self.app_start(GUI 启动时刻)。
#
# 行着色规则(基于实测的 ttk.Treeview 标签行为):
#   1. 标签是行级的:前景色作用于整行所有列,无法只给『进程名』一格上色
#   2. 多标签优先级 = tag_configure 调用顺序(先配置者胜),
#      与 item 的 tags 列表顺序无关
#   3. 前景与背景是两个独立维度,各自取优先标签,可来自不同标签
#   4. 选中行会被系统高亮色(蓝底白字)覆盖,自定义底色在选中时不可见
# 着色优先级:
#   前景:不可注入灰字 > 进程名色(cmd 黑 / pwsh 蓝 / bash 橙 / python 黄 / 其余蓝)
#   背景:工具自身紫 > 已注入标准绿 > GUI 启动后新起进程浅绿
#   状态不再用前景色表达:可注入/已注入的区分改由文字内容 + 绿底承担。

import os
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from ..i18n import REASON_TEXT, STATUS_TEXT, _t
from ..paths import EXE_NAME

# ---------- 行着色配色 ----------
FG_REJECTED = "#8a8a8a"   # 不可注入:灰字淡化(压过进程名色)
BG_TOOL = "#e5daf7"       # 工具自身(GUI / 注入器)-> 紫,最高优先(压过两种绿底)
BG_INJECTED = "#cdebd4"   # 已注入 -> 标准绿
BG_NEW = "#eaf7ee"        # GUI 启动之后才起的进程 -> 浅绿(次优先)

# 进程名(去 .exe、小写) -> 前景色;白底可读性修正后的色值
NAME_COLOR = {
    "cmd": "#000000",
    "pwsh": "#012456",
    "powershell": "#012456",
    "bash": "#D95F00",
    "python": "#B8860B",
}
DEFAULT_NAME_COLOR = "#1F4FD8"   # 其他进程 -> 蓝

# 排序列 -> 取值函数(用底层数据,非显示文本);status 特殊:已注入 > 可注入 > 不可注入
# 未列出的列(start_time / cmd_line / reason)与字段同名,走 _sort_value 的默认分支
_SORT = {
    "pid": lambda t: t["pid"],
    "name": lambda t: t["name"].lower(),
    "status": lambda t: (not t["injectable"], not t["already_injected"]),
    "arch": lambda t: t["x64"],
    "console": lambda t: t["console"],
    "injected": lambda t: t["already_injected"],
}


def _fg_tag(color: str) -> str:
    """色值 -> 前景标签名;name_tag 与 tag_configure 共用,保证两侧一致"""
    return "nm_" + color.lstrip("#")


def name_tag(name: str) -> str:
    """按进程名取前景标签:大小写不敏感,带不带 .exe 后缀等价"""
    key = (name or "").lower()
    if key.endswith(".exe"):
        key = key[:-4]
    return _fg_tag(NAME_COLOR.get(key, DEFAULT_NAME_COLOR))


def is_tool_process(target) -> bool:
    """本行是否为『工具自身』(标紫,最高优先):GUI 自身或注入器

    - GUI 自身按 PID(os.getpid())判定,源码态(python.exe)与打包态
      (terminal-injector-gui.exe)通吃;不能按名字匹配,否则源码态会把
      系统里所有 python 进程一起标紫(实测同机可有多个 python.exe)。
    - 注入器按进程名匹配 EXE_NAME(大小写不敏感),覆盖 WT 里常驻的
      --mediator 实例;正在跑 --list-targets 的那个子进程由 C++ 侧
      自我排除(ProcessHelper.cpp),本就不出现在列表里。
    """
    if target.get("pid") == os.getpid():
        return True
    return (target.get("name") or "").lower() == EXE_NAME


def configure_tags(tree):
    """给 Treeview 配置行标签 —— 调用顺序即优先级(实测,见模块头):

    紫底(工具自身)最先,压过标准绿与浅绿;灰字先于进程名色;
    标准绿先于浅绿。前景/背景两个维度各自取先配置者。
    _render_table 已用互斥分支保证每行至多一个背景标签,这里的顺序
    是第二道保险:将来一行若同时挂上多个背景标签,仍是紫 > 绿 > 浅绿。
    进程名色之间每行只挂一个,互不冲突,按色值去重后配置。
    """
    tree.tag_configure("tool", background=BG_TOOL)
    tree.tag_configure("rejected", foreground=FG_REJECTED)
    for color in dict.fromkeys([*NAME_COLOR.values(), DEFAULT_NAME_COLOR]):
        tree.tag_configure(_fg_tag(color), foreground=color)
    tree.tag_configure("injected", background=BG_INJECTED)
    tree.tag_configure("new", background=BG_NEW)


class TableMixin:
    """进程列表表格"""

    def _build_table(self):
        # 列定义：cid -> (标题, 宽度, 对齐)；既是「显示列」菜单的数据源,
        # 也是 Treeview 的列 id 清单(取自 dict 插入序,避免两处列清单不同步)
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
        wrap = ttk.Frame(self, padding=(6, 0))
        wrap.pack(fill="both", expand=True)
        # extended:支持多选(窗口探测命中宿主的多个子进程时需一次性全部标记)
        self.tree = ttk.Treeview(wrap, columns=tuple(self.col_labels),
                                 show="headings", selectmode="extended")
        for cid, (text, width, anchor) in self.col_labels.items():
            self.tree.heading(cid, text=text,
                              command=lambda c=cid: self._on_heading_click(c))
            self.tree.column(cid, width=width, anchor=anchor)
        # 排序状态:默认按启动时间降序(晚的在上);可注入行恒在最上
        self.sort_cid = "start_time"
        self.sort_reverse = True
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.vpaned.add(wrap, weight=1)  # 上 pane:表格(占满剩余空间)

        self._build_columns_menu()  # 表格列就绪后构建『显示列』菜单

        # 行标签:顺序即优先级,集中配置在 configure_tags
        configure_tags(self.tree)

        self.tree.bind("<Double-1>", self._show_detail)
        self.tree.bind("<Button-3>", self._show_context_menu)

    def _build_columns_menu(self):
        """构建『设置 -> 显示列』子菜单:每列一个勾选项 + 全选/全不选"""
        # col_visible: 列 cid -> BooleanVar;默认显示核心列,
        # 启动时间/启动命令行/原因以及「已注入」默认隐藏(避免表格过宽,可在菜单中手动开启)
        hidden_by_default = {"injected", "start_time", "cmd_line", "reason"}
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

    def _render_table(self):
        """按过滤与排序把 targets 渲染进表格,并应用行着色标签。
        重建前记录全部选中 PID,重建后按原样恢复(多选不退化为单选)。"""
        prev_sel = list(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        rows = self.targets
        if self.only_injectable_var.get():
            rows = [t for t in rows if t["injectable"]]
        # 排序:先按当前列排,再用一次稳定排序把可注入行提到顶部。
        # 分两步是必须:合成单键再 reverse 会连主键一起反转,
        # 降序时不可注入行会跑到最上,与『可注入恒在上』矛盾。
        if self.sort_cid is None:
            rows = sorted(rows, key=lambda x: x["pid"])
        else:
            rows = sorted(rows, key=self._sort_value(self.sort_cid),
                          reverse=self.sort_reverse)
        rows = sorted(rows, key=lambda x: not x["injectable"])
        for t in rows:
            injected = t["injectable"] and t["already_injected"]
            if injected:
                status = STATUS_TEXT["injected"]
            elif t["injectable"]:
                status = STATUS_TEXT["injectable"]
            else:
                status = STATUS_TEXT["rejected"]
            reason = "" if t["injectable"] else REASON_TEXT.get(
                t["reason"], t["reason"] or "")
            # 多标签叠加:前景(灰字或进程名色)与背景(紫/绿底)相互独立,
            # 优先级由 configure_tags 的配置顺序决定,与本 tags 列表顺序无关。
            tags = [name_tag(t["name"]) if t["injectable"] else "rejected"]
            if is_tool_process(t):
                tags.append("tool")                # 紫底,最高优先
            elif injected:
                tags.append("injected")            # 标准绿,次之
            elif self._is_new_since_launch(t):
                tags.append("new")                 # 浅绿,再次
            self.tree.insert(
                "", "end", iid=str(t["pid"]),
                values=(t["pid"], t["name"], status,
                        "x64" if t["x64"] else "x86",
                        "CUI" if t["console"] else "GUI",
                        _t("yes") if injected else _t("no"),
                        t.get("start_time", ""),
                        t.get("cmd_line", ""), reason),
                tags=tags)
        # 恢复选中:保持全部选中项,只剔除已不在过滤结果中的(进程退出/被过滤)
        still = [p for p in prev_sel if self.tree.exists(p)]
        if still:
            self.tree.selection_set(still)
            self.tree.see(still[0])
        self._update_selection_info()

    def _is_new_since_launch(self, target):
        """本进程是否启动于 GUI 启动之后(等于基准时刻不算)

        start_time 为空(权限不足取不到)或格式异常时按『无法判断』处理,不着色。
        """
        if not self.app_start:
            return False
        raw = target.get("start_time") or ""
        try:
            started = datetime.fromisoformat(raw)   # "YYYY-MM-DD HH:MM:SS"
        except ValueError:
            return False
        return started > self.app_start

    def _sort_value(self, cid):
        """该列排序取值函数:先查 _SORT,字符串列按同名字段取值,缺省空串"""
        return _SORT.get(cid) or (lambda t: t.get(cid, ""))

    def _on_heading_click(self, cid):
        """点击表头:同列切换升降序,异列重置为升序;可注入始终在上"""
        if self.sort_cid == cid:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_cid = cid
            self.sort_reverse = False
        self._render_table()

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
        target = self.by_pid.get(pid)
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
        # 右键命中已选中项时保留整个多选集合(与 Windows 原生列表一致),
        # 只有点在未选中行上才把选择收窄为该行
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        pid = int(iid)
        target = self.by_pid.get(pid)

        menu = tk.Menu(self, tearoff=0)
        # 动态项:已注入显示『卸载』,可注入显示『注入』,二者互斥
        if target and target.get("already_injected"):
            menu.add_command(label=_t("ctx_unload"), command=self.unload_selected)
        elif target and target.get("injectable"):
            menu.add_command(label=_t("ctx_inject"), command=self.inject_selected)
            menu.add_command(label=_t("ctx_in_wt"), command=self.launch_in_wt)
        menu.add_separator()
        menu.add_command(label=_t("ctx_detail"), command=self._show_detail)
        self._ctx_menu = menu   # 保住引用:弹窗期间局部变量不得被回收
        menu.tk_popup(event.x_root, event.y_root)

    def _update_selection_info(self):
        sel = self.tree.selection()
        if not sel:
            self.sel_var.set(_t("no_sel_info"))
        elif len(sel) == 1:
            vals = self.tree.item(sel[0], "values")
            self.sel_var.set(_t("sel_info").format(vals[1], vals[0]))
        else:
            self.sel_var.set(_t("sel_multi_info").format(len(sel)))

    def select_pids(self, pids):
        """把窗口探测命中的所有关联进程一次性全部标记选中

        若某些目标当前被「仅显示可注入」过滤掉,先自动放开过滤并重渲染,
        保证宿主与全部子进程都能在表里被看见/选中。
        """
        pids = [p for p in pids if p is not None]
        if not pids:
            return

        # 目标不在当前视图(被过滤)时自动放开过滤
        visible = set(self.tree.get_children())
        if any(str(p) not in visible for p in pids) \
                and self.only_injectable_var.get():
            self.only_injectable_var.set(False)
            self._render_table()
            visible = set(self.tree.get_children())

        iids = [str(p) for p in pids if str(p) in visible]
        if not iids:
            return

        self.tree.selection_set(iids)
        self.tree.see(iids[0])
        self._update_selection_info()
        return iids
