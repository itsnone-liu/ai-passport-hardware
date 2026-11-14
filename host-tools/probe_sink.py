import socket

for target in [("127.0.0.1", 18181), ("192.168.2.108", 18181)]:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(target)
        print(target, "OK")
        s.close()
    except Exception as exc:
        print(target, "FAIL", exc)