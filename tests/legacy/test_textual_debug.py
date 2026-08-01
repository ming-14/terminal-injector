"""Debug version of textual mouse test."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "helpers"))
from injector import (
    start_target_cmd, start_wt_mediator, clear_log, wait_for_handshake,
    focus_wt, cleanup, LOG_PATH, BUILD_BIN
)

target_pid = start_target_cmd()
print(f"PID={target_pid}")

clear_log()
print("log cleared")

print("starting mediator...")
t0 = time.time()
mediator_proc = start_wt_mediator(target_pid)
print(f"mediator started in {time.time()-t0:.1f}s")

print("waiting for handshake...")
t0 = time.time()
ok = wait_for_handshake(timeout=20)
print(f"handshake result={ok} in {time.time()-t0:.1f}s")

if ok:
    print("focusing WT...")
    hwnd = focus_wt()
    print(f"hwnd=0x{hwnd:08x}" if hwnd else "hwnd=None")
    
    print("typing command...")
    import input_sim as sim
    sim.type_text("cd C:\\Users\\rikka\\Desktop\\terminal-injector")
    sim.type_enter()
    time.sleep(1)
    sim.type_text("python ..\\textual_demo.py")
    sim.type_enter()
    print("waiting 15s for Textual...")
    time.sleep(15)
    
    log = get_log()
    print(f"log size={len(log)}")
    for line in log.split("\n"):
        l = line.strip()
        if not l: continue
        if "ModeSwitchNotify" in l or "ModeChange" in l or "mouse" in l.lower() or "MOUSE" in l:
            print(f"  {l}")

print("\nCtrl+C to exit")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    cleanup(target_pid, mediator_proc)