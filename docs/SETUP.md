# Builders Truss QR — Setup Guide

This guide covers installation, first-time configuration, and multi-machine
network setup for Builders Truss QR.

---

## Requirements

- **Windows 10 or 11** (the app uses Windows-only APIs)
- **Python 3.10 or later** — [python.org](https://www.python.org/downloads/)
  - During install, check **"Add Python to PATH"**
- Network share access (for multi-machine use)

---

## Installation

1. Copy or clone the `QR-Code-Truss-Tracking` folder to each machine that will run the app.
   A local path like `C:\BuildersTrussQR\` works well.

2. Open a command prompt in that folder and create a virtual environment:

   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. Run the app:

   ```cmd
   python BuildersQRLabels.py
   ```

   You can create a desktop shortcut pointing to `python BuildersQRLabels.py`
   with the working directory set to the app folder.

---

## Single-Machine Setup

On first launch, the app creates a local `builders_qr_labels.db` and an empty
`config.json` in the app folder.

1. Click **Jobs Folder** → choose the Local Jobsite Package Folder where job subfolders live
2. Click **Watch Folder** → choose the folder where incoming packages arrive
3. Open **⚙ Settings** → configure your cloud provider (see [Cloud Provider Setup](#cloud-provider-setup))
4. Click **Validate** to scan

That's it for single-machine use.

---

## Multi-Machine Network Setup

When multiple people need to see the same job status in real time, store the
shared database on a network share that all users can reach.

### Step 1 — Create the shared folder (IT)

On the file server, create:

```
\\SERVER\BuildersQRLabels\
```

Give every user account **Read + Write** access to this folder.

### Step 2 — First machine (admin)

1. Open **⚙ Settings**
2. Under **Shared Database**, set **DB Path** to:
   ```
   \\SERVER\BuildersQRLabels\builders_qr_labels.db
   ```
   Click the status indicator — it should show **Connected**.
3. Set **Watch Folder**, **Local Jobsite Package Folder**, and **Log Path**:
   ```
   Watch Folder:                  \\SERVER\Jobs\Watch
   Local Jobsite Package Folder:  \\SERVER\Jobs\LocalFinals
   Log Path:                      \\SERVER\BuildersQRLabels\builders_qr_labels.log
   ```
4. Click **Save**

These folder paths are written to the shared database so all other machines
pick them up automatically.

### Step 3 — Every other machine

1. Open **⚙ Settings**
2. Set only **DB Path**:
   ```
   \\SERVER\BuildersQRLabels\builders_qr_labels.db
   ```
3. Click **Save**

Watch Folder, Local Jobsite Package Folder, and Log Path are read from the
shared database — no manual entry needed.

### How sync works

- Every 30 seconds (configurable in Settings), the app re-reads the shared DB
  and updates any rows whose status changed on another machine.
- When User A generates stickers, User B's job list turns green within one
  refresh cycle — no manual refresh required.
- If the network path is unreachable, the app falls back to a local database
  and shows a warning. No crash or data loss.

---

## Cloud Provider Setup

### Dropbox

1. Go to [dropbox.com/developers](https://www.dropbox.com/developers) and create an app:
   - **API**: Scoped Access
   - **Access type**: Full Dropbox
   - Give it a name (e.g. `BuildersTrussQR`)
2. Note the **App Key** and **App Secret** from the app's Settings page
3. In Builders Truss QR → **⚙ Settings** → **Dropbox**:
   - Enter the App Key and App Secret
   - Click **Authenticate with Dropbox**
   - A browser window opens; log in and approve
   - Paste the confirmation code back into the dialog
4. Status should show **✓ Authenticated**

Tokens are saved to `token_dropbox.json` (next to the app). Re-authentication
is only needed if the token expires or is revoked.

### OneDrive

OneDrive requires an Azure App Registration. Ask IT to complete these steps once:

#### IT Setup (one-time, ~5 minutes)

1. Sign in to [portal.azure.com](https://portal.azure.com) with an admin account
2. Go to **Microsoft Entra ID → App registrations → New registration**:
   - Name: `Builders Truss QR`
   - Supported account types: `Accounts in this organizational directory only`
   - No redirect URI needed
3. After registration, go to **API permissions → Add a permission → Microsoft Graph → Delegated**:
   - Add `Files.ReadWrite`
   - Add `offline_access`
   - *(Optional — for SharePoint/shared drives)*: Add `Sites.ReadWrite.All` and click **Grant admin consent**
4. Note the **Application (client) ID** and the **Directory (tenant) ID** from the Overview page

#### User Setup

1. In Builders Truss QR → **⚙ Settings** → **OneDrive**:
   - Enter the **Client ID** (Application ID from step 4 above)
   - Enter the **Tenant ID** (or leave as `common` for personal Microsoft accounts)
   - Click **Authenticate with OneDrive**
2. A dialog shows a code and a URL (`https://microsoft.com/devicelogin`):
   - Open that URL in a browser
   - Enter the code
   - Sign in with your Microsoft account
3. Status shows **✓ Authenticated**

Tokens are saved to `token_onedrive.json`. Users authenticate with their own
Microsoft account — no shared password or secret needed.

---

## config.json Reference

`config.json` is created automatically in the app folder on first run. It stores
per-machine settings only. **Do not commit this file to source control** — it
contains credentials.

```json
{
  "db_path":             "\\\\SERVER\\BuildersQRLabels\\builders_qr_labels.db",
  "cloud_provider":      "dropbox",
  "dropbox_app_key":     "your_app_key",
  "dropbox_app_secret":  "your_app_secret",
  "onedrive_client_id":  "",
  "onedrive_tenant_id":  "common",
  "company_name":        "Builders Inc.",
  "company_address":     "17600 E Smith Rd. Aurora, CO 80011"
}
```

| Key | Description |
|---|---|
| `db_path` | Path to the shared SQLite DB. Leave empty to use a local DB. |
| `cloud_provider` | `"dropbox"` or `"onedrive"` |
| `dropbox_app_key` / `dropbox_app_secret` | From your Dropbox app registration |
| `onedrive_client_id` | Application ID from Azure |
| `onedrive_tenant_id` | Azure tenant ID, or `"common"` for any Microsoft account |
| `company_name` / `company_address` | Printed on every sticker |

Shared settings (Watch Folder, Local Jobsite Package Folder, auto-refresh interval, log path)
are stored in the database `settings` table, not here.

---

## Files Created at Runtime

These files are auto-generated and should be **gitignored** (they already are):

| File | Purpose |
|---|---|
| `config.json` | Per-machine settings and credentials |
| `token_dropbox.json` | Dropbox OAuth tokens |
| `token_onedrive.json` | OneDrive OAuth tokens (MSAL cache) |
| `builders_qr_labels.db` | Local fallback database (when network is unavailable) |
| `*.log` | Log files |

---

## Folder Structure Requirements

### Local Jobsite Package Folder

Each job lives in a subfolder named exactly `XXXXXX-XXX` (6 digits, hyphen, 3 digits):

```
Local Jobsite Package Folder\
└── 208135-001\
    ├── 208135-001.csv                   ← required; jobnumber column = "208135-001"
    ├── 208135-001_QR.svg                ← generated after first upload
    ├── Stickers_208135-001.pdf          ← generated by the app
    └── 208135-001_Stickers_Summary.txt  ← generated by the app
```

### Watch Folder

Incoming packages are placed anywhere under the Watch folder inside a subfolder
named `_Final Jobsite Package QR`:

```
Watch Folder\
└── [any subfolder]\
    └── _Final Jobsite Package QR\
        ├── 208135-001.csv
        └── 208135-001_SomeName.pdf
```

Files whose names match `208135-001_JOB_*.pdf` are automatically excluded
from cloud uploads.

### The CSV must be named {JOBNUMBER}.csv with these columns (case-insensitive):

| Column	| Type	| Purpose
|---|---|
jobnumber|	int|	Row filter: df[df['jobnumber'] == int(job_id)]|
trsname|	str|	Truss code (large text on sticker)|
trusstype|	str|	Truss type description|
batch|	str|	Batch grouping (last 2 chars = batch code)|
customer|	str|	Client name (truncated to 25 chars)|
jobname|	str|	Project name (truncated to 25 chars)|
qty|	int|	Stickers per truss|
ply|	int|	If > 1: total = qty × ply; if == 1: total = qty|


---

## Troubleshooting

| Symptom | Check |
|---|---|
| Job list is empty | Verify Local Jobsite Package Folder is set and contains `XXXXXX-XXX` subfolders |
| "No rows for job X found in CSV" | Confirm the `jobnumber` column in the CSV matches the folder name exactly (e.g. `208135-001`) |
| Cloud status stuck at Pending | Verify cloud provider is authenticated in Settings |
| DB Path shows "Unavailable" | Confirm the network share is reachable and the path is correct |
| Sticker PDF is blank / wrong | Check that the CSV has a `trsname` column with truss codes |
| Authentication keeps failing (OneDrive) | Confirm IT has registered the app and granted `Files.ReadWrite` permission |

For persistent errors, click **⚠ View Log** in the status bar and check for
`[ERROR]` or `[EXCEPTION]` entries.
