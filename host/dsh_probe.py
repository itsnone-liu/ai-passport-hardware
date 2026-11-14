"""Diagnostic: when exactly do our bytes arrive, and does the chip reboot after?

Prints every line with a timestamp relative to port open, and re-sends the command
on a schedule, so a missing reply can be attributed to either 'the write landed
during the boot window and was dropped' or 'the firmware ignored the command'.
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
arg = sys.argv[2] if len(sys.argv) > 2 else "@cmd.txt"
wait = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0

with open(arg[1:], "r", encoding="utf-8") as f:
    payload = "".join(line for line in f if line.strip())
if not payload.endswith("\n"):
    payload += "\n"

ser = serial.Serial(port, 115200, timeout=0.1)
t0 = time.time()


def stamp():
    return f"{time.time() - t0:6.2f}s"


sent_at = []
next_send = 0.0
deadline = t0 + wait
buf = b""
while time.time() < deadline:
    now = time.time() - t0
    # 0.5s, 3s, 6s, 9s: one of these lands after the CDC driver is installed
    if now >= next_send and len(sent_at) < 4:
        ser.write(payload.encode("utf-8"))
        ser.flush()
        sent_at.append(now)
        print(f"{stamp()}  >>> send #{len(sent_at)}", flush=True)
        next_send = next_send + 0.5 if not sent_at[1:] else now + 3.0
        next_send = [0.5, 3.0, 6.0, 9.0][len(sent_at)] if len(sent_at) < 4 else 1e9
    chunk = ser.read(4096)
    if not chunk:
        continue
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        tag = "JSON>" if text.startswith("{") else "LOG> "
        print(f"{stamp()}  {tag} {text}", flush=True)
ser.close()