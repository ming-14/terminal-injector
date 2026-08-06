"""特性: OSC 7 工作目录    类别: special_sequences

链路: 目标 WriteFile `\\x1b]7;file:///<cwd>\\x07` → DLL → mediator ChildVtOutput →
      ConPTY → WT（WT 用 OSC 7 更新标签页的工作目录）

预期:
  - mediator 日志含 OSC 7 序列字节
  - 序列内容为文件系统当前工作目录（动态生成，不硬编码路径）

验证方式: 目标发送 + 上报 hex + 驱动解析日志字节
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_workdir"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
# OSC 7 工作目录（file:// URI）：用当前工作目录动态构造，避免写死机器路径
import os as _os
_cwd = _os.getcwd().replace("\\\\", "/")
_payload = b"\\x1b]7;file:///" + _cwd.encode("utf-8") + b"\\x07"
rec("HEX", " ".join("{:02X}".format(b) for b in _payload))
ok, _ = write_bytes(h_out, _payload)
rec("SENT", str(int(ok)))
time.sleep(1.0)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "SENT", timeout=15.0)
            hexval = s.wait_result(NAME, "HEX", timeout=15.0)
            if not v or not hexval:
                print("  [FAIL] SENT/HEX: 无结果")
                failures += 1
            else:
                # 按目标上报的 hex 字节匹配 mediator 日志（避免硬编码路径断言）
                pattern = r"hex\[\d+\]=" + re.escape(hexval)
                m = s.log().wait_for_regex(pattern, timeout=8.0)
                if m:
                    print("  [PASS] 日志含 OSC 7 工作目录序列（发送侧直通）")
                else:
                    print("  [FAIL] 日志未找到 OSC 7 工作目录序列")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
