"""BCD generation (bcdboot-style): device blobs, hive integrity, entries. The hive tests need the
image's BCD-Template: set WOA_BCD_TEMPLATE=/path/to/BCD-Template (they are skipped otherwise)."""
import os, struct, sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import woa_bcd as b

# --- byte-exact device blobs taken from a BCD that boots Windows on a real device
GOOD_LOCATE_WINLOAD = bytes.fromhex(
    "00000000000000000000000000000000080000000000000058000000000000000100000000000000000000005c00570069006e0064006f00770073005c00730079007300740065006d00330032005c00770069006e006c006f00610064002e006500660069000000")
GOOD_LOCATE_WINDOWS = bytes.fromhex(
    "0000000000000000000000000000000008000000000000002e000000000000000100000000000000000000005c00570069006e0064006f00770073000000")
GOOD_BOOT_DEVICE = bytes.fromhex(
    "00000000000000000000000000000000050000000000000048000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000")
assert b.locate_device("\\Windows\\system32\\winload.efi") == GOOD_LOCATE_WINLOAD
assert b.locate_device("\\Windows") == GOOD_LOCATE_WINDOWS
assert b.BOOT_DEVICE == GOOD_BOOT_DEVICE
assert b._hash_lh("Elements") == b._hash_lh("ELEMENTS")
print("device blobs OK")

tpl_path = os.environ.get("WOA_BCD_TEMPLATE")
if not tpl_path or not Path(tpl_path).is_file():
    print("BCD hive tests skipped (set WOA_BCD_TEMPLATE)"); sys.exit(0)


def check_integrity(data):
    """Independent structural walk of a regf hive; raises AssertionError on any inconsistency."""
    u32 = lambda o: struct.unpack_from("<I", data, o)[0]
    x = 0
    for i in range(127):
        x ^= u32(4 * i)
    assert u32(0x1FC) == (1 if x == 0 else 0xFFFFFFFE if x == 0xFFFFFFFF else x), "header checksum"
    assert u32(4) == u32(8), "sequence numbers"
    size = u32(0x28)
    assert 0x1000 + size == len(data) and size % 4096 == 0, "bins size"
    pos, alloc = 0x1000, set()
    while pos < len(data):                                   # hbin chain + cell chain
        assert data[pos:pos + 4] == b"hbin" and u32(pos + 4) == pos - 0x1000, "hbin header"
        hsize = u32(pos + 8)
        assert hsize and hsize % 4096 == 0, "hbin size"
        c = pos + 32
        while c < pos + hsize:
            cs = struct.unpack_from("<i", data, c)[0]
            n = abs(cs)
            assert n >= 8 and n % 8 == 0 and c + n <= pos + hsize, "cell size @%x" % c
            if cs < 0:
                alloc.add(c - 0x1000)
            c += n
        assert c == pos + hsize, "cells do not fill the hbin"
        pos += hsize
    cell = lambda off: 0x1000 + off + 4
    seen = set()

    def walk(off, parent):
        assert off in alloc, "key cell not allocated"
        d = cell(off)
        assert data[d:d + 2] == b"nk", "nk signature"
        if parent is not None:
            assert u32(d + 0x10) == parent, "parent pointer"
        nsub, lst = u32(d + 0x14), u32(d + 0x1C)
        names = []
        if nsub:
            assert lst in alloc, "subkey list not allocated"
            ld = cell(lst)
            sig = data[ld:ld + 2]
            assert sig in (b"lf", b"lh", b"li", b"ri"), "list signature"
            if sig == b"lh":
                assert struct.unpack_from("<H", data, ld + 2)[0] == nsub, "subkey count"
                for i in range(nsub):
                    co, h = struct.unpack_from("<II", data, ld + 4 + 8 * i)
                    nm = walk(co, off)
                    assert h == b._hash_lh(nm), "lh hash for " + nm
                    names.append(nm.upper())
                assert names == sorted(names), "subkeys not sorted"
        nval, vl = u32(d + 0x24), u32(d + 0x28)
        if nval:
            assert vl in alloc, "value list not allocated"
            for i in range(nval):
                vo = struct.unpack_from("<I", data, cell(vl) + 4 * i)[0]
                assert vo in alloc and data[cell(vo):cell(vo) + 2] == b"vk", "vk"
                dl, do = struct.unpack_from("<II", data, cell(vo) + 4)
                if not dl & 0x80000000 and dl:
                    assert do in alloc, "value data not allocated"
        nl = struct.unpack_from("<H", data, d + 0x48)[0]
        assert off not in seen, "cell referenced twice"
        seen.add(off)
        return data[d + 0x4C:d + 0x4C + nl].decode("ascii")
    walk(u32(0x24), None)
    return True


template = Path(tpl_path).read_bytes()
check_integrity(template)                                    # sanity-check the checker on a real hive
out = b.build_bcd(template, "Windows 11 Test", "en-US")
assert check_integrity(out)
assert len(out) % 4096 == 0

h = b.Hive(out)
objects = h.find("Objects")
names = {h.name(c).lower(): c for c in h.children(objects)}
assert not any(g in names for g in b.TEMPLATE_OBJECTS), "template-only objects must be removed"
loaders = [c for c in h.children(objects) if h.get_value(h.child(c, "Description"), "Type")[1] == struct.pack("<I", 0x10200003)]
resumes = [c for c in h.children(objects) if h.get_value(h.child(c, "Description"), "Type")[1] == struct.pack("<I", 0x10200004)]
assert len(loaders) == 1 and len(resumes) == 1
els = lambda o: h.child(o, "Elements")
val = lambda o, e: h.get_value(h.child(els(o), e), "Element")[1]
assert val(loaders[0], "11000001") == GOOD_LOCATE_WINLOAD and val(loaders[0], "21000001") == GOOD_LOCATE_WINDOWS
assert val(loaders[0], "12000004").decode("utf-16le").rstrip("\0") == "Windows 11 Test"
rg = val(loaders[0], "23000003").decode("utf-16le").rstrip("\0")
assert rg.lower() == h.name(resumes[0]).lower(), "loader must point at the new resume object"
mgr = names[b.BOOTMGR]
lg = val(mgr, "23000003").decode("utf-16le").rstrip("\0")
assert lg.lower() == h.name(loaders[0]).lower(), "boot manager default"
assert val(mgr, "24000001").decode("utf-16le").split("\0")[0].lower() == lg.lower(), "boot manager display order"
assert val(mgr, "11000001") == GOOD_BOOT_DEVICE
assert h.child(els(mgr), "45000001") is None and h.child(els(mgr), "47000006") is None, "template-class elements removed"

out2 = b.build_bcd(template)                                 # fresh GUIDs each time, still valid
assert check_integrity(out2) and out2 != out
try:
    b.build_bcd(b"regf" + b"\0" * 4092 + b"\0" * 64)
    raise SystemExit("garbage accepted")
except (b.HiveError, struct.error, IndexError):
    pass
print("BCD hive OK (%d -> %d bytes, loader %s)" % (len(template), len(out), h.name(loaders[0])))
