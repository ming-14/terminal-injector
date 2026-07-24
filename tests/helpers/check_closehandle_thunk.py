"""验证 kernel32!CloseHandle 的 import thunk 跳转目标。

kernel32!CloseHandle 第一条指令是 FF 25 xx xx xx xx (jmp qword ptr [rip+disp32])
读取 import 表项的值，看它指向哪个地址。
"""
import ctypes
import ctypes.wintypes as wt

k32 = ctypes.windll.kernel32
kbase = ctypes.windll.kernelbase

# 显式设置 argtypes，避免 64 位地址 OverflowError
VirtualProtect = k32.VirtualProtect
VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                           ctypes.POINTER(wt.DWORD)]
VirtualProtect.restype = wt.BOOL

addr_k32 = ctypes.cast(k32.CloseHandle, ctypes.c_void_p).value
addr_kbase = ctypes.cast(kbase.CloseHandle, ctypes.c_void_p).value

print("kernel32!CloseHandle   = {:#018x}".format(addr_k32))
print("kernelbase!CloseHandle = {:#018x}".format(addr_kbase))

# 读取 kernel32!CloseHandle 前 6 字节
buf = (ctypes.c_ubyte * 6)()
old = wt.DWORD(0)
VirtualProtect(addr_k32, 6, 0x10, ctypes.byref(old))
ctypes.memmove(buf, addr_k32, 6)
VirtualProtect(addr_k32, 6, old.value, ctypes.byref(old))
print("kernel32!CloseHandle first 6 bytes: {}".format(
    " ".join("{:02X}".format(b) for b in buf)))

# 解析 FF 25 disp32
# FF 25 = jmp qword ptr [rip+disp32]
# rip = addr + 6 (下一条指令地址)
# target_mem = rip + disp32
if buf[0] == 0xFF and buf[1] == 0x25:
    disp = ctypes.c_int32.from_buffer(
        (ctypes.c_ubyte * 4)(*buf[2:6])).value
    rip = addr_k32 + 6
    target_mem = rip + disp
    print("disp32 = {:#x}".format(disp))
    print("rip (next instr) = {:#018x}".format(rip))
    print("target memory addr = {:#018x}".format(target_mem))

    # 读取 target_mem 处的 8 字节（指针）
    ptr_buf = (ctypes.c_ubyte * 8)()
    VirtualProtect(target_mem, 8, 0x10, ctypes.byref(old))
    ctypes.memmove(ptr_buf, target_mem, 8)
    VirtualProtect(target_mem, 8, old.value, ctypes.byref(old))
    ptr_val = ctypes.c_uint64.from_buffer(
        (ctypes.c_ubyte * 8)(*ptr_buf)).value
    print("import table entry value = {:#018x}".format(ptr_val))
    print("same as kernelbase!CloseHandle? {}".format(ptr_val == addr_kbase))

    # 反汇编目标地址前 16 字节
    target_buf = (ctypes.c_ubyte * 16)()
    VirtualProtect(ptr_val, 16, 0x10, ctypes.byref(old))
    ctypes.memmove(target_buf, ptr_val, 16)
    VirtualProtect(ptr_val, 16, old.value, ctypes.byref(old))
    print("target addr first 16 bytes: {}".format(
        " ".join("{:02X}".format(b) for b in target_buf)))
else:
    print("kernel32!CloseHandle is NOT an import thunk")
    print("first 2 bytes: {:02X} {:02X}".format(buf[0], buf[1]))
