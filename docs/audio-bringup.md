# Audio bring-up on the real board

What the Passport's audio path actually does, measured rather than assumed. These
numbers are the device-side constraints the voice protocol has to live inside;
`docs/voice-protocol-v1.md` is the contract, this file is the evidence behind it.

Every figure below comes from a real ESP32-C3 Passport over USB-Serial/JTAG, not
from the emulator.

## How to reproduce

```
python host/dsh_cmd.py COM3 "@cmd-audio.txt" 10
```

where `cmd-audio.txt` holds:

```json
{"id":40,"cmd":"audio_selftest","ms":2000}
```

The device records that many milliseconds at 16 kHz / mono / s16le into one
buffer, computes level statistics, then plays the same PCM back through the same
codec. It answers immediately and sends the result as an event when done:

```json
{"event":"audio_selftest","ok":true,"seq":1,"format":"pcm_s16le/16000/1",
 "frame_ms":20,"frames":100,"bytes":64000,"rms":7601,"peak":32768,"dc":810,
 "clips":526,"nonzero":15835,"record_ms":1992,"play_ms":1928,
 "rate_ratio":99,"read_fail":0,"write_fail":0}
```

## Format

`pcm_s16le / 16000 / 1` works directly with no conversion. 20 ms is 640 bytes
(320 samples), and 100 such frames read and wrote with zero failures, so the DMA
buffers keep up with a 20 ms cadence at this rate.

`rate_ratio` is `record_ms * 100 / nominal_ms`. It sits at 98–99, i.e. reads are
paced by the codec clock to within 1–2%. That matters twice: it means a frame is a
frame (no silent speed-up), and it means the device cannot manufacture audio
faster than real time, so nothing on the server side should assume the device can
drain a burst at network speed.

## Memory: an utterance does not fit

| Buffer | Bytes | Result |
|---|---|---|
| 2 s | 64,000 | allocated |
| 4 s | 128,000 | **failed** |

`get_status` reports the live numbers:

```json
"heap":118624,"heap_min":113236,"heap_block":106496
```

The largest free contiguous block is ~104 KB, below the 128 KB a 4 s buffer needs.
There is no PSRAM on this board. The conclusion is not "use a smaller buffer" but
"do not buffer the utterance": audio has to stream through a bounded ring, and
the device must report underrun instead of growing the buffer. The protocol
already requires this; the measurement shows it is not negotiable.

## Signal quality

Captured in a normal room at the BSP's 30 dB microphone PGA:

- `rms` ≈ 5,300–7,600 depending on ambient noise
- `dc` ≈ 810, about 2.5% of full scale — a DC offset, typical of the ES8311 ADC
- `clips` ≈ 526 of 16,000 samples (3.3%) within 1.5 dB of full scale

Two consequences for the server side:

1. **Block the DC offset before ASR.** It is a constant bias, not signal.
2. **Watch for clipping.** 3.3% of samples clipped is enough to hurt recognition
   in a loud room. The knob is the microphone PGA in `components/bsp/src/bsp_audio.c`
   (`esp_codec_dev_set_in_gain(s_dev, 30.0f)`); 24 dB is the first thing to try if
   ASR quality disappoints. Changing it needs a re-measurement, not a guess.

## Known gap

The self test proves the capture path, the framing, and that `bsp_audio_write`
accepts every frame. It does **not** prove the speaker makes sound: a successful
I2S write only means the bytes left the SoC. Whether the ES8311 DAC and the power
amplifier are actually driving the speaker can only be confirmed by a human
standing next to the device. That confirmation is still outstanding.

If it turns out to be silent, the order to investigate is: amplifier enable
pin / PA voltage, then output volume (`bsp_audio_set_volume`, currently 60), then
the ES8311 DAC power-up sequence.