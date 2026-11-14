"""验证 sitecustomize 生效：反复开关串口，设备的 boot 计数不应增长。"""
import json
import time

import serial

PORT = "COM3"


def send(ser, obj):
    ser.write((json.dumps(obj) + "\n").encode())
    ser.flush()


def read(ser, seconds):
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
    return buf.decode("utf-8", "replace")


def status(ser):
    send(ser, {"id": 900, "cmd": "get_status"})
    for line in read(ser, 1.5).splitlines():
        line = line.strip()
        if line.startswith("{") and '"cmd":"status"' in line:
            return json.loads(line)
    return {}


def main():
    for i in range(4):
        ser = serial.Serial(PORT, 115200, timeout=0.25)  # 故意用会产生复位的老写法
        time.sleep(0.3)
        ser.reset_input_buffer()
        st = status(ser)
        print(f"第 {i + 1} 次打开: boot={st.get('boot')} reset={st.get('reset')} "
              f"heap={st.get('heap')} prev_heap_min={st.get('prev_heap_min')}")
        ser.close()
        time.sleep(1.0)


if __name__ == "__main__":
    main()