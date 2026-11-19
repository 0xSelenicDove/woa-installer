#!/usr/bin/env python3
"""Guided Tk wizard for woa_installer (standard library only).

Design notes: one decision per screen, a visible step list, plain-language copy,
a read-only "pre-check" step before anything is changed, destructive confirmations spelled out, and a
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
from tkinter import filedialog, ttk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import woa_data as data  # noqa: E402
import woa_installer as w  # noqa: E402

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
STEPS = ["Welcome", "Device", "Files", "Storage", "Setup", "Review", "Pre-check", "Install", "Done"]
REVIEW, PRECHECK, INSTALL, DONE = 5, 6, 7, 8
PX = dict(base=16, small=15, h=19, title=32, box=20, mono=15)   # font pixel sizes
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
        self.box = tk.Label(self, bg=bg, fg=C["accent"], font=("TkDefaultFont", -PX["box"]))
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
        self.geometry("980x780")
        self.minsize(900, 640)
        self.configure(bg=C["bg"])
        # Sizes are in PIXELS (negative = pixels in Tk). Point sizes are converted with the Tk
        # version's own scaling (Tk 9 = 1.33, Tk 8.x = 1.0), which made packaged builds render ~25%
        # smaller than a source run. Pixel sizes look the same everywhere.
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(size=-PX["base"])
            except tk.TclError:
                pass
        try:
            tkfont.nametofont("TkFixedFont").configure(size=-PX["mono"])
        except tk.TclError:
            pass
        base = tkfont.nametofont("TkDefaultFont")
        fam = base.cget("family")
        self.f_title = tkfont.Font(family=fam, size=-PX["title"], weight="bold")
        self.f_h = tkfont.Font(family=fam, size=-PX["h"], weight="bold")
        self.f_small = tkfont.Font(family=fam, size=-PX["small"])
        self.f_mono = tkfont.Font(family="Menlo" if sys.platform == "darwin" else "Consolas", size=-PX["mono"])
        self.f_bold = tkfont.Font(family=fam, size=-PX["base"], weight="bold")
        self.f_base = base
        self.q = queue.Queue()
        self.s = dict(info=None, iso="", uefi="", create=False, esp="512M", win="64G",
                      dry=False, pc=None, nonab=False, noreboot=False, ack=False, oobe=True, user="user", pw="",
                      host="WOA-DEVICE", lang="", locale="", tz="", drivers="", iso_info=None, edition=None,
                      repo="", repo_state=None, sha="", bcd="",
                      job=-1, log="", result=None, rerun=False)
        self.page = 0
        self.logbox = self.job_rows = self.status = None
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
        self.canvas = tk.Canvas(right, bg=C["bg"], highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=(36, 20), pady=(30, 0))
        self.body = tk.Frame(self.canvas, bg=C["bg"])
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self._fit())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self._win, width=e.width), self._fit()))
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_all(ev, self._wheel)

    def _fit(self):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0)

    def _wheel(self, e):
        if self.body.winfo_reqheight() <= self.canvas.winfo_height():
            return
        if isinstance(e.widget, (tk.Text, ttk.Combobox)):      # let those handle their own scrolling
            return
        step = -1 if (e.num == 4 or e.delta > 0) else 1
        if sys.platform == "darwin" and e.delta:
            step = -int(e.delta)
        self.canvas.yview_scroll(step, "units")

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
        self.logbox = self.job_rows = self.status = None
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
        [self.p_welcome, self.p_phone, self.p_files, self.p_storage, self.p_setup, self.p_review,
         self.p_precheck, self.p_install, self.p_done][n]()

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
                  "A UEFI installer zip for your device, or a GitHub repository that publishes one"):
            tk.Label(c, text="✓  " + t, bg=C["card"], fg=C["text"], anchor="w").pack(
                fill="x", padx=20, pady=3)
        tk.Frame(c, height=12, bg=C["card"]).pack()
        self.callout(self.body, "Your boot image is backed up first, and a pre-check verifies everything "
                     "before anything is changed. Creating new partitions can still erase "
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
        self.iso_stat = tk.Label(iso, bg=C["card"], fg=C["muted"], anchor="w", wraplength=560, justify="left")
        self.iso_stat.pack(fill="x", padx=20, pady=(4, 0))
        Btn(iso, "Choose ISO…", self._pick_iso, "secondary").pack(anchor="w", padx=20, pady=(12, 8))
        if isinstance(self.s["iso_info"], dict):
            arm = self.s["iso_info"]["arm_images"]
            one = len(arm) == 1
            row = tk.Frame(iso, bg=C["card"])
            row.pack(fill="x", padx=20, pady=(0, 14))
            tk.Label(row, text="Edition" + ("  ·  only one in this image" if one else ""), bg=C["card"],
                     fg=C["text"], anchor="w").pack(anchor="w")
            self._ed_map = {"%s (build %s)" % (i["name"], i["build"]): i["index"] for i in arm}
            cur = [k for k, v in self._ed_map.items() if v == self.s["edition"]]
            self.ed_v = tk.StringVar(value=cur[0] if cur else "")
            cb = ttk.Combobox(row, textvariable=self.ed_v, values=list(self._ed_map), width=46,
                              state="disabled" if one else "readonly")
            cb.pack(anchor="w", pady=(4, 0))
            cb.bind("<<ComboboxSelected>>", self._edition_changed)
        else:
            tk.Frame(iso, height=4, bg=C["card"]).pack()
        tk.Frame(self.body, height=14, bg=C["bg"]).pack()
        uefi = card(self.body)
        uefi.pack(fill="x")
        tk.Label(uefi, text="UEFI installer zip", bg=C["card"], fg=C["text"], font=self.f_h,
                 anchor="w").pack(fill="x", padx=20, pady=(14, 2))
        self.uefi_lbl = tk.Label(uefi, bg=C["card"], fg=C["muted"], anchor="w", wraplength=520,
                                 justify="left")
        self.uefi_lbl.pack(fill="x", padx=20)
        self.uefi_note = tk.Label(uefi, bg=C["card"], fg=C["muted"], anchor="w", wraplength=560,
                                  justify="left", font=self.f_small)
        self.uefi_note.pack(fill="x", padx=20, pady=(2, 0))
        bar = tk.Frame(uefi, bg=C["card"])
        bar.pack(anchor="w", padx=20, pady=12)
        Btn(bar, "Choose zip…", self._pick_uefi, "secondary").pack(side="left")
        tk.Label(uefi, text="…or download from a GitHub repository", bg=C["card"], fg=C["text"],
                 anchor="w").pack(fill="x", padx=20)
        rrow = tk.Frame(uefi, bg=C["card"])
        rrow.pack(fill="x", padx=20, pady=(4, 14))
        self.repo_v = tk.StringVar(value=self.s["repo"])
        ent = tk.Entry(rrow, textvariable=self.repo_v, width=40, relief="flat", bg=C["sec"], fg=C["text"],
                       insertbackground=C["text"], highlightthickness=1, highlightbackground=C["border"])
        ent.pack(side="left", ipady=7)
        ent.bind("<Return>", lambda e: self._use_repo())
        Btn(rrow, "Use repo", self._use_repo, "secondary").pack(side="left", padx=8)
        tk.Label(uefi, text="owner/name or https://github.com/owner/name", bg=C["card"], fg=C["muted"],
                 font=self.f_small, anchor="w").pack(fill="x", padx=20, pady=(0, 12))
        tk.Label(uefi, text="Expected SHA-256 (optional, verifies the zip)", bg=C["card"], fg=C["text"],
                 anchor="w").pack(fill="x", padx=20)
        self.sha_v = tk.StringVar(value=self.s["sha"])
        sent = tk.Entry(uefi, textvariable=self.sha_v, width=64, relief="flat", bg=C["sec"], fg=C["text"],
                        insertbackground=C["text"], highlightthickness=1, highlightbackground=C["border"])
        sent.pack(anchor="w", padx=20, pady=(4, 14), ipady=7)
        sent.bind("<KeyRelease>", self._sha_changed)
        self._files_refresh()
        self.nav(back=lambda: self.show(1), nxt=lambda: self.show(3), nxt_ok=self._files_ok())

    def _repo_ok(self):
        st = self.s["repo_state"]
        return bool(self.s["repo"]) and isinstance(st, dict)

    def _img(self):
        """The selected edition's image record (name, languages, ...) or None."""
        info = self.s["iso_info"]
        if not isinstance(info, dict):
            return None
        for i in info["arm_images"]:
            if i["index"] == self.s["edition"]:
                return i
        return info["image"]

    @staticmethod
    def _img_langs(img):
        return img["langs"] or ([img["default_lang"]] if img["default_lang"] else ["en-US"])

    _sha_cache = {}

    def _expected_sha(self):
        """(hash, source) from the entry, or a <zip>.sha256 sidecar; (None, '') if none."""
        h = self.s["sha"].strip().lower()
        if h:
            return h, "the value you entered"
        z = self.s["uefi"]
        for side in ((z + ".sha256"), (z + ".sha256sum")) if z else ():
            if Path(side).is_file():
                got = w.parse_checksum(Path(side).read_text(errors="replace"), Path(z).name)
                if got:
                    return got, Path(side).name
        return None, ""

    def _sha_problem(self):
        h = self.s["sha"].strip()
        if h and not re.fullmatch(r"[0-9a-fA-F]{64}", h):
            return "The SHA-256 must be 64 hexadecimal characters."
        z = self.s["uefi"]
        exp, src = self._expected_sha()
        if z and exp:
            if z not in self._sha_cache:
                self._sha_cache[z] = w.sha256_file(z)
            if self._sha_cache[z] != exp:
                return "Checksum mismatch (%s): the zip is corrupt or has been altered." % src
        return None

    def _sha_changed(self, _=None):
        self.s["sha"] = self.sha_v.get()
        self._files_refresh()
        self.next_btn.set_enabled(self._files_ok())

    def _files_ok(self):
        iso = self.s["iso"]
        return bool(iso and Path(iso).is_file() and isinstance(self.s["iso_info"], dict)
                    and (self.s["uefi"] or self._repo_ok()) and not self._zip_problem() and not self._sha_problem())

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
        info = self.s["iso_info"]
        if not ok:
            self.iso_lbl.configure(text="No file chosen", fg=C["muted"])
        else:
            self.iso_lbl.configure(text="%s  (%s)" % (Path(iso).name, w.human(Path(iso).stat().st_size)),
                                   fg=C["muted"])
            if info == "busy":
                self.iso_stat.configure(text="Reading the image…", fg=C["muted"])
            elif isinstance(info, dict):
                langs = ", ".join(data.locale_label(t) for t in self._img_langs(self._img()))
                self.iso_stat.configure(text="✓ ARM64 · " + langs, fg=C["ok"])
            elif isinstance(info, tuple):
                self.iso_stat.configure(text="✕ " + info[1], fg=C["err"])
        z = self.s["uefi"]
        if z:
            bad = self._zip_problem()
            self.uefi_lbl.configure(text=Path(z).name + ("\n✕ " + bad if bad else "\n✓ Compatible"),
                                    fg=C["err"] if bad else C["ok"])
        elif self.s["repo"] and self.s["repo_state"] == "busy":
            self.uefi_lbl.configure(text="Checking %s…" % self.s["repo"], fg=C["muted"])
        elif self.s["repo"] and isinstance(self.s["repo_state"], dict):
            st = self.s["repo_state"]
            self.uefi_lbl.configure(text="✓ %s · latest release %s · %s" % (self.s["repo"], (st["tag"][:24] + "…") if len(st["tag"]) > 25 else st["tag"], st["name"]),
                                    fg=C["ok"])
        elif self.s["repo"] and isinstance(self.s["repo_state"], tuple):
            self.uefi_lbl.configure(text="✕ " + self.s["repo_state"][1], fg=C["err"])
        else:
            self.uefi_lbl.configure(text="Required for this device. Choose the zip you were given, "
                                    "or download it from a GitHub repository.", fg=C["muted"])
        self._uefi_note()

    def _uefi_note(self):
        bad = self._sha_problem()
        exp, src = self._expected_sha()
        z, st = self.s["uefi"], self.s["repo_state"]
        if bad:
            self.uefi_note.configure(text="✕ " + bad, fg=C["err"])
        elif z and exp:
            self.uefi_note.configure(text="✓ SHA-256 verified (%s)" % src, fg=C["ok"])
        elif z:
            self._sha_cache.setdefault(z, w.sha256_file(z))
            self.uefi_note.configure(text="! Not verified. SHA-256 %s… — paste the publisher's hash below "
                                     "to check it." % self._sha_cache[z][:16], fg=C["warn"])
        elif self.s["repo"] and isinstance(st, dict):
            if exp:
                self.uefi_note.configure(text="The download will be verified against the hash you entered.",
                                         fg=C["ok"])
            elif st.get("sum"):
                self.uefi_note.configure(text="✓ The release publishes a checksum; the download will be verified.",
                                         fg=C["ok"])
            else:
                self.uefi_note.configure(text="! This release publishes no checksum, so the download can't be "
                                         "verified unless you enter one below.", fg=C["warn"])
        else:
            self.uefi_note.configure(text="")

    def _pick_iso(self):
        f = filedialog.askopenfilename(title="Windows 11 ARM64 ISO",
                                       filetypes=[("ISO image", "*.iso"), ("All files", "*")])
        if f:
            self.s.update(iso=f, iso_info="busy", lang="", locale="")
            self.show(2)

            def work():
                try:
                    res = w.read_iso_info(f)
                except w.Fail as e:
                    res = ("err", str(e))
                except Exception as e:      # noqa: BLE001 - show anything unexpected
                    res = ("err", "Could not read the image: %r" % e)
                self.q.put(("iso", (f, res)))
            threading.Thread(target=work, daemon=True).start()

    def _edition_changed(self, _=None):
        idx = self._ed_map.get(self.ed_v.get())
        if idx and idx != self.s["edition"]:
            self.s.update(edition=idx, lang="", locale="")     # languages differ per edition
            self.show(2)

    def _pick_uefi(self):
        f = filedialog.askopenfilename(title="UEFI installer zip", filetypes=[("Zip", "*.zip")])
        if f:
            self.s.update(uefi=f, repo="", repo_state=None)
            self.show(2)

    def _use_repo(self):
        try:
            repo = w.parse_repo(self.repo_v.get())
        except w.Fail as e:
            self.s.update(repo=self.repo_v.get().strip(), repo_state=("err", str(e)), uefi="")
            self.show(2)
            return
        self.s.update(repo=repo, repo_state="busy", uefi="")
        self.show(2)

        def work():
            try:
                tag, asset, summ = w.find_uefi_release(repo)
                res = {"tag": tag, "name": asset["name"], "sum": bool(summ)}
            except w.Fail as e:
                res = ("err", str(e))
            self.q.put(("repo", (repo, res)))
        threading.Thread(target=work, daemon=True).start()

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

    def p_setup(self):
        if self.s["create"]:            # partitions only this round; Windows setup comes next run
            self.show(5)
            return
        self.title_("Windows setup", "Skip Windows' first-run screens so it starts straight at the desktop. "
                    "No network, WiFi driver or Microsoft account needed.")
        self.oobe_v = tk.BooleanVar(value=self.s["oobe"])
        Check(self.body, "Skip the setup screens (recommended)", self.oobe_v, bg=C["bg"],
              command=self._setup_toggle).pack(anchor="w")
        brow = tk.Frame(self.body, bg=C["bg"])
        brow.pack(fill="x", pady=(12, 0))
        tk.Label(brow, text="Boot configuration (BCD)", bg=C["bg"], fg=C["text"], anchor="w").pack(anchor="w")
        tk.Label(brow, text="Optional. Only needed if the device's boot partition has none to reuse.",
                 bg=C["bg"], fg=C["muted"], font=self.f_small, anchor="w").pack(anchor="w")
        bbar = tk.Frame(brow, bg=C["bg"])
        bbar.pack(anchor="w", pady=(6, 0))
        Btn(bbar, "Choose…", self._pick_bcd, "secondary").pack(side="left")
        self.bcd_lbl = tk.Label(bbar, text=Path(self.s["bcd"]).name if self.s["bcd"] else "None",
                                bg=C["bg"], fg=C["muted"], font=self.f_small)
        self.bcd_lbl.pack(side="left", padx=10)
        self.setup_card = card(self.body)
        form = tk.Frame(self.setup_card, bg=C["card"])
        form.pack(fill="x", padx=20, pady=14)
        img = self._img()
        langs = self._img_langs(img) if img else ["en-US"]
        if not self.s["lang"]:
            self.s["lang"] = (img["default_lang"] or langs[0]) if img else "en-US"
        if not self.s["locale"]:
            self.s["locale"] = self.s["lang"] if self.s["lang"] in data.LOCALES else "en-US"
        if not self.s["tz"]:
            self.s["tz"] = data.default_timezone()
        self.sv = {k: tk.StringVar(value=self.s[k]) for k in ("user", "pw", "host")}
        self.lang_v = tk.StringVar(value=data.locale_label(self.s["lang"]))
        self.loc_v = tk.StringVar(value=data.locale_label(self.s["locale"]))
        self.tz_v = tk.StringVar(value=self.s["tz"])
        self._lang_map = {data.locale_label(t): t for t in langs}
        self._loc_map = {data.locale_label(t): t for t in sorted(data.LOCALES, key=lambda x: data.LOCALES[x])}
        if self.loc_v.get() not in self._loc_map:       # unusual tag: still selectable
            self._loc_map[self.loc_v.get()] = self.s["locale"]
        for r, (lab, key, show) in enumerate((("Account name", "user", None), ("Password (optional)", "pw", "•"),
                                              ("Device name", "host", None))):
            tk.Label(form, text=lab, bg=C["card"], fg=C["text"], width=20, anchor="w").grid(row=r, column=0, pady=4)
            tk.Entry(form, textvariable=self.sv[key], width=29, show=show or "", relief="flat", bg=C["sec"],
                     fg=C["text"], insertbackground=C["text"], highlightthickness=1,
                     highlightbackground=C["border"]).grid(row=r, column=1, sticky="w")
        one = len(langs) == 1
        for r, (lab, var, values, state, note) in enumerate((
                ("Language", self.lang_v, list(self._lang_map), "disabled" if one else "readonly",
                 "Only one in this image" if one else ""),
                ("Region & format", self.loc_v, list(self._loc_map), "readonly", "Dates & keyboard"),
                ("Time zone", self.tz_v, data.TIMEZONES, "readonly", "")), 3):
            left = tk.Frame(form, bg=C["card"])
            left.grid(row=r, column=0, pady=4, sticky="w")
            tk.Label(left, text=lab, bg=C["card"], fg=C["text"], width=20, anchor="w").pack(anchor="w")
            if note:
                tk.Label(left, text=note, bg=C["card"], fg=C["muted"], font=self.f_small,
                         anchor="w").pack(anchor="w")
            ttk.Combobox(form, textvariable=var, values=values, state=state, width=30, height=14
                         ).grid(row=r, column=1, sticky="w")
        tk.Label(form, text="Drivers folder", bg=C["card"], fg=C["text"], width=20, anchor="w").grid(row=6, column=0, pady=4)
        drow = tk.Frame(form, bg=C["card"])
        drow.grid(row=6, column=1, sticky="w")
        Btn(drow, "Choose…", self._pick_drivers, "secondary").pack(side="left")
        self.drv_lbl = tk.Label(drow, text=self.s["drivers"] or "None (optional)", bg=C["card"],
                                fg=C["muted"], anchor="w", font=self.f_small)
        self.drv_lbl.pack(side="left", padx=10)
        tk.Label(self.setup_card, text="The password is stored in plain text in the install files "
                 "(Windows requires it). Use a throwaway one.", bg=C["card"], fg=C["muted"], font=self.f_small,
                 anchor="w", wraplength=600, justify="left").pack(fill="x", padx=20, pady=(0, 12))
        self.setup_err = tk.Label(self.body, text="", bg=C["bg"], fg=C["err"], anchor="w", wraplength=620,
                                  justify="left")
        self.nav(back=lambda: self.show(3), nxt=self._setup_next)
        self.setup_err.pack(fill="x", pady=6, side="bottom")
        self._setup_toggle()

    def _setup_toggle(self):
        if self.oobe_v.get():
            self.setup_card.pack(fill="x", pady=14)
        else:
            self.setup_card.pack_forget()

    def _pick_bcd(self):
        f = filedialog.askopenfilename(title="BCD file (registry hive)")
        if not f:
            return
        try:
            with open(f, "rb") as fh:
                ok = fh.read(4) == b"regf"
        except OSError:
            ok = False
        if not ok:
            self.setup_err.configure(text="That is not a BCD file (Windows registry hive).")
            return
        self.setup_err.configure(text="")
        self.s["bcd"] = f
        self.bcd_lbl.configure(text=Path(f).name)

    def _pick_drivers(self):
        d = filedialog.askdirectory(title="Folder with .inf drivers")
        if d:
            self.s["drivers"] = d
            self.drv_lbl.configure(text=d)

    def _setup_next(self):
        self.s["oobe"] = self.oobe_v.get()
        if self.s["oobe"]:
            vals = {k: v.get().strip() if k != "pw" else v.get() for k, v in self.sv.items()}
            vals["lang"] = self._lang_map.get(self.lang_v.get(), self.s["lang"])
            vals["locale"] = self._loc_map.get(self.loc_v.get(), self.s["locale"])
            vals["tz"] = self.tz_v.get()
            try:
                w.build_unattend(vals["user"], vals["pw"], vals["host"], vals["locale"], vals["tz"],
                                 language=vals["lang"])
            except w.Fail as e:
                self.setup_err.configure(text=str(e))
                return
            self.s.update(vals)
        self.show(5)

    def p_review(self):
        self.title_("Ready to go", "Here is what will happen.")
        i = self.s["info"]
        c = card(self.body)
        c.pack(fill="x")
        items = ["Back up the boot image of your %s" % (i["model"] or i["device"])]
        img = self._img()
        if self.s["create"]:
            items += ["Create the ESP and Windows partitions",
                      "Restart the device, then you run this guide again to install Windows"]
        else:
            items += ["Format the ESP and Windows partitions",
                      "Copy %s from your ISO to the device" % (img["name"] if img else "Windows")
                      + (", set up to start straight at the desktop" if self.s["oobe"] else ""),
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
        tk.Label(self.body, text="Next, a pre-check verifies your device and files. It changes nothing.",
                 bg=C["bg"], fg=C["muted"], anchor="w").pack(anchor="w", pady=(0, 10))
        self.nonab = tk.BooleanVar(value=self.s["nonab"])
        if not i["ab"]:
            Check(self.body, "Allow overwriting the only boot image (this device has no A/B slots)",
                  self.nonab, bg=C["bg"], command=self._review_btn).pack(anchor="w", pady=6)
        self.noreb = tk.BooleanVar(value=self.s["noreboot"])
        Check(self.body, "Don't restart the device when finished", self.noreb, bg=C["bg"]
              ).pack(anchor="w", pady=6)
        self.nav(back=lambda: self.show(3 if self.s["create"] else 4), nxt=self._start,
                 nxt_text="Start pre-check")
        self._review_btn()

    def _review_btn(self):
        self.next_btn.set_enabled(self.nonab.get() or self.s["info"]["ab"])

    # ---- pre-check and install
    def _job_card(self):
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
        self.s["job"] = -1

    def p_precheck(self):
        pc = self.s["pc"]
        if pc is None:                          # first visit: run it (nothing is changed)
            self.s.update(pc="running", dry=True, log="", result=None, rerun=False)
            threading.Thread(target=self._worker, args=(self._argv(), "pc"), daemon=True).start()
            pc = "running"
        if pc == "running":
            self.title_("Running pre-check…", "Checking your device, files and the plan. "
                        "Nothing is changed.")
            self._job_card()
            self._details()
            self.nav()
        elif pc[0] == "ok":
            self.title_("Pre-check passed", "Nothing on the device was changed.")
            self.callout(self.body, "Everything checked out. When you're ready, start the install.",
                         "ok").pack(fill="x")
            self._details()
            self.nav(back=lambda: self.show(REVIEW), nxt=self._start_install, nxt_text="Install now")
        else:
            self.title_("Pre-check found a problem", "Nothing on the device was changed.")
            self.callout(self.body, pc[1] or "Unknown error.", "err").pack(fill="x")
            self.callout(self.body, "You can still continue, but the install may fail partway and leave "
                         "the device without a working Windows. Your boot image is backed up first.",
                         "warn").pack(fill="x", pady=14)
            self.proceed_v = tk.BooleanVar(value=False)
            Check(self.body, "I understand the risk and want to install anyway", self.proceed_v,
                  bg=C["bg"], command=lambda: self.next_btn.set_enabled(self.proceed_v.get())
                  ).pack(anchor="w")
            self._details()
            self.nav(back=lambda: self.show(REVIEW), nxt=self._start_install,
                     nxt_text="Install anyway", nxt_ok=False)

    def _start_install(self):
        self.s.update(dry=False, log="", result=None, rerun=False)
        self.show(INSTALL)

    def p_install(self):
        self.title_("Installing…", "Keep the device connected. You may be asked to confirm a step.")
        self._job_card()
        self._details()
        threading.Thread(target=self._worker, args=(self._argv(), "install"), daemon=True).start()

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
        elif s["repo"] and isinstance(s["repo_state"], dict):
            a += ["--repo", s["repo"]]
        if s["sha"].strip():
            a += ["--sha256", s["sha"].strip()]
        if isinstance(s["iso_info"], dict):
            a += ["--image-index", str(s["edition"])]
        if s["dry"]:
            a.append("--dry-run")
        if s["create"]:
            a += ["--create-partitions", "--esp-size", s["esp"], "--win-size", s["win"]]
        if not s["create"]:
            if s["bcd"]:
                a += ["--bcd", s["bcd"]]
            if s["oobe"]:
                a += ["--user", s["user"], "--password", s["pw"], "--hostname", s["host"],
                      "--language", s["lang"], "--locale", s["locale"], "--timezone", s["tz"]]
            else:
                a.append("--no-unattend")
            if s["drivers"]:
                a += ["--drivers", s["drivers"]]
        if s["nonab"]:
            a.append("--allow-non-ab")
        if s["noreboot"]:
            a.append("--no-reboot")
        return a

    def _start(self):
        self.s.update(nonab=self.nonab.get(), noreboot=self.noreb.get(), pc=None, dry=True,
                      log="", result=None, rerun=False)
        self.show(PRECHECK)

    def _prompt_from_thread(self, msg):
        box = {"event": threading.Event(), "answer": ""}
        self.q.put(("prompt", (msg, box)))
        box["event"].wait()
        return box["answer"]

    def _worker(self, argv, mode):
        w.PROMPT = self._prompt_from_thread
        res = ("ok", "")
        w.NEEDS_RERUN = False
        try:
            with contextlib.redirect_stdout(LogWriter(self.q)):
                w.main(argv)
            self.s["rerun"] = w.NEEDS_RERUN
        except w.Fail as e:
            res = ("fail", str(e))
        except SystemExit:
            res = ("fail", "Invalid options.")
        except Exception as e:      # noqa: BLE001 - surface anything unexpected
            res = ("fail", "Unexpected error: %r" % e)
        self.q.put(("done", (mode, res)))

    def _pump(self):
        try:
            while True:
                kind, p = self.q.get_nowait()
                if kind == "log":
                    self._log(p)
                    for m in re.finditer(r"\[(\d)b?/6\]", p):
                        self.s["job"] = int(m.group(1)) - 1
                        if self.job_rows is not None:
                            self._set_job(self.s["job"])
                    lines = [x for x in p.replace("\r", "\n").split("\n") if x.strip()]
                    if lines and self.status is not None:
                        self.status.configure(text=lines[-1].strip()[:110])
                elif kind == "prompt":
                    msg, box = p
                    box["answer"] = PromptDialog(self, msg).result
                    box["event"].set()
                elif kind == "iso":
                    path, res = p
                    if path == self.s["iso"]:
                        self.s["iso_info"] = res
                        if isinstance(res, dict):
                            self.s["edition"] = res["image"]["index"]
                            lang = res["default_lang"]
                            self.s["lang"] = lang
                            self.s["locale"] = lang if lang in data.LOCALES else "en-US"
                        if self.page == 2:
                            self.show(2)
                elif kind == "repo":
                    repo, res = p
                    if repo == self.s["repo"]:
                        self.s["repo_state"] = res
                        if self.page == 2:
                            self.show(2)
                elif kind == "probed":
                    self.s["info"] = p
                    if self.page == 1:
                        self.show(1)
                elif kind == "done":
                    mode, res = p
                    if mode == "pc":
                        self.s["pc"] = ("ok",) if res[0] == "ok" else ("fail", res[1])
                        if self.page == PRECHECK:
                            self.show(PRECHECK)
                    else:
                        self.s["result"] = res
                        self.show(DONE)
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
        elif kind == "ok":
            self.title_("All done", "Windows is installed.")
            self.callout(self.body, "The device is restarting into Windows. The first boot runs Windows "
                         "setup and can take several minutes"
                         + (" and then signs in as “%s” automatically" % self.s["user"] if self.s["oobe"] else "")
                         + ". To return to Android, open the Boot Manager and pick “Reboot to other slot”.", "ok").pack(fill="x")
            self.nav(nxt=self.destroy, nxt_text="Close")
        else:
            self.title_("Something went wrong", "Nothing further was changed after this point.")
            self.callout(self.body, msg or "Unknown error.", "err").pack(fill="x")
            tk.Label(self.body, text="Your boot image backup is in ~/.woa-installer/backup.",
                     bg=C["bg"], fg=C["muted"], anchor="w").pack(fill="x", pady=10)
            self.nav(back=lambda: self.show(REVIEW), nxt=self.destroy, nxt_text="Close")
        self._details()


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
                 font=("TkDefaultFont", -PX["box"], "bold"), anchor="w").pack(fill="x")
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
