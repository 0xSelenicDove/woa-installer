#!/usr/bin/env python3
"""munch-installer: one-shot Windows 11 ARM64 + edk2-msm UEFI installer for the
Xiaomi POCO F4 / Redmi K40S (munch), driven from a Windows or macOS PC.

Automates steps 2-5 of the edk2-msm-munch README plus building the Windows
install on the phone's existing ESP (sda35) and NTFS (sda36) partitions:

  1. check phone (adb, root, munch, unlocked, partitions)
  2. back up boot_a (host + phone)
  3. read the user's ARM64 Windows ISO, pick an edition
  4. wipe + populate ESP/NTFS partitions over adb (root)
  5. copy boot_a -> boot_b, run the UEFI installer zip against slot B
  6. fastboot set_active b, reboot

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
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".munch-installer"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""

RELEASES_API = "https://api.github.com/repos/0xSelenicDove/edk2-msm-munch/releases/latest"
UEFI_ASSET = "uefi-installer-munch.zip"
PT_URL = "https://dl.google.com/android/repository/platform-tools-latest-%s.zip" % (
    "windows" if IS_WIN else "darwin")
WIMLIB_WIN_URL = "https://wimlib.net/downloads/wimlib-1.14.4-windows-x86_64-bin.zip"

SUPPORTED_DEVICES = {"munch", "munch_in", "alioth_munch"}
BOOT_BY_NAME = "/dev/block/bootdevice/by-name"
WORK = "/data/local/tmp/munch-install"          # on the phone


class Fail(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def step(n, msg):
    say("\n[%s] %s" % (n, msg))


def ask_yes(prompt):
    return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024


# --------------------------------------------------------------------------- #
# Host tools
# --------------------------------------------------------------------------- #
def download(url, dest, what):
    say("  downloading %s ..." % what)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "munch-installer"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


class Tools:
    def __init__(self, platform_tools=None):
        self.adb = self.fastboot = self.wimlib = None
        self._find_platform_tools(platform_tools)
        self._find_wimlib()

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
        w = shutil.which("wimlib-imagex")
        if w:
            self.wimlib = w
            return
        if IS_WIN:
            d = CACHE / "wimlib"
            exe = d / "wimlib-imagex.exe"
            if not exe.exists():
                z = CACHE / "wimlib.zip"
                download(WIMLIB_WIN_URL, z, "wimlib")
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(d)
                z.unlink()
            self.wimlib = str(exe)
            return
        raise Fail("wimlib-imagex not found. Install it first:  brew install wimlib")


def run(cmd, check=True, capture=True, **kw):
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

    def adb(self, *args, **kw):
        return run([self.t.adb, *args], **kw)

    def sh(self, cmd, check=True):
        """Run as the shell user."""
        return self.adb("shell", cmd, check=check).stdout.decode(errors="replace").strip()

    def su(self, cmd, check=True, mutating=False):
        """Run as root through Magisk su (cmd must not contain single quotes)."""
        assert "'" not in cmd, cmd
        if mutating and self.dry:
            say("  [dry-run] su: " + cmd)
            return ""
        r = self.adb("shell", "su -c '%s'" % cmd, check=check)
        return r.stdout.decode(errors="replace").strip()

    def prop(self, name):
        return self.sh("getprop " + name)

    def stream_in(self, cmd):
        """Popen of `adb exec-in su -c cmd`; caller writes binary to .stdin."""
        assert "'" not in cmd
        return subprocess.Popen([self.t.adb, "exec-in", "su -c '%s'" % cmd],
                                stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def wait_device(self):
        run([self.t.adb, "start-server"], check=False)
        for _ in range(30):
            out = self.adb("devices", check=False).stdout.decode()
            lines = [l for l in out.splitlines()[1:] if l.strip()]
            if any(l.endswith("device") for l in lines):
                return
            if any("unauthorized" in l for l in lines):
                say("  Phone shows an 'Allow USB debugging?' prompt - accept it.")
            time.sleep(2)
        raise Fail("No phone found over adb. Enable USB debugging and connect it.")


def check_phone(ph):
    ph.wait_device()
    dev = ph.prop("ro.product.device")
    say("  device: %s (%s)" % (dev, ph.prop("ro.product.model")))
    if dev not in SUPPORTED_DEVICES:
        raise Fail("Unsupported device '%s'. Only munch / POCO F4 / Redmi K40S." % dev)
    if "uid=0" not in ph.su("id", check=False):
        raise Fail("No root. Grant the adb shell superuser access in Magisk and retry.")
    vb = ph.prop("ro.boot.verifiedbootstate")
    if vb and vb != "orange":
        raise Fail("Bootloader looks locked (verifiedbootstate=%s). Unlock it first." % vb)
    slot = ph.prop("ro.boot.slot_suffix")
    say("  root: ok   bootloader: unlocked   current slot: %s" % (slot or "?"))
    if slot != "_a":
        say("  WARNING: expected to be running Android from slot A (got %r)." % slot)
        if not ask_yes("  Continue anyway?"):
            raise Fail("aborted")
    return slot


def find_partitions(ph, esp, win):
    out = {}
    for label, name in (("ESP", esp), ("Windows", win)):
        dev = "/dev/block/" + name
        sz = ph.su("blockdev --getsize64 " + dev, check=False)
        if not sz.isdigit():
            raise Fail(
                "Partition %s (%s) does not exist.\n"
                "Create the ESP and Windows partitions first (see README, 'Partitions'),\n"
                "or pass --esp-part/--win-part." % (dev, label))
        out[label] = (dev, int(sz))
        say("  %-8s %-20s %s" % (label, dev, human(int(sz))))
    if out["Windows"][1] < 20 * 1024 ** 3:
        raise Fail("The Windows partition is under 20 GB; it needs room for Windows.")
    if out["ESP"][1] > 2 * 1024 ** 3:
        raise Fail("The ESP is suspiciously large - wrong partition? Check --esp-part.")
    return out


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
                       "arch": g("Architecture"), "build": g("Build")})
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


# --------------------------------------------------------------------------- #
# UEFI package
# --------------------------------------------------------------------------- #
def get_uefi_zip(path):
    if path:
        z = Path(path)
        if not z.is_file():
            raise Fail("UEFI zip not found: %s" % z)
    else:
        z = CACHE / UEFI_ASSET
        say("  no --uefi-zip given; fetching the latest release")
        req = urllib.request.Request(RELEASES_API, headers={"User-Agent": "munch-installer"})
        with urllib.request.urlopen(req) as r:
            rel = json.load(r)
        asset = next((a for a in rel.get("assets", []) if a["name"] == UEFI_ASSET), None)
        if not asset:
            raise Fail("Latest release (%s) has no %s asset." % (rel.get("tag_name"), UEFI_ASSET))
        download(asset["browser_download_url"], z, "%s %s" % (UEFI_ASSET, rel.get("tag_name")))
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    ok = (any(n.endswith("_UEFI.fd") for n in names)
          and "BootShim.Dualboot.bin" in names
          and "META-INF/com/google/android/update-binary" in names)
    if not ok:
        raise Fail("%s is not a compatible UEFI installer zip (needs *_UEFI.fd, "
                   "BootShim.Dualboot.bin, META-INF/.../update-binary)." % z.name)
    sha = hashlib.sha256(z.read_bytes()).hexdigest()
    say("  UEFI zip: %s  sha256 %s..." % (z.name, sha[:16]))
    return z


# --------------------------------------------------------------------------- #
# Phone-side filesystem work
# --------------------------------------------------------------------------- #
def mount_ntfs(ph, dev, mp):
    ph.su("mkdir -p " + mp, mutating=True)
    for cmd in ("mount.ntfs -o rw,noatime %s %s" % (dev, mp),
                "ntfs-3g -o rw,noatime %s %s" % (dev, mp),
                "mount -t ntfs3 -o noatime %s %s" % (dev, mp)):
        if ph.dry:
            say("  [dry-run] su: " + cmd)
            return
        ph.su(cmd + " 2>&1", check=False)
        if ph.su("grep -c ' %s ' /proc/mounts" % mp, check=False).strip() not in ("", "0"):
            return
    raise Fail("Could not mount NTFS read-write on the phone (tried mount.ntfs, ntfs-3g, ntfs3).\n"
               "Install an NTFS-3G Magisk module and retry.")


def mount_vfat(ph, dev, mp):
    ph.su("mkdir -p " + mp, mutating=True)
    ph.su("mount -t vfat -o rw,noatime,umask=000 %s %s" % (dev, mp), mutating=True)


def wipe(ph, dev, mp, label):
    fmt = ph.su("which mkntfs mkfs.ntfs", check=False) if label == "Windows" else ""
    if fmt:
        say("  quick-formatting %s ..." % label)
        ph.su("umount %s" % mp, check=False, mutating=True)
        tool = fmt.splitlines()[0]
        ph.su("%s -f -L Windows -s 4096 %s" % (tool, dev), mutating=True)
        mount_ntfs(ph, dev, mp)
    else:
        say("  clearing %s ..." % label)
        ph.su("find %s -mindepth 1 -maxdepth 1 -exec rm -rf {} +" % mp, mutating=True)


def stream_tree(ph, src, dest_cmd_dir, label, esp=False):
    """tar `src` straight into `su tar -x` on the phone."""
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
        say("  [dry-run] would stream to " + dest_cmd_dir)
        return
    proc = ph.stream_in("tar -x -C %s" % dest_cmd_dir)
    done = [0, 0.0]

    def flt(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o755 if ti.isdir() else 0o644
        return ti

    class Counting:
        def write(self, b):
            proc.stdin.write(b)
            done[0] += len(b)
            if time.time() - done[1] > 2:
                done[1] = time.time()
                print("\r  %s / %s" % (human(done[0]), human(total)), end="", flush=True)
            return len(b)

    try:
        with tarfile.open(fileobj=Counting(), mode="w|", format=tarfile.GNU_FORMAT) as tf:
            for name in sorted(os.listdir(src)):
                tf.add(os.path.join(src, name), arcname=name, filter=flt)
    except BrokenPipeError:
        pass
    proc.stdin.close()
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise Fail("tar on the phone failed: " + err)
    print()


def build_esp_tree(win_tree, bcd, esp_dir):
    """Lay out EFI/Microsoft/Boot + EFI/Boot/bootaa64.efi from the applied image."""
    boot_src = win_tree / "Windows" / "Boot"
    dst = esp_dir / "EFI" / "Microsoft" / "Boot"
    dst.mkdir(parents=True)
    efi = boot_src / "EFI"
    if not (efi / "bootmgfw.efi").exists():
        raise Fail("Image has no Windows/Boot/EFI/bootmgfw.efi - not an ARM64 UEFI image?")
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


def resolve_bcd(ph, arg, esp_mp):
    if arg:
        p = Path(arg)
        if not p.is_file():
            raise Fail("BCD not found: %s" % p)
        return p
    tpl = HERE / "assets" / "BCD"
    if tpl.is_file():
        return tpl
    say("  no BCD template; trying to reuse the BCD already on the phone's ESP")
    mount_vfat(ph, "/dev/block/" + ARGS.esp_part, esp_mp)
    old = esp_mp + "/EFI/Microsoft/Boot/BCD"
    if ph.su("[ -f %s ] && echo y" % old, check=False) != "y":
        raise Fail(
            "No BCD available. Provide one with --bcd, drop it at assets/BCD, or install\n"
            "Windows once so the ESP already has EFI/Microsoft/Boot/BCD.")
    out = Path(tempfile.mkdtemp(prefix="munch-bcd-")) / "BCD"
    with open(out, "wb") as f:
        f.write(ph.adb("exec-out", "su -c 'cat %s'" % old).stdout)
    ph.su("umount " + esp_mp, check=False)
    return out


# --------------------------------------------------------------------------- #
# Backup / UEFI install / switch
# --------------------------------------------------------------------------- #
def backup_boot_a(ph, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / ("boot_a_stock_%s.img" % time.strftime("%Y%m%d-%H%M%S"))
    say("  reading boot_a -> %s" % dest)
    data = ph.adb("exec-out", "su -c 'dd if=%s/boot_a bs=1M 2>/dev/null'" % BOOT_BY_NAME).stdout
    if len(data) < 16 * 1024 * 1024 or data[:8] != b"ANDROID!":
        raise Fail("boot_a backup looks wrong (%d bytes). Not continuing." % len(data))
    dest.write_bytes(data)
    ph.su("cp %s /sdcard/boot_a_stock.img" % "/dev/block/bootdevice/by-name/boot_a", check=False,
          mutating=True)
    return dest


def install_uefi(ph, zip_path):
    say("  copying boot_a -> boot_b")
    ph.su("dd if=%s/boot_a of=%s/boot_b bs=1M" % (BOOT_BY_NAME, BOOT_BY_NAME), mutating=True)
    ph.su("rm -rf %s/inst && mkdir -p %s/inst" % (WORK, WORK), mutating=True)
    if not ph.dry:
        ph.adb("push", str(zip_path), WORK + "/uefi-installer.zip")
    ph.su("unzip -o %s/uefi-installer.zip -d %s/inst" % (WORK, WORK), mutating=True)
    ph.su("chmod -R 755 %s/inst" % WORK, mutating=True)
    ub = WORK + "/inst/META-INF/com/google/android/update-binary"
    ph.su('sed -i "s/^slot=.*/slot=_b/" ' + ub, mutating=True)
    if not ph.dry and ph.su("grep -c '^slot=_b' " + ub, check=False) != "1":
        raise Fail("could not retarget the installer at slot B")
    say("  running the UEFI installer against slot B")
    out = ph.su("cd %s/inst && sh %s dummy 1 %s/uefi-installer.zip 2>&1" % (WORK, ub, WORK),
                mutating=True)
    say("    " + out.replace("\n", "\n    "))
    if not ph.dry and "Done installing" not in out:
        raise Fail("UEFI installer did not report success - NOT switching slots.")


def switch_to_b(ph, tools):
    if ph.dry:
        say("  [dry-run] adb reboot bootloader; fastboot set_active b; fastboot reboot")
        return
    ph.adb("reboot", "bootloader", check=False)
    for _ in range(40):
        if b"fastboot" in run([tools.fastboot, "devices"], check=False).stdout:
            break
        time.sleep(2)
    else:
        raise Fail("Phone did not appear in fastboot. Hold Power + Volume Down, then run:\n"
                   "  fastboot set_active b && fastboot reboot")
    run([tools.fastboot, "set_active", "b"])
    run([tools.fastboot, "reboot"])


# --------------------------------------------------------------------------- #
def main():
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="Windows 11 ARM64 ISO (prompted if omitted)")
    ap.add_argument("--uefi-zip", help="uefi-installer-munch.zip (default: latest GitHub release)")
    ap.add_argument("--image-index", type=int, help="install.wim image index (default: Pro)")
    ap.add_argument("--bcd", help="BCD hive for the ESP (default: assets/BCD or the phone's existing one)")
    ap.add_argument("--esp-part", default="sda35")
    ap.add_argument("--win-part", default="sda36")
    ap.add_argument("--platform-tools", help="folder with adb/fastboot")
    ap.add_argument("--backup-dir", default=str(CACHE / "backup"))
    ap.add_argument("--skip-windows", action="store_true", help="only install UEFI into slot B")
    ap.add_argument("--no-reboot", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="read-only: show what would be written")
    ARGS = ap.parse_args()

    say("munch-installer - Windows 11 ARM64 on POCO F4 (munch)")
    if ARGS.dry_run:
        say("DRY RUN: nothing on the phone will be modified.")

    iso = ARGS.iso
    if not iso and not ARGS.skip_windows:
        iso = input("Path to your Windows 11 ARM64 ISO: ").strip().strip('"')
    if not ARGS.skip_windows and not Path(iso).is_file():
        raise Fail("ISO not found: %s" % iso)

    tools = Tools(ARGS.platform_tools)
    ph = Phone(tools, ARGS.dry_run)

    step("1/6", "Checking the phone")
    check_phone(ph)
    parts = find_partitions(ph, ARGS.esp_part, ARGS.win_part) if not ARGS.skip_windows else None

    step("2/6", "UEFI package")
    uefi = get_uefi_zip(ARGS.uefi_zip)

    step("3/6", "Backing up boot_a")
    bk = backup_boot_a(ph, Path(ARGS.backup_dir))
    say("  saved %s (keep this!)" % bk)

    tmp = Path(tempfile.mkdtemp(prefix="munch-install-"))
    try:
        if not ARGS.skip_windows:
            step("4/6", "Reading the Windows ISO")
            with MountedIso(iso) as mp:
                wim = find_wim(mp)
                img = pick_image(list_images(tools, wim), ARGS.image_index)
                say("  using image %d: %s" % (img["index"], img["name"]))
                say("\n  This will ERASE %s (ESP) and %s (Windows) on the phone." %
                    (parts["ESP"][0], parts["Windows"][0]))
                if not ARGS.dry_run and input("  Type ERASE to continue: ").strip() != "ERASE":
                    raise Fail("aborted")
                say("  extracting the image (this takes a while) ...")
                tree = tmp / "win"
                tree.mkdir()
                run([tools.wimlib, "apply", str(wim), str(img["index"]), str(tree), "--no-acls"],
                    capture=False)

            step("5/6", "Writing Windows to the phone")
            esp_mp, win_mp = WORK + "/esp", WORK + "/win"
            bcd = resolve_bcd(ph, ARGS.bcd, esp_mp)
            esp_tree = tmp / "esp"
            esp_tree.mkdir()
            build_esp_tree(tree, bcd, esp_tree)
            for dev, mp in ((parts["ESP"][0], esp_mp), (parts["Windows"][0], win_mp)):
                ph.su("umount %s" % mp, check=False, mutating=True)
            mount_vfat(ph, parts["ESP"][0], esp_mp)
            mount_ntfs(ph, parts["Windows"][0], win_mp)
            wipe(ph, parts["ESP"][0], esp_mp, "ESP")
            wipe(ph, parts["Windows"][0], win_mp, "Windows")
            stream_tree(ph, str(esp_tree), esp_mp, "ESP")
            stream_tree(ph, str(tree), win_mp, "Windows")
            if not ph.dry:
                for f, where in ((win_mp + "/Windows/System32/winload.efi", "winload.efi"),
                                 (esp_mp + "/EFI/Microsoft/Boot/bootmgfw.efi", "bootmgfw.efi"),
                                 (esp_mp + "/EFI/Microsoft/Boot/BCD", "BCD")):
                    if ph.su("[ -f %s ] && echo y" % f, check=False) != "y":
                        raise Fail("verification failed: %s missing on the phone" % where)
            ph.su("sync", mutating=True)
            ph.su("umount %s; umount %s" % (esp_mp, win_mp), check=False, mutating=True)
            say("  Windows files written and verified.")
        else:
            step("4/6", "Skipping Windows (--skip-windows)")
            step("5/6", "Skipping Windows (--skip-windows)")

        step("6/6", "Installing UEFI into slot B")
        install_uefi(ph, uefi)
        ph.su("rm -rf " + WORK, check=False, mutating=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if ARGS.no_reboot:
        say("\nDone. Boot slot B with:  fastboot set_active b && fastboot reboot")
    else:
        switch_to_b(ph, tools)
        say("\nDone. The phone is rebooting into UEFI/Windows (slot B).")
        say("First boot runs Windows setup and can take several minutes.")
    say("Back to Android any time: Boot Manager -> 'Reboot to other slot', or")
    say("  fastboot set_active a && fastboot reboot")


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print("\nERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        sys.exit(130)
