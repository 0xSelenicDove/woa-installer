#!/usr/bin/env python3
"""Tk front end for woa_installer (standard library only)."""
import contextlib
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import woa_installer as w


class LogWriter:
    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(("log", text))
        return len(text)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WOA Installer")
        self.geometry("760x680")
        self.q = queue.Queue()
        self.vars = {k: tk.StringVar() for k in ("iso", "uefi", "esp", "win", "index", "bcd")}
        self.opts = {k: tk.BooleanVar(value=v) for k, v in
                     (("dry", True), ("create", False), ("nonab", False), ("noreboot", False),
                      ("skipwin", False))}
        self.esp_size, self.win_size = tk.StringVar(value="512M"), tk.StringVar(value="64G")
        self._build()
        self.after(100, self._pump)

    # ---- layout
    def _file_row(self, parent, label, key, types, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(parent, text="Browse...", command=lambda: self._browse(key, types)
                   ).grid(row=row, column=2)

    def _browse(self, key, types):
        f = filedialog.askopenfilename(filetypes=types)
        if f:
            self.vars[key].set(f)

    def _build(self):
        pad = {"padx": 14, "pady": 6}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)
        self._file_row(top, "Windows 11 ARM64 ISO", "iso", [("ISO", "*.iso"), ("All", "*")], 0)
        self._file_row(top, "UEFI installer zip\n(optional)", "uefi", [("ZIP", "*.zip")], 1)

        o = ttk.LabelFrame(self, text="Options")
        o.pack(fill="x", **pad)
        ttk.Checkbutton(o, text="Dry run (read-only rehearsal - recommended first)",
                        variable=self.opts["dry"]).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(o, text="Create ESP / Windows partitions if missing (may erase data)",
                        variable=self.opts["create"]).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(o, text="ESP size").grid(row=2, column=0, sticky="e")
        ttk.Entry(o, textvariable=self.esp_size, width=8).grid(row=2, column=1, sticky="w")
        ttk.Label(o, text="Windows size").grid(row=2, column=2, sticky="e")
        ttk.Entry(o, textvariable=self.win_size, width=8).grid(row=2, column=3, sticky="w")
        ttk.Checkbutton(o, text="Allow non-A/B device (overwrites the only boot image)",
                        variable=self.opts["nonab"]).grid(row=3, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(o, text="Don't reboot at the end", variable=self.opts["noreboot"]
                        ).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(o, text="UEFI only (skip Windows)", variable=self.opts["skipwin"]
                        ).grid(row=4, column=2, columnspan=2, sticky="w")

        adv = ttk.LabelFrame(self, text="Advanced (leave blank for auto)")
        adv.pack(fill="x", **pad)
        adv.columnconfigure(1, weight=1)
        for r, (lab, key) in enumerate((("ESP partition", "esp"), ("Windows partition", "win"),
                                        ("Image index", "index"))):
            ttk.Label(adv, text=lab).grid(row=r, column=0, sticky="w")
            ttk.Entry(adv, textvariable=self.vars[key], width=24).grid(row=r, column=1, sticky="w",
                                                                        padx=6)
        ttk.Label(adv, text="BCD file").grid(row=3, column=0, sticky="w")
        ttk.Entry(adv, textvariable=self.vars["bcd"]).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Button(adv, text="Browse...", command=lambda: self._browse("bcd", [("All", "*")])
                   ).grid(row=3, column=2)

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.btn = ttk.Button(bar, text="Start", command=self.start)
        self.btn.pack(side="left")
        self.prog = ttk.Progressbar(bar, mode="indeterminate")
        self.prog.pack(side="left", fill="x", expand=True, padx=10)

        self.log = scrolledtext.ScrolledText(self, height=16, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ---- logging (handles \r progress lines)
    def _log(self, text):
        self.log.configure(state="normal")
        for i, part in enumerate(text.replace("\r\n", "\n").split("\r")):
            if i > 0:
                self.log.delete("end-1c linestart", "end-1c")
            self.log.insert("end", part)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "prompt":
                    msg, box = payload
                    box["answer"] = self._ask(msg)
                    box["event"].set()
                elif kind == "done":
                    self.prog.stop()
                    self.btn.configure(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _ask(self, msg):
        m = msg.strip()
        if m.endswith("[y/N]"):
            return "y" if messagebox.askyesno("woa-installer", m[:-5].strip()) else "n"
        ans = simpledialog.askstring("woa-installer", m.rstrip(": ") + "\n", parent=self)
        return ans or ""

    # ---- running
    def _prompt_from_thread(self, msg):
        box = {"event": threading.Event(), "answer": ""}
        self.q.put(("prompt", (msg, box)))
        box["event"].wait()
        return box["answer"]

    def _argv(self):
        v, o = self.vars, self.opts
        a = []
        if v["iso"].get().strip():
            a += ["--iso", v["iso"].get().strip()]
        if v["uefi"].get().strip():
            a += ["--uefi-zip", v["uefi"].get().strip()]
        for key, flag in (("esp", "--esp-part"), ("win", "--win-part"),
                          ("index", "--image-index"), ("bcd", "--bcd")):
            if v[key].get().strip():
                a += [flag, v[key].get().strip()]
        if o["dry"].get():
            a.append("--dry-run")
        if o["create"].get():
            a += ["--create-partitions", "--esp-size", self.esp_size.get(),
                  "--win-size", self.win_size.get()]
        for key, flag in (("nonab", "--allow-non-ab"), ("noreboot", "--no-reboot"),
                          ("skipwin", "--skip-windows")):
            if o[key].get():
                a.append(flag)
        return a

    def start(self):
        if not self.opts["skipwin"].get() and not self.vars["iso"].get().strip():
            messagebox.showwarning("woa-installer", "Choose a Windows ARM64 ISO first.")
            return
        if not self.opts["dry"].get() and not messagebox.askyesno(
                "woa-installer", "This is NOT a dry run and will modify the phone.\nContinue?"):
            return
        argv = self._argv()
        self.btn.configure(state="disabled")
        self.prog.start(12)
        self._log("\n$ woa-installer %s\n" % " ".join(argv))
        threading.Thread(target=self._worker, args=(argv,), daemon=True).start()

    def _worker(self, argv):
        w.PROMPT = self._prompt_from_thread
        try:
            with contextlib.redirect_stdout(LogWriter(self.q)):
                w.main(argv)
            self.q.put(("log", "\nFinished.\n"))
        except w.Fail as e:
            self.q.put(("log", "\nERROR: %s\n" % e))
        except SystemExit:
            self.q.put(("log", "\nInvalid options.\n"))
        except Exception as e:          # noqa: BLE001 - show anything unexpected
            self.q.put(("log", "\nUNEXPECTED ERROR: %r\n" % e))
        finally:
            self.q.put(("done", None))


if __name__ == "__main__":
    App().mainloop()
