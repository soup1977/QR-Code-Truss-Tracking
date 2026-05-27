# Builders Truss QR — Deployment Guide

## Overview

The build produces a portable ZIP (no installer required). Deployment involves:
1. Building on the dev machine
2. Copying two files to the network share
3. Extracting the ZIP on each target machine (first install or update)

---

## Part 1 — Build Machine Setup (one-time)

Requirements before the first build:

| Requirement | Command |
|---|---|
| Python 3.10+ | `python --version` |
| Virtual environment | `python -m venv .venv` |
| Dependencies | `.venv\Scripts\pip install -r requirements.txt` |
| PyInstaller | `.venv\Scripts\pip install pyinstaller` |
| Inno Setup 6 (optional) | https://jrsoftware.org/isinfo.php |

Update `SHARE_PATH` in `build.bat` to match the folder containing the database:

```bat
set SHARE_PATH=X:\PROJECT\QRCodes\Database\
```

This must be the same directory as `db_path` in `config.json` on all machines —
that is where the app looks for `version.json` to check for updates.

---

## Part 2 — Releasing a New Version

### Step 1 — Bump the version number

Edit these two files, both must match:

**`build.bat`** (line 22):
```bat
set VERSION=0.9.1
```

**`BuildersQRLabels.py`** (line 11):
```python
__version__ = "0.9.1"
```

Commit the version bump to the repo before building.

---

### Step 2 — Run the build

From the project root:

```
build.bat
```

The four steps it runs:

| Step | What it does |
|---|---|
| [1/4] PyInstaller | Bundles the app into `dist\BuildersQRLabels\` |
| [2/4] Inno Setup | Builds `Output\BuildersTrussQR-X.X.X-Setup.exe` (skipped if not installed) |
| [3/4] ZIP | Zips the bundle to `Output\BuildersTrussQR-X.X.X-Portable.zip` |
| [4/4] version.json | Generates `Output\version.json` with the new version and ZIP path |

---

### Step 3 — Edit release notes

Open `Output\version.json` and fill in the `release_notes` field:

```json
{
    "version":  "0.9.1",
    "installer_path":  "\\\\SERVER\\QRCodes\\Database\\BuildersTrussQR-0.9.1-Portable.zip",
    "release_notes":  "Bug fixes and performance improvements"
}
```

Keep release notes short — they appear in the update banner inside the app.

---

### Step 4 — Copy to the network share

Copy both files to the share path (`SHARE_PATH` in `build.bat`):

```
Output\BuildersTrussQR-0.9.1-Portable.zip  →  X:\PROJECT\QRCodes\Database\
Output\version.json                         →  X:\PROJECT\QRCodes\Database\
```

> **Note:** Overwriting `version.json` is what triggers the update notification
> for all running instances on their next app launch. Copy the ZIP first, then
> `version.json` — so users are never prompted before the ZIP is available.

---

## Part 3 — Installing on a Machine (First Install)

### Step 1 — Copy and extract the ZIP

1. Navigate to the network share (`X:\PROJECT\QRCodes\Database\`)
2. Copy `BuildersTrussQR-X.X.X-Portable.zip` to the target machine
3. Extract it to a permanent location, e.g.:
   ```
   C:\Program Files\BuildersTrussQR\
   ```
4. Run `BuildersQRLabels.exe` from the extracted folder

> The app can run from any local folder. A network path is not recommended
> (slower startup, fails when offline).

### Step 2 — Create config.json (first run on a new machine)

On first launch the app will prompt for settings. Alternatively, create
`config.json` next to `BuildersQRLabels.exe` before running:

```json
{
  "db_path": "X:\\PROJECT\\QRCodes\\Database\\builders_qr_labels.db",
  "cloud_provider": "onedrive",
  "onedrive_client_id": "",
  "onedrive_tenant_id": "common",
  "company_name": "Builders Inc.",
  "company_address": "17600 E. Smith Rd. Aurora, CO. 80011"
}
```

> `db_path` is the only setting that must point to the correct network share —
> all other shared settings (jobs folder, refresh interval, log path) are read
> from the database `settings` table automatically.

### Step 3 — Create a desktop shortcut (optional)

Right-click `BuildersQRLabels.exe` → Send to → Desktop (create shortcut).

---

## Part 4 — Updating an Existing Installation

When a new version is deployed to the network share, the app shows a yellow
banner at the bottom on next launch:

> **Update available: v0.9.1 — Bug fixes**  `[Open Installer Folder]`  `[Dismiss]`

To update:

1. Click **Open Installer Folder** — opens the network share folder
2. Copy the new `BuildersTrussQR-X.X.X-Portable.zip` to the target machine
3. Close the running app
4. Extract the ZIP, overwriting the existing installation folder
5. Relaunch `BuildersQRLabels.exe`

> `config.json` lives next to the `.exe` and is not overwritten by the ZIP —
> user settings are preserved across updates.

---

## Part 5 — Network Share Layout

```
X:\PROJECT\QRCodes\Database\
├── builders_qr_labels.db          ← shared SQLite database (all machines)
├── version.json                   ← update manifest (maintained by dev)
├── BuildersTrussQR-0.9.1-Portable.zip   ← current release
└── BuildersTrussQR-0.9.0-Portable.zip   ← previous release (keep for rollback)
```

Keep one previous release on the share for quick rollback if needed.

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| App won't launch — Defender blocks it | Unsigned executable | See IT to whitelist, or add folder exclusion in Defender |
| Update banner never appears | `version.json` missing or wrong path | Confirm `db_path` in `config.json` points to the share; check `version.json` exists there |
| App can't reach database | Network share offline or unmapped | Check drive mapping; app falls back to local DB silently |
| `version.json` has wrong path | `SHARE_PATH` in `build.bat` is wrong | Update `SHARE_PATH` and rebuild |
