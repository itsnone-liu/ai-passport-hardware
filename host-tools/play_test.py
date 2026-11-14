"""发一条命令到 USB 链路并收回复/事件。

用法: python play_test.py <命令文件> [秒数]
命令文件里放一行 JSON（PowerShell 5.1 会剥掉传给原生 exe 的双引号，所以走文件）。

注意：必须用「先设 dtr/rts 再 open」的方式打开串口。直接
serial.Serial(PORT, BAUD) 会拉 DTR 触发 ESP32-C3 复位，于是每条命令都在
设备刚重启、Wi-Fi 还没起来时发出——我因此误判过"连不上服务器"。
"""
import json
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200


def open_port():
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 0.25
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cmd = json.loads(f.read())
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    ser = open_port()
    time.sleep(0.2)
    ser.reset_input_buffer()
    payload = (json.dumps(cmd) + "\n").encode()
    print("[send] " + json.dumps(cmd, ensure_ascii=False))
    ser.write(payload)
    ser.flush()

    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        chunk = ser.read(8192)
        if chunk:
            buf += chunk
    ser.close()

    for line in buf.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            print(line)
        elif "audio:" in line or "cap:" in line or "play:" in line or "pav1:" in line:
            print(line)


if __name__ == "__main__":
    main()