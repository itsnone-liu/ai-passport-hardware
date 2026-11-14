"""播放自测：起播 → 等 3 s → get_audio 核对状态机与计数。"""
import json
import time

import serial

PORT = "COM3"
BAUD = 115200


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def drain(seconds, want=None):
        end = time.time() + seconds
        buf = b""
        while time.time() < end:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
            if want is not None and b'"id":%d' % want in buf.replace(b" ", b""):
                time.sleep(0.2)
                buf += ser.read(8192)
                break
        return buf.decode("utf-8", "replace")

    def show(text, tag):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                print(f"[{tag}] {line}")

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    show(drain(7.0), "boot")

    send({"id": 1, "cmd": "vendor.playback_selftest", "ms": 2000})
    show(drain(2.0, want=1), "start-ack")

    time.sleep(0.8)
    send({"id": 2, "cmd": "get_audio"})
    show(drain(2.0, want=2), "mid")

    time.sleep(2.5)
    send({"id": 3, "cmd": "get_audio"})
    show(drain(2.0, want=3), "after")

    ser.close()


if __name__ == "__main__":
    main()