# woa-installer

One-command installer for **Windows on ARM + edk2-msm UEFI** on rooted Android phones, run from a **Windows or macOS** PC. It automates the manual dual-boot flow: back up the running boot slot, put Windows on the ESP + NTFS partitions, copy the boot slot into the other slot, run the UEFI installer zip against it, and boot it.

You supply a **Windows 11 ARM64 ISO** and, for your phone, a **UEFI installer zip** (`*_UEFI.fd` + `BootShim.Dualboot.bin` + `update-binary`, the edk2-msm format). For known devices (currently POCO F4 / Redmi K40S `munch`) the latest release is downloaded if you omit it; for any other phone pass `--uefi-zip` or `--repo owner/name`.

> The installer is device-agnostic, but the UEFI firmware is not: Windows only boots on a phone if someone has ported edk2 to it. The installer refuses a zip whose device name does not match the phone (`--force-device` overrides).

## Before you start

* Unlocked bootloader, Magisk root (grant the `adb shell` user superuser), USB debugging on.
* An **A/B** device (recommended): Android keeps running from the current slot and the UEFI goes in the other one. Non-A/B phones need `--allow-non-ab` and the UEFI replaces your only boot image.
* ESP (FAT32) and Windows (NTFS, 20 GB+) partitions already exist. They are auto-detected by common by-name labels (`esp`, `win`, ...); otherwise pass `--esp-part` / `--win-part` (a label or `sdXN`). Creating or resizing partitions is **not** automated (it means shrinking `userdata`, which wipes the phone).
* ~25 GB free on the PC for the extracted image.
* An NTFS-3G binary on the phone (`mount.ntfs` / `ntfs-3g`, e.g. a Magisk module). `mkntfs` is optional; without it the partition is cleared instead of reformatted.

PC requirements: Python 3.8+. macOS also needs `brew install wimlib`; on Windows `wimlib`, `adb` and `fastboot` are downloaded automatically (into `~/.woa-installer`).

## Use

macOS: double-click `install-macos.command`, or

```bash
python3 munch_installer.py --iso ~/Downloads/Win11_ARM64.iso
```

Windows: double-click `install-windows.bat`, or

```bat
py -3 munch_installer.py --iso C:\Users\me\Downloads\Win11_ARM64.iso
```

Useful flags: `--uefi-zip PATH`, `--repo owner/name`, `--esp-part`, `--win-part`, `--allow-non-ab`, `--image-index N` (default: Pro), `--bcd PATH`, `--dry-run` (read-only rehearsal), `--skip-windows` (UEFI only), `--no-reboot`.

## BCD

The Windows boot config is not generated on the PC (macOS cannot run `bcdboot`). The installer uses, in order: `--bcd`, `assets/BCD`, or the BCD already on the phone's ESP. Put a BCD with the settings from the main repo's *Optimal BCD Configuration* at `assets/BCD` for clean installs.

## Safety

* The running boot image is saved to `~/.woa-installer/backup/` (and `/sdcard`) before anything is touched, and the running slot is never modified.
* Wiping ESP/Windows requires typing `ERASE`.
* Slots are only switched if the UEFI installer reports success.
* Recover Android with `fastboot set_active <original slot> && fastboot reboot`.

## Status

Not yet run against a real phone. Known limits: files are copied with `tar` (NTFS ACLs from the image are not preserved), and no Qualcomm driver injection is done. Use `--dry-run` first.
