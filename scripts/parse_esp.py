import struct, sys, hashlib

TYPES = {0:'app',1:'data'}
APP_SUB = {0:'factory',0x10:'ota_0',0x11:'ota_1',0x20:'test'}
DATA_SUB = {0:'ota',1:'phy',2:'nvs',3:'coredump',4:'nvs_keys',5:'efuse',6:'undefined',
            0x80:'esphttpd',0x81:'fat',0x82:'spiffs',0x83:'littlefs'}

def cstr(b):
    return b.split(b'\x00')[0].decode('utf-8', 'replace')

print('=== 分区表 (flash 0x8000) ===')
data = open(sys.argv[1],'rb').read()
print(f"{'label':16} {'type':6} {'subtype':10} {'offset':>10} {'size':>10}  size_human")
for i in range(0, len(data), 32):
    e = data[i:i+32]
    if len(e) < 32:
        break
    magic = struct.unpack('<H', e[0:2])[0]
    if magic == 0xEBEB:
        print('(MD5 校验分区)')
        continue
    if magic != 0x50AA:
        if e == b'\xff'*32:
            break
        print(f'  [未知条目 magic=0x{magic:04x}]')
        break
    ptype, psub = e[2], e[3]
    off, size = struct.unpack('<II', e[4:12])
    label = cstr(e[12:28])
    tname = TYPES.get(ptype, str(ptype))
    if ptype == 0:
        sname = APP_SUB.get(psub, hex(psub))
    elif ptype == 1:
        sname = DATA_SUB.get(psub, hex(psub))
    else:
        sname = hex(psub)
    human = f'{size/1024:.0f}K' if size < 1024*1024 else f'{size/1048576:.1f}M'
    print(f'{label:16} {tname:6} {sname:10} 0x{off:08x} {size:>10}  {human}')

print()
print('=== 固件镜像头 (flash 0x10000) ===')
img = open(sys.argv[2],'rb').read()
if img[0] != 0xE9:
    print(f'  不是有效镜像: 首字节 0x{img[0]:02x}')
    sys.exit(1)
seg_count = img[1]
spi_mode = {0:'QIO',1:'QOUT',2:'DIO',3:'DOUT'}.get(img[2], img[2])
speed_size = img[3]
flash_sizes = {0:'1MB',1:'2MB',2:'4MB',3:'8MB',4:'16MB'}
freq = {0:'40MHz',1:'26MHz',2:'20MHz',0xf:'80MHz'}.get(speed_size & 0xf, speed_size & 0xf)
fsize = flash_sizes.get((speed_size >> 4) & 0xf, (speed_size >> 4) & 0xf)
entry = struct.unpack('<I', img[4:8])[0]
chip_id = struct.unpack('<H', img[12:14])[0]
chip_names = {0:'ESP32',2:'ESP32-S2',5:'ESP32-C3',9:'ESP32-S3',12:'ESP32-C2',13:'ESP32-C6',16:'ESP32-H2',18:'ESP32-P4'}
print(f'  段数={seg_count}  SPI模式={spi_mode}  flash={fsize}@{freq}  入口=0x{entry:08x}  芯片={chip_names.get(chip_id, chip_id)}')

# 第一个段 = esp_app_desc_t
seg_off = struct.unpack('<I', img[24+4:24+8])[0]
desc = img[24+8:24+8+256]
magic = struct.unpack('<I', desc[0:4])[0]
print(f'  第一段地址=0x{seg_off:08x}  应用描述符 magic=0x{magic:08x}')
if magic == 0xABCD5432:
    secure_ver = struct.unpack('<I', desc[4:8])[0]
    print(f'  项目名称   : {cstr(desc[48:80])}')
    print(f'  固件版本   : {cstr(desc[16:48])}')
    print(f'  构建时间   : {cstr(desc[112:144])} {cstr(desc[80:112])}')
    print(f'  IDF 版本   : {cstr(desc[144:176])}')
    print(f'  安全版本号 : {secure_ver}')
    print(f'  ELF SHA256 : {desc[176:208].hex()}')
else:
    print('  未找到 IDF 应用描述符（可能是 Arduino/其他框架）')