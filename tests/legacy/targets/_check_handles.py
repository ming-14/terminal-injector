import ctypes
k32 = ctypes.windll.kernel32
kbase = ctypes.windll.LoadLibrary('kernelbase.dll')
for name in ['AllocConsole', 'AttachConsole', 'FreeConsole', 'CloseHandle']:
    h_k32 = ctypes.cast(getattr(k32, name), ctypes.c_void_p).value
    h_kbase = ctypes.cast(getattr(kbase, name), ctypes.c_void_p).value
    print(name, 'k32=', hex(h_k32), 'kbase=', hex(h_kbase), 'same=', h_k32 == h_kbase)
