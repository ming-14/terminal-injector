# -*- coding: utf-8 -*-
"""pe_info.py -- 打印 PE 的机器类型与导出表（探针工具）

用途：确认 32 位产物真的落在 32 位构建树下、导出名有没有被 C++ 修饰过
（RemoteCallExport 按裸名 GetProcAddress，被修饰就找不到）。

用法: python pe_info.py <file> [file ...]
"""
import os
import struct
import sys

MACHINE = {0x014C: "I386(32BIT)", 0x8664: "AMD64(64BIT)", 0xAA64: "ARM64"}
SUBSYS = {2: "GUI", 3: "CONSOLE"}


def parse(path):
    d = open(path, "rb").read()
    e = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e:e + 4] != b"PE\0\0":
        return None
    machine = struct.unpack_from("<H", d, e + 4)[0]
    nsec = struct.unpack_from("<H", d, e + 6)[0]
    opt_size = struct.unpack_from("<H", d, e + 20)[0]
    characteristics = struct.unpack_from("<H", d, e + 22)[0]
    opt = e + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    is64 = (magic == 0x20B)
    subsystem = struct.unpack_from("<H", d, opt + 68)[0]
    image_base = struct.unpack_from("<Q" if is64 else "<I", d, opt + (24 if is64 else 28))[0]

    secs = []
    s = opt + opt_size
    for i in range(nsec):
        o = s + i * 40
        vsz, va, rsz, ra = struct.unpack_from("<IIII", d, o + 8)
        secs.append((va, vsz, ra, rsz))

    def r2o(rva):
        for va, vsz, ra, rsz in secs:
            if va <= rva < va + max(vsz, rsz):
                return ra + (rva - va)
        return None

    dd_off = opt + (0x70 if is64 else 0x60)
    exp_rva = struct.unpack_from("<I", d, dd_off)[0]
    names = []
    if exp_rva:
        eo = r2o(exp_rva)
        nnames = struct.unpack_from("<I", d, eo + 24)[0]
        addr_names = struct.unpack_from("<I", d, eo + 32)[0]
        no = r2o(addr_names)
        for i in range(nnames):
            nr = struct.unpack_from("<I", d, no + i * 4)[0]
            o2 = r2o(nr)
            end = d.index(b"\0", o2)
            names.append(d[o2:end].decode("latin1"))

    return {
        "machine": MACHINE.get(machine, hex(machine)),
        "subsystem": SUBSYS.get(subsystem, str(subsystem)),
        "kind": "DLL" if (characteristics & 0x2000) else "EXE",
        "image_base": image_base,
        "exports": sorted(names),
    }


def main(argv):
    for p in argv[1:]:
        info = parse(p)
        print("== %s" % os.path.basename(p))
        if info is None:
            print("   不是 PE 文件")
            continue
        print("   机器=%s 类型=%s 子系统=%s ImageBase=0x%X"
              % (info["machine"], info["kind"], info["subsystem"], info["image_base"]))
        if info["exports"]:
            for n in info["exports"]:
                print("   export: %s" % n)
        else:
            print("   (无导出)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
