"""一次性取证：status + get_audio + 最近串口日志里的采集行。

用法: python snap.py [日志秒数]
"""
import json
import sys
import time

import serial


def open_port():
    ser = serial.Serial()
    ser.port = "COM3"
    ser.baudrate = 115200
    ser.timeout = 0.25
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    ser = open_port()
    time.sleep(0.2)
    ser.reset_input_buffer()

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    def read(seconds):
        end = time.time() + seconds
        buf = b""
        while time.time() < end:
            chunk = ser.read(8192)
            if chunk:
                buf += chunk
        return buf.decode("utf-8", "replace")

    send({"id": 1, "cmd": "get_status"})
    send({"id": 2, "cmd": "get_audio"})
    text = read(secs)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            cmd = o.get("cmd")
            if cmd == "status":
                print(f"STATUS boot={o.get('boot')} auth={o.get('auth')} "
                      f"audio_ready={o.get('audio_ready')} heap={o.get('heap')} "
                      f"min={o.get('heap_min')} uptime={o.get('uptime')}s")
            elif cmd == "get_audio":
                print("AUDIO  " + json.dumps(o, ensure_ascii=False))
        elif any(k in line for k in ("cap:", "pav1:", "audio:", "play:", "auth:")):
            print("LOG    " + line)
    ser.close()


if __name__ == "__main__":
    main()