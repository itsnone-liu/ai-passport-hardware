"""IMA/DVI ADPCM 4:1 分块编解码（ai-passport 上行）。

为什么不是 Opus（实测结论，别再来一遍）：
    libopus 的编码器状态 opus_encoder_get_size() = 24,716 B，必须是一块连续堆；
    再加编码任务 12 KB 栈，一共要 ~37 KB 连续 RAM。本机（ESP32-C3，400 KB SRAM，
    无 PSRAM）最大连续块只有 28.7 KB，opus_encoder_create 直接 OPUS_ALLOC_FAIL(-7)。
    腾出这个量只能砍放音环，那是拿下行换上行，不划算。ADPCM 4:1 后上行 8.2 KB/s，
    200 ms RTT 下在途 1.6 KB，而链路实测 18.7 KB/s，余量 2 倍以上。

线上格式（PAV1 kind=5 的 payload）：
    [u32 LE seq][164 B 块]
    块 = [int16 LE 预测值][u8 步长索引][u8 保留=0][160 B 半字节]
    20 ms / 块（320 采样 @16 kHz）→ 168 B / 20 ms = 8.2 KB/s

分块而非裸流的理由：块头自带解码器起步状态，任何一块都能独立解码。第 3 段换 UDP
丢包时，坏一块只烂 20 ms，不会像裸流那样从此全体失步。
"""

from __future__ import annotations

import struct
from typing import List, Tuple

SAMPLE_HZ = 16000
FRAME_MS = 20
SAMPLES = SAMPLE_HZ // 1000 * FRAME_MS      # 320
NIBBLE_BYTES = SAMPLES // 2                 # 160
BLOCK_BYTES = 4 + NIBBLE_BYTES              # 164
PAYLOAD_BYTES = 4 + BLOCK_BYTES             # 168（含 seq）
PCM_BYTES = SAMPLES * 2                     # 640

STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55,
    60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
    20350, 22385, 24623, 27086, 29794, 32767,
)
INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)


def _clamp16(v: int) -> int:
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def decode_block(block: bytes) -> bytes:
    """一块 164 B → 640 B PCM16LE。"""
    if len(block) != BLOCK_BYTES:
        raise ValueError(f"adpcm block must be {BLOCK_BYTES} bytes, got {len(block)}")
    predicted = struct.unpack_from("<h", block, 0)[0]
    index = min(block[2], 88)
    out = bytearray(PCM_BYTES)
    pos = 0
    for byte in block[4:]:
        for shift in (0, 4):
            nib = (byte >> shift) & 0x0F
            step = STEP_TABLE[index]
            vpdiff = step >> 3
            if nib & 4:
                vpdiff += step
            if nib & 2:
                vpdiff += step >> 1
            if nib & 1:
                vpdiff += step >> 2
            predicted = _clamp16(predicted - vpdiff if nib & 8 else predicted + vpdiff)
            index += INDEX_TABLE[nib]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            struct.pack_into("<h", out, pos, predicted)
            pos += 2
    return bytes(out)


def split_payload(payload: bytes) -> Tuple[int, bytes]:
    """kind=5 payload → (seq, 164 B 块)。"""
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"adpcm payload must be {PAYLOAD_BYTES} bytes, got {len(payload)}")
    seq = struct.unpack_from("<I", payload, 0)[0]
    return seq, payload[4:]


def decode_payload(payload: bytes) -> bytes:
    """kind=5 payload → 640 B PCM16LE。"""
    _seq, block = split_payload(payload)
    return decode_block(block)


def encode_block(pcm: bytes, state: "Tuple[int, int]" = (0, 0)) -> Tuple[bytes, Tuple[int, int]]:
    """测试用编码器（与设备侧 C 实现逐句对应）。返回 (164 B 块, 新状态)。"""
    if len(pcm) != PCM_BYTES:
        raise ValueError(f"pcm must be {PCM_BYTES} bytes, got {len(pcm)}")
    predicted, index = state
    samples = struct.unpack(f"<{SAMPLES}h", pcm)
    block = bytearray(BLOCK_BYTES)
    struct.pack_into("<h", block, 0, predicted)
    block[2] = index & 0xFF
    block[3] = 0
    pos = 4
    for i in range(0, SAMPLES, 2):
        byte = 0
        for k in range(2):
            step = STEP_TABLE[index]
            diff = samples[i + k] - predicted
            nib = 0
            if diff < 0:
                nib = 8
                diff = -diff
            vpdiff = step >> 3
            if diff >= step:
                nib |= 4
                diff -= step
                vpdiff += step
            step >>= 1
            if diff >= step:
                nib |= 2
                diff -= step
                vpdiff += step
            step >>= 1
            if diff >= step:
                nib |= 1
                vpdiff += step
            predicted = _clamp16(predicted - vpdiff if nib & 8 else predicted + vpdiff)
            index += INDEX_TABLE[nib]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            byte |= (nib & 0x0F) << (4 * k)
        block[pos] = byte
        pos += 1
    return bytes(block), (predicted, index)


def encode_payload(seq: int, block: bytes) -> bytes:
    return struct.pack("<I", seq & 0xFFFFFFFF) + block


def pcm_rms(pcm: bytes) -> float:
    """PCM16LE 的 RMS（与设备 capture.ended 里的 rms 同口径，用来对账）。"""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5
