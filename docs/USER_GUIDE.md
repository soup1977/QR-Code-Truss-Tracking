# Builders Connect — User Guide

**Builders Connect** is the QR label and cloud sync manager for Builders Inc.
It handles the full job workflow: validating job folders, generating QR sticker
PDFs, and uploading completed job packages to cloud storage.

---

## The Main Window

```
┌──────────────────────────────────────────────────────────────────────────┐
│ [Jobs Folder] [Watch Folder] [Validate] [Generate] [Upload] [Sync All] [⚙ Settings] │
├──────────────────────────────────────────────────────────────────────────┤
│ [Search...                                                   ] [🔍] [✕]  │
├──────────────────────────────────────────────────────────────────────────┤
│ Job ID      │ Stickers │ Sticker Status │ Cloud Status  │  %   │ Timestamp  │
│ 208135-001  │    42    │   Pending      │   Local       │  50% │ 10:30      │
│ 208136-002  │    18    │   Completed    │   Uploaded    │ 100% │ 10:28      │
├──────────────────────────────────────────────────────────────────────────┤
│ ◉ Dropbox   Last scan: 10:31   [12 jobs]        [⚠ 1 error — View Log]  │
└──────────────────────────────────────────────────────────────────────────┘
```

### Toolbar Buttons

| Button | What it does |
|---|---|
| **Jobs Folder** | Set the Local Jobsite Package Folder — where job subfolders live (e.g. `\\SERVER\Jobs\LocalFinals`) |
| **Watch Folder** | Set the Watch folder — where incoming job packages arrive from MiTek (e.g. `\\SERVER\Jobs\Watch`) |
| **Validate** | Scan all folders and refresh the job list (also `F5`) |
| **Generate** | Generate sticker PDFs for the selected Pending jobs |
| **Upload** | Upload the selected Completed jobs to cloud storage |
| **Sync All** | Generate all Pending jobs, then upload all eligible Completed jobs in one step |
| **⚙ Settings** | Open the Settings dialog to configure cloud credentials, folders, and more |

### Job List Columns

| Column | Meaning |
|---|---|
| **Job ID** | Job number in `XXXXXX-XXX` format (e.g. `208135-001`) |
| **Stickers** | Number of stickers to be printed, or a short error note |
| **Sticker Status** | Whether sticker PDFs have been generated (`Pending` / `Completed` / `Error`) |
| **Cloud Status** | Whether the job has been uploaded to cloud storage (see table below) |
| **%** | Upload progress for jobs currently being transferred |
| **Timestamp** | When this job's status was last updated |

### Row Colors

| Color | Meaning |
|---|---|
| Green | Stickers complete **and** uploaded to cloud — fully done |
| Yellow | Stickers pending or completed but not yet uploaded |
| Blue | Job exists in cloud only (not local), or currently uploading/generating |
| Red/Pink | An error occurred — hover over the row or check the log |

### Status Bar (bottom)

- **Cloud indicator** — shows which provider is active (Dropbox or OneDrive)
- **Last scan** — time of the most recent folder scan
- **Job count** — total jobs currently visible
- **⚠ N errors — View Log** — appears when errors have occurred; click to open the log viewer

---

## First-Time Setup

1. Click **Jobs Folder** and choose your Local Jobsite Package Folder
   (where individual job subfolders like `208135-001\` live)
2. Click **Watch Folder** and choose your Watch folder
   (where incoming `_Final Jobsite Package QR` packages arrive from MiTek)
3. Click **⚙ Settings**, enter your cloud credentials, and click **Authenticate**
4. Click **Validate** to scan and populate the job list

> **Note:** If your office uses a shared network database, an admin will have already
> configured the folders. You only need to set the DB Path in Settings once — all
> other paths will sync automatically.

---

## Daily Workflow

### 1 — Validate (scan for new jobs)

Click **Validate** or press `F5` at any time to rescan the folders. The job list
refreshes automatically every 30 seconds in the background.

### 2 — Generate Stickers

1. Select one or more jobs with **Pending** sticker status (hold `Ctrl` or `Shift`
   to multi-select; `Ctrl+A` selects all)
2. Click **Generate** or press `Ctrl+G`
3. The row turns blue while generating, then green/yellow when complete
4. Sticker PDFs are saved to the job folder:
   - `Stickers_208135-001.pdf` — the printable sticker sheet
   - `208135-001_Stickers_Summary.txt` — a record of all stickers and tokens

### 3 — Upload to Cloud

1. Select one or more jobs with **Completed** sticker status
2. Click **Upload** or press `Ctrl+U`
3. Files are uploaded to the configured cloud folder
4. A QR code SVG (`208135-001_QR.svg`) is generated from the share link and saved
   locally and in the cloud
5. Cloud Status changes to **Uploaded** and the row turns green

### 4 — Sync All (one-click)

Press `Ctrl+Shift+S` or click **Sync All** to:
- Generate stickers for every Pending job
- Upload every Completed-but-not-uploaded job to cloud

Use this at the end of the day or when you want to catch everything up at once.

---

## Right-Click Menu

Right-click any job row to access:

| Option | When available |
|---|---|
| Open Job Folder | Always — opens the local folder in Explorer |
| Revalidate Job | Always — rescans just that one job |
| Generate Stickers | When Sticker Status is Pending or Error |
| Upload to Cloud | When Cloud Status is Local |
| Download from Cloud | When Cloud Status is Cloud Only |
| Open Cloud Link | Always — opens the shared folder link in a browser |
| Copy Job ID | Always — copies `208135-001` to the clipboard |
| Refresh | Always — same as Validate |

**Double-click** any row to open that job's folder directly in Windows Explorer.

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `F5` | Validate / Refresh |
| `Ctrl+G` | Generate stickers for selection |
| `Ctrl+U` | Upload selection to cloud |
| `Ctrl+Shift+S` | Sync All |
| `Ctrl+,` | Open Settings |
| `Ctrl+A` | Select all jobs |
| `Esc` | Clear selection |

---

## Job Status Reference

### Sticker Status

| Status | Meaning |
|---|---|
| **Pending** | CSV and QR SVG are present; sticker PDF has not been generated yet |
| **Completed** | `Stickers_XXXXXX-XXX.pdf` exists in the job folder |
| **Error** | Something went wrong during generation — check the log |

### Cloud Status

| Status | Meaning |
|---|---|
| **Uploaded** | Job has been uploaded and a share link exists |
| **Local** | Job folder is present locally but not yet uploaded |
| **Pending** | Job exists in Watch folder waiting to be copied/uploaded |
| **Cloud Only** | Job exists in cloud storage but not on this machine |
| **Incomplete** | Job folder is present but missing required files |
| **CSV Only** | Only the CSV file is present; QR SVG is missing |
| **Error** | Cloud check failed — see the log |

---

## Settings Dialog

Open with **⚙ Settings** or `Ctrl+,`.

| Section | What to configure |
|---|---|
| **Cloud Provider** | Choose Dropbox or OneDrive, enter credentials, and authenticate |
| **Shared Database** | Path to the shared SQLite DB (see [SETUP.md](SETUP.md)); Watch folder, Local Jobsite Package Folder, and Log Path |
| **Company** | Company name and address printed on every sticker |
| **Auto-refresh** | How often (in seconds) the app re-reads the shared database |

Changes take effect immediately after clicking **Save**.

---

## Viewing the Error Log

If the status bar shows **⚠ N errors — View Log**, click it to open a read-only
window showing the full contents of the log file. The log path is configured in
Settings → Shared Database → Log Path.

---

## Required Folder Structure

The app expects job folders to follow these naming and file conventions:

**Local Jobsite Package Folder** (where the app generates stickers and uploads from):

```
Local Jobsite Package Folder\
└── 208135-001\
    ├── 208135-001.csv           ← copied from Watch folder; required for sticker generation
    ├── 208135-001_QR.svg        ← generated after first upload
    ├── Stickers_208135-001.pdf  ← generated by the app
    └── 208135-001_Stickers_Summary.txt  ← generated by the app
```

**Watch folder** (where MiTek drops the Final Jobsite Package):

```
Watch Folder\
└── [any subfolder]\
    └── _Final Jobsite Package QR\
        ├── 208135-001.csv
        └── 208135-001_SomeName.pdf
```

Files matching `208135-001_JOB_*.pdf` are excluded from uploads automatically.
