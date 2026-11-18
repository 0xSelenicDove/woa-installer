"""Offline test of the GPT editor against a synthetic 4Kn disk image."""
import importlib.util, re, struct, sys, uuid, zlib
from pathlib import Path

spec = importlib.util.spec_from_file_location("w", Path(__file__).parent.parent / "woa_installer.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

SS, TOTAL = 4096, 256 * 1024 * 1024 * 1024 // 4096     # 256 GiB
ARRAY_SECS = 4
IMG = {}                                               # sparse sector store

def put(lba, data):
    for i in range(0, len(data), SS):
        IMG[lba + i // SS] = bytearray(data[i:i + SS].ljust(SS, b"\0"))

def get(lba, count):
    return b"".join(bytes(IMG.get(lba + i, bytearray(SS))) for i in range(count))

def entry(t, first, last, name):
    return (t.bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0)
            + name.encode("utf-16le").ljust(72, b"\0")).ljust(128, b"\0")

def hdr(me, alt, elba, arr, first, last):
    raw = bytearray(SS)
    struct.pack_into(w.Gpt.HDR, raw, 0, b"EFI PART", 0x10000, 92, 0, 0, me, alt, first, last,
                     uuid.uuid4().bytes_le, elba, 128, 128, zlib.crc32(arr))
    struct.pack_into("<I", raw, 16, zlib.crc32(bytes(raw[:92])))
    return bytes(raw)

first, last = 6, TOTAL - 6
ud = (last - 6 * 1024 * 1024 // 4096 * 0) - 1
ents = [entry(w.BASIC_TYPE, 6, 1000, "boot_a"), entry(w.BASIC_TYPE, 1001, ud, "userdata")]
ents += [b"\0" * 128] * 126
arr = b"".join(ents)
put(2, arr); put(1, hdr(1, TOTAL - 1, 2, arr, first, last))
put(TOTAL - 5, arr); put(TOTAL - 1, hdr(TOTAL - 1, 1, TOTAL - 5, arr, first, last))

class R:  # fake result
    def __init__(s, b): s.stdout = b
class Proc:
    def __init__(s, lba): s.lba, s.returncode = lba, 0
    def communicate(s, data): put(s.lba, data)

class FakePhone:
    dry = False; dev = "fake"; bydir = "/dev/block/by-name"; slot = "_a"
    def su(self, cmd, check=True, mutating=False):
        if "logical_block_size" in cmd: return str(SS)
        if cmd.endswith("/size"): return str(TOTAL * SS // 512)
        return ""
    def adb(self, *a, **k):
        m = re.search(r"skip=(\d+) count=(\d+)", a[1]); return R(get(int(m[1]), int(m[2])))
    def stream_in(self, cmd): return Proc(int(re.search(r"seek=(\d+)", cmd)[1]))

ph = FakePhone()
g = w.Gpt(ph, "sda")
new, notes, shrink = w.plan_layout(g, 512 << 20, 64 << 30)
print("\n".join(notes))
assert shrink
g.commit(new)
g2 = w.Gpt(ph, "sda")                      # re-parses + CRC-checks primary AND backup
names = {u["name"]: u for _, u in g2.used()}
assert set(names) == {"boot_a", "userdata", "esp", "win"}, names
assert names["userdata"]["last"] < names["esp"]["first"] < names["win"]["first"]
assert names["win"]["last"] <= last and (names["win"]["last"] - names["win"]["first"] + 1) * SS == 64 << 30
assert names["esp"]["first"] % 256 == 0 and names["win"]["first"] % 256 == 0
print("GPT edit OK")

# free-space path: userdata ends 100 GiB before the disk end -> nothing is shrunk
ents2 = list(ents); ents2[1] = entry(w.BASIC_TYPE, 1001, ud - 100 * 1024 ** 3 // SS, "userdata")
arr2 = b"".join(ents2)
put(2, arr2); put(1, hdr(1, TOTAL - 1, 2, arr2, first, last))
put(TOTAL - 5, arr2); put(TOTAL - 1, hdr(TOTAL - 1, 1, TOTAL - 5, arr2, first, last))
g3 = w.Gpt(ph, "sda")
_, notes3, shrink3 = w.plan_layout(g3, 512 << 20, 64 << 30)
assert not shrink3 and len(notes3) == 2
print("free-space path OK")

# FAT32 ESP geometry: the real-device failure was "16896 clusters too few for FAT32, need 65525"
sys.path.insert(0, str(Path(__file__).parent.parent))
for sector, size in ((4096, 500 << 20), (512, 500 << 20), (4096, 300 << 20), (512, 64 << 20)):
    bps, spc = w.fat32_args(sector, size)
    assert bps == sector and size // (bps * spc) >= 65525, (sector, size, bps, spc)
assert w.fat32_args(4096, 500 << 20) == (4096, 1) and w.fat32_args(512, 500 << 20) == (512, 8)
assert w.fat32_args("", 500 << 20)[0] == 512
print("fat32 geometry OK")
# too small for FAT32 at 4096-byte sectors: caught before anything is formatted
class _P:
    dry = False
    def su(self, cmd, check=True, mutating=False): return "4096" if "getss" in cmd else ""
try:
    w.format_partitions(_P(), {"ESP": ("/dev/x", 100 << 20), "Windows": ("/dev/y", 1 << 40)}, {"mkfs.fat": "newfs_msdos", "mkntfs": "mkntfs"})
    raise SystemExit("tiny ESP accepted")
except w.Fail as e:
    assert "too small" in str(e), e
print("small-ESP guard OK")
