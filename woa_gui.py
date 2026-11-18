#!/usr/bin/env python3
"""Guided Tk wizard for woa_installer (standard library only).

Design notes: one decision per screen, a visible step list, plain-language copy,
read-only "rehearsal" on by default, destructive confirmations spelled out, and a
live checklist (not a raw log) while installing. The log is one click away.
"""
import contextlib
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import zipfile
from pathlib import Path
from tkinter import filedialog

import woa_installer as w

LIGHT = dict(bg="#F5F6F8", card="#FFFFFF", border="#E2E5EA", text="#151A23", muted="#667085",
             accent="#4F46E5", accent_hover="#4338CA", accent_fg="#FFFFFF", ok="#16A34A",
             warn="#B45309", warn_bg="#FEF3C7", err="#DC2626", err_bg="#FEE2E2",
             ok_bg="#DCFCE7", side="#0F172A", side_text="#CBD5E1", side_active="#FFFFFF",
             sec="#EEF0F4", sec_hover="#E2E5EA", log="#0F172A", log_fg="#E2E8F0")
DARK = dict(bg="#11131A", card="#1A1D27", border="#2A2F3C", text="#E8EAF0", muted="#98A2B3",
            accent="#6366F1", accent_hover="#818CF8", accent_fg="#FFFFFF", ok="#4ADE80",
            warn="#FBBF24", warn_bg="#3A2E10", err="#F87171", err_bg="#3F1D1D",
            ok_bg="#12301F", side="#0A0C11", side_text="#98A2B3", side_active="#FFFFFF",
            sec="#242836", sec_hover="#2F3446", log="#0A0C11", log_fg="#E2E8F0")


def is_dark():
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                               capture_output=True, text=True)
            return "Dark" in r.stdout
        if os.name == "nt":
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:       # noqa: BLE001 - theme detection is best effort
        pass
    return False


C = DARK if is_dark() else LIGHT
STEPS = ["Welcome", "Device", "Files", "Storage", "Review", "Install", "Done"]
JOBS = ["Check the device", "Prepare UEFI & partitions", "Back up boot image",
        "Read Windows ISO", "Write Windows", "Install UEFI"]


# --------------------------------------------------------------------------- widgets
class Btn(tk.Label):
    def __init__(self, parent, text, command, kind="primary", **kw):
        self.kind, self.command, self.enabled = kind, command, True
        super().__init__(parent, text=text, padx=20, pady=9, cursor="hand2", **kw)
        self.bind("<Button-1>", lambda e: self.enabled and self.command())
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self._paint(False)

    def _paint(self, hover):
        if not self.enabled:
            self.configure(bg=C["sec"], fg=C["muted"], cursor="arrow")
        elif self.kind == "primary":
            self.configure(bg=C["accent_hover"] if hover else C["accent"], fg=C["accent_fg"],
                           cursor="hand2")
        else:
            self.configure(bg=C["sec_hover"] if hover else C["sec"], fg=C["text"], cursor="hand2")

    def set_enabled(self, on):
        self.enabled = on
        self._paint(False)


class Check(tk.Frame):
    def __init__(self, parent, text, var, bg=None, command=None):
        bg = bg or C["card"]
        super().__init__(parent, bg=bg, cursor="hand2")
        self.var, self.cmd = var, command
        self.box = tk.Label(self, bg=bg, fg=C["accent"], font=("TkDefaultFont", 15))
        self.box.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=C["text"], justify="left", anchor="w",
                            wraplength=520)
        self.lbl.pack(side="left", padx=(6, 0))
        for wdg in (self, self.box, self.lbl):
            wdg.bind("<Button-1>", self.toggle)
        self.render()

    def toggle(self, _=None):
        self.var.set(not self.var.get())
        self.render()
        if self.cmd:
            self.cmd()

    def render(self):
        self.box.configure(text="☑" if self.var.get() else "☐")


def card(parent, **kw):
    return tk.Frame(parent, bg=C["card"], highlightthickness=1, highlightbackground=C["border"], **kw)


# --------------------------------------------------------------------------- app
class Wizard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WOA Installer")
        self.geometry("980x660")
        self.minsize(900, 620)
        self.configure(bg=C["bg"])
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=12)
        fam = base.cget("family")
        self.f_title = tkfont.Font(family=fam, size=24, weight="bold")
        self.f_h = tkfont.Font(family=fam, size=14, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=11)
        self.f_mono = tkfont.Font(family="Menlo" if sys.platform == "darwin" else "Consolas", size=11)
        self.f_bold = tkfont.Font(family=fam, size=12, weight="bold")
        self.f_base = base
        self.q = queue.Queue()
        self.s = dict(info=None, iso="", uefi="", create=False, esp="512M", win="64G",
                      dry=True, nonab=False, noreboot=False, ack=False,
                      job=-1, log="", result=None, rerun=False)
        self.page = 0
        self.logbox = None
        self._shell()
        self.show(0)
        self.after(100, self._pump)

    # ---- chrome
    def _shell(self):
        side = tk.Frame(self, bg=C["side"], width=230)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="WOA Installer", bg=C["side"], fg=C["side_active"],
                 font=self.f_h, anchor="w").pack(fill="x", padx=24, pady=(28, 2))
        tk.Label(side, text="Windows on ARM for\nrooted Android devices", bg=C["side"],
                 fg=C["side_text"], font=self.f_small, justify="left", anchor="w"
                 ).pack(fill="x", padx=24, pady=(0, 24))
        self.step_rows = []
        for name in STEPS:
            row = tk.Frame(side, bg=C["side"])
            row.pack(fill="x", padx=20, pady=3)
            dot = tk.Label(row, width=2, bg=C["side"], fg=C["side_text"])
            dot.pack(side="left")
            lbl = tk.Label(row, text=name, bg=C["side"], fg=C["side_text"], anchor="w")
            lbl.pack(side="left", padx=6)
            self.step_rows.append((dot, lbl))
        right = tk.Frame(self, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)
        self.foot = tk.Frame(right, bg=C["bg"])
        self.foot.pack(side="bottom", fill="x", padx=36, pady=22)
        self.body = tk.Frame(right, bg=C["bg"])
        self.body.pack(fill="both", expand=True, padx=36, pady=(30, 0))

    def _stepper(self):
        for i, (dot, lbl) in enumerate(self.step_rows):
            if i < self.page:
                dot.configure(text="✓", fg=C["ok"])
                lbl.configure(fg=C["side_text"], font=self.f_base)
            elif i == self.page:
                dot.configure(text="●", fg=C["accent_hover"])
                lbl.configure(fg=C["side_active"], font=self.f_bold)
            else:
                dot.configure(text="○", fg=C["side_text"])
                lbl.configure(fg=C["side_text"], font=self.f_base)

    def clear(self):
        self.logbox = None
        for f in (self.body, self.foot):
            for c in f.winfo_children():
                c.destroy()

    def nav(self, back=None, nxt=None, nxt_text="Continue", nxt_ok=True):
        self.back_btn = None
        if back:
            self.back_btn = Btn(self.foot, "Back", back, "secondary")
            self.back_btn.pack(side="left")
        self.next_btn = None
        if nxt:
            self.next_btn = Btn(self.foot, nxt_text, nxt, "primary")
            self.next_btn.pack(side="right")
            self.next_btn.set_enabled(nxt_ok)

    def show(self, n):
        self.page = n
        self.clear()
        self._stepper()
        [self.p_welcome, self.p_phone, self.p_files, self.p_storage, self.p_review,
         self.p_install, self.p_done][n]()

    def title_(self, text, sub=None):
        tk.Label(self.body, text=text, bg=C["bg"], fg=C["text"], font=self.f_title,
                 anchor="w").pack(fill="x")
        if sub:
            tk.Label(self.body, text=sub, bg=C["bg"], fg=C["muted"], anchor="w", justify="left",
                     wraplength=660).pack(fill="x", pady=(6, 18))

    def callout(self, parent, text, kind="warn"):
        bg, fg = {"warn": (C["warn_bg"], C["warn"]), "err": (C["err_bg"], C["err"]),
                  "ok": (C["ok_bg"], C["ok"])}[kind]
        f = tk.Frame(parent, bg=bg)
        tk.Label(f, text=text, bg=bg, fg=fg, justify="left", anchor="w", wraplength=620,
                 padx=14, pady=10).pack(fill="x")
        return f

    # ---- pages
    def p_welcome(self):
        self.title_("Put Windows on your device",
                    "This guide installs Windows 11 ARM and a UEFI boot loader next to Android. "
                    "It takes about 30-60 minutes, most of it copying files.")
        c = card(self.body)
        c.pack(fill="x")
        tk.Label(c, text="Before you start, you need", bg=C["card"], fg=C["text"], font=self.f_h,
                 anchor="w").pack(fill="x", padx=20, pady=(16, 8))
        for t in ("A device with an unlocked bootloader and Magisk root",
                  "USB debugging turned on, and a USB cable",
                  "A Windows 11 ARM64 ISO file",
                  "A UEFI installer zip for your device (optional for the POCO F4)"):
            tk.Label(c, text="✓  " + t, bg=C["card"], fg=C["text"], anchor="w").pack(
                fill="x", padx=20, pady=3)
        tk.Frame(c, height=12, bg=C["card"]).pack()
        self.callout(self.body, "Your boot image is backed up first, and you can rehearse the whole "
                     "install without changing anything. Creating new partitions can still erase "
                     "all data on the device, so back up your files.").pack(fill="x", pady=16)
        self.nav(nxt=lambda: self.show(1), nxt_text="Get started")

    def p_phone(self):
        self.title_("Connect your device",
                    "Plug it in over USB with Android running, then check the connection.")
        steps = ("1.  Settings > Developer options > turn on USB debugging\n"
                 "2.  Connect the cable and accept the prompt on the device\n"
                 "3.  When Magisk asks, allow superuser access for the shell")
        tk.Label(self.body, text=steps, bg=C["bg"], fg=C["text"], justify="left",
                 anchor="w").pack(fill="x", pady=(0, 14))
        self.chk_btn = Btn(self.body, "Check device", self._probe)
        self.chk_btn.pack(anchor="w")
        self.res = card(self.body)
        self.res.pack(fill="x", pady=16)
        self._render_probe()
        i = self.s["info"]
        self.nav(back=lambda: self.show(0), nxt=lambda: self.show(2),
                 nxt_ok=isinstance(i, dict) and self._phone_ok(i))

    @staticmethod
    def _phone_ok(i):
        return not i["error"] and i["root"] and i["locked"] is False and not i.get("wimlib")

    def row(self, parent, state, title, detail=""):
        icon, col = {"ok": ("✓", C["ok"]), "bad": ("✕", C["err"]), "warn": ("!", C["warn"]),
                     "wait": ("…", C["muted"])}[state]
        f = tk.Frame(parent, bg=C["card"])
        f.pack(fill="x", padx=20, pady=5)
        ic = tk.Label(f, text=icon, bg=C["card"], fg=col, width=2, font=self.f_h)
        ic.pack(side="left")
        t = tk.Frame(f, bg=C["card"])
        t.pack(side="left", fill="x")
        tk.Label(t, text=title, bg=C["card"], fg=C["text"], anchor="w").pack(fill="x")
        if detail:
            tk.Label(t, text=detail, bg=C["card"], fg=C["muted"], font=self.f_small, anchor="w",
                     justify="left", wraplength=560).pack(fill="x")
        return ic

    SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _spin(self, label, n=0):
        if self.s["info"] != "busy" or not label.winfo_exists():
            return
        label.configure(text=self.SPIN[n % len(self.SPIN)], fg=C["accent"])
        self.after(80, self._spin, label, n + 1)

    def _render_probe(self):
        for c in self.res.winfo_children():
            c.destroy()
        tk.Frame(self.res, height=8, bg=C["card"]).pack()
        i = self.s["info"]
        if i is None:
            self.row(self.res, "wait", "Not checked yet", "Press “Check device”.")
        elif i == "busy":
            ic = self.row(self.res, "wait", "Checking…", "The first run downloads adb (about 10 MB).")
            self._spin(ic)
        elif i["error"] and not i["device"]:
            self.row(self.res, "bad", "No device found", i["error"])
        else:
            self.row(self.res, "ok", "Device connected", "%s (%s)" % (i["model"] or "?", i["device"]))
            self.row(self.res, "ok" if i["root"] else "bad", "Root access",
                     "" if i["root"] else "Open Magisk > Superuser and allow “Shell”, then check again.")
            if i["locked"] is True:
                self.row(self.res, "bad", "Bootloader is locked", "Unlock it first, then check again.")
            else:
                self.row(self.res, "ok", "Bootloader unlocked")
            if i["ab"]:
                self.row(self.res, "ok", "A/B slots",
                         "Running from slot %s; the UEFI goes in the other slot." % i["slot"][1:].upper())
            else:
                self.row(self.res, "warn", "No A/B slots", "The UEFI would replace your only boot image.")
            if i.get("wimlib"):
                self.row(self.res, "bad", "wimlib is missing", "Install it with:  brew install wimlib")
            if i["error"]:
                self.row(self.res, "warn", "Note", i["error"])
        tk.Frame(self.res, height=8, bg=C["card"]).pack()

    def _probe(self):
        self.s["info"] = "busy"
        self._render_probe()
        self.chk_btn.set_enabled(False)
        self.next_btn.set_enabled(False)
        self.back_btn.set_enabled(False)
        threading.Thread(target=lambda: self.q.put(("probed", w.probe())), daemon=True).start()

    def p_files(self):
        self.title_("Choose your files", "Pick the Windows image, and the UEFI zip for your device.")
        iso = card(self.body)
        iso.pack(fill="x")
        tk.Label(iso, text="Windows 11 ARM64 ISO", bg=C["card"], fg=C["text"], font=self.f_h,
                 anchor="w").pack(fill="x", padx=20, pady=(14, 2))
        self.iso_lbl = tk.Label(iso, bg=C["card"], fg=C["muted"], anchor="w", wraplength=520,
                                justify="left")
        self.iso_lbl.pack(fill="x", padx=20)
        Btn(iso, "Choose ISO…", self._pick_iso, "secondary").pack(anchor="w", padx=20, pady=12)
        tk.Frame(self.body, height=14, bg=C["bg"]).pack()
        uefi = card(self.body)
        uefi.pack(fill="x")
        tk.Label(uefi, text="UEFI installer zip", bg=C["card"], fg=C["text"], font=self.f_h,
                 anchor="w").pack(fill="x", padx=20, pady=(14, 2))
        self.uefi_lbl = tk.Label(uefi, bg=C["card"], fg=C["muted"], anchor="w", wraplength=520,
                                 justify="left")
        self.uefi_lbl.pack(fill="x", padx=20)
        bar = tk.Frame(uefi, bg=C["card"])
        bar.pack(anchor="w", padx=20, pady=12)
        Btn(bar, "Choose zip…", self._pick_uefi, "secondary").pack(side="left")
        if self._auto_ok():
            Btn(bar, "Use latest release", self._auto_uefi, "secondary").pack(side="left", padx=8)
        self._files_refresh()
        self.nav(back=lambda: self.show(1), nxt=lambda: self.show(3), nxt_ok=self._files_ok())

    def _auto_ok(self):
        i = self.s["info"]
        return isinstance(i, dict) and i["device"] in w.PROFILES

    def _files_ok(self):
        iso = self.s["iso"]
        return bool(iso and Path(iso).is_file() and (self.s["uefi"] or self._auto_ok())
                    and not self._zip_problem())

    def _zip_problem(self):
        z, i = self.s["uefi"], self.s["info"]
        if not z:
            return None
        try:
            with zipfile.ZipFile(z) as zf:
                names = zf.namelist()
        except (OSError, zipfile.BadZipFile):
            return "That file isn't a valid zip."
        fds = [n for n in names if n.endswith("_UEFI.fd")]
        if not fds or "BootShim.Dualboot.bin" not in names:
            return "This zip isn't a UEFI installer (no *_UEFI.fd / BootShim)."
        targets = {n.split("_")[0] for n in fds}
        if isinstance(i, dict) and i["device"] not in targets and i["model"] not in targets:
            return "This zip is for %s, but your device is %s." % (", ".join(sorted(targets)), i["device"])
        return None

    def _files_refresh(self):
        iso = self.s["iso"]
        ok = iso and Path(iso).is_file()
        self.iso_lbl.configure(text="%s  (%s)" % (Path(iso).name, w.human(Path(iso).stat().st_size))
                               if ok else "No file chosen")
        z = self.s["uefi"]
        if z:
            bad = self._zip_problem()
            self.uefi_lbl.configure(text=Path(z).name + ("\n✕ " + bad if bad else "\n✓ Compatible"),
                                    fg=C["err"] if bad else C["ok"])
        elif self._auto_ok():
            self.uefi_lbl.configure(text="The latest release is downloaded automatically.", fg=C["muted"])
        else:
            self.uefi_lbl.configure(text="Required for this device. Choose the zip you were given.",
                                    fg=C["muted"])

    def _pick_iso(self):
        f = filedialog.askopenfilename(title="Windows 11 ARM64 ISO",
                                       filetypes=[("ISO image", "*.iso"), ("All files", "*")])
        if f:
            self.s["iso"] = f
            self.show(2)

    def _pick_uefi(self):
        f = filedialog.askopenfilename(title="UEFI installer zip", filetypes=[("Zip", "*.zip")])
        if f:
            self.s["uefi"] = f
            self.show(2)

    def _auto_uefi(self):
        self.s["uefi"] = ""
        self.show(2)

    def p_storage(self):
        parts = self.s["info"].get("parts")
        self.title_("Storage", "Windows needs two partitions on the device: a small boot (ESP) "
                    "partition and a large one for Windows itself.")
        c = card(self.body)
        c.pack(fill="x")
        tk.Frame(c, height=8, bg=C["card"]).pack()
        if parts:
            self.s["create"] = False
            self.row(c, "ok", "Partitions found",
                     "ESP %s   ·   Windows %s" % (w.human(parts["ESP"][1]), w.human(parts["Windows"][1])))
            tk.Frame(c, height=6, bg=C["card"]).pack()
            self.callout(self.body, "Both partitions will be formatted. Anything on them is erased.",
                         "warn").pack(fill="x", pady=16)
            ok = True
        else:
            self.s["create"] = True
            self.row(c, "warn", "No Windows partitions yet", "The installer can create them for you.")
            form = tk.Frame(c, bg=C["card"])
            form.pack(fill="x", padx=20, pady=(4, 12))
            self.esp_v, self.win_v = tk.StringVar(value=self.s["esp"]), tk.StringVar(value=self.s["win"])
            for r, (lab, var) in enumerate((("ESP size", self.esp_v), ("Windows size", self.win_v))):
                tk.Label(form, text=lab, bg=C["card"], fg=C["text"], width=14, anchor="w"
                         ).grid(row=r, column=0, pady=4)
                tk.Entry(form, textvariable=var, width=10, relief="flat", bg=C["sec"],
                         fg=C["text"], insertbackground=C["text"], highlightthickness=1,
                         highlightbackground=C["border"]).grid(row=r, column=1, sticky="w")
            self.callout(self.body, "If there is no free space left, userdata is shrunk, which ERASES "
                         "ALL DATA on the device. After that the device restarts, you set Android "
                         "up again, and run this guide a second time to finish.",
                         "err").pack(fill="x", pady=16)
            self.ack = tk.BooleanVar(value=self.s["ack"])
            Check(self.body, "I understand and have backed up my data", self.ack, bg=C["bg"],
                  command=lambda: self.next_btn.set_enabled(self.ack.get())).pack(anchor="w")
            ok = self.s["ack"]
        self.nav(back=lambda: self.show(2), nxt=self._storage_next, nxt_ok=ok)

    def _storage_next(self):
        if self.s["create"]:
            self.s.update(esp=self.esp_v.get().strip() or "512M", win=self.win_v.get().strip() or "64G",
                          ack=self.ack.get())
        self.show(4)

    def p_review(self):
        self.title_("Ready to go", "Here is what will happen.")
        i = self.s["info"]
        c = card(self.body)
        c.pack(fill="x")
        items = ["Back up the boot image of your %s" % (i["model"] or i["device"])]
        if self.s["create"]:
            items += ["Create the ESP and Windows partitions",
                      "Restart the device, then you run this guide again to install Windows"]
        else:
            items += ["Format the ESP and Windows partitions",
                      "Copy Windows from your ISO to the device",
                      "Install the UEFI boot loader in the other slot",
                      "Restart the device into Windows"]
        tk.Frame(c, height=8, bg=C["card"]).pack()
        for n, t in enumerate(items, 1):
            r = tk.Frame(c, bg=C["card"])
            r.pack(fill="x", padx=20, pady=4)
            tk.Label(r, text=str(n), bg=C["card"], fg=C["accent"], width=3, anchor="w",
                     font=self.f_bold).pack(side="left")
            tk.Label(r, text=t, bg=C["card"], fg=C["text"], anchor="w").pack(side="left")
        tk.Frame(c, height=8, bg=C["card"]).pack()
        tk.Frame(self.body, height=14, bg=C["bg"]).pack()
        self.dry = tk.BooleanVar(value=self.s["dry"])
        Check(self.body, "Rehearse first (recommended): reads the device but changes nothing",
              self.dry, bg=C["bg"], command=self._review_btn).pack(anchor="w")
        self.nonab = tk.BooleanVar(value=self.s["nonab"])
        if not i["ab"]:
            Check(self.body, "Allow overwriting the only boot image (this device has no A/B slots)",
                  self.nonab, bg=C["bg"], command=self._review_btn).pack(anchor="w", pady=6)
        self.noreb = tk.BooleanVar(value=self.s["noreboot"])
        Check(self.body, "Don't restart the device when finished", self.noreb, bg=C["bg"]
              ).pack(anchor="w", pady=6)
        self.nav(back=lambda: self.show(3), nxt=self._start, nxt_text="Start rehearsal")
        self._review_btn()

    def _review_btn(self):
        self.next_btn.configure(text="Start rehearsal" if self.dry.get() else "Install now")
        self.next_btn.set_enabled(self.dry.get() or self.nonab.get() or self.s["info"]["ab"])

    # ---- install
    def p_install(self):
        self.title_("Rehearsing…" if self.s["dry"] else "Installing…",
                    "Keep the device connected. You may be asked to confirm a step.")
        c = card(self.body)
        c.pack(fill="x")
        tk.Frame(c, height=8, bg=C["card"]).pack()
        self.job_rows = []
        for name in JOBS:
            f = tk.Frame(c, bg=C["card"])
            f.pack(fill="x", padx=20, pady=5)
            ic = tk.Label(f, text="○", bg=C["card"], fg=C["muted"], width=2, font=self.f_h)
            ic.pack(side="left")
            lb = tk.Label(f, text=name, bg=C["card"], fg=C["muted"], anchor="w")
            lb.pack(side="left")
            self.job_rows.append((ic, lb))
        tk.Frame(c, height=8, bg=C["card"]).pack()
        self.status = tk.Label(self.body, text="", bg=C["bg"], fg=C["muted"], anchor="w",
                               font=self.f_small)
        self.status.pack(fill="x", pady=(10, 0))
        self._details()
        self.s["job"] = -1
        threading.Thread(target=self._worker, args=(self._argv(),), daemon=True).start()

    def _details(self):
        self.log_btn = Btn(self.body, "Show details", self._toggle_log, "secondary")
        self.log_btn.pack(anchor="w", pady=10)
        self.logbox = tk.Text(self.body, height=9, bg=C["log"], fg=C["log_fg"], font=self.f_mono,
                              relief="flat", wrap="word", padx=10, pady=8, state="disabled")
        self._log(self.s["log"], store=False)

    def _toggle_log(self):
        if self.logbox.winfo_ismapped():
            self.logbox.pack_forget()
            self.log_btn.configure(text="Show details")
        else:
            self.logbox.pack(fill="both", expand=True)
            self.log_btn.configure(text="Hide details")

    def _log(self, text, store=True):
        if store:
            self.s["log"] += text
        lb = self.logbox
        if lb is None:
            return
        lb.configure(state="normal")
        for k, part in enumerate(text.replace("\r\n", "\n").split("\r")):
            if k > 0:
                lb.delete("end-1c linestart", "end-1c")
            lb.insert("end", part)
        lb.see("end")
        lb.configure(state="disabled")

    def _set_job(self, n):
        for k, (ic, lb) in enumerate(self.job_rows):
            if k < n:
                ic.configure(text="✓", fg=C["ok"])
                lb.configure(fg=C["text"])
            elif k == n:
                ic.configure(text="●", fg=C["accent"])
                lb.configure(fg=C["text"])
            else:
                ic.configure(text="○", fg=C["muted"])
                lb.configure(fg=C["muted"])

    def _argv(self):
        s, a = self.s, []
        if s["iso"]:
            a += ["--iso", s["iso"]]
        if s["uefi"]:
            a += ["--uefi-zip", s["uefi"]]
        if s["dry"]:
            a.append("--dry-run")
        if s["create"]:
            a += ["--create-partitions", "--esp-size", s["esp"], "--win-size", s["win"]]
        if s["nonab"]:
            a.append("--allow-non-ab")
        if s["noreboot"]:
            a.append("--no-reboot")
        return a

    def _start(self):
        self.s.update(dry=self.dry.get(), nonab=self.nonab.get(), noreboot=self.noreb.get(),
                      log="", result=None, rerun=False)
        self.show(5)

    def _prompt_from_thread(self, msg):
        box = {"event": threading.Event(), "answer": ""}
        self.q.put(("prompt", (msg, box)))
        box["event"].wait()
        return box["answer"]

    def _worker(self, argv):
        w.PROMPT = self._prompt_from_thread
        res = ("ok", "")
        try:
            with contextlib.redirect_stdout(LogWriter(self.q)):
                w.main(argv)
        except w.Fail as e:
            res = ("fail", str(e))
        except SystemExit:
            res = ("fail", "Invalid options.")
        except Exception as e:      # noqa: BLE001 - surface anything unexpected
            res = ("fail", "Unexpected error: %r" % e)
        self.q.put(("done", res))

    def _pump(self):
        try:
            while True:
                kind, p = self.q.get_nowait()
                if kind == "log":
                    self._log(p)
                    for m in re.finditer(r"\[(\d)b?/6\]", p):
                        self.s["job"] = int(m.group(1)) - 1
                        if self.page == 5:
                            self._set_job(self.s["job"])
                    lines = [x for x in p.replace("\r", "\n").split("\n") if x.strip()]
                    if lines and self.page == 5:
                        self.status.configure(text=lines[-1].strip()[:110])
                    if "installer again" in p:
                        self.s["rerun"] = True
                elif kind == "prompt":
                    msg, box = p
                    box["answer"] = PromptDialog(self, msg).result
                    box["event"].set()
                elif kind == "probed":
                    self.s["info"] = p
                    if self.page == 1:
                        self.show(1)
                elif kind == "done":
                    self.s["result"] = p
                    self.show(6)
        except queue.Empty:
            pass
        self.after(100, self._pump)

    # ---- done
    def p_done(self):
        kind, msg = self.s["result"]
        if kind == "ok" and self.s["rerun"]:
            self.title_("Partitions created", "The device is restarting.")
            self.callout(self.body, "If data was erased: finish Android setup, turn USB debugging back "
                         "on, make sure Magisk root works, then run this guide again.", "ok"
                         ).pack(fill="x")
            self.nav(nxt=self.destroy, nxt_text="Close")
        elif kind == "ok" and self.s["dry"]:
            self.title_("Rehearsal finished", "Nothing on the device was changed.")
            self.callout(self.body, "Everything checked out. When you're ready, run it for real.",
                         "ok").pack(fill="x")
            self.nav(back=lambda: self.show(4), nxt=self._run_real, nxt_text="Install now")
        elif kind == "ok":
            self.title_("All done", "Windows is installed.")
            self.callout(self.body, "The device is restarting into Windows. The first boot runs Windows "
                         "setup and can take several minutes. To return to Android, open the Boot "
                         "Manager and pick “Reboot to other slot”.", "ok").pack(fill="x")
            self.nav(nxt=self.destroy, nxt_text="Close")
        else:
            self.title_("Something went wrong", "Nothing further was changed after this point.")
            self.callout(self.body, msg or "Unknown error.", "err").pack(fill="x")
            tk.Label(self.body, text="Your boot image backup is in ~/.woa-installer/backup.",
                     bg=C["bg"], fg=C["muted"], anchor="w").pack(fill="x", pady=10)
            self.nav(back=lambda: self.show(4), nxt=self.destroy, nxt_text="Close")
        self._details()

    def _run_real(self):
        self.s.update(dry=False, log="", result=None, rerun=False)
        self.show(5)


class LogWriter:
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))
        return len(text)

    def flush(self):
        pass


class PromptDialog(tk.Toplevel):
    def __init__(self, parent, msg):
        super().__init__(parent)
        self.result = ""
        self.configure(bg=C["bg"])
        self.title("Confirm")
        self.transient(parent)
        m = msg.strip()
        self.yn = m.endswith("[y/N]")
        text = m[:-5].strip() if self.yn else m
        body = tk.Frame(self, bg=C["bg"], padx=26, pady=22)
        body.pack()
        tk.Label(body, text="Please confirm", bg=C["bg"], fg=C["text"],
                 font=("TkDefaultFont", 15, "bold"), anchor="w").pack(fill="x")
        tk.Label(body, text=text.rstrip(": "), bg=C["bg"], fg=C["text"], justify="left",
                 wraplength=420, anchor="w").pack(fill="x", pady=12)
        self.entry = None
        if not self.yn:
            self.entry = tk.Entry(body, relief="flat", bg=C["sec"], fg=C["text"],
                                  insertbackground=C["text"], highlightthickness=1,
                                  highlightbackground=C["border"])
            self.entry.pack(fill="x", ipady=6)
            self.entry.focus_set()
        bar = tk.Frame(body, bg=C["bg"])
        bar.pack(fill="x", pady=(16, 0))
        Btn(bar, "Cancel", lambda: self._done(False), "secondary").pack(side="right", padx=(8, 0))
        Btn(bar, "Yes" if self.yn else "Confirm", lambda: self._done(True), "primary").pack(side="right")
        self.update_idletasks()
        self.geometry("+%d+%d" % (parent.winfo_rootx() + 220, parent.winfo_rooty() + 180))
        self.grab_set()
        self.wait_window()

    def _done(self, ok):
        if self.yn:
            self.result = "y" if ok else "n"
        else:
            self.result = self.entry.get() if ok else ""
        self.destroy()


if __name__ == "__main__":
    Wizard().mainloop()
