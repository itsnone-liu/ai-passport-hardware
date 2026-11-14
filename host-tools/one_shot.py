"""一次打开串口，跑完整个序列 —— 避免每开一次端口就复位一次板子。"""
import json
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    # 明确放开 DTR/RTS：USB-Serial/JTAG 上这两根线会触发复位/下载模式。
    ser.dtr = False
    ser.rts = False
    time.sleep(0.3)
    ser.reset_input_buffer()

    def drain(seconds, want=None):
        """读 seconds 秒；want 给定时，收到该 id 的应答就提前返回。"""
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

    def jsons(text, tag):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                print(f"[{tag}] {line}")

    def send(obj):
        ser.write((json.dumps(obj) + "\n").encode())
        ser.flush()

    # 等设备就位（ready 事件或 6 s 兜底）
    boot = drain(6.0)
    jsons(boot, "boot")

    send({"id": 1, "cmd": "get_status"})
    jsons(drain(3.0, want=1), "status-1")

    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    send({"id": 2, "cmd": "vendor.uplink_test", "frames": frames})
    print(f"[send] vendor.uplink_test frames={frames}")
    jsons(drain(8.0, want=2), "uplink-ack")

    # 自测由 pav1 任务按 20 ms 节拍发送，等它跑完再看计数，否则读到的永远是 0。
    time.sleep(5.0)

    send({"id": 3, "cmd": "get_status"})
    jsons(drain(3.0, want=3), "status-2")

    ser.close()


if __name__ == "__main__":
    main()