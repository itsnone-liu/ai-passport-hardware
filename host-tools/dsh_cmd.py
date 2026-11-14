"""Send protocol lines to the Passport and print the replies.

Opens the port, waits for the device's boot banner ({"event":"ready",...}) before
writing, then prints every reply line for a fixed window. Waiting for ready
matters: the USB-Serial/JTAG CDC driver installs ~1.5 s after reset, and anything
written before that is silently discarded by the ROM loader, which makes a
perfectly good command look like a firmware bug.

    python dsh_cmd.py COM3 @cmd.txt [listen_seconds]
"""
import sys
import time

import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
arg = sys.argv[2] if len(sys.argv) > 2 else "@cmd.txt"
wait = float(sys.argv[3]) if len(sys.argv) > 3 else 8.0

if arg.startswith("@"):
    with open(arg[1:], "r", encoding="utf-8") as f:
        payload = "".join(line for line in f if line.strip())
else:
    payload = arg

ser = serial.Serial(port, 115200, timeout=0.2)
# 不要碰 DTR/RTS：在 ESP32-C3 的 USB-Serial/JTAG 上它们既会触发复位，也会让 CDC 端
# 把写入的数据丢掉——表现为"命令发出去了但设备毫无反应"，极难归因。
time.sleep(0.3)


def read_for(seconds, until_ready=False):
    """Collect bytes for a while; optionally stop as soon as 'ready' shows up."""
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        chunk = ser.read(4096)
        if not chunk:
            continue
        buf += chunk
        if until_ready and b'"event":"ready"' in buf:
            return buf, True
    return buf, False


# Wait for the boot banner so the command cannot fall into the pre-CDC window.
_, ok = read_for(12.0, until_ready=True)
print(f"[cmd] device ready: {'yes' if ok else 'timeout, sending anyway'}", flush=True)
time.sleep(0.3)

ser.write(payload.encode("utf-8"))
ser.flush()

buf, _ = read_for(wait)
text = buf.decode("utf-8", "replace")
for line in text.splitlines():
    line = line.strip()
    if line.startswith("{"):
        print("JSON> " + line)
    elif line:
        print("LOG>  " + line)
ser.close()