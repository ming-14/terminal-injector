"""PTY-Agent 辅助模块：用 PTY-Agent 替代 WT 作为 mediator 的 PTY 父端。

PTY-Agent 是独立 skill 项目（详见 PTY-Agent/SKILL.md），通过 pyte 终端
模拟器渲染 mediator 输出的 VT 序列，提供屏幕快照、光标定位、输入写入能力。

测试流程（与 injector.py 的 WT 路径并行，二者互斥）：
  1. start_target_cmd() 启动注入目标 cmd，返回 PID
  2. PtyAgentSession.start_mediator(pid, session_id) 启动 PTY-Agent 会话运行 mediator
     --snapshot-mode 启用屏幕快照模式，PTY-Agent 用 pyte 维护屏幕状态
  3. session.send(cmd_text, trigger=...) 发送命令文本到 PTY（替代 SendInput）
     --trigger 等待关键输出后返回屏幕快照
  4. session.read_snapshot() 读取屏幕快照验证渲染结果
  5. session.get_cursor_location() 获取光标位置
  6. session.kill() + cleanup() 清理

PTY-Agent 路径与 WT 路径的差异：
  - WT 路径：用 SendInput 模拟键盘 → 验证 mediator 日志字节流
  - PTY-Agent 路径：用 send 写入 PTY stdin → 验证 pyte 渲染的屏幕快照
  - 后者能验证"屏幕最终渲染内容"，前者只能验证"VT 字节是否流转"

依赖：psutil（进程管理）
环境变量：
  PTY_AGENT_PATH — PTY-Agent app.py 路径，未设置时使用默认位置
"""
import json
import os
import subprocess
import time
from typing import List, Optional, Tuple

import psutil

# 项目路径
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")

# PTY-Agent app.py 路径解析：
# 1. 优先环境变量 PTY_AGENT_PATH
# 2. 默认位置（与现有 manual 测试一致，PTY-Agent 项目根 app.py）
# 注意：不硬编码到代码逻辑中，仅作为环境变量未设置时的回退
_DEFAULT_PTY_AGENT_PATH = r"c:\Users\rikka\Desktop\PTY-Agent\PTY-Agent\app.py"


def find_pty_agent() -> str:
    """查找 PTY-Agent app.py 路径。

    优先环境变量 PTY_AGENT_PATH，未设置时使用默认位置。
    找不到时抛 FileNotFoundError，提示用户设置环境变量。
    """
    path = os.environ.get("PTY_AGENT_PATH", "")
    if path and os.path.exists(path):
        return path
    if os.path.exists(_DEFAULT_PTY_AGENT_PATH):
        return _DEFAULT_PTY_AGENT_PATH
    raise FileNotFoundError(
        "PTY-Agent app.py 未找到。请设置环境变量 PTY_AGENT_PATH 指向 app.py 路径，"
        "或确保默认位置存在: {}".format(_DEFAULT_PTY_AGENT_PATH)
    )


def start_target_cmd() -> int:
    """启动目标 cmd 进程（新控制台窗口），返回 PID。

    与 injector.start_target_cmd 一致：cmd 是注入目标，注入后输出被劫持到
    mediator → PTY-Agent。CREATE_NEW_CONSOLE 让 ConHost 输出版本横幅 + prompt，
    供 DLL StateSnapshot 捕获并补发。
    """
    proc = subprocess.Popen(
        ["cmd.exe"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=PROJECT_ROOT,
    )
    return proc.pid


def clear_log() -> None:
    """清空 mediator 日志（测试前调用，便于 wait_for_handshake 匹配新日志）。"""
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
    except OSError:
        pass


def wait_for_handshake(timeout: float = 15.0) -> bool:
    """等待 mediator 日志出现 'Handshake OK'，表示注入握手成功。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if "Handshake OK" in content:
                        return True
                    if "Handshake failed" in content or "ERROR" in content:
                        return False
            except OSError:
                pass
        time.sleep(0.3)
    return False


class PtyAgentSession:
    """PTY-Agent 会话封装。

    一个会话对应一个 PTY-Agent exec 启动的子进程（如 mediator）。
    通过 send/read/mouse 命令与 PTY 交互，所有命令通过 subprocess 调 app.py。
    """

    def __init__(self, session_id: str, size: str = "120x40"):
        """创建会话句柄。

        Args:
            session_id: PTY-Agent 会话标识（用于后续 send/read/kill）
            size: 终端尺寸 WxH（默认 120x40，避免 80x24 折行）
        """
        self.session_id = session_id
        self.size = size
        self._app_py = find_pty_agent()
        self._started = False

    def _run(self, args: List[str], timeout: float = 30.0,
             check: bool = False) -> Tuple[str, str, int]:
        """执行 PTY-Agent 命令，返回 (stdout, stderr, returncode)。

        Args:
            args: PTY-Agent 子命令参数（如 ["send", session_id, "ls", "-t", ">"]）
            timeout: 超时秒数
            check: 为 True 时 returncode != 0 抛 CalledProcessError
        """
        cmd = ["python", self._app_py] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            stderr = e.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return stdout, stderr, -1
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr)
        return result.stdout, result.stderr, result.returncode

    def start_mediator(self, target_pid: int,
                       snapshot_mode: bool = True,
                       timeout: float = 30.0) -> bool:
        """启动 PTY-Agent exec 会话运行 mediator。

        等效命令：
          python app.py exec <session_id> -c "<mediator> --mediator --target-pid <pid>"
              --size <WxH> --snapshot-mode --keep-ansi

        Args:
            target_pid: 注入目标 cmd 的 PID
            snapshot_mode: 是否启用屏幕快照模式（强烈建议 True）
            timeout: exec 启动超时（默认 30s，含 idle-timeout 等待握手）

        Returns:
            True 表示启动成功（PTY-Agent exec 返回 0），False 表示失败
        """
        cmd_str = '"{}" --mediator --target-pid {}'.format(MEDIATOR_EXE, target_pid)
        args = [
            "exec", self.session_id,
            "-c", cmd_str,
            "--size", self.size,
            # 不用 --snapshot-mode 让 exec 阻塞等待，改用 --idle-timeout 让 exec
            # 在握手+屏幕补发完成后立即返回（约 3 秒无新输出）
            "--idle-timeout", "3",
            "--timeout", "20",
            "--keep-ansi",
        ]
        if snapshot_mode:
            args.append("--snapshot-mode")

        stdout, stderr, rc = self._run(args, timeout=timeout)
        if rc != 0:
            print("[pty_agent] exec failed rc={} stderr={}".format(rc, stderr[:500]))
            return False
        self._started = True
        return True

    def send(self, text: str,
             trigger: Optional[str] = None,
             snapshot: bool = False,
             timeout: float = 15.0,
             send_eol: str = "cr",
             json_escaping: bool = False,
             idle_timeout: Optional[float] = None) -> dict:
        """向 PTY stdin 写入文本，可选等待触发条件后返回屏幕快照或增量输出。

        Args:
            text: 要发送的文本（默认 raw 模式，不转义）
            trigger: 正则触发条件，匹配后返回（屏幕快照模式下匹配屏幕变化行）
            snapshot: True 返回屏幕快照，False 返回增量输出
            timeout: 等待超时
            send_eol: 末尾追加行尾符（"cr"=\\r, "lf"=\\n, "crlf"=\\r\\n, "none"=不追加）
            json_escaping: True 启用 -j 转义模式（{enter} {ctrl+c} 等控制字符）
            idle_timeout: 静默超时（指定秒数内无新输出时返回）

        Returns:
            PTY-Agent 响应 JSON 解析后的 dict，主要字段：
              outputStream: 屏幕快照文本或增量输出
              triggerReturnReason: matched/timeout/idle_timeout/ok
              program: 子进程运行状态
        """
        args = ["send", self.session_id, text]
        if trigger:
            args.extend(["-t", trigger])
        if snapshot:
            args.append("--snapshot")
        if json_escaping:
            args.append("-j")
        args.extend(["-e", send_eol])
        args.extend(["--timeout", str(int(timeout))])
        if idle_timeout is not None:
            args.extend(["--idle-timeout", str(idle_timeout)])

        stdout, stderr, rc = self._run(args, timeout=timeout + 5)
        return self._parse_json(stdout, stderr, rc)

    def read_snapshot(self, lines: Optional[str] = None,
                      grep: Optional[str] = None,
                      timeout: float = 15.0) -> dict:
        """读取屏幕快照（不消费 offset）。

        Args:
            lines: 行范围（如 "10" 最后 10 行，"5:10" 第 5-10 行）
            grep: 正则过滤行
            timeout: 超时

        Returns:
            PTY-Agent 响应 JSON dict，outputStream 字段为屏幕快照文本
        """
        args = ["read", self.session_id, "--snapshot"]
        if lines:
            args.extend(["-l", lines])
        if grep:
            args.extend(["-g", grep])
        args.extend(["--timeout", str(int(timeout))])

        stdout, stderr, rc = self._run(args, timeout=timeout + 5)
        return self._parse_json(stdout, stderr, rc)

    def get_cursor_location(self, timeout: float = 15.0) -> Optional[dict]:
        """获取 PTY 终端光标位置（col, row）及所在行内容。

        Returns:
            光标信息 dict（含 col, row, line），失败返回 None
        """
        args = ["mouse", self.session_id, "_get_cursor_location",
                "--timeout", str(int(timeout))]
        stdout, stderr, rc = self._run(args, timeout=timeout + 5)
        data = self._parse_json(stdout, stderr, rc)
        if data.get("performed"):
            return data.get("cursor")
        return None

    def kill(self) -> None:
        """终止 PTY-Agent 会话（不报错，幂等）。"""
        if not self._started:
            return
        try:
            self._run(["kill", self.session_id], timeout=10)
        except Exception:
            pass
        self._started = False

    @staticmethod
    def _parse_json(stdout: str, stderr: str, rc: int) -> dict:
        """解析 PTY-Agent 响应 JSON。

        失败时返回包含 error 字段的 dict，便于调用方判断。
        """
        if not stdout.strip():
            return {"error": "empty stdout", "stderr": stderr, "returncode": rc}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            return {
                "error": "json decode failed: {}".format(e),
                "stdout_head": stdout[:500],
                "stderr": stderr[:500],
                "returncode": rc,
            }
        return data


def parse_snapshot_text(data: dict) -> str:
    """从 PTY-Agent 响应中提取屏幕快照文本。

    PTY-Agent 不同模式返回字段不同：
      - outputStream 可能是 str（直接文本）或 list（行列表）或 dict（含 cursor）
    本函数统一返回 str（多行文本）。
    """
    if not data:
        return ""
    if "error" in data:
        return ""
    stream = data.get("outputStream")
    if stream is None:
        # 兜底从 output 字段取
        output = data.get("output", {})
        if isinstance(output, dict):
            stream = output.get("snapshot") or output.get("text") or output.get("screen")
        else:
            stream = output
    if isinstance(stream, str):
        return stream
    if isinstance(stream, list):
        return "\n".join(str(x) for x in stream)
    if isinstance(stream, dict):
        return stream.get("snapshot") or stream.get("text") or ""
    return ""


def cleanup(target_pid: int, session: Optional[PtyAgentSession] = None) -> None:
    """清理测试进程：终止目标 cmd + PTY-Agent 会话 + mediator 进程。

    遵循 project_memory 规则：自动终止测试遗留进程，无需询问用户。
    """
    # 终止 PTY-Agent 会话
    if session is not None:
        session.kill()

    # 终止目标 cmd（及其子进程，包括 python 测试目标程序）
    try:
        p = psutil.Process(target_pid)
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        p.terminate()
        p.wait(timeout=3)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass

    # 清理可能残留的 terminal_injector.exe（mediator / inject 子进程）
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = proc.info["name"] or ""
            if name.lower() == "terminal_injector.exe":
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    # 等待进程退出
    time.sleep(1.0)
