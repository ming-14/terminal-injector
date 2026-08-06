"""Start cmd + mediator + adplus crash monitoring.

adplus attaches to cmd in crash mode, captures a dump when cmd crashes.
"""
import os
import subprocess
import sys
import time
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# Clean old logs
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
ADPLUS = os.path.join(paths.cdb_tools(), "adplus.exe")
DUMP_DIR = os.path.join(paths.dump_dir(), "cmd_dumps")
PID_FILE = os.path.join(paths.out_dir(), "debug_pids.txt")

try:
    os.makedirs(DUMP_DIR, exist_ok=True)
except OSError:
    pass

# Clean old dumps
for f in glob.glob(os.path.join(DUMP_DIR, "*")):
    try:
        os.remove(f)
    except OSError:
        pass

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
print("cmd PID = {}".format(target_pid))

# Start WT mediator
wt_cmd = [WT_EXE, "--", MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)]
proc_wt = subprocess.Popen(wt_cmd)
print("WT/mediator PID = {}".format(proc_wt.pid))

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
        except OSError:
            pass
    time.sleep(0.3)

print("handshake_ok = {}".format(handshake_ok))
time.sleep(3.0)

# Write PIDs
with open(PID_FILE, "w") as f:
    f.write("cmd_pid={}\n".format(target_pid))
    f.write("wt_pid={}\n".format(proc_wt.pid))
    f.write("handshake_ok={}\n".format(handshake_ok))
    f.flush()

# Keep running
print("Keeping processes alive. Kill them manually when done.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
