"""特性: 高频大流量输出（5MB 无截断/无卡死）    类别: performance

链路: 目标 WriteFile 直通写 5MB 确定性数据 → DLL → BatchSender → mediator
      ChildVtOutput → WT stdout

预期:
  - 5MB 全部到达：ChildVtOutput len 总和 == 目标写入字节数（±补发序列小容差）
  - 内容完整：首 16 字节标记出现在 hex 头字节流中（内容确定性直通，无篡改）
  - 无卡死：目标在 60s 内完成全部写入（含 WT 消费背压）
  - 目标侧哈希记录供人工比对（mediator 日志 hex 只记前 256 字节/包，
    无法全量重建内容，字节完整性以 len 总和为准）

验证方式: 目标写入 + 自算 sha256 + 驱动从 mediator 日志统计字节数
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import vt_capture

NAME = "high_freq_output"

TOTAL_BYTES = 5 * 1024 * 1024  # 5MB
CHUNK = 64 * 1024

TARGET_BODY = f'''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import hashlib
h_out = get_std_out()
# 确定性内容：每行 80 字符 + \\n，纯 ASCII 可打印（无 ESC，直通字节精确）
row = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" + ".-_=" * 4
row = row[:80] + "\\n"
rows = {TOTAL_BYTES} // len(row)
body = row.encode("ascii") * rows
body = body[:{TOTAL_BYTES}]
rec("EXPECTED", str(len(body)))
rec("HEAD", body[:16].hex())
sha = hashlib.sha256(body).hexdigest()
# 64KB 分块 WriteFile 直通写
written_total = 0
ok = 0
t0 = time.time()
for off in range(0, len(body), {CHUNK}):
    chunk = body[off:off + {CHUNK}]
    n = wintypes.DWORD(0)
    if _k.WriteFile(h_out, chunk, len(chunk), ctypes.byref(n), None) and n.value == len(chunk):
        written_total += n.value
        ok += 1
    else:
        rec("FAIL_AT", str(off))
        break
rec("TIME_MS", str(int((time.time() - t0) * 1000)))
rec("OK", str(ok))
rec("WRITTEN", str(written_total))
rec("HASH", sha)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY",
                         ready_timeout=30.0)
            time.sleep(1.0)  # 让目标脚本开始输出
            v_ok = s.wait_result(NAME, "OK", timeout=60.0)
            v_written = s.wait_result(NAME, "WRITTEN", timeout=10.0)
            v_hash = s.wait_result(NAME, "HASH", timeout=10.0)
            v_time = s.wait_result(NAME, "TIME_MS", timeout=10.0)
            v_head = s.wait_result(NAME, "HEAD", timeout=10.0)
            v_expected = s.wait_result(NAME, "EXPECTED", timeout=10.0)

            if not v_ok or not v_written:
                print("  [FAIL] 目标未完成（疑似卡死/写入失败）: OK={} WRITTEN={}"
                      .format(v_ok, v_written))
                failures += 1
            else:
                expected = int(v_expected) if v_expected else TOTAL_BYTES
                print("  [INFO] 目标写完成: 耗时 {}ms, 写入 {} 字节（期望 {}）, "
                      "sha256={}".format(v_time, v_written, expected, v_hash))
                if int(v_ok) == 0 or int(v_written) != expected:
                    print("  [FAIL] 目标写入不完整: OK={} WRITTEN={}（期望 {}）"
                          .format(v_ok, v_written, expected))
                    failures += 1
                else:
                    # 目标 DONE 后 BatchSender 仍有最后一批未 flush（进程退出
                    # 时 Shutdown 最终 flush），等待日志字节数达到期望再断言
                    total = 0
                    head = b""
                    packets = 0
                    for _ in range(60):  # 最多 12s
                        total, head, packets = vt_capture.parse_child_vt_output(
                            s.log().read_all())
                        if total >= expected:
                            break
                        time.sleep(0.2)
                    over = total - expected
                    if not (0 <= over <= 8192):
                        print("  [FAIL] ChildVtOutput 字节不完整: 总和 {}（期望 {}，"
                              "容差 <=8192，差 {}），包数 {}"
                              .format(total, expected, over, packets))
                        failures += 1
                    else:
                        print("  [PASS] 字节完整性: {} 字节（含补发 {} 字节，{} 包）"
                              .format(total, over, packets))
                    exp_head = bytes.fromhex(v_head) if v_head else b""
                    if exp_head and exp_head in head:
                        print("  [PASS] 内容头部标记 {} 在 hex 流中（内容直通完整）"
                              .format(v_head))
                    else:
                        print("  [FAIL] 头部标记未在 hex 流中找到（内容可能被篡改）: "
                              "{}".format(v_head))
                        failures += 1
                    if v_time and int(v_time) > 30000:
                        print("  [FAIL] 写入耗时 {}ms（> 30s，疑似性能问题）"
                              .format(v_time))
                        failures += 1
                    else:
                        print("  [PASS] 写入耗时 {}ms（5MB 无卡死）".format(v_time))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
