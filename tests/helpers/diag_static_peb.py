"""静态 PEB Ldr 诊断：对已存在的 PID 读 injected.dll 的 Ldr Entry + DdagNode 字节。

不启动新 cmd/WT，避免影响其他 WT 进程。
用于 Phase 11 卸载问题排查：dump 卸载后（DLL 仍驻留）的 _LDR_DDAG_NODE 字节。

用法：python tests\helpers\diag_static_peb.py <pid>
"""
import ctypes
import sys
import os
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(__file__))
from diag_peb_loadcount import (  # noqa: E402
    find_injected_loadcount,
    hex_dump,
)


def main():
    if len(sys.argv) < 2:
        print("用法: python diag_static_peb.py <pid>")
        sys.exit(1)
    pid = int(sys.argv[1])
    print("[static] 对 PID={} 做 PEB Ldr 静态诊断".format(pid), flush=True)

    # 用 cdb 查 _LDR_DDAG_NODE 结构定义（需要 ntdll 符号）
    # 先用 ReadProcessMemory 读字节
    result = find_injected_loadcount(pid, dump_bytes=True)
    if not result:
        print("  未找到 injected.dll（可能已卸载或 PID 无效）", flush=True)
        sys.exit(0)

    print("\n[injected.dll Ldr Entry]", flush=True)
    print("  base_dll   = {}".format(result["base_dll"]), flush=True)
    print("  full_dll   = {}".format(result["full_dll"]), flush=True)
    print("  dll_base   = {:#x}".format(result["dll_base"]), flush=True)
    print("  size       = {:#x}".format(result["size"]), flush=True)
    print("  flags      = {:#x}".format(result["flags"]), flush=True)
    print("  obs_loadcount = {} (ObsoleteLoadCount, Win10+ 通常 0xffff)".format(
        result["obs_loadcount"]), flush=True)
    print("  entry_addr = {:#x}".format(result["entry_addr"]), flush=True)
    print("  ddag       = {:#x}".format(result["ddag"]), flush=True)
    print("  loadcount(@ddag+0x18) = {}".format(result["loadcount"]), flush=True)

    print("\n[_LDR_DATA_TABLE_ENTRY dump (前 0xA0 字节)]", flush=True)
    hex_dump(result["entry_dump"], result["entry_addr"])

    print("\n[_LDR_DDAG_NODE dump (前 0x40 字节)]", flush=True)
    hex_dump(result["ddag_dump"], result["ddag"])

    # 关键字段标注
    print("\n[关键字段解析]", flush=True)
    ed = result["entry_dump"]
    dd = result["ddag_dump"]
    print("  Entry+0x30 DllBase         = {:#x}".format(
        int.from_bytes(ed[0x30:0x38], "little")), flush=True)
    print("  Entry+0x40 SizeOfImage     = {:#x}".format(
        int.from_bytes(ed[0x40:0x44], "little")), flush=True)
    print("  Entry+0x68 Flags           = {:#x}".format(
        int.from_bytes(ed[0x68:0x6c], "little")), flush=True)
    print("  Entry+0x6c ObsoleteLoadCount = {}".format(
        int.from_bytes(ed[0x6c:0x6e], "little")), flush=True)
    print("  Entry+0x98 DdagNode        = {:#x}".format(
        int.from_bytes(ed[0x98:0xa0], "little")), flush=True)
    print("  DDAG+0x00 Modules.Flink    = {:#x}".format(
        int.from_bytes(dd[0x00:0x08], "little")), flush=True)
    print("  DDAG+0x08 Modules.Blink    = {:#x}".format(
        int.from_bytes(dd[0x08:0x10], "little")), flush=True)
    print("  DDAG+0x10 ServiceTagList   = {:#x}".format(
        int.from_bytes(dd[0x10:0x18], "little")), flush=True)
    print("  DDAG+0x18 LoadCount        = {}".format(
        int.from_bytes(dd[0x18:0x1c], "little")), flush=True)
    print("  DDAG+0x1c LoadWhileUnloadingCount = {}".format(
        int.from_bytes(dd[0x1c:0x20], "little")), flush=True)
    print("  DDAG+0x20 LowestLink       = {}".format(
        int.from_bytes(dd[0x20:0x24], "little")), flush=True)
    print("  DDAG+0x24 (padding/next)   = {:#x}".format(
        int.from_bytes(dd[0x24:0x28], "little")), flush=True)
    print("  DDAG+0x28 ???              = {:#x}".format(
        int.from_bytes(dd[0x28:0x2c], "little")), flush=True)
    print("  DDAG+0x2c ???              = {:#x}".format(
        int.from_bytes(dd[0x2c:0x30], "little")), flush=True)


if __name__ == "__main__":
    main()
