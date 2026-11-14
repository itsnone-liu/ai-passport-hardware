"""验证 pre-roll：先 arm 填 300 ms，再 capture.start，看是否补发。

注意 arm 有 1500 ms 时限（§6.5 cap_start_timeout），所以 arm 与 capture.start
之间不能插查询命令 —— 否则测试自己把自己等超时。
"""
import json
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200
TURN = int(sys.argv[1]) if len(sys.argv) > 1 else 5150
ARM = not (len(sys.argv) > 2 and sys.argv[2] == "noarm")


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
                time.sleep(0.25)
                buf += ser.read(8192)
                break
        return buf.decode("utf-8", "replace")

    def show(text, tag):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{") or "pav1:" in line or "cap:" in line:
                print(f"[{tag}] {line}")

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    show(drain(7.0), "boot")

    # 音频通道要等 Wi-Fi + 控制认证 + 音频认证，早于它就发 capture.start 只会拿到 busy。
    for i in range(20):
        send({"id": 100 + i, "cmd": "get_audio"})
        text = drain(2.0, want=100 + i)
        if '"state":"idle"' in text:
            time.sleep(1.0)  # 让 pav1 握手完成
            break
        time.sleep(1.0)

    if ARM:
        send({"id": 1, "cmd": "vendor.capture_arm"})
        show(drain(1.0, want=1), "arm")
        time.sleep(0.42)  # 填满 15 帧 = 300 ms，且必须早于 1500 ms 时限
    else:
        print("[skip] 不 arm：直接 capture.start，期望 pre_roll_ms 为 0")

    print(f"[send] capture.start turn_id={TURN}")
    send({"id": 2, "cmd": "capture.start", "turn_id": TURN})
    show(drain(3.0, want=2), "start-ack")

    time.sleep(1.5)
    send({"id": 3, "cmd": "get_audio"})
    show(drain(2.0, want=3), "audio-capturing")

    send({"id": 4, "cmd": "capture.stop", "turn_id": TURN})
    show(drain(3.0, want=4), "stop-ack")

    time.sleep(1.0)
    send({"id": 5, "cmd": "get_audio"})
    show(drain(2.0, want=5), "audio-after")

    ser.close()


if __name__ == "__main__":
    main()