"""keyboard 阶段公共目标模板与解析工具。

READER_TEMPLATE：目标脚本读输入队列，把 KEY_EVENT_RECORD 逐条写入结果文件。
每条格式：KEY<i>=<down> <repeat> <vk> <scan> <hex(uChar)> <ctrlState>

注意：模板内部不得出现 {}（外层 build_body 用 .format 注入参数）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

READER_TEMPLATE = '''
rec("READY", "PASS")
h_in = get_std_in()
h_wait = get_input_wait_handle()
set_mode(h_in, {MODE})
_k.FlushConsoleInputBuffer(h_in)

keys = []
deadline = time.time() + {TIMEOUT}
while len(keys) < {N} and time.time() < deadline:
    if wait_input(h_wait, 500):
        recs = read_input_records(h_in, 16)
        for r in recs:
            if r.EventType == KEY_EVENT:
                keys.append(r.KeyEvent)
    else:
        time.sleep(0.02)
for i, k in enumerate(keys):
    rec("KEY" + str(i), str(int(k.bKeyDown)) + " " + str(k.wRepeatCount) + " " +
        str(k.wVirtualKeyCode) + " " + str(k.wVirtualScanCode) + " " +
        hex(ord(k.uChar)) + " " + str(k.dwControlKeyState))
rec("KEYS", str(len(keys)))
done()
'''


def build_reader(n: int, timeout: float = 8.0, mode: int = 0) -> str:
    """生成读取 n 个按键事件的目标脚本。mode 为 stdin 模式，默认 0 原始模式"""
    return READER_TEMPLATE.format(MODE=mode, TIMEOUT=timeout, N=n)


def parse_keys(s, name: str, timeout: float = 5.0) -> list:
    """从结果文件解析 KEY<i> 事件列表（等待全部写出，KEYS 出现为准）。"""
    total = s.wait_result(name, "KEYS", timeout=timeout)
    if not total:
        return []
    keys = []
    for i in range(int(total)):
        v = s.wait_result(name, "KEY" + str(i), timeout=timeout)
        if not v:
            break
        parts = v.split()
        if len(parts) != 6:
            continue
        keys.append({
            "down": parts[0] == "1",
            "repeat": int(parts[1]),
            "vk": int(parts[2]),
            "scan": int(parts[3]),
            "char": chr(int(parts[4], 16)),
            "ctrl": int(parts[5]),
        })
    return keys


def assert_key(keys: list, idx: int, name: str, expected: dict) -> str:
    """断言 keys[idx] 的字段满足 expected 子集，返回 "" 或失败详情。"""
    if idx >= len(keys):
        return "{}: 事件不足 (got {} need >{})".format(name, len(keys), idx)
    k = keys[idx]
    for field, want in expected.items():
        if k[field] != want:
            return "{}: {}={!r} expected {!r} (key={!r})".format(
                name, field, k[field], want, k)
    return ""
