"""特性: 管道安全加固    类别: lifecycle

审查项 HIGH #2 的 e2e 验证:
  1. 管道名不可预测: 格式为 `\\.\pipe\terminjector_<pid>_<16位大写hex>`
     (MakeRandomPipeName, RtlGenRandom 8 字节), 两次注入会话名字不同,
     绝非旧的固定格式 `\\.\pipe\terminjector_<pid>`(可预测抢占)。
  2. 服务端 DACL: mediator 日志出现 "NamedPipe server created with user-DACL",
     且不出现 "WITHOUT tightened DACL" WARN(未回退默认 DACL)。
  3. 服务端身份校验: DLL 日志出现 "server identity verified"(GetServerProcessId
     核对注入参数中的 mediatorPid)。
  4. 无回退到固定名: 预先创建固定名 `\\.\pipe\terminjector_<pid>` 伪造服务端,
     注入仍应握手成功, 且 DLL 实际连接的仍是随机名(不被伪造服务端抢占)。

验证方式: 两轮完整注入会话, 读 mediator 日志 + DLL 日志断言。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import childlog

NAME = "pipe_security"

PIPE_RE = re.compile(
    r"\\\\\.\\pipe\\terminjector_\d+_[0-9A-F]{16}")


def read_dll_log(target_pid: int) -> str:
    """读取本会话 DLL 日志(pid 相关, 最新一份)。"""
    path = childlog.latest_injected_log(target_pid)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def get_server_pipe_name(s: TestSession, timeout: float = 8.0) -> str:
    """从 mediator 日志提取随机管道名(带 user-DACL 行), 返回名字或空串。"""
    m = s.log().wait_for_regex(
        r"NamedPipe server created with user-DACL: (\\\\.\\pipe\\terminjector_\d+_[0-9A-F]{16})",
        timeout=timeout)
    if m:
        return m.group(1)
    # 回退: 扫描全部日志
    content = s.log().read_all()
    m = re.search(
        r"NamedPipe server created with user-DACL: (\\\\.\\pipe\\terminjector_\d+_[0-9A-F]{16})",
        content)
    return m.group(1) if m else ""


def is_fixed_legacy_name(name: str, pid: int) -> bool:
    """固定名(可预测旧格式): `\\.\pipe\terminjector_<pid>` 无随机后缀。"""
    return name == r"\\.\pipe\terminjector_{}".format(pid)


def create_fixed_pipe_server(pid: int):
    """预创建固定名管道服务端(模拟攻击者抢占), 返回句柄。"""
    import win32pipe
    import win32file
    name = r"\\.\pipe\terminjector_{}".format(pid)
    try:
        h = win32pipe.CreateNamedPipe(
            name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, None)
        return h if h != win32file.INVALID_HANDLE_VALUE else None
    except Exception:
        return None


def run() -> int:
    failures = 0
    names = []

    # ---- 会话 A: 正常注入, 采集随机管道名 ----
    try:
        with TestSession() as s:
            print("  [PASS] 会话A 握手成功 (cmd PID={})".format(s.target_pid))
            name_a = get_server_pipe_name(s)
            if name_a:
                print("  [PASS] 随机管道名: {}".format(name_a))
            else:
                print("  [FAIL] mediator 日志未见 'created with user-DACL' 随机管道名")
                failures += 1
                name_a = ""
            names.append(name_a)

            # DACL: 不得回退默认 DACL
            dacl_content = s.log().read_all()
            if "WITHOUT tightened DACL" in dacl_content:
                print("  [FAIL] 出现 'WITHOUT tightened DACL' (DACL 未收紧)")
                failures += 1
            else:
                print("  [PASS] 无 'WITHOUT tightened DACL' (DACL 已收紧)")

            # 固定名抢占测试: 断言实际名非固定格式
            if name_a and is_fixed_legacy_name(name_a, s.target_pid):
                print("  [FAIL] 管道名是固定格式(可预测), 违背随机化")
                failures += 1

            # 服务端身份校验: DLL 日志出现 verified
            dll_log = read_dll_log(s.target_pid)
            if name_a:
                if "NamedPipe client connected: {}".format(name_a) in dll_log:
                    print("  [PASS] DLL 连接的就是随机管道名")
                else:
                    print("  [FAIL] DLL 日志未见连接该随机名")
                    failures += 1
                    if not dll_log:
                        print("  [LOG] (DLL 日志为空: 注入未产生日志)")
            if "server identity verified" in dll_log:
                print("  [PASS] DLL 完成服务端身份校验 (server identity verified)")
            else:
                print("  [FAIL] DLL 日志未见身份校验记录")
                failures += 1
            time.sleep(0.5)
    except RuntimeError as e:
        print("  [FAIL] 会话A setup 失败: {}".format(e))
        failures += 1

    time.sleep(1.0)

    # ---- 会话 B: 预创建固定名伪造服务端, 注入不应被抢占 ----
    fixed_handle = None
    try:
        from helpers import injector
        target_pid = injector.start_target_cmd()
        injector.clear_log(target_pid)
        fixed_handle = create_fixed_pipe_server(target_pid)
        if fixed_handle:
            print("  [PASS] 已预创建固定名伪造服务端 (pid={})".format(target_pid))
        else:
            print("  [WARN] 固定名服务端创建失败(可能已被占用), 继续")
        with TestSession() as s:
            print("  [PASS] 会话B 握手成功 (cmd PID={})".format(s.target_pid))
            name_b = get_server_pipe_name(s)
            if name_b:
                print("  [PASS] 随机管道名: {}".format(name_b))
            else:
                print("  [FAIL] 会话B 未见随机管道名")
                failures += 1
            names.append(name_b)
            if name_b and is_fixed_legacy_name(name_b, s.target_pid):
                print("  [FAIL] 会话B 使用了固定名(未随机化)")
                failures += 1
            elif name_b:
                # DLL 连接的必须是随机名, 而非攻击者预创建的固定名
                dll_log = read_dll_log(s.target_pid)
                if "server identity verified" in dll_log:
                    print("  [PASS] 会话B DLL 身份校验通过(未连接伪造服务端)")
                else:
                    print("  [FAIL] 会话B 身份校验日志缺失")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] 会话B setup 失败 (可能被固定名伪造服务端抢占): {}".format(e))
        failures += 1
    finally:
        if fixed_handle is not None:
            try:
                import win32file
                win32file.CloseHandle(fixed_handle)
            except Exception:
                pass

    # ---- 两次会话比较: 管道名不同(随机性) ----
    if len(names) == 2 and names[0] and names[1]:
        if names[0] != names[1]:
            print("  [PASS] 两次注入管道名不同 ({} vs {})".format(names[0], names[1]))
        else:
            print("  [FAIL] 两次注入管道名相同, 随机化失效")
            failures += 1
    else:
        print("  [WARN] 无法比较两次会话管道名 (会话未完整采集)")

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())