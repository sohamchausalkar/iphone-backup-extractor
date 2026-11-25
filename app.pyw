"""iPhone Backup Extractor - decrypts an encrypted iPhone backup and sorts the files into folders."""
import csv
import os
import queue
import re
import shutil
import sqlite3
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from tkinter import filedialog, messagebox, ttk

from iphone_backup_decrypt import EncryptedBackup
from readable import make_readable

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".3gp"}


# ---------- helpers ----------
def clean(part):
    part = re.sub(r'[<>:"|?*\x00-\x1f]', "_", part).strip().rstrip(". ")
    return "_" if part in ("", ".", "..") else part


def long_path(path):
    path = os.path.abspath(path)
    return "\\\\?\\" + path if os.name == "nt" and len(path) > 240 and not path.startswith("\\\\?\\") else path


def apple_time(v):
    if v is None:
        return ""
    try:
        v = float(v)
        if abs(v) > 1e12:  # nanoseconds
            v /= 1e9
        return (APPLE_EPOCH + timedelta(seconds=v)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def find_backup(folder):
    """Accept the device folder, or the parent 'Backup' folder (picks the newest backup inside)."""
    if os.path.exists(os.path.join(folder, "Manifest.plist")):
        return folder
    subs = [os.path.join(folder, d) for d in os.listdir(folder)
            if os.path.exists(os.path.join(folder, d, "Manifest.plist"))]
    if not subs:
        raise RuntimeError("No iPhone backup found in that folder (no Manifest.plist).")
    return max(subs, key=os.path.getmtime)


def default_backup_folder():
    for base in (os.path.join(os.path.expanduser("~"), "Apple", "MobileSync", "Backup"),
                 os.path.join(os.environ.get("APPDATA", ""), "Apple Computer", "MobileSync", "Backup")):
        if os.path.isdir(base):
            try:
                return find_backup(base)
            except Exception:
                pass
    return ""


# ---------- sorting rules ----------
def classify(domain, rel, apps, everything):
    """Return the output path parts for a file, or None to skip it."""
    p = rel.split("/")
    name = p[-1]
    low = domain.lower()
    ext = os.path.splitext(name)[1].lower()

    if rel.startswith("Media/DCIM/"):
        if ext == ".aae":
            return ["Photos", "Edit info", name]
        return ["Videos" if ext in VIDEO_EXT else "Photos", name]
    if "whatsapp" in low:
        if rel.startswith("Message/Media/"):
            return ["WhatsApp", "Media"] + p[2:]
        if ext in (".sqlite", ".sqlite-wal", ".sqlite-shm") and len(p) <= 2:
            return ["WhatsApp", "Database", name]
        return ["WhatsApp", "Other"] + p if everything else None
    if rel.startswith("Library/SMS/"):
        return ["Messages"] + p[2:]
    if rel.startswith("Library/AddressBook/"):
        return ["Contacts"] + p[2:]
    if rel.startswith("Library/CallHistoryDB/"):
        return ["Call History"] + p[2:]
    if rel.startswith("Library/Voicemail/"):
        return ["Voicemail"] + p[2:]
    if rel.startswith("Media/Recordings/"):
        return ["Voice Memos"] + p[2:]
    if rel.startswith("Library/Calendar/"):
        return ["Calendar"] + p[2:]
    if rel.startswith("Library/Safari/"):
        return ["Safari"] + p[2:]
    if "group.com.apple.notes" in low:
        return ["Notes"] + p
    if low.startswith("healthdomain"):
        return ["Health"] + p
    if "fileprovider.localstorage" in low:
        return ["Files app"] + p[1:] if p[0] == "File Provider Storage" else ["Files app"] + p
    if domain.startswith("AppDomain") and apps and rel.startswith("Documents/"):
        return ["Apps", domain.split("-", 1)[-1]] + p[1:]
    if everything:
        return ["Other", domain] + p
    return None


# ---------- main job ----------
def run_extraction(backup_dir, out_dir, password, apps, everything, log, progress, cancelled):
    backup_dir = find_backup(backup_dir)
    log("Unlocking backup (this can take a minute)...")
    backup = EncryptedBackup(backup_directory=backup_dir, passphrase=password)
    try:
        try:
            backup.test_decryption()
        except Exception as e:
            raise RuntimeError("Could not unlock the backup. Wrong password? (" + type(e).__name__ + ")")
        log("Unlocked. Reading file list...")
        items = []
        for file_id, domain, rel, fp in backup._get_manifest_rows_generator(flags=1):
            parts = classify(domain, rel, apps, everything)
            if parts:
                items.append((file_id, parts, fp))
        total_bytes = sum(i[2].filesize or 0 for i in items)
        log(f"{len(items)} files to extract ({total_bytes / 1e9:.2f} GB).")

        os.makedirs(out_dir, exist_ok=True)
        free = shutil.disk_usage(out_dir).free
        if total_bytes * 1.05 > free:
            raise RuntimeError(f"Not enough disk space: need ~{total_bytes / 1e9:.1f} GB, "
                               f"{free / 1e9:.1f} GB free on that drive.")

        used, done, missing, failed = set(), 0, 0, 0
        for n, (file_id, parts, fp) in enumerate(items, 1):
            if cancelled():
                log("Cancelled.")
                return out_dir
            parts = [clean(x) for x in parts]
            dest = os.path.join(out_dir, *parts)
            base, ext = os.path.splitext(dest)
            k = 1
            while dest.lower() in used:
                dest = f"{base}_{k}{ext}"
                k += 1
            used.add(dest.lower())
            try:
                os.makedirs(long_path(os.path.dirname(dest)), exist_ok=True)
                key = backup.keybag.unwrap_key_for_class(fp.protection_class, fp.encryption_key)
                backup._decrypt_file_to_disk(file_id=file_id, key=key, file_plist=fp,
                                             output_filepath=long_path(dest))
                done += 1
            except FileNotFoundError:
                missing += 1  # iOS lists some files it never stored
            except Exception as e:
                failed += 1
                if failed <= 5:
                    log(f"Skipped {'/'.join(parts)}: {type(e).__name__}")
            if n % 25 == 0 or n == len(items):
                progress(n, len(items))
        log(f"Extracted {done} files. Not in backup: {missing}. Failed: {failed}.")

        make_readable(out_dir, log)
        return out_dir
    finally:
        backup._cleanup()  # removes the temporary decrypted index


# ---------- window ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iPhone Backup Extractor")
        self.geometry("720x520")
        self.q = queue.Queue()
        self.stop = False
        self.worker = None

        self.backup = tk.StringVar(value=default_backup_folder())
        self.out = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Desktop", "iPhone Extracted"))
        self.pw = tk.StringVar()
        self.apps = tk.BooleanVar(value=True)
        self.everything = tk.BooleanVar(value=False)

        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Backup folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.backup).grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(f, text="Browse", command=lambda: self.pick(self.backup)).grid(row=0, column=2)
        ttk.Label(f, text="Save to").grid(row=1, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.out).grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(f, text="Browse", command=lambda: self.pick(self.out)).grid(row=1, column=2)
        ttk.Label(f, text="Backup password").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.pw, show="*").grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        ttk.Checkbutton(f, text="Include app documents (Documents folder of each app)",
                        variable=self.apps).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(f, text="Include everything else (system files, can be large)",
                        variable=self.everything).grid(row=4, column=1, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=5, column=1, sticky="w", pady=8)
        self.start_btn = ttk.Button(row, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.readable_btn = ttk.Button(row, text="Rebuild readable pages only (no password)", command=self.rebuild)
        self.readable_btn.pack(side="left", padx=6)
        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", pady=4)
        self.logbox = tk.Text(f, height=14, state="disabled", wrap="word")
        self.logbox.grid(row=7, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(7, weight=1)
        self.after(100, self.poll)

    def pick(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or None)
        if d:
            var.set(os.path.normpath(d))

    def log(self, msg):
        self.q.put(("log", msg))

    def poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", val + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "progress":
                    self.bar["value"] = val
                elif kind == "done":
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.pw.set("")
                    if val:
                        self.log("Done. Opening the output folder.")
                        os.startfile(val)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def start(self):
        if not self.backup.get() or not self.pw.get():
            messagebox.showwarning("Missing info", "Choose the backup folder and enter the password.")
            return
        self.stop = False
        self.bar["value"] = 0
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        args = (self.backup.get(), self.out.get(), self.pw.get(), self.apps.get(), self.everything.get())
        self.worker = threading.Thread(target=self.work, args=args, daemon=True)
        self.worker.start()

    def rebuild(self):
        out = self.out.get()
        if not os.path.isdir(out):
            messagebox.showwarning("Missing folder", "The 'Save to' folder doesn't exist yet.")
            return
        self.start_btn.configure(state="disabled")
        threading.Thread(target=self.rebuild_work, args=(out,), daemon=True).start()

    def rebuild_work(self, out):
        try:
            make_readable(out, self.log)
            self.q.put(("done", os.path.join(out, "START HERE.html")))
        except Exception as e:
            self.log(f"ERROR: {e}")
            self.q.put(("done", None))

    def cancel(self):
        self.stop = True

    def work(self, backup, out, pw, apps, everything):
        result = None
        try:
            result = run_extraction(backup, out, pw, apps, everything, self.log,
                                    lambda n, t: self.q.put(("progress", 100 * n / t)), lambda: self.stop)
        except Exception as e:
            self.log(f"ERROR: {e}")
        self.q.put(("done", result))


if __name__ == "__main__":
    App().mainloop()
