"""反向吞吐 + Wi-Fi 省电 A/B：设备监听 18182，本机连进去全速灌 3 s。

对比 PS=NONE 与 PS=MIN_MODEM 下设备侧真实接收速率。
"""
import json
import socket
import time

import serial

PORT = "COM3"
BAUD = 115200
DEV = "192.168.2.107"
P = 18182


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
    cid = [500]

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

    def blast(tag):
        cid[0] += 1
        send({"id": cid[0], "cmd": "vendor.tcp_accept", "port": P})
        time.sleep(1.0)
        s = socket.socket()
        s.settimeout(5)
        try:
            s.connect((DEV, P))
        except Exception as exc:
            print(f"[{tag}] 连接失败 {exc}")
            return
        blob = bytes(65536)
        sent = 0
        t0 = time.time()
        try:
            while time.time() - t0 < 3.0:
                s.sendall(blob)
                sent += len(blob)
        except OSError as exc:
            print(f"[{tag}] 发送中断 {exc}")
        s.close()
        el = time.time() - t0
        text = read(4.0)
        ev = events(text, "tcp_accept")
        if ev:
            e = ev[-1]
            print(f"[{tag}] 设备收到 {e.get('bytes')} B / {e.get('ms')} ms = "
                  f"{e.get('kbps')} KB/s (recv {e.get('calls')} 次) | 本机发出 {sent} B "
                  f"= {sent/1024/el:.1f} KB/s | heap={e.get('heap')} block={e.get('heap_block')}")
        else:
            print(f"[{tag}] 无事件（本机发 {sent} B）")

    def ps(mode):
        cid[0] += 1
        send({"id": cid[0], "cmd": "vendor.wifi_ps", "mode": mode})
        read(1.0)

    blast("PS=NONE")
    ps(1)
    time.sleep(2)
    blast("PS=MIN_MODEM")
    ps(0)
    time.sleep(2)
    blast("PS=NONE-again")
    ser.close()


if __name__ == "__main__":
    main()