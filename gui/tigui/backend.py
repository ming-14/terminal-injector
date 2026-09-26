# backend.py — terminal_injector.exe 调用层
# 设计:所有子进程交互集中在此,不依赖 tkinter,可脱离 GUI 单独测试。
# log 回调把操作日志交给 UI(None 时静默);线程安全入口见 tasks.post_log。
# 与 CLI 的参数约定:
#   --list-targets --json --all / --inject <pid> / --unload-remote <pid> <dllBase>
#   --mediator --target-pid <pid> --pipe <name>(由 wt 新 tab 承载)

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .discover import (SEARCH_MAX_DEPTH, SEARCH_TIMEOUT, DiscoveryResult,
                       SearchRound, describe_limits, find_binaries)
from .i18n import _t
from .paths import DLL_NAME, EXE_NAME, resolve_base_dir
from .winapi import decode_output, find_dll_base


class InjectorBackend:
    """terminal_injector.exe / injected.dll 封装(exe/dll 与 gui.py 同目录)"""

    def __init__(self, log=None):
        self._log_sink = log
        # exe/dll 定位:PyInstaller onefile 打包时随包解压到 _MEIPASS,
        # 源码运行时取入口脚本所在目录(见 paths.resolve_base_dir)
        self.exe_dir = resolve_base_dir()
        self.exe_path = self.exe_dir / EXE_NAME
        self.dll_path = self.exe_dir / DLL_NAME

    def set_log_sink(self, log):
        """设置日志回调(UI 面板建好后接管)"""
        self._log_sink = log

    def log(self, text, tag="info"):
        """与主窗口 log 同签名:搬运而来的方法体可原样调用"""
        if self._log_sink is not None:
            self._log_sink(text, tag)

    def missing_binaries(self):
        """启动校验:返回缺失的二进制名(缺失仅提示,不阻塞启动)"""
        return [p.name for p in (self.exe_path, self.dll_path)
                if not p.exists()]

    def _run(self, *args, timeout):
        """执行 terminal_injector.exe(隐藏子进程窗口),返回 CompletedProcess

        stdout/stderr 保留原始 bytes:JSON 交给 json.loads 自识别编码,
        文本场景由调用方 decode_output(UTF-8 -> GBK -> replace)。
        """
        return subprocess.run([str(self.exe_path), *args],
                              capture_output=True, timeout=timeout,
                              creationflags=subprocess.CREATE_NO_WINDOW)

    # ---------- 自动探测 ----------

    def discover(self, root=None, max_depth=SEARCH_MAX_DEPTH,
                 timeout=SEARCH_TIMEOUT) -> DiscoveryResult:
        """递归搜索缺失的 exe/dll 并回填 self.exe_path / self.dll_path

        两轮:
          第一轮  在 root(默认程序自身目录)下按深度/超时搜索;
          第二轮  仍缺失则上探一层到第一轮根的父目录,排除第一轮根的整棵
                  子树,超时预算独立(同样用 timeout),只补仍缺失的名字。
        两轮统计合并返回,第二轮记在 parent_round(discover.SearchRound)。

        **阻塞**,只在工作线程跑(GUI 侧走 tasks.run_readonly)。
        只补缺失项,已就位的路径绝不覆盖;全部就位时直接返回,不扫描。
        搜索根默认是程序自身目录(paths.resolve_program_dir),与
        exe_dir(onefile 时是 _MEIPASS)可能不同 —— 缺失本来就意味着
        约定位置没有,得往真实发布位置下面找。
        """
        missing = self.missing_binaries()
        if not missing:
            return DiscoveryResult({}, Path(self.exe_dir))
        res = find_binaries(missing, root=root, max_depth=max_depth,
                            timeout=timeout)
        self._fill_paths(res.found)
        left = self.missing_binaries()
        if not left or res.error:
            return res                      # 第一轮就齐了 / 根本没搜成
        return self._search_upper(res, left, max_depth, timeout)

    def _fill_paths(self, found):
        """把命中的『原始文件名 -> 绝对路径』回填到 exe_path / dll_path"""
        for name, path in found.items():
            if name == EXE_NAME:
                self.exe_path = path
            elif name == DLL_NAME:
                self.dll_path = path

    def _search_upper(self, res, names, max_depth, timeout) -> DiscoveryResult:
        """第二轮:上探一层到 res.root 的父目录,排除 res.root 子树(**阻塞**)

        父目录不存在、或 res.root 已经是根(盘符根的 parent 是它自己)时
        不搜,原样返回且不记 parent_round。
        """
        parent = res.root.parent
        if parent == res.root or not parent.is_dir():
            self.log(_t("disc_up_skip").format(res.root), "err")
            return res
        self.log(_t("disc_up_start").format(
            parent, res.root, describe_limits(max_depth, timeout)), "info")
        r2 = find_binaries(names, root=parent, max_depth=max_depth,
                           timeout=timeout, exclude=res.root)
        if r2.error:
            # 第二轮自身失败(父目录在调用瞬间消失等)只单独报一条,
            # 不并进 error:否则会连带吞掉第一轮已找到的结果与刷新
            self.log(_t("disc_error").format(r2.error), "err")
        self._fill_paths(r2.found)
        return res._replace(
            found=dict(res.found, **r2.found),   # 名字来自 left,不会重叠
            elapsed=res.elapsed + r2.elapsed,
            scanned=res.scanned + r2.scanned,
            timed_out=res.timed_out or r2.timed_out,
            parent_round=SearchRound(r2.root, r2.scanned, r2.timed_out,
                                     r2.elapsed),
        )

    # ---------- 列表 ----------

    def list_targets(self):
        # --all 取全量（含不可注入及原因），'仅显示可注入'过滤在 _render_table 做
        res = self._run("--list-targets", "--json", "--all", timeout=30)
        if res.returncode != 0:
            raise RuntimeError(
                _t("fetch_err").format(
                    res.returncode, decode_output(res.stderr).strip()))
        return json.loads(res.stdout)

    def version(self):
        """--version:状态栏显示"""
        return decode_output(self._run("--version", timeout=10).stdout).strip()

    # ---------- 注入 / 卸载 ----------

    def inject(self, pid):
        self.log(_t("injecting").format(pid=pid), "cmd")
        args = ["--inject", str(pid)]
        if self.dll_path.exists():
            args += ["--dll", str(self.dll_path)]
        res = self._run(*args, timeout=60)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(
                _t("inject_failed").format(res.returncode, out))
        return _t("inject_ok").format(pid=pid, out=out)

    def unload(self, pid):
        base = find_dll_base(pid)   # 目标进程可能已退出 -> 抛异常
        self.log(_t("unloading").format(pid=pid, base=base), "cmd")
        res = self._run("--unload-remote", str(pid), f"0x{base:X}", timeout=30)
        out = (decode_output(res.stdout) + decode_output(res.stderr)).strip()
        if res.returncode != 0:
            raise RuntimeError(
                _t("unload_failed").format(res.returncode, out))
        return _t("unload_ok").format(pid=pid, out=out)

    def unload_many(self, pids):
        results = []
        for pid in pids:
            try:
                results.append(self.unload(pid))
            except Exception as exc:  # noqa: BLE001 - 单进程失败不中断
                results.append(_t("unload_one_failed").format(pid, exc))
        return "\n".join(results)

    # ---------- 在 WT 中使用 ----------

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

    def launch_in_wt(self, pid, name):
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
