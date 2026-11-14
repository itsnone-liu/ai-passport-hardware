# -*- coding: utf-8 -*-
"""构造模拟器可接受的 sanitized 镜像：只含 0x0..factory 区末尾（bootloader+分区表+phy+app），
不含 nvs / cardid / 任何素材或凭据分区。"""
import io, sys, hashlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

src = r'D:\dsh-work\flash\full-backup.bin'
d = open(src, 'rb').read()
print('source len :', len(d), hex(len(d)))

# 从分区表里取 factory 区的结束位置
import struct
app_end = None
for i in range(0, 0x1000, 32):
    e = d[0x8000 + i:0x8000 + i + 32]
    if struct.unpack('<H', e[0:2])[0] != 0x50AA:
        break
    label = e[12:28].split(b'\x00')[0].decode('utf-8', 'replace')
    off, size = struct.unpack('<II', e[4:12])
    if label == 'factory':
        app_end = off + size
print('factory ends:', hex(app_end) if app_end else None)

merged = bytearray(d[:app_end])
# 清空 nvs(0x9000..0xF000)：Wi-Fi 凭据 / MQTT token / BLE 绑定，否则模拟器拒绝加载
merged[0x9000:0xF000] = b'\xff' * 0x6000
print('nvs erased :', merged[0x9000:0xF000].count(0xFF) == 0x6000)
merged = bytes(merged)
out = r'D:\dsh-work\flash\merged-app.bin'
open(out, 'wb').write(merged)
print('written    :', out, len(merged), 'bytes')
print('sha256     :', hashlib.sha256(merged).hexdigest())
print('contains nvs region? :', app_end > 0x9000)
print('head magic :', merged[0:1].hex(), '(0xe9 = esp image)')