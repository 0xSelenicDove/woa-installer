"""SHA-256 verification of the UEFI zip and the PE architecture check."""
import hashlib, importlib.util, struct, sys, tempfile, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
spec = importlib.util.spec_from_file_location("w", Path(__file__).parent.parent / "woa_installer.py")
w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)

H = "a" * 64
assert w.parse_checksum(H, "x.zip") == H
assert w.parse_checksum(H.upper() + "  x.zip\n", "x.zip") == H
assert w.parse_checksum("%s *x.zip" % H, "x.zip") == H
assert w.parse_checksum("SHA256 (x.zip) = %s" % H, "x.zip") == H
assert w.parse_checksum("%s  other.zip\n%s  x.zip" % ("b" * 64, H), "x.zip") == H
assert w.parse_checksum("%s  other.zip\n%s  third.zip" % ("b" * 64, H), "x.zip") is None
assert w.parse_checksum("not a hash", "x.zip") is None

d = Path(tempfile.mkdtemp())
z = d / "uefi-installer-test.zip"
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("test_UEFI.fd", b"fd"); zf.writestr("BootShim.Dualboot.bin", b"b")
    zf.writestr("META-INF/com/google/android/update-binary", b"#!/sbin/sh")
good = hashlib.sha256(z.read_bytes()).hexdigest()
assert w.sha256_file(z) == good

assert w.get_uefi_zip(str(z), "t", None, good) == z                       # explicit, matches
assert w.get_uefi_zip(str(z), "t", None, good.upper()) == z               # case-insensitive
for kw in (dict(sha256="0" * 64), dict(sha256="xyz")):                    # mismatch / malformed
    try:
        w.get_uefi_zip(str(z), "t", None, **kw); raise SystemExit("accepted %r" % kw)
    except w.Fail as e:
        assert "mismatch" in str(e) or "64 hex" in str(e), e
try:
    w.get_uefi_zip(str(z), "t", None, None, True); raise SystemExit("require_checksum ignored")
except w.Fail:
    pass
assert w.get_uefi_zip(str(z), "t", None) == z                             # unverified: warns only
(d / (z.name + ".sha256")).write_text("%s  %s\n" % (good, z.name))        # sidecar picked up
assert w.get_uefi_zip(str(z), "t", None, None, True) == z
(d / (z.name + ".sha256")).write_text("%s  %s\n" % ("0" * 64, z.name))    # wrong sidecar
try:
    w.get_uefi_zip(str(z), "t", None); raise SystemExit("bad sidecar accepted")
except w.Fail as e:
    assert "mismatch" in str(e)

# PE machine type
def pe(machine):
    b = bytearray(0x100); b[:2] = b"MZ"; struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\0\0"; struct.pack_into("<H", b, 0x84, machine); return bytes(b)
for name, code, ok in (("a64", 0xAA64, True), ("x64", 0x8664, False), ("x86", 0x014C, False)):
    f = d / (name + ".efi"); f.write_bytes(pe(code))
    try:
        w.check_arm64(f); assert ok, name
    except w.Fail as e:
        assert not ok and "not ARM64" in str(e), e
(d / "junk.efi").write_bytes(b"hello")
try:
    w.check_arm64(d / "junk.efi"); raise SystemExit("junk accepted")
except w.Fail:
    pass
print("checksum + arm64 OK")
