#!/usr/bin/env python3
"""快速 cdb 附加脚本：抓取 cmd 进程线程栈，定位卡死位置。
避免 ?? 查询匿名命名空间变量失败导致 qd 不执行。"""
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

TOOLS = paths.cdb_tools()
CDB = os.path.join(TOOLS, "cdb.exe")
SYM = paths.symbol_path()
EXE_PATH = paths.build_bin()

def main():
    if len(sys.argv) < 2:
        print("Usage: python cdb_quick.py <pid>")
        sys.exit(1)
    pid = sys.argv[1]
    out_file = os.path.join(EXE_PATH, f"cdb_quick_{pid}.txt")

    # 命令：列线程 + 全栈 + 分离（qd）
    # 不查匿名命名空间变量，避免符号解析失败导致 qd 不执行
    cmd_str = (
        f'"{CDB}" -p {pid} '
        f'-y "{SYM};{EXE_PATH}" '
        f'-i "{EXE_PATH}" '
        f'-logo "{out_file}" '
        f'-c "~;~*kp;qd"'
    )
    print(f"Attaching to pid={pid}, output: {out_file}")
    print(f"Command: {cmd_str}")

    try:
        result = subprocess.run(cmd_str, shell=True, timeout=30,
                              capture_output=True, text=True)
        print(f"Exit code: {result.returncode}")
        if result.stdout:
            print("STDOUT:", result.stdout[:500])
        if result.stderr:
            print("STDERR:", result.stderr[:500])
    except subprocess.TimeoutExpired:
        print("TIMEOUT: cdb did not finish in 30s, killing")
    except Exception as e:
        print(f"Error: {e}")

    # 读取输出文件
    if os.path.exists(out_file):
        with open(out_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        print(f"\n=== Output file ({len(content)} bytes) ===")
        # 只打印关键部分
        lines = content.split('\n')
        for i, line in enumerate(lines):
            if any(kw in line for kw in ['injected!', 'Lock', 'Wait', 'Sleep',
                                          'CloseHandle', 'LazyInit', 'Logger',
                                          'DbgBreak', 'LdrInitialize']):
                print(f"  L{i}: {line}")

if __name__ == '__main__':
    main()
