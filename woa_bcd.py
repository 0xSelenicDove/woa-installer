"""Build a Windows boot configuration (BCD) the way `bcdboot` does, without Windows.

A normal install runs `bcdboot`, which copies Windows\\System32\\config\\BCD-Template from the
image to \\EFI\\Microsoft\\Boot\\BCD, removes the Setup-only entries, adds the Windows loader and
resume entries and points the boot manager at them. This module repeats those steps by editing the
template registry hive directly (pure Python, standard library only).

The loader/resume entries are partition-independent ("locate" devices find Windows by file name),
so the result does not depend on disk GUIDs, and use the settings this project's README lists as
the optimal BCD configuration (testsigning, nointegritychecks, hypervisor/VSM off, ...).
"""
import struct
import time
import uuid

REG_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 3, 4, 7

BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
# Setup-only / template objects that bcdboot removes after cloning them
TEMPLATE_OBJECTS = ["{7254a080-1510-4e85-ac0f-e7fb3d444736}",     # Windows Setup (boot.wim) loader
                    "{b012b84d-c47c-4ed5-b722-c0c42163e569}",     # OS loader template
                    "{0c334284-9a41-4de1-99b3-a7e87e8ff07e}"]     # resume template
BOOTLOADERSETTINGS = "{6efb52bf-1766-41db-a6b3-0ee5eff72bd7}"
RESUMELOADERSETTINGS = "{1afa9c49-16ab-4a5c-901b-212802da9460}"


class HiveError(Exception):
    pass


def _filetime():
    return struct.pack("<Q", int((time.time() + 11644473600) * 10_000_000))


def _hash_lh(name):
    h = 0
    for c in name.upper():
        h = (h * 37 + ord(c)) & 0xFFFFFFFF
    return h


class Hive:
    """Minimal in-place registry hive (regf) editor: add/delete keys and values."""

    def __init__(self, data):
        self.b = bytearray(data)
        if self.b[:4] != b"regf":
            raise HiveError("not a registry hive")
        self.root = struct.unpack_from("<I", self.b, 0x24)[0]
        size = struct.unpack_from("<I", self.b, 0x28)[0]
        if 0x1000 + size > len(self.b):
            raise HiveError("truncated hive")
        self.b = self.b[:0x1000 + size]
        self._hbin = None            # absolute position of the hbin we append new cells to

    # ---- raw cell access
    def _p(self, off):
        return 0x1000 + off

    def _cell(self, off):
        p = self._p(off)
        size = abs(struct.unpack_from("<i", self.b, p)[0])
        return p + 4, size - 4

    def _free(self, off):
        if off in (0xFFFFFFFF, 0):
            return
        p = self._p(off)
        size = struct.unpack_from("<i", self.b, p)[0]
        if size < 0:
            struct.pack_into("<i", self.b, p, -size)

    def _alloc(self, data):
        if self._hbin is None:
            self._hbin = len(self.b)
            self.b += b"hbin" + struct.pack("<II", self._hbin - 0x1000, 0) + b"\0" * 20
        size = (len(data) + 4 + 7) & ~7
        off = len(self.b) - 0x1000
        self.b += struct.pack("<i", -size) + bytes(data) + b"\0" * (size - 4 - len(data))
        return off

    def _g(self, off, field, fmt="<I"):
        return struct.unpack_from(fmt, self.b, self._p(off) + 4 + field)[0]

    def _s(self, off, field, val, fmt="<I"):
        struct.pack_into(fmt, self.b, self._p(off) + 4 + field, val)

    # ---- key reading
    def name(self, off):
        n = self._g(off, 0x48, "<H")
        flags = self._g(off, 0x02, "<H")
        raw = bytes(self.b[self._p(off) + 4 + 0x4C: self._p(off) + 4 + 0x4C + n])
        return raw.decode("ascii" if flags & 0x20 else "utf-16le")

    def _list(self, off):
        """Offsets of the subkeys listed at `off` (lf/lh/li/ri)."""
        d, _ = self._cell(off)
        sig = bytes(self.b[d:d + 2])
        cnt = struct.unpack_from("<H", self.b, d + 2)[0]
        if sig in (b"lf", b"lh"):
            return [struct.unpack_from("<I", self.b, d + 4 + 8 * i)[0] for i in range(cnt)]
        if sig == b"li":
            return [struct.unpack_from("<I", self.b, d + 4 + 4 * i)[0] for i in range(cnt)]
        if sig == b"ri":
            out = []
            for i in range(cnt):
                out += self._list(struct.unpack_from("<I", self.b, d + 4 + 4 * i)[0])
            return out
        raise HiveError("unknown subkey list type %r" % sig)

    def children(self, off):
        lst = self._g(off, 0x1C)
        if self._g(off, 0x14) == 0 or lst == 0xFFFFFFFF:
            return []
        return self._list(lst)

    def child(self, off, name):
        for c in self.children(off):
            if self.name(c).upper() == name.upper():
                return c
        return None

    def find(self, path):
        off = self.root
        for part in [p for p in path.split("\\") if p]:
            off = self.child(off, part)
            if off is None:
                raise HiveError("key not found: " + path)
        return off

    def values(self, off):
        n, lst = self._g(off, 0x24), self._g(off, 0x28)
        if n == 0 or lst == 0xFFFFFFFF:
            return []
        d, _ = self._cell(lst)
        return [struct.unpack_from("<I", self.b, d + 4 * i)[0] for i in range(n)]

    def value_name(self, voff):
        n = self._g(voff, 2, "<H")
        flags = self._g(voff, 0x10, "<H")
        s = self._p(voff) + 4 + 0x14
        return bytes(self.b[s:s + n]).decode("ascii" if flags & 1 else "utf-16le")

    def get_value(self, off, name):
        for v in self.values(off):
            if self.value_name(v).upper() == name.upper():
                length = self._g(v, 4)
                typ = self._g(v, 0x0C)
                if length & 0x80000000:
                    length &= 0x7FFFFFFF
                    s = self._p(v) + 4 + 8
                    return typ, bytes(self.b[s:s + length])
                d, _ = self._cell(self._g(v, 8))
                return typ, bytes(self.b[d:d + length])
        return None

    # ---- key writing
    def _write_subkeys(self, off, entries):
        """Replace the subkey list of `off` by a fresh, sorted 'lh' list of (name, offset)."""
        old = self._g(off, 0x1C)
        if self._g(off, 0x14) and old != 0xFFFFFFFF:
            self._free_list_cells(old)
        entries = sorted(entries, key=lambda e: e[0].upper())
        if not entries:
            self._s(off, 0x14, 0)
            self._s(off, 0x1C, 0xFFFFFFFF)
            return
        body = b"lh" + struct.pack("<H", len(entries)) + b"".join(
            struct.pack("<II", o, _hash_lh(n)) for n, o in entries)
        self._s(off, 0x1C, self._alloc(body))
        self._s(off, 0x14, len(entries))
        longest = max(len(n) for n, _ in entries) * 2
        if longest > self._g(off, 0x34):
            self._s(off, 0x34, longest)

    def _free_list_cells(self, off):
        d, _ = self._cell(off)
        if bytes(self.b[d:d + 2]) == b"ri":
            cnt = struct.unpack_from("<H", self.b, d + 2)[0]
            for i in range(cnt):
                self._free(struct.unpack_from("<I", self.b, d + 4 + 4 * i)[0])
        self._free(off)

    def add_key(self, parent, name):
        sk = self._g(parent, 0x2C)
        nk = bytearray(0x4C)
        nk[0:2] = b"nk"
        struct.pack_into("<H", nk, 2, 0x20)                    # compressed (ASCII) name
        nk[4:12] = _filetime()
        struct.pack_into("<I", nk, 0x10, parent)
        struct.pack_into("<I", nk, 0x1C, 0xFFFFFFFF)
        struct.pack_into("<I", nk, 0x20, 0xFFFFFFFF)
        struct.pack_into("<I", nk, 0x28, 0xFFFFFFFF)
        struct.pack_into("<I", nk, 0x2C, sk)
        struct.pack_into("<I", nk, 0x30, 0xFFFFFFFF)
        struct.pack_into("<H", nk, 0x48, len(name))
        off = self._alloc(bytes(nk) + name.encode("ascii"))
        if sk not in (0, 0xFFFFFFFF):                          # one more key shares this descriptor
            sd, _ = self._cell(sk)
            struct.pack_into("<I", self.b, sd + 0x0C, struct.unpack_from("<I", self.b, sd + 0x0C)[0] + 1)
        subs = [(self.name(c), c) for c in self.children(parent)] + [(name, off)]
        self._write_subkeys(parent, subs)
        return off

    def _write_values(self, off, voffs):
        old = self._g(off, 0x28)
        if self._g(off, 0x24) and old != 0xFFFFFFFF:
            self._free(old)
        if not voffs:
            self._s(off, 0x24, 0)
            self._s(off, 0x28, 0xFFFFFFFF)
            return
        self._s(off, 0x28, self._alloc(b"".join(struct.pack("<I", v) for v in voffs)))
        self._s(off, 0x24, len(voffs))

    def add_value(self, off, name, vtype, data):
        data = bytes(data)
        vk = bytearray(0x14)
        vk[0:2] = b"vk"
        struct.pack_into("<H", vk, 2, len(name))
        if len(data) <= 4:
            struct.pack_into("<I", vk, 4, 0x80000000 | len(data))
            vk[8:12] = data.ljust(4, b"\0")
        else:
            struct.pack_into("<I", vk, 4, len(data))
            struct.pack_into("<I", vk, 8, self._alloc(data))
        struct.pack_into("<I", vk, 0x0C, vtype)
        struct.pack_into("<H", vk, 0x10, 1 if name else 0)
        voff = self._alloc(bytes(vk) + name.encode("ascii"))
        self._write_values(off, self.values(off) + [voff])
        if len(name) * 2 > self._g(off, 0x3C):
            self._s(off, 0x3C, len(name) * 2)
        if len(data) > self._g(off, 0x40):
            self._s(off, 0x40, len(data))
        return voff

    def delete_value(self, off, name):
        keep = []
        for v in self.values(off):
            if self.value_name(v).upper() == name.upper():
                if not self._g(v, 4) & 0x80000000:
                    self._free(self._g(v, 8))
                self._free(v)
            else:
                keep.append(v)
        self._write_values(off, keep)

    def delete_key(self, off):
        for c in self.children(off):
            self.delete_key(c)
        for v in self.values(off):
            if not self._g(v, 4) & 0x80000000:
                self._free(self._g(v, 8))
            self._free(v)
        if self._g(off, 0x24) and self._g(off, 0x28) != 0xFFFFFFFF:
            self._free(self._g(off, 0x28))
        if self._g(off, 0x14) and self._g(off, 0x1C) != 0xFFFFFFFF:
            self._free_list_cells(self._g(off, 0x1C))
        parent = self._g(off, 0x10)
        sk = self._g(off, 0x2C)
        if sk not in (0, 0xFFFFFFFF):
            sd, _ = self._cell(sk)
            cnt = struct.unpack_from("<I", self.b, sd + 0x0C)[0]
            struct.pack_into("<I", self.b, sd + 0x0C, max(1, cnt - 1))
        self._free(off)
        self._write_subkeys(parent, [(self.name(c), c) for c in self.children(parent) if c != off])

    def to_bytes(self):
        b = self.b
        if self._hbin is not None:                              # close the appended hbin
            used = len(b) - self._hbin
            total = (used + 4095) // 4096 * 4096
            pad = total - used
            if 0 < pad < 8:
                total += 4096
                pad += 4096
            if pad:
                b += struct.pack("<i", pad) + b"\0" * (pad - 4)   # one free cell fills the rest
            struct.pack_into("<I", b, self._hbin + 8, total)
        struct.pack_into("<I", b, 0x28, len(b) - 0x1000)
        seq = (struct.unpack_from("<I", b, 4)[0] + 1) & 0xFFFFFFFF
        struct.pack_into("<II", b, 4, seq, seq)
        b[0x0C:0x14] = _filetime()
        x = 0
        for i in range(127):
            x ^= struct.unpack_from("<I", b, 4 * i)[0]
        struct.pack_into("<I", b, 0x1FC, 1 if x == 0 else 0xFFFFFFFE if x == 0xFFFFFFFF else x)
        return bytes(b)


# ---------------------------------------------------------------------------
# bcdboot-style construction
# ---------------------------------------------------------------------------
def _utf16z(s):
    return s.encode("utf-16le") + b"\0\0"


def locate_device(path):
    """BCD 'locate' device: find the partition containing `path` (no disk/partition GUIDs)."""
    tail = _utf16z(path)
    total = 44 + len(tail)
    return b"\0" * 16 + struct.pack("<IIIIIII", 8, 0, total - 16, 0, 1, 0, 0) + tail


# 'boot' device (the partition the boot manager itself was loaded from)
BOOT_DEVICE = b"\0" * 16 + struct.pack("<IIII", 5, 0, 0x48, 0) + b"\0" * 56


def _multi(*items):
    return b"".join(_utf16z(i) for i in items) + b"\0\0"


def _q(n):
    return struct.pack("<Q", n)


def loader_elements(description, locale, resume_guid):
    return {
        "11000001": (REG_BINARY, locate_device("\\Windows\\system32\\winload.efi")),
        "12000002": (REG_SZ, _utf16z("\\Windows\\system32\\winload.efi")),
        "12000004": (REG_SZ, _utf16z(description)),
        "12000005": (REG_SZ, _utf16z(locale)),
        "14000006": (REG_MULTI_SZ, _multi(BOOTLOADERSETTINGS)),
        "14000008": (REG_MULTI_SZ, _multi("{00000000-0000-0000-0000-000000000000}")),
        "16000009": (REG_BINARY, b"\1"),
        "16000048": (REG_BINARY, b"\1"),          # nointegritychecks Yes
        "16000049": (REG_BINARY, b"\1"),          # testsigning Yes
        "16000060": (REG_BINARY, b"\0"),
        "21000001": (REG_BINARY, locate_device("\\Windows")),
        "22000002": (REG_SZ, _utf16z("\\Windows")),
        "23000003": (REG_SZ, _utf16z(resume_guid)),
        "25000020": (REG_BINARY, _q(0)),
        "250000e0": (REG_BINARY, _q(1)),          # bootstatuspolicy IgnoreAllFailures
        "25000142": (REG_BINARY, _q(0)),          # vsmlaunchtype Off
        "26000090": (REG_BINARY, b"\1"),
        "26000091": (REG_BINARY, b"\1"),
    }


def resume_elements(description, locale):
    return {
        "11000001": (REG_BINARY, locate_device("\\Windows\\system32\\winresume.efi")),
        "12000002": (REG_SZ, _utf16z("\\Windows\\system32\\winresume.efi")),
        "12000004": (REG_SZ, _utf16z(description)),
        "12000005": (REG_SZ, _utf16z(locale)),
        "14000006": (REG_MULTI_SZ, _multi(RESUMELOADERSETTINGS)),
        "14000008": (REG_MULTI_SZ, _multi("{00000000-0000-0000-0000-000000000000}")),
        "16000009": (REG_BINARY, b"\1"),
        "21000001": (REG_BINARY, locate_device("\\Windows")),
        "21000026": (REG_BINARY, locate_device("\\Windows")),
        "22000002": (REG_SZ, _utf16z("\\hiberfil.sys")),
        "26000006": (REG_BINARY, b"\0"),
    }


def _make_object(h, objects, guid, otype, elements):
    obj = h.add_key(objects, guid)
    desc = h.add_key(obj, "Description")
    h.add_value(desc, "Type", REG_DWORD, struct.pack("<I", otype))
    els = h.add_key(obj, "Elements")
    for eid, (vtype, data) in elements.items():
        e = h.add_key(els, eid)
        h.add_value(e, "Element", vtype, data)
    return obj


def build_bcd(template, description="Windows 11 ARM64", locale="en-US"):
    """Return the BCD hive bytes made from the image's BCD-Template."""
    h = Hive(template)
    objects = h.find("Objects")
    if h.child(objects, BOOTMGR) is None:
        raise HiveError("this does not look like a BCD template (no Windows Boot Manager)")
    for g in TEMPLATE_OBJECTS:
        c = h.child(objects, g)
        if c is not None:
            h.delete_key(c)
    loader = "{%s}" % uuid.uuid4()
    resume = "{%s}" % uuid.uuid4()
    _make_object(h, objects, loader, 0x10200003, loader_elements(description, locale, resume))
    _make_object(h, objects, resume, 0x10200004, resume_elements(description, locale))

    mgr = h.child(objects, BOOTMGR)
    els = h.child(mgr, "Elements")
    for template_only in ("45000001", "47000006"):          # template-class elements bcdboot strips
        c = h.child(els, template_only)
        if c is not None:
            h.delete_key(c)
    for eid, vtype, data in (("11000001", REG_BINARY, BOOT_DEVICE),
                             ("23000003", REG_SZ, _utf16z(loader)),
                             ("24000001", REG_MULTI_SZ, _multi(loader)),
                             ("25000004", REG_BINARY, _q(30)),
                             ("26000021", REG_BINARY, b"\0")):
        e = h.child(els, eid) or h.add_key(els, eid)
        h.delete_value(e, "Element")
        h.add_value(e, "Element", vtype, data)
    # bcdboot marks the store as the system store; without these the BCD API refuses to
    # open it (Windows boots, then specialize fails: "File is not system store" 0xC0000098)
    desc = h.find("Description")
    for name, vtype, data in (("KeyName", REG_SZ, _utf16z("BCD00000000")),
                              ("System", REG_DWORD, struct.pack("<I", 1)),
                              ("TreatAsSystem", REG_DWORD, struct.pack("<I", 1))):
        h.delete_value(desc, name)
        h.add_value(desc, name, vtype, data)
    return h.to_bytes()

