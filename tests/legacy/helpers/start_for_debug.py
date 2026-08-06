"""Start cmd + mediator, print PIDs, and keep running for cdb attach.

Unlike debug_start.py, this script writes PIDs to a file immediately so
the caller can read them even if stdout is buffered.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# Clean old logs
import glob
for f in glob.glob(paths.injected_log_glob()):
    try:
        os.remove(f)
    except OSError:
        pass

PROJECT_ROOT = paths.project_root()
BUILD_BIN = paths.build_bin()
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
DLL_PATH = os.path.join(BUILD_BIN, "injected.dll")
WT_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                      "Microsoft", "WindowsApps", "wt.exe")
LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")
PID_FILE = os.path.join(paths.out_dir(), "debug_pids.txt")

# Clean mediator log
try:
    os.remove(LOG_PATH)
except OSError:
    pass

# Start target cmd
proc_cmd = subprocess.Popen(
    ["cmd.exe"],
    creationflags=subprocess.CREATE_NEW_CONSOLE,
    cwd=PROJECT_ROOT,
)
target_pid = proc_cmd.pid

# Start WT mediator
wt_cmd = [WT_EXE, "--", MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)]
proc_wt = subprocess.Popen(wt_cmd)

# Write PIDs to file immediately
with open(PID_FILE, "w") as f:
    f.write("cmd_pid={}\n".format(target_pid))
    f.write("wt_pid={}\n".format(proc_wt.pid))
    f.write("mediator_exe_pid={}\n".format(proc_wt.pid))
    f.flush()

# Wait for handshake
deadline = time.time() + 20.0
handshake_ok = False
while time.time() < deadline:
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "Handshake OK" in content:
                handshake_ok = True
                break
            if "Handshake failed" in content or "ERROR" in content:
                break
        except OSError:
            pass
    time.sleep(0.3)

# Wait extra 5s for LazyInit to complete
time.sleep(5.0)

# Check DLL log
dll_log = paths.injected_log(target_pid)
dll_log_size = 0
if os.path.exists(dll_log):
    dll_log_size = os.path.getsize(dll_log)

# Append status to PID file
with open(PID_FILE, "a") as f:
    f.write("handshake_ok={}\n".format(handshake_ok))
    f.write("dll_log={}\n".format(dll_log))
    f.write("dll_log_size={}\n".format(dll_log_size))
    f.flush()

# Keep running - do NOT cleanup
# Caller will kill processes after debugging
print("STARTED cmd_pid={} wt_pid={} handshake={} dll_log_size={}".format(
    target_pid, proc_wt.pid, handshake_ok, dll_log_size))
print("PID file: {}".format(PID_FILE))
print("Keeping processes alive. Kill them manually when done.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
