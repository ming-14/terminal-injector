"""结果文件协议（目标侧自检 → 驱动侧断言）。

与 v1（tests/e2e/common/result.py）同一套协议，刻意保持一致：
目标脚本每行写 `KEY=VALUE`，断言值为 `PASS` / `FAIL:<原因>`，跑完写 `DONE=1`。
文件落在本套件的 _results/ 下。

先跑通握手再断言的目标脚本一律先 `rec("READY","PASS")`，驱动侧
`run_target(..., ready_key="READY")` 用它确认脚本真的起来了
（避免"注入没通 → 断言超时"和"脚本没起来 → 断言超时"混为一谈）。
"""
import os
import time

from . import paths


def result_file(name: str) -> str:
    return os.path.join(paths.RESULTS_DIR, name + ".txt")


def clear_result(name: str) -> None:
    """删除旧结果文件（测试开始前调用，避免上一轮残留造成假 PASS）。"""
    try:
        os.remove(result_file(name))
    except OSError:
        pass


def read_result(name: str) -> dict:
    """读取结果文件 → {KEY: VALUE}（重复 KEY 取最后一次）。"""
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
    """等待出现 key，返回其 VALUE；超时返回 ""。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = read_result(name)
        if key in res:
            return res[key]
        time.sleep(0.2)
    return ""


def wait_done(name: str, timeout: float = 20.0) -> bool:
    return wait_result(name, "DONE", timeout) == "1"
