# -*- coding: utf-8 -*-
"""分析 AI passport (ESP32-C3) 的 8MB 全片备份：分区/文件系统/端点/密钥/NVS 结构"""
import sys, os, re, struct, hashlib, math, collections, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

DUMP = r'D:\dsh-work\flash\full-backup.bin'
OUTDIR = r'D:\dsh-work\analysis'
os.makedirs(OUTDIR, exist_ok=True)

TYPE_NAME = {0: 'app', 1: 'data'}
APP_SUB = {0: 'factory', 0x10: 'ota_0', 0x11: 'ota_1', 0x20: 'test'}
DATA_SUB = {0: 'ota', 1: 'phy', 2: 'nvs', 3: 'coredump', 4: 'nvs_keys', 5: 'efuse',
            0x80: 'esphttpd', 0x81: 'fat', 0x82: 'spiffs', 0x83: 'littlefs'}

report = []
def out(s=''):
    print(s)
    report.append(str(s))

def entropy(b):
    if not b:
        return 0.0
    c = collections.Counter(b)
    n = len(b)
    return -sum((v / n) * math.log2(v / n) for v in c.values())

def printable_runs(b, minlen=4):
    res, cur = [], bytearray()
    for x in b:
        if 32 <= x < 127:
            cur.append(x)
        else:
            if len(cur) >= minlen:
                res.append(cur.decode('latin-1'))
            cur = bytearray()
    if len(cur) >= minlen:
        res.append(cur.decode('latin-1'))
    return res

def detect_fs(b):
    head = b[:8192]
    if b.count(0xFF) == len(b):
        return '空（全 0xFF）'
    if b'littlefs' in head:
        return 'LittleFS'
    if b.find(b'FAT32') > 0 or b.find(b'FAT16') > 0:
        return 'FAT'
    if b.find(b'\xAA\x55') == 0 or (len(b) >= 4096 and struct.unpack('<I', b[0:4])[0] in (0xFFFFFFFE, 0xFFFFFFFC, 0xFFFFFFF8, 0xFFFFFFFF) and b[8] == 0xFE):
        return 'NVS'
    if head.find(b'spiffs') >= 0:
        return 'SPIFFS'
    return '未知/自定义'

# ---------- 1. 载入 ----------
if not os.path.exists(DUMP):
    print(f'缺少备份文件 {DUMP}')
    sys.exit(1)
data = open(DUMP, 'rb').read()
out(f'备份文件 : {DUMP}')
out(f'大小     : {len(data)} bytes ({len(data)/1048576:.2f} MB)')
out(f'SHA256   : {hashlib.sha256(data).hexdigest()}')
out(f'MD5      : {hashlib.md5(data).hexdigest()}')
out()

# ---------- 2. 分区表 ----------
out('=' * 78)
out('分区表与每区概况')
out('=' * 78)
parts = []
for i in range(0, 0x1000, 32):
    e = data[0x8000 + i: 0x8000 + i + 32]
    if len(e) < 32:
        break
    magic = struct.unpack('<H', e[0:2])[0]
    if magic != 0x50AA:
        break
    ptype, psub = e[2], e[3]
    off, size = struct.unpack('<II', e[4:12])
    label = e[12:28].split(b'\x00')[0].decode('utf-8', 'replace')
    parts.append((label, ptype, psub, off, size))

out(f"{'label':14}{'type/sub':16}{'offset':>10}{'size':>10}  {'MD5(前16)':18}{'熵':>5}  文件系统")
for label, ptype, psub, off, size in parts:
    body = data[off:off + size]
    tn = TYPE_NAME.get(ptype, str(ptype))
    sn = (APP_SUB if ptype == 0 else DATA_SUB).get(psub, hex(psub))
    md5 = hashlib.md5(body).hexdigest()[:16]
    ent = entropy(body)
    out(f'{label:14}{tn + "/" + sn:16}{hex(off):>10}{size:>10}  {md5:18}{ent:5.2f}  {detect_fs(body)}')
out()

# ---------- 3. 网络端点 ----------
URL_RE = re.compile(r'(?:https?|wss?|mqtts?)://[A-Za-z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]{3,140}')
DOM_RE = re.compile(r'\b[a-z0-9][a-z0-9\-]{1,40}\.(?:com|cn|net|org|io|me|xyz|top|dev|app|cc|site|shop|tech|ai|co|cloud|link)\b')
IP4_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{2,5})?\b')
NOISE = ('w3.org', 'schemas.', 'xmlns', 'example.com', 'github.com', 'espressif.com',
         'apache.org', 'opensource.org', 'openssl.org', 'linux.org', 'kernel.org')

urls = collections.Counter(); doms = collections.Counter(); ips = collections.Counter()
for label, ptype, psub, off, size in parts:
    body = data[off:off + size]
    for s in printable_runs(body, 6):
        for m in URL_RE.findall(s):
            urls[m] += 1
        for m in DOM_RE.findall(s.lower()):
            if not any(n in m for n in NOISE):
                doms[m] += 1
        for m in IP4_RE.findall(s):
            if not m.startswith(('0.', '127.', '255.')) and m not in ('1.0.0',):
                ips[m] += 1

out('=' * 78)
out('网络端点（固件里硬编码的服务器地址）')
out('=' * 78)
out('--- URL ---')
for u, c in urls.most_common(40):
    out(f'  {u}   (x{c})')
if not urls:
    out('  （无）')
out('--- 域名 ---')
for d, c in doms.most_common(30):
    out(f'  {d}   (x{c})')
out('--- IPv4[:port] ---')
for a, c in ips.most_common(20):
    out(f'  {a}   (x{c})')
if not ips:
    out('  （无）')
out()

# ---------- 4. 敏感关键字 ----------
KW = ['token', 'secret', 'password', 'passwd', 'apikey', 'api_key', 'authorization',
      'bearer', 'aes', 'hmac', 'private_key', 'client_secret', 'access_key', 'device_secret']
out('=' * 78)
out('敏感关键字命中（值已截断，避免整段凭据刷屏）')
out('=' * 78)
hits = collections.Counter()
samples = {}
for label, ptype, psub, off, size in parts:
    for s in printable_runs(data[off:off + size], 8):
        low = s.lower()
        for k in KW:
            if k in low:
                hits[k] += 1
                if k not in samples and len(s) < 120:
                    samples[k] = (label, s[:100])
for k, c in hits.most_common():
    lab, smp = samples.get(k, ('-', ''))
    out(f'  {k:16} x{c:<4} 例[{lab}]: {smp}')
if not hits:
    out('  （无命中）')
out()

# ---------- 5. 证书 ----------
certs = data.count(b'-----BEGIN CERTIFICATE-----')
out(f'内嵌 TLS 证书: {certs} 张')
keys = data.count(b'-----BEGIN')
if keys - certs > 0:
    out(f'⚠ 发现非证书的 PEM 私钥块: {keys - certs} 处（需重点核查）')
out()

# ---------- 6. NVS 解析 ----------
NVS_TYPES = {0x01: 'U8', 0x02: 'U16', 0x04: 'U32', 0x08: 'U64', 0x11: 'I8',
             0x12: 'I16', 0x14: 'I32', 0x18: 'I64', 0x21: 'STR', 0x41: 'BLOB',
             0x42: 'BLOB_DATA', 0x48: 'BLOB_IDX'}
MASK_KW = ('pass', 'ssid', 'token', 'key', 'secret', 'url', 'pwd', 'cred')

def parse_nvs(body):
    found = []
    for pbase in range(0, len(body), 4096):
        page = body[pbase:pbase + 4096]
        if len(page) < 4096:
            break
        state = struct.unpack('<I', page[0:4])[0]
        if state not in (0xFFFFFFFE, 0xFFFFFFFC, 0xFFFFFFF8):
            continue
        if page[8] != 0xFE:
            continue
        for ebase in range(32, 4096, 32):
            ent = page[ebase:ebase + 32]
            ns, etype, span, chunk = ent[0], ent[1], ent[2], ent[3]
            if etype == 0xFF or span == 0 or span > 8:
                continue
            key = ent[8:24].split(b'\x00')[0]
            if not key or not all(32 <= c < 127 for c in key):
                continue
            datalen = span * 32 - 8
            val = ent[24:24 + min(datalen, 32)]
            found.append((ns, etype, key.decode('latin-1'), span, val))
    return found

out('=' * 78)
out('NVS 命名空间与键名（字符串值已脱敏，只给长度）')
out('=' * 78)
for label, ptype, psub, off, size in parts:
    if not (ptype == 1 and psub in (2,)) and label not in ('netcfg', 'cardid'):
        continue
    body = data[off:off + size]
    entries = parse_nvs(body)
    if not entries:
        out(f'[{label}] 未解析出条目')
        continue
    nsmap = {}
    for ns, etype, key, span, val in entries:
        if ns == 0:
            nsmap[val[0]] = key
    out(f'[{label}]  命名空间: {list(nsmap.values())}')
    for ns, etype, key, span, val in entries:
        if ns == 0:
            continue
        tname = NVS_TYPES.get(etype, hex(etype))
        if etype == 0x21:
            sval = val.split(b'\x00')[0].decode('latin-1', 'replace')
            if any(k in key.lower() for k in MASK_KW):
                shown = (sval[:3] + '*') if sval else ''
                out(f'   {nsmap.get(ns, ns)}.{key:22} {tname:6} len={len(sval):3}  值={shown}')
            else:
                out(f'   {nsmap.get(ns, ns)}.{key:22} {tname:6} len={len(sval):3}  值={sval[:60]}')
        else:
            out(f'   {nsmap.get(ns, ns)}.{key:22} {tname:6} span={span}')
out()

# ---------- 7. 私有分区探针 ----------
out('=' * 78)
out('私有分区内容探针（自定类型的数据区）')
out('=' * 78)
for label, ptype, psub, off, size in parts:
    if label in ('nvs', 'phy_init', 'netcfg', 'cardid', 'factory', 'recovery', 'assets'):
        continue
    body = data[off:off + size]
    runs = printable_runs(body, 6)
    uniq = [s for s, _ in collections.Counter(runs).most_common(8)]
    out(f'[{label}] {size} bytes, 熵={entropy(body):.2f}, 可读串 {len(runs)} 条')
    for s in uniq:
        out(f'    {s[:90]}')
out()

with open(os.path.join(OUTDIR, 'firmware-report.txt'), 'w', encoding='utf-8') as f:
    f.write('\n'.join(report))
print(f'\n报告已保存: {os.path.join(OUTDIR, "firmware-report.txt")}')