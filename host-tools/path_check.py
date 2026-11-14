"""抓一条完整 status，再分别测到三个锚点的 RTT（设备侧视角）。"""
import json
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
    ser = open_port()
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

    def cmd(obj, seconds):
        send(obj)
        out = []
        for line in read(seconds).splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    st = [o for o in cmd({"id": 1, "cmd": "get_status"}, 2.0) if o.get("cmd") == "status"]
    if st:
        s = st[0]
        print(f"boot={s.get('boot')} reset={s.get('reset')}(code {s.get('reset_code')}) "
              f"prev_heap_min={s.get('prev_heap_min')} prev_block={s.get('prev_heap_block')} "
              f"prev_uptime={s.get('prev_uptime')}s")
        print(f"now: heap={s.get('heap')} min={s.get('heap_min')} block={s.get('heap_block')} "
              f"auth={s.get('auth')} audio_ready={s.get('audio_ready')}")

    # 三个锚点：一个国内公网、一个境外、一个自己的服务器。
    for host, port in [("223.5.5.5", 443), ("1.1.1.1", 443), ("154.64.231.1", 18080)]:
        res = [o for o in cmd({"id": 7, "cmd": "vendor.rtt", "host": host, "port": port,
                               "tries": 6}, 25.0) if o.get("cmd") == "vendor.rtt"]
        if res:
            r = res[0]
            if r.get("ok"):
                print(f"{host}:{port:<6} ok={r['success']}/{r['tries']} "
                      f"min={r['min_ms']} avg={r['avg_ms']} max={r['max_ms']} ms")
            else:
                print(f"{host}:{port:<6} 失败 {r.get('err')}")
    ser.close()


if __name__ == "__main__":
    main()