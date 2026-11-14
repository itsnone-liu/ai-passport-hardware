"""等会话就绪后取 status：轮询到 auth==ok，打印 heap 与音频计数。"""
import json
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200
TRIES = int(sys.argv[1]) if len(sys.argv) > 1 else 20


def main():
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 0.3
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.3)
    ser.reset_input_buffer()

    def read_until_id(want, timeout=3.0):
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            chunk = ser.read(8192)
            if chunk:
                buf += chunk
            if b'"id":%d' % want in buf.replace(b" ", b""):
                break
        return buf.decode("utf-8", "replace")

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    for i in range(TRIES):
        time.sleep(1.0)
        send({"id": 200 + i, "cmd": "get_status"})
        text = read_until_id(200 + i)
        st = {}
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("cmd") == "status":
                    st = o
        if not st:
            continue
        print(f"t={i+1:2d}s auth={st['auth']:4s} audio_ready={str(st['audio_ready']):5s} "
              f"heap={st['heap']:6d} heap_min={st['heap_min']:6d} block={st['heap_block']:6d} "
              f"rx={st['audio_rx_frames']} tx={st['audio_tx_frames']}")
        if st["auth"] == "ok" and st["audio_ready"]:
            break
    ser.close()


if __name__ == "__main__":
    main()