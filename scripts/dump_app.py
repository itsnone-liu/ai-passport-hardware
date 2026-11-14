import struct, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def cstr(b):
    return b.split(b'\x00')[0].decode('utf-8', 'replace')

CHIPS = {0:'ESP32',2:'ESP32-S2',5:'ESP32-C3',9:'ESP32-S3',12:'ESP32-C2',13:'ESP32-C6',16:'ESP32-H2',18:'ESP32-P4'}

for path in sys.argv[2:]:
    img = open(path,'rb').read()
    print(f'--- 镜像: {path} ---')
    if not img or img[0] != 0xE9:
        print(f'  首字节 0x{img[0] if img else 0:02x}，不是 ESP 应用镜像（可能为空或非 app 分区）')
        print()
        continue
    speed_size = img[3]
    print(f'  段数={img[1]}  SPI={ {0:"QIO",1:"QOUT",2:"DIO",3:"DOUT"}.get(img[2],img[2]) }  '
          f'flash={ {0:"1MB",1:"2MB",2:"4MB",3:"8MB",4:"16MB"}.get((speed_size>>4)&0xf,(speed_size>>4)&0xf) }'
          f'@{ {0:"40MHz",0xf:"80MHz"}.get(speed_size&0xf,str(speed_size&0xf)) }  '
          f'入口=0x{struct.unpack("<I",img[4:8])[0]:08x}  芯片={CHIPS.get(struct.unpack("<H",img[12:14])[0],"?")}')
    desc = img[24+8:24+8+256]
    if struct.unpack('<I', desc[0:4])[0] != 0xABCD5432:
        print('  无 IDF 应用描述符')
        print()
        continue
    print(f'  version      : {cstr(desc[16:48])}')
    print(f'  project_name : {cstr(desc[48:80])}')
    print(f'  build time   : {cstr(desc[80:112])}')
    print(f'  build date   : {cstr(desc[112:144])}')
    print(f'  idf_ver      : {cstr(desc[144:176])}')
    print(f'  secure_ver   : {struct.unpack("<I", desc[4:8])[0]}')
    print(f'  elf_sha256   : {desc[176:208].hex()}')
    print(f'  原始字节 144..208: {desc[144:208].hex()}')
    print()