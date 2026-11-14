"""复现处暑的 2x 填充：起播 114 帧（2.28 s），按 2x 实时推，每 250 ms 采样。"""
import json
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200
SPEED = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def read_until_id(want, timeout=2.0):
        end = time.time() + timeout
        buf = b""
        while time.time() < end:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
            if b'"id":%d' % want in buf.replace(b" ", b""):
                break
        return buf.decode("utf-8", "replace")

    def field(text, key):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if key in obj:
                    return obj
        return {}

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    # 等就位
    end = time.time() + 8.0
    while time.time() < end:
        if b'"event":"ready"' in ser.read(4096):
            break

    print(f"speed={SPEED}（每 20 ms 推 {SPEED} 帧，即 {SPEED}x 实时）")
    send({"id": 1, "cmd": "vendor.playback_selftest", "ms": 2280, "speed": SPEED})
    field(read_until_id(1), "cmd")

    t0 = time.time()
    for i in range(26):
        time.sleep(0.25)
        send({"id": 100 + i, "cmd": "get_audio"})
        a = field(read_until_id(100 + i), "play_state")
        if not a:
            continue
        print(f"t={(time.time()-t0):.2f}s {a.get('play_state'):9s} turn={a.get('play_turn')} "
              f"ring={a.get('ring_frames'):3d} played={a.get('played'):4d} "
              f"under={a.get('play_underruns')} over={a.get('play_overflows')} "
              f"mismatch={a.get('play_mismatch')}")
    ser.close()


if __name__ == "__main__":
    main()