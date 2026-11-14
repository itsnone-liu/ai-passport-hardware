# Minimal ESP32 image / partition-table inspector
import sys, struct

path = sys.argv[1]
d = open(path, 'rb').read()
print("file      : %s" % path)
print("size      : %d bytes (0x%X)" % (len(d), len(d)))

print("\n--- image header @0x0 ---")
print("magic      : 0x%02X %s" % (d[0], "(OK, ESP image)" if d[0] == 0xE9 else "(NOT an ESP image!)"))
if d[0] == 0xE9:
    print("segments   : %d" % d[1])
    print("spi_mode   : %d" % d[2])
    print("spi_speed  : %d" % (d[3] & 0x0F))
    print("spi_size   : %d  (0=1MB 1=2MB 2=4MB 3=8MB 4=16MB)" % (d[3] >> 4))
    print("entry_addr : 0x%08X" % struct.unpack("<I", d[4:8])[0])

print("\n--- partition table @0x8000 ---")
off = 0x8000
found = 0
for i in range(95):
    e = d[off + i * 32: off + i * 32 + 32]
    if len(e) < 32:
        break
    magic = struct.unpack("<H", e[0:2])[0]
    if magic != 0x50AA:
        break
    ptype = e[2]
    subtype = e[3]
    poff = struct.unpack("<I", e[4:8])[0]
    psize = struct.unpack("<I", e[8:12])[0]
    name = e[12:28].rstrip(b"\x00").decode("ascii", "replace")
    tname = {0: "app", 1: "data"}.get(ptype, str(ptype))
    print("  %-10s type=%d(%s) subtype=%d  off=0x%06X  size=0x%06X (%d KB)"
          % (name, ptype, tname, subtype, poff, psize, psize // 1024))
    found += 1
print("  entries: %d" % found)

app_parts = []
off = 0x8000
for i in range(95):
    e = d[off + i * 32: off + i * 32 + 32]
    if len(e) < 32 or struct.unpack("<H", e[0:2])[0] != 0x50AA:
        break
    if e[2] == 0:
        app_parts.append((e[12:28].rstrip(b"\x00").decode("ascii", "replace"),
                          struct.unpack("<I", e[4:8])[0]))

print("\n--- app images present in file ---")
for name, poff in app_parts:
    if poff + 1 <= len(d):
        m = d[poff]
        print("  %-8s @0x%06X magic=0x%02X %s" % (name, poff, m, "(valid)" if m == 0xE9 else "(empty/absent)"))

print("\n--- tail ---")
nonzero = len(d.rstrip(b"\xff"))
print("last non-0xFF byte at 0x%X" % nonzero)