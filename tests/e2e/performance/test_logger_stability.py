"""特性: Logger 双路高频写入无死锁（200 次触发）    类别: performance

链路: 目标高频 WriteConsoleW → DLL 每次调用产生多条日志（Logger worker
      异步写 OutputDebugString + 文件双路）→ 验证 worker 不卡死

预期:
  - 200 次调用全部成功返回（Hook 路径无死锁）
  - 子进程 DLL 日志文件在测试期间持续增长（worker 正常消费队列）
  - 日志行数 >= 调用次数（每次调用至少 1 条日志）

验证方式: 目标自检 + 驱动轮询 %TEMP%\\injected_*.log（按 mtime 定位
          本次会话新增的子进程日志）检查增长与行数
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from common import childlog

NAME = "logger_stability"

CALLS = 200
MIN_GROWTH_BYTES = 10 * 1024  # 200 次调用 × 每次多条日志（实测 ~22KB，留余量）


TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
msg = "L" * 64 + "\\n"
ok = 0
t0 = time.time()
for _ in range({CALLS}):
    n = wintypes.DWORD(0)
    if _k.WriteConsoleW(h_out, msg, len(msg), ctypes.byref(n), None) \
            and n.value == len(msg):
        ok += 1
    else:
        rec("FAIL_AT", str(_))
        break
rec("TIME_MS", str(int((time.time() - t0) * 1000)))
rec("OK", str(ok))
rec("PID", str(os.getpid()))
done()
'''.format(CALLS=CALLS)


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY",
                         ready_timeout=30.0)
            v_ok = s.wait_result(NAME, "OK", timeout=60.0)
            v_time = s.wait_result(NAME, "TIME_MS", timeout=10.0)
            v_pid = s.wait_result(NAME, "PID", timeout=10.0)
            if not v_ok:
                print("  [FAIL] 目标未完成（疑似 Hook/Logger 死锁）")
                failures += 1
            else:
                print("  [INFO] 目标完成: OK={} 耗时 {}ms"
                      .format(v_ok, v_time))
                if int(v_ok) != CALLS:
                    print("  [FAIL] 成功 {} 次（期望 {}）".format(v_ok, CALLS))
                    failures += 1
                else:
                    print("  [PASS] {} 次调用全部成功（无死锁）".format(CALLS))
            # 子进程 DLL 日志按 pid 精确定位（模糊 glob 会误取前序测试
            # 延迟出现的日志文件，全量回归时实测偶发）
            log_path = childlog.latest_injected_log(int(v_pid))
            if not log_path:
                print("  [FAIL] 未发现子进程 DLL 日志（injected_{}_*.log）"
                      .format(v_pid))
                failures += 1
            else:
                print("  [INFO] 子进程 DLL 日志: {}".format(
                    os.path.basename(log_path)))
                # 日志文件增长验证（Logger worker 无死锁）
                time.sleep(1.0)  # 等 worker 把队列刷完
                size = os.path.getsize(log_path)
                with open(log_path, "r", encoding="utf-8",
                          errors="replace") as f:
                    lines = f.readlines()
                if size >= MIN_GROWTH_BYTES and len(lines) >= CALLS:
                    print("  [PASS] 日志增长 {}+KB，行数 {}（>= 调用次数 {}，"
                          "worker 持续写入）"
                          .format(size // 1024, len(lines), CALLS))
                else:
                    print("  [FAIL] 日志增长不足: 增长 {}B（期望 >= {}B），行数 {}"
                          .format(size, MIN_GROWTH_BYTES, len(lines)))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
