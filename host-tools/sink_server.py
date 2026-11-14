"""只会往连接里灌数据的 TCP sink：用于测设备侧纯下行吞吐。

用法: python sink_server.py [port] [seconds]
连上来就先握手一行 ASCII（让设备端日志有东西可看），然后全速发 640 B 块。
"""
import socket
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18181
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

CHUNK = 640
BLOB = bytes(65536)


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT))
    srv.listen(2)
    srv.settimeout(1.0)
    print(f"sink listening on 0.0.0.0:{PORT} for {SECONDS:.0f}s", flush=True)
    end = time.time() + SECONDS
    while time.time() < end:
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = f"{addr[0]}:{addr[1]}"
        print(f"connect from {peer}", flush=True)
        sent = 0
        t0 = time.time()
        try:
            while time.time() - t0 < 20.0:
                conn.sendall(BLOB)
                sent += len(BLOB)
                elapsed = time.time() - t0
                if int(elapsed) % 5 == 4 and sent % (BLOB.__len__() * 64) < len(BLOB):
                    print(f"  {peer} sent {sent/1024:.0f} KB in {elapsed:.1f}s "
                          f"= {sent/1024/elapsed:.1f} KB/s", flush=True)
        except OSError as exc:
            print(f"  {peer} ended: {exc}", flush=True)
        finally:
            elapsed = max(time.time() - t0, 1e-6)
            print(f"  {peer} total {sent} B in {elapsed:.2f}s = "
                  f"{sent/1024/elapsed:.1f} KB/s", flush=True)
            conn.close()
    srv.close()


if __name__ == "__main__":
    main()