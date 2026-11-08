#!/usr/bin/env python3
"""
Ghidra Installer for macOS
Downloads the latest Ghidra release from GitHub, extracts it,
and wraps it in a native macOS .app bundle.

Requirements: Python 3.8+  (no third-party packages needed)
"""

import glob as _glob
import json
import os
import platform
import plistlib
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

# ──────────────────────────────────────────────────────────────────
#  Constants
# ──────────────────────────────────────────────────────────────────
GITHUB_API       = "https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/latest"
# Adoptium API — {ver} is replaced at runtime with the required JDK major
ADOPTIUM_API_BASE = "https://api.adoptium.net/v3/assets/latest/{ver}/hotspot"
APP_NAME         = "Ghidra.app"
CHUNK_SIZE       = 256 * 1024          # 256 KB per read chunk
DEFAULT_DIR      = "/Applications"
INFO_DIR         = Path.home() / "Library" / "Application Support" / "GhidraInstaller"
INFO_FILE        = INFO_DIR / "install_info.json"
JDK_INSTALL_BASE = Path.home() / "Library" / "Java" / "JavaVirtualMachines"
DEFAULT_JAVA_MAJOR = 25                # floor for Ghidra 12.2+; overridden at runtime by LaunchSupport.jar


def _jdk_bundle_name(major: int) -> str:
    """Return the standard macOS bundle folder name for a Temurin JDK major."""
    return f"temurin-{major}.jdk"


# ──────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────

def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_speed(bps: float) -> str:
    return f"{fmt_size(bps)}/s"


def create_ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context that works inside PyInstaller bundles and on macOS
    machines where Python's bundled certs are missing.

    Search order:
      1. certifi  (best — includes Mozilla CA bundle)
      2. Common system CA paths (macOS / Linux)
      3. ssl.create_default_context()  (works on Python 3.10+ / macOS 10.15+)
      4. Unverified context  (last resort — still downloads correctly)
    """
    # 1. certifi
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass

    # 2. System CA bundles
    ca_candidates = [
        "/etc/ssl/cert.pem",                           # macOS
        "/usr/local/etc/openssl/cert.pem",             # Homebrew Intel
        "/opt/homebrew/etc/openssl@3/cert.pem",        # Homebrew Apple Silicon
        "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew ca-certificates
        "/etc/ssl/certs/ca-certificates.crt",          # Debian/Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",            # Fedora/RHEL
    ]
    for path in ca_candidates:
        if os.path.exists(path):
            try:
                return ssl.create_default_context(cafile=path)
            except Exception:
                continue

    # 3. Default context (may load system keychain on newer macOS)
    try:
        return ssl.create_default_context()
    except Exception:
        pass

    # 4. Unverified — still encrypts the connection, just skips cert chain check
    ctx = ssl._create_unverified_context()  # noqa: SLF001
    return ctx


_DEBUG = bool(os.environ.get("GHIDRA_DEBUG"))


def _dbg(msg: str):
    """Print a startup trace line when run with GHIDRA_DEBUG=1."""
    if _DEBUG:
        import sys
        print(f"[debug {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def is_dark_mode() -> bool:
    """Return True when macOS is currently in Dark Mode."""
    try:
        r = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True, text=True, timeout=2)
        return r.stdout.strip().lower() == "dark"
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────
#  Native macOS appearance
# ──────────────────────────────────────────────────────────────────

# Legacy colour names used around the code → semantic roles
_ROLE_ALIASES = {
    "gray": "secondary", "grey": "secondary", "secondary": "secondary",
    "blue": "accent", "accent": "accent",
    "green": "green", "#4caf50": "green",
    "orange": "orange", "darkorange": "orange",
    "red": "red", "#cc3333": "red",
    "black": "primary", "white": "primary", "primary": "primary",
}


class _Theme:
    """Adaptive macOS colours and fonts.

    Prefers Tk's native macOS system colours (they follow Light/Dark mode
    and the user's accent colour automatically).  Where a Tk build doesn't
    know a system colour, falls back to Apple's published light/dark values.
    Re-applies everything when the system appearance changes.
    """

    # role: (Tk system colour names to try, light hex, dark hex)
    _SPEC = {
        "primary":   (("systemLabelColor", "systemTextColor"),       "#1D1D1F", "#E5E5E7"),
        "secondary": (("systemSecondaryLabelColor",),               "#6E6E73", "#98989D"),
        "accent":    (("systemControlAccentColor", "systemLinkColor"), "#0068DA", "#0A84FF"),
        "textbg":    (("systemTextBackgroundColor",),               "#FFFFFF", "#1E1E1E"),
        "text":      (("systemTextColor",),                         "#1D1D1F", "#E5E5E7"),
        # No Tk system names for these — Apple's accessible system colours
        "green":     ((), "#248A3D", "#32D74B"),
        "orange":    ((), "#C93400", "#FF9F0A"),
        "red":       ((), "#D70015", "#FF453A"),
    }

    def __init__(self, root: tk.Misc):
        self.root = root
        self._painted: dict = {}          # widget -> role
        self._callbacks: list = []
        self.dark = self._detect_dark()
        self._resolve()
        self._fonts()
        # Tk 8.6.10+ fires these when the user flips Light/Dark
        for ev in ("<<LightAqua>>", "<<DarkAqua>>"):
            try:
                root.bind(ev, lambda e: self.refresh(), add="+")
            except tk.TclError:
                pass
        root.after(3000, self._poll)

    # ── detection ──
    def _detect_dark(self) -> bool:
        try:
            return bool(int(self.root.tk.call(
                "tk::unsupported::MacWindowStyle", "isdark", self.root)))
        except Exception:
            return is_dark_mode()

    def _poll(self):
        # Fallback for Tk builds that don't send <<DarkAqua>>
        try:
            if self._detect_dark() != self.dark:
                self.refresh()
            self.root.after(3000, self._poll)
        except Exception:
            pass

    # ── colours ──
    def _resolve(self):
        self._col = {}
        for role, (names, light, dark) in self._SPEC.items():
            chosen = dark if self.dark else light
            for n in names:
                try:
                    self.root.winfo_rgb(n)
                    chosen = n
                    break
                except tk.TclError:
                    continue
            self._col[role] = chosen

    def color(self, role: str) -> str:
        return self._col.get(_ROLE_ALIASES.get(role.lower(), role),
                             self._col["primary"])

    def paint(self, widget, role: str):
        role = _ROLE_ALIASES.get(str(role).lower(), "primary")
        self._painted[widget] = role
        ttk.Label.configure(widget, foreground=self._col[role])

    def on_change(self, fn):
        self._callbacks.append(fn)

    def refresh(self):
        self.dark = self._detect_dark()
        self._resolve()
        for w, role in list(self._painted.items()):
            try:
                if w.winfo_exists():
                    ttk.Label.configure(w, foreground=self._col[role])
                else:
                    self._painted.pop(w, None)
            except tk.TclError:
                self._painted.pop(w, None)
        for fn in list(self._callbacks):
            try:
                fn()
            except Exception:
                pass

    # ── brand banner (navy, same in Light and Dark mode) ──
    BANNER_BG    = "#16213E"
    BANNER_TITLE = "#FFFFFF"
    BANNER_SUB   = "#A8B2D1"

    def banner(self, parent, title: str, subtitle: str,
               title_size: int = 20) -> tk.Frame:
        """Full-width navy header with a white title and a muted subtitle."""
        bar = tk.Frame(parent, bg=self.BANNER_BG, highlightthickness=0, bd=0)
        tk.Label(bar, text=title, bg=self.BANNER_BG, fg=self.BANNER_TITLE,
                 font=(self.f_title[0], title_size, "bold")
                 ).pack(pady=(14, 0))
        tk.Label(bar, text=subtitle, bg=self.BANNER_BG, fg=self.BANNER_SUB,
                 font=self.f_subtitle).pack(pady=(2, 14))
        return bar

    # ── fonts ──
    def _fonts(self):
        fams = set(tkfont.families(self.root))
        base = tkfont.nametofont("TkDefaultFont").actual().get("family", "Helvetica")
        ui   = "SF Pro Text"    if "SF Pro Text"    in fams else base
        disp = "SF Pro Display" if "SF Pro Display" in fams else ui
        mono = next((f for f in ("SF Mono", "Menlo", "Monaco") if f in fams), "Courier")
        self.f_title    = (disp, 22, "bold")
        self.f_subtitle = (ui, 13)
        self.f_body     = (ui, 13)
        self.f_bodybold = (ui, 13, "bold")
        self.f_small    = (ui, 11)
        self.f_mono     = (mono, 11)
        self.f_monosm   = (mono, 11)


class _SemLabel(ttk.Label):
    """ttk.Label that understands legacy fg="blue"/"gray"/… and maps it to
    an adaptive macOS colour, so existing call sites keep working."""
    theme: "_Theme | None" = None

    def __init__(self, master=None, role: str = "primary", **kw):
        kw.pop("fg", None)
        super().__init__(master, **kw)
        if _SemLabel.theme:
            _SemLabel.theme.paint(self, role)

    def configure(self, cnf=None, **kw):
        fg = kw.pop("fg", None)
        if fg is not None and _SemLabel.theme:
            _SemLabel.theme.paint(self, fg)
        if cnf or kw:
            return super().configure(cnf, **kw)
        return None

    config = configure


# ──────────────────────────────────────────────────────────────────
#  Multi-version install tracking helpers
# ──────────────────────────────────────────────────────────────────

def _read_installs() -> list:
    """Read the list of tracked Ghidra installs from INFO_FILE."""
    try:
        if INFO_FILE.exists():
            data = json.loads(INFO_FILE.read_text())
            # Migrate old single-dict format to list
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _write_installs(installs: list):
    INFO_DIR.mkdir(parents=True, exist_ok=True)
    INFO_FILE.write_text(json.dumps(installs, indent=2))


def _append_install(entry: dict, log=None):
    """Add or update a tracked install entry (matched by app_path)."""
    installs = _read_installs()
    duplicate = next(
        (i for i in installs if i.get("app_path") == entry.get("app_path")), None)
    if duplicate:
        installs.remove(duplicate)
    installs.append(entry)
    _write_installs(installs)
    if log:
        log(f"Saved install metadata: {entry.get('version')}")


# ──────────────────────────────────────────────────────────────────
#  Main application
# ──────────────────────────────────────────────────────────────────

class GhidraInstaller(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Ghidra Installer")
        _dbg(f"Tk {self.tk.call('info', 'patchlevel')}  "
             f"windowing={self.tk.call('tk', 'windowingsystem')}  "
             f"Python {platform.python_version()}  macOS {platform.mac_ver()[0]}")

        # State
        self._download_url: str  = ""
        self._asset_name:   str  = ""
        self._asset_size:   int  = 0
        self._cancel:       bool = False
        self._install_dir   = tk.StringVar(value=DEFAULT_DIR)
        self._releases:     list = []          # all fetched releases
        self._version_var   = tk.StringVar()   # combobox selection

        try:
            _dbg("building UI…")
            self._build_ui()
            _dbg("UI built")
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print(tb)
            messagebox.showerror("Ghidra Installer — UI error", tb)
            raise

        # Let Tk size the window to its content (never force WxH — on some
        # Tk builds reqwidth isn't ready yet and the window locks tiny),
        # then centre it once the real size is known.
        self._centre_tries = 0
        self.after(0, self._centre_window)

        # Quit paths: red close button, ⌘Q / app menu Quit, Dock › Quit
        self._closing = False
        self.protocol("WM_DELETE_WINDOW", self._quit)
        try:
            self.createcommand("::tk::mac::Quit", self._quit)
        except Exception:
            pass
        self.bind_all("<Command-q>", lambda e: self._quit())
        self.bind_all("<Command-w>", lambda e: self._quit())

        # Bring window to front (needed when launched from a .app bundle)
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()

        # Kick off background checks
        threading.Thread(target=self._fetch_releases, daemon=True).start()
        threading.Thread(target=self._check_java,     daemon=True).start()
        self._load_installed_version()
        try:
            self._log(f"Python {platform.python_version()} · "
                      f"Tk {self.tk.call('info', 'patchlevel')} · "
                      f"macOS {platform.mac_ver()[0] or '?'} ({platform.machine()})")
        except Exception:
            pass

    # ── UI construction ───────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("aqua")          # native macOS widgets

        _dbg("theme…")
        T = self.theme = _Theme(self)
        _dbg(f"theme ok: dark={T.dark} fonts={T.f_body} "
             f"primary={T.color('primary')} accent={T.color('accent')}")
        _SemLabel.theme = T

        style.configure("Key.TLabel",   font=T.f_body)
        style.configure("Value.TLabel", font=T.f_body)
        style.configure("TLabelframe.Label", font=T.f_small)

        # ── Header banner ───────────────────────────────────────
        T.banner(self, "Ghidra Installer",
                 "NSA Software Reverse Engineering Framework").pack(fill="x")

        outer = ttk.Frame(self, padding=(20, 10, 20, 12))
        outer.pack(fill="both", expand=True)

        # ── Release group ───────────────────────────────────────
        card = ttk.Labelframe(outer, text="Release", padding=(16, 6, 16, 8))
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)
        self._row_i = 0

        def key(parent, text):
            lbl = _SemLabel(parent, text=text, role="secondary",
                            font=T.f_body, anchor="e")
            lbl.grid(row=self._row_i, column=0, sticky="e",
                     padx=(0, 12), pady=1)
            return lbl

        key(card, "Version:")
        self.combo_version = ttk.Combobox(
            card, textvariable=self._version_var,
            font=T.f_body, state="readonly", width=34)
        self.combo_version.grid(row=self._row_i, column=1, sticky="w", pady=(1, 3))
        self.combo_version.bind("<<ComboboxSelected>>", self._on_version_selected)
        self.combo_version.set("Fetching releases…")
        self._row_i += 1

        def info_row(parent, label, font=None, role="primary"):
            key(parent, label)
            lbl = _SemLabel(parent, text="—", role=role,
                            font=font or T.f_body, anchor="w")
            lbl.grid(row=self._row_i, column=1, sticky="w", pady=1)
            self._row_i += 1
            return lbl

        self.lbl_installed = info_row(card, "Installed:")
        self.lbl_date      = info_row(card, "Released:")
        self.lbl_size      = info_row(card, "Download size:")
        self.lbl_sha       = info_row(card, "SHA-256:", font=T.f_monosm, role="secondary")
        self.lbl_java      = info_row(card, "Java:")
        self.lbl_natives   = info_row(card, "Native build:")

        # ── Install location group ──────────────────────────────
        loc = ttk.Labelframe(outer, text="Install Location", padding=(16, 6, 16, 8))
        loc.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(loc)
        row.pack(fill="x")
        self.entry_dir = ttk.Entry(row, textvariable=self._install_dir,
                                   font=T.f_body)
        self.entry_dir.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Choose…",
                   command=self._browse).pack(side="left", padx=(8, 0))
        _SemLabel(loc, role="secondary", font=T.f_small,
                  text="A versioned app such as “Ghidra 12.2.app” is created in "
                       "this folder.").pack(anchor="w", pady=(4, 0))

        # ── Progress ────────────────────────────────────────────
        pf = ttk.Frame(outer)
        pf.pack(fill="x", pady=(10, 0))
        self.lbl_status = _SemLabel(pf, text="Ready to install.",
                                    role="secondary", font=T.f_body, anchor="w")
        self.lbl_status.pack(fill="x")
        self.progress = ttk.Progressbar(pf, mode="determinate", length=460)
        self.progress.pack(fill="x", pady=(4, 2))
        self.lbl_detail = _SemLabel(pf, text=" ", role="secondary",
                                    font=T.f_small, anchor="w")
        self.lbl_detail.pack(fill="x")

        # ── Buttons (primary action rightmost, per macOS HIG) ───
        bf = ttk.Frame(outer)
        bf.pack(fill="x", pady=(8, 10))
        ttk.Button(bf, text="Quit",
                   command=self._quit).pack(side="left")
        ttk.Button(bf, text="Manage Installs…",
                   command=self._show_uninstall_dialog).pack(side="left", padx=(8, 0))
        self.btn_install = ttk.Button(bf, text="Download & Install",
                                      default="active",
                                      command=self._start_install, state="disabled")
        self.btn_install.pack(side="right")
        self.btn_cancel = ttk.Button(bf, text="Cancel",
                                     command=self._cancel_install, state="disabled")
        self.btn_cancel.pack(side="right", padx=(0, 8))

        # ── Log ─────────────────────────────────────────────────
        lf = ttk.Labelframe(outer, text="Activity", padding=(2, 2, 2, 2))
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=6, font=T.f_mono,
                           wrap="word", state="disabled",
                           relief="flat", borderwidth=0, highlightthickness=0,
                           padx=10, pady=8, spacing1=1, spacing3=1)
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        def _paint_log():
            self.log.configure(bg=T.color("textbg"), fg=T.color("text"),
                               insertbackground=T.color("text"),
                               selectbackground=T.color("accent"))
        _paint_log()
        T.on_change(_paint_log)

    # ── Quitting ──────────────────────────────────────────────────

    def _install_running(self) -> bool:
        try:
            return self.btn_cancel.instate(["!disabled"])
        except Exception:
            return False

    @staticmethod
    def _kill_child_processes():
        """Terminate every process this app started (bsdtar, gradlew, java,
        curl…) so nothing is left running after quit."""
        def _descendants(pid: int) -> list:
            try:
                r = subprocess.run(["pgrep", "-P", str(pid)],
                                   capture_output=True, text=True, timeout=3)
                kids = [int(x) for x in r.stdout.split() if x.isdigit()]
            except Exception:
                return []
            out = []
            for k in kids:
                out += _descendants(k) + [k]      # deepest first
            return out

        import signal
        procs = _descendants(os.getpid())
        for sig in (signal.SIGTERM, signal.SIGKILL):
            alive = []
            for pid in procs:
                try:
                    os.kill(pid, sig)
                    alive.append(pid)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if not alive:
                break
            time.sleep(0.4)
            procs = alive

    def _quit(self, *_):
        """Quit cleanly from any path, even mid-install.

        Background threads can be blocked in a download or a subprocess and
        keep calling into Tk; with Tk 9 that deadlocks a normal destroy().
        So: confirm if busy, stop children, tear down Tk, then hard-exit the
        interpreter so no thread can hold the process open.
        """
        if getattr(self, "_closing", False):
            return
        if self._install_running():
            if not messagebox.askyesno(
                    "Quit Ghidra Installer?",
                    "An installation is still in progress.\n\n"
                    "If you quit now it will be stopped and the partial "
                    "download removed.",
                    icon="warning", default="no", parent=self):
                return
        self._closing = True
        self._cancel  = True
        self._kill_child_processes()
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def _centre_window(self):
        """Position (not size) the window in the centre of the screen."""
        self.update_idletasks()
        w = max(self.winfo_reqwidth(),  self.winfo_width())
        h = max(self.winfo_reqheight(), self.winfo_height())
        _dbg(f"centre try {self._centre_tries}: req={self.winfo_reqwidth()}x"
             f"{self.winfo_reqheight()} actual={self.winfo_width()}x"
             f"{self.winfo_height()} mapped={self.winfo_ismapped()}")
        if (w < 400 or h < 400) and self._centre_tries < 20:
            self._centre_tries += 1          # layout not settled yet
            self.after(50, self._centre_window)
            return
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        # Usable height: screen minus menu bar (~38), Dock (~90) and the
        # window's title bar (~28).  On small/VM screens the window is
        # capped to this and the Activity log (packed last, expand=True)
        # gives up the space — so nothing is ever clipped off-screen.
        avail = sh - 156
        try:
            mh = int(self.maxsize()[1])
            if 300 < mh < avail:
                avail = mh - 28
        except Exception:
            pass
        if w < 400 or h < 400:
            # Layout never reported a sane size — position only, never lock
            self.geometry(f"+{max(0, (sw - w) // 2)}+60")
            return
        h = min(h, max(480, avail))
        x = max(0, (sw - w) // 2)
        y = max(38, min((sh - h) // 3, sh - h - 118))
        self.geometry(f"{w}x{h}+{x}+{y}")
        # Width fixed at the layout width; height can grow to show more log
        self.minsize(w, min(h, 520))
        self.resizable(False, True)
        _dbg(f"window placed: {self.geometry()}")
        if _DEBUG:
            for c in self.winfo_children():
                _dbg(f"  child {c.winfo_class()} req={c.winfo_reqwidth()}x"
                     f"{c.winfo_reqheight()} manager={c.winfo_manager() or 'NONE'}")

    def report_callback_exception(self, exc, val, tb):
        """Show Tk callback errors in the Activity log instead of losing them."""
        import traceback
        msg = "".join(traceback.format_exception(exc, val, tb))
        print(msg)
        try:
            self._log("✗ Internal error:\n" + msg)
        except Exception:
            pass

    # ── Logging / status helpers ──────────────────────────────────

    def _log(self, msg: str):
        if getattr(self, "_closing", False):
            return
        def _do():
            self.log.configure(state="normal")
            self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _do)

    def _set_status(self, msg: str, color: str = "gray"):
        if getattr(self, "_closing", False):
            return
        self.after(0, lambda: self.lbl_status.configure(text=msg, fg=color))

    def _set_detail(self, msg: str):
        if getattr(self, "_closing", False):
            return
        self.after(0, lambda: self.lbl_detail.configure(text=msg))

    def _set_progress(self, pct: float):
        if getattr(self, "_closing", False):
            return
        self.after(0, lambda: self.progress.configure(value=pct))

    # ── Browse directory ──────────────────────────────────────────

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self._install_dir.get(),
                                    title="Choose Install Directory")
        if d:
            self._install_dir.set(d)

    # ── Detect installed version ──────────────────────────────────

    def _load_installed_version(self):
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

    # ── Java check (UI startup probe) ────────────────────────────

    def _check_java(self):
        """Background startup check — updates the Java status label."""
        ok, label = self._probe_java()
        if ok:
            self.after(0, lambda: self.lbl_java.configure(
                text=label, fg="green"))
        else:
            self.after(0, lambda: self.lbl_java.configure(
                text=label + " — will be installed automatically", fg="orange"))

    @staticmethod
    def _java_homes() -> "list[Path]":
        """Return every JDK home on this Mac, in priority order, without
        duplicates.  Never touches the /usr/bin/java stub directly, because
        on a Mac with no JDK that stub pops up a system "Install Java" dialog.
        """
        homes: list[Path] = []

        # 1. JAVA_HOME environment variable
        env_home = os.environ.get("JAVA_HOME", "")
        if env_home:
            homes.append(Path(env_home))

        # 2. User JVM bundles (where this installer puts Temurin)
        # 3. System JVM bundles (pkg installers, brew --cask temurin)
        for base in (JDK_INSTALL_BASE, Path("/Library/Java/JavaVirtualMachines")):
            if base.exists():
                # Highest version first so we prefer the newest JDK
                for b in sorted(base.glob("*.jdk"), reverse=True):
                    homes.append(b / "Contents" / "Home")

        # 4. Homebrew openjdk formulae (Apple Silicon + Intel)
        for pattern in ("/opt/homebrew/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
                        "/usr/local/opt/openjdk*/libexec/openjdk.jdk/Contents/Home",
                        "/opt/homebrew/opt/openjdk*",
                        "/usr/local/opt/openjdk*"):
            homes.extend(Path(p) for p in sorted(_glob.glob(pattern), reverse=True))

        # 5. SDKMAN
        sdkman = Path.home() / ".sdkman" / "candidates" / "java"
        if sdkman.exists():
            homes.extend(p for p in sorted(sdkman.iterdir(), reverse=True)
                         if p.is_dir() and p.name != "current")

        # 6. /usr/libexec/java_home -V — the macOS JVM registry
        try:
            r = subprocess.run(["/usr/libexec/java_home", "-V"],
                               capture_output=True, text=True, timeout=5)
            for line in (r.stderr + r.stdout).splitlines():
                m = re.search(r"(/\S.*?/Contents/Home)\s*$", line.strip())
                if m:
                    homes.append(Path(m.group(1)))
        except Exception:
            pass

        seen: set[str] = set()
        out: list[Path] = []
        for h in homes:
            try:
                key = str(h.resolve())
            except Exception:
                key = str(h)
            if key in seen:
                continue
            seen.add(key)
            if (h / "bin" / "java").exists():
                out.append(h)
        return out

    @staticmethod
    def _java_major_of(java_bin: "Path | str") -> int:
        """Return the major version of a java binary, or 0 on failure."""
        try:
            r = subprocess.run([str(java_bin), "-version"],
                               capture_output=True, text=True, timeout=5)
            m = re.search(r'version "(\d+)(?:\.(\d+))?', r.stderr + r.stdout)
            if not m:
                return 0
            major = int(m.group(1))
            # Legacy "1.8.0" style → 8
            if major == 1 and m.group(2):
                major = int(m.group(2))
            return major
        except Exception:
            return 0

    @classmethod
    def _probe_java(cls, min_major: int = DEFAULT_JAVA_MAJOR) -> tuple:
        """Scan every JVM on the machine and return (ok, label)."""
        best_old = 0
        for home in cls._java_homes():
            major = cls._java_major_of(home / "bin" / "java")
            if not major:
                continue
            src = " (bundled)" if str(JDK_INSTALL_BASE) in str(home) else ""
            if major >= min_major:
                return True, f"Java {major}{src} found ✓"
            best_old = max(best_old, major)
        if best_old:
            return False, f"Java {best_old} found (need ≥{min_major})"
        return False, "Not found"

    @classmethod
    def _find_java_home(cls, min_major: int = DEFAULT_JAVA_MAJOR) -> "Path | None":
        """Return the JDK home of the newest JVM that meets min_major."""
        best: "tuple[int, Path] | None" = None
        for home in cls._java_homes():
            major = cls._java_major_of(home / "bin" / "java")
            if major >= min_major and (best is None or major > best[0]):
                best = (major, home)
        return best[1] if best else None

    # ── Detect the Java version Ghidra requires ───────────────────

    @classmethod
    def _get_required_java_version(cls, ghidra_src: Path) -> int:
        """Detect the minimum Java version Ghidra requires — fully automatic.

        Strategy (stops at first confident answer):
          1. Run Ghidra's own LaunchSupport.jar with whatever Java is available
             and parse its error message — the authoritative source, correct
             for any future Ghidra/Java release.
          2. Scan support/launch.properties and other launch scripts for
             JAVA_HOME / MINIMUM_SUPPORTED_JAVA style keys (highest match wins).
          3. Last resort: DEFAULT_JAVA_MAJOR.
        """
        # ── Strategy 1: ask LaunchSupport.jar ─────────────────────────────
        launch_support = ghidra_src / "support" / "LaunchSupport.jar"
        launch_props   = ghidra_src / "support" / "launch.properties"

        if launch_support.exists() and launch_props.exists():
            # Any JVM is good enough to run this tiny utility jar
            java_candidates = [str(h / "bin" / "java") for h in cls._java_homes()]

            ls_args_suffix = [
                str(launch_props), "fg", "jdk", "Ghidra",
                "512m", "", "ghidra.GhidraRun",
            ]
            # Narrow patterns: match the REQUIRED version, not the found one
            req_patterns = [
                r'[Jj]ava\s+(\d+)(?:\s*\([^)]*\))?\s+(?:or\s+(?:higher|later|above)\s+)?is\s+required',
                r'[Jj]ava\s+(\d+)\s+or\s+(?:higher|later|above)',
                r'[Rr]equires?\s+[Jj]ava\s+(\d+)',
                r'(?:at\s+least|minimum)\s+[Jj]ava\s+(\d+)',
                r'MINIMUM_SUPPORTED_JAVA\s*[=:]\s*(\d+)',
            ]
            for java_cmd in java_candidates[:3]:
                try:
                    r = subprocess.run(
                        [java_cmd, "-jar", str(launch_support)] + ls_args_suffix,
                        capture_output=True, text=True, timeout=20,
                        cwd=str(ghidra_src))
                    combined = r.stdout + "\n" + r.stderr
                    best_from_jar = 0
                    for pat in req_patterns:
                        for m in re.finditer(pat, combined, re.IGNORECASE):
                            ver = int(m.group(1))
                            if 17 <= ver <= 99:
                                best_from_jar = max(best_from_jar, ver)
                    if best_from_jar:
                        return best_from_jar
                except Exception:
                    continue

        # ── Strategy 2: parse Ghidra's text files ─────────────────────────
        probe_files = [
            ghidra_src / "support" / "launch.properties",
            ghidra_src / "support" / "launch.sh",
            ghidra_src / "ghidraRun",
            ghidra_src / "support" / "ghidraRun",
        ]
        support_dir = ghidra_src / "support"
        if support_dir.is_dir():
            for p in support_dir.iterdir():
                if p.suffix.lower() in (".sh", ".properties", "") and p not in probe_files:
                    probe_files.append(p)

        text_patterns = [
            r'MINIMUM_SUPPORTED_JAVA\s*[=:]\s*(\d+)',
            r'MINIMUM_JAVA_VERSION\s*[=:]\s*(\d+)',
            r'JAVA_VERSION\s*[=:]\s*(\d+)',
            r'JAVA_VER\s*[=:]\s*(\d+)',
            r'jdk[_-](\d{2,})',
        ]
        best = 0
        for pf in probe_files:
            if not pf.is_file():
                continue
            try:
                text = pf.read_text(errors="ignore")
                for pat in text_patterns:
                    for m in re.finditer(pat, text, re.IGNORECASE):
                        ver = int(m.group(1))
                        if 17 <= ver <= 99:
                            best = max(best, ver)
            except Exception:
                continue

        # ── Strategy 3: safe floor ────────────────────────────────────────
        return best if best else DEFAULT_JAVA_MAJOR

    # ── Automatic Java install via Adoptium ───────────────────────

    def _fetch_java_info(self, java_major: int = DEFAULT_JAVA_MAJOR) -> tuple:
        """
        Query Adoptium API for the latest Temurin JDK of the given major for
        this machine.  Returns (download_url: str, size: int, version_str: str).
        """
        arch_map = {"arm64": "aarch64", "x86_64": "x64"}
        arch = arch_map.get(platform.machine(), "x64")
        url  = (f"{ADOPTIUM_API_BASE.format(ver=java_major)}"
                f"?architecture={arch}&image_type=jdk&os=mac&vendor=eclipse")
        self._log(f"Adoptium API: {url}")
        req  = urllib.request.Request(url, headers={"User-Agent": "GhidraInstaller/1.0"})
        with urllib.request.urlopen(req, timeout=20,
                                    context=create_ssl_context()) as resp:
            data = json.loads(resp.read().decode())
        if not data:
            raise RuntimeError(
                f"Adoptium API returned no JDK {java_major} release for this platform.\n"
                f"Check https://adoptium.net/ and install JDK {java_major} manually.")
        pkg = data[0]["binary"]["package"]
        ver = data[0]["version"]["semver"]
        return pkg["link"], pkg["size"], ver

    def _install_java_tarball(self, tar_path: Path, java_ver: str,
                              java_major: int = DEFAULT_JAVA_MAJOR):
        """
        Extract a Temurin JDK tar.gz into the macOS JVM bundle structure:
          ~/Library/Java/JavaVirtualMachines/temurin-{major}.jdk/Contents/Home/

        Works with both flat tarballs (jdk-21.x/bin/java) and pre-bundled
        tarballs (jdk-21.x.jdk/Contents/Home/bin/java).
        """
        bundle_name  = _jdk_bundle_name(java_major)
        bundle       = JDK_INSTALL_BASE / bundle_name
        contents_dir = bundle / "Contents"
        home_dir     = contents_dir / "Home"
        tmp_dir      = JDK_INSTALL_BASE / "_jdk_install_tmp"

        # Clean up previous attempts
        for d in (bundle, tmp_dir):
            if d.exists():
                shutil.rmtree(d)
        tmp_dir.mkdir(parents=True)

        # ── Step 1: extract tarball to tmp (with per-file progress) ──
        self._log(f"Extracting JDK {java_major} tarball…")
        with tarfile.open(tar_path, "r:gz") as tf:
            members = tf.getmembers()
            total   = len(members)
            for i, member in enumerate(members):
                if self._cancel:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return
                try:
                    tf.extract(member, tmp_dir)
                except Exception as e:
                    self._log(f"  warn: {e}")
                self._set_progress((i + 1) / total * 100)
                self._set_detail(f"Extracting JDK {i+1}/{total}")

        # ── Step 2: locate bin/java regardless of tarball layout ─────
        java_bins = sorted(tmp_dir.rglob("bin/java"))
        if not java_bins:
            raise RuntimeError("bin/java not found inside JDK tarball — "
                               "the download may be corrupt")
        # jdk_home is the directory that directly contains bin/
        jdk_home_src = java_bins[0].parent.parent
        self._log(f"JDK home detected: {jdk_home_src.relative_to(tmp_dir)}")

        # ── Step 3: move JDK home into the macOS bundle ──────────────
        home_dir.mkdir(parents=True)
        for item in jdk_home_src.iterdir():
            shutil.move(str(item), home_dir / item.name)
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # ── Step 4: ensure execute bits on bin/ ───────────────────────
        bin_dir = home_dir / "bin"
        if bin_dir.exists():
            for p in bin_dir.iterdir():
                if p.is_file():
                    try:
                        p.chmod(p.stat().st_mode |
                                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    except Exception:
                        pass

        # ── Step 5: remove quarantine extended attr (if present) ─────
        subprocess.run(["xattr", "-rd", "com.apple.quarantine", str(bundle)],
                       capture_output=True)

        # ── Step 6: write Info.plist for /usr/libexec/java_home ──────
        # Derive clean version strings from semver (e.g. "21.0.11+10.0.LTS")
        clean_ver  = re.sub(r'\.LTS.*', '', java_ver)          # "21.0.11+10.0"
        clean_ver  = re.sub(r'\.0$', '', clean_ver)            # "21.0.11+10"
        short_ver  = clean_ver.split("+")[0]                   # "21.0.11"
        plist = {
            "CFBundleIdentifier":          f"net.adoptium.{bundle_name.replace('.jdk', '')}",
            "CFBundleName":                f"Temurin JDK {short_ver}",
            "CFBundleVersion":             clean_ver,
            "CFBundleShortVersionString":  short_ver,
            "CFBundleExecutable":          "libjli.dylib",
            "CFBundlePackageType":         "BNDL",
            "JavaVM": {
                "JVMCapabilities":            ["CommandLine"],
                "JVMMinimumFrameworkVersion": "1.5.0",
                "JVMPlatformVersion":         short_ver,
                "JVMVendor":                  "Eclipse Adoptium",
                "JVMVersion":                 clean_ver,
            },
        }
        with open(contents_dir / "Info.plist", "wb") as fh:
            plistlib.dump(plist, fh)

        self._log(f"JDK {java_major} installed → {home_dir}")
        self._log(f"JVM registered as: {clean_ver}")

    # ── Fetch available releases (populates version combobox) ──────────

    @staticmethod
    def _parse_release(data: dict) -> dict | None:
        """Extract info from a single GitHub release dict. Returns None if
        the release has no PUBLIC zip asset."""
        tag  = data.get("tag_name", "")
        date = (data.get("published_at") or "")[:10]
        body = data.get("body", "")

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
            "tag":      tag,
            "date":     date,
            "url":      asset["browser_download_url"],
            "filename": asset["name"],
            "size":     asset.get("size", 0),
            "sha256":   sha,
        }

    def _fetch_releases(self):
        """Fetch the last 10 Ghidra releases from GitHub and populate the
        version combobox.  Auto-selects the newest."""
        self._log("Fetching available Ghidra releases from GitHub…")
        # GitHub API endpoint for multiple releases
        list_url = (GITHUB_API.replace("/releases/latest", "/releases")
                    + "?per_page=10")
        try:
            req = urllib.request.Request(
                list_url,
                headers={"User-Agent": "GhidraInstaller/1.0",
                         "Accept":     "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15,
                                        context=create_ssl_context()) as resp:
                raw_list = json.loads(resp.read().decode())
        except Exception as e:
            self._log(f"Failed to fetch release list: {e}")
            self.after(0, lambda: self.combo_version.set(
                "Error — check connection"))
            return

        releases = [r for r in
                    (self._parse_release(d) for d in raw_list)
                    if r is not None]

        if not releases:
            self._log("No usable releases found.")
            return

        self._releases = releases
        labels = [f"{r['version']}   ({r['date']})" for r in releases]
        self._log(f"Found {len(releases)} releases: "
                  + ", ".join(r['version'] for r in releases))

        def _update():
            self.combo_version["values"] = labels
            self.combo_version.current(0)      # select newest
            self._on_version_selected()        # populate info fields
        self.after(0, _update)

    def _on_version_selected(self, event=None):
        """Called when the user picks a version from the combobox."""
        idx = self.combo_version.current()
        if idx < 0 or idx >= len(self._releases):
            return
        r = self._releases[idx]

        self._download_url = r["url"]
        self._asset_name   = r["filename"]
        self._asset_size   = r["size"]

        # Ghidra 12.1+ does NOT ship macOS native binaries — must compile locally
        def _ver_tuple(v):
            try:
                return tuple(int(x) for x in v.split(".")[:3])
            except Exception:
                return (0,)
        needs_build = _ver_tuple(r["version"]) >= _ver_tuple("12.1")
        build_note  = ("Compiled from source during install (about 5–10 min)"
                       if needs_build else "Pre-built binaries included")

        self.lbl_date.configure(text=r["date"])
        self.lbl_size.configure(text=fmt_size(r["size"]))
        self.lbl_sha.configure(text=r["sha256"] if r["sha256"] else "Not available")
        self.lbl_natives.configure(
            text=build_note,
            fg="orange" if needs_build else "#4CAF50")

        # Show "Reinstall" only if this exact version is already tracked on disk
        already = any(
            e.get("version") == r["version"] and Path(e.get("app_path", "")).exists()
            for e in _read_installs()
        )
        self.btn_install.configure(
            state="normal",
            text="Reinstall" if already else "Download & Install")

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

    # ── Pre-flight helpers ─────────────────────────────────────────

    def _ensure_rosetta(self):
        """Install Rosetta 2 silently if not already present (Apple Silicon only)."""
        r = subprocess.run(['arch', '-x86_64', '/usr/bin/true'],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            self._log("Rosetta 2: already installed ✓")
            return

        self._log("Rosetta 2: not installed — installing silently…")
        self._set_status("Pre-flight: installing Rosetta 2…", "blue")
        r = subprocess.run(
            ['/usr/sbin/softwareupdate', '--install-rosetta', '--agree-to-license'],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            self._log("Rosetta 2: installed ✓")
        else:
            msg = (r.stderr.strip() or r.stdout.strip())[:120]
            self._log(f"Rosetta 2: {msg or 'install attempted (may already be present)'}")

    def _ensure_clt(self) -> bool:
        """Ensure Xcode Command Line Tools are installed."""

        MAX_WAIT_SECONDS = 1200  # 20 mins
        POLL_INTERVAL = 10

        try:
            r = subprocess.run(
                ['xcode-select', '-p'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if r.returncode == 0:
                self._log(
                    f"Command Line Tools: "
                    f"{r.stdout.strip()} ✓"
                )
                return True

        except Exception:
            pass

        self._log("Command Line Tools: not installed")
        self._set_status(
            "Installing Command Line Tools…",
            "blue"
        )

        # Try silent install first
        try:
            self._log(
                "Checking for Command Line Tools package..."
            )

            result = subprocess.run(
                ['/usr/sbin/softwareupdate', '-l'],
                capture_output=True,
                text=True,
                timeout=60
            )

            label = None

            for line in result.stdout.splitlines():
                if "Command Line Tools" in line:
                    line = line.strip()

                    if "Label:" in line:
                        label = line.split("Label:")[-1].strip()
                    else:
                        label = line.lstrip("* ").strip()

                    break

            if label:
                self._log(
                    f"Attempting silent install: {label}"
                )

                subprocess.Popen([
                    '/usr/sbin/softwareupdate',
                    '--install',
                    label
                ])

            else:
                raise RuntimeError(
                    "No CLT package label found"
                )

        except Exception as e:
            self._log(
                f"Silent install unavailable: {e}"
            )

            user_choice = messagebox.askyesno(
                "Command Line Tools Required",
                (
                    "Ghidra requires Apple's "
                    "Command Line Tools.\n\n"
                    "The installer will now open "
                    "the Apple installation dialog.\n\n"
                    "Click 'Yes' to continue "
                    "or 'No' to cancel."
                ),
                parent=self
            )

            if not user_choice:
                self._log(
                    "User cancelled Command "
                    "Line Tools installation."
                )
                return False

            try:
                subprocess.Popen(
                    ['xcode-select', '--install']
                )

                self._log(
                    "Apple install dialog opened"
                )

            except Exception as ex:
                self._log(
                    f"Failed to launch CLT installer: {ex}"
                )

                messagebox.showerror(
                    "Install Failed",
                    (
                        "Unable to open the "
                        "Command Line Tools installer.\n\n"
                        f"{ex}"
                    ),
                    parent=self
                )

                return False

        def _surface_clt_dialog():
            applescript = """
            tell application "System Events"
                repeat with proc in (every process)
                    try
                        if name of proc contains "Install" then
                            set frontmost of proc to true
                        end if
                    end try
                end repeat
            end tell
            """

            subprocess.run(
                ['osascript', '-e', applescript],
                capture_output=True,
                timeout=10
            )

            self.lift()

        self.after(1500, _surface_clt_dialog)

        self._log(
            f"Waiting up to "
            f"{MAX_WAIT_SECONDS // 60} minutes "
            f"for Command Line Tools..."
        )

        start = time.time()

        while (time.time() - start) < MAX_WAIT_SECONDS:

            if self._cancel:
                return False

            time.sleep(POLL_INTERVAL)

            try:
                r = subprocess.run(
                    ['xcode-select', '-p'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )

                if r.returncode == 0:

                    self._log(
                        "Command Line Tools: installed ✓"
                    )

                    self.after(0, self.deiconify)
                    self.after(0, self.lift)

                    return True

            except Exception:
                pass

        self._log(
            "Timed out waiting for Command Line Tools."
        )

        self.after(0, self.deiconify)
        self.after(0, self.lift)

        return False


    # ── Background worker ─────────────────────────────────────────

    def _worker(self, install_dir: str):
        work_dir: Path | None = None
        try:
            os.makedirs(install_dir, exist_ok=True)

            work_dir = Path(tempfile.mkdtemp(prefix="GhidraInstall_"))
            self._log(f"Work dir : {work_dir}")
            self._log(f"Install  : {install_dir}")

            # ── Pre-flight: Rosetta + Command Line Tools ──────────────────
            self._log("\n── Pre-flight ─────────────────────────────────────────────────")
            if platform.machine() == 'arm64':
                self._ensure_rosetta()
            self._ensure_clt()

            # ── Step 1: Download Ghidra ZIP ───────────────────────────────
            tmp_zip = work_dir / self._asset_name
            self._log("\n── [1/3] Download Ghidra ────────────────────")
            self._log(f"URL : {self._download_url}")
            self._log(f"Dest: {tmp_zip}")
            self._set_status("[1/3] Downloading Ghidra…", "blue")
            self._download(self._download_url, tmp_zip)
            if self._cancel:
                self._log("Cancelled.")
                self._reset_ui()
                return

            # ── Step 2: Extract Ghidra ────────────────────────────────────
            self._log("\n── [2/3] Extract Ghidra ─────────────────────")
            self._set_status("[2/3] Extracting Ghidra…", "blue")
            tmp_extract = work_dir / "_extract"
            self._extract(tmp_zip, tmp_extract)
            if self._cancel:
                self._log("Cancelled.")
                self._reset_ui()
                return

            # Find the inner ghidra_X.X.X_PUBLIC folder
            ghidra_dirs = sorted(
                [d for d in tmp_extract.iterdir()
                 if d.is_dir() and d.name.startswith("ghidra_")])
            if not ghidra_dirs:
                raise RuntimeError("Could not find ghidra folder inside ZIP")
            ghidra_src = ghidra_dirs[0]
            self._log(f"Found: {ghidra_src.name}")

            # ── Step 3: Detect required Java version from Ghidra files ────
            java_major = self._get_required_java_version(ghidra_src)
            self._log(f"\n── [3/4] Java {java_major}+ check ────────────────────────────")
            self._log(f"Ghidra requires Java {java_major}+")
            self.after(0, lambda m=java_major: self.lbl_java.configure(
                text=f"Checking for Java {m}+…", fg="gray"))

            java_ok, java_label = self._probe_java(min_major=java_major)
            if java_ok:
                self._log(f"Java OK: {java_label}")
                self._set_status(f"[3/4] Java {java_major}+ OK — skipping JDK download", "gray")
                self.after(0, lambda lbl=java_label: self.lbl_java.configure(
                    text=lbl, fg="green"))
            else:
                self._log(f"Java status: {java_label}")
                self._log(f"Fetching Temurin JDK {java_major} info from Adoptium…")
                self._set_status(f"[3/4] Fetching Java {java_major} info…", "blue")

                # Try the required version first, then newer majors (a newer
                # JDK always satisfies the minimum).  Never fall back to an
                # OLDER major — Ghidra would refuse to launch with it.
                java_url = java_size = java_ver = None
                for try_major in range(java_major, java_major + 4):
                    try:
                        java_url, java_size, java_ver = self._fetch_java_info(try_major)
                        java_major = try_major    # use whatever actually worked
                        break
                    except Exception as e:
                        self._log(f"  JDK {try_major} not available from Adoptium: {e}")
                if java_url is None:
                    raise RuntimeError(
                        f"Could not find JDK {java_major} on Adoptium. "
                        "Install a JDK manually from https://adoptium.net/ and retry.")

                self._log(f"Temurin JDK {java_ver}  ({fmt_size(java_size)})")
                self._log(f"URL: {java_url}")

                tmp_jdk = work_dir / f"temurin-jdk-{java_major}.tar.gz"
                self._set_status(
                    f"[3/4] Downloading Java {java_major} ({fmt_size(java_size)})…", "blue")
                self._download(java_url, tmp_jdk, hint_size=java_size)
                if self._cancel:
                    self._log("Cancelled.")
                    self._reset_ui()
                    return

                self._set_status(f"[3/4] Installing Java {java_major}…", "blue")
                self._install_java_tarball(tmp_jdk, java_ver, java_major)
                tmp_jdk.unlink(missing_ok=True)

                if self._cancel:
                    self._log("Cancelled.")
                    self._reset_ui()
                    return

                self._log(f"Java {java_ver} installed ✓")
                self.after(0, lambda v=java_ver: self.lbl_java.configure(
                    text=f"Temurin {v} (bundled) ✓", fg="green"))

            # Resolve java_home (any location: user, system, Homebrew, SDKMAN)
            java_home = self._find_java_home(java_major)
            if java_home:
                self._log(f"Using JAVA_HOME: {java_home}")
            else:
                self._log(f"⚠ No Java {java_major}+ home resolved — build may fail")

            # ── Step 4: Build macOS native components if missing ──────────
            self._build_native_components(ghidra_src, java_home, java_major)

            # ── Step 5: Build Ghidra.app ──────────────────────────────────
            self._set_status("[4/4] Building Ghidra.app…", "blue")
            version_str      = ghidra_src.name.split("_")[1] if "_" in ghidra_src.name else "unknown"
            versioned_app_name = f"Ghidra {version_str}.app"

            app_work = work_dir / versioned_app_name
            self._create_app(ghidra_src, app_work, java_major)

            # ── Step 6: Move finished .app to install location ────────────
            app_final = Path(install_dir) / versioned_app_name
            self._log(f"\n── Install ────────────────────────────────────────")
            if app_final.exists():
                self._log(f"Removing existing {versioned_app_name} from {install_dir}…")
                shutil.rmtree(app_final)
            self._set_status("Moving Ghidra.app to install location…", "blue")
            self._log(f"Moving {versioned_app_name} → {install_dir}")
            shutil.move(str(app_work), str(app_final))
            self._log(f"Installed: {app_final}")

            # Strip Gatekeeper quarantine
            self._log("Removing quarantine from bundle…")
            subprocess.run(
                ["xattr", "-rd", "com.apple.quarantine", str(app_final)],
                capture_output=True)

            # Save install metadata
            try:
                _append_install({
                    "version":     version_str,
                    "source":      ghidra_src.name,
                    "app_path":    str(app_final),
                    "install_dir": install_dir,
                    "date":        time.strftime("%Y-%m-%d"),
                }, log=self._log)
            except Exception as e:
                self._log(f"Note: could not save install info: {e}")
            self.after(0, self._load_installed_version)

            self._log(f"\n✓ Done — {app_final}")
            self.after(0, lambda: self._finish(app_final))

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

    # ── Download with progress ────────────────────────────────────

    def _download(self, url: str, dest: Path, hint_size: int = 0):
        req = urllib.request.Request(url, headers={"User-Agent": "GhidraInstaller/1.0"})
        t0  = time.time()
        got = 0
        with urllib.request.urlopen(req, timeout=120,
                                    context=create_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or hint_size or self._asset_size or 0)
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
                    eta     = f"  ETA {int((total - got) / speed)}s" if speed and total else ""
                    detail  = (f"{fmt_size(got)}"
                               + (f" / {fmt_size(total)}" if total else "")
                               + f"  {fmt_speed(speed)}{eta}")
                    self._set_progress(pct)
                    self._set_detail(detail)
        if not self._cancel:
            self._log(f"Downloaded {fmt_size(got)} in {time.time()-t0:.1f}s")

    # ── Build macOS native components (Ghidra 12.1+) ────────────────

    def _build_native_components(self, ghidra_src: Path,
                                  java_home: Path | None,
                                  java_major: int = DEFAULT_JAVA_MAJOR) -> bool:
        """
        Compile the macOS-specific native binaries (decompiler, sleigh, etc.)
        that are absent from Ghidra 12.1+ official releases.

        Searches several locations for gradlew (Ghidra reorganises its build
        layout between minor versions) and falls back through known task names.

        Built binaries are copied to os/<platform>/ so they survive .app bundling.
        """
        arch     = platform.machine()    # 'arm64' or 'x86_64'
        plat_dir = f"mac_{arch}"

        # ── Already present? ─────────────────────────────────────────
        # Check several locations where native binaries might live
        native_check_paths = [
            ghidra_src / "Ghidra" / "Features" / "Decompiler" / "os" / plat_dir,
            ghidra_src / "Ghidra" / "Features" / "Decompiler" / "build" / "os" / plat_dir,
        ]
        for check_path in native_check_paths:
            if check_path.exists() and any(check_path.iterdir()):
                self._log(f"Native components already present for {plat_dir}")
                return True

        # ── Find gradlew — check multiple locations ───────────────────
        gradlew_candidates = [
            ghidra_src / "support" / "gradle" / "gradlew",    # Ghidra ≤12.0
            ghidra_src / "support" / "gradlew",               # possible alt
            ghidra_src / "gradle" / "gradlew",                # possible alt
            ghidra_src / "gradlew",                           # root (12.1+?)
        ]
        gradlew = next((g for g in gradlew_candidates if g.exists()), None)
        if gradlew is None:
            self._log("gradlew not found in any expected location — skipping native build")
            self._log(f"  Searched: {[str(g) for g in gradlew_candidates]}")
            return False

        # Ensure gradlew is executable
        gradlew.chmod(gradlew.stat().st_mode |
                      stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        self._log(f"\n── Building macOS native components ({plat_dir}) ──────")
        self._log(f"gradlew: {gradlew.relative_to(ghidra_src)}")
        self._log("Ghidra 12.1+ does not ship pre-built macOS binaries.")
        self._log("Compiling from source via gradlew (needs internet + clang).")
        self._log("This typically takes 3–10 minutes on first run...")
        self._set_status("[4/4] Building macOS native components… (3-10 min)", "blue")

        # Build environment with correct JAVA_HOME
        env = os.environ.copy()
        if java_home and java_home.exists():
            env["JAVA_HOME"] = str(java_home)
            env["PATH"]      = f"{java_home}/bin:{env.get('PATH', '/usr/bin:/bin')}"
            self._log(f"JAVA_HOME = {java_home}")
        else:
            # Try /usr/libexec/java_home as a last resort
            try:
                r = subprocess.run(
                    ["/usr/libexec/java_home", f"-v{java_major}+"],
                    capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    env["JAVA_HOME"] = r.stdout.strip()
                    env["PATH"] = f"{r.stdout.strip()}/bin:{env.get('PATH', '/usr/bin:/bin')}"
                    self._log(f"JAVA_HOME (from java_home): {r.stdout.strip()}")
            except Exception:
                pass

        # Switch progress bar to indeterminate pulse while building
        self.after(0, lambda: self.progress.configure(mode="indeterminate"))
        self.after(0, lambda: self.progress.start(20))

        build_ok   = False
        used_task  = None

        # Try task names in order — Ghidra has used different names across versions
        task_names = [
            "buildNatives",
            f"buildNatives{arch.capitalize()}",   # e.g. buildNativesArm64
            "buildNativesMac",
            "assembleNatives",
            "buildGhidraJar",                     # older fallback
        ]

        try:
            for task in task_names:
                if self._cancel:
                    break
                self._log(f"Trying gradlew task: {task} …")
                try:
                    proc = subprocess.Popen(
                        [str(gradlew), "--no-daemon", task],
                        cwd=str(gradlew.parent),
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                    output_lines = []
                    for line in proc.stdout:
                        stripped = line.rstrip()
                        if stripped:
                            self._log(stripped)
                            output_lines.append(stripped)
                    proc.wait()

                    if proc.returncode == 0:
                        build_ok  = True
                        used_task = task
                        self._log(f"✓ gradlew {task} succeeded")
                        break
                    else:
                        # Distinguish "task not found" from a real build failure
                        combined = "\n".join(output_lines[-20:])
                        task_missing = any(
                            phrase in combined for phrase in [
                                "Task '" + task + "' not found",
                                f"Could not determine the dependencies of task ':{task}'",
                                "not find a matching task",
                            ])
                        if task_missing:
                            self._log(f"  Task '{task}' not found, trying next…")
                            continue
                        else:
                            self._log(f"  gradlew {task} failed (exit {proc.returncode})")
                            # Still try the next task in case this one errored for other reasons
                            continue
                except Exception as e:
                    self._log(f"  gradlew {task} error: {e}")
                    continue
        finally:
            self.after(0, lambda: self.progress.stop())
            self.after(0, lambda: self.progress.configure(
                mode="determinate", value=0))

        if not build_ok:
            self._log("Native component build FAILED — Ghidra will show the"
                      " \"Missing Native Components\" warning on launch.")
            self._log("You can still try running Ghidra; some features may be limited.")
            return False

        # ── Copy build/os/<plat>/ → os/<plat>/ for each module ──────────
        self._log(f"Installing built binaries into os/{plat_dir} directories…")
        installed = 0
        for built_dir in ghidra_src.rglob(f"build/os/{plat_dir}"):
            # built_dir is e.g.: .../Decompiler/build/os/mac_arm64
            # target is:         .../Decompiler/os/mac_arm64
            target = built_dir.parent.parent.parent / "os" / plat_dir
            target.mkdir(parents=True, exist_ok=True)
            for f in built_dir.iterdir():
                if f.is_file():
                    dest = target / f.name
                    shutil.copy2(f, dest)
                    dest.chmod(dest.stat().st_mode |
                               stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    installed += 1

        if installed:
            subprocess.run(
                ["xattr", "-rd", "com.apple.quarantine", str(ghidra_src)],
                capture_output=True)
            self._log(f"Native components built and installed: "
                      f"{installed} file(s) in os/{plat_dir}/")
        else:
            self._log(f"Build task '{used_task}' succeeded but no binaries found in "
                      f"build/os/{plat_dir}/ — Ghidra may still warn.")

        return installed > 0

    # ── Extract ZIP, restoring permissions ───────────────────────

    def _extract(self, zip_path: Path, dest: Path):
        """
        Extract the Ghidra ZIP while preserving Unix execute bits.

        Uses bsdtar (always on macOS) which preserves all ZIP metadata
        including Unix permission bits. Falls back to Python zipfile.
        """
        dest.mkdir(parents=True, exist_ok=True)

        # ── Attempt 1: bsdtar (preserves all Unix permission bits) ───────
        bsdtar = "/usr/bin/bsdtar"
        if os.path.exists(bsdtar):
            self._log("Extracting with bsdtar (preserves +x on native binaries)…")
            self._set_detail("Extracting with bsdtar…")
            self._set_progress(5)
            try:
                r = subprocess.run(
                    [bsdtar, "-xf", str(zip_path), "-C", str(dest)],
                    capture_output=True, text=True
                )
                if r.returncode == 0:
                    count = sum(1 for _ in dest.rglob("*") if _.is_file())
                    self._set_progress(100)
                    self._log(f"Extracted {count} files")
                    return
                self._log(f"bsdtar exit {r.returncode}: {r.stderr[:300]}")
                self._log("Falling back to Python zipfile extraction…")
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True)
            except Exception as e:
                self._log(f"bsdtar failed ({e}), falling back to Python zipfile…")
                shutil.rmtree(dest, ignore_errors=True)
                dest.mkdir(parents=True)
        else:
            self._log("/usr/bin/bsdtar not found — using Python zipfile extraction")

        # ── Attempt 2: Python zipfile with manual per-file progress ──────
        self._log("Extracting with Python zipfile (may lose +x on some binaries)…")
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = zf.infolist()
            total   = len(members)
            for i, member in enumerate(members):
                if self._cancel:
                    break
                zf.extract(member, dest)
                # Restore Unix permissions stored in external_attr
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                target    = dest / member.filename
                if unix_mode and target.exists():
                    target.chmod(unix_mode)
                elif target.is_file() and (
                    member.filename.endswith(".sh") or
                    os.path.basename(member.filename) in {
                        "ghidraRun", "analyzeHeadless", "ghidra-server",
                        "svrAdmin", "server", "hstart64", "vmlinuxx64",
                    }
                ):
                    target.chmod(target.stat().st_mode |
                                 stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                self._set_progress((i + 1) / total * 100)
                self._set_detail(f"Extracting {i+1} / {total}")
        self._log(f"Extracted {total} entries (zipfile fallback)")

    # ── Create macOS .app bundle ──────────────────────────────────

    def _create_app(self, ghidra_src: Path, app_path: Path,
                    java_major: int = DEFAULT_JAVA_MAJOR):
        if app_path.exists():
            self._log(f"Removing existing {app_path.name}…")
            shutil.rmtree(app_path)

        contents   = app_path / "Contents"
        macos_dir  = contents / "MacOS"
        res_dir    = contents / "Resources"
        ghidra_dst = res_dir  / "ghidra"

        macos_dir.mkdir(parents=True)
        res_dir.mkdir(parents=True)

        # Copy Ghidra files
        self._log("Copying Ghidra into bundle…")
        shutil.copytree(ghidra_src, ghidra_dst)

        # Re-apply execute bits after copytree (which may strip them)
        self._restore_exec_bits(ghidra_dst)

        # Convert and embed the Ghidra icon
        icon_ok = self._create_icns(ghidra_dst, res_dir)

        # Launcher script
        launcher = macos_dir / "Ghidra"
        launcher.write_text(self._launcher_script(java_major))
        launcher.chmod(launcher.stat().st_mode |
                       stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._log("Launcher script created")

        # Info.plist
        parts       = ghidra_src.name.split("_")   # ghidra_12.0.4_PUBLIC
        ver_str     = parts[1] if len(parts) > 1 else "1.0"
        display_name = f"Ghidra {ver_str}"
        plist_data  = {
            "CFBundleName":                display_name,
            "CFBundleDisplayName":         display_name,
            "CFBundleIdentifier":          "gov.nsa.ghidra",
            "CFBundleVersion":             ver_str,
            "CFBundleShortVersionString":  ver_str,
            "CFBundlePackageType":         "APPL",
            "CFBundleSignature":           "????",
            "CFBundleExecutable":          "Ghidra",
            "CFBundleInfoDictionaryVersion": "6.0",
            "NSHighResolutionCapable":     True,
            "NSSupportsAutomaticGraphicsSwitching": True,
            "LSMinimumSystemVersion":      "11.0",
            "LSUIElement":                 False,
        }
        if icon_ok:
            plist_data["CFBundleIconFile"] = "Ghidra"
        with open(contents / "Info.plist", "wb") as fh:
            plistlib.dump(plist_data, fh)
        (contents / "PkgInfo").write_text("APPL????")
        self._log(f"Info.plist written  (version {ver_str})")

        self._log(f"Bundle ready: {app_path}")

    # ── ICO → ICNS icon conversion ─────────────────────────────────

    def _create_icns(self, ghidra_bundle_dir: Path, resources_dir: Path) -> bool:
        """
        Find the Ghidra icon (ICO or PNG) inside the extracted Ghidra bundle,
        convert it to a proper macOS ICNS file, and place it in resources_dir.
        Uses only macOS built-in tools: sips + iconutil.
        Returns True on success, False if icon creation was skipped/failed.
        """
        candidates = [
            ghidra_bundle_dir / "support" / "ghidra.ico",
            ghidra_bundle_dir / "support" / "Ghidra.ico",
            ghidra_bundle_dir / "support" / "ghidra64.png",
            ghidra_bundle_dir / "support" / "ghidra.png",
        ]
        support = ghidra_bundle_dir / "support"
        if support.is_dir():
            for f in sorted(support.iterdir()):
                if f.suffix.lower() in (".ico", ".png") and f not in candidates:
                    candidates.append(f)

        src = next((p for p in candidates if p.exists()), None)
        if src is None:
            self._log("Icon: no source image found, attempting download…")
            try:
                downloaded = resources_dir / "ghidra_download.ico"
                urllib.request.urlretrieve(
                    "https://raw.githubusercontent.com/NationalSecurityAgency/ghidra/master/Ghidra/RuntimeScripts/Windows/support/ghidra.ico",
                    downloaded
                )
                if downloaded.exists():
                    src = downloaded
                    self._log("Icon: downloaded ghidra.ico")
                else:
                    self._log("Icon download failed")
                    return False
            except Exception as e:
                self._log(f"Icon download failed: {e}")
                return False

        self._log(f"Icon source: {src.name}")
        icns_out = resources_dir / "Ghidra.icns"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path  = Path(tmp)
            base_png  = tmp_path / "base.png"

            if src.suffix.lower() == ".ico":
                r = subprocess.run(
                    ["sips", "-s", "format", "png", str(src),
                     "--out", str(base_png)],
                    capture_output=True)
                if r.returncode != 0 or not base_png.exists():
                    self._log(f"Icon: sips ICO→PNG failed: "
                              f"{r.stderr.decode(errors='replace').strip()}")
                    return False
            else:
                shutil.copy2(src, base_png)

            iconset = tmp_path / "Ghidra.iconset"
            iconset.mkdir()
            icon_sizes = [
                ("icon_16x16.png",      16),
                ("icon_16x16@2x.png",   32),
                ("icon_32x32.png",      32),
                ("icon_32x32@2x.png",   64),
                ("icon_128x128.png",   128),
                ("icon_128x128@2x.png", 256),
                ("icon_256x256.png",   256),
                ("icon_256x256@2x.png", 512),
                ("icon_512x512.png",   512),
            ]
            for name, px in icon_sizes:
                subprocess.run(
                    ["sips", "-z", str(px), str(px),
                     str(base_png), "--out", str(iconset / name)],
                    capture_output=True)

            r = subprocess.run(
                ["iconutil", "-c", "icns",
                 str(iconset), "-o", str(icns_out)],
                capture_output=True)
            if r.returncode == 0 and icns_out.exists():
                kb = icns_out.stat().st_size // 1024
                self._log(f"Icon: Ghidra.icns created ({kb} KB)")
                return True
            self._log(f"Icon: iconutil failed: "
                      f"{r.stderr.decode(errors='replace').strip()}")
            return False

    # ── Restore +x bits on files that should be executable ───────

    @staticmethod
    def _restore_exec_bits(base: Path):
        EXEC_NAMES = {
            "ghidraRun", "analyzeHeadless", "ghidra-server",
            "svrAdmin", "server", "hstart64",
        }
        NATIVE_OS_DIRS = {
            "mac_arm64", "mac_x86_64", "mac_64",
            "linux_arm_64", "linux_x86_64",
        }
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            mode = p.stat().st_mode
            if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            elif p.suffix == ".sh" or p.name in EXEC_NAMES:
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            elif p.parent.name in NATIVE_OS_DIRS and not p.suffix:
                p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # ── Bash launcher script content ──────────────────────────────

    @staticmethod
    def _launcher_script(min_java: int = DEFAULT_JAVA_MAJOR) -> str:
        script = r"""#!/bin/bash
# ─────────────────────────────────────────────────────────
#  Ghidra macOS Launcher
#  Searches for a JDK __MIN_JAVA__+ installation, then starts Ghidra.
#  Checks every JVM bundle on the machine — not a fixed path.
# ─────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GHIDRA_DIR="$SCRIPT_DIR/../Resources/ghidra"
MIN_JAVA=__MIN_JAVA__

# Return 0 if the given java binary is version MIN_JAVA or newer
java_ok() {
    local java_bin="$1"
    [ -x "$java_bin" ] || return 1
    local ver
    ver=$("$java_bin" -version 2>&1 | sed -n 's/.*version "\([0-9]*\).*/\1/p' | head -1)
    [ -n "$ver" ] && [ "$ver" -ge "$MIN_JAVA" ] 2>/dev/null
}

JAVA_BIN=""

# 1. JAVA_HOME environment variable
if [ -n "$JAVA_HOME" ] && java_ok "$JAVA_HOME/bin/java"; then
    JAVA_BIN="$JAVA_HOME/bin/java"
fi

# 2. Every JVM bundle in ~/Library/Java/JavaVirtualMachines/ (catches any major)
if [ -z "$JAVA_BIN" ]; then
    for jvm in "$HOME"/Library/Java/JavaVirtualMachines/*/Contents/Home; do
        if java_ok "$jvm/bin/java"; then
            JAVA_BIN="$jvm/bin/java"
            export JAVA_HOME="$jvm"
            break
        fi
    done
fi

# 3. /usr/libexec/java_home registry (finds any registered JVM >= MIN_JAVA)
if [ -z "$JAVA_BIN" ]; then
    jh=$(/usr/libexec/java_home -v${MIN_JAVA}+ 2>/dev/null)
    if [ -n "$jh" ] && java_ok "$jh/bin/java"; then
        JAVA_BIN="$jh/bin/java"
        export JAVA_HOME="$jh"
    fi
fi

# 4. System-wide /Library/Java/JavaVirtualMachines/
if [ -z "$JAVA_BIN" ]; then
    for jvm in /Library/Java/JavaVirtualMachines/*/Contents/Home; do
        if java_ok "$jvm/bin/java"; then
            JAVA_BIN="$jvm/bin/java"
            export JAVA_HOME="$jvm"
            break
        fi
    done
fi

# 5. PATH java — only if a real JDK is registered; otherwise /usr/bin/java
#    is Apple's stub, which pops up an "Install Java" system dialog.
if [ -z "$JAVA_BIN" ] && /usr/libexec/java_home >/dev/null 2>&1 \
        && java_ok "/usr/bin/java"; then
    JAVA_BIN="/usr/bin/java"
fi

# 6. Homebrew openjdk (Apple Silicon + Intel)
if [ -z "$JAVA_BIN" ]; then
    for brew_java in \
        /opt/homebrew/opt/openjdk*/bin/java \
        /usr/local/opt/openjdk*/bin/java; do
        if java_ok "$brew_java"; then
            JAVA_BIN="$brew_java"
            export JAVA_HOME="$(dirname "$(dirname "$brew_java")")"
            break
        fi
    done
fi

if [ -z "$JAVA_BIN" ]; then
    osascript -e "display alert \"Java ${MIN_JAVA}+ Required\" message \"Ghidra requires Java Development Kit (JDK) version ${MIN_JAVA} or later.\n\nDownload JDK from:\nhttps://adoptium.net/\" as critical"
    exit 1
fi

export JAVA="$JAVA_BIN"
export PATH="$(dirname "$JAVA_BIN"):$PATH"
exec "$GHIDRA_DIR/ghidraRun"
"""
        return script.replace("__MIN_JAVA__", str(int(min_java)))

    # ── Post-install ──────────────────────────────────────────────

    def _cleanup_ghidra_if_empty(self):
        """Remove ~/Library/Application Support/GhidraInstaller when no
        tracked installs remain."""
        try:
            remaining = _read_installs()
            remaining = [e for e in remaining
                         if Path(e.get("app_path", "")).exists()]
            if remaining:
                self._log(
                    f"Cleanup skipped: {len(remaining)} Ghidra app(s) remain")
                return

            self._log("No Ghidra installs remain. Removing support data…")
            if INFO_DIR.exists():
                shutil.rmtree(INFO_DIR)
                self._log(f"Removed: {INFO_DIR}")
        except Exception as e:
            self._log(f"Ghidra cleanup warning: {e}")

    # ── Uninstall dialog ────────────────────────────────

    def _show_uninstall_dialog(self):
        """
        Manage / uninstall tracked Ghidra installations.
        - Treeview listing every tracked version with date and path.
        - Missing-path rows highlighted in red.
        - Remove button deletes the .app and cleans up metadata.
        - Removes support directory when the last version is gone.
        """
        dlg = tk.Toplevel(self)
        dlg.title("Manage Ghidra Installations")
        dlg.resizable(True, False)
        dlg.transient(self)
        dlg.grab_set()

        T = self.theme
        T.banner(dlg, "Manage Ghidra Installations",
                 "Select a version to remove it or show it in Finder.",
                 title_size=16).pack(fill="x")
        body = ttk.Frame(dlg, padding=(20, 14, 20, 16))
        body.pack(fill="both", expand=True)

        # Treeview
        tv_frame = ttk.Frame(body)
        tv_frame.pack(fill="both", expand=True)
        cols = ("version", "date", "path")
        tree = ttk.Treeview(tv_frame, columns=cols,
                            show="headings", height=7, selectmode="browse")
        tree.heading("version", text="Version")
        tree.heading("date",    text="Installed")
        tree.heading("path",    text="Location")
        tree.column("version", width=90,  stretch=False, anchor="center")
        tree.column("date",    width=90,  stretch=False, anchor="center")
        tree.column("path",    width=400, stretch=True)

        vsb = ttk.Scrollbar(tv_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        # Status bar
        status_var = tk.StringVar(value="")
        _SemLabel(body, textvariable=status_var, role="secondary",
                  font=T.f_small, anchor="w").pack(fill="x", pady=(8, 0))

        installs: list = []

        def _refresh():
            nonlocal installs
            installs = _read_installs()
            tree.delete(*tree.get_children())
            for e in installs:
                ver    = e.get("version", "?")
                date   = e.get("date",    "—")
                path   = e.get("app_path", "—")
                exists = Path(path).exists() if path else False
                tree.insert("", "end", values=(ver, date, path),
                            tags=("ok" if exists else "missing",))
            tree.tag_configure("missing", foreground=T.color("red"))
            n = len(installs)
            status_var.set(
                f"{n} version{'s' if n != 1 else ''} installed"
                if n else "No versions tracked.")

        _refresh()

        def _selected():
            sel = tree.selection()
            if not sel:
                status_var.set("Please select a version first.")
                return None
            return installs[tree.index(sel[0])]

        def _reveal():
            entry = _selected()
            if not entry:
                return
            path = Path(entry.get("app_path", ""))
            if path.exists():
                subprocess.run(["open", "-R", str(path)])
            else:
                status_var.set(f"Not found on disk: {path}")

        def _remove():
            entry = _selected()
            if not entry:
                return
            ver  = entry.get("version", "unknown")
            path = Path(entry.get("app_path", ""))

            msg = f"Remove Ghidra {ver}?"
            if path.exists():
                msg += f"\n\n{path}\n\nThis will move the .app to the Trash."
            else:
                msg += f"\n\n(App not found at: {path})\n\nRemove from tracking list?"

            if not messagebox.askyesno(
                    f"Remove Ghidra {ver}", msg,
                    icon="warning", parent=dlg):
                return

            errors = []

            if path.exists():
                try:
                    r = subprocess.run(
                        ["osascript", "-e",
                         f'tell application "Finder" to delete POSIX file "{path}"'],
                        capture_output=True, timeout=10)
                    if r.returncode != 0:
                        shutil.rmtree(path)
                    self._log(f"Removed: {path}")
                except Exception as exc:
                    errors.append(str(exc))

            old_alias = Path(entry.get("install_dir", "")) / "Ghidra.app"
            if old_alias != path and old_alias.exists():
                try:
                    subprocess.run(
                        ["osascript", "-e",
                         f'tell application "Finder" to delete POSIX file "{old_alias}"'],
                        capture_output=True, timeout=10)
                    self._log(f"Removed old alias: {old_alias}")
                except Exception:
                    pass

            remaining = [e for e in _read_installs()
                         if e.get("app_path") != entry.get("app_path")]
            _write_installs(remaining)

            _refresh()
            self._load_installed_version()

            if not remaining:
                self._cleanup_ghidra_if_empty()

            if errors:
                status_var.set(f"Removed with warnings: {'; '.join(errors)}")
            else:
                status_var.set(f"Ghidra {ver} removed ✓")

        # Buttons
        bf = ttk.Frame(body)
        bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Remove…",
                   command=_remove).pack(side="left")
        ttk.Button(bf, text="Done", default="active",
                   command=dlg.destroy).pack(side="right")
        ttk.Button(bf, text="Show in Finder",
                   command=_reveal).pack(side="right", padx=(0, 8))
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        # Centre over main window
        def _place_dlg(tries=0):
            dlg.update_idletasks()
            dw = max(620, dlg.winfo_reqwidth())
            dh = max(dlg.winfo_reqheight(), dlg.winfo_height())
            if dh < 250 and tries < 20:
                dlg.after(50, lambda: _place_dlg(tries + 1))
                return
            mx = self.winfo_x() + (self.winfo_width()  - dw) // 2
            my = self.winfo_y() + (self.winfo_height() - dh) // 3
            dlg.minsize(620, 0)
            dlg.geometry(f"+{max(0, mx)}+{max(0, my)}")
        dlg.after(0, _place_dlg)

    def _finish(self, app_path: Path):
        self._set_status("Installation complete.", "green")
        self._set_progress(100)
        self._set_detail(str(app_path))
        self.btn_install.configure(state="normal", text="Reinstall")
        self.btn_cancel.configure(state="disabled")
        self.entry_dir.configure(state="normal")

        answer = messagebox.askyesno(
            "Installation Complete",
            f"{app_path.name} has been installed to:\n\n{app_path}\n\nReveal in Finder?")
        if answer:
            subprocess.run(["open", "-R", str(app_path)])


# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if _DEBUG:
        import faulthandler
        # If startup hangs, dump every thread's stack after 10 s
        faulthandler.dump_traceback_later(10, repeat=False)
    app = GhidraInstaller()
    _dbg("entering mainloop")
    if _DEBUG:
        import faulthandler
        faulthandler.cancel_dump_traceback_later()
    app.mainloop()
