# munch-installer

One-command installer for **Windows 11 ARM64 + edk2-msm UEFI** on the Xiaomi POCO F4 / Redmi K40S (`munch`), run from a **Windows or macOS** PC. It automates the manual flow from [edk2-msm-munch](https://github.com/0xSelenicDove/edk2-msm-munch): back up `boot_a`, put Windows on the ESP + NTFS partitions, copy `boot_a` into `boot_b`, run the UEFI installer against slot B, and boot it.

You supply a **Windows 11 ARM64 ISO**. Optionally supply a compatible UEFI zip (`uefi-installer-munch.zip`); otherwise the latest release is downloaded.

## Before you start

* Unlocked bootloader, Magisk root (grant the `adb shell` user superuser), USB debugging on.
* Android running from **slot A**.
* The ESP (`sda35`, FAT32) and Windows (`sda36`, NTFS, 20 GB+) partitions already exist. Creating or resizing partitions is **not** automated (it means shrinking `userdata`, which wipes the phone). Use `--esp-part` / `--win-part` if yours differ.
* Free disk space on the PC: ~25 GB for the extracted image.
* An NTFS-3G binary on the phone (`mount.ntfs` / `ntfs-3g`, e.g. from a Magisk module). `mkntfs` is optional; without it the partition is cleared instead of reformatted.

PC requirements: Python 3.8+. macOS also needs `brew install wimlib`; on Windows `wimlib`, `adb` and `fastboot` are downloaded automatically (into `~/.munch-installer`).

## Use

macOS: double-click `install-macos.command`, or

```bash
python3 munch_installer.py --iso ~/Downloads/Win11_ARM64.iso
```

Windows: double-click `install-windows.bat`, or

```bat
py -3 munch_installer.py --iso C:\Users\me\Downloads\Win11_ARM64.iso
```

Useful flags: `--uefi-zip PATH`, `--image-index N` (default: Pro), `--bcd PATH`, `--dry-run` (read-only rehearsal), `--skip-windows` (UEFI only), `--no-reboot`.

## BCD

The Windows boot config is not generated on the PC (macOS cannot run `bcdboot`). The installer uses, in order: `--bcd`, `assets/BCD`, or the BCD already on the phone's ESP. Put a BCD with the settings from the main repo's *Optimal BCD Configuration* at `assets/BCD` for clean installs.

## Safety

* `boot_a` is saved to `~/.munch-installer/backup/` (and `/sdcard/boot_a_stock.img`) before anything is touched, and slot A is never modified.
* Wiping ESP/Windows requires typing `ERASE`.
* Slots are only switched if the UEFI installer reports success.
* Recover Android with `fastboot set_active a && fastboot reboot`.

## Status

Not yet run against a real phone. Known limits: files are copied with `tar` (NTFS ACLs from the image are not preserved), and no Qualcomm driver injection is done. Use `--dry-run` first.
