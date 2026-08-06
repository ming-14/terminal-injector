"""分析 dump 文件中 injected.dll 偏移 0x623e5 处的代码。

用 cdb 加载 dump，反汇编崩溃地址附近的代码，并查看最近的符号。
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

CDB = paths.cdb_exe()
# PDB 路径（在 build/bin/Release/）
PDB_DIR = paths.build_bin()
SYMPATH = paths.symbol_path()


def main():
    if len(sys.argv) < 3:
        print("用法: python analyze_crash.py <dump_path> <output_log>")
        sys.exit(1)
    dump = sys.argv[1]
    out_log = sys.argv[2]

    # cdb 命令：
    #   lm m injected*           查看 injected.dll 模块基址
    #   .reload /f injected.dll  强制加载符号
    #   ln injected.dll+0x623e5  查看最近的符号
    #   u injected.dll+0x623a0 injected.dll+0x62430  反汇编崩溃地址附近（前 0x45 后 0x4b）
    cmds = (
        "lm m injected*; "
        ".reload /f injected.dll; "
        "ln injected.dll+0x623e5; "
        "u injected.dll+0x623a0 injected.dll+0x62430; "
        "q"
    )
    cmd = [CDB, "-y", SYMPATH, "-z", dump, "-c", cmds]
    print("[run] cmd: {}".format(" ".join(cmd)))
    print("[run] output: {}".format(out_log))

    with open(out_log, "w", encoding="utf-8", errors="ignore") as f:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, encoding="utf-8", errors="ignore")
        for line in proc.stdout:
            f.write(line)
            print(line, end="")
        proc.wait()
    print("[done] exit_code={}".format(proc.returncode))


if __name__ == "__main__":
    main()
