"""Wi-Fi 省电 A/B：同一块板子，PS=NONE / MIN_MODEM 下测纯下行吞吐。"""
import json
import time

import serial

PORT = "COM3"
BAUD = 115200
HOST = "192.168.2.108"
SINK_PORT = 18181


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

    def events(text, name):
        out = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("event") == name:
                    out.append(o)
        return out

    # 等就位
    ready = False
    end = time.time() + 20
    while time.time() < end:
        if '"audio_ready":true' in read(1.5):
            ready = True
            break
    print(f"audio_ready={ready}")

    cid = 300

    def sink(tag):
        nonlocal cid
        cid += 1
        send({"id": cid, "cmd": "vendor.tcp_sink", "host": HOST, "port": SINK_PORT, "ms": 3000})
        text = read(5.0)
        ev = events(text, "tcp_sink")
        if ev:
            e = ev[-1]
            print(f"[{tag}] ok={e.get('ok')} bytes={e.get('bytes')} kbps={e.get('kbps')} "
                  f"calls={e.get('calls')} heap={e.get('heap')} min={e.get('heap_min')}")
        else:
            print(f"[{tag}] 无事件")

    def ps(mode):
        nonlocal cid
        cid += 1
        send({"id": cid, "cmd": "vendor.wifi_ps", "mode": mode})
        print(read(1.5).strip().splitlines()[-1:])

    sink("PS=NONE(默认)")
    ps(1)
    time.sleep(2)
    sink("PS=MIN_MODEM")
    ps(0)
    time.sleep(2)
    sink("PS=NONE(恢复)")
    ser.close()


if __name__ == "__main__":
    main()