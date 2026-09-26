# tasks.py — 后台任务框架(线程 + 消息队列 + busy)
#
# 为什么需要它:注入/卸载/刷新都是阻塞的 subprocess 调用,放主线程会冻住界面;
# 而 tkinter 只能在主线程操作,所以后台线程只往 queue 写消息,主线程轮询派发。
#
# 三类任务:
#   run           写任务(注入/卸载/接管):busy 期间拒绝新任务并禁用工具栏按钮
#   run_readonly  只读刷新:不占 busy、不锁 UI,可与写任务并发
#   run_oneshot   一次性任务(取版本):不占 busy,也不发 done
# 队列消息:log / ok / err / refresh_ok / refresh_err / done

import queue
import threading

from .i18n import _t


class TaskRunner:
    """后台线程 + 消息队列 + busy 状态"""

    def __init__(self, root, log, on_error, on_busy):
        self._root = root
        self._log = log            # 日志渲染回调(text, tag),只在主线程调用
        self._on_error = on_error  # 失败回调(payload)
        self._on_busy = on_busy    # 忙碌回调(bool)
        self.queue = queue.Queue()  # 后台线程 -> 主线程消息队列
        self.busy = False           # 是否有写任务在跑,防止并发注入/卸载

    def run(self, fn, on_ok):
        """提交写任务:busy 期间拒绝并提示;结束消息 done 负责恢复按钮"""
        if self.busy:
            self._log(_t("task_running"), "err")
            return
        self.busy = True
        self._on_busy(True)
        self._submit(fn, on_ok, done=True)

    def run_readonly(self, fn, on_ok):
        """提交只读刷新任务:不占 busy、不锁 UI"""
        self._submit(fn, on_ok, prefix="refresh_")

    def run_oneshot(self, fn, on_ok):
        """提交一次性任务:不发 done,避免误清其他写任务的忙态"""
        self._submit(fn, on_ok)

    def post_log(self, text, tag="info"):
        """后台线程写日志的唯一入口:只入队,渲染留给主线程"""
        self.queue.put(("log", None, (text, tag)))

    def poll(self):
        """主线程轮询消息队列(100ms 周期)"""
        try:
            while True:
                kind, cb, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(*payload)
                elif kind in ("ok", "refresh_ok"):
                    cb(payload)
                elif kind in ("err", "refresh_err"):
                    self._on_error(payload)
                else:                      # done:写任务收尾
                    self.busy = False
                    self._on_busy(False)
        except queue.Empty:
            pass
        finally:
            # 某条消息的回调抛异常时,轮询链也不能中断
            self._root.after(100, self.poll)

    def _submit(self, fn, on_ok, prefix="", done=False):
        """工作线程入口:结果按 prefix 拼成 _ok/_err 入队,done 决定是否发收尾消息

        prefix="" -> ok/err(写任务与一次性任务);"refresh_" -> refresh_ok/refresh_err
        (只读刷新,不触碰 busy)。主线程侧派发见 poll。
        """
        def work():
            try:
                self.queue.put((prefix + "ok", on_ok, fn()))
            except Exception as exc:  # noqa: BLE001 - 统一上报给 UI
                self.queue.put((prefix + "err", on_ok, str(exc)))
            finally:
                if done:
                    self.queue.put(("done", None, None))
        threading.Thread(target=work, daemon=True).start()
