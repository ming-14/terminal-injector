"""运行 cdb 分析 dump 文件，输出所有线程 callstack 到日志文件。

用法：python tests/helpers/run_cdb_dump.py <dump_path> <output_log>
"""
import os
import sys
import subprocess

PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
CDB = os.path.join(PROJECT_ROOT, ".agents", "skills", "windows-debugging",
                    "10.0.19041.5609", "cdb.exe")
SYMPATH = "srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols;C:\\UserProgram\\py31011;C:\\Program Files\\Python311"


def main():
    if len(sys.argv) < 3:
        print("用法: python run_cdb_dump.py <dump_path> <output_log>")
        sys.exit(1)
    dump = sys.argv[1]
    out_log = sys.argv[2]

    # cdb 命令：
    #   ~* kn 50      所有线程 callstack（带行号）
    #   !analyze -v   自动分析
    #   lm m injected*  检查 injected.dll 是否加载
    #   q             退出
    cmds = "~* kn 50; !analyze -v; lm m injected*; q"
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
