"""结果文件协议（runner 侧）。

目标脚本把自检结果写入 results/<name>.txt，格式每行 KEY=VALUE。
约定：
  - 断言：<KEY>=PASS 或 <KEY>=FAIL:<原因>
  - 状态：DONE=1（脚本跑完）、QUIT=1、UNSUPPORTED=<原因>、OK=<值>
"""
import os
import time

from . import paths

def result_file(name: str) -> str:
    """返回测试名对应的结果文件绝对路径。"""
    return os.path.join(paths.RESULTS_DIR, name + ".txt")


def clear_result(name: str) -> None:
    """删除旧结果文件（测试开始前调用）。"""
    try:
        os.remove(result_file(name))
    except OSError:
        pass


def read_result(name: str) -> dict:
    """读取结果文件，返回 {KEY: VALUE}。重复 KEY 取最后一次。"""
    out = {}
    path = result_file(name)
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def wait_result(name: str, key: str, timeout: float = 20.0) -> str:
    """等待结果文件中出现 key，返回其 VALUE；超时返回 ""。

    与目标脚本的 check() 配套：断言值 PASS 或 FAIL:<detail>。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = read_result(name)
        if key in res:
            return res[key]
        time.sleep(0.2)
    return ""


def wait_done(name: str, timeout: float = 20.0) -> bool:
    """等待目标脚本写入 DONE=1。"""
    return wait_result(name, "DONE", timeout) == "1"


def check_value(actual: str, key: str, expected: str = "PASS") -> bool:
    """断言 wait_result 的返回值，返回是否通过（FAIL 前缀视为失败）。"""
    if actual == expected:
        return True
    if actual.startswith("FAIL"):
        return False
    return False
