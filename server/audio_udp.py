"""UDP 音频通道（第 3 段）。

一个数据报 = 一个 20 ms 块：PAV1 16 字节头 + 载荷。这个模块只做三件事，
其余全部复用 TCP 路径的代码，目的是让"音频走 UDP"不改变任何语义：

  1. `parse` / `build`：数据报和 PAV1 头互转（头的布局和 TCP 完全一样，
     所以抓包、日志、离线工具都不用改）。
  2. `Reorder`：按 seq 重排。UDP 会乱序、会丢、会重复，而下游的
     `on_uplink_pcm` 只会按到达顺序往缓冲里追加 PCM，所以顺序必须在这里修好。
  3. `DatagramProtocol`：asyncio 的胶水，把数据报转给 Service。

为什么要重排而不是丢掉乱序包：每块 ADPCM 自带预测器和步长，单独解码不会
炸，但**拼错顺序的 PCM 听起来是电流声**，而且 ASR 会把它当成真语音去识别。
看上去像"识别不准"，实际是拼接顺序错了——这种误诊之前已经付过一次代价。

打洞：设备发 kind=2 的 HELLO，服务端从**源地址**学到它，回一个 kind=2 的 ack。
服务端不需要任何入站端口配置，设备也不需要知道自己公网地址。
"""

import logging
import struct
from typing import Dict, List, Tuple

log = logging.getLogger(__name__)

HDR = struct.Struct("<4sBBHII")
HDR_LEN = HDR.size

KIND_HELLO = 2
KIND_ADPCM = 5

# 为乱序等最多这么多包（4 × 20 ms = 80 ms）之后再认定是丢了。
# 依据：实测 RTT 205 ms，但同一连接内的抖动远小于 RTT；80 ms 已经能盖住
# 常见的 1–2 包倒置，再等就会把延迟推到影响打断（barge-in）的程度。
REORDER_HOLD = 4

MAX_PAYLOAD = 4096


class BadDatagram(ValueError):
    """数据报不合法。调用方只计数，不要抛到事件循环里。"""


def parse(dgram: bytes) -> Tuple[int, int, bytes]:
    """返回 (kind, turn_id, payload)。"""
    if len(dgram) < HDR_LEN:
        raise BadDatagram(f"short datagram {len(dgram)}B")
    magic, kind, _flags, _res, turn, plen = HDR.unpack_from(dgram)
    if magic != b"PAV1":
        raise BadDatagram(f"bad magic {magic!r}")
    if plen > MAX_PAYLOAD:
        raise BadDatagram(f"payload too large {plen}")
    if HDR_LEN + plen > len(dgram):
        raise BadDatagram(f"truncated: header says {plen}, have {len(dgram) - HDR_LEN}")
    return kind, turn, dgram[HDR_LEN:HDR_LEN + plen]


def build(kind: int, turn: int, payload: bytes = b"") -> bytes:
    return HDR.pack(b"PAV1", kind, 0, 0, turn, len(payload)) + payload


class Reorder:
    """按 seq 重排，给乱序有限的耐心，超时就当丢了。"""

    def __init__(self, hold: int = REORDER_HOLD) -> None:
        self.hold = hold
        self.reset()

    def reset(self, expected: int = 0) -> None:
        self.expected = expected
        self.buf: Dict[int, bytes] = {}
        self.dups = 0
        self.reordered = 0
        self.gaps = 0

    def push(self, seq: int, payload: bytes) -> List[bytes]:
        out: List[bytes] = []
        if seq < self.expected:
            self.dups += 1
            return out
        if seq != self.expected:
            self.reordered += 1
        self.buf.setdefault(seq, payload)
        while self.expected in self.buf:
            out.append(self.buf.pop(self.expected))
            self.expected += 1
        # 等太久了：把空洞当丢失，跳到目前最小的那个，别让整轮卡在这一包上。
        if self.buf and (max(self.buf) - self.expected) >= self.hold:
            nxt = min(self.buf)
            if nxt > self.expected:
                self.gaps += 1
                self.expected = nxt
                while self.expected in self.buf:
                    out.append(self.buf.pop(self.expected))
                    self.expected += 1
        return out

    def flush(self) -> List[bytes]:
        """收轮时把剩下的按 seq 吐出来（空洞仍然是空洞，不补静音）。"""
        out = [self.buf[seq] for seq in sorted(self.buf)]
        if self.buf:
            self.expected = max(self.buf) + 1
            self.buf.clear()
        return out

    def stats(self) -> str:
        return (f"reordered={self.reordered} dups={self.dups} gaps={self.gaps} "
                f"pending={len(self.buf)}")


class DatagramProtocol:
    """asyncio 胶水：一个方法，转给 Service。

    Service.on_udp_datagram 是同步的（不能 await）：数据报处理必须立刻完成，
    否则内核缓冲被后来的包冲掉——UDP 的缓冲只有几十 KB。
    """

    def __init__(self, service) -> None:  # noqa: ANN001 - 避免循环导入
        self.service = service

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: ANN001
        # 回调里任何异常都会被 asyncio 吞成一行栈：服务照跑、包照到、日志照静。
        # 实测这样让整条 UDP 音频通道看起来"根本没收到包"。兜底 + 计数。
        try:
            self.service.on_udp_datagram(data, addr, self.transport)
        except Exception:  # noqa: BLE001 - 单包出错不能拖垮整条通道
            self.service.udp_errors += 1
            if self.service.udp_errors <= 3:
                log.exception("%s udp datagram handler failed", self.service)

    def error_received(self, exc) -> None:  # noqa: ANN001
        self.service.udp_errors += 1

    def connection_made(self, transport) -> None:  # noqa: ANN001
        self.transport = transport
