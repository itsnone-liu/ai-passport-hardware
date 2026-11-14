"""UDP 音频通道的单元测试：解析、重排、以及"乱序不等于丢包"这条判据。"""

import pytest

from server import audio_udp


def test_build_parse_roundtrip():
    dg = audio_udp.build(audio_udp.KIND_ADPCM, 7, b"\x01\x02\x03\x04hello")
    # this case covers an unsigned datagram; the MAC path is exercised by
    # test_mac_roundtrip_and_tamper.
    kind, turn, payload, mac = audio_udp.parse(dg)
    assert (kind, turn, payload) == (audio_udp.KIND_ADPCM, 7, b"\x01\x02\x03\x04hello")
    assert mac == b""
    assert len(dg) == audio_udp.HDR_LEN + 9


def test_header_is_the_tcp_header():
    """UDP 的头必须和 TCP 的 PAV1 头逐字节一致，否则抓包工具会看不懂。"""
    from server import pav1
    dg = audio_udp.build(audio_udp.KIND_HELLO, 0x01020304, b"")
    assert dg[:4] == b"PAV1"
    assert dg[4] == pav1.KIND_HELLO
    assert dg[8:12] == bytes([4, 3, 2, 1]), "turn_id 必须是 LE"


@pytest.mark.parametrize("bad", [
    b"",
    b"PAV",
    b"XAV1" + bytes(12),
    audio_udp.HDR.pack(b"PAV1", 5, 0, 0, 1, 999) + b"short",
])
def test_bad_datagrams_raise(bad):
    with pytest.raises(audio_udp.BadDatagram):
        audio_udp.parse(bad)


def test_in_order_passes_through():
    r = audio_udp.Reorder()
    out = []
    for seq in range(5):
        out += r.push(seq, bytes([seq]))
    assert out == [bytes([i]) for i in range(5)]
    assert (r.reordered, r.dups, r.gaps) == (0, 0, 0)


def test_one_swap_is_reordered_not_lost():
    """倒置两包：一包都不能少，而且出队顺序必须是 0,1,2,3。"""
    r = audio_udp.Reorder()
    out = r.push(0, b"a") + r.push(2, b"c") + r.push(1, b"b") + r.push(3, b"d")
    assert out == [b"a", b"b", b"c", b"d"]
    assert r.dups == 0 and r.gaps == 0
    assert r.reordered == 1


def test_duplicate_is_dropped():
    r = audio_udp.Reorder()
    out = r.push(0, b"a") + r.push(0, b"a") + r.push(1, b"b")
    assert out == [b"a", b"b"]
    assert r.dups == 1


def test_hole_smaller_than_hold_waits():
    """丢的那一包如果 80 ms 内补上来，就不该算丢——这正是重排缓冲存在的理由。"""
    r = audio_udp.Reorder(hold=4)
    out = r.push(0, b"a") + r.push(1, b"b") + r.push(3, b"d")
    assert out == [b"a", b"b"], "3 要先扣在缓冲里等 2"
    late = r.push(2, b"c")
    assert late == [b"c", b"d"], "补上 2 之后必须按 2,3 的顺序吐"
    assert r.gaps == 0


def test_hole_bigger_than_hold_gives_up():
    r = audio_udp.Reorder(hold=2)
    out = r.push(0, b"a") + r.push(5, b"f")
    assert out == [b"a", b"f"], "等不到就跳过空洞，别卡住整轮"
    assert r.gaps == 1


def test_flush_keeps_order():
    r = audio_udp.Reorder(hold=8)
    assert r.push(0, b"a") == [b"a"]
    assert r.push(2, b"c") == [], "2 等着 1"
    assert r.push(1, b"b") == [b"b", b"c"], "补齐后立刻按序吐出"
    assert r.push(5, b"f") == [], "5 还早，先扣着"
    assert r.flush() == [b"f"], "收轮时剩下的按序吐"
    assert r.buf == {}


def test_mac_roundtrip_and_tamper():
    """载荷一个 bit 变、头一个 bit 变、长度差一字节，都必须验不过。"""
    secret = bytes(range(16))
    body = audio_udp.build(audio_udp.KIND_ADPCM, 3, b"\x00" * 168)
    dg = body + audio_udp.sign(secret, body, audio_udp.MAC_AUDIO)
    kind, turn, payload, mac = audio_udp.parse(dg, audio_udp.MAC_AUDIO)
    assert (kind, turn) == (audio_udp.KIND_ADPCM, 3)
    assert audio_udp.verify(secret, dg[:-audio_udp.MAC_AUDIO], mac)

    for pos in (4, 8, 20, len(dg) - audio_udp.MAC_AUDIO - 1):
        bad = bytearray(dg)
        bad[pos] ^= 0x01
        assert not audio_udp.verify(secret, bytes(bad[:-audio_udp.MAC_AUDIO]),
                                    bytes(bad[-audio_udp.MAC_AUDIO:])), pos

    for chopped in (dg[:-1], dg + b"\x00"):
        with pytest.raises(audio_udp.BadDatagram):
            audio_udp.parse(chopped, audio_udp.MAC_AUDIO)


def test_hello_mac_is_longer_than_audio():
    """打洞包更值钱：8 字节，伪造一次 2^64；音频块 4 字节换 2% 带宽。"""
    assert audio_udp.mac_len_for(audio_udp.KIND_HELLO) == 8
    assert audio_udp.mac_len_for(audio_udp.KIND_ADPCM) == 4
