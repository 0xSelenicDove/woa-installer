# woa-installer

One-command installer for **Windows on ARM + edk2-msm UEFI** on rooted Android devices, run from a **Windows or macOS** PC. It automates the manual dual-boot flow: back up the running boot slot, put Windows on the ESP + NTFS partitions, copy the boot slot into the other slot, run the UEFI installer zip against it, and boot it.

You supply a **Windows 11 ARM64 ISO** and, for your device, a **UEFI installer zip** (`*_UEFI.fd` + `BootShim.Dualboot.bin` + `update-binary`, the edk2-msm format). A UEFI installer is always required and nothing is built in: pass `--uefi-zip PATH`, or `--repo` with `owner/name` or a full github.com link to download that repository's latest `uefi-installer*.zip` (the wizard has a field for it that checks the release before you continue).

> The installer is device-agnostic, but the UEFI firmware is not: Windows only boots on a device if someone has ported edk2 to it. The installer refuses a zip whose target device name does not match the connected one (`--force-device` overrides).

## Before you start

* Unlocked bootloader, Magisk root (grant the `adb shell` user superuser), USB debugging on.
* An **A/B** device (recommended): Android keeps running from the current slot and the UEFI goes in the other one. Non-A/B devices need `--allow-non-ab` and the UEFI replaces your only boot image.
* ESP and Windows partitions: auto-detected by label (`esp`, `win`, ...) or via `--esp-part` / `--win-part`. If they don't exist, run once with `--create-partitions` (see below).
* ~25 GB free on the PC for the extracted image.

PC requirements: Python 3.8+. macOS also needs `brew install wimlib`; on Windows `wimlib`, `adb` and `fastboot` are downloaded automatically (into `~/.woa-installer`).

## Use

**Guided GUI:** `python3 woa_gui.py` (macOS: double-click `install-macos.command`; Windows: `install-windows.bat`). A seven-step wizard (Welcome, Device, Files, Storage, Review, Install, Done) checks the device, validates your ISO and UEFI zip, explains what will be erased, and defaults to a read-only **rehearsal** before the real install. Follows the system light/dark theme. Release downloads: `woa-installer-macos.dmg` (Apple Silicon) and `woa-installer-windows.exe`, both bundling Python and unsigned (macOS: right-click > Open the first time; Windows SmartScreen: More info > Run anyway); from source the GUI needs Tk (included with python.org Python).

**Command line**

macOS (`install-macos.command --cli` also works):

```bash
python3 woa_installer.py --iso ~/Downloads/Win11_ARM64.iso
```

Windows:

```bat
py -3 woa_installer.py --iso C:\Users\me\Downloads\Win11_ARM64.iso
```

Useful flags: `--uefi-zip PATH`, `--repo owner/name|github-link`, `--esp-part`, `--win-part`, `--allow-non-ab`, `--image-index N` (default: Pro), `--bcd PATH`, `--create-partitions`, `--esp-size`, `--win-size`, `--phone-tools`, `--dry-run` (read-only rehearsal), `--skip-windows` (UEFI only), `--no-reboot`.

## Unattended first boot

By default the installer writes an `unattend.xml` into the Windows image so the first boot needs no network and shows no setup screens:

* creates a local administrator (`--user`, `--password`, default `user` / blank) and signs in automatically, straight to the desktop;
* skips the network, Microsoft-account, EULA and privacy screens (`BypassNRO`, `DisablePrivacyExperience`, `ProtectYourPC=3`, hidden online-account and wireless pages);
* turns off first-logon animation, consumer extras and automatic BitLocker device encryption;
* sets the device name, display language, region & format and time zone (`--hostname`, `--language`, `--locale`, `--timezone`). The display language must exist in the image; the wizard reads the image's editions and languages: an Edition picker (multi-version ISOs) and a Language picker, each disabled when the image offers only one choice. Region & format (dates, numbers, keyboard) is a separate picker, and the time zone is a list of Windows time zone names (the host's, when running on Windows).

`--drivers DIR` copies a folder of `.inf` drivers (e.g. WiFi) to `C:\Drivers` and installs them at first logon with `pnputil`. `--no-unattend` turns all of this off. The password is stored in plain text in the install files, so use a throwaway one. This is not yet verified on a real boot; `tests/test_unattend.py` checks the generated XML.

## Creating partitions

`--create-partitions` (sizes: `--esp-size 512M`, `--win-size 64G`) edits the GPT of the disk holding `userdata`:

* If there is enough free space after the last partition, it just adds `esp` and `win` there.
* Otherwise it shrinks `userdata` (it must be the last partition). **That erases all device data**, so you must type `WIPE DATA`. The installer then runs `fastboot erase userdata` + `erase metadata` and reboots; finish Android setup, re-enable USB debugging, make sure root works, and run the installer again.
* The old GPT is saved to `~/.woa-installer/backup/`, both copies are rewritten with valid CRCs, and the result is re-read and verified before rebooting. Always try `--dry-run` first.

## NTFS and FAT on the device

Android has no NTFS-3G, so the installer pushes static aarch64 `mkntfs`, `ntfs-3g` and `mkfs.fat` if the device lacks them. They come from this repo's release asset `phone-tools-aarch64.tar.gz` (checksum-verified), built by `.github/workflows/phone-tools.yml` / `phone-tools/build.sh`, or from a folder via `--phone-tools DIR`. Both partitions are formatted fresh (NTFS quick-format with the device's sector size).

## BCD

The Windows boot config is not generated on the PC (macOS cannot run `bcdboot`). The installer uses, in order: `--bcd`, `assets/BCD`, or the BCD already on the device's ESP. Put a BCD with the settings from the main repo's *Optimal BCD Configuration* at `assets/BCD` for clean installs.

## Safety

* The running boot image is saved to `~/.woa-installer/backup/` (and `/sdcard`) before anything is touched, and the running slot is never modified.
* Wiping ESP/Windows requires typing `ERASE`.
* Slots are only switched if the UEFI installer reports success.
* Recover Android with `fastboot set_active <original slot> && fastboot reboot`.

## Status

Not yet run against a real device, and the phone-tools CI build has not been run yet (publish a `v*` tag to produce the release asset). `tests/test_gpt.py` checks the GPT editor offline. Known limits: files are copied with `tar` (NTFS ACLs from the image are not preserved), and no Qualcomm driver injection is done. Use `--dry-run` first.
