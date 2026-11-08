#!/usr/bin/env python3
"""
Ghidra Installer for Windows
Downloads the latest (or any selected) Ghidra release from GitHub,
installs Temurin JDK if needed, extracts Ghidra to the chosen
directory, creates a launcher batch file, and adds shortcuts.

Requirements: Python 3.8+  (no third-party packages needed)
Run:          python ghidra_installer_windows.py
"""

import json
import os
import platform
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Suppress CMD windows for all subprocess calls on Windows
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# ──────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────
GITHUB_API         = "https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/latest"
ADOPTIUM_API_BASE  = "https://api.adoptium.net/v3/assets/latest/{ver}/hotspot"
DEFAULT_JAVA_MAJOR = 25   # floor for Ghidra 12.2+; overridden at runtime by LaunchSupport.jar
CHUNK_SIZE         = 256 * 1024

# Windows default paths
_LOCALAPPDATA    = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
_APPDATA         = Path(os.environ.get("APPDATA",      str(Path.home())))
DEFAULT_DIR      = str(_LOCALAPPDATA / "Ghidra")
JDK_INSTALL_DIR  = _LOCALAPPDATA / "GhidraInstaller" / "jdk"
INFO_DIR         = _APPDATA / "GhidraInstaller"
INFO_FILE        = INFO_DIR / "install_info.json"

# ──────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────

class _RebootRequired(Exception):
    """Raised by _force_rmtree when the locked path was scheduled for
    deletion on next reboot but still exists.  Carries the locked path."""
    def __init__(self, path: Path):
        self.path = path
        super().__init__(str(path))


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_speed(bps: float) -> str:
    return f"{fmt_size(bps)}/s"


def create_ssl_context() -> ssl.SSLContext:
    """Return the best available SSL context for this environment.

    Priority in a frozen (PyInstaller) bundle:
      1. Windows system certificate store  -- handles corporate proxies,
         custom CAs, Parallels NAT, etc.  certifi does NOT include these.
      2. certifi bundle extracted to sys._MEIPASS (last resort if Windows
         store is somehow unavailable).
      3. Unverified context (still encrypted, just no chain check).

    Priority when running as a plain Python script:
      1. certifi (always up-to-date CA bundle)
      2. Windows system cert store
      3. Unverified context.
    """
    import sys as _sys

    if getattr(_sys, 'frozen', False):
        # --- Frozen bundle ---
        # IMPORTANT: use the Windows cert store FIRST, not certifi.
        # Specifying cafile= replaces the Windows store; omitting it loads it.
        try:
            return ssl.create_default_context()       # loads Windows Root CA store
        except Exception:
            pass
        # certifi fallback inside the bundle
        mei = Path(getattr(_sys, '_MEIPASS', ''))
        for ca in (mei / 'certifi' / 'cacert.pem', mei / 'cacert.pem'):
            if ca.exists():
                try:
                    return ssl.create_default_context(cafile=str(ca))
                except Exception:
                    break
        return ssl._create_unverified_context()       # noqa: SLF001

    # --- Normal script run ---
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        pass
    return ssl._create_unverified_context()           # noqa: SLF001


def _urlopen_ssl(req: urllib.request.Request, timeout: int = 30):
    """urlopen with automatic SSL fallback."""
    try:
        return urllib.request.urlopen(req, timeout=timeout,
                                      context=create_ssl_context())
    except urllib.error.URLError as exc:
        msg = str(exc)
        if 'CERTIFICATE_VERIFY_FAILED' in msg or 'SSL' in msg.upper():
            return urllib.request.urlopen(
                req, timeout=timeout,
                context=ssl._create_unverified_context())  # noqa: SLF001
        raise


def is_dark_mode() -> bool:
    """Check Windows dark mode via registry."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────
#  Install metadata helpers
# ──────────────────────────────────────────────────────────────────

def _read_installs():
    try:
        if INFO_FILE.exists():
            with open(INFO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def _write_installs(installs):
    INFO_DIR.mkdir(parents=True, exist_ok=True)
    with open(INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(installs, f, indent=2)


def _append_install(entry, log=None):
    installs = _read_installs()
    duplicate = next((i for i in installs
                      if i.get("folder") == entry.get("folder")
                      and i.get("version") == entry.get("version")), None)
    if duplicate:
        installs.remove(duplicate)
    installs.append(entry)
    _write_installs(installs)
    if log:
        log(f"Saved install metadata: {entry.get('version')}")


def _get_start_menu_programs_dir():
    return (
        Path(os.environ["APPDATA"])
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
    )


# ──────────────────────────────────────────────────────────────────
#  Main application
# ──────────────────────────────────────────────────────────────────

class GhidraInstallerWindows(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Ghidra Installer")
        self.resizable(True, True)

        # State
        self._download_url: str = ""
        self._asset_name:   str = ""
        self._asset_size:   int = 0
        self._cancel:       bool = False
        self._install_dir   = tk.StringVar(value=DEFAULT_DIR)
        self._releases:     list = []
        self._version_var   = tk.StringVar()

        self._build_ui()

        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h   = self.winfo_reqwidth(),    self.winfo_reqheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.minsize(560, 620)

        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()

        threading.Thread(target=self._fetch_releases, daemon=True).start()
        threading.Thread(target=self._check_java,     daemon=True).start()
        self._load_installed_version()

    # ── UI ────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("vista")          # native Windows theme

        PAD = dict(padx=20, pady=6)

        # Header
        hdr = tk.Frame(self, bg="#16213e")
        hdr.pack(fill="x")
        tk.Label(hdr, text="Ghidra Installer",
                 font=("Segoe UI", 20, "bold"),
                 fg="white", bg="#16213e", pady=14).pack()
        tk.Label(hdr, text="NSA Software Reverse Engineering Framework",
                 font=("Segoe UI", 11),
                 fg="#8892b0", bg="#16213e").pack(pady=(0, 12))

        # Release card
        card = tk.LabelFrame(self, text=" Ghidra Release ",
                             font=("Segoe UI", 10), padx=14, pady=8)
        card.pack(fill="x", **PAD)

        # Version selector
        ver_row = tk.Frame(card)
        ver_row.pack(fill="x", pady=2)
        tk.Label(ver_row, text="Version:",
                 font=("Segoe UI", 10, "bold"),
                 width=14, anchor="w").pack(side="left")
        self.combo_version = ttk.Combobox(
            ver_row, textvariable=self._version_var,
            font=("Segoe UI", 10), state="readonly", width=32)
        self.combo_version.pack(side="left", fill="x", expand=True)
        self.combo_version.bind("<<ComboboxSelected>>", self._on_version_selected)
        self.combo_version.set("Fetching releases…")

        def info_row(parent, label):
            row = tk.Frame(parent)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, font=("Segoe UI", 10, "bold"),
                     width=14, anchor="w").pack(side="left")
            lbl = tk.Label(row, text="—", font=("Segoe UI", 10))
            lbl.pack(side="left")
            return lbl

        self.lbl_installed = info_row(card, "Installed:")
        self.lbl_date      = info_row(card, "Released:")
        self.lbl_size      = info_row(card, "Size:")
        self.lbl_sha       = info_row(card, "SHA-256:")
        self.lbl_java      = info_row(card, "Java 25+:")
        self.lbl_natives   = info_row(card, "Windows build:")

        # Install location
        loc = tk.LabelFrame(self, text=" Install Location ",
                            font=("Segoe UI", 10), padx=14, pady=8)
        loc.pack(fill="x", **PAD)
        row = tk.Frame(loc)
        row.pack(fill="x")
        self.entry_dir = ttk.Entry(row, textvariable=self._install_dir,
                                   font=("Segoe UI", 10))
        self.entry_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…",
                   command=self._browse).pack(side="left", padx=(8, 0))
        tk.Label(loc,
                 text="Ghidra will be extracted into a subfolder here. "
                      "A Desktop shortcut will also be created.",
                 font=("Segoe UI", 9), fg="gray",
                 wraplength=420, justify="left").pack(anchor="w", pady=(4, 0))

        # Options
        opt = tk.LabelFrame(self, text=" Options ",
                            font=("Segoe UI", 10), padx=14, pady=8)
        opt.pack(fill="x", **PAD)
        self._shortcut_var  = tk.BooleanVar(value=True)
        self._startmenu_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Create Desktop shortcut",
                        variable=self._shortcut_var).pack(anchor="w")
        ttk.Checkbutton(opt, text="Add to Start Menu",
                        variable=self._startmenu_var).pack(anchor="w")

        # Progress
        pf = tk.Frame(self, padx=20, pady=4)
        pf.pack(fill="x")
        self.lbl_status = tk.Label(pf, text="Ready",
                                   font=("Segoe UI", 10), anchor="w")
        self.lbl_status.pack(fill="x")
        self.progress = ttk.Progressbar(pf, mode="determinate", length=460)
        self.progress.pack(fill="x", pady=(4, 0))
        self.lbl_detail = tk.Label(pf, text="",
                                   font=("Segoe UI", 9), anchor="w")
        self.lbl_detail.pack(fill="x")

        # Buttons
        bf = tk.Frame(self, padx=20, pady=12)
        bf.pack(fill="x")
        ttk.Button(bf, text="Quit",
                   command=self.destroy).pack(side="left")
        ttk.Button(bf, text="Uninstall…",
                   command=self._show_uninstall_dialog).pack(side="left", padx=(8, 0))
        self.btn_cancel = ttk.Button(bf, text="Cancel",
                                     command=self._cancel_install,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.btn_install = ttk.Button(bf, text="Download & Install",
                                      command=self._start_install,
                                      state="disabled")
        self.btn_install.pack(side="right")

        # Log
        lf = tk.LabelFrame(self, text=" Log ",
                           font=("Segoe UI", 10), padx=12, pady=6)
        lf.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        dark  = is_dark_mode()
        logbg = "#1c1c1e" if dark else "#f5f5f5"
        logfg = "#e5e5ea" if dark else "#1c1c1e"
        self.log = tk.Text(lf, height=9, font=("Consolas", 9),
                           wrap="word", state="disabled",
                           bg=logbg, fg=logfg)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

    # ── Helpers ───────────────────────────────────────────────────

    def _log(self, msg: str):
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _do)

    def _set_status(self, msg: str, color: str = "gray"):
        self.after(0, lambda: self.lbl_status.configure(text=msg, fg=color))

    def _set_detail(self, msg: str):
        self.after(0, lambda: self.lbl_detail.configure(text=msg))

    def _set_progress(self, pct: float):
        self.after(0, lambda: self.progress.configure(value=pct))

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._install_dir.get(),
                                    title="Choose Install Directory")
        if d:
            self._install_dir.set(d)

    def _load_installed_version(self):
        """Read install_info.json and update the Installed: label."""
        installs = _read_installs()
        if installs:
            if len(installs) == 1:
                txt = installs[0].get("version", "Unknown")
            else:
                latest = installs[-1].get("version", "?")
                txt = f"{len(installs)} versions  (latest: {latest})"
            self.after(0, lambda: self.lbl_installed.configure(text=txt))
        else:
            self.after(0, lambda: self.lbl_installed.configure(
                text="Not installed"))

    # ── Java ─────────────────────────────────────────────────────

    def _check_java(self):
        ok, label = self._probe_java(DEFAULT_JAVA_MAJOR)
        if ok:
            self.after(0, lambda: self.lbl_java.configure(
                text=label, fg="#4CAF50"))
        else:
            self.after(0, lambda: self.lbl_java.configure(
                text=label + " — will auto-install", fg="darkorange"))

    @staticmethod
    def _probe_java(min_major: int = DEFAULT_JAVA_MAJOR) -> tuple:
        """Scan all known JVM locations and return (ok, label)."""
        import glob as _glob

        candidates: list[str] = []

        # Bundled JDK (always check first)
        candidates.append(str(JDK_INSTALL_DIR / "bin" / "java.exe"))

        # JAVA_HOME environment variable
        java_home_env = os.environ.get("JAVA_HOME", "")
        if java_home_env:
            candidates.insert(0, str(Path(java_home_env) / "bin" / "java.exe"))

        # Vendor scan under Program Files
        pf_dirs = [
            os.environ.get("ProgramFiles",      r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("ProgramW6432",      r"C:\Program Files"),
        ]
        for pf in set(filter(None, pf_dirs)):
            for pattern in (
                str(Path(pf) / "Eclipse Adoptium" / "jdk-*" / "bin" / "java.exe"),
                str(Path(pf) / "Microsoft"         / "jdk-*" / "bin" / "java.exe"),
                str(Path(pf) / "Java"              / "jdk*"  / "bin" / "java.exe"),
                str(Path(pf) / "BellSoft"          / "LibericaJDK-*" / "bin" / "java.exe"),
                str(Path(pf) / "Amazon Corretto"   / "jdk*"  / "bin" / "java.exe"),
            ):
                candidates.extend(sorted(_glob.glob(pattern)))

        # PATH fallback
        candidates.append("java")

        best_old = ""
        seen: set[str] = set()
        for java_bin in candidates:
            if java_bin in seen:
                continue
            seen.add(java_bin)
            try:
                r = subprocess.run([java_bin, "-version"],
                                   capture_output=True, text=True, timeout=5,
                                   creationflags=_CREATE_NO_WINDOW)
                m = re.search(r'version "(\d+)', r.stderr + r.stdout)
                if m:
                    major = int(m.group(1))
                    src   = " (bundled)" if "GhidraInstaller" in java_bin else ""
                    if major >= min_major:
                        return True, f"Java {major}{src} found ✓"
                    else:
                        best_old = f"Java {major} found (need ≥{min_major})"
            except Exception:
                continue
        if best_old:
            return False, best_old
        return False, "Not found"

    @staticmethod
    def _find_java_home(min_major: int = DEFAULT_JAVA_MAJOR) -> "Path | None":
        """Return the Java home directory for the first JVM that meets min_major."""
        import glob as _glob

        candidates: list[Path] = []

        # Bundled JDK
        if JDK_INSTALL_DIR.exists():
            candidates.append(JDK_INSTALL_DIR)

        # JAVA_HOME env
        java_home_env = os.environ.get("JAVA_HOME", "")
        if java_home_env:
            candidates.insert(0, Path(java_home_env))

        # Vendor scan
        pf_dirs = [
            os.environ.get("ProgramFiles",  r"C:\Program Files"),
            os.environ.get("ProgramW6432",  r"C:\Program Files"),
        ]
        for pf in set(filter(None, pf_dirs)):
            for pattern in (
                str(Path(pf) / "Eclipse Adoptium" / "jdk-*"),
                str(Path(pf) / "Microsoft"         / "jdk-*"),
                str(Path(pf) / "Java"              / "jdk*"),
                str(Path(pf) / "BellSoft"          / "LibericaJDK-*"),
                str(Path(pf) / "Amazon Corretto"   / "jdk*"),
            ):
                candidates.extend(Path(p) for p in sorted(_glob.glob(pattern)))

        for home in candidates:
            java_exe = home / "bin" / "java.exe"
            if not java_exe.exists():
                continue
            try:
                r = subprocess.run([str(java_exe), "-version"],
                                   capture_output=True, text=True, timeout=5,
                                   creationflags=_CREATE_NO_WINDOW)
                m = re.search(r'version "(\d+)', r.stderr + r.stdout)
                if m and int(m.group(1)) >= min_major:
                    return home
            except Exception:
                continue
        return None

    def _kill_ghidra(self, ghidra_path: "Path | None" = None):
        """Terminate Ghidra, wscript, and Java processes related to Ghidra.

        Uses tasklist for ghidra-named processes, then PowerShell
        Get-CimInstance for command-line inspection (wmic.exe was removed
        from Windows 11 so we never call it).
        """
        path_hint = str(ghidra_path).lower() if ghidra_path else ""
        killed = 0

        # ── Pass 1: tasklist — kill ghidra* / analyzeheadless* ────────────
        try:
            r = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW)
            for line in r.stdout.splitlines():
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) < 2:
                    continue
                name = cols[0].lower()
                pid  = cols[1]
                if any(x in name for x in ("ghidra", "analyzeheadless")):
                    self._log(f"Terminating {cols[0]} (PID {pid})…")
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True,
                                   creationflags=_CREATE_NO_WINDOW)
                    killed += 1
        except Exception as e:
            self._log(f"tasklist warning: {e}")

        # ── Pass 2: PowerShell CIM — find wscript/java running from Ghidra ─
        # wmic.exe is not available on Windows 11; Get-CimInstance replaces it.
        try:
            ps_cmd = (
                "Get-CimInstance Win32_Process "
                "| Select-Object Name,ProcessId,CommandLine "
                "| ConvertTo-Csv -NoTypeInformation"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-Command", ps_cmd],
                capture_output=True, text=True, timeout=15,
                creationflags=_CREATE_NO_WINDOW)
            for line in r.stdout.splitlines()[1:]:   # skip CSV header
                cols = [c.strip('"') for c in line.split('","')]
                if len(cols) < 3:
                    continue
                name    = cols[0].lower()
                pid     = cols[1].strip()
                cmdline = cols[2].lower() if len(cols) > 2 else ""
                if not pid.isdigit():
                    continue
                if name not in ("wscript.exe", "java.exe",
                                "javaw.exe", "cmd.exe"):
                    continue
                if path_hint in cmdline or "ghidra" in cmdline:
                    self._log(
                        f"Terminating {cols[0]} (PID {pid}) — Ghidra process")
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", pid],
                        capture_output=True,
                        creationflags=_CREATE_NO_WINDOW)
                    killed += 1
        except Exception as e:
            self._log(f"PowerShell process scan warning: {e}")

        if killed:
            self._log(
                f"Terminated {killed} process(es). Waiting for handles to release…")
            time.sleep(2.0)
        else:
            self._log("No Ghidra processes found running.")

    def _force_rmtree(self, path: Path):
        """Remove a directory tree, handling read-only and locked files.

        Strategy (each step tried in order, with a short retry loop first):
        1. Python rmtree with onexc/onerror (chmod read-only files then retry).
        2. PowerShell Remove-Item -Recurse -Force  (bypasses some Explorer locks).
        3. Rename the folder out of the way, then delete the renamed copy.
        4. MoveFileExW(MOVEFILE_DELAY_UNTIL_REBOOT) to schedule deletion.
           If the rename in Step 3 also failed, schedule the original path.
           Raises RuntimeError only after Step 4 has been attempted.
        """
        import ctypes, sys

        MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004

        def _try_movefile_reboot(target: Path) -> bool:
            """Schedule target (file or dir) for deletion on next Windows boot."""
            try:
                ok = ctypes.windll.kernel32.MoveFileExW(
                    str(target), None, MOVEFILE_DELAY_UNTIL_REBOOT)
                return bool(ok)
            except Exception:
                return False

        # ── Retry wrapper: Windows Defender / Indexer often releases within ──
        # a few seconds.  Try the whole removal sequence up to 3 times.
        for attempt in range(1, 4):
            if attempt > 1:
                self._log(f"Retry {attempt}/3 — waiting 4 s for handles to release…")
                time.sleep(4)

            # ── Step 1: rmtree with per-file chmod callback ───────────────────
            try:
                if sys.version_info >= (3, 12):
                    def _on_exc(func, fpath, exc):
                        try:
                            os.chmod(fpath, stat.S_IWRITE)
                            func(fpath)
                        except Exception:
                            pass
                    shutil.rmtree(path, onexc=_on_exc)
                else:
                    def _on_error(func, fpath, exc_info):
                        try:
                            os.chmod(fpath, stat.S_IWRITE)
                            func(fpath)
                        except Exception:
                            pass
                    shutil.rmtree(path, onerror=_on_error)
                if not path.exists():
                    self._log("Existing folder removed ✓")
                    return
            except Exception:
                pass

            # ── Step 2: PowerShell Remove-Item ───────────────────────────────
            try:
                self._log("Trying PowerShell Remove-Item…")
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Remove-Item -LiteralPath '{path}' -Recurse -Force"
                     " -ErrorAction SilentlyContinue"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=_CREATE_NO_WINDOW)
                if not path.exists():
                    self._log("Removed via PowerShell ✓")
                    return
            except Exception as e:
                self._log(f"PowerShell remove warning: {e}")

            # ── Step 3: rename out of the way, then delete renamed copy ──────
            tombstone = path.parent / (path.name + f"._old_{int(time.time())}")
            renamed_ok = False
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Rename-Item -LiteralPath '{path}' -NewName '{tombstone.name}'"
                     " -Force -ErrorAction Stop"],
                    capture_output=True, text=True, timeout=15,
                    creationflags=_CREATE_NO_WINDOW)
                if not path.exists():
                    renamed_ok = True
                    self._log(f"Renamed locked folder → {tombstone.name}")
            except Exception:
                pass

            if renamed_ok:
                # Delete the tombstone (best effort)
                try:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         f"Remove-Item -LiteralPath '{tombstone}' -Recurse -Force"
                         " -ErrorAction SilentlyContinue"],
                        capture_output=True, text=True, timeout=30,
                        creationflags=_CREATE_NO_WINDOW)
                    if not tombstone.exists():
                        self._log("Old folder deleted ✓")
                        return
                except Exception:
                    pass

                # ── Step 4a: tombstone renamed but still locked → schedule ──
                ok = _try_movefile_reboot(tombstone)
                msg = ("will be cleaned up on next reboot." if ok
                       else "could not be scheduled — delete it manually.")
                self._log(f"Note: '{tombstone.name}' {msg}")
                return   # path itself is free; install can proceed

            # Rename also failed — loop and retry, or fall through to Step 4b
            if attempt < 3:
                continue

        # ── Step 4b: even rename failed after all retries ────────────────
        # Schedule the original locked path for deletion on next boot.
        ok = _try_movefile_reboot(path)
        if ok:
            self._log(
                f"⚠ '{path.name}' is locked and scheduled for deletion on next reboot.\n"
                "The new version will be installed to a temporary name and shortcuts\n"
                "updated. Restart Windows to complete the cleanup.")
            # Signal to caller that path still exists but reboot-delete is queued.
            # We raise a specific sentinel so the worker can choose an alt install path.
            raise _RebootRequired(path)
        raise RuntimeError(
            f"Cannot remove or rename '{path.name}'.\n\n"
            "A process (Ghidra, Windows Defender, Search Indexer, or "
            "Windows Explorer) is still holding a handle on that folder.\n\n"
            "Try: close any Explorer window showing the Ghidra folder, "
            "wait a few seconds, then click Install again."
        )

    @staticmethod
    def _get_required_java_version(ghidra_src: Path) -> int:
        """Detect the minimum Java version Ghidra requires — fully automatic.

        Strategy (stops at first confident answer):
          1. Run Ghidra's own LaunchSupport.jar with whatever Java is available
             and parse its error message — this is the authoritative source and
             will always be correct regardless of future Ghidra/Java releases.
          2. Fall back to scanning support/launch.properties and other text
             files for MINIMUM_SUPPORTED_JAVA= (stable key, present since 10.x).
          3. Last resort: return DEFAULT_JAVA_MAJOR (21) as a safe floor.

        No hard-coded version list means this works for Java 25, 26, 27 … etc.
        without any code changes.
        """

        # ── Strategy 1: ask LaunchSupport.jar ─────────────────────────────────
        # Ghidra ships support/LaunchSupport.jar specifically to validate the JDK.
        # When called with an incompatible (or missing) JDK it prints something
        # like "Java 25 (64-bit) is required to launch Ghidra."
        # We try every java executable we can find; even a "wrong" version is
        # good enough to run this tiny utility jar.
        launch_support = ghidra_src / "support" / "LaunchSupport.jar"
        launch_props   = ghidra_src / "support" / "launch.properties"

        if launch_support.exists() and launch_props.exists():
            # Candidates: bundled JDK first, then whatever is on PATH.
            java_candidates: list[str] = []
            bundled = JDK_INSTALL_DIR / "bin" / "java.exe"
            if bundled.exists():
                java_candidates.append(str(bundled))
            java_candidates.append("java")          # PATH fallback

            # LaunchSupport args: <props> <mode> <jdk-type> <app> <maxmem> <vmargs> <classname>
            ls_args_suffix = [
                str(launch_props), "fg", "jdk", "Ghidra",
                "512m", "", "ghidra.GhidraRun",
            ]

            # Patterns that identify the REQUIRED version (not the found version).
            # Deliberately narrow so "Java 21 was found" doesn't match.
            # We collect ALL matches and take the maximum to handle outputs like
            # "Java 21 was found, but Java 25 (64-bit) is required."
            _req_patterns = [
                # "Java 25 (64-bit) is required"  /  "Java 25 or higher is required"
                r'[Jj]ava\s+(\d+)(?:\s*\([^)]*\))?\s+(?:or\s+(?:higher|later|above)\s+)?is\s+required',
                # "Java 25 or higher is required"  (alt word order)
                r'[Jj]ava\s+(\d+)\s+or\s+(?:higher|later|above)',
                # "requires Java 25"  /  "require Java 25"
                r'[Rr]equires?\s+[Jj]ava\s+(\d+)',
                # "at least Java 25"  /  "minimum Java 25"
                r'(?:at\s+least|minimum)\s+[Jj]ava\s+(\d+)',
                # "MINIMUM_SUPPORTED_JAVA=25" echoed to stderr
                r'MINIMUM_SUPPORTED_JAVA\s*[=:]\s*(\d+)',
            ]

            for java_cmd in java_candidates:
                try:
                    r = subprocess.run(
                        [java_cmd, "-jar", str(launch_support)] + ls_args_suffix,
                        capture_output=True, text=True, timeout=20,
                        creationflags=_CREATE_NO_WINDOW,
                        cwd=str(ghidra_src),
                    )
                    combined = r.stdout + "\n" + r.stderr
                    # Collect ALL version matches; take the highest.
                    best_from_jar: int = 0
                    for pat in _req_patterns:
                        for m in re.finditer(pat, combined, re.IGNORECASE):
                            ver = int(m.group(1))
                            if 17 <= ver <= 99:
                                best_from_jar = max(best_from_jar, ver)
                    if best_from_jar:
                        return best_from_jar
                    # LaunchSupport exited 0 (JDK acceptable) or no parseable
                    # version — fall through to text-file scanning.
                except Exception:
                    continue

        # ── Strategy 2: parse Ghidra's text files ─────────────────────────────
        probe_files = [
            ghidra_src / "support" / "launch.properties",   # primary source
            ghidra_src / "support" / "launch.bat",
            ghidra_src / "support" / "launch.sh",
            ghidra_src / "ghidraRun.bat",
            ghidra_src / "support" / "ghidraRun",
        ]
        support_dir = ghidra_src / "support"
        if support_dir.is_dir():
            for p in support_dir.iterdir():
                if p.suffix.lower() in (".bat", ".sh", ".properties", ""):
                    if p not in probe_files:
                        probe_files.append(p)

        # Ordered most-specific → least-specific to avoid false matches.
        text_patterns = [
            r'MINIMUM_SUPPORTED_JAVA\s*[=:]\s*(\d+)',
            r'MINIMUM_JAVA_VERSION\s*[=:]\s*(\d+)',
            r'JAVA_VERSION\s*[=:]\s*(\d+)',
            r'JAVA_VER\s*[=:]\s*(\d+)',
            r'jdk[_-](\d{2,})',
        ]

        best: int = 0
        for pf in probe_files:
            if not pf.exists() or not pf.is_file():
                continue
            try:
                text = pf.read_text(errors="ignore")
                for pat in text_patterns:
                    for m in re.finditer(pat, text, re.IGNORECASE):
                        ver = int(m.group(1))
                        if 17 <= ver <= 99 and ver > best:
                            best = ver
            except Exception:
                continue

        # ── Strategy 3: safe floor ────────────────────────────────────────────
        return best if best else DEFAULT_JAVA_MAJOR

    @staticmethod
    def _try_winget_java(java_major: int, log_fn) -> "tuple[bool, str]":
        """
        Install or upgrade Temurin JDK {java_major} via winget.
        Returns (success, status_message).  On success the caller should call
        _find_java_home() to locate the newly installed JAVA_HOME.
        Falls back gracefully if winget is unavailable or the install fails.
        """
        # 1. Is winget present?
        try:
            r = subprocess.run(
                ["winget", "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW)
            if r.returncode != 0:
                return False, "winget not available"
        except FileNotFoundError:
            return False, "winget not installed"
        except Exception as e:
            return False, f"winget probe error: {e}"

        pkg_id = f"EclipseAdoptium.Temurin.{java_major}.JDK"
        log_fn(f"Installing Java {java_major} via winget ({pkg_id})…")

        # 2. Try install (winget handles upgrade automatically if already present)
        try:
            r = subprocess.run(
                ["winget", "install",
                 "--id", pkg_id,
                 "--silent",
                 "--accept-package-agreements",
                 "--accept-source-agreements"],
                capture_output=True, text=True, timeout=300,
                creationflags=_CREATE_NO_WINDOW)
            combined = (r.stdout + r.stderr).lower()
            if r.returncode == 0:
                log_fn(f"winget: {pkg_id} installed ✓")
                return True, f"Temurin {java_major} (winget)"
            # Already installed and up to date (various winget exit codes)
            if any(p in combined for p in (
                    "already installed", "no available upgrade",
                    "no newer package", "up to date")):
                log_fn(f"winget: {pkg_id} already up to date")
                return True, f"Temurin {java_major} already current (winget)"
            log_fn(f"winget exited {r.returncode}: {r.stdout.strip()[:300]}")
            return False, f"winget failed (rc={r.returncode})"
        except subprocess.TimeoutExpired:
            return False, "winget timed out"
        except Exception as e:
            return False, f"winget exception: {e}"

    def _fetch_java_info(self, java_major: int = DEFAULT_JAVA_MAJOR) -> tuple:
        """Fetch the download URL, size, and version for Temurin JDK."""
        arch_map = {"AMD64": "x64", "x86_64": "x64",
                    "ARM64": "aarch64", "aarch64": "aarch64"}
        arch = arch_map.get(platform.machine(), "x64")
        url  = (f"{ADOPTIUM_API_BASE.format(ver=java_major)}"
                f"?architecture={arch}&image_type=jdk&os=windows&vendor=eclipse")
        req  = urllib.request.Request(url, headers={"User-Agent": "GhidraInstaller/1.0"})
        with _urlopen_ssl(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        if not data:
            raise RuntimeError(f"Adoptium API returned no JDK {java_major} for Windows/{arch}")
        pkg = data[0]["binary"]["package"]
        ver = data[0]["version"]["semver"]
        return pkg["link"], pkg["size"], ver

    def _install_java_zip(self, zip_path: Path, java_ver: str,
                          java_major: int = DEFAULT_JAVA_MAJOR):
        """Extract Temurin JDK zip to JDK_INSTALL_DIR.

        When an old JDK is present we upgrade in-place with copytree
        (dirs_exist_ok=True) rather than deleting the directory first.
        This sidesteps Windows directory-level locks held by Defender /
        Search Indexer entirely — the directory itself is never removed,
        only individual files are overwritten.  Any file that is still
        locked at copy time is scheduled for replacement on next reboot
        via MoveFileExW.
        """
        global JDK_INSTALL_DIR  # noqa: PLW0603
        import ctypes

        MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
        MOVEFILE_REPLACE_EXISTING   = 0x00000001

        # ── Extract to a temp staging folder ─────────────────────────────
        tmp = JDK_INSTALL_DIR.parent / "_jdk_tmp"
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)

        self._log(f"Extracting JDK {java_ver}…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            total   = len(members)
            for i, m in enumerate(members):
                if self._cancel:
                    return
                zf.extract(m, tmp)
                self._set_progress((i + 1) / total * 100)
                self._set_detail(f"Extracting JDK {i+1}/{total}")

        roots = [d for d in tmp.iterdir() if d.is_dir()]
        if not roots:
            raise RuntimeError("JDK zip contained no root directory")
        jdk_root = roots[0]

        # ── Install / upgrade ─────────────────────────────────────────────
        if JDK_INSTALL_DIR.exists():
            # Kill java processes from our JDK so jvm.dll handles drop.
            try:
                frag = str(JDK_INSTALL_DIR).replace("'", "''")
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "Get-CimInstance Win32_Process "
                     f"| Where-Object {{ $_.Name -match 'java' -and "
                     f"  $_.ExecutablePath -like '{frag}*' }} "
                     "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=_CREATE_NO_WINDOW)
                time.sleep(1.0)
            except Exception:
                pass

            self._log("Upgrading JDK in-place (no directory removal needed)…")

            # Copy each new file over the old one.  If a file is still
            # locked, schedule it for replacement on the next Windows boot.
            def _copy_or_schedule(src, dst):
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    try:
                        ctypes.windll.kernel32.MoveFileExW(
                            src, dst,
                            MOVEFILE_DELAY_UNTIL_REBOOT | MOVEFILE_REPLACE_EXISTING)
                    except Exception:
                        pass   # best-effort; old file stays for now

            shutil.copytree(str(jdk_root), str(JDK_INSTALL_DIR),
                            dirs_exist_ok=True, copy_function=_copy_or_schedule)

            # Purge stale CDS class-data archives.  When we copy JDK N over
            # JDK N-1 in-place, the old *.jsa files remain.  JDK N rejects
            # them and emits a warning to stderr that Ghidra's LaunchSupport
            # .jar misreads as the java executable path → Windows "cannot
            # find" dialog.  Deleting them lets the JVM regenerate them on
            # first run.
            for _jsa in JDK_INSTALL_DIR.rglob("*.jsa"):
                try:
                    _jsa.unlink()
                except OSError:
                    pass
            self._log("Stale JVM class-data archives cleared.")
        else:
            JDK_INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
            jdk_root.rename(JDK_INSTALL_DIR)

        shutil.rmtree(tmp, ignore_errors=True)
        self._log(f"JDK installed → {JDK_INSTALL_DIR}")

    # ── Release fetching ──────────────────────────────────────────

    @staticmethod
    def _parse_release(data: dict) -> "dict | None":
        tag   = data.get("tag_name", "")
        date  = (data.get("published_at") or "")[:10]
        body  = data.get("body", "")
        asset = next(
            (a for a in data.get("assets", [])
             if a.get("name", "").endswith(".zip")
             and "PUBLIC" in a.get("name", "")),
            None)
        if not asset:
            return None
        sha = ""
        for line in body.splitlines():
            m = re.search(r"[0-9a-f]{64}", line, re.IGNORECASE)
            if m:
                sha = m.group(0)
                break
        ver = tag.replace("Ghidra_", "").replace("_build", "")
        return {
            "version":  ver,
            "date":     date,
            "url":      asset["browser_download_url"],
            "filename": asset["name"],
            "size":     asset.get("size", 0),
            "sha256":   sha,
        }

    def _fetch_releases(self):
        self._log("Fetching available Ghidra releases from GitHub…")
        list_url = (GITHUB_API.replace("/releases/latest", "/releases")
                    + "?per_page=10")
        try:
            req = urllib.request.Request(
                list_url,
                headers={"User-Agent": "GhidraInstaller/1.0",
                         "Accept":     "application/vnd.github+json"})
            with _urlopen_ssl(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode())
        except Exception as e:
            self._log(f"Failed to fetch releases: {e}")
            self.after(0, lambda: self.combo_version.set("Error — check connection"))
            return

        releases = [r for r in (self._parse_release(d) for d in raw)
                    if r is not None]
        if not releases:
            self._log("No usable releases found.")
            return

        self._releases = releases
        labels = [f"{r['version']}   ({r['date']}   {fmt_size(r['size'])})" for r in releases]
        self._log(f"Found {len(releases)} releases: "
                  + ", ".join(r['version'] for r in releases))

        def _update():
            self.combo_version["values"] = labels
            self.combo_version.current(0)
            self._on_version_selected()
        self.after(0, _update)

    def _on_version_selected(self, event=None):
        idx = self.combo_version.current()
        if idx < 0 or idx >= len(self._releases):
            return
        r = self._releases[idx]
        self._download_url = r["url"]
        self._asset_name   = r["filename"]
        self._asset_size   = r["size"]

        self.lbl_date.configure(text=r["date"])
        self.lbl_size.configure(text=fmt_size(r["size"]))
        self.lbl_sha.configure(text=r["sha256"] if r["sha256"] else "n/a")
        self.lbl_natives.configure(
            text="✅ pre-built win_x86_64 binaries included",
            fg="#4CAF50")

        self.btn_install.configure(state="normal", text="Download & Install")
        self.entry_dir.configure(state="normal")

    # ── Install workflow ──────────────────────────────────────────

    def _start_install(self):
        if not self._download_url:
            messagebox.showwarning("Not Ready", "Release info not yet fetched.")
            return
        install_dir = self._install_dir.get().strip()
        if not install_dir:
            messagebox.showwarning("No Directory", "Please select an install directory.")
            return
        self._cancel = False
        self.btn_install.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.entry_dir.configure(state="disabled")
        self._set_progress(0)
        threading.Thread(target=self._worker, args=(install_dir,), daemon=True).start()

    def _cancel_install(self):
        self._cancel = True
        self._log("Cancelling…")

    def _reset_ui(self):
        def _do():
            self.btn_install.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            self.entry_dir.configure(state="normal")
        self.after(0, _do)

    # ── Background worker ─────────────────────────────────────────

    def _worker(self, install_dir: str):
        work_dir = None
        try:
            os.makedirs(install_dir, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="GhidraInstall_"))
            self._log(f"Work dir : {work_dir}")
            self._log(f"Install  : {install_dir}")

            # ── [1/3] Download Ghidra ─────────────────────────────
            tmp_zip = work_dir / self._asset_name
            self._log("\n── [1/3] Download Ghidra ────────────────────")
            self._log(f"URL : {self._download_url}")
            self._set_status("[1/3] Downloading Ghidra…", "blue")
            self._download(self._download_url, tmp_zip)
            if self._cancel:
                self._log("Cancelled.")
                self._reset_ui()
                return

            # ── [2/3] Extract + detect required Java version ──────
            self._log("\n── [2/3] Extract & Java ─────────────────────")
            self._set_status("[2/3] Extracting Ghidra…", "blue")
            tmp_extract = work_dir / "_extract"
            self._extract_zip(tmp_zip, tmp_extract)
            tmp_zip.unlink(missing_ok=True)
            if self._cancel:
                self._log("Cancelled.")
                self._reset_ui()
                return

            ghidra_dirs = sorted(
                [d for d in tmp_extract.iterdir()
                 if d.is_dir() and d.name.startswith("ghidra_")])
            if not ghidra_dirs:
                raise RuntimeError("Could not find ghidra folder inside ZIP")
            ghidra_src = ghidra_dirs[0]
            self._log(f"Found: {ghidra_src.name}")

            # Derive version string
            version_str = (ghidra_src.name.split("_")[1]
                           if "_" in ghidra_src.name else "unknown")

            # Read the Java version Ghidra actually needs
            java_major = self._get_required_java_version(ghidra_src)
            self._log(f"Ghidra requires Java {java_major}+")
            self.after(0, lambda m=java_major: self.lbl_java.configure(
                text=f"Checking for Java {m}+…"))

            # Java check / install
            java_ok, java_label = self._probe_java(java_major)
            if java_ok:
                self._log(f"Java OK: {java_label}")
                self._set_status("[2/3] Java OK — skipping JDK download", "gray")
                self.after(0, lambda lbl=java_label: self.lbl_java.configure(
                    text=lbl, fg="#4CAF50"))
                java_home = self._find_java_home(java_major)
            else:
                self._log(f"Java status: {java_label}")
                java_home = None

                # ── Strategy 1: winget (cleanest; handles upgrade natively) ──
                self._set_status(f"[2/3] Installing Java {java_major} via winget…", "blue")
                winget_ok, winget_msg = self._try_winget_java(java_major, self._log)
                if winget_ok:
                    java_home = self._find_java_home(java_major)
                    if java_home:
                        self._log(f"Java {java_major} installed → {java_home}")
                        self.after(0, lambda m=java_major: self.lbl_java.configure(
                            text=f"Java {m} (winget) ✓", fg="#4CAF50"))
                    else:
                        self._log("winget reported success but JDK not found — "
                                  "falling back to Adoptium zip.")
                        winget_ok = False

                # ── Strategy 2: Adoptium zip (bundled, no admin needed) ──
                if not winget_ok:
                    self._log(f"Fetching Temurin JDK {java_major} info for Windows…")
                    self._set_status(f"[2/3] Fetching Java {java_major} info…", "blue")
                    java_url, java_size, java_ver = self._fetch_java_info(java_major)
                    self._log(f"Temurin JDK {java_ver}  ({fmt_size(java_size)})")

                    tmp_jdk = work_dir / f"temurin-jdk-{java_major}.zip"
                    self._set_status(
                        f"[2/3] Downloading Java {java_major} ({fmt_size(java_size)})…",
                        "blue")
                    self._download(java_url, tmp_jdk, hint_size=java_size)
                    if self._cancel:
                        self._log("Cancelled.")
                        self._reset_ui()
                        return

                    self._set_status(f"[2/3] Installing Java {java_major}…", "blue")
                    self._install_java_zip(tmp_jdk, java_ver, java_major)
                    tmp_jdk.unlink(missing_ok=True)
                    if self._cancel:
                        self._log("Cancelled.")
                        self._reset_ui()
                        return

                    self._log(f"Java {java_ver} installed ✓")
                    self.after(0, lambda v=java_ver: self.lbl_java.configure(
                        text=f"Temurin {v} (bundled) ✓", fg="#4CAF50"))
                    java_home = JDK_INSTALL_DIR

            # ── [3/3] Install Ghidra + shortcuts ──────────────────
            self._log("\n── [3/3] Install Ghidra ─────────────────────")
            ghidra_final = Path(install_dir) / ghidra_src.name
            self._set_status("[3/3] Installing Ghidra…", "blue")

            duplicate_install = ghidra_final.exists()

            if duplicate_install:
                _result = [None]
                _event  = threading.Event()
                self.after(0, lambda: self._ask_reinstall(
                    version_str, str(ghidra_final), _result, _event))
                _event.wait()
                answer = _result[0]

                if answer is None:
                    self._log("Install cancelled.")
                    self._reset_ui()
                    return

                # Both reinstall and repair: kill Ghidra processes then
                # copy new files over the existing folder in-place.
                # This avoids directory-level locks from Defender / Indexer
                # entirely — the folder itself is never removed.
                self._log(f"{'Repairing' if answer is True else 'Reinstalling'} {ghidra_final.name}…")
                self._kill_ghidra(ghidra_final)

                import ctypes as _ct
                _DELAY_REBOOT  = 0x00000004
                _REPLACE_EXIST = 0x00000001

                def _copy_or_schedule_ghidra(src, dst):
                    try:
                        shutil.copy2(src, dst)
                    except OSError:
                        try:
                            _ct.windll.kernel32.MoveFileExW(
                                src, dst, _DELAY_REBOOT | _REPLACE_EXIST)
                        except Exception:
                            pass

                self._log("Upgrading Ghidra in-place…")
                shutil.copytree(str(ghidra_src), str(ghidra_final),
                                dirs_exist_ok=True,
                                copy_function=_copy_or_schedule_ghidra)
                # Source tree is now merged; skip the move below.
                duplicate_install = True   # sentinel: skip move block

            if not duplicate_install:
                self._log(f"Moving to {ghidra_final}")
                shutil.move(str(ghidra_src), str(ghidra_final))

            # Create launcher
            launcher = self._create_launcher(ghidra_final, java_home)
            self._log(f"Launcher: {launcher}")

            shortcut_name = f"Ghidra {version_str}"

            # Desktop shortcut
            if self._shortcut_var.get():
                try:
                    self._create_shortcut(
                        target=launcher,
                        name=shortcut_name,
                        dest=Path.home() / "Desktop" / f"{shortcut_name}.lnk",
                        icon=ghidra_final / "support" / "ghidra.ico",
                        work_dir=ghidra_final)
                    self._log(f"Desktop shortcut created: {shortcut_name}.lnk")
                except Exception as e:
                    self._log(f"Desktop shortcut warning: {e}")

            # Start Menu shortcuts
            if self._startmenu_var.get():
                # Per-user location (AppData\Roaming) — always writable.
                # All-users location (ProgramData) — requires admin; skip silently
                # if we don't have the rights rather than printing a warning.
                start_menu_locations = [
                    Path(os.environ.get("APPDATA", ""))
                    / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                    Path(os.environ.get("ProgramData", r"C:\ProgramData"))
                    / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                ]
                created_paths = []
                for sm in start_menu_locations:
                    shortcut_path = sm / f"{shortcut_name}.lnk"
                    try:
                        sm.mkdir(parents=True, exist_ok=True)
                        self._create_shortcut(
                            target=launcher,
                            name=shortcut_name,
                            dest=shortcut_path,
                            icon=ghidra_final / "support" / "ghidra.ico",
                            work_dir=ghidra_final)
                        created_paths.append(sm)
                        self._log(f"Start Menu shortcut created: {shortcut_path}")
                    except PermissionError:
                        # All-users location needs admin — silently skip
                        pass
                    except Exception as e:
                        self._log(f"Start Menu warning ({sm.name}): {e}")
                self._notify_shell(*created_paths)

            # Shell refresh
            try:
                import ctypes
                ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
            except Exception as e:
                self._log(f"Shell refresh warning: {e}")

            # Save metadata
            sc_desk = (str(Path.home() / "Desktop" / f"{shortcut_name}.lnk")
                       if self._shortcut_var.get() else "")
            sc_menu = (str(Path(os.environ.get("APPDATA", ""))
                           / "Microsoft" / "Windows" / "Start Menu"
                           / "Programs" / f"{shortcut_name}.lnk")
                       if self._startmenu_var.get() else "")
            _append_install({
                "version":            version_str,
                "folder":             str(ghidra_final),
                "source":             ghidra_src.name,
                "date":               time.strftime("%Y-%m-%d"),
                "shortcut_desktop":   sc_desk,
                "shortcut_startmenu": sc_menu,
                "shortcut_name":      shortcut_name,
            }, log=self._log)
            self.after(0, self._load_installed_version)

            self._log(f"\n✓ Done — {ghidra_final}")
            self.after(0, lambda: self._finish(ghidra_final))

        except Exception as exc:
            import traceback
            self._log(f"\n✗ Error: {exc}")
            self._log(traceback.format_exc())
            self._set_status(f"Error: {exc}", "red")
            self._reset_ui()
        finally:
            if work_dir and work_dir.exists():
                self._log("Cleaning up work directory…")
                shutil.rmtree(work_dir, ignore_errors=True)

    # ── Download ──────────────────────────────────────────────────

    def _download(self, url: str, dest: Path, hint_size: int = 0):
        req = urllib.request.Request(
            url, headers={"User-Agent": "GhidraInstaller/1.0"})
        t0  = time.time()
        got = 0
        with _urlopen_ssl(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or
                        hint_size or self._asset_size or 0)
            with open(dest, "wb") as fh:
                while not self._cancel:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    elapsed = time.time() - t0 or 0.001
                    speed   = got / elapsed
                    pct     = (got / total * 100) if total else 0
                    eta     = (f"  ETA {int((total-got)/speed)}s"
                               if speed and total else "")
                    detail  = (f"{fmt_size(got)}"
                               + (f" / {fmt_size(total)}" if total else "")
                               + f"  {fmt_speed(speed)}{eta}")
                    self._set_progress(pct)
                    self._set_detail(detail)
        if not self._cancel:
            self._log(f"Downloaded {fmt_size(got)} in {time.time()-t0:.1f}s")

    # ── Extraction ────────────────────────────────────────────────

    def _extract_zip(self, zip_path: Path, dest: Path):
        """Extract ZIP with per-file progress."""
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            total   = len(members)
            for i, member in enumerate(members):
                if self._cancel:
                    break
                zf.extract(member, dest)
                self._set_progress((i + 1) / total * 100)
                self._set_detail(f"Extracting {i+1} / {total}")
        self._log(f"Extracted {total} entries")

    # ── Launcher + Shortcuts ──────────────────────────────────────

    def _create_launcher(self, ghidra_dir: Path,
                         java_home: "Path | None") -> Path:
        """
        Create two launcher files:
        1. launch_ghidra.bat  — sets JAVA_HOME, calls ghidraRun.bat
        2. launch_ghidra.vbs  — runs the .bat hidden via WScript so
                                NO CMD window appears when double-clicked.
        Returns the .vbs path (used as the shortcut target).
        """
        bat = ghidra_dir / "launch_ghidra.bat"
        java_line = (f'set "JAVA_HOME={java_home}"\r\n'
                     f'set "PATH=%JAVA_HOME%\\bin;%PATH%"\r\n'
                     if java_home else "")
        bat_content = (
            "@echo off\r\n"
            f"{java_line}"
            f'cd /d "{ghidra_dir}"\r\n'
            'call ghidraRun.bat\r\n'
        )
        bat.write_bytes(bat_content.encode("utf-8"))

        vbs = ghidra_dir / "launch_ghidra.vbs"
        vbs_content = (
            'CreateObject("WScript.Shell")'
            f'.Run "cmd /c """"{bat}""""", 0, False\r\n'
        )
        vbs.write_bytes(vbs_content.encode("utf-8"))
        self._log("Launcher created (VBScript + batch)")
        return vbs

    @staticmethod
    def _notify_shell(*paths):
        """Call SHChangeNotify so Windows re-indexes the Start Menu immediately."""
        try:
            import ctypes
            fn = ctypes.windll.shell32.SHChangeNotify
            fn.restype = None
            fn.argtypes = [ctypes.c_long, ctypes.c_uint,
                           ctypes.c_wchar_p, ctypes.c_wchar_p]
            for p in paths:
                fn(0x00001000, 0x0005, str(p), None)
            fn.argtypes = [ctypes.c_long, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_void_p]
            fn(0x08000000, 0, None, None)
        except Exception:
            pass

    def _create_shortcut(self, target: Path, name: str, dest: Path,
                         icon: "Path | None", work_dir: Path):
        """Create a Windows .lnk shortcut using PowerShell."""

        def ps_escape(value):
            return str(value).replace("'", "''")

        suffix  = target.suffix.lower()
        sysroot = Path(os.environ.get("SystemRoot", r"C:\Windows"))

        if suffix in (".vbs", ".js", ".wsf"):
            lnk_target = str(sysroot / "System32" / "wscript.exe")
            lnk_args   = f'"{target}"'
        elif suffix in (".bat", ".cmd"):
            lnk_target = str(sysroot / "System32" / "cmd.exe")
            lnk_args   = f'/c "{target}"'
        else:
            lnk_target = str(target)
            lnk_args   = ""

        ps_lines = [
            "$w = New-Object -ComObject WScript.Shell",
            f"$s = $w.CreateShortcut('{ps_escape(dest)}')",
            f"$s.TargetPath = '{ps_escape(lnk_target)}'",
            f"$s.WorkingDirectory = '{ps_escape(work_dir)}'",
            "$s.WindowStyle = 7",
        ]
        if lnk_args:
            ps_lines.append(f"$s.Arguments = '{ps_escape(lnk_args)}'")
        if icon and icon.exists():
            ps_lines.append(f"$s.IconLocation = '{ps_escape(icon)}'")
        ps_lines.append("$s.Save()")

        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", "\r\n".join(ps_lines)],
            capture_output=True, text=True,
            creationflags=_CREATE_NO_WINDOW)
        if result.returncode != 0:
            raise PermissionError(result.stderr.strip()[:500])

    def _cleanup_ghidra_if_empty(self):
        """Remove all Ghidra leftovers if no installs remain."""
        try:
            ghidra_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Ghidra"
            if not ghidra_root.exists():
                return

            known_folders = {Path(e["folder"]).resolve()
                             for e in _read_installs() if e.get("folder")}

            real_installs = []
            for p in ghidra_root.iterdir():
                if not (p.is_dir() and p.name.lower().startswith("ghidra_")):
                    continue
                if p.name.endswith("_removing"):
                    continue          # tombstone being deleted — ignore
                if p.resolve() not in known_folders:
                    # Orphaned folder (metadata already removed) — delete it
                    self._log(f"Removing orphaned install folder: {p.name}")
                    import tempfile
                    subprocess.run(
                        ["attrib", "-R", "-S", "-H", "/S", "/D", str(p)],
                        capture_output=True, creationflags=_CREATE_NO_WINDOW)
                    subprocess.run(
                        ["takeown", "/f", str(p), "/r", "/d", "y"],
                        capture_output=True, creationflags=_CREATE_NO_WINDOW)
                    _empty = Path(tempfile.mkdtemp(prefix="ghidra_del_"))
                    try:
                        subprocess.run(
                            ["robocopy", str(_empty), str(p),
                             "/MIR", "/MT:32", "/R:0", "/W:0",
                             "/NFL", "/NDL", "/NJH", "/NJS"],
                            capture_output=True,
                            creationflags=_CREATE_NO_WINDOW)
                    finally:
                        try: _empty.rmdir()
                        except OSError: pass
                    try: p.rmdir()
                    except OSError: pass
                else:
                    real_installs.append(p)

            if real_installs:
                self._log(f"Cleanup skipped: {len(real_installs)} install(s) remain")
                return
            self._log("No Ghidra installs remain. Performing cleanup…")
            for sm in [
                Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
            ]:
                try:
                    for lnk in sm.glob("Ghidra*.lnk"):
                        lnk.unlink(missing_ok=True)
                        self._log(f"Deleted shortcut: {lnk.name}")
                except Exception as e:
                    self._log(f"Shortcut cleanup warning: {e}")
            for desktop in [
                Path(os.environ.get("USERPROFILE", "")) / "Desktop",
                Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
            ]:
                try:
                    for lnk in desktop.glob("Ghidra*.lnk"):
                        lnk.unlink(missing_ok=True)
                        self._log(f"Deleted desktop shortcut: {lnk.name}")
                except Exception as e:
                    self._log(f"Desktop cleanup warning: {e}")
            roaming = Path(os.environ.get("APPDATA", "")) / "GhidraInstaller"
            if roaming.exists():
                try:
                    shutil.rmtree(roaming)
                    self._log(f"Removed installer data: {roaming}")
                except Exception as e:
                    self._log(f"Installer cleanup warning: {e}")
            # Only delete the root if no tombstones are still being removed
            # in the background; those hold file locks that cause WinError 5.
            active_tombstones = [
                p for p in ghidra_root.iterdir()
                if p.is_dir() and p.name.endswith("_removing")]
            if active_tombstones:
                self._log("Ghidra root folder will be removed once "
                          "background deletion finishes.")
            else:
                try:
                    shutil.rmtree(str(ghidra_root), ignore_errors=True)
                    if ghidra_root.exists():
                        ghidra_root.rmdir()   # last resort for stubborn empties
                    self._log(f"Removed Ghidra folder: {ghidra_root}")
                except Exception as e:
                    self._log(f"Ghidra folder cleanup warning: {e}")
        except Exception as e:
            self._log(f"Ghidra cleanup failed: {e}")

    # ── Thread-safe reinstall prompt ──────────────────────────────

    def _ask_reinstall(self, version_str: str, folder: str,
                        result: list, event: threading.Event):
        result[0] = messagebox.askyesnocancel(
            "Already Installed",
            f"Ghidra {version_str} is already installed at:\n\n"
            f"  {folder}\n\n"
            "Yes    →  Keep existing files, repair shortcuts only\n"
            "No     →  Delete and do a clean reinstall\n"
            "Cancel →  Abort",
        )
        event.set()

    # ── Finish ────────────────────────────────────────────────────

    def _finish(self, ghidra_dir: Path):
        self._set_status("✓ Installation complete!", "green")
        self._set_progress(100)
        self._set_detail(str(ghidra_dir))
        self.btn_install.configure(state="normal", text="Reinstall")
        self.btn_cancel.configure(state="disabled")
        self.entry_dir.configure(state="normal")
        msg = f"Ghidra has been installed to:\n\n{ghidra_dir}"
        if self._shortcut_var.get():
            msg += "\n\nA Desktop shortcut has been created."
        if self._startmenu_var.get():
            msg += "\nA Start Menu entry has been added."
        if messagebox.askyesno("Installation Complete",
                               msg + "\n\nLaunch Ghidra now?"):
            vbs = ghidra_dir / "launch_ghidra.vbs"
            if vbs.exists():
                subprocess.Popen(["wscript.exe", str(vbs)],
                                 creationflags=_CREATE_NO_WINDOW)
            else:
                subprocess.Popen([str(ghidra_dir / "ghidraRun.bat")],
                                 shell=True, creationflags=_CREATE_NO_WINDOW)

    # ── Manage / Uninstall dialog ─────────────────────────────────

    def _show_uninstall_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("Manage Ghidra Installations")
        dlg.resizable(True, False)
        dlg.transient(self)
        dlg.grab_set()

        # Dark header
        hdr = tk.Frame(dlg, bg="#16213e")
        hdr.pack(fill="x")
        tk.Label(hdr, text="Manage Ghidra Installations",
                 font=("Segoe UI", 14, "bold"),
                 fg="white", bg="#16213e", pady=12).pack()
        tk.Label(hdr,
                 text="Select a version to uninstall or repair its shortcuts.",
                 font=("Segoe UI", 10),
                 fg="#8892b0", bg="#16213e").pack(pady=(0, 10))

        # Treeview
        tv_frame = tk.Frame(dlg, padx=16, pady=10)
        tv_frame.pack(fill="both", expand=True)

        cols = ("version", "date", "folder")
        tree = ttk.Treeview(tv_frame, columns=cols,
                            show="headings", height=8,
                            selectmode="browse")
        tree.heading("version", text="Version")
        tree.heading("date",    text="Installed")
        tree.heading("folder",  text="Location")
        tree.column("version", width=90,  stretch=False, anchor="center")
        tree.column("date",    width=90,  stretch=False, anchor="center")
        tree.column("folder",  width=440, stretch=True)

        vsb = ttk.Scrollbar(tv_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        status_var = tk.StringVar(value="")
        tk.Label(dlg, textvariable=status_var,
                 font=("Segoe UI", 9), anchor="w",
                 fg="gray", padx=16).pack(fill="x")

        def _set_st(msg, col="gray"):
            status_var.set(msg)

        installs = []

        def _refresh():
            nonlocal installs
            installs = _read_installs()
            tree.delete(*tree.get_children())
            for e in installs:
                ver    = e.get("version", "?")
                date   = e.get("date",    "—")
                folder = e.get("folder",  "—")
                exists = Path(folder).is_dir() if folder else False
                tree.insert("", "end",
                            values=(ver, date, folder),
                            tags=("ok" if exists else "missing",))
            tree.tag_configure("missing", foreground="#cc3333")
            n = len(installs)
            _set_st(f"{n} version{'s' if n != 1 else ''} installed"
                    if n else "No versions tracked.")

        _refresh()

        def _selected():
            sel = tree.selection()
            if not sel:
                _set_st("Please select a version first.", "darkorange")
                return None
            idx = tree.index(sel[0])
            return installs[idx] if 0 <= idx < len(installs) else None

        def _uninstall():
            entry = _selected()
            if not entry:
                return
            ver    = entry.get("version", "unknown")
            folder = entry.get("folder", "")
            sc_d   = entry.get("shortcut_desktop", "")
            sc_m   = entry.get("shortcut_startmenu", "")

            parts = [f"Ghidra {ver}"]
            if folder:
                parts.append(f"\nFolder:  {folder}")
            if sc_d:
                parts.append(f"Desktop shortcut: {Path(sc_d).name}")
            if sc_m:
                parts.append(f"Start Menu entry: {Path(sc_m).name}")
            parts.append("\nThis action cannot be undone. Continue?")

            if not messagebox.askyesno(
                    f"Uninstall Ghidra {ver}",
                    "\n".join(parts),
                    icon="warning", parent=dlg):
                return

            # Disable buttons while working
            for w in (btn_uninstall, btn_repair):
                try:
                    w.configure(state="disabled")
                except Exception:
                    pass
            _set_st(f"Removing Ghidra {ver}…", "blue")

            def _do_remove():
                import ctypes
                MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
                folder_path = Path(folder) if folder else None

                # ── Step 1: kill processes so handles drop ────────────────
                # Use taskkill /IM directly — avoids PowerShell startup (~2s)
                # and the CIM process scan we don't need for a simple uninstall.
                _killed = 0
                for _exe in ("ghidraRun.exe", "ghidraRun64.exe",
                             "analyzeHeadless.exe", "java.exe", "javaw.exe",
                             "wscript.exe"):
                    r = subprocess.run(
                        ["taskkill", "/F", "/IM", _exe, "/T"],
                        capture_output=True, creationflags=_CREATE_NO_WINDOW)
                    if r.returncode == 0:
                        _killed += 1
                if _killed:
                    time.sleep(0.8)   # brief settle so handles release

                # ── Step 2: rename folder to a tombstone (near-instant) ───
                # Renaming only touches the directory entry — it succeeds even
                # when individual files inside are locked by Defender/Indexer.
                # The original path is freed immediately; deletion happens in
                # the background so the UI is never blocked.
                tombstone = None
                reboot_needed = False
                if folder_path and folder_path.exists():
                    ts_path = folder_path.parent / (folder_path.name + "_removing")
                    try:
                        folder_path.rename(ts_path)
                        tombstone = ts_path
                    except OSError:
                        # Python rename failed — try PowerShell (handles ACL edge cases)
                        r = subprocess.run(
                            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             f"Rename-Item -LiteralPath '{str(folder_path)}'"
                             f" -NewName '{ts_path.name}' -Force"],
                            capture_output=True, text=True, timeout=15,
                            creationflags=_CREATE_NO_WINDOW)
                        if ts_path.exists():
                            tombstone = ts_path
                        else:
                            tombstone = folder_path   # rename failed; delete in place

                # ── Step 3: remove shortcuts, Roaming config & update UI ──
                # AppData\Roaming\ghidra — user settings/preferences/logs
                _roaming_ghidra = _APPDATA / "ghidra"
                if _roaming_ghidra.exists():
                    shutil.rmtree(str(_roaming_ghidra), ignore_errors=True)

                for sc in filter(None, (sc_d, sc_m)):
                    p = Path(sc)
                    if p.exists():
                        try:
                            p.unlink()
                        except Exception:
                            pass
                for lnk_dir in (
                    Path.home() / "Desktop",
                    _APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs",
                ):
                    for lnk in lnk_dir.glob(f"Ghidra {ver}*.lnk"):
                        try:
                            lnk.unlink()
                        except Exception:
                            pass

                def _update_ui():
                    sm_programs = _APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                    self._notify_shell(sm_programs)
                    _write_installs([x for x in installs
                                     if x.get("folder") != entry.get("folder")])
                    self._cleanup_ghidra_if_empty()
                    _refresh()
                    self._load_installed_version()
                    for w in (btn_uninstall, btn_repair):
                        try:
                            w.configure(state="normal")
                        except Exception:
                            pass
                    if reboot_needed:
                        _set_st(f"Ghidra {ver} removed — reboot to finish cleanup. ✓",
                                "darkorange")
                    else:
                        _set_st(f"Ghidra {ver} fully removed. ✓", "#4CAF50")

                dlg.after(0, _update_ui)   # UI updates instantly after rename

                # ── Step 4: delete tombstone in background (no UI timeout) ─
                if tombstone and tombstone.exists():
                    import tempfile

                    def _bg_log(msg):
                        try:
                            dlg.after(0, lambda m=msg: self._log(m))
                        except Exception:
                            pass

                    _bg_log(f"Background deletion started: {tombstone.name}")

                    # 4a: Move tombstone into Windows Temp (same NTFS volume
                    # = instant rename, no copy).  Temp is outside the user's
                    # Ghidra folder so it won't show as a leftover install, and
                    # Defender is less aggressive there.  If the rename fails we
                    # just delete in place.
                    import tempfile as _tf
                    _tmp_target = Path(_tf.gettempdir()) / tombstone.name
                    try:
                        tombstone.rename(_tmp_target)
                        tombstone = _tmp_target
                        _bg_log(f"Moved to Temp for cleanup: {tombstone.name}")
                    except OSError:
                        _bg_log("Could not move to Temp — deleting in place")

                    # 4b: strip read-only/system/hidden so deletion tools
                    # can touch every file.
                    subprocess.run(
                        ["attrib", "-R", "-S", "-H", "/S", "/D",
                         str(tombstone)],
                        capture_output=True,
                        creationflags=_CREATE_NO_WINDOW)

                    # 4c: delete — let rd run without a timeout; Defender will
                    # scan files but it will finish.
                    _deleted = False
                    r = subprocess.run(
                        ["cmd", "/c", "rd", "/s", "/q", str(tombstone)],
                        capture_output=True, text=True,
                        creationflags=_CREATE_NO_WINDOW)
                    if not tombstone.exists():
                        _deleted = True
                        _bg_log("Folder deleted ✓")
                    else:
                        # rd left something — PowerShell inline cleanup
                        _bg_log(f"rd incomplete (rc={r.returncode}), "
                                "trying PowerShell…")
                        _ps = (
                            f"Get-ChildItem -LiteralPath '{tombstone}' "
                            f"-Recurse -Force | ForEach-Object {{"
                            f" try {{ $_.Attributes = 'Normal' }} catch {{}} }}; "
                            f"Remove-Item -LiteralPath '{tombstone}' "
                            f"-Recurse -Force -ErrorAction SilentlyContinue"
                        )
                        subprocess.run(
                            ["powershell", "-NoProfile", "-NonInteractive",
                             "-Command", _ps],
                            capture_output=True,
                            creationflags=_CREATE_NO_WINDOW)
                        if not tombstone.exists():
                            _deleted = True
                            _bg_log("PowerShell cleanup succeeded ✓")
                        else:
                            # Still locked — schedule every remaining file
                            # for deletion on next reboot via MoveFileExW.
                            _bg_log("Files still locked — scheduling reboot "
                                    "deletion…")
                            reboot_needed = True
                            for _root, _dirs, _files in os.walk(
                                    str(tombstone), topdown=False):
                                for _f in _files:
                                    _fp = os.path.join(_root, _f)
                                    try:
                                        os.unlink(_fp)
                                    except OSError:
                                        ctypes.windll.kernel32.MoveFileExW(
                                            _fp, None,
                                            MOVEFILE_DELAY_UNTIL_REBOOT)
                                for _d in _dirs:
                                    try:
                                        os.rmdir(os.path.join(_root, _d))
                                    except OSError:
                                        pass
                            try:
                                os.rmdir(str(tombstone))
                            except OSError:
                                ctypes.windll.kernel32.MoveFileExW(
                                    str(tombstone), None,
                                    MOVEFILE_DELAY_UNTIL_REBOOT)
                            _bg_log("Reboot scheduled ✓")

                    # Clean up the now-empty parent Ghidra folder
                    if _deleted:
                        try:
                            _parent = tombstone.parent
                            _left = [p for p in _parent.iterdir()
                                     if p.is_dir()
                                     and p.name.lower().startswith("ghidra_")]
                            if not _left:
                                shutil.rmtree(str(_parent), ignore_errors=True)
                                _bg_log("Ghidra root folder removed ✓")
                        except Exception:
                            pass

            threading.Thread(target=_do_remove, daemon=True).start()

        def _repair():
            entry = _selected()
            if not entry:
                return
            ver     = entry.get("version", "unknown")
            folder  = Path(entry.get("folder", ""))
            sc_name = entry.get("shortcut_name", entry.get("version", f"Ghidra {ver}"))
            if not sc_name.startswith("Ghidra "):
                sc_name = f"Ghidra {sc_name}"

            launcher = folder / "launch_ghidra.vbs"
            icon     = folder / "support" / "ghidra.ico"

            if not folder.is_dir():
                _set_st(f"Folder not found: {folder}", "red")
                messagebox.showerror(
                    "Folder Missing",
                    f"Cannot repair — installation folder not found:\n\n"
                    f"{folder}\n\nConsider uninstalling this entry.",
                    parent=dlg)
                return

            if not launcher.exists():
                _set_st("Launcher (launch_ghidra.vbs) missing — try reinstalling.", "red")
                return

            errors = []
            sc_d = str(Path.home() / "Desktop" / f"{sc_name}.lnk")
            try:
                self._create_shortcut(
                    target=launcher, name=sc_name,
                    dest=Path(sc_d), icon=icon, work_dir=folder)
                entry["shortcut_desktop"] = sc_d
            except Exception as exc:
                errors.append(str(exc))

            sm_dir = _APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            sm_dir.mkdir(parents=True, exist_ok=True)
            sc_m = str(sm_dir / f"{sc_name}.lnk")
            try:
                self._create_shortcut(
                    target=launcher, name=sc_name,
                    dest=Path(sc_m), icon=icon, work_dir=folder)
                entry["shortcut_startmenu"] = sc_m
            except Exception as exc:
                errors.append(str(exc))

            entry["shortcut_name"] = sc_name
            _append_install(entry)
            self._notify_shell(sm_dir)
            _refresh()

            if errors:
                _set_st(f"Repair of {sc_name} completed with warnings.", "darkorange")
            else:
                _set_st(f"Shortcuts for {sc_name} recreated. ✓", "#4CAF50")

        bf = tk.Frame(dlg, padx=16, pady=12)
        bf.pack(fill="x")
        ttk.Button(bf, text="Close",
                   command=dlg.destroy).pack(side="left")
        btn_repair = ttk.Button(bf, text="Repair Shortcuts", command=_repair)
        btn_repair.pack(side="right", padx=(8, 0))
        btn_uninstall = ttk.Button(bf, text="Uninstall Selected", command=_uninstall)
        btn_uninstall.pack(side="right")

        dlg.update_idletasks()
        dw = max(680, dlg.winfo_reqwidth())
        dh = dlg.winfo_reqheight()
        mx = self.winfo_x() + (self.winfo_width()  - dw) // 2
        my = self.winfo_y() + (self.winfo_height() - dh) // 2
        dlg.geometry(f"{dw}x{dh}+{mx}+{my}")
        dlg.minsize(680, dh)


# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = GhidraInstallerWindows()
    app.mainloop()
