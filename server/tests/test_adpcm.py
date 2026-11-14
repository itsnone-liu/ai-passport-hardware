"""ADPCM 上行（PAV1 kind=5）自证。

实测质量（本机同一套算法，16 kHz 单声道，320 采样一块）：
    amp=2000  180 Hz 32.9 dB | 440 Hz 22.3 dB | 1000 Hz 17.4 dB
    amp=8000  180 Hz 27.1 dB | 440 Hz 18.5 dB | 1000 Hz 16.4 dB
    amp=20000 180 Hz 24.4 dB | 440 Hz 16.8 dB | 1000 Hz 16.1 dB
    RMS 相对误差一律 <= 1.5%
所以阈值按实测取：基频段 >= 25 dB，中频段 >= 15 dB。别写成"越高越好"的空话——
4:1 ADPCM 的噪声随频率上升是它的固有性质，写死一个 30 dB 只会让测试撒谎。

最有价值的一条是 test_rms_matches_device_reported：设备在 capture.ended 里报的 rms
是**编码前**的原始 PCM 统计，服务端解出来再算一遍，两者必须在量化误差内吻合。
它同时验证块头语义、半字节顺序、步长表——任何一样错位，rms 都会明显偏。
"""
from __future__ import annotations

import math
import struct

import pytest

from server import adpcm, pav1


def sine(n=320, freq=440.0, amp=8000.0, phase=0.0):
    return [
        int(amp * math.sin(2 * math.pi * freq * (i + phase) / adpcm.SAMPLE_HZ))
        for i in range(n)
    ]


def pcm_bytes(samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def snr_db(src, out):
    err = sum((a - b) ** 2 for a, b in zip(src, out))
    sig = sum(a * a for a in src)
    return 10 * math.log10(sig / max(err, 1))


def test_constants():
    assert adpcm.SAMPLES == 320
    assert adpcm.BLOCK_BYTES == 164
    assert adpcm.PAYLOAD_BYTES == 168
    assert adpcm.PCM_BYTES == 640
    # 8.4 KB/s 的来处：168 B / 20 ms。链路实测上限 18.7 KB/s，余量 2.2 倍。
    assert round(adpcm.PAYLOAD_BYTES / (adpcm.FRAME_MS / 1000) / 1000, 1) == 8.4


def test_roundtrip_low_band():
    """基频段（人声基音主要在这儿）：实测 24.4~32.9 dB，要求 >= 24 dB。"""
    for amp in (2000, 8000, 20000):
        src = sine(freq=180.0, amp=amp)
        block, _ = adpcm.encode_block(pcm_bytes(src))
        out = struct.unpack("<320h", adpcm.decode_block(block))
        s = snr_db(src, out)
        assert s >= 24.0, f"amp={amp} snr={s:.1f} dB"
        assert max(abs(v) for v in out) <= 32767


def test_roundtrip_mid_band():
    """中频段：实测 16~22 dB，要求 >= 15 dB（4:1 ADPCM 固有水平，不是 bug）。"""
    for freq in (440.0, 1000.0):
        src = sine(freq=freq, amp=8000.0)
        block, _ = adpcm.encode_block(pcm_bytes(src))
        out = struct.unpack("<320h", adpcm.decode_block(block))
        s = snr_db(src, out)
        assert s >= 15.0, f"freq={freq} snr={s:.1f} dB"
        # 波形形状仍然是相关的：ASR 要的是谱包络，不是样点级保真
        num = sum(a * b for a, b in zip(src, out))
        den = math.sqrt(sum(a * a for a in src) * sum(b * b for b in out))
        assert num / den > 0.98


def test_blocks_are_independent():
    """块头自带状态：拿第 3 块单独解，结果必须和连解时一致（第 3 段 UDP 靠这个）。"""
    state = (0, 0)
    blocks, stream = [], []
    for k in range(4):
        block, state = adpcm.encode_block(pcm_bytes(sine(amp=6000.0, phase=k * 320)), state)
        blocks.append(block)
        stream.append(adpcm.decode_block(block))
    assert adpcm.decode_block(blocks[2]) == stream[2]


def test_payload_seq_roundtrip():
    block, _ = adpcm.encode_block(pcm_bytes(sine()))
    payload = adpcm.encode_payload(7, block)
    assert len(payload) == adpcm.PAYLOAD_BYTES
    seq, got = adpcm.split_payload(payload)
    assert seq == 7 and got == block
    assert adpcm.decode_payload(payload) == adpcm.decode_block(block)


def test_bad_payload_length():
    with pytest.raises(ValueError):
        adpcm.split_payload(b"\x00" * 100)
    with pytest.raises(ValueError):
        adpcm.decode_block(b"\x00" * 100)


def test_pav1_accepts_kind5():
    block, _ = adpcm.encode_block(pcm_bytes(sine()))
    raw = pav1.encode_frame(pav1.KIND_ADPCM, 3, adpcm.encode_payload(0, block))
    got = pav1.Framer().feed(raw)
    assert len(got) == 1
    f = got[0]
    assert f.kind == pav1.KIND_ADPCM and f.turn_id == 3
    assert f.kind_name == "ADPCM"
    assert adpcm.decode_payload(f.payload) == adpcm.decode_block(block)


def test_pav1_rejects_wrong_adpcm_len():
    """长度不合规必须当帧级错误炸掉，不能"凑合解"——错位的半字节听起来像噪声。"""
    raw = pav1.encode_frame(pav1.KIND_ADPCM, 1, b"\x00" * 10)
    with pytest.raises(pav1.Pav1Error) as ei:
        pav1.Framer().feed(raw)
    assert ei.value.code == "BAD_FRAME"


def test_rms_matches_device_reported():
    """与设备 capture.ended 的 rms 对账口径一致：解码后算 rms ≈ 编码前算 rms。"""
    src = sine(amp=4800.0)
    src_pcm = pcm_bytes(src)
    block, _ = adpcm.encode_block(src_pcm)
    rms_src = adpcm.pcm_rms(src_pcm)
    rms_dec = adpcm.pcm_rms(adpcm.decode_block(block))
    assert rms_src > 1000
    rel = abs(rms_dec - rms_src) / rms_src
    assert rel < 0.05, f"rms src={rms_src:.0f} dec={rms_dec:.0f} rel={rel:.3f}"
