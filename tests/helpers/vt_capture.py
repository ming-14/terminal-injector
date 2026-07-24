"""VT 输出捕获与日志验证模块。

读取 mediator 日志，解析 VT 序列流转记录，验证预期模式。
mediator 日志格式示例：
  stdin→router: converted 4 bytes: 64 69 72 0D    (用户输入 d i r \r)
  RouteInput: routed to parent, len=4 sent=18/18  (路由到目标进程)
  pipe→stdout: VtOutput len=6 written=6 ok=1      (DLL 输出 VT)

验证策略：
  - 用户输入 → 日志 stdin→router 字节匹配
  - DLL 输出 → 日志 pipe→stdout: VtOutput 出现
"""
import os
import re
import time
from typing import List, Optional


class MediatorLog:
    """mediator 日志读取器，支持增量读取（只读 mark 之后的新增内容）。"""

    def __init__(self, path: str):
        self.path = path
        self._offset = 0

    def mark(self) -> None:
        """记录当前日志末尾位置，后续 read_new 只读新增部分。"""
        if os.path.exists(self.path):
            self._offset = os.path.getsize(self.path)
        else:
            self._offset = 0

    def read_new(self) -> str:
        """读取 mark 之后新增的日志内容，并更新 offset。"""
        if not os.path.exists(self.path):
            return ""
        try:
            size = os.path.getsize(self.path)
            if size < self._offset:
                # 日志被截断/重建，从头读
                self._offset = 0
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._offset)
                content = f.read()
                self._offset = f.tell()
            return content
        except OSError:
            return ""

    def wait_for(self, pattern: str, timeout: float = 5.0) -> bool:
        """等待新增日志中出现 pattern（字符串子串匹配）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            content = self.read_new()
            if pattern in content:
                return True
            time.sleep(0.2)
        return False

    def wait_for_regex(self, pattern: str, timeout: float = 5.0) -> Optional[re.Match]:
        """等待新增日志中出现匹配 pattern 的正则，返回 Match 或 None。"""
        deadline = time.time() + timeout
        regex = re.compile(pattern)
        while time.time() < deadline:
            content = self.read_new()
            m = regex.search(content)
            if m:
                return m
            time.sleep(0.2)
        return None


def parse_stdin_bytes(log: str) -> List[int]:
    """从日志提取所有 stdin→router: converted N bytes: XX XX 的字节序列。

    返回所有匹配行的字节合并列表（按出现顺序）。
    """
    result = []
    # 匹配 "stdin→router: converted 4 bytes: 64 69 72 0D "
    pattern = re.compile(r"stdin.*router: converted \d+ bytes: ((?:[0-9A-Fa-f]{2} )*)")
    for m in pattern.finditer(log):
        hex_str = m.group(1).strip()
        if hex_str:
            result.extend(int(x, 16) for x in hex_str.split())
    return result


def parse_route_input_bytes(log: str) -> List[int]:
    """从日志提取 RouteInput: routed to parent 行的字节数。

    返回每行 len 值的列表（用于验证输入被路由）。
    """
    result = []
    pattern = re.compile(r"RouteInput: routed to parent, len=(\d+)")
    for m in pattern.finditer(log):
        result.append(int(m.group(1)))
    return result


def count_vt_output(log: str) -> int:
    """统计 pipe→stdout: VtOutput 出现次数（DLL 输出 VT 序列次数）。"""
    return len(re.findall(r"pipe.*stdout: VtOutput", log))


def bytes_to_hex(data: bytes) -> str:
    """字节转十六进制字符串（大写，空格分隔）。"""
    return " ".join("{:02X}".format(b) for b in data)


def verify_input_in_log(log: str, expected_bytes: bytes) -> bool:
    """验证 expected_bytes 是否作为子序列出现在 stdin→router 字节流中。

    用于验证用户输入（经 SendInput）是否被 mediator 正确接收。
    """
    actual = parse_stdin_bytes(log)
    expected = list(expected_bytes)
    # 子序列匹配：expected 中的字节按顺序出现在 actual 中
    i = 0
    for b in actual:
        if i < len(expected) and b == expected[i]:
            i += 1
        if i >= len(expected):
            return True
    return i >= len(expected)


def verify_utf8_input(log: str, text: str) -> bool:
    """验证 text 的 UTF-8 编码是否出现在 stdin→router 字节流中。"""
    return verify_input_in_log(log, text.encode("utf-8"))
