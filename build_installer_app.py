#!/usr/bin/env python3
"""
build_installer_app.py
Builds a fully self-contained GhidraInstaller.app using PyInstaller.



# --- Robust Ghidra icon lookup ---
import json
import urllib.request

GITHUB_REPO = "NationalSecurityAgency/ghidra"
KNOWN_PATHS = ["Ghidra/RuntimeScripts/support/ghidra.ico"]
The resulting .app bundles its own Python interpreter and tkinter/Tcl-Tk,
so end users need NO system Python, NO Rosetta, and NO external dependencies.

Requirements (build machine only — not needed at runtime):
    python3 build_installer_app.py        # PyInstaller auto-installed if missing

Output: GhidraInstaller.app  (alongside this script)
"""

import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE       = Path(__file__).parent.resolve()
SRC_SCRIPT = HERE / "ghidra_installer_macOS.py"
APP_NAME   = "GhidraInstaller"
APP_OUT    = HERE / f"{APP_NAME}.app"
BUILD_DIR  = HERE / "_pyinstaller_build"

# ── Sanity checks ─────────────────────────────────────────────────────────────
if not SRC_SCRIPT.exists():
    sys.exit(f"ERROR: {SRC_SCRIPT} not found — run from the Ghidra folder")

import platform as _plat
_arch = _plat.machine()
print(f"Builder Python : {sys.version.split()[0]}  ({_arch})")
print(f"Output         : {APP_OUT}")
print()

# ── Ensure PyInstaller is available ──────────────────────────────────────────
print("Checking PyInstaller… ", end="", flush=True)
try:
    import PyInstaller as _pyi
    print(f"found ({_pyi.__version__})")
except ImportError:
    print("not installed — installing…")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet", "pyinstaller"])
    print("PyInstaller installed.")

# ── Download Ghidra icon + convert to .icns ──────────────────────────────────
_ICO_URL = ("https://raw.githubusercontent.com/NationalSecurityAgency/ghidra/refs/heads/master/Ghidra/RuntimeScripts/support/ghidra.ico")


def _make_icns(dest_dir: Path):
    """Download ghidra.ico → Ghidra.icns using sips + iconutil."""
    with tempfile.TemporaryDirectory() as _tmp:
        tmp      = Path(_tmp)
        ico_file = tmp / "ghidra.ico"
        base_png = tmp / "base.png"
        iconset  = tmp / "Ghidra.iconset"
        icns_out = dest_dir / "Ghidra.icns"

        print("Downloading Ghidra icon… ", end="", flush=True)
        try:
            urllib.request.urlretrieve(_ICO_URL, ico_file)
            print("OK")
        except Exception as e:
            print(f"FAILED ({e}) — app will use default icon")
            return None

        r = subprocess.run(["sips", "-s", "format", "png", str(ico_file),
                            "--out", str(base_png)], capture_output=True)
        if r.returncode != 0 or not base_png.exists():
            print("  sips ICO→PNG failed — skipping icon")
            return None

        iconset.mkdir()
        for px, name in [
            (16,  "icon_16x16.png"),
            (32,  "icon_16x16@2x.png"),
            (32,  "icon_32x32.png"),
            (64,  "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
        ]:
            subprocess.run(["sips", "-z", str(px), str(px),
                            str(base_png), "--out", str(iconset / name)],
                           capture_output=True)

        r = subprocess.run(["iconutil", "-c", "icns",
                            str(iconset), "-o", str(icns_out)],
                           capture_output=True)
        if r.returncode == 0 and icns_out.exists():
            print(f"  Icon: Ghidra.icns ({icns_out.stat().st_size // 1024} KB)")
            return icns_out
        print("  iconutil failed — skipping icon")
        return None


icns_path = _make_icns(HERE)

# ── Clean previous build artefacts ───────────────────────────────────────────
for path in (APP_OUT, BUILD_DIR):
    if path.exists():
        print(f"Removing {path.name}…")
        shutil.rmtree(path)
for spec in HERE.glob("*.spec"):
    spec.unlink()

# ── Run PyInstaller ──────────────────────────────────────────────────────────────────
print(f"\nBuilding {APP_NAME}.app with PyInstaller…")
print("(Bundles Python + tkinter — ~30-60 s on first run)\n")

# Use a temp directory for PyInstaller's dist output to avoid path-with-spaces
# issues on machines where the project folder contains spaces.
import tempfile as _tf
_tmp_dist = _tf.mkdtemp(prefix="ghidra_pyi_dist_")
_tmp_work = _tf.mkdtemp(prefix="ghidra_pyi_work_")

try:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",           # proper .app bundle structure
        "--windowed",         # GUI app — no terminal window attached
        "--clean",
        "--name", APP_NAME,
        "--distpath", _tmp_dist,   # temp path — no spaces issues
        "--workpath", _tmp_work,
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--hidden-import", "tkinter.font",
        "--hidden-import", "tkinter.scrolledtext",
        "--hidden-import", "ssl",
        "--hidden-import", "urllib.request",
        "--hidden-import", "urllib.error",
        "--hidden-import", "traceback",
        "--collect-all", "tkinter",
    ]
    if icns_path and icns_path.exists():
        cmd += ["--icon", str(icns_path)]
    cmd.append(str(SRC_SCRIPT))

    # Run PyInstaller — output goes directly to the terminal/IDE console
    result = subprocess.run(cmd, cwd=str(HERE))

    if result.returncode != 0:
        sys.exit(f"\nERROR: PyInstaller exited with code {result.returncode}.\n"
                 "Check the output above for the specific failure.")

    # Move the finished .app from temp dist to the project folder
    built_app = Path(_tmp_dist) / f"{APP_NAME}.app"
    if not built_app.exists():
        sys.exit(f"ERROR: expected {built_app} — not found after build")
    if APP_OUT.exists():
        shutil.rmtree(APP_OUT)
    shutil.move(str(built_app), str(APP_OUT))

finally:
    shutil.rmtree(_tmp_dist, ignore_errors=True)
    shutil.rmtree(_tmp_work, ignore_errors=True)

# ── Tidy up build leftovers ────────────────────────────────────────────────────────────
if BUILD_DIR.exists():
    shutil.rmtree(BUILD_DIR)
for spec in HERE.glob("*.spec"):
    spec.unlink()
if icns_path and icns_path.exists():
    icns_path.unlink()

# ── Strip extended attributes + ad-hoc sign ────────────────────────────────────────
# xattr -rc removes ALL extended attrs (resource forks, quarantine, Finder info)
# which would otherwise cause codesign to fail with "detritus not allowed".
print("Stripping extended attributes… ", end="", flush=True)
subprocess.run(["xattr", "-rc", str(APP_OUT)], capture_output=True)
print("done")

print("Ad-hoc code signing… ", end="", flush=True)
r = subprocess.run(
    ["codesign", "-s", "-", "--force", "--all-architectures",
     "--deep", str(APP_OUT)],
    capture_output=True, text=True)
if r.returncode == 0:
    print("done")
else:
    print(f"warning: {r.stderr.strip()[:120]}")

# ── Refresh Finder icon cache ─────────────────────────────────────────────────
subprocess.run(["touch", str(APP_OUT)], capture_output=True)
subprocess.run([
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister",
    "-f", str(APP_OUT),
], capture_output=True)

# ── Done ──────────────────────────────────────────────────────────────────────
print(f"\n✓  Built: {APP_OUT}")
print(f"   Architecture : {_arch}  (no Rosetta needed on Apple Silicon)")
print("   Self-contained: no system Python or tkinter required at runtime.")
print(f"\n   Distribute by copying {APP_NAME}.app — that single bundle is all users need.")

try:
    subprocess.run(["open", "-R", str(APP_OUT)])
except Exception:
    pass
