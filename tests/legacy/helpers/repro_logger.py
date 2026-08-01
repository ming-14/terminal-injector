"""Minimal repro: load injected.dll and check if Logger writes to file.

This isolates whether the Logger worker can actually write to the log file
when the DLL is loaded outside of the full injection chain.
"""
import ctypes
import os
import sys
import time

DLL_PATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release\injected.dll"

# Clean old logs
for f in [r"C:\temp\injected_repro.log"]:
    try:
        os.remove(f)
    except OSError:
        pass

print("[1] Loading injected.dll...")
try:
    dll = ctypes.CDLL(DLL_PATH)
    print("    Loaded. Handle={:#x}".format(dll._handle))
except Exception as e:
    print("    FAILED: {}".format(e))
    sys.exit(1)

# Check if g_probe_dllmain was incremented (DllMain ran)
try:
    probe = ctypes.c_long.in_dll(dll, "g_probe_dllmain")
    print("    g_probe_dllmain = {}".format(probe.value))
except Exception as e:
    print("    g_probe_dllmain not found: {}".format(e))

# Check loaded modules
import subprocess
pid = os.getpid()
print("[2] PID={}, checking loaded DLLs...".format(pid))
r = subprocess.run(
    ["powershell", "-Command",
     "(Get-Process -Id {}).Modules | Where-Object {{ $_.ModuleName -like 'injected*' -or $_.ModuleName -like 'MinHook*' }} | Select-Object ModuleName,FileName | Format-Table -AutoSize".format(pid)],
    capture_output=True, text=True
)
print(r.stdout)
if r.stderr:
    print("STDERR:", r.stderr)

# Trigger LazyInit by calling a Console API (WriteConsoleW)
print("[3] Triggering LazyInit via WriteConsoleW...")
kernel32 = ctypes.windll.kernel32
STD_OUTPUT_HANDLE = 0xFFFFFFF5
h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
print("    stdout handle = {:#x}".format(h_out if h_out else 0))

# Write something to trigger WriteConsoleW Detour
msg = "hello from repro\n"
written = ctypes.c_ulong(0)
ret = kernel32.WriteConsoleW(h_out, msg, len(msg), ctypes.byref(written), None)
print("    WriteConsoleW ret={} written={}".format(ret, written.value))

# Wait for async Logger to flush
print("[4] Waiting 3s for Logger worker to flush...")
time.sleep(3)

# Check log files
print("[5] Checking log files...")
for f in [r"C:\temp\injected_{}.log".format(pid), r"C:\temp\injected_repro.log"]:
    if os.path.exists(f):
        size = os.path.getsize(f)
        print("    {}: {} bytes".format(f, size))
        if size > 0:
            with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                for i, line in enumerate(fp):
                    if i >= 20:
                        print("    ... (truncated)")
                        break
                    print("    {}".format(line.rstrip()))
    else:
        print("    {}: NOT FOUND".format(f))

print("[6] Done. Press Enter to exit (DLL will unload).")
input()
