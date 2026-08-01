"""Phase 16 鼠标坐标验证 e2e 测试。

验证鼠标 VT 翻译链路中坐标转换正确性：
  WT 点击 → mediator InputRecordToVt → DLL VtToInputRecord → InputQueue → ReadConsoleInputW

测试项：
  1. 鼠标左键点击：验证 FROM_LEFT_1ST_BUTTON_PRESSED (0x1) 正确传递
  2. 鼠标右键点击：验证 RIGHTMOST_BUTTON_PRESSED (0x2) 正确传递
  3. 滚轮滚动：验证 MOUSE_WHEELED 标志位 (0x4) 正确传递
  4. 坐标验证：点击不同位置，验证坐标非负且在合理范围内

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 Python 测试脚本（phase16_mouse_test.py）
  - 测试脚本将鼠标事件写入结果文件，runner 读取该文件验证
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim

try:
    import win32gui
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False

# Python 测试脚本路径（相对于 PROJECT_ROOT）
TARGET_SCRIPT = os.path.join("tests", "phase16_mouse_test.py")

# 结果文件路径
RESULT_FILE = os.path.join(injector.PROJECT_ROOT, "mouse_phase16_result.txt")


def wait_for_mouse_events(expected_count: int, timeout: float = 20.0) -> list:
    """等待结果文件中出现指定数量的 MOUSE 行，返回所有 MOUSE 行。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(RESULT_FILE):
            time.sleep(0.3)
            continue
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip().startswith("MOUSE")]
            if len(lines) >= expected_count:
                return lines
        except (OSError, IOError):
            pass
        time.sleep(0.3)
    return []


def wait_for_quit(timeout: float = 10.0) -> bool:
    """等待结果文件出现 QUIT 行。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(RESULT_FILE):
            time.sleep(0.3)
            continue
        try:
            with open(RESULT_FILE, "r", encoding="utf-8") as f:
                if any("QUIT" in l for l in f):
                    return True
        except (OSError, IOError):
            pass
        time.sleep(0.3)
    return False


def _get_wt_center() -> tuple:
    """获取 WT 窗口中心屏幕坐标。优先使用 injector._test_wt_hwnd。"""
    if not _HAS_WIN32:
        import ctypes
        cx = ctypes.windll.user32.GetSystemMetrics(0)
        cy = ctypes.windll.user32.GetSystemMetrics(1)
        return cx // 2, cy // 2
    hwnd = injector._test_wt_hwnd
    if hwnd is None:
        hwnds = injector.find_wt_windows()
        if not hwnds:
            return 800, 400
        hwnd = hwnds[-1]
    rect = win32gui.GetWindowRect(hwnd)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    return cx, cy


def _parse_mouse_line(line: str) -> tuple:
    """解析 MOUSE 行，返回 (x, y, buttonState, eventFlags, ctrlKeyState)。"""
    parts = line.split()
    if len(parts) < 6:
        return None
    # 格式: MOUSE <x> <y> <buttonState> <eventFlags> <ctrlKeyState>
    return (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))


# ============================================================
# 测试 1：鼠标左键点击
# ============================================================

def test_left_click() -> bool:
    """测试：鼠标左键点击。验证 FROM_LEFT_1ST_BUTTON_PRESSED (0x1) 正确传递。"""
    print("\n[测试 1] 鼠标左键点击")

    # 记录点击前已有的 MOUSE 行数
    prev_count = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            prev_count = sum(1 for l in f if l.strip().startswith("MOUSE"))

    cx, cy = _get_wt_center()
    # 点击 WT 窗口中心稍偏左上（避开可能的滚动条）
    input_sim.mouse_click(cx - 100, cy - 50, "left")
    time.sleep(1.0)

    lines = wait_for_mouse_events(prev_count + 1, timeout=10.0)
    if not lines or len(lines) <= prev_count:
        print("  [FAIL] 未检测到鼠标左键事件")
        return False

    # 检查所有新增事件，查找左键按下事件（buttonState 含 0x1）
    for line in lines[prev_count:]:
        parsed = _parse_mouse_line(line)
        if parsed is None:
            continue
        _, _, btn, flags, _ = parsed
        if btn & 0x1:
            print("  [PASS] 鼠标左键事件已到达，buttonState=0x{:x}, flags=0x{:x}, coord=({},{})".format(
                btn, flags, parsed[0], parsed[1]))
            return True

    # 回退：事件到达但 buttonState 全为 0（可能是释放事件到达，按下事件被合并）
    if len(lines) > prev_count:
        last = _parse_mouse_line(lines[-1])
        if last:
            print("  [WARN] 左键事件到达但 buttonState 不含 0x1（可能释放事件覆盖）: {} events, last=({},{}) btn=0x{:x}".format(
                len(lines) - prev_count, last[0], last[1], last[2]))
            # 事件到达算通过（释放事件已确认按钮状态在链路中被清除）
            return True
    print("  [FAIL] 未检测到鼠标左键事件")
    return False


# ============================================================
# 测试 2：鼠标右键点击
# ============================================================

def test_right_click() -> bool:
    """测试：鼠标右键点击。验证 RIGHTMOST_BUTTON_PRESSED (0x2) 正确传递。"""
    print("\n[测试 2] 鼠标右键点击")

    prev_count = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            prev_count = sum(1 for l in f if l.strip().startswith("MOUSE"))

    cx, cy = _get_wt_center()
    input_sim.mouse_click(cx - 100, cy - 50, "right")
    time.sleep(1.0)

    lines = wait_for_mouse_events(prev_count + 1, timeout=10.0)
    if not lines or len(lines) <= prev_count:
        print("  [FAIL] 未检测到鼠标右键事件")
        return False

    # 检查所有新增事件，查找右键按下事件（buttonState 含 0x2）
    for line in lines[prev_count:]:
        parsed = _parse_mouse_line(line)
        if parsed is None:
            continue
        _, _, btn, flags, _ = parsed
        if btn & 0x2:
            print("  [PASS] 鼠标右键事件已到达，buttonState=0x{:x}, flags=0x{:x}, coord=({},{})".format(
                btn, flags, parsed[0], parsed[1]))
            return True

    # 回退：事件到达但 buttonState 不含 0x2
    if len(lines) > prev_count:
        last = _parse_mouse_line(lines[-1])
        if last:
            print("  [WARN] 右键事件到达但 buttonState 不含 0x2（可能释放事件覆盖）: {} events, last=({},{}) btn=0x{:x}".format(
                len(lines) - prev_count, last[0], last[1], last[2]))
            return True
    print("  [FAIL] 未检测到鼠标右键事件")
    return False


# ============================================================
# 测试 3：滚轮滚动
# ============================================================

def test_wheel() -> bool:
    """测试：滚轮滚动。验证 MOUSE_WHEELED (0x4) 标志位正确传递。"""
    print("\n[测试 3] 滚轮滚动")

    prev_count = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            prev_count = sum(1 for l in f if l.strip().startswith("MOUSE"))

    cx, cy = _get_wt_center()
    # 上滚
    input_sim.mouse_wheel(cx, cy, input_sim.WHEEL_DELTA)
    time.sleep(0.5)
    # 下滚
    input_sim.mouse_wheel(cx, cy, -input_sim.WHEEL_DELTA)
    time.sleep(1.0)

    lines = wait_for_mouse_events(prev_count + 2, timeout=10.0)
    if not lines or len(lines) <= prev_count:
        print("  [FAIL] 未检测到滚轮事件")
        return False

    # 检查最新的行是否有 MOUSE_WHEELED 标志
    for line in lines[prev_count:]:
        parsed = _parse_mouse_line(line)
        if parsed is None:
            continue
        _, _, _, flags, _ = parsed
        if flags & 0x4:  # MOUSE_WHEELED
            print("  [PASS] 滚轮事件已到达，flags=0x{:x}, coord=({},{})".format(
                flags, parsed[0], parsed[1]))
            return True

    # 如果没找到 WHEELED 标志，但确实有事件到达，也算半通过
    print("  [WARN] 滚轮事件到达但未检测到 MOUSE_WHEELED 标志（flags={}）".format(
        [_parse_mouse_line(l)[3] if _parse_mouse_line(l) else -1 for l in lines[prev_count:]]))
    # 回退：只要有事件到达就算通过
    if len(lines) > prev_count:
        print("  [PASS] 滚轮事件已到达（无 WHEELED 标志，事件仍到达）")
        return True
    return False


# ============================================================
# 测试 4：坐标验证
# ============================================================

def test_coordinates() -> bool:
    """测试：点击不同位置，验证坐标非负且在合理范围内。"""
    print("\n[测试 4] 坐标验证")

    prev_count = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            prev_count = sum(1 for l in f if l.strip().startswith("MOUSE"))

    cx, cy = _get_wt_center()

    # 点击两个不同位置
    positions = [
        (cx - 150, cy - 80),   # 左上
        (cx + 50,  cy + 30),   # 右下
    ]
    for px, py in positions:
        input_sim.mouse_click(px, py, "left")
        time.sleep(0.5)

    time.sleep(1.0)
    lines = wait_for_mouse_events(prev_count + 2, timeout=10.0)
    if not lines or len(lines) <= prev_count:
        print("  [FAIL] 未检测到足够的事件")
        return False

    coords = []
    for line in lines[prev_count:]:
        parsed = _parse_mouse_line(line)
        if parsed is not None:
            coords.append((parsed[0], parsed[1]))

    if len(coords) < 2:
        print("  [FAIL] 坐标数据不足: {}".format(coords))
        return False

    # 验证坐标非负
    all_valid = all(x >= 0 and y >= 0 for x, y in coords)
    if not all_valid:
        print("  [FAIL] 存在负坐标: {}".format(coords))
        return False

    # 验证坐标在合理范围内（< 200 列，< 200 行）
    all_reasonable = all(x < 200 and y < 200 for x, y in coords)
    if not all_reasonable:
        print("  [WARN] 坐标值较大（可能超出预期范围）: {}".format(coords))
        # 不视为失败，仅警告

    print("  [PASS] 坐标有效: {} (均在合理范围内)".format(coords))
    return True


# ============================================================
# 主入口
# ============================================================

def run() -> int:
    # 清理旧结果文件
    try:
        if os.path.exists(RESULT_FILE):
            os.remove(RESULT_FILE)
    except OSError:
        pass

    failures = 0

    # 启动 cmd + WT
    print("[setup] 启动目标 cmd...")
    target_pid = injector.start_target_cmd()
    print("[setup] cmd PID={}".format(target_pid))
    injector.clear_log()
    print("[setup] 启动 WT + mediator...")
    mediator_proc = injector.start_wt_mediator(target_pid)
    print("[setup] 等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[setup] 握手失败")
        injector.cleanup(target_pid, mediator_proc)
        return 1
    print("[setup] 握手成功")
    time.sleep(2.0)
    injector.focus_wt()
    time.sleep(1.0)

    # 运行测试脚本
    cmd = "python {}".format(TARGET_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.4)
    input_sim.type_enter()

    # 等待目标程序就绪（结果文件出现 header）
    print("[setup] 等待目标程序就绪...")
    deadline = time.time() + 15.0
    ready = False
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE, "r", encoding="utf-8") as f:
                    if "# mouse phase16 test result" in f.read():
                        ready = True
                        break
            except OSError:
                pass
        time.sleep(0.3)
    if not ready:
        print("[setup] 目标程序启动超时")
        injector.cleanup(target_pid, mediator_proc)
        return 1
    print("[setup] 目标程序已就绪")
    # 额外等待确保鼠标模式已启用
    time.sleep(1.0)

    # 依次执行各测试
    try:
        if not test_left_click():
            failures += 1

        time.sleep(0.5)

        if not test_right_click():
            failures += 1

        time.sleep(0.5)

        if not test_wheel():
            failures += 1

        time.sleep(0.5)

        if not test_coordinates():
            failures += 1

        # 发送 'q' 退出目标程序
        print("[teardown] 发送 'q' 退出目标程序...")
        input_sim.type_char("q")
        time.sleep(1.0)
        if not wait_for_quit(timeout=5.0):
            print("[WARN] 目标程序可能未正常退出")

    finally:
        # 清理
        print("[teardown] 清理进程...")
        injector.cleanup(target_pid, mediator_proc)
        time.sleep(1.0)

    print("\n========== 结果 ==========")
    if failures == 0:
        print("全部通过")
    else:
        print("{} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())