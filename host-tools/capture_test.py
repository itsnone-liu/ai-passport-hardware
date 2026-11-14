"""capture.start / capture.stop 真机验证：单次开端口，避免每开一次就复位。"""
import json
import time

import serial

PORT = "COM3"
BAUD = 115200
TURN = 4242


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
                time.sleep(0.3)
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

    send({"id": 1, "cmd": "get_audio"})
    show(drain(2.0, want=1), "audio-before")

    print(f"[send] capture.start turn_id={TURN}")
    send({"id": 2, "cmd": "capture.start", "turn_id": TURN})
    show(drain(3.0, want=2), "start-ack")

    time.sleep(3.0)  # 实时送帧约 3 s
    send({"id": 3, "cmd": "get_audio"})
    show(drain(2.0, want=3), "audio-capturing")

    print(f"[send] capture.stop turn_id={TURN}")
    send({"id": 4, "cmd": "capture.stop", "turn_id": TURN})
    show(drain(3.0, want=4), "stop-ack")

    time.sleep(1.5)
    send({"id": 5, "cmd": "get_audio"})
    show(drain(2.0, want=5), "audio-after")
    send({"id": 6, "cmd": "get_status"})
    show(drain(2.0, want=6), "status")

    ser.close()


if __name__ == "__main__":
    main()