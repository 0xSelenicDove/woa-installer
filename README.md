# woa-installer

One-command installer for **Windows on ARM + edk2-msm UEFI** on rooted Android phones, run from a **Windows or macOS** PC. It automates the manual dual-boot flow: back up the running boot slot, put Windows on the ESP + NTFS partitions, copy the boot slot into the other slot, run the UEFI installer zip against it, and boot it.

You supply a **Windows 11 ARM64 ISO** and, for your phone, a **UEFI installer zip** (`*_UEFI.fd` + `BootShim.Dualboot.bin` + `update-binary`, the edk2-msm format). For known devices (currently POCO F4 / Redmi K40S `munch`) the latest release is downloaded if you omit it; for any other phone pass `--uefi-zip` or `--repo owner/name`.

> The installer is device-agnostic, but the UEFI firmware is not: Windows only boots on a phone if someone has ported edk2 to it. The installer refuses a zip whose device name does not match the phone (`--force-device` overrides).

## Before you start

* Unlocked bootloader, Magisk root (grant the `adb shell` user superuser), USB debugging on.
* An **A/B** device (recommended): Android keeps running from the current slot and the UEFI goes in the other one. Non-A/B phones need `--allow-non-ab` and the UEFI replaces your only boot image.
* ESP and Windows partitions: auto-detected by label (`esp`, `win`, ...) or via `--esp-part` / `--win-part`. If they don't exist, run once with `--create-partitions` (see below).
* ~25 GB free on the PC for the extracted image.

PC requirements: Python 3.8+. macOS also needs `brew install wimlib`; on Windows `wimlib`, `adb` and `fastboot` are downloaded automatically (into `~/.woa-installer`).

## Use

**GUI:** run `python3 woa_gui.py` (macOS: double-click `install-macos.command`; Windows: double-click `install-windows.bat`), pick your ISO (and optionally a UEFI zip), leave **Dry run** ticked for a first pass, then Start. Confirmations (`ERASE`, `WIPE DATA`) appear as dialogs. Release builds (`woa-installer-macos.zip`, `woa-installer-windows.zip`, built by `.github/workflows/gui.yml`) bundle Python; the GUI needs Tk, which ships with python.org Python.

**Command line**

macOS (`install-macos.command --cli` also works):

```bash
python3 woa_installer.py --iso ~/Downloads/Win11_ARM64.iso
```

Windows: double-click `install-windows.bat`, or

```bat
py -3 woa_installer.py --iso C:\Users\me\Downloads\Win11_ARM64.iso
```

Useful flags: `--uefi-zip PATH`, `--repo owner/name`, `--esp-part`, `--win-part`, `--allow-non-ab`, `--image-index N` (default: Pro), `--bcd PATH`, `--create-partitions`, `--esp-size`, `--win-size`, `--phone-tools`, `--dry-run` (read-only rehearsal), `--skip-windows` (UEFI only), `--no-reboot`.

## Creating partitions

`--create-partitions` (sizes: `--esp-size 512M`, `--win-size 64G`) edits the GPT of the disk holding `userdata`:

* If there is enough free space after the last partition, it just adds `esp` and `win` there.
* Otherwise it shrinks `userdata` (it must be the last partition). **That erases all phone data**, so you must type `WIPE DATA`. The installer then runs `fastboot erase userdata` + `erase metadata` and reboots; finish Android setup, re-enable USB debugging, make sure root works, and run the installer again.
* The old GPT is saved to `~/.woa-installer/backup/`, both copies are rewritten with valid CRCs, and the result is re-read and verified before rebooting. Always try `--dry-run` first.

## NTFS and FAT on the phone

Android has no NTFS-3G, so the installer pushes static aarch64 `mkntfs`, `ntfs-3g` and `mkfs.fat` if the phone lacks them. They come from this repo's release asset `phone-tools-aarch64.tar.gz` (checksum-verified), built by `.github/workflows/phone-tools.yml` / `phone-tools/build.sh`, or from a folder via `--phone-tools DIR`. Both partitions are formatted fresh (NTFS quick-format with the device's sector size).

## BCD

The Windows boot config is not generated on the PC (macOS cannot run `bcdboot`). The installer uses, in order: `--bcd`, `assets/BCD`, or the BCD already on the phone's ESP. Put a BCD with the settings from the main repo's *Optimal BCD Configuration* at `assets/BCD` for clean installs.

## Safety

* The running boot image is saved to `~/.woa-installer/backup/` (and `/sdcard`) before anything is touched, and the running slot is never modified.
* Wiping ESP/Windows requires typing `ERASE`.
* Slots are only switched if the UEFI installer reports success.
* Recover Android with `fastboot set_active <original slot> && fastboot reboot`.

## Status

Not yet run against a real phone, and the phone-tools CI build has not been run yet (publish a `v*` tag to produce the release asset). `tests/test_gpt.py` checks the GPT editor offline. Known limits: files are copied with `tar` (NTFS ACLs from the image are not preserved), and no Qualcomm driver injection is done. Use `--dry-run` first.
