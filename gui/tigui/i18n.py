# i18n.py — 界面文本(按系统 UI 主语言自动切换:中文/英文)
# 约定:zh/en 两份词典的键必须完全一致;缺键时 _t() 返回键名本身,便于发现遗漏。
# _t 为包内共享命名(各模块 from .i18n import _t),不对外暴露。

import ctypes


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
        # 自动探测 (discover.py)
        "m_rediscover": "重新探测 exe/dll",
        "disc_busy": "后台搜索已在进行,请稍候",
        "disc_all_present": "exe 与 dll 均已就位,无需探测",
        "disc_start": "后台搜索: {} — 根目录 {} ({})",
        "disc_limits": "深度≤{} / 超时 {}",
        "disc_seconds": "{} 秒",
        "disc_unlimited": "不限时",
        "disc_found": "找到 {}: {}",
        "disc_timeout": "搜索超时({}),已扫描 {} 项,仍缺少: {}",
        "disc_not_found": "搜索结束({}),已扫描 {} 项,未找到: {}",
        # 上探第二轮(仍缺失 -> 搜父目录,排除第一轮根;见 backend.discover)
        "disc_up_start": "第一轮未找全,上探父目录 {}(排除 {} — {})",
        "disc_up_skip": "无法继续上探: {} 没有可用的上层目录",
        "disc_timeout_2": "搜索超时:根 {} 与上层 {}(各 {}),已扫描 {} 项,仍缺少: {}",
        "disc_not_found_2": "搜索结束:根 {} 与上层 {}(各 {}),已扫描 {} 项,未找到: {}",
        "disc_error": "搜索失败: {}",
        "disc_bad_root": "搜索根目录不存在或不是目录: {}",
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
        "sel_multi_info": "已选中: {} 个进程",
        # 窗口探测 (Spy++)
        "spy_tooltip": "拖动瞄准镜到窗口以定位进程",
        "spy_drag_active": "正在拖动准星,请在目标窗口上松开鼠标",
        "spy_dragging": "探测窗口中: {} (PID: {})",
        "spy_matched_one": "通过窗口选中进程: {} (PID {})",
        "spy_matched_multi": "通过窗口标记 {} 个关联进程 (宿主 PID {})",
        "spy_not_found": "未找到窗口关联的活动进程 (PID {})",
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
        "about_unknown": "未知",
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
        "missing_files_msg": "Not found: {}\nMake sure {} and {} are in the same directory as "
                             "gui.py.",
        # Auto-discovery (discover.py)
        "m_rediscover": "Re-detect exe/dll",
        "disc_busy": "A background search is already running",
        "disc_all_present": "exe and dll are both in place; nothing to search",
        "disc_start": "Searching in background: {} — root {} ({})",
        "disc_limits": "depth<={} / timeout {}",
        "disc_seconds": "{}s",
        "disc_unlimited": "unlimited",
        "disc_found": "Found {}: {}",
        "disc_timeout": "Search timed out ({}), {} entries scanned, still missing: {}",
        "disc_not_found": "Search finished ({}), {} entries scanned, not found: {}",
        # Upper-level round (still missing -> search parent, excluding the
        # first-round root; see backend.discover)
        "disc_up_start": "Round 1 incomplete; searching parent {} (excluding {} — {})",
        "disc_up_skip": "Cannot search further up: no parent above {}",
        "disc_timeout_2": "Search timed out: root {} and parent {} (each {}), {} entries scanned, still missing: {}",
        "disc_not_found_2": "Search finished: root {} and parent {} (each {}), {} entries scanned, not found: {}",
        "disc_error": "Search failed: {}",
        "disc_bad_root": "Search root missing or not a directory: {}",
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
        "already_injected_msg": "Process {} ({}) is already injected.\nUnload it first, then use "
                                "in WT.",
        "wt_not_found": "wt.exe (Windows Terminal) not found. Install Windows Terminal and retry.",
        "wt_retry_portable": "wt failed to start, falling back to bundled portable: {}",
        "taking_over": "Taking over in WT: {} (pid={})",
        "wt_launched": "Launched: {} (pid={})\nA new WT tab will open the mediator and auto-inject "
                       "the process.\nClosing the tab ends the session.",
        "no_selection": "No Selection",
        "select_first": "Select a process from the list first",
        "sel_info": "Selected: {} (PID {})",
        "no_sel_info": "No process selected",
        "sel_multi_info": "Selected: {} processes",
        # Window Finder (Spy++)
        "spy_tooltip": "Drag crosshair to window to locate process",
        "spy_drag_active": "Dragging: release over the target window",
        "spy_dragging": "Inspecting window: {} (PID: {})",
        "spy_matched_one": "Selected process via window: {} (PID {})",
        "spy_matched_multi": "Selected {} processes via window (Host PID {})",
        "spy_not_found": "No matching process found for window (PID {})",
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
        "about_unknown": "unknown",
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
