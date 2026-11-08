# Ghidra Installer

A lightweight, standalone GUI installer for [Ghidra](https://ghidra-sre.org/) — the NSA's open-source software reverse engineering framework. Available for **macOS** and **Windows**. No package manager, no dependencies, no admin rights required.

---

## Features

### Both platforms
- Fetches the latest (or any selected) Ghidra release directly from the official GitHub repository
- Automatically downloads and installs **Temurin JDK 21** if Java is missing or out of date
- Multi-version support — install and manage several Ghidra versions side by side
- Version picker combobox with **Download & Install** / **Reinstall** button that reflects actual install state
- Real-time download progress bar with speed and size readout
- Dark mode support (detected automatically from system preference)
- Uninstall manager — remove any tracked version, clean up metadata automatically
- All install metadata stored locally; no telemetry, no internet use beyond GitHub and Adoptium APIs

### macOS
- Wraps Ghidra in a native **`.app` bundle** with correct `Info.plist`, icon (`.icns`), and executable permissions
- Builds Ghidra's **native macOS components** automatically for releases that require it (Ghidra 12.1+)
- **Silent Rosetta 2 install** on Apple Silicon when running an Intel-only Ghidra build
- **Silent Xcode Command Line Tools install** via `softwareupdate` — no Finder or Automation permission prompt required; falls back gracefully to the interactive Apple dialog if the catalog lookup fails
- Extracts archives with `bsdtar` when available to preserve all Unix permission bits; falls back to Python's `zipfile` module
- Native `aqua` widget theme; respects macOS Dark Mode
- Reveal in Finder button in the uninstall manager
- Trash-based removal (no `tell application "Finder"` — no Automation permission dialog)

### Windows
- Creates a **batch launcher** and a **VBScript wrapper** so Ghidra opens without a visible CMD window
- Optional **Desktop shortcut** and **Start Menu entry** (both toggleable before install)
- Shell notification (`SHChangeNotify`) so shortcuts appear immediately without a logoff
- **Repair Shortcuts** button in the uninstall manager for broken `.lnk` files
- Dark mode detected via Windows registry (`AppsUseLightTheme`)
- No admin rights needed — installs to `%LOCALAPPDATA%` by default

---

## Requirements

| | macOS | Windows |
|---|---|---|
| Python | 3.8+ | 3.8+ |
| Third-party packages | None | None |
| Java | Auto-installed (Temurin 21) | Auto-installed (Temurin 21) |
| Xcode CLT | Auto-installed silently | — |
| Rosetta 2 | Auto-installed (Apple Silicon) | — |

---

## Usage

**macOS**
```bash
python3 ghidra_installer_macOS.py
```

**Windows**
```bash
python ghidra_installer_windows.py
```

No `pip install` required. The scripts use only Python standard library modules.

---

## What gets installed

### macOS
| Item | Location |
|---|---|
| Ghidra `.app` bundle | `/Applications` (default, choosable) |
| Temurin JDK 21 | `~/Library/Java/JavaVirtualMachines/temurin-21.jdk` |
| Install metadata | `~/Library/Application Support/GhidraInstaller/install_info.json` |

### Windows
| Item | Location |
|---|---|
| Ghidra folder | `%LOCALAPPDATA%\Ghidra` (default, choosable) |
| Temurin JDK 21 | `%LOCALAPPDATA%\GhidraInstaller\jdk` |
| Install metadata | `%APPDATA%\GhidraInstaller\install_info.json` |
| Desktop shortcut | `%USERPROFILE%\Desktop\Ghidra <version>.lnk` |
| Start Menu entry | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\` |

---

## Uninstalling

Open the installer and click **Uninstall…** to open the manage dialog. Select a version and click **Remove** — the app is moved to Trash (macOS) or deleted (Windows), shortcuts are removed, and install metadata is cleaned up. The metadata file and support directory are deleted automatically when no tracked installs remain.

---

## SSL / network notes

The installer handles SSL gracefully across all common environments:

- **macOS** — tries `certifi`, then common system CA paths, then Python's default context, then falls back to an unverified context (connection still encrypted)
- **Windows** — in a frozen bundle, loads the Windows Root CA store first (handles corporate proxies and custom CAs that `certifi` does not include); falls back to `certifi` then unverified context

---

## Packaging as a standalone app

Both scripts are compatible with **PyInstaller**. The macOS version includes SSL cert path handling for PyInstaller bundles (`sys._MEIPASS`).

---

## Disclaimer

This installer is an unofficial third-party tool. Ghidra is developed and maintained by the [National Security Agency](https://github.com/NationalSecurityAgency/ghidra). Temurin JDK is provided by [Eclipse Adoptium](https://adoptium.net/). This project is not affiliated with either.
