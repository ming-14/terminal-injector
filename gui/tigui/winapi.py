# winapi.py — Win32 直接调用(ctypes):查 injected.dll 基址 + 子进程输出解码

import ctypes
from pathlib import Path

from .i18n import _t
from .paths import DLL_NAME


# 进程访问权限(ctypes 查询模块基址用)
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03
TH32CS_SNAPPROCESS = 0x00000002
GA_ROOT = 2

# 窗口探测(准星)永不自动选中的外壳进程:它们只是控制台宿主,不可注入,
# 选中只会污染操作对象;实体命令行进程才是真正的目标。
# 仅作用于窗口探测链路,列表中手工点选不受限制。
SPY_EXCLUDED_NAMES = frozenset({"conhost.exe", "openconsole.exe"})


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260)
    ]


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


# 模块级 Win32 句柄:原型只声明一次,避免每次调用重复设置 argtypes/restype
_user32 = ctypes.windll.user32
_user32.WindowFromPoint.argtypes = [POINT]
_user32.WindowFromPoint.restype = ctypes.c_void_p
_user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
_user32.GetCursorPos.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
_user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_user32.GetAncestor.restype = ctypes.c_void_p
_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
_user32.GetWindowRect.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int

_kernel32 = ctypes.windll.kernel32
_kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
_kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
_kernel32.Process32FirstW.restype = ctypes.c_int
_kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32)]
_kernel32.Process32NextW.restype = ctypes.c_int
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def get_window_at_cursor():
    """获取鼠标光标所在顶层窗口:(hwnd, pid, 标题, (left, top, right, bottom))

    窗口不可用(桌面空白/最小化)时 hwnd 为 None 或矩形无效,由调用方决定是否提示。
    """
    pt = POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return None, 0, "", (0, 0, 0, 0)
    root_hwnd = _user32.GetAncestor(hwnd, GA_ROOT) or hwnd

    pid = ctypes.c_uint32()
    _user32.GetWindowThreadProcessId(root_hwnd, ctypes.byref(pid))
    title_buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(root_hwnd, title_buf, 512)
    rc = RECT()
    _user32.GetWindowRect(root_hwnd, ctypes.byref(rc))

    return root_hwnd, pid.value, title_buf.value, (
        rc.left, rc.top, rc.right, rc.bottom)


def get_process_relations():
    """获取系统所有活动进程的父子关系字典:(名称, 父PID, 子PID列表)"""
    hSnapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not hSnapshot or hSnapshot == ctypes.c_void_p(-1).value:
        return {}, {}, {}
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    names, parents, children = {}, {}, {}
    if _kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe)):
        while True:
            pid = pe.th32ProcessID
            ppid = pe.th32ParentProcessID
            name = pe.szExeFile
            names[pid] = name
            parents[pid] = ppid
            children.setdefault(ppid, []).append(pid)
            if not _kernel32.Process32NextW(hSnapshot, ctypes.byref(pe)):
                break
    _kernel32.CloseHandle(hSnapshot)
    return names, parents, children


def find_associated_processes(window_pid: int, targets: list) -> list:
    """根据窗口所属 PID 及进程树，查找其对应的所有关联进程（含全部子进程）

    对于 Windows Terminal 或 conhost 等外壳宿主，自动深搜整棵子进程树，
    提取出所有正在运行的关联子进程，可注入者排在前面。
    """
    target_dict = {t["pid"]: t for t in targets}
    names, parents, children = get_process_relations()

    def get_descendants(root_pid):
        desc = []
        queue = [root_pid]
        visited = {root_pid}
        while queue:
            curr = queue.pop(0)
            for child in children.get(curr, []):
                if child not in visited:
                    visited.add(child)
                    desc.append(child)
                    queue.append(child)
        return desc

    candidate_roots = [window_pid]
    wname = names.get(window_pid, "").lower()

    # 如果窗口直接属于 conhost，其父进程（控制台客户端进程）也作为候选根
    if "conhost" in wname:
        ppid = parents.get(window_pid)
        if ppid:
            candidate_roots.append(ppid)

    # 收集整棵树的所有子孙进程
    all_related_pids = set()
    for root in candidate_roots:
        all_related_pids.add(root)
        all_related_pids.update(get_descendants(root))

    # 过滤出当前 targets 中实际存在的进程项
    matched_pids = [pid for pid in all_related_pids if pid in target_dict]

    # 黑名单:conhost/OpenConsole 仅为控制台外壳,探测时永不自动选中。
    # 若黑名单把结果清空(窗口本身即外壳且无其他关联),回退原集合,
    # 保证用户在准星上松开鼠标后总能得到选中反馈。
    picked = [pid for pid in matched_pids
              if target_dict[pid].get("name", "").lower() not in SPY_EXCLUDED_NAMES]
    if not picked and matched_pids:
        picked = matched_pids

    # 优先级排序：
    # 1. injectable 优先于不可注入
    # 2. 具体工作进程优先于外壳宿主（conhost, windowsterminal 等）
    # 3. 后创建的进程优先
    def sort_key(p):
        t = target_dict[p]
        pname = t.get("name", "").lower()
        is_shell = pname in ("windowsterminal.exe", "openconsole.exe", "conhost.exe")
        return (not t.get("injectable", False), is_shell, -p)

    picked.sort(key=sort_key)
    return picked
