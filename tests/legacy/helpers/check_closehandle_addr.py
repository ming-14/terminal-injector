"""验证 kernel32!CloseHandle 与 kernelbase!CloseHandle 的地址关系。

在普通 python（无注入）中运行，检查两者是否为同一地址（forwarder）。
"""
import ctypes

k32 = ctypes.windll.kernel32
kbase = ctypes.windll.kernelbase

# GetProcAddress 返回导出函数地址
# 若 kernel32!CloseHandle 是 forwarder 到 kernelbase!CloseHandle，
# GetProcAddress(kernel32, "CloseHandle") 返回 kernelbase!CloseHandle 地址
addr_k32 = ctypes.cast(k32.CloseHandle, ctypes.c_void_p).value
addr_kbase = ctypes.cast(kbase.CloseHandle, ctypes.c_void_p).value

print("kernel32!CloseHandle   = {:#018x}".format(addr_k32))
print("kernelbase!CloseHandle = {:#018x}".format(addr_kbase))
print("same? {}".format(addr_k32 == addr_kbase))
print("diff = {:#x}".format(abs(addr_k32 - addr_kbase)))

# 反汇编前 16 字节
import ctypes.wintypes as wt

MEM_COMMIT = 0x1000
PAGE_READWRITE = 0x4

VirtualProtect = k32.VirtualProtect
VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wt.DWORD,
                           ctypes.POINTER(wt.DWORD)]
VirtualProtect.restype = wt.BOOL

for name, addr in [("kernel32", addr_k32), ("kernelbase", addr_kbase)]:
    buf = (ctypes.c_ubyte * 16)()
    old = wt.DWORD(0)
    if VirtualProtect(addr, 16, 0x10, ctypes.byref(old)):  # PAGE_EXECUTE
        ctypes.memmove(buf, addr, 16)
        VirtualProtect(addr, 16, old.value, ctypes.byref(old))
        hex_bytes = " ".join("{:02X}".format(b) for b in buf)
        print("{}!CloseHandle first 16 bytes: {}".format(name, hex_bytes))
    else:
        print("{}!CloseHandle VirtualProtect failed err={}".format(
            name, k32.GetLastError()))
