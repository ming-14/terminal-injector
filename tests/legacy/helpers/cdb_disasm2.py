#!/usr/bin/env python3
"""cdb 简单反汇编：用 x 找符号，u 反汇编。"""
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
        print("Usage: python cdb_disasm2.py <pid>")
        sys.exit(1)
    pid = sys.argv[1]
    out_file = os.path.join(EXE_PATH, f"cdb_disasm2_{pid}.txt")

    # 用 x 搜索符号，u 反汇编
    cmd_str = (
        f'"{CDB}" -p {pid} '
        f'-y "{SYM};{EXE_PATH}" '
        f'-i "{EXE_PATH}" '
        f'-logo "{out_file}" '
        f'-c ".symopt- 100;x injected!*CloseHandle*;u injected!terminjector::hooks::CloseHandle_Detour L30;~;~*kp;qd"'
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
        print(content[:5000])

if __name__ == '__main__':
    main()
