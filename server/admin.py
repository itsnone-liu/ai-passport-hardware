"""Tiny admin client:  python3 -m server.admin list | send | show | speak

Talks to the service's localhost admin socket (server/config `admin_port`), which
is how a human or a script drives a device without being the device. Local only:
the socket binds 127.0.0.1 and everything it can do reaches a speaker in a room.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys


def call(port: int, req: dict) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m server.admin")
    ap.add_argument("--port", type=int, default=18090)
    sub = ap.add_subparsers(dest="op", required=True)

    sub.add_parser("list")
    p_send = sub.add_parser("send", help="send a raw control line, id preserved")
    p_send.add_argument("device")
    p_send.add_argument("json")
    p_send.add_argument("--timeout", type=float, default=20.0)
    p_show = sub.add_parser("show")
    p_show.add_argument("device")
    p_show.add_argument("text")
    p_speak = sub.add_parser("speak")
    p_speak.add_argument("device")
    p_speak.add_argument("text")

    args = ap.parse_args(argv)
    if args.op == "list":
        req = {"op": "list"}
    elif args.op == "send":
        req = {"op": "send", "device": args.device, "json": json.loads(args.json),
               "timeout": args.timeout}
    elif args.op == "show":
        req = {"op": "show", "device": args.device, "text": args.text}
    else:
        req = {"op": "speak", "device": args.device, "text": args.text}

    print(json.dumps(call(args.port, req), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
