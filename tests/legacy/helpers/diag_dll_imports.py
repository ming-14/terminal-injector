"""分析 injected.dll 的导入表，列出依赖的 DLL。

用于 Phase 11 卸载诊断：
  LoadCount 初始值 5，怀疑和依赖关系有关。
  如果某 DLL 静态导入 injected.dll，会保持 LoadCount > 0。
  如果 injected.dll 依赖其他 DLL，加载器可能对每个依赖增加 LoadCount。

用法：python tests\helpers\diag_dll_imports.py [dll_path]
"""
import os
import sys

try:
    import pefile
except ImportError:
    print("请先安装 pefile: pip install pefile")
    sys.exit(1)


def analyze_imports(dll_path):
    print("[分析] {}".format(dll_path), flush=True)
    if not os.path.exists(dll_path):
        print("  文件不存在", flush=True)
        return

    pe = pefile.PE(dll_path, fast_load=False)
    print("\n[PE 基本信息]", flush=True)
    print("  Machine        = {:#x} ({})".format(
        pe.FILE_HEADER.Machine,
        "x64" if pe.FILE_HEADER.Machine == 0x8664 else "x86" if pe.FILE_HEADER.Machine == 0x14c else "?"),
        flush=True)
    print("  TimeDateStamp  = {:#x}".format(pe.FILE_HEADER.TimeDateStamp), flush=True)
    print("  SizeOfImage    = {:#x}".format(pe.OPTIONAL_HEADER.SizeOfImage), flush=True)

    # 导入表
    print("\n[导入表 - injected.dll 依赖的 DLL]", flush=True)
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            dll_name = entry.dll.decode("ascii", errors="replace")
            # 统计导入函数数
            func_count = len(entry.imports)
            print("  {} ({} 个函数)".format(dll_name, func_count), flush=True)
            # 列出前 5 个函数（调试用）
            for imp in entry.imports[:5]:
                if imp.name:
                    print("    - {}".format(imp.name.decode("ascii", errors="replace")),
                          flush=True)
                else:
                    print("    - ordinal {}".format(imp.ordinal), flush=True)
            if func_count > 5:
                print("    ... (共 {} 个)".format(func_count), flush=True)
    else:
        print("  无导入表", flush=True)

    # 导出表（看 injected.dll 导出哪些函数）
    print("\n[导出表 - injected.dll 导出的函数]", flush=True)
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            name = exp.name.decode("ascii", errors="replace") if exp.name else "(ordinal {})".format(exp.ordinal)
            print("  {}".format(name), flush=True)
    else:
        print("  无导出表", flush=True)

    # 检查是否有 Delay Load 目录
    print("\n[延迟加载目录]", flush=True)
    if hasattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_DELAY_IMPORT:
            dll_name = entry.dll.decode("ascii", errors="replace")
            print("  {} (delay-load)".format(dll_name), flush=True)
    else:
        print("  无延迟加载", flush=True)

    # 检查是否有 Load Config
    print("\n[Load Config 目录]", flush=True)
    if hasattr(pe, "DIRECTORY_ENTRY_LOAD_CONFIG"):
        lc = pe.DIRECTORY_ENTRY_LOAD_CONFIG.struct
        print("  Size           = {:#x}".format(lc.Size), flush=True)
        print("  SecurityCookie = {:#x}".format(lc.SecurityCookie), flush=True)
    else:
        print("  无 Load Config", flush=True)

    pe.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        dll_path = sys.argv[1]
    else:
        # 默认 injected.dll
        dll_path = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release\injected.dll"
    analyze_imports(dll_path)
