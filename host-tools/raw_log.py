"""原始串口转储：抓 N 秒所有 log 行，按关键词过滤。"""
import re
import sys
import time

import serial

PORT = "COM3"
BAUD = 115200
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 15
PATTERN = sys.argv[2] if len(sys.argv) > 2 else None


def main():
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    ser.dtr = False
    ser.rts = False
    time.sleep(0.2)
    ser.reset_input_buffer()
    end = time.time() + SECONDS
    out = []
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            out.append(chunk.decode("utf-8", "replace"))
    ser.close()
    text = "".join(out)
    if PATTERN:
        rx = re.compile(PATTERN)
        for line in text.splitlines():
            if rx.search(line):
                print(line)
    else:
        print(text)


if __name__ == "__main__":
    main()