"""Stand-in for the dsh side: a TCP server speaking the terminal's line protocol.

Same framing the device already uses over USB — one JSON object per line, '\\n'
terminated — so this file doubles as the reference for the real server on the
Ubuntu box.

It greets a newly connected device with show_text + get_status, then prints
everything the device pushes (ready / key events) and answers pings.

usage: python tcp_lab_server.py [--port 18080]
"""
import argparse
import json
import socket
import threading
import time


def pump(conn, addr, lock):
    buf = b""
    seq = [100]

    def send(obj):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with lock:
            conn.sendall(line.encode("utf-8"))
        print(f"[srv] -> {line.strip()}", flush=True)

    send({"id": seq[0], "cmd": "show_text", "text": "Hello from dsh\nnet link up"})
    send({"id": seq[0] + 1, "cmd": "get_status"})

    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                print(f"[srv] {addr} closed", flush=True)
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                print(f"[srv] <- {text}", flush=True)
                try:
                    msg = json.loads(text)
                except Exception:
                    continue
                if msg.get("event") == "key":
                    # 服务器侧的示例反应：按键到达即回一条文本
                    seq[0] += 1
                    send(
                        {
                            "id": seq[0],
                            "cmd": "show_text",
                            "text": f"key {msg.get('btn')} {msg.get('ev')}\nfrom dsh server",
                        }
                    )
    except OSError:
        print(f"[srv] {addr} error", flush=True)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(4)
    print(f"[srv] listening on 127.0.0.1:{args.port}", flush=True)
    lock = threading.Lock()
    while True:
        conn, addr = srv.accept()
        print(f"[srv] connection from {addr}", flush=True)
        threading.Thread(target=pump, args=(conn, addr, lock), daemon=True).start()


if __name__ == "__main__":
    main()