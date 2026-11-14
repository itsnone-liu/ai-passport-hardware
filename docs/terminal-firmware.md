# Thin-terminal firmware (`dsh terminal`)

A self-built firmware that turns the Passport into a **thin terminal**: the device only
captures/renders, the host (dsh on the Ubuntu server) does all reasoning and output.
This document records what was built, how it was verified, and how to rebuild it.

## Why a thin terminal instead of the stock xiaozhi stack

The stock firmware is a full voice assistant: it owns ASR/TTS/LLM session state, MCP tool
dispatch, Wi-Fi provisioning and OTA. For a "screen + buttons + audio" satellite of dsh,
almost all of that is duplicated state that has to be kept in sync with the host.

Measured constraints (from the hardware survey):

| Item | Value | Consequence |
|---|---|---|
| SoC | ESP32-C3, single core, 160 MHz | no on-device ASR/LLM |
| PSRAM | none | small working set only |
| Free SRAM at boot | ~142 KB (stock), ~173 KB (thin) | LVGL + one audio stream fits |
| App size | 703,104 B (USB) / 1,256,368 B (+Wi-Fi) vs ~1.5 MB stock | room to grow in one slot |
| Flash | 8 MB, XMC | assets no longer needed |

ASR/TTS never ran on the device in the stock firmware either — only wake word (ESP-SR
WakeNet), AFE/AEC/VAD and Opus did. Moving the session to the user's own server removes a
WAN round trip and is strictly faster.

## Architecture

```
        USB (data-capable cable)                    LAN / Wi-Fi
dsh  ──────────────────────────►  Passport  ◄──────────────────────  Ubuntu server
host                             ESP32-C3                            (ASR/LLM/TTS)
```

* **M1 (done)**: control channel over USB-Serial/JTAG. No Wi-Fi, no credentials, no OTA.
* **M3 (done)**: the same framing over a plain TCP socket; the device dials out to the server.

Both transports are live at the same time and share one command layer
(`main/terminal_link.c`): a line arriving on USB and a line arriving on the socket go through
the identical parser, and every reply/event is written to every attached transport.

### Why TCP and not WebSocket

The protocol is already "one JSON object per line over a byte stream". A bare TCP socket needs
zero extra components (lwIP ships with ESP-IDF), keeps the image ~1.26 MB, and leaves reconnect
policy entirely under our control. `esp_websocket_client` is no longer part of the IDF 5.5.3
component set — it has to be pulled from the component registry — and WebSocket only buys
something when the device must cross an HTTP proxy or be dialled by a browser. If that need
appears, it becomes one more sink next to `usb_sink` / `tcp_sink`; the command layer is untouched.

The device is always the **client**. It never listens on a port, so no port-forwarding and
no inbound attack surface. Firmware updates go over USB only — the ESP32-C3 USB-Serial/JTAG
peripheral lives in ROM, so the device is recoverable even with corrupted firmware.

## Wire protocol

One JSON object per line (`\n` terminated) over a raw byte stream — the USB-Serial/JTAG CDC
channel or a TCP socket. The same parser serves both, so adding the network transport did not
touch any command implementation.

Requests:

```json
{"id":1,"cmd":"get_status"}
{"id":2,"cmd":"show_text","text":"build 42 ok\nlink=net 4s"}
{"id":3,"cmd":"set_volume","value":45}
{"id":4,"cmd":"set_brightness","value":35}
{"id":5,"cmd":"ping"}
{"id":6,"cmd":"get_net"}
{"id":7,"cmd":"set_net","ssid":"lab-5g","password":"...","host":"192.168.1.10","port":8080}
```

Responses:

```json
{"id":1,"ok":true,"cmd":"status","battery":94,"mv":4120,"volume":60,"brightness":80,"uptime":10,"link":"usb"}
{"id":3,"ok":true,"cmd":"set_volume","volume":45}
{"id":6,"ok":true,"cmd":"net","ssid":"lab-5g","host":"192.168.1.10","port":8080,"password_set":true}
{"ok":false,"err":"bad json"}
{"ok":false,"err":"unknown cmd"}
```

Unsolicited events:

```json
{"event":"ready","link":"usb"}
{"event":"ready","link":"net"}
{"event":"key","btn":"ok","ev":"click"}
```

`btn` is `up` / `down` / `ok`; `ev` is `press` / `click` / `double` / `long` / `release`.
A `ready` event is emitted once per transport as it comes up, so a host can tell whether the
device is on USB, on the network, or both. Hosts must ignore serial lines that do not start
with `{` — ESP-IDF logs share the stream.

## Counter semantics: per-turn or since boot

`get_audio` mixes both kinds, and that cost a full debugging round between the two agents: a
conservation check compared a **per-turn** `played` against a **cumulative** `rx`, got
`played +0` while `rx +114`, and concluded the implementation was losing frames. It was not.
The rule below is the fix, so nobody has to infer the convention from behaviour.

| Field | Scope | Reset at |
|---|---|---|
| `audio_rx_frames`, `audio_rx_end`, `audio_tx_frames` | since boot | restart |
| `push_ok`, `rej_idle`, `rej_turn`, `rej_size`, `play_mismatch` | since boot | restart |
| `played_total` | since boot | restart |
| `played`, `play_overflows`, `play_underruns` | **per turn** | `playback.start` |
| `frames`, `dropped`, `rms`, `peak` (capture) | **per turn** | `capture.start` |
| `turns`, `read_fail`, `overflows` (capture) | since boot | restart |

Two consequences worth stating outright:

- A conservation check must be written on **one** axis. `push_ok + rej_idle + rej_turn +
  rej_size` equals the `audio_rx_frames` delta; `played_total` delta equals what actually
  reached I2S. Comparing across axes always "fails".
- Any counter that resets is also a restart detector. If `played` drops while `boot` is
  unchanged, that is a real bug; if `boot` moved, it was a restart and the counters are
  simply new. `boot`/`reset`/`prev_heap_min` in `get_status` exist so this is a field
  comparison rather than an argument.

## Network configuration

Compile-time defaults live in `menuconfig → dsh terminal` (`TERMINAL_WIFI_SSID`,
`TERMINAL_WIFI_PASSWORD`, `TERMINAL_SERVER_HOST`, `TERMINAL_SERVER_PORT`). On first boot they
are written to NVS (namespace `dshtc`) and from then on NVS wins, so raising the firmware
defaults never silently re-points an already deployed device.

To re-point a device in the field, no re-flash is needed — send `set_net` over USB:

```powershell
python dsh_send.py COM3 '@cmd-setnet.txt' 4
# cmd-setnet.txt: {"id":7,"cmd":"set_net","ssid":"lab-5g","password":"...","host":"192.168.1.10","port":8080}
```

The device persists the new values, drops the socket, re-associates and reconnects. Omitted
fields keep their current value, so changing only the server host is a one-field edit. The
response never echoes the password; `get_net` reports `password_set` instead.

## Layout

```
main/terminal_main.c    app entry: init order, key queue, 1 Hz status tick
main/terminal_link.c    transport-neutral core: cJSON parse, commands, events, fan-out
main/terminal_proto.c   USB-Serial/JTAG transport (line framing -> terminal_link_dispatch)
main/terminal_net.c     Wi-Fi STA + TCP transport (dial out, reconnect, NVS config)
main/terminal_ui.c      self-drawn LVGL UI (no reuse of the demo menu shell)
main/Kconfig.projbuild  SSID / password / server host / server port
main/CMakeLists.txt     SRCS swapped: demo_*.c and main.c are no longer compiled
components/bsp/         unchanged vendor board support package
```

`components/bsp` stays untouched — reusable board logic lives there, per the repository
guidelines; pages and state machines live in `main`.

The UI is a 240x320 dark terminal: title bar (`dsh` + link badge: `--` / `USB OK` / `NET OK`),
a wrapped message area, and three status cells (battery / volume / uptime). ASCII only — the
default Montserrat fonts carry no CJK glyphs.

## Build and flash

```powershell
$env:IDF_TOOLS_PATH = 'D:\esp\espressif'
$env:TEMP = 'D:\esp\tmp'; $env:TMP = 'D:\esp\tmp'
. 'D:\esp\esp-idf-v5.5.3\export.ps1'
Set-Location 'D:\esp\ai-passport'
idf.py build
idf.py -p COM3 flash          # app + bootloader + partition table, app ~1.26 MB
```

`BT_ENABLED=n` in `sdkconfig.defaults`: the thin terminal never uses BLE, and dropping it frees
flash and removes Wi-Fi/BT coexistence overhead. Wi-Fi STA plus lwIP is what pushes the image
from 703 KB (USB only) to 1,256,368 B — the single 7.9 MB factory slot is still 85% free.

Driving it from the host (one command, prints the reply):

```powershell
python dsh_send.py COM3 '@cmd-status.txt' 3      # file holds: {"id":1,"cmd":"get_status"}
```

Pass JSON through a **file**, not an inline argument: PowerShell 5.1 strips the double
quotes when handing a native process its argv, so the device receives `{id:1,...}` and
answers `bad json`.

## Verification log

### M1 — USB control channel (emulated + real hardware)

Emulated (PassportSim 1.0.3, ELF SHA256 `d96716de982d4498…`): boot matched `until_ui_settled`,
no tripwires, no fidelity entries; injected commands round-tripped; screenshots in
`sim/terminal-boot.png` and `sim/terminal-dynamic.png`.

On real hardware (after flashing the self-built image):

```
I (162) dsh: dsh terminal 启动
I (165) bsp_i2c:   发现设备 @ 0x18  <- ES8311 音频 codec
I (176) bsp_i2c:   发现设备 @ 0x63  <- CW2017 电量计
I (310) bsp_disp: 显示就绪 240x320
I (482) bsp_batt: CW2017 ready: SOC=94%, voltage=4120 mV
I (494) bsp_btn: 按键就绪:ADC1_CH0 三键分压
I (512) ui: 终端界面已建立
I (589) proto: USB 控制通道就绪（一行一条 JSON）
I (590) dsh: 就绪: battery=1 audio=1 usb=1
```

```
→ {"id":1,"cmd":"get_status"}
← {"id":1,"ok":true,"cmd":"status","battery":94,"mv":4120,"volume":60,"brightness":80,"uptime":10,"link":"usb"}
→ {"id":2,"cmd":"show_text","text":"dsh terminal\nconnected over USB"}
← {"id":2,"ok":true,"cmd":"show_text"}
→ {"id":4,"cmd":"set_brightness","value":35}
← {"id":4,"ok":true,"cmd":"set_brightness","brightness":35}
```

### M3 — network transport (emulated + real hardware)

Emulator recipe — a virtual AP, a host port bridged into the device's LAN, and a throwaway
dsh stand-in (`tcp_lab_server.py`, ~90 lines, no dependencies):

```
passportsim start psim
passportsim wifi_ap --ssid dsh-lab --auth wpa2-psk --key dsh-lab-pass
passportsim net_http --op bridge --port 8080 --host-port 18080
passportsim clock take --owner agent
passportsim run --for 15000ms
```

Device side (from the emulated serial stream) — DHCP through the virtual LAN, outbound dial,
server commands applied, device state returned:

```
I (5018) net: 拿到 IP 10.23.0.100
I (5345) net: 已连上服务器 host.emu.internal:8080
{"event":"ready","link":"net"}
{"id":100,"ok":true,"cmd":"show_text"}
{"id":101,"ok":true,"cmd":"status","battery":100,"mv":4200,"volume":60,"brightness":80,"uptime":5}
```

Pressing a key closes the loop device → server → device; the server's reply becomes the on-screen
message (`sim/m3-net.png`, `sim/m3-key.png`):

```
[srv] <- {"event":"key","btn":"ok","ev":"click"}
[srv] -> {"id": 103, "cmd": "show_text", "text": "key ok click\nfrom dsh server"}
[srv] <- {"id":103,"ok":true,"cmd":"show_text"}
```

`set_net` was verified by re-pointing a running device at a second AP and a second server:

```
I (13392) net: 网络参数已更新: ssid="dsh-lab-2" server=host.emu.internal:8081
{"id":9,"ok":true,"cmd":"set_net"}
I (14845) net: 已连上服务器 host.emu.internal:8081
{"event":"ready","link":"net"}
```

Simulator fidelity for this image: `"features":{"ble":"not linked","wifi":"bound"}`,
`"hle":{"bound":"idf-5.5.3/wifi"}`, `tripwires_hit":[]`, `"result":"pass"`.

On real hardware, the same image boots, keeps USB responsive and retries Wi-Fi on a 2.4 s loop
when the configured SSID is absent — no panic, no watchdog, `dsh_net` task stable over a 22 s
capture:

```
I (573) bsp_batt: CW2017 ready: SOC=94%, voltage=4118 mV
I (679) USB 载体就绪 / {"event":"ready","link":"usb"}
I (704) net: 连接 Wi-Fi "dsh-lab"，服务器 host.emu.internal:8080
W (3159) net: Wi-Fi 断开，重连中   (repeats every ~2.4 s, as designed)
→ {"id":10,"cmd":"get_net"}
← {"id":10,"ok":true,"cmd":"net","ssid":"dsh-lab","host":"host.emu.internal","port":8080,"password_set":true}
```

Joining the deployment Wi-Fi and reaching the real dsh server is a `set_net` away; the emulated
run above exercises exactly that code path.

### Real hardware: the 5 GHz trap

The first real deployment attempt failed with a repeat-every-2.4 s disconnect and no reason code
in the log — a blind spot, since `WIFI_EVENT_STA_DISCONNECTED` carries the reason. Logging it
(`reason=201` = AP never seen, `15`/`204` = handshake failed/wrong password, `205` = rejected by
AP) plus a `scan` command turned the guess into data:

```
→ {"id":20,"cmd":"scan"}
← {"id":20,"ok":true,"cmd":"scan","count":12,"aps":[
     {"ssid":"2005","rssi":-41},{"ssid":"5-2105","rssi":-55},{"ssid":"5-1805","rssi":-63},
     {"ssid":"5-1904","rssi":-63},{"ssid":"YYY-5G","rssi":-67},{"ssid":"2004-5G","rssi":-67},
     {"ssid":"2006-5G","rssi":-74},...,{"ssid":"JSCN-VODTV-3C6704","rssi":-79}]}
```

The requested SSID was simply **absent**, while the same router's 2.4 GHz SSID sat at -41 dBm,
the strongest AP in range. ESP32-C3 has a 2.4 GHz-only radio: a `-5G` SSID is unreachable no
matter how correct the password is. `scan` distinguishes "wrong password" from "wrong band" in
one round trip, which the disconnect reason alone cannot always do.

Once pointed at the 2.4 GHz SSID the association is immediate:

```
I (703) net: 连接 Wi-Fi "2005"，服务器 ...
I (856) wifi:state: init -> auth (0xb0)
I (888) wifi:state: assoc -> run (0x10)
I (923) wifi:connected with 2005, aid = 14, channel 4, 40U, bssid = 68:77:da:08:5c:e8
I (4136) esp_netif_handlers: sta ip: 192.168.2.107, mask: 255.255.255.0, gw: 192.168.2.1
I (4137) net: 拿到 IP 192.168.2.107
```

Credentials are stored in NVS only; they are deliberately absent from this repository.

### Two bugs the real network exposed

Emulation never showed either of these, because the emulator's LAN always accepts connections
and never writes non-JSON bytes. Both were found within minutes of pointing the device at a
real host.

**1. A blocked port made the device go silent.** Windows Firewall silently drops inbound SYNs;
a blocking `connect()` then sits in SYN retransmission for tens of seconds. The device printed
nothing at all — indistinguishable from a hang. `connect()` is now non-blocking with a 5 s
`select()` deadline, and every failure is logged with its errno:

```
I (7354) net: 已连上服务器 192.168.2.1:80          (works)
W (9210) net: 连接 192.168.2.108:18080 超时（5 s）  (silently dropped)
W (17214) net: 连接 192.168.2.108:18080 超时（5 s）
```

`TCP_NODELAY` and `SO_KEEPALIVE` are set for the same reason they matter to any terminal:
key presses must not wait on Nagle, and a half-dead peer must eventually be noticed.

**2. A non-JSON peer caused a reply storm.** Pointed at the LAN gateway's HTTP port, the router
answered with HTTP bytes; the device treated every line as a command and answered
`{"ok":false,"err":"bad json"}` dozens of times per connection, drowning the link. The protocol
already told hosts to ignore lines that do not start with `{` — the device now applies the same
rule in the other direction: anything not starting with `{` is dropped silently, and only a line
that *does* start with `{` and still fails to parse gets an error reply. Verified after the fix:
0 `bad json` lines over a 15 s capture against the same HTTP port.

This also keeps the USB transport clean, because ESP-IDF log output shares the USB-Serial/JTAG
stream with the protocol.

Note: `rst:0x15 (USB_UART_CHIP_RESET)` in a capture is the host's DTR/RTS pulse resetting the
chip, not a crash. On ESP32-C3 the USB-Serial/JTAG peripheral lives in ROM, so this is also the
reason firmware can always be recovered over USB.

## Rollback

The stock firmware and the factory flash dump remain available; any state of this device is
recoverable through ROM download mode over USB. The device identity (`cardid`) is treated as
disposable and is **not** preserved across re-flashes.