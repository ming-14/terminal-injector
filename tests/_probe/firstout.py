import os, time, sys
_STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_t0.txt")
open(_STAMP, "w").write(repr(time.time()))
print("FIRST_OUTPUT")
time.sleep(20)
