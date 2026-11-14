"""复位取证自检：故意 panic 一次，重启后确认 reset 被标成 panic、计数 +1。"""
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


def read(ser, seconds):
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
    return buf.decode("utf-8", "replace")


def status(ser, ident=1):
    ser.write((json.dumps({"id": ident, "cmd": "get_status"}) + "\n").encode())
    ser.flush()
    for line in read(ser, 2.0).splitlines():
        line = line.strip()
        if line.startswith("{") and '"cmd":"status"' in line:
            return json.loads(line)
    return {}


def report(tag, st):
    print(f"{tag}: boot={st.get('boot')} reset={st.get('reset')}(code {st.get('reset_code')}) "
          f"prev_heap_min={st.get('prev_heap_min')} prev_uptime={st.get('prev_uptime')}s "
          f"heap={st.get('heap')}")


def main():
    ser = open_port()
    time.sleep(0.3)
    ser.reset_input_buffer()
    before = status(ser)
    report("panic 前", before)
    ser.write((json.dumps({"id": 2, "cmd": "vendor.crash"}) + "\n").encode())
    ser.flush()
    print("已发出 vendor.crash，等设备重启…")
    time.sleep(8)
    ser.reset_input_buffer()
    after = status(ser, 3)
    report("panic 后", after)
    grew = (after.get("boot", 0) or 0) > (before.get("boot", 0) or 0)
    print(f"boot 递增={grew}  复位原因={after.get('reset')}  上次最小堆="
          f"{after.get('prev_heap_min')}")
    ser.close()


if __name__ == "__main__":
    main()