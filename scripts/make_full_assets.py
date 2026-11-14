# -*- coding: utf-8 -*-
"""构造带素材分区、无凭据的完整镜像，供模拟器加载。
抹除：nvs(凭据)、cardid(卡密)、以及模拟器文档里点名的 cardid 窗口 0x356000..0x359FFF。
保留：bootloader、分区表、phy、factory app、petart、companion、imgava、assets、theme_cache。"""
import io, sys, hashlib, struct
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

src = r'D:\dsh-work\flash\full-backup.bin'
d = bytearray(open(src, 'rb').read())
print('source len:', len(d), hex(len(d)))

# 1) nvs
d[0x9000:0xF000] = b'\xff' * 0x6000
# 2) cardid 分区（我们实测的位置）
d[0x66A000:0x6AA000] = b'\xff' * 0x4000
# 3) 模拟器文档点名的 cardid 窗口
d[0x356000:0x35A000] = b'\xff' * 0x4000

# 保留到 theme_cache 结束（含全部素材），丢掉 cardid 之后的分区内容
end = 0x653000 + 94208
out_img = bytes(d[:end])
out = r'D:\dsh-work\flash\full-assets.bin'
open(out, 'wb').write(out_img)
print('written   :', out, len(out_img), 'bytes (end 0x%X)' % end)
print('sha256    :', hashlib.sha256(out_img).hexdigest())
print('nvs blank :', out_img[0x9000:0xF000].count(0xFF) == 0x6000)
print('cardid blank:', out_img[0x66A000:0x66A000+0x100].count(0xFF) == 0x100)
print('petart keeps:', out_img[0x2DE000:0x2DE010].hex())