"""按键监听：一边读串口原始输出，一边每 400 ms 轮询 get_audio。

用途：确认 OK 键到底有没有产生 key 事件、有没有进入 armed。
（同一时刻只有一个进程能占用 COM3，所以收发合在一个脚本里。）
"""
import json
import sys
import time

import serial


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    ser = serial.Serial()
    ser.port = "COM3"
    ser.baudrate = 115200
    ser.timeout = 0.05
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.2)
    ser.reset_input_buffer()

    end = time.time() + secs
    buf = b""
    last_poll = 0.0
    ident = 10
    states = []
    while time.time() < end:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if '"event"' in text or "cap:" in text or "capture" in text or "arm" in text.lower():
                    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)
        now = time.time()
        if now - last_poll > 0.4:
            last_poll = now
            ident += 1
            ser.write((json.dumps({"id": ident, "cmd": "get_audio"}) + "\n").encode())
            ser.flush()
            # 读掉回执，只关心状态字段
            deadline = time.time() + 0.3
            got = b""
            while time.time() < deadline:
                more = ser.read(4096)
                if more:
                    got += more
                    buf += more
            for line in got.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith('{"id"') and '"get_audio"' in line:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    snap = (o.get("state"), o.get("turn_id"), o.get("frames"), o.get("turns"))
                    if not states or states[-1] != snap:
                        states.append(snap)
                        print(f"[{time.strftime('%H:%M:%S')}] state={snap[0]} turn={snap[1]} "
                              f"frames={snap[2]} turns={snap[3]}", flush=True)
    ser.close()
    print("监听结束")


if __name__ == "__main__":
    main()