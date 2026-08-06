#!/usr/bin/env python3
"""cdb 反汇编脚本：反汇编 CloseHandle_Detour，确认 +0xe 对应什么指令。"""
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
        print("Usage: python cdb_disasm.py <pid>")
        sys.exit(1)
    pid = sys.argv[1]
    out_file = os.path.join(EXE_PATH, f"cdb_disasm_{pid}.txt")

    # 反汇编 CloseHandle_Detour 前 20 字节
    # 用 ln 找符号地址，用 ub 反汇编
    cmd_str = (
        f'"{CDB}" -p {pid} '
        f'-y "{SYM};{EXE_PATH}" '
        f'-i "{EXE_PATH}" '
        f'-logo "{out_file}" '
        f'-c "ln terminjector!terminjector::hooks::CloseHandle_Detour;'
        f'u terminjector!terminjector::hooks::CloseHandle_Detour L20;'
        f'~;~*k;qd"'
    )
    print(f"Attaching to pid={pid}")
    try:
        result = subprocess.run(cmd_str, shell=True, timeout=30,
                              capture_output=True, text=True)
        print(f"Exit code: {result.returncode}")
    except subprocess.TimeoutExpired:
        print("TIMEOUT")

    if os.path.exists(out_file):
        with open(out_file, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        # 打印 CloseHandle_Detour 反汇编部分
        lines = content.split('\n')
        in_disasm = False
        for line in lines:
            if 'CloseHandle_Detour' in line and ('+' in line or 'ln' in line):
                in_disasm = True
            if in_disasm or 'CloseHandle_Detour' in line:
                print(line)
            if in_disasm and line.strip() == '':
                in_disasm = False
        # 打印线程栈关键部分
        print("\n=== Thread stacks ===")
        for line in lines:
            if any(kw in line for kw in ['injected!', 'Child-SP', 'Id:']):
                print(line)

if __name__ == '__main__':
    main()
