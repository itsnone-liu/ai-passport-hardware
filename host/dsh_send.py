# Send one JSON line to the dsh terminal over USB CDC and print whatever comes back.
# usage: python dsh_send.py COM3 @cmd.txt [seconds]   (file holds the raw JSON line)
#        python dsh_send.py COM3 '{"id":1,...}' [seconds]
import sys, time
import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM3"
arg = sys.argv[2] if len(sys.argv) > 2 else '{"id":1,"cmd":"get_status"}'
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0

if arg.startswith("@"):
    with open(arg[1:], "r", encoding="utf-8") as f:
        line = f.read().strip()
else:
    line = arg

sp = serial.Serial(port, 115200, timeout=0.1)
time.sleep(0.3)
sp.reset_input_buffer()
sp.write((line + "\n").encode("utf-8"))
sp.flush()

end = time.time() + secs
chunks = []
while time.time() < end:
    d = sp.read(4096)
    if d:
        chunks.append(d.decode("utf-8", "replace"))
sp.close()

text = "".join(chunks)
print("--- sent: %s" % line)
for ln in text.splitlines():
    if ln.startswith("{"):
        print("JSON> " + ln)