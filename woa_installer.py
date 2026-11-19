#!/usr/bin/env python3
"""Windows-on-ARM + edk2-msm UEFI installer for rooted Android devices, driven from
a Windows or macOS PC. Works with any device that has an edk2-msm style UEFI
installer zip (device names are read from the zip's *_UEFI.fd file).

  1. check device (adb, root, unlocked bootloader, A/B slots, partitions)
  2. back up the running boot slot (host + device)
  3. read the user's ARM64 Windows ISO, pick an edition
  4. wipe + populate the ESP / NTFS partitions over adb (root)
  5. copy the running boot slot into the other one, run the UEFI installer zip on it
  6. fastboot set_active <other slot>, reboot

Standard library only. Needs Python 3.8+.
"""
import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import struct
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib
import zipfile
from xml.sax.saxutils import escape as xesc
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:            # also works under `python -I`
    sys.path.insert(0, str(HERE))
from woa_data import TIMEZONES as WIN_TIMEZONES  # noqa: E402
import woa_bcd  # noqa: E402
CACHE = Path.home() / ".woa-installer"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""

RELEASES_API = "https://api.github.com/repos/%s/releases/latest"
PT_URL = "https://dl.google.com/android/repository/platform-tools-latest-%s.zip" % (
    "windows" if IS_WIN else "darwin")
WIMLIB_WIN_URL = "https://wimlib.net/downloads/wimlib-1.14.4-windows-x86_64-bin.zip"

BYNAME_DIRS = ["/dev/block/bootdevice/by-name", "/dev/block/by-name",
               "/dev/block/platform/*/by-name", "/dev/block/platform/*/*/by-name"]
ESP_NAMES = ("esp", "winesp", "win_esp", "efi", "uefi_esp")
WIN_NAMES = ("win", "windows", "winos", "win_os", "winpe")
NEEDS_RERUN = False      # set when partitions were created and the install must be run again
WORK = "/data/local/tmp/woa-install"
STREAM_CHUNK = 512 * 1024 * 1024  # bytes per `tar -x` on the device (bounds adbd memory)
TOOLS_REPO = "0xSelenicDove/woa-installer"      # releases carry phone-tools-aarch64.tar.gz
TOOLS_ASSET = "phone-tools-aarch64.tar.gz"
PHONE_TOOLS = ("mkntfs", "ntfs-3g", "mkfs.fat")  # static aarch64 binaries, see phone-tools/
ESP_TYPE = uuid.UUID("C12A7328-F81F-11D2-BA4B-00A0C93EC93B")
BASIC_TYPE = uuid.UUID("EBD0A0A2-B9E5-4433-87C0-68B6B72699C7")


class Fail(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def step(n, msg):
    say("\n[%s] %s" % (n, msg))


PROMPT = input          # the GUI swaps this for dialogs


def ask_yes(prompt):
    return PROMPT(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def run_stream(cmd):
    """Run a long command, echoing its output line by line (works under the GUI too)."""
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    last = 0.0
    buf = b""
    while True:
        chunk = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
        if not chunk:
            break
        buf = (buf + chunk)[-4096:]
        if time.time() - last > 2:
            last = time.time()
            line = re.split(rb"[\r\n]", buf.strip())[-1].decode(errors="replace").strip()
            if line:
                say("    " + line)
    if p.wait() != 0:
        raise Fail("command failed: " + " ".join(map(str, cmd)))


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024


# --------------------------------------------------------------------------- #
# Host tools
# --------------------------------------------------------------------------- #
def fetch(url, dest=None):
    """GET url -> bytes (or save to dest). Falls back to curl when Python lacks CA certs
    (common with python.org builds on macOS)."""
    import ssl
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "woa-installer"})
        with urllib.request.urlopen(req) as r:
            if dest:
                with open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return None
            return r.read()
    except (ssl.SSLError, urllib.error.URLError) as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e) and not isinstance(e, ssl.SSLError):
            raise Fail("download failed: %s (%s)" % (url, e))
    curl = shutil.which("curl") or shutil.which("curl.exe")
    if not curl:
        raise Fail("Python cannot verify HTTPS certificates and curl is missing. On macOS run "
                   "'Install Certificates.command' from your Python folder.")
    cmd = [curl, "-fsSL", "-A", "woa-installer"] + (["-o", str(dest)] if dest else []) + [url]
    r = run(cmd)
    return None if dest else r.stdout


def download(url, dest, what):
    say("  downloading %s ..." % what)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fetch(url, dest)


class Tools:
    def __init__(self, platform_tools=None):
        self.adb = self.fastboot = self.wimlib = None
        self.wimlib_error = None
        self._find_platform_tools(platform_tools)
        try:
            self._find_wimlib()
        except Fail as e:
            self.wimlib_error = str(e)

    def _find_platform_tools(self, override):
        cands = []
        if override:
            cands.append(Path(override))
        cands.append(CACHE / "platform-tools")
        for d in cands:
            if (d / ("adb" + EXE)).exists() and (d / ("fastboot" + EXE)).exists():
                self.adb, self.fastboot = str(d / ("adb" + EXE)), str(d / ("fastboot" + EXE))
                return
        a, f = shutil.which("adb"), shutil.which("fastboot")
        if a and f:
            self.adb, self.fastboot = a, f
            return
        z = CACHE / "platform-tools.zip"
        download(PT_URL, z, "Android platform-tools (Google)")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(CACHE)
        z.unlink()
        d = CACHE / "platform-tools"
        if not IS_WIN:
            for n in ("adb", "fastboot"):
                os.chmod(d / n, 0o755)
        self.adb, self.fastboot = str(d / ("adb" + EXE)), str(d / ("fastboot" + EXE))

    def _find_wimlib(self):
        self.wimlib = locate_wimlib()


def locate_wimlib():
    # Finder-launched apps don't inherit the shell PATH, so also look in Homebrew's folders
    extra = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "")])
    w = shutil.which("wimlib-imagex", path=extra)
    if w:
        return w
    if IS_WIN:
        d = CACHE / "wimlib"
        exe = d / "wimlib-imagex.exe"
        if not exe.exists():
            z = CACHE / "wimlib.zip"
            download(WIMLIB_WIN_URL, z, "wimlib")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(d)
            z.unlink()
        return str(exe)
    raise Fail("wimlib-imagex not found. Install it first:  brew install wimlib")


def run(cmd, check=True, capture=True, **kw):
    kw.setdefault("stdin", subprocess.DEVNULL)      # `adb shell` would otherwise swallow our own stdin
    r = subprocess.run(cmd, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.PIPE if capture else None, **kw)
    if check and r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").strip()
        raise Fail("command failed (%s): %s\n%s" % (r.returncode, " ".join(map(str, cmd)), err))
    return r


# --------------------------------------------------------------------------- #
# Phone access
# --------------------------------------------------------------------------- #
class Phone:
    def __init__(self, tools, dry):
        self.t, self.dry = tools, dry
        self.bydir = None
        self.slot = ""      # running slot suffix, e.g. "_a" ("" = not A/B)
        self.dev = ""

    def adb(self, *args, **kw):
        return run([self.t.adb, *args], **kw)

    def sh(self, cmd, check=True):
        """Run as the shell user."""
        return self.adb("shell", cmd, check=check).stdout.decode(errors="replace").strip()

    def su(self, cmd, check=True, mutating=False):
        """Run as root through Magisk su (cmd must not contain single quotes)."""
        assert "'" not in cmd, cmd
        if mutating and self.dry:
            say("  [pre-check] su: " + cmd)
            return ""
        r = self.adb("shell", "su -c '%s'" % cmd, check=check)
        return r.stdout.decode(errors="replace").strip()

    def prop(self, name):
        return self.sh("getprop " + name)

    def stream_in(self, cmd):
        """Popen of `adb shell su -c cmd`; caller writes binary to .stdin."""
        assert "'" not in cmd
        # NB: `adb exec-in` silently drops piped data on current platform-tools; a piped
        # `adb shell` (shell protocol v2, no pty) is binary-safe and propagates exit codes.
        return subprocess.Popen([self.t.adb, "shell", "su -c '%s'" % cmd],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def find_byname(self):
        for pat in BYNAME_DIRS:
            out = self.su("for d in %s; do [ -d $d ] && echo $d && break; done" % pat, check=False)
            if out:
                self.bydir = out.splitlines()[0]
                return self.bydir
        raise Fail("Could not find the partition by-name directory on this device.")

    def wait_device(self):
        run([self.t.adb, "start-server"], check=False)
        for _ in range(30):
            out = self.adb("devices", check=False).stdout.decode()
            lines = [l for l in out.splitlines()[1:] if l.strip()]
            if any(l.endswith("device") for l in lines):
                return
            if any("unauthorized" in l for l in lines):
                say("  Device shows an 'Allow USB debugging?' prompt - accept it.")
            time.sleep(2)
        raise Fail("No device found over adb. Enable USB debugging and connect it.")


def check_device(ph):
    ph.wait_device()
    names = [ph.prop(p) for p in ("ro.product.device", "ro.product.model", "ro.build.product")]
    ph.dev = names[0]
    say("  device: %s (model %s)" % (names[0], names[1]))
    if "uid=0" not in ph.su("id", check=False):
        raise Fail("No root. Grant the adb shell superuser access in Magisk and retry.")
    if ph.prop("ro.boot.verifiedbootstate") == "green" or ph.prop("ro.boot.flash.locked") == "1":
        raise Fail("The bootloader looks locked. Unlock it first.")
    ph.slot = ph.prop("ro.boot.slot_suffix")
    ph.find_byname()
    say("  root: ok   running slot: %s   partitions: %s" % (ph.slot or "none (not A/B)", ph.bydir))
    if not ph.slot:
        say("  WARNING: this device is not A/B. There is no second boot slot, so the UEFI")
        say("  would replace your only boot image and Android could only be restored by")
        say("  flashing the backup. Re-run with --allow-non-ab to accept that.")
        if not ARGS.allow_non_ab:
            raise Fail("not an A/B device (use --allow-non-ab to proceed)")
    return names


def check_compat(zip_path, names):
    with zipfile.ZipFile(zip_path) as zf:
        fds = [n for n in zf.namelist() if n.endswith("_UEFI.fd")]
    targets = {n.split("_")[0] for n in fds}
    say("  UEFI zip targets: %s" % ", ".join(sorted(targets)))
    if not targets & set(names) and not ARGS.force_device:
        raise Fail("This UEFI zip is for %s but the device reports %s. Wrong firmware would\n"
                   "brick the boot slot. (Override with --force-device only if you are sure.)"
                   % (", ".join(sorted(targets)), "/".join(n for n in names if n)))


def resolve_part(ph, arg):
    if arg.startswith("/"):
        return arg
    if ph.su("[ -e %s/%s ] && echo y" % (ph.bydir, arg), check=False) == "y":
        return "%s/%s" % (ph.bydir, arg)
    return "/dev/block/" + arg


def autodetect(ph, names, candidates):
    listing = ph.su("ls %s" % ph.bydir, check=False).split()
    low = {n.lower(): n for n in listing}
    for c in candidates:
        if c in low:
            return low[c]
    return None


def find_partitions(ph, esp, win):
    esp = esp or autodetect(ph, None, ESP_NAMES)
    win = win or autodetect(ph, None, WIN_NAMES)
    if not esp or not win:
        return None
    out = {}
    for label, name in (("ESP", esp), ("Windows", win)):
        dev = resolve_part(ph, name)
        sz = ph.su("blockdev --getsize64 " + dev, check=False)
        if not sz.isdigit():
            if not (ARGS.esp_part or ARGS.win_part) and ARGS.create_partitions:
                return None      # profile default (e.g. sda35) is not there yet
            raise Fail("Partition %s (%s) does not exist or is unreadable." % (dev, label))
        out[label] = (dev, int(sz))
        say("  %-8s %-34s %s" % (label, dev, human(int(sz))))
    if out["Windows"][1] < 20 * 1024 ** 3:
        raise Fail("The Windows partition is under 20 GB; it needs room for Windows.")
    if out["ESP"][1] > 2 * 1024 ** 3:
        raise Fail("The ESP is suspiciously large - wrong partition? Check --esp-part.")
    if out["ESP"][0] == out["Windows"][0]:
        raise Fail("ESP and Windows partition are the same device.")
    return out


# --------------------------------------------------------------------------- #
# Partition creation (GPT edited directly; no sgdisk needed on the device)
# --------------------------------------------------------------------------- #
def parse_size(txt):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)I?B?", txt.strip().upper())
    if not m:
        raise Fail("bad size %r (use e.g. 512M, 64G)" % txt)
    return int(float(m.group(1)) * 1024 ** "_KMGT".index(m.group(2) or "_"))


class Gpt:
    HDR = "<8sIIIIQQQQ16sQIII"          # 92 bytes

    def __init__(self, ph, disk):
        self.ph, self.disk = ph, disk
        self.ss = int(ph.su("cat /sys/block/%s/queue/logical_block_size" % disk))
        self.total = int(ph.su("cat /sys/block/%s/size" % disk)) * 512 // self.ss
        self.dev = "/dev/block/" + disk
        self.p_sec = self.read(1, 1)
        self.p = self.parse(self.p_sec)
        self.ents = self.read_entries(self.p)
        self.b_sec = self.read(self.p["alt"], 1)
        self.b = self.parse(self.b_sec)
        self.b_ents = self.read_entries(self.b)
        if self.ents != self.b_ents:
            raise Fail("Primary and backup GPT disagree; refusing to touch this disk.")

    def read(self, lba, count):
        return self.ph.adb("exec-out", "su -c 'dd if=%s bs=%d skip=%d count=%d 2>/dev/null'"
                           % (self.dev, self.ss, lba, count)).stdout

    def parse(self, sec):
        f = struct.unpack(self.HDR, sec[:92])
        if f[0] != b"EFI PART":
            raise Fail("No GPT found on %s" % self.dev)
        h = dict(size=f[2], crc=f[3], me=f[5], alt=f[6], first=f[7], last=f[8], guid=f[9],
                 elba=f[10], n=f[11], esz=f[12], ecrc=f[13])
        raw = bytearray(sec[:h["size"]])
        raw[16:20] = b"\0\0\0\0"
        if zlib.crc32(bytes(raw)) != h["crc"]:
            raise Fail("GPT header CRC is bad on %s" % self.dev)
        return h

    def read_entries(self, h):
        nsec = -(-h["n"] * h["esz"] // self.ss)
        raw = self.read(h["elba"], nsec)[:h["n"] * h["esz"]]
        if zlib.crc32(raw) != h["ecrc"]:
            raise Fail("GPT entry array CRC is bad on %s" % self.dev)
        return [raw[i * h["esz"]:(i + 1) * h["esz"]] for i in range(h["n"])]

    @staticmethod
    def decode(e):
        return dict(type=e[:16], uid=e[16:32], first=struct.unpack("<Q", e[32:40])[0],
                    last=struct.unpack("<Q", e[40:48])[0],
                    name=e[56:128].decode("utf-16le", "ignore").rstrip("\0"))

    def used(self):
        return [(i, self.decode(e)) for i, e in enumerate(self.ents) if e[:16] != b"\0" * 16]

    def build_entry(self, ptype, first, last, name):
        nm = name.encode("utf-16le").ljust(72, b"\0")
        e = ptype.bytes_le + uuid.uuid4().bytes_le + struct.pack("<QQQ", first, last, 0) + nm
        return e.ljust(self.p["esz"], b"\0")

    def header(self, h, sec, me, alt, elba, ents):
        arr = b"".join(ents)
        raw = bytearray(sec)
        struct.pack_into(self.HDR, raw, 0, b"EFI PART", 0x10000, 92, 0, 0, me, alt, h["first"],
                         h["last"], h["guid"], elba, h["n"], h["esz"], zlib.crc32(arr))
        struct.pack_into("<I", raw, 16, zlib.crc32(bytes(raw[:92])))
        return bytes(raw), arr

    def write(self, lba, data):
        proc = self.ph.stream_in("dd of=%s bs=%d seek=%d conv=notrunc 2>/dev/null"
                                 % (self.dev, self.ss, lba))
        proc.communicate(data)
        if proc.returncode != 0:
            raise Fail("writing GPT at LBA %d failed" % lba)

    def commit(self, ents):
        p_hdr, arr = self.header(self.p, self.p_sec, 1, self.p["alt"], self.p["elba"], ents)
        b_hdr, _ = self.header(self.b, self.b_sec, self.b["me"], 1, self.b["elba"], ents)
        # backup first, then primary, so a crash leaves a valid GPT either way
        self.write(self.b["elba"], arr)
        self.write(self.b["me"], b_hdr)
        self.write(self.p["elba"], arr)
        self.write(1, p_hdr)
        self.ph.su("sync")


def plan_layout(g, esp_bytes, win_bytes):
    """Return (new_entries, notes, shrinks_userdata)."""
    align = max(1, (1024 * 1024) // g.ss)
    used = g.used()
    names = {u["name"].lower(): (i, u) for i, u in used}
    if "esp" in names or "win" in names:
        raise Fail("A partition named esp/win already exists; use --esp-part/--win-part.")
    last_part = max(used, key=lambda x: x[1]["last"])
    esp_n = -(-esp_bytes // g.ss)
    win_n = -(-win_bytes // g.ss)
    end = (g.p["last"] + 1) // align * align - 1          # last aligned sector
    win_first = (end + 1 - win_n) // align * align
    esp_first = (win_first - esp_n) // align * align
    ents = list(g.ents)
    notes, shrink = [], False
    if last_part[1]["last"] >= esp_first:
        if last_part[1]["name"].lower() != "userdata":
            raise Fail("No free space at the end of the disk and the last partition is '%s', "
                       "not userdata. Partition manually." % last_part[1]["name"])
        shrink = True
        new_last = esp_first - 1
        keep = (new_last - last_part[1]["first"] + 1) * g.ss
        if keep < 16 * 1024 ** 3:
            raise Fail("Not enough room: userdata would shrink to %s. Use smaller --win-size."
                       % human(keep))
        e = bytearray(ents[last_part[0]])
        e[40:48] = struct.pack("<Q", new_last)
        ents[last_part[0]] = bytes(e)
        notes.append("shrink userdata to %s (DATA WILL BE ERASED)" % human(keep))
    free = [i for i, e in enumerate(ents) if e[:16] == b"\0" * 16]
    if len(free) < 2:
        raise Fail("GPT has no free entry slots.")
    ents[free[0]] = g.build_entry(ESP_TYPE, esp_first, win_first - 1, "esp")
    ents[free[1]] = g.build_entry(BASIC_TYPE, win_first, win_first + win_n - 1, "win")
    notes.append("add esp   %s  (LBA %d-%d)" % (human(esp_n * g.ss), esp_first, win_first - 1))
    notes.append("add win   %s  (LBA %d-%d)" % (human(win_n * g.ss), win_first, win_first + win_n - 1))
    return ents, notes, shrink


def create_partitions(ph, tools):
    ref = ph.su("readlink -f %s/userdata" % ph.bydir, check=False)
    if not ref.startswith("/dev/block/"):
        raise Fail("Could not locate the userdata partition; create esp/win manually.")
    disk = ph.su("basename $(dirname $(readlink -f /sys/class/block/%s))" % ref.split("/")[-1])
    say("  userdata lives on %s" % disk)
    g = Gpt(ph, disk)
    say("  sector size %d, disk %s, %d partitions" % (g.ss, human(g.total * g.ss), len(g.used())))
    ents, notes, shrink = plan_layout(g, parse_size(ARGS.esp_size), parse_size(ARGS.win_size))
    say("  plan:")
    for n in notes:
        say("    - " + n)
    outdir = Path(ARGS.backup_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    bk = outdir / ("gpt_%s_%s.bin" % (ph.dev, time.strftime("%Y%m%d-%H%M%S")))
    nsec = 2 + -(-g.p["n"] * g.p["esz"] // g.ss) + 1
    bk.write_bytes(g.read(0, nsec))
    say("  saved the current GPT to %s" % bk)
    if ph.dry:
        say("  [pre-check] not writing")
        return False
    need = "WIPE DATA" if shrink else "PARTITION"
    if PROMPT("  Type %s to rewrite the partition table: " % need).strip() != need:
        raise Fail("aborted")
    g.commit(ents)
    chk = Gpt(ph, disk)
    if chk.ents != ents:
        raise Fail("GPT verify failed after writing. Restore with the saved backup: " + str(bk))
    say("  partition table written and verified.")
    ph.adb("reboot", "bootloader", check=False)
    for _ in range(40):
        if b"fastboot" in run([tools.fastboot, "devices"], check=False).stdout:
            break
        time.sleep(2)
    else:
        raise Fail("Device did not reach fastboot. Boot it to fastboot manually"
                   + (", then run: fastboot erase userdata && fastboot erase metadata" if shrink else ""))
    if shrink:
        say("  erasing userdata + metadata (its filesystem no longer matches)")
        run([tools.fastboot, "erase", "userdata"], check=False)
        run([tools.fastboot, "erase", "metadata"], check=False)
    run([tools.fastboot, "reboot"], check=False)
    return True


# --------------------------------------------------------------------------- #
# ISO -> WIM
# --------------------------------------------------------------------------- #
class MountedIso:
    def __init__(self, iso):
        self.iso, self.mp = Path(iso).resolve(), None

    def __enter__(self):
        if IS_WIN:
            ps = ("(Mount-DiskImage -ImagePath '%s' -PassThru | Get-Volume).DriveLetter" % self.iso)
            out = run(["powershell", "-NoProfile", "-Command", ps]).stdout.decode().strip()
            self.mp = Path(out + ":\\")
        elif IS_MAC:
            out = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(self.iso)]).stdout
            ents = plistlib.loads(out)["system-entities"]
            mps = [e["mount-point"] for e in ents if e.get("mount-point")]
            if not mps:
                raise Fail("hdiutil could not mount the ISO")
            self.mp = Path(mps[0])
        else:
            raise Fail("Only Windows and macOS hosts are supported")
        return self.mp

    def __exit__(self, *a):
        if IS_WIN:
            run(["powershell", "-NoProfile", "-Command",
                 "Dismount-DiskImage -ImagePath '%s'" % self.iso], check=False)
        elif self.mp:
            run(["hdiutil", "detach", str(self.mp)], check=False)


def find_wim(root):
    for n in ("install.wim", "install.esd", "install.swm"):
        for sub in ("sources", "Sources"):
            p = root / sub / n
            if p.exists():
                return p
    raise Fail("No sources/install.wim or install.esd in the ISO. Is it a Windows ISO?")


def list_images(tools, wim):
    out = run([tools.wimlib, "info", str(wim)]).stdout.decode(errors="replace")
    images = []
    for block in re.split(r"\n\s*\n", out):
        m = re.search(r"^Index:\s+(\d+)", block, re.M)
        if not m:
            continue
        g = lambda k: (re.search(r"^%s:\s+(.*)$" % k, block, re.M) or [None, ""])[1].strip()
        images.append({"index": int(m.group(1)), "name": g("Name"),
                       "arch": g("Architecture"), "build": g("Build"),
                       "langs": g("Languages").split(), "default_lang": g("Default Language")})
    return images


def pick_image(images, wanted):
    arm = [i for i in images if i["arch"].lower() in ("arm64", "aarch64")]
    if not arm:
        archs = sorted({i["arch"] or "?" for i in images})
        raise Fail("This ISO is not ARM64 (found: %s). Use a Windows 11 ARM64 ISO." % ", ".join(archs))
    for i in arm:
        say("    %d: %s (build %s)" % (i["index"], i["name"], i["build"]))
    if wanted:
        for i in arm:
            if i["index"] == wanted:
                return i
        raise Fail("--image-index %d is not an ARM64 image in this ISO" % wanted)
    for i in arm:
        if "pro" in i["name"].lower() and "education" not in i["name"].lower() \
                and "workstation" not in i["name"].lower():
            return i
    return arm[0]


def read_iso_info(iso, index=None):
    """Mount the ISO, read its editions/languages, unmount. For the GUI's Files step."""
    import types
    tools = types.SimpleNamespace(wimlib=locate_wimlib())
    with MountedIso(iso) as mp:
        images = list_images(tools, find_wim(mp))
        img = pick_image(images, index)
    langs = img["langs"] or ([img["default_lang"]] if img["default_lang"] else [])
    arm = [i for i in images if i["arch"].lower() in ("arm64", "aarch64")]
    return {"images": images, "arm_images": arm, "image": img, "langs": langs,
            "default_lang": img["default_lang"] or (langs[0] if langs else "en-US")}


# --------------------------------------------------------------------------- #
# UEFI package
# --------------------------------------------------------------------------- #
_REPO_RE = re.compile(r"^(?:git@github\.com:|(?:https?://)?(?:www\.)?github\.com/)?"
                      r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#].*)?$")


def parse_repo(text):
    """'owner/name' or any github.com link to it -> 'owner/name'."""
    m = _REPO_RE.match((text or "").strip())
    if not m or {m.group(1), m.group(2)} & {".", ".."}:
        raise Fail("Not a GitHub repository: %r. Use owner/name or a github.com link." % text)
    return "%s/%s" % (m.group(1), m.group(2))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_checksum(text, filename):
    """Extract the SHA-256 for `filename` from a .sha256 / SHA256SUMS style file.
    Accepts a bare hash, 'hash  name', 'hash *name' and BSD 'SHA256 (name) = hash'."""
    sums = {}
    for line in (text or "").splitlines():
        line = line.strip()
        m = re.fullmatch(r"([0-9a-fA-F]{64})(?:\s+\*?(.+))?", line) or \
            re.fullmatch(r"SHA256 \((.+)\) = ([0-9a-fA-F]{64})", line)
        if not m:
            continue
        if m.re.pattern.startswith("SHA256"):
            name, digest = m.group(1), m.group(2)
        else:
            digest, name = m.group(1), m.group(2)
        sums[os.path.basename((name or "").strip()) or None] = digest.lower()
    if filename in sums:
        return sums[filename]
    if len(sums) == 1:                    # single entry (or bare hash): it is for this file
        return next(iter(sums.values()))
    return None


def find_uefi_release(repo):
    """Latest release of `repo` -> (tag, asset, checksum_asset_or_None); raises Fail if unusable."""
    repo = parse_repo(repo)
    try:
        rel = json.loads(fetch(RELEASES_API % repo))
    except (Fail, ValueError):
        raise Fail("Could not read the releases of %s (repository not found, private, or "
                   "it has no releases)." % repo)
    assets = rel.get("assets", [])
    asset = next((a for a in assets if re.match(r"uefi-installer.*\.zip$", a["name"])), None)
    if not asset:
        raise Fail("The latest release of %s (%s) has no uefi-installer*.zip file."
                   % (repo, rel.get("tag_name", "?")))
    names = [asset["name"] + ".sha256", asset["name"] + ".sha256sum", "SHA256SUMS",
             "SHA256SUMS.txt", "sha256sums.txt", "checksums.txt"]
    summ = next((a for n in names for a in assets if a["name"] == n), None)
    return rel.get("tag_name", "?"), asset, summ


def get_uefi_zip(path, dev, repo, sha256=None, require_checksum=False):
    expected, source, downloaded = None, "", False
    if sha256:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256.strip()):
            raise Fail("--sha256 must be 64 hex characters.")
        expected, source = sha256.strip().lower(), "--sha256"
    if path:
        z = Path(path)
        if not z.is_file():
            raise Fail("UEFI zip not found: %s" % z)
        if not expected:
            for side in (Path(str(z) + ".sha256"), Path(str(z) + ".sha256sum")):
                if side.is_file():
                    expected = parse_checksum(side.read_text(errors="replace"), z.name)
                    source = side.name
                    break
    else:
        repo = parse_repo(repo) if repo else None
        if not repo:
            raise Fail("A UEFI installer for '%s' is required. Pass --uefi-zip PATH, or --repo "
                       "owner/name (or a github.com link) to download the latest release." % dev)
        say("  fetching the latest UEFI release of %s" % repo)
        tag, asset, summ = find_uefi_release(repo)
        z = CACHE / asset["name"]
        download(asset["browser_download_url"], z, "%s %s" % (asset["name"], tag))
        downloaded = True
        if not expected and summ:
            text = fetch(summ["browser_download_url"]).decode(errors="replace")
            expected, source = parse_checksum(text, asset["name"]), "release " + summ["name"]
            if not expected:
                say("  WARNING: could not find %s in %s" % (asset["name"], summ["name"]))
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    ok = (any(n.endswith("_UEFI.fd") for n in names)
          and "BootShim.Dualboot.bin" in names
          and "META-INF/com/google/android/update-binary" in names)
    if not ok:
        raise Fail("%s is not a compatible UEFI installer zip (needs *_UEFI.fd, "
                   "BootShim.Dualboot.bin, META-INF/.../update-binary)." % z.name)
    actual = sha256_file(z)
    if expected:
        if actual != expected:
            if downloaded:
                z.unlink()
            raise Fail("Checksum mismatch for %s (%s).\n  expected %s\n  got      %s\n"
                       "The file is corrupt or was tampered with; not using it."
                       % (z.name, source, expected, actual))
        say("  UEFI zip: %s  sha256 verified (%s)" % (z.name, source))
    else:
        if require_checksum:
            raise Fail("No checksum to verify %s against (--require-checksum). Pass --sha256 HEX, "
                       "put a .sha256 file next to the zip, or publish one in the release." % z.name)
        say("  UEFI zip: %s  sha256 %s" % (z.name, actual))
        say("  WARNING: not verified - no published checksum. Compare the hash above with the "
            "publisher's, or pass --sha256.")
    return z


# --------------------------------------------------------------------------- #
# Device-side filesystem work
# --------------------------------------------------------------------------- #
def mount_ntfs(ph, dev, mp, tl=None):
    ph.su("mkdir -p " + mp, mutating=True)
    drv = (tl or {}).get("ntfs-3g", "ntfs-3g")
    for cmd in ("%s -o rw,noatime %s %s" % (drv, dev, mp),
                "mount -t ntfs3 -o noatime %s %s" % (dev, mp)):
        if ph.dry:
            say("  [pre-check] su: " + cmd)
            return
        ph.su(cmd + " 2>&1", check=False)
        if ph.su('grep -c " %s " /proc/mounts' % mp, check=False).strip() not in ("", "0"):
            return
    raise Fail("Could not mount NTFS read-write on the device (tried ntfs-3g and the kernel ntfs3).")


def mount_vfat(ph, dev, mp):
    ph.su("mkdir -p " + mp, mutating=True)
    ph.su("mount -t vfat -o rw,noatime,umask=000 %s %s" % (dev, mp), mutating=True)


def ensure_phone_tools(ph):
    """Make mkntfs / ntfs-3g / mkfs.fat runnable on the device; returns {name: path}."""
    found, missing = {}, []
    for t in PHONE_TOOLS:
        alt = {"mkntfs": "mkntfs mkfs.ntfs", "ntfs-3g": "ntfs-3g mount.ntfs",
               "mkfs.fat": "mkfs.fat mkfs.vfat newfs_msdos"}[t]
        p = ph.su("which %s 2>/dev/null" % alt, check=False).splitlines()
        if p:
            found[t] = p[0]
        else:
            missing.append(t)
    if not missing:
        return found
    say("  device is missing: %s - pushing static builds" % ", ".join(missing))
    src = Path(ARGS.phone_tools) if ARGS.phone_tools else None
    if src is None:
        d = CACHE / "phone-tools"
        tgz = CACHE / TOOLS_ASSET
        if not d.exists():
            base = "https://github.com/%s/releases/latest/download/" % TOOLS_REPO
            download(base + TOOLS_ASSET, tgz, "NTFS/FAT tools for the device")
            download(base + TOOLS_ASSET + ".sha256", CACHE / (TOOLS_ASSET + ".sha256"), "checksum")
            want = (CACHE / (TOOLS_ASSET + ".sha256")).read_text().split()[0]
            if hashlib.sha256(tgz.read_bytes()).hexdigest() != want:
                tgz.unlink()
                raise Fail("device tools checksum mismatch")
            d.mkdir(parents=True)
            with tarfile.open(tgz) as tf:
                tf.extractall(d)
        src = d
    if ph.dry:
        say("  [pre-check] would push %s from %s" % (", ".join(missing), src))
        return {t: WORK + "/bin/" + t for t in PHONE_TOOLS}
    ph.su("mkdir -p %s/bin" % WORK)
    for t in missing:
        f = next((c for c in (src / t, src / "bin" / t) if c.is_file()), None)
        if not f:
            raise Fail("%s not found in %s (see phone-tools/)" % (t, src))
        ph.adb("push", str(f), "%s/bin/%s" % (WORK, t))
        ph.su("chmod 755 %s/bin/%s" % (WORK, t))
        found[t] = "%s/bin/%s" % (WORK, t)
    return found


def fat32_args(sector, size_bytes):
    """(bytes/sector, sectors/cluster) for a FAT32 ESP. Tools pick large clusters by default, which
    leaves a ~500 MB ESP under FAT32's 65525-cluster minimum (fails on 4096-byte-sector UFS)."""
    sector = int(sector) if str(sector).isdigit() and int(sector) else 512
    spc = max(1, 4096 // sector)                     # 4 KiB clusters
    while spc > 1 and size_bytes // (sector * spc) < 66000:
        spc //= 2                                    # keep comfortably above the FAT32 minimum
    return sector, spc


def format_partitions(ph, parts, tl):
    sec, spc = fat32_args(ph.su("blockdev --getss " + parts["ESP"][0], check=False), parts["ESP"][1])
    if parts["ESP"][1] // (sec * spc) < 65525:
        raise Fail("The ESP (%s) is too small for FAT32 with %d-byte sectors (needs at least %s). "
                   "Create a larger ESP, e.g. --esp-size 512M." % (human(parts["ESP"][1]), sec,
                                                                   human(65525 * sec)))
    say("  formatting ESP as FAT32 (%d-byte sectors, %d sector(s) per cluster)" % (sec, spc))
    fat = tl["mkfs.fat"]
    if fat.endswith("newfs_msdos"):
        ph.su("%s -F 32 -S %d -c %d -L ESP %s" % (fat, sec, spc, parts["ESP"][0]), mutating=True)
    else:
        ph.su("%s -F 32 -n ESP -S %d -s %d %s" % (fat, sec, spc, parts["ESP"][0]), mutating=True)
    say("  formatting Windows partition as NTFS (quick)")
    ntfs = tl["mkntfs"]
    ph.su("%s -f -F -L Windows %s" % (ntfs, parts["Windows"][0]), mutating=True)


def stream_tree(ph, src, dest_cmd_dir, label, esp=False):
    """tar `src` straight into `su tar -x` on the device."""
    files = total = 0
    for root, _, fs in os.walk(src):
        for f in fs:
            files += 1
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    say("  %s: %d files, %s" % (label, files, human(total)))
    if ph.dry:
        say("  [pre-check] would stream to " + dest_cmd_dir)
        return
    done = [0, 0.0]

    def flt(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o755 if ti.isdir() else 0o644
        return ti

    def entries():
        for root, dirs, fs in os.walk(src):
            dirs.sort()
            rel = os.path.relpath(root, src)
            if rel != ".":
                yield os.path.join(root, ""), rel
            for f in sorted(fs):
                yield os.path.join(root, f), os.path.normpath(os.path.join(rel, f))

    # The device cannot absorb the stream as fast as the PC sends it: adbd queues the
    # difference in RAM (a single 18 GB `tar` grew it past 12 GB and crashed the device).
    # So send in bounded chunks, one `tar -x` per chunk, and `sync` between them.
    state = {"proc": None, "tf": None, "sent": 0}

    def open_chunk():
        proc = ph.stream_in("tar -x -C %s" % dest_cmd_dir)

        class Counting:
            def write(self, b):
                proc.stdin.write(b)
                done[0] += len(b)
                if time.time() - done[1] > 2:
                    done[1] = time.time()
                    print("\r  %s / %s" % (human(done[0]), human(total)), end="", flush=True)
                return len(b)

        state.update(proc=proc, sent=0,
                     tf=tarfile.open(fileobj=Counting(), mode="w|", format=tarfile.GNU_FORMAT))

    def close_chunk():
        proc, tf = state["proc"], state["tf"]
        if proc is None:
            return
        try:
            tf.close()
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise Fail("tar on the device failed: " + err)
        ph.su("sync", check=False)
        state["proc"] = None

    for path, arc in entries():
        if state["proc"] is None:
            open_chunk()
        state["tf"].add(path.rstrip(os.sep), arcname=arc, recursive=False, filter=flt)
        if not os.path.isdir(path.rstrip(os.sep)) and not os.path.islink(path.rstrip(os.sep)):
            state["sent"] += os.path.getsize(path)
        if state["sent"] >= STREAM_CHUNK:
            close_chunk()
    close_chunk()
    print()


PE_MACHINES = {0xAA64: "ARM64", 0x8664: "x64", 0x014C: "x86", 0x01C4: "ARM32"}


def pe_machine(path):
    """Machine type of a PE file (e.g. 'ARM64', 'x64'), read from its header."""
    with open(path, "rb") as f:
        head = f.read(64)
        if head[:2] != b"MZ":
            raise Fail("%s is not a Windows executable." % Path(path).name)
        f.seek(struct.unpack_from("<I", head, 0x3C)[0])
        sig = f.read(6)
    if sig[:4] != b"PE\0\0":
        raise Fail("%s has no PE header." % Path(path).name)
    m = struct.unpack("<H", sig[4:6])[0]
    return PE_MACHINES.get(m, "unknown (0x%04X)" % m)


def check_arm64(path):
    arch = pe_machine(path)
    if arch != "ARM64":
        raise Fail("%s is %s code, not ARM64. This ISO is not a Windows on ARM image."
                   % (Path(path).name, arch))


def build_esp_tree(win_tree, bcd, esp_dir):
    """Lay out EFI/Microsoft/Boot + EFI/Boot/bootaa64.efi from the applied image."""
    boot_src = win_tree / "Windows" / "Boot"
    dst = esp_dir / "EFI" / "Microsoft" / "Boot"
    dst.mkdir(parents=True)
    efi = boot_src / "EFI"
    if not (efi / "bootmgfw.efi").exists():
        raise Fail("Image has no Windows/Boot/EFI/bootmgfw.efi - not an ARM64 UEFI image?")
    check_arm64(efi / "bootmgfw.efi")
    for extra in ("System32/winload.efi",):          # present on full (full install) extractions
        if (win_tree / "Windows" / extra).is_file():
            check_arm64(win_tree / "Windows" / extra)
    for f in efi.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    for sub in ("Fonts", "Resources"):
        if (boot_src / sub).is_dir():
            shutil.copytree(boot_src / sub, dst / sub)
    # per-language resources live under Windows/Boot/EFI/<locale>? copy locale dirs too
    for d in efi.iterdir():
        if d.is_dir():
            shutil.copytree(d, dst / d.name, dirs_exist_ok=True)
    fb = esp_dir / "EFI" / "Boot"
    fb.mkdir()
    shutil.copy2(efi / "bootmgfw.efi", fb / "bootaa64.efi")
    shutil.copy2(bcd, dst / "BCD")


def resolve_bcd(ph, arg, esp_dev, esp_mp, tree=None, description="Windows 11 ARM64", workdir=None):
    """The boot configuration for the ESP, in order of preference:
    1. --bcd FILE (your own),
    2. built from the BCD-Template inside the Windows image, the way `bcdboot` does in a normal
       install (see woa_bcd.py),
    3. the BCD already on the device's ESP."""
    if arg:
        p = Path(arg)
        if not p.is_file():
            raise Fail("BCD not found: %s" % p)
        return p
    tpl = Path(tree) / "Windows" / "System32" / "config" / "BCD-Template" if tree else None
    if tpl and tpl.is_file():
        try:
            data = woa_bcd.build_bcd(tpl.read_bytes(), description,
                                     (ARGS.language or ARGS.locale or "en-US"))
            woa_bcd.Hive(data).find("Objects")                    # parses back
        except (woa_bcd.HiveError, struct.error, IndexError, ValueError) as e:
            say("  could not build a BCD from the image's template (%s); looking for another" % e)
        else:
            out = Path(workdir or tempfile.mkdtemp(prefix="woa-bcd-")) / "BCD"
            out.write_bytes(data)
            say("  built the boot configuration from the image's BCD-Template, as bcdboot does")
            return out
    say("  no BCD template in the image; trying to reuse the BCD already on the device's ESP")
    try:                # read-only, so it also runs during the pre-check
        ph.adb("shell", "su -c 'mkdir -p %s && umount %s 2>/dev/null; mount -t vfat -o ro %s %s'"
               % (esp_mp, esp_mp, esp_dev, esp_mp), check=False)
    except Fail:
        pass            # fresh / unformatted ESP: nothing to reuse
    old = esp_mp + "/EFI/Microsoft/Boot/BCD"
    if ph.su("[ -f %s ] && echo y" % old, check=False) != "y":
        raise Fail(
            "No boot configuration (BCD) available: the image has no BCD-Template and the ESP "
            "has no BCD to reuse. Provide one with --bcd.")
    out = CACHE / "backup" / ("BCD_%s_%s.bin" % (ph.dev or "device", time.strftime("%Y%m%d-%H%M%S")))
    out.parent.mkdir(parents=True, exist_ok=True)
    say("  saving the device's current BCD to %s" % out)
    with open(out, "wb") as f:
        f.write(ph.adb("exec-out", "su -c 'cat %s'" % old).stdout)
    ph.su("umount %s; rmdir %s %s" % (esp_mp, esp_mp, WORK), check=False)   # read-only; tidy up even during the pre-check
    return out


# --------------------------------------------------------------------------- #
# Unattended first boot (skip OOBE: network, privacy, account screens)
# --------------------------------------------------------------------------- #
BAD_USER = set('"/\\[]:;|=,+*?<>@')
SPECIALIZE_CMDS = [
    # let OOBE continue without a network (no WiFi driver yet)
    r'reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f',
    # no privacy-settings screens / first-logon animation / consumer extras
    r'reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\OOBE" /v DisablePrivacyExperience /t REG_DWORD /d 1 /f',
    r'reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v EnableFirstLogonAnimation /t REG_DWORD /d 0 /f',
    r'reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\CloudContent" /v DisableWindowsConsumerFeatures /t REG_DWORD /d 1 /f',
    # do not auto-encrypt the system drive on first boot
    r'reg add "HKLM\SYSTEM\CurrentControlSet\Control\BitLocker" /v PreventDeviceEncryption /t REG_DWORD /d 1 /f',
]


def build_unattend(user="user", password="", hostname="WOA-DEVICE", locale="en-US",
                   timezone="UTC", drivers=False, language=None):
    """unattend.xml for arm64 Windows 11: local admin account, autologon, no OOBE screens."""
    if not user or len(user) > 20 or BAD_USER & set(user) or user.endswith("."):
        raise Fail("Invalid account name %r (max 20 chars, none of %s)." % (user, "".join(sorted(BAD_USER))))
    if not re.fullmatch(r"[A-Za-z0-9-]{1,15}", hostname or "") or hostname.isdigit():
        raise Fail("Invalid device name %r (1-15 letters, digits or '-')." % hostname)
    if not re.fullmatch(r"[A-Za-z]{2,3}-[A-Za-z0-9-]{2,}", locale or ""):
        raise Fail("Invalid language %r (use e.g. en-US)." % locale)
    language = language or locale
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})+", language):
        raise Fail("Invalid display language %r (use e.g. en-US)." % language)
    if timezone not in WIN_TIMEZONES:
        raise Fail("Unknown time zone %r (use a Windows time zone name, e.g. UTC)." % timezone)
    u, pw = xesc(user), xesc(password)
    arch = 'processorArchitecture="arm64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"'
    cmds = "".join(
        '<RunSynchronousCommand wcm:action="add"><Order>%d</Order><Path>%s</Path></RunSynchronousCommand>'
        % (i, xesc(c)) for i, c in enumerate(SPECIALIZE_CMDS, 1))
    first = ""
    if drivers:
        first = ('<FirstLogonCommands><SynchronousCommand wcm:action="add"><Order>1</Order>'
                 '<CommandLine>pnputil /add-driver C:\\Drivers\\*.inf /subdirs /install</CommandLine>'
                 '<Description>Install bundled drivers</Description></SynchronousCommand></FirstLogonCommands>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<unattend xmlns="urn:schemas-microsoft-com:unattend" '
        'xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">\n'
        '<settings pass="specialize">'
        '<component name="Microsoft-Windows-Deployment" %(a)s><RunSynchronous>%(cmds)s</RunSynchronous></component>'
        '<component name="Microsoft-Windows-Shell-Setup" %(a)s><ComputerName>%(host)s</ComputerName></component>'
        '</settings>\n'
        '<settings pass="oobeSystem">'
        '<component name="Microsoft-Windows-International-Core" %(a)s>'
        '<InputLocale>%(loc)s</InputLocale><SystemLocale>%(loc)s</SystemLocale>'
        '<UILanguage>%(lang)s</UILanguage><UserLocale>%(loc)s</UserLocale></component>'
        '<component name="Microsoft-Windows-Shell-Setup" %(a)s>'
        '<OOBE><HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>'
        '<HideOnlineAccountScreens>true</HideOnlineAccountScreens>'
        '<HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>'
        '<HideLocalAccountScreen>true</HideLocalAccountScreen>'
        '<ProtectYourPC>3</ProtectYourPC><NetworkLocation>Home</NetworkLocation>'
        '<SkipMachineOOBE>true</SkipMachineOOBE><SkipUserOOBE>true</SkipUserOOBE></OOBE>'
        '<UserAccounts><LocalAccounts><LocalAccount wcm:action="add"><Name>%(u)s</Name>'
        '<Group>Administrators</Group><Password><Value>%(pw)s</Value><PlainText>true</PlainText></Password>'
        '</LocalAccount></LocalAccounts></UserAccounts>'
        '<AutoLogon><Enabled>true</Enabled><LogonCount>999999</LogonCount><Username>%(u)s</Username>'
        '<Password><Value>%(pw)s</Value><PlainText>true</PlainText></Password></AutoLogon>'
        '<TimeZone>%(tz)s</TimeZone>%(first)s'
        '</component></settings>\n</unattend>\n'
    ) % dict(a=arch, cmds=cmds, host=xesc(hostname), loc=xesc(locale), lang=xesc(language), u=u, pw=pw,
             tz=xesc(timezone), first=first)


def write_unattend(tree, xml):
    for rel in ("Windows/Panther/unattend.xml", "Windows/Panther/Unattend/Unattend.xml"):
        f = Path(tree) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(xml, encoding="utf-8")


def copy_drivers(tree, src):
    src = Path(src)
    if not src.is_dir() or not list(src.rglob("*.inf")):
        raise Fail("--drivers must be a folder containing .inf driver packages: %s" % src)
    shutil.copytree(src, Path(tree) / "Drivers", dirs_exist_ok=True)
    return len(list(src.rglob("*.inf")))


# --------------------------------------------------------------------------- #
# Backup / UEFI install / switch
# --------------------------------------------------------------------------- #
def other_slot(slot):
    return {"_a": "_b", "_b": "_a"}.get(slot, "")


def backup_boot(ph, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    src = "%s/boot%s" % (ph.bydir, ph.slot)
    dest = outdir / ("boot%s_stock_%s_%s.img" % (ph.slot, ph.dev, time.strftime("%Y%m%d-%H%M%S")))
    say("  reading %s -> %s" % (src, dest))
    data = ph.adb("exec-out", "su -c 'dd if=%s bs=1M 2>/dev/null'" % src).stdout
    if len(data) < 8 * 1024 * 1024 or data[:8] != b"ANDROID!":
        raise Fail("boot backup looks wrong (%d bytes). Not continuing." % len(data))
    dest.write_bytes(data)
    ph.su("cp %s /sdcard/boot%s_stock.img" % (src, ph.slot), check=False, mutating=True)
    return dest


def install_uefi(ph, zip_path):
    dst = other_slot(ph.slot)
    if ph.slot:
        say("  copying boot%s -> boot%s" % (ph.slot, dst))
        ph.su("dd if=%s/boot%s of=%s/boot%s bs=1M" % (ph.bydir, ph.slot, ph.bydir, dst),
              mutating=True)
    ph.su("rm -rf %s/inst && mkdir -p %s/inst" % (WORK, WORK), mutating=True)
    if not ph.dry:
        # WORK is root-owned (made through su), so adb's shell user cannot write into it:
        # push to the shell-writable /data/local/tmp and let root move it into place.
        stage = "/data/local/tmp/woa-uefi-installer.zip"
        ph.adb("push", str(zip_path), stage)
        ph.su("mv %s %s/uefi-installer.zip" % (stage, WORK))
    ph.su("unzip -o %s/uefi-installer.zip -d %s/inst" % (WORK, WORK), mutating=True)
    ph.su("chmod -R 755 %s/inst" % WORK, mutating=True)
    ub = WORK + "/inst/META-INF/com/google/android/update-binary"
    # retarget the installer: this device's by-name dir and the slot we want to write
    ph.su('sed -i "s|^target=.*|target=%s/boot;|; s|^slot=.*|slot=%s|" %s'
          % (ph.bydir, dst, ub), mutating=True)
    if not ph.dry and ph.su('grep -c "^slot=%s" %s' % (dst, ub), check=False) != "1":
        raise Fail("could not retarget the installer at slot %s" % (dst or "(none)"))
    say("  running the UEFI installer against %s" % ("slot " + dst[1:].upper() if dst else "boot"))
    out = ph.su("cd %s/inst && sh %s dummy 1 %s/uefi-installer.zip 2>&1" % (WORK, ub, WORK),
                mutating=True)
    say("    " + out.replace("\n", "\n    "))
    if not ph.dry and "Done installing" not in out:
        raise Fail("UEFI installer did not report success - NOT switching slots.")


def switch_to_uefi(ph, tools):
    dst = other_slot(ph.slot)
    if ph.dry:
        say("  [pre-check] adb reboot; fastboot set_active %s" % (dst[1:] or "(none)"))
        return
    if not dst:
        ph.adb("reboot", check=False)
        return
    ph.adb("reboot", "bootloader", check=False)
    for _ in range(40):
        if b"fastboot" in run([tools.fastboot, "devices"], check=False).stdout:
            break
        time.sleep(2)
    else:
        raise Fail("Device did not appear in fastboot. Hold the bootloader key combo, then run:\n"
                   "  fastboot set_active %s && fastboot reboot" % dst[1:])
    run([tools.fastboot, "set_active", dst[1:]])
    run([tools.fastboot, "reboot"])


# --------------------------------------------------------------------------- #
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="Windows 11 ARM64 ISO (prompted if omitted)")
    ap.add_argument("--uefi-zip", help="UEFI installer zip for your device (default: latest release of a known device)")
    ap.add_argument("--repo", help="GitHub repo (owner/name or a github.com link) to fetch the latest uefi-installer*.zip from")
    ap.add_argument("--sha256", help="expected SHA-256 of the UEFI zip (also read from <zip>.sha256 or the release)")
    ap.add_argument("--require-checksum", action="store_true", help="refuse a UEFI zip that cannot be checksum-verified")
    ap.add_argument("--force-device", action="store_true", help="skip the zip-vs-device name check")
    ap.add_argument("--allow-non-ab", action="store_true", help="allow devices without A/B slots (overwrites the only boot image)")
    ap.add_argument("--image-index", type=int, help="install.wim image index (default: Pro)")
    ap.add_argument("--bcd", help="your own BCD hive for the ESP (default: built from the Windows image, like a normal install)")
    ap.add_argument("--esp-part", help="ESP partition: by-name label or sdXN (default: auto-detect)")
    ap.add_argument("--win-part", help="Windows partition: by-name label or sdXN (default: auto-detect)")
    ap.add_argument("--platform-tools", help="folder with adb/fastboot")
    ap.add_argument("--backup-dir", default=str(CACHE / "backup"))
    ap.add_argument("--create-partitions", action="store_true",
                    help="create esp/win partitions at the end of the disk if they are missing")
    ap.add_argument("--esp-size", default="512M")
    ap.add_argument("--win-size", default="64G")
    ap.add_argument("--phone-tools", help="folder with static aarch64 mkntfs, ntfs-3g, mkfs.fat")
    ap.add_argument("--no-unattend", action="store_true",
                    help="don't inject unattend.xml (first boot shows the normal Windows setup)")
    ap.add_argument("--user", default="user", help="local administrator account (default: user)")
    ap.add_argument("--password", default="", help="its password (default: blank)")
    ap.add_argument("--hostname", default="WOA-DEVICE", help="Windows device name")
    ap.add_argument("--locale", default="en-US", help="region & format (and keyboard), e.g. en-GB")
    ap.add_argument("--language", help="Windows display language, must exist in the image (default: same as --locale)")
    ap.add_argument("--timezone", default="UTC", help='Windows time zone name, e.g. "Pacific Standard Time"')
    ap.add_argument("--drivers", help="folder of .inf drivers to copy to C:\\Drivers and install at first logon")
    ap.add_argument("--skip-windows", action="store_true", help="only install the UEFI (skip Windows)")
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--dry-run", "--pre-check", dest="dry_run", action="store_true",
                    help="pre-check: verify everything and show what would be written, without changing the device")
    return ap


def probe(argv=None):
    """Read-only device inspection for the GUI. Returns a dict of findings."""
    global ARGS
    ARGS = build_parser().parse_args(argv or [])
    info = {"error": None, "device": "", "model": "", "root": False, "locked": None,
            "slot": "", "ab": False, "esp": None, "win": None, "wimlib": None}
    try:
        tools = Tools(ARGS.platform_tools)
        info["wimlib"] = tools.wimlib_error
        ph = Phone(tools, True)
        ph.wait_device()
        info["device"] = ph.prop("ro.product.device")
        info["model"] = ph.prop("ro.product.model")
        info["root"] = "uid=0" in ph.su("id", check=False)
        vb, fl = ph.prop("ro.boot.verifiedbootstate"), ph.prop("ro.boot.flash.locked")
        info["locked"] = (vb == "green") or fl == "1"
        info["slot"] = ph.prop("ro.boot.slot_suffix")
        info["ab"] = bool(info["slot"])
        if info["root"]:
            ph.find_byname()
            info["parts"] = find_partitions(ph, ARGS.esp_part, ARGS.win_part)
    except Fail as e:
        info["error"] = str(e)
    return info


def main(argv=None):
    global ARGS
    ap = build_parser()
    ARGS = ap.parse_args(argv)

    say("woa-installer - Windows on ARM + edk2-msm UEFI for rooted Android devices")
    if ARGS.dry_run:
        say("PRE-CHECK: nothing on the device will be modified.")

    iso = ARGS.iso
    if not iso and not ARGS.skip_windows:
        iso = PROMPT("Path to your Windows 11 ARM64 ISO: ").strip().strip('"')
    if not ARGS.skip_windows and not Path(iso).is_file():
        raise Fail("ISO not found: %s" % iso)

    tools = Tools(ARGS.platform_tools)
    if not ARGS.skip_windows and tools.wimlib is None:
        raise Fail(tools.wimlib_error)
    ph = Phone(tools, ARGS.dry_run)

    step("1/6", "Checking the device")
    names = check_device(ph)

    step("2/6", "UEFI package and partitions")
    uefi = get_uefi_zip(ARGS.uefi_zip, ph.dev, ARGS.repo, ARGS.sha256, ARGS.require_checksum)
    check_compat(uefi, names)
    parts = None
    if not ARGS.skip_windows:
        parts = find_partitions(ph, ARGS.esp_part, ARGS.win_part)
        if parts is None:
            if not ARGS.create_partitions:
                listing = ph.su("ls %s" % ph.bydir, check=False).replace("\n", " ")
                raise Fail("No ESP / Windows partitions found (partitions: %s).\n"
                           "Re-run with --create-partitions to create them, or pass\n"
                           "--esp-part / --win-part." % listing)
            step("2b/6", "Creating the ESP and Windows partitions")
            if create_partitions(ph, tools):
                global NEEDS_RERUN
                NEEDS_RERUN = True
                say("\nThe device is rebooting. If data was erased, finish Android setup, enable USB")
                say("debugging, make sure root works (reinstall Magisk if needed), then run this")
                say("installer again WITHOUT --create-partitions.")
            return

    step("3/6", "Backing up the stock boot image")
    bk = backup_boot(ph, Path(ARGS.backup_dir))
    say("  saved %s (keep this!)" % bk)

    tmp = Path(tempfile.mkdtemp(prefix="woa-install-"))
    try:
        if not ARGS.skip_windows:
            step("4/6", "Reading the Windows ISO")
            with MountedIso(iso) as mp:
                wim = find_wim(mp)
                img = pick_image(list_images(tools, wim), ARGS.image_index)
                say("  using image %d: %s" % (img["index"], img["name"]))
                say("\n  This will ERASE %s (ESP) and %s (Windows) on the device." %
                    (parts["ESP"][0], parts["Windows"][0]))
                if not ARGS.dry_run and PROMPT("  Type ERASE to continue: ").strip() != "ERASE":
                    raise Fail("aborted")
                tree = tmp / "win"
                tree.mkdir()
                img_name = img["name"]
                if ARGS.dry_run:
                    say("  [pre-check] extracting only Windows/Boot to test the ESP layout ...")
                    (tree / "Windows").mkdir()
                    run_stream([tools.wimlib, "extract", str(wim), str(img["index"]),
                                "/Windows/Boot", "--dest-dir", str(tree / "Windows"), "--no-acls"])
                    (tree / "Windows" / "System32" / "config").mkdir(parents=True)
                    run_stream([tools.wimlib, "extract", str(wim), str(img["index"]),
                                "/Windows/System32/config/BCD-Template", "--dest-dir",
                                str(tree / "Windows" / "System32" / "config"), "--no-acls"])
                else:
                    say("  extracting the image (this takes a while) ...")
                    run_stream([tools.wimlib, "apply", str(wim), str(img["index"]), str(tree),
                                "--no-acls"])

            step("5/6", "Writing Windows to the device")
            esp_mp, win_mp = WORK + "/esp", WORK + "/win"
            bcd = resolve_bcd(ph, ARGS.bcd, parts["ESP"][0], esp_mp, tree, img_name, tmp)
            if not ARGS.no_unattend:
                xml = build_unattend(ARGS.user, ARGS.password, ARGS.hostname, ARGS.locale,
                                     ARGS.timezone, bool(ARGS.drivers), ARGS.language)
                write_unattend(tree, xml)
                say("  unattended first boot: account '%s', no network/privacy screens, autologon"
                    % ARGS.user)
            if ARGS.drivers:
                say("  bundling %d driver package(s) from %s" % (copy_drivers(tree, ARGS.drivers), ARGS.drivers))
            esp_tree = tmp / "esp"
            esp_tree.mkdir()
            build_esp_tree(tree, bcd, esp_tree)
            for mp in (esp_mp, win_mp):
                ph.su("umount %s" % mp, check=False, mutating=True)
            tl = ensure_phone_tools(ph)
            format_partitions(ph, parts, tl)
            mount_vfat(ph, parts["ESP"][0], esp_mp)
            mount_ntfs(ph, parts["Windows"][0], win_mp, tl)
            stream_tree(ph, str(esp_tree), esp_mp, "ESP")
            stream_tree(ph, str(tree), win_mp, "Windows")
            if not ph.dry:
                for f, where in ((win_mp + "/Windows/System32/winload.efi", "winload.efi"),
                                 (esp_mp + "/EFI/Microsoft/Boot/bootmgfw.efi", "bootmgfw.efi"),
                                 (esp_mp + "/EFI/Microsoft/Boot/BCD", "BCD")):
                    if ph.su("[ -f %s ] && echo y" % f, check=False) != "y":
                        raise Fail("verification failed: %s missing on the device" % where)
            ph.su("sync", mutating=True)
            ph.su("umount %s; umount %s" % (esp_mp, win_mp), check=False, mutating=True)
            say("  [pre-check] nothing written." if ph.dry else "  Windows files written and verified.")
        else:
            step("4/6", "Skipping Windows (--skip-windows)")
            step("5/6", "Skipping Windows (--skip-windows)")

        step("6/6", "Installing UEFI into the other boot slot")
        install_uefi(ph, uefi)
        ph.su("rm -rf " + WORK, check=False, mutating=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if ARGS.dry_run:
        say("\nPre-check finished. Nothing on the device was changed.")
        return
    if ARGS.no_reboot:
        say("\nDone. Boot the UEFI slot with:  fastboot set_active %s && fastboot reboot"
            % (other_slot(ph.slot)[1:] or "<slot>"))
    else:
        switch_to_uefi(ph, tools)
        say("\nDone. The device is rebooting into UEFI/Windows.")
        say("First boot runs Windows setup and can take several minutes.")
    if ph.slot:
        say("Back to Android any time: Boot Manager -> 'Reboot to other slot', or")
        say("  fastboot set_active %s && fastboot reboot" % ph.slot[1:])
    else:
        say("Restore Android by flashing the backup:  fastboot flash boot <backup.img>")


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print("\nERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        sys.exit(130)
