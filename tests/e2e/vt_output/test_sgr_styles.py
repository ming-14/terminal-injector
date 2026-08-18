"""特性: SGR 样式（粗体/斜体/下划线/闪烁/反显/隐藏/删除线/重置）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - 粗体 1、斜体 3、下划线 4、闪烁 5、反显 7、隐藏 8、重置 0
    共 7 个序列原样到达日志（重置 0 附带验证 0 序列本身）
  - 删除线 9：断言缺席（2026-08-17 修复）。ConHost 的 16 位属性字没有
    删除线位，实测忽略 ESC[9m；DLL 直通入口按 ConHost 实际渲染模型剥离
    SGR 9/29（VtSgrFilter），否则 WT 渲染删除线空格为横线（vim 欢迎页
    标题变 -------- bug）。镜像必须忠实于目标控制台。
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[1m", "LOG_STYLE_BOLD"),      # 粗体
    (b"\x1b[3m", "LOG_STYLE_ITALIC"),    # 斜体
    (b"\x1b[4m", "LOG_STYLE_UNDERLINE"), # 下划线
    (b"\x1b[5m", "LOG_STYLE_BLINK"),     # 闪烁
    (b"\x1b[7m", "LOG_STYLE_REVERSE"),   # 反显
    (b"\x1b[8m", "LOG_STYLE_HIDDEN"),    # 隐藏
    (b"\x1b[9m", "LOG_STYLE_STRIKE", "absent"),  # 删除线：ConHost 无此属性位，DLL 入口剥离
    (b"\x1b[0m", "LOG_STYLE_RESET"),     # 重置
]


def run() -> int:
    return run_vt_byte_test("sgr_styles", SEQS)


if __name__ == "__main__":
    sys.exit(run())
