"""
BuildersQRLabels.py — Builders Connect QR Label & Cloud Sync Manager
Builders Inc. — single-file desktop application (Windows only, Python 3.10+)

Entry point:  python BuildersQRLabels.py
Architecture: AppConfig → JobDatabase → JobManager → StickerEngine
              CloudProvider (ABC) → DropboxProvider / OneDriveProvider
              BuildersQRLabelsApp (Tkinter UI — Phase 3)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Tkinter (UI — Phase 3) ────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

# ── Third-party ───────────────────────────────────────────────────────────────
import dropbox
from dropbox import DropboxOAuth2FlowNoRedirect
from dropbox.files import WriteMode
from dropbox.sharing import RequestedVisibility, SharedLinkSettings
import msal
import pandas as pd
import qrcode
from qrcode.image.svg import SvgPathImage
import requests
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas as PDFCanvas
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

# ── Logging (basic config — Phase 7 adds file handler from DB settings) ───────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Module-level constants ────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
TOKEN_DROPBOX_FILE = "token_dropbox.json"
TOKEN_ONEDRIVE_FILE = "token_onedrive.json"
QR_SUFFIX = "_QR.svg"
DROPBOX_UPLOAD_ROOT = "/Cloud Manager Uploads"
WATCH_SUBDIR = "_Final Jobsite Package QR"
JOB_ID_RE = re.compile(r"^\d{7}$")
GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/drive"


# ════════════════════════════════════════════════════════════════════════════════
# JobInfo — shared data record
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class JobInfo:
    """Single job record — persisted in the database and displayed in the UI."""
    job_id: str
    sticker_status: str = "Pending"    # Pending / Completed / Error
    cloud_status: str = "Pending"      # Uploaded / Cloud Only / Local / CSV Only / Incomplete / Pending / Error
    sticker_qty: Optional[int] = None
    cloud_progress: str = "0%"
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_by: str = field(default_factory=socket.gethostname)


# ════════════════════════════════════════════════════════════════════════════════
# AppConfig — per-machine bootstrap settings (config.json)
# ════════════════════════════════════════════════════════════════════════════════

class AppConfig:
    """
    Loads and saves config.json.  Holds only per-machine / bootstrap settings.
    Shared path config (watch_folder, target_folder, etc.) lives in JobDatabase.settings.
    Writes are atomic: write to .tmp, then os.replace().
    """

    _DEFAULTS: dict = {
        "db_path":             "",
        "cloud_provider":      "dropbox",
        "dropbox_app_key":     "",
        "dropbox_app_secret":  "",
        "onedrive_client_id":  "",
        "onedrive_tenant_id":  "common",
        "company_name":        "Builders Inc.",
        "company_address":     "2644 Byington Solway Rd, Knoxville",
    }

    def __init__(self, config_path: str = CONFIG_FILE) -> None:
        self._path = config_path
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except FileNotFoundError:
            self._data = {}
        except Exception:
            logging.exception("Failed to load %s; using defaults", self._path)
            self._data = {}

    def save(self) -> None:
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            logging.exception("Failed to save %s", self._path)
            if os.path.exists(tmp):
                os.remove(tmp)

    def _get(self, key: str) -> str:
        return self._data.get(key, self._DEFAULTS.get(key, ""))

    def _set(self, key: str, value: str) -> None:
        self._data[key] = value

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def db_path(self) -> str:             return self._get("db_path")
    @db_path.setter
    def db_path(self, v: str):            self._set("db_path", v)

    @property
    def cloud_provider(self) -> str:      return self._get("cloud_provider")
    @cloud_provider.setter
    def cloud_provider(self, v: str):     self._set("cloud_provider", v)

    @property
    def dropbox_app_key(self) -> str:     return self._get("dropbox_app_key")
    @dropbox_app_key.setter
    def dropbox_app_key(self, v: str):    self._set("dropbox_app_key", v)

    @property
    def dropbox_app_secret(self) -> str:  return self._get("dropbox_app_secret")
    @dropbox_app_secret.setter
    def dropbox_app_secret(self, v: str): self._set("dropbox_app_secret", v)

    @property
    def onedrive_client_id(self) -> str:  return self._get("onedrive_client_id")
    @onedrive_client_id.setter
    def onedrive_client_id(self, v: str): self._set("onedrive_client_id", v)

    @property
    def onedrive_tenant_id(self) -> str:  return self._get("onedrive_tenant_id")
    @onedrive_tenant_id.setter
    def onedrive_tenant_id(self, v: str): self._set("onedrive_tenant_id", v)

    @property
    def company_name(self) -> str:        return self._get("company_name")
    @company_name.setter
    def company_name(self, v: str):       self._set("company_name", v)

    @property
    def company_address(self) -> str:     return self._get("company_address")
    @company_address.setter
    def company_address(self, v: str):    self._set("company_address", v)


# ════════════════════════════════════════════════════════════════════════════════
# JobDatabase — SQLite wrapper (jobs + settings tables)
# ════════════════════════════════════════════════════════════════════════════════

class JobDatabase:
    """
    Wraps SQLite.  Owns two tables:
      jobs     — one row per job_id (sticker + cloud status)
      settings — key/value shared config (watch_folder, target_folder, etc.)

    If db_path is a UNC path that is unreachable, falls back to a local DB
    without crashing the app.
    """

    LOCAL_FALLBACK = "builders_qr_labels.db"

    def __init__(self, db_path: str = "") -> None:
        self._path = db_path.strip() or self.LOCAL_FALLBACK
        self._available = False
        self._lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        try:
            parent = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(parent, exist_ok=True)
            conn = self._open()
            self._init_schema(conn)
            conn.close()
            self._available = True
            log.info("Database connected: %s", self._path)
        except Exception:
            logging.exception("DB unavailable at %s; falling back to %s",
                              self._path, self.LOCAL_FALLBACK)
            if self._path != self.LOCAL_FALLBACK:
                self._path = self.LOCAL_FALLBACK
                try:
                    conn = self._open()
                    self._init_schema(conn)
                    conn.close()
                    self._available = True
                    log.warning("Using local fallback DB: %s", self._path)
                except Exception:
                    logging.exception("Local fallback DB also failed")

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id          TEXT PRIMARY KEY,
                sticker_status  TEXT DEFAULT 'Pending',
                cloud_status    TEXT DEFAULT 'Pending',
                sticker_qty     INTEGER,
                cloud_progress  TEXT DEFAULT '0%',
                last_updated    TEXT,
                updated_by      TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key          TEXT PRIMARY KEY,
                value        TEXT,
                last_updated TEXT,
                updated_by   TEXT
            );
        """)
        conn.commit()

    def is_available(self) -> bool:
        return self._available

    # ── Jobs ─────────────────────────────────────────────────────────────────

    def upsert_job(self, job: JobInfo) -> None:
        if not self._available:
            return
        with self._lock:
            try:
                conn = self._open()
                conn.execute(
                    """
                    INSERT INTO jobs
                        (job_id, sticker_status, cloud_status, sticker_qty,
                         cloud_progress, last_updated, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        sticker_status = excluded.sticker_status,
                        cloud_status   = excluded.cloud_status,
                        sticker_qty    = excluded.sticker_qty,
                        cloud_progress = excluded.cloud_progress,
                        last_updated   = excluded.last_updated,
                        updated_by     = excluded.updated_by
                    """,
                    (job.job_id, job.sticker_status, job.cloud_status,
                     job.sticker_qty, job.cloud_progress,
                     job.last_updated, job.updated_by),
                )
                conn.commit()
                conn.close()
            except Exception:
                logging.exception("upsert_job failed for %s", job.job_id)

    def get_all_jobs(self) -> list[JobInfo]:
        if not self._available:
            return []
        with self._lock:
            try:
                conn = self._open()
                rows = conn.execute("SELECT * FROM jobs ORDER BY job_id").fetchall()
                conn.close()
                return [self._row_to_job(r) for r in rows]
            except Exception:
                logging.exception("get_all_jobs failed")
                return []

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        if not self._available:
            return None
        with self._lock:
            try:
                conn = self._open()
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                conn.close()
                return self._row_to_job(row) if row else None
            except Exception:
                logging.exception("get_job failed for %s", job_id)
                return None

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobInfo:
        return JobInfo(
            job_id=row["job_id"],
            sticker_status=row["sticker_status"],
            cloud_status=row["cloud_status"],
            sticker_qty=row["sticker_qty"],
            cloud_progress=row["cloud_progress"],
            last_updated=row["last_updated"] or "",
            updated_by=row["updated_by"] or "",
        )

    # ── Settings ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str = "") -> str:
        if not self._available:
            return default
        with self._lock:
            try:
                conn = self._open()
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                conn.close()
                return row["value"] if (row and row["value"] is not None) else default
            except Exception:
                logging.exception("get_setting failed for %s", key)
                return default

    def set_setting(self, key: str, value: str) -> None:
        if not self._available:
            return
        with self._lock:
            try:
                conn = self._open()
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT INTO settings (key, value, last_updated, updated_by)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value        = excluded.value,
                        last_updated = excluded.last_updated,
                        updated_by   = excluded.updated_by
                    """,
                    (key, value, now, socket.gethostname()),
                )
                conn.commit()
                conn.close()
            except Exception:
                logging.exception("set_setting failed for %s", key)


# ════════════════════════════════════════════════════════════════════════════════
# CloudProvider — abstract base
# ════════════════════════════════════════════════════════════════════════════════

class CloudProvider(ABC):
    """Abstract interface for cloud integrations.  All providers implement this."""

    @abstractmethod
    def authenticate(self, auth_code: str = "") -> bool:
        """Complete the auth flow. Returns True if authenticated."""

    @abstractmethod
    def is_authenticated(self) -> bool:
        """True if a valid session exists."""

    @abstractmethod
    def list_jobs(self) -> dict[str, str]:
        """Return {job_id: cloud_folder_path} for all jobs in cloud."""

    @abstractmethod
    def ensure_folder(self, job_id: str) -> str:
        """Create cloud folder for job_id if missing. Returns cloud path."""

    @abstractmethod
    def upload_file(self, local_path: str, cloud_folder: str) -> None:
        """Upload a single file to the given cloud folder."""

    @abstractmethod
    def download_file(self, cloud_path: str, local_path: str) -> None:
        """Download a file from the cloud to local_path."""

    @abstractmethod
    def create_share_link(self, cloud_folder: str) -> str:
        """Create and return a public share URL for the folder."""

    @abstractmethod
    def list_folder_files(self, cloud_folder: str) -> list[str]:
        """Return list of filenames in the cloud folder."""


# ════════════════════════════════════════════════════════════════════════════════
# DropboxProvider — Phase 2: auth wiring; Phase 5: full upload/download logic
# ════════════════════════════════════════════════════════════════════════════════

class DropboxProvider(CloudProvider):
    """
    Dropbox implementation.
    Auth is fully wired here (needed by Settings dialog in Phase 4).
    Upload / download / list methods are stubbed — implemented in Phase 5.
    """

    _CACHE_TTL = 5  # seconds for job-list cache

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._dbx: Optional[dropbox.Dropbox] = None
        self._token_file = TOKEN_DROPBOX_FILE
        self._auth_flow: Optional[DropboxOAuth2FlowNoRedirect] = None
        self._jobs_cache: Optional[dict] = None
        self._jobs_cache_time: float = 0.0
        self._load_tokens()

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _load_tokens(self) -> None:
        try:
            with open(self._token_file, "r", encoding="utf-8") as fh:
                tokens = json.load(fh)
            self._dbx = dropbox.Dropbox(
                oauth2_access_token=tokens.get("access_token", ""),
                oauth2_refresh_token=tokens.get("refresh_token", ""),
                app_key=self._config.dropbox_app_key,
                app_secret=self._config.dropbox_app_secret,
            )
            log.info("Dropbox tokens loaded from %s", self._token_file)
        except FileNotFoundError:
            pass
        except Exception:
            logging.exception("Failed to load Dropbox tokens")

    def _save_tokens(self, access_token: str, refresh_token: str) -> None:
        try:
            with open(self._token_file, "w", encoding="utf-8") as fh:
                json.dump({"access_token": access_token, "refresh_token": refresh_token}, fh)
        except Exception:
            logging.exception("Failed to save Dropbox tokens")

    def get_auth_url(self) -> str:
        """Return the OAuth2 URL the user must visit. Call before authenticate()."""
        if not self._config.dropbox_app_key:
            raise RuntimeError("Dropbox App Key is not configured. Set it in Settings.")
        self._auth_flow = DropboxOAuth2FlowNoRedirect(
            self._config.dropbox_app_key,
            self._config.dropbox_app_secret,
            token_access_type="offline",
        )
        return self._auth_flow.start()

    def authenticate(self, auth_code: str = "") -> bool:
        """Exchange auth_code for tokens. Requires get_auth_url() called first."""
        if not self._auth_flow:
            raise RuntimeError("Call get_auth_url() before authenticate().")
        try:
            result = self._auth_flow.finish(auth_code.strip())
            self._dbx = dropbox.Dropbox(
                oauth2_access_token=result.access_token,
                oauth2_refresh_token=result.refresh_token,
                app_key=self._config.dropbox_app_key,
                app_secret=self._config.dropbox_app_secret,
            )
            self._save_tokens(result.access_token, result.refresh_token)
            log.info("Dropbox authenticated successfully")
            return True
        except Exception:
            logging.exception("Dropbox authentication failed")
            return False

    def is_authenticated(self) -> bool:
        if not self._dbx:
            return False
        try:
            self._dbx.users_get_current_account()
            return True
        except Exception:
            return False

    # ── Cloud operations (Phase 5) ────────────────────────────────────────────

    def list_jobs(self) -> dict[str, str]:
        raise NotImplementedError("Phase 5: DropboxProvider.list_jobs")

    def ensure_folder(self, job_id: str) -> str:
        raise NotImplementedError("Phase 5: DropboxProvider.ensure_folder")

    def upload_file(self, local_path: str, cloud_folder: str) -> None:
        raise NotImplementedError("Phase 5: DropboxProvider.upload_file")

    def download_file(self, cloud_path: str, local_path: str) -> None:
        raise NotImplementedError("Phase 5: DropboxProvider.download_file")

    def create_share_link(self, cloud_folder: str) -> str:
        raise NotImplementedError("Phase 5: DropboxProvider.create_share_link")

    def list_folder_files(self, cloud_folder: str) -> list[str]:
        raise NotImplementedError("Phase 5: DropboxProvider.list_folder_files")


# ════════════════════════════════════════════════════════════════════════════════
# OneDriveProvider — Phase 2: auth wiring; Phase 5: full upload/download logic
# ════════════════════════════════════════════════════════════════════════════════

class OneDriveProvider(CloudProvider):
    """
    Microsoft OneDrive via Graph API + MSAL device-code flow.
    Auth is fully wired here.  Upload / download / list stubbed for Phase 5.
    """

    SCOPES = ["Files.ReadWrite", "offline_access"]
    _AUTHORITY_TEMPLATE = "https://login.microsoftonline.com/{tenant}"

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._token_file = TOKEN_ONEDRIVE_FILE
        self._msal_app: Optional[msal.PublicClientApplication] = None
        self._device_flow: Optional[dict] = None
        self._init_msal()

    def _init_msal(self) -> None:
        if not self._config.onedrive_client_id:
            return
        try:
            authority = self._AUTHORITY_TEMPLATE.format(
                tenant=self._config.onedrive_tenant_id or "common"
            )
            cache = msal.SerializableTokenCache()
            if os.path.exists(self._token_file):
                try:
                    with open(self._token_file, "r", encoding="utf-8") as fh:
                        cache.deserialize(fh.read())
                except Exception:
                    logging.exception("Failed to load OneDrive token cache")
            self._msal_app = msal.PublicClientApplication(
                self._config.onedrive_client_id,
                authority=authority,
                token_cache=cache,
            )
        except Exception:
            logging.exception("MSAL app init failed")

    def _save_token_cache(self) -> None:
        if self._msal_app and self._msal_app.token_cache.has_state_changed:
            try:
                with open(self._token_file, "w", encoding="utf-8") as fh:
                    fh.write(self._msal_app.token_cache.serialize())
            except Exception:
                logging.exception("Failed to save OneDrive token cache")

    def _get_access_token(self) -> Optional[str]:
        if not self._msal_app:
            return None
        accounts = self._msal_app.get_accounts()
        if accounts:
            result = self._msal_app.acquire_token_silent(self.SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._save_token_cache()
                return result["access_token"]
        return None

    def start_device_code_flow(self) -> dict:
        """Initiate device-code flow. Returns dict with 'user_code' and 'verification_uri'."""
        if not self._msal_app:
            raise RuntimeError("OneDrive Client ID is not configured. Set it in Settings.")
        flow = self._msal_app.initiate_device_flow(scopes=self.SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(
                f"Device code flow failed: {flow.get('error_description', 'unknown error')}"
            )
        self._device_flow = flow
        return flow

    def authenticate(self, auth_code: str = "") -> bool:
        """Poll MSAL until device-code auth completes. Call after start_device_code_flow()."""
        if not self._device_flow:
            raise RuntimeError("Call start_device_code_flow() before authenticate().")
        try:
            result = self._msal_app.acquire_token_by_device_flow(self._device_flow)
            if "access_token" in result:
                self._save_token_cache()
                log.info("OneDrive authenticated successfully")
                return True
            log.error("OneDrive auth failed: %s", result.get("error_description", "unknown"))
            return False
        except Exception:
            logging.exception("OneDrive authentication failed")
            return False

    def is_authenticated(self) -> bool:
        try:
            return self._get_access_token() is not None
        except Exception:
            return False

    # ── Cloud operations (Phase 5) ────────────────────────────────────────────

    def list_jobs(self) -> dict[str, str]:
        raise NotImplementedError("Phase 5: OneDriveProvider.list_jobs")

    def ensure_folder(self, job_id: str) -> str:
        raise NotImplementedError("Phase 5: OneDriveProvider.ensure_folder")

    def upload_file(self, local_path: str, cloud_folder: str) -> None:
        raise NotImplementedError("Phase 5: OneDriveProvider.upload_file")

    def download_file(self, cloud_path: str, local_path: str) -> None:
        raise NotImplementedError("Phase 5: OneDriveProvider.download_file")

    def create_share_link(self, cloud_folder: str) -> str:
        raise NotImplementedError("Phase 5: OneDriveProvider.create_share_link")

    def list_folder_files(self, cloud_folder: str) -> list[str]:
        raise NotImplementedError("Phase 5: OneDriveProvider.list_folder_files")


# ════════════════════════════════════════════════════════════════════════════════
# JobManager — folder scanning and job status classification
# ════════════════════════════════════════════════════════════════════════════════

class JobManager:
    """
    Scans the target folder and watch folder, classifies each job's sticker and
    cloud status, and upserts results into JobDatabase.
    """

    def __init__(self, db: JobDatabase, cloud: Optional[CloudProvider] = None) -> None:
        self._db = db
        self._cloud = cloud

    # ── Folder path helpers (read from shared DB settings) ───────────────────

    @property
    def watch_folder(self) -> str:
        return self._db.get_setting("watch_folder")

    @property
    def target_folder(self) -> str:
        return self._db.get_setting("target_folder")

    # ── Main scan ────────────────────────────────────────────────────────────

    def scan_all(self) -> list[JobInfo]:
        """
        Scan target + watch folders, classify all jobs, upsert to DB.
        Returns sorted list of JobInfo.
        """
        target = self.target_folder
        watch = self.watch_folder
        jobs: dict[str, JobInfo] = {}

        # Local jobs from target folder
        if target and os.path.isdir(target):
            for name in os.listdir(target):
                if JOB_ID_RE.match(name) and os.path.isdir(os.path.join(target, name)):
                    status, qty = self.classify_sticker_status(name)
                    jobs[name] = JobInfo(job_id=name, sticker_status=status, sticker_qty=qty)

        # Pending jobs visible only in the watch folder
        if watch and os.path.isdir(watch):
            for job_id in self._find_watch_jobs(watch):
                if job_id not in jobs:
                    jobs[job_id] = JobInfo(job_id=job_id, sticker_status="Pending")

        # Cloud status
        cloud_jobs: dict[str, str] = {}
        if self._cloud and self._cloud.is_authenticated():
            try:
                cloud_jobs = self._cloud.list_jobs()
            except NotImplementedError:
                pass  # Phase 5 not yet implemented
            except Exception:
                logging.exception("Cloud job list failed")

        for job_id, info in jobs.items():
            info.cloud_status, info.cloud_progress = self._classify_cloud_status(
                job_id, cloud_jobs
            )
            self._db.upsert_job(info)

        result = sorted(jobs.values(), key=lambda j: j.job_id)
        log.info("Scan complete: %d jobs found", len(result))
        return result

    # ── Sticker status ────────────────────────────────────────────────────────

    def classify_sticker_status(self, job_id: str) -> tuple[str, Optional[int]]:
        """
        Return (status, qty_or_None) by inspecting the job folder contents.
        Requires target_folder to be set.
        """
        target = self.target_folder
        if not target:
            return "Error", None
        folder = os.path.join(target, job_id)
        if not os.path.isdir(folder):
            return "Error", None

        files = os.listdir(folder)
        has_csv     = any(f.lower() == f"{job_id}.csv" for f in files)
        has_qr      = any("_qr.svg" in f.lower() for f in files)
        has_pdf     = f"Stickers_{job_id}.pdf" in files
        has_summary = f"{job_id}_Stickers_Summary.txt" in files

        if has_pdf and has_summary:
            qty = self._read_qty_from_summary(
                os.path.join(folder, f"{job_id}_Stickers_Summary.txt")
            )
            return "Completed", qty
        if has_csv and has_qr:
            return "Pending", None
        if has_csv:
            return "Pending", None
        return "Error", None

    # ── Cloud status ──────────────────────────────────────────────────────────

    def _classify_cloud_status(
        self, job_id: str, cloud_jobs: dict[str, str]
    ) -> tuple[str, str]:
        """Return (cloud_status, progress_pct)."""
        target = self.target_folder
        in_cloud = job_id in cloud_jobs
        has_local = bool(target) and os.path.isdir(os.path.join(target, job_id))

        if in_cloud and has_local:
            return "Uploaded", "100%"
        if in_cloud and not has_local:
            return "Cloud Only", "100%"
        if not has_local:
            return "Pending", "0%"

        # Local only — granular progress
        files = os.listdir(os.path.join(target, job_id))
        has_pdf = f"Stickers_{job_id}.pdf" in files
        has_csv = any(f.lower().endswith(".csv") for f in files)

        if has_pdf:
            return "Local", "50%"
        if has_csv:
            return "CSV Only", "25%"
        return "Incomplete", "25%"

    # ── Watch folder helpers ──────────────────────────────────────────────────

    def find_watch_source(self, job_id: str) -> Optional[str]:
        """
        Walk the watch folder tree for a _Final Jobsite Package QR subfolder
        that contains {job_id}.csv.  Returns path or None.
        """
        watch = self.watch_folder
        if not watch or not os.path.isdir(watch):
            return None
        for root, dirs, _ in os.walk(watch):
            if WATCH_SUBDIR in dirs:
                candidate = os.path.join(root, WATCH_SUBDIR)
                if any(f.lower() == f"{job_id}.csv" for f in os.listdir(candidate)):
                    return candidate
        return None

    def copy_watch_to_target(self, job_id: str) -> bool:
        """
        Copy valid files from the watch source to the target folder.
        Excludes files matching {job_id}_JOB_* pattern.
        Returns True if at least one file was copied.
        """
        source = self.find_watch_source(job_id)
        if not source:
            log.warning("No watch source found for job %s", job_id)
            return False
        target = self.target_folder
        if not target:
            log.error("Target folder not configured")
            return False

        dest = os.path.join(target, job_id)
        os.makedirs(dest, exist_ok=True)

        copied = 0
        for fname in os.listdir(source):
            if re.match(rf"{re.escape(job_id)}_JOB_", fname, flags=re.IGNORECASE):
                log.debug("Skipping JOB exclusion file: %s", fname)
                continue
            src_path = os.path.join(source, fname)
            if os.path.isfile(src_path):
                shutil.copy2(src_path, os.path.join(dest, fname))
                copied += 1

        log.info("Copied %d file(s) from watch → target for job %s", copied, job_id)
        return copied > 0

    def _find_watch_jobs(self, watch: str) -> list[str]:
        """Return job IDs found anywhere under the watch folder tree."""
        found: list[str] = []
        for root, dirs, files in os.walk(watch):
            if os.path.basename(root) == WATCH_SUBDIR:
                for fname in files:
                    if fname.lower().endswith(".csv"):
                        stem = os.path.splitext(fname)[0]
                        if JOB_ID_RE.match(stem) and stem not in found:
                            found.append(stem)
        return found

    @staticmethod
    def _read_qty_from_summary(path: str) -> Optional[int]:
        """Parse the TOTAL STICKERS line from a summary file."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("TOTAL STICKERS:"):
                        # Format: "TOTAL STICKERS: 42  (QTY: 30 | PLY: 12)"
                        token = line.split(":")[1].strip().split()[0]
                        return int(token)
        except Exception:
            pass
        return None


# ════════════════════════════════════════════════════════════════════════════════
# StickerEngine — PDF + summary generation
# ════════════════════════════════════════════════════════════════════════════════

class StickerEngine:
    """
    Generates sticker PDFs (100mm × 30mm label pages) and summary TXT files
    from job folder data.  No UI dependencies.
    """

    # ── Layout constants ──────────────────────────────────────────────────────
    STICKER_W = 100 * mm
    STICKER_H = 30 * mm
    QR_SIZE   = 20 * mm
    MARGIN    = 2 * mm

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        # {job_id|trsname|sticker_type: {token, scanned, timestamp}}
        self._tokens: dict[str, dict] = {}

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_job(self, job_path: str) -> tuple[bool, dict]:
        """
        Return (is_valid, details).
        details keys: has_csv, has_qr, csv_path, qr_path, error.
        """
        job_id = os.path.basename(job_path)
        details: dict = {
            "has_csv": False, "has_qr": False,
            "csv_path": None, "qr_path": None, "error": None,
        }
        try:
            files = os.listdir(job_path)
            csv_matches = [f for f in files if f.lower() == f"{job_id}.csv"]
            qr_matches  = [f for f in files if "_qr.svg" in f.lower()]

            if csv_matches:
                details["has_csv"] = True
                details["csv_path"] = os.path.join(job_path, csv_matches[0])
            if qr_matches:
                details["has_qr"] = True
                details["qr_path"] = os.path.join(job_path, qr_matches[0])

            if not details["has_csv"]:
                details["error"] = f"Missing CSV: {job_id}.csv"
            elif not details["has_qr"]:
                details["error"] = f"Missing QR SVG: {job_id}_QR.svg"
        except Exception as exc:
            details["error"] = str(exc)

        return (details["has_csv"] and details["has_qr"], details)

    # ── CSV parsing ───────────────────────────────────────────────────────────

    def extract_csv_data(self, csv_path: str, job_id: str) -> tuple[list[dict], Optional[str]]:
        """
        Parse CSV, filter to job_id rows.
        Returns (list_of_truss_dicts, error_message_or_None).
        Each dict: trsname, trusstype, batch, customer, jobname, qty, ply.
        """
        try:
            df = pd.read_csv(csv_path)
            df.columns = df.columns.str.lower().str.strip()

            required = {"jobnumber", "trsname", "trusstype", "batch",
                        "customer", "jobname", "qty", "ply"}
            missing = required - set(df.columns)
            if missing:
                return [], f"Missing columns: {', '.join(sorted(missing))}"

            # Filter to this job's rows (jobnumber may be int or str in CSV)
            df = df[df["jobnumber"].astype(str).str.strip() == str(int(job_id))]
            if df.empty:
                return [], f"No rows for job {job_id} found in CSV"

            trusses = []
            for _, row in df.iterrows():
                trusses.append({
                    "trsname":   str(row["trsname"]).strip(),
                    "trusstype": str(row["trusstype"]).strip(),
                    "batch":     str(row["batch"]).strip(),
                    "customer":  str(row["customer"]).strip()[:25],
                    "jobname":   str(row["jobname"]).strip()[:25],
                    "qty":       int(row["qty"]),
                    "ply":       int(row["ply"]),
                })
            return trusses, None
        except Exception as exc:
            logging.exception("CSV parse error: %s", csv_path)
            return [], str(exc)

    def calculate_sticker_count(self, qty: int, ply: int) -> int:
        return qty if ply == 1 else qty * ply

    # ── PDF generation ────────────────────────────────────────────────────────

    def generate_pdf(self, job_path: str) -> str:
        """
        Generate Stickers_{job_id}.pdf and {job_id}_Stickers_Summary.txt.
        Returns the PDF output path.  Raises ValueError on validation failure.
        """
        job_id = os.path.basename(job_path)
        valid, details = self.validate_job(job_path)
        if not valid:
            raise ValueError(details["error"])

        trusses, error = self.extract_csv_data(details["csv_path"], job_id)
        if error:
            raise ValueError(error)

        pdf_path = os.path.join(job_path, f"Stickers_{job_id}.pdf")
        c = PDFCanvas(pdf_path, pagesize=(self.STICKER_W, self.STICKER_H))

        for truss in trusses:
            is_ply = truss["ply"] > 1
            label_char = "P" if is_ply else "Q"
            count = self.calculate_sticker_count(truss["qty"], truss["ply"])
            for idx in range(1, count + 1):
                sticker_type = f"{label_char}{idx:02d}/{count:02d}"
                token_data = self._register_sticker_token(
                    job_id, truss["trsname"], sticker_type
                )
                self._draw_sticker(
                    c,
                    job_id=job_id,
                    truss=truss,
                    sticker_type=sticker_type,
                    token=token_data["token"],
                    qr_svg_path=details["qr_path"],
                )
                c.showPage()

        c.save()
        log.info("PDF generated: %s (%d trusses)", pdf_path, len(trusses))

        self.generate_summary(job_id, trusses, job_path)
        return pdf_path

    def _draw_sticker(
        self,
        c: PDFCanvas,
        job_id: str,
        truss: dict,
        sticker_type: str,
        token: str,
        qr_svg_path: Optional[str],
    ) -> None:
        """Draw a single 100mm × 30mm sticker page onto canvas c."""
        W, H = self.STICKER_W, self.STICKER_H
        m = self.MARGIN
        qs = self.QR_SIZE

        # ── Outer border ─────────────────────────────────────────────────────
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.5)
        c.rect(1 * mm, 1 * mm, W - 2 * mm, H - 2 * mm)
        c.setStrokeColorRGB(0, 0, 0)
        c.setFillColorRGB(0, 0, 0)

        # ── Left QR — cloud/drive link (from SVG) ────────────────────────────
        left_qr_x = m
        left_qr_y = 4 * mm
        if qr_svg_path and os.path.isfile(qr_svg_path):
            try:
                drawing = svg2rlg(qr_svg_path)
                if drawing:
                    sx = qs / drawing.width
                    sy = qs / drawing.height
                    drawing.width  = qs
                    drawing.height = qs
                    drawing.transform = (sx, 0, 0, sy, 0, 0)
                    renderPDF.draw(drawing, c, left_qr_x, left_qr_y)
            except Exception:
                logging.exception("Failed to render SVG QR: %s", qr_svg_path)

        # ── Right QR — token data ─────────────────────────────────────────────
        right_qr_x = W - qs - m
        right_qr_y = 4 * mm
        qr_data = f"{truss['batch']}|{truss['trsname']}|{sticker_type}|{token}"
        tmp_png: Optional[str] = None
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=1,
            )
            qr.add_data(qr_data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            fd, tmp_png = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            img.save(tmp_png)
            c.drawImage(tmp_png, right_qr_x, right_qr_y,
                        width=qs, height=qs, mask="auto")
        except Exception:
            logging.exception("Failed to generate token QR")
        finally:
            if tmp_png and os.path.exists(tmp_png):
                os.remove(tmp_png)

        # ── Text region (between the two QRs) ────────────────────────────────
        tx_left   = left_qr_x + qs + 1 * mm   # ≈ 23 mm
        tx_right  = right_qr_x - 1 * mm        # ≈ 77 mm
        tx_center = (tx_left + tx_right) / 2   # ≈ 50 mm

        # Row 1 — Job ID | Customer | Sticker counter
        row1_y = H - 4 * mm
        c.setFont("Helvetica-Bold", 10)
        c.drawString(tx_left, row1_y, job_id)

        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(tx_center, row1_y, truss["customer"])

        c.setFont("Helvetica-Bold", 7)
        c.drawRightString(tx_right, row1_y, sticker_type)

        # Row 2 — Job name
        row2_y = H - 7 * mm
        c.setFont("Helvetica", 7)
        c.drawCentredString(tx_center, row2_y, truss["jobname"])

        # Large truss code (trsname) centered in the QR zone
        code = truss["trsname"]
        code_len = len(code)
        if code_len <= 4:
            font_size = 48
        elif code_len == 5:
            font_size = 42
        elif code_len == 6:
            font_size = 36
        else:
            font_size = 30

        c.setFont("Helvetica-Bold", font_size)
        c.drawCentredString(tx_center, 9 * mm, code)

        # ── Footer labels ─────────────────────────────────────────────────────
        footer_y = 2 * mm
        c.setFont("Helvetica", 4)
        c.drawCentredString(left_qr_x + qs / 2, footer_y, "JOBSITE PACKAGE")
        c.drawCentredString(right_qr_x + qs / 2, footer_y, "BUILDERS USE ONLY")

        company = f"{self._config.company_name}  \u2014  {self._config.company_address}"
        c.setFont("Helvetica-Bold", 5.5)
        c.drawCentredString(tx_center, footer_y, company)

    # ── Summary file ──────────────────────────────────────────────────────────

    def generate_summary(self, job_id: str, trusses: list[dict], job_folder: str) -> str:
        """Write {job_id}_Stickers_Summary.txt. Returns output path."""
        output_path = os.path.join(job_folder, f"{job_id}_Stickers_Summary.txt")
        qty_total = 0
        ply_total = 0

        header = "| Job ID  | Code     | Type         | Label     | Token        | Batch | Printed |"
        sep    = "|---------|----------|--------------|-----------|--------------|-------|---------|"
        rows: list[str] = [header, sep]

        for truss in trusses:
            is_ply = truss["ply"] > 1
            label_char = "P" if is_ply else "Q"
            count = self.calculate_sticker_count(truss["qty"], truss["ply"])
            batch_display = str(truss["batch"])[-2:]
            for idx in range(1, count + 1):
                label = f"{label_char}{idx:02d}/{count:02d}"
                key = f"{job_id}|{truss['trsname']}|{label}"
                token = self._tokens.get(key, {}).get("token", "")
                rows.append(
                    f"| {job_id} | {truss['trsname']:<8} | {truss['trusstype']:<12} "
                    f"| {label:<9} | {token:<12} | {batch_display:<5} | No     |"
                )
            if is_ply:
                ply_total += count
            else:
                qty_total += count

        rows.append("")
        rows.append(
            f"TOTAL STICKERS: {qty_total + ply_total}  (QTY: {qty_total} | PLY: {ply_total})"
        )

        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(rows))

        log.info("Summary written: %s", output_path)
        return output_path

    # ── Token management ──────────────────────────────────────────────────────

    def _register_sticker_token(
        self, job_id: str, trsname: str, sticker_type: str, token: str = ""
    ) -> dict:
        key = f"{job_id}|{trsname}|{sticker_type}"
        if key not in self._tokens:
            self._tokens[key] = {
                "token":     token or uuid.uuid4().hex[:12],
                "scanned":   False,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        return self._tokens[key]

    def recover_tokens_from_summary(self, job_folder: str, job_id: str) -> int:
        """
        Parse an existing summary file and restore tokens into memory.
        Returns number of tokens recovered.
        """
        summary_path = os.path.join(job_folder, f"{job_id}_Stickers_Summary.txt")
        if not os.path.isfile(summary_path):
            return 0
        recovered = 0
        try:
            with open(summary_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.startswith("|"):
                        continue
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) < 7:
                        continue
                    row_job = parts[1]
                    trsname = parts[2]
                    label   = parts[4]
                    token   = parts[5]
                    if JOB_ID_RE.match(row_job) and row_job == job_id and token:
                        self._register_sticker_token(row_job, trsname, label, token)
                        recovered += 1
        except Exception:
            logging.exception("Token recovery failed for %s", summary_path)
        log.info("Recovered %d token(s) for job %s", recovered, job_id)
        return recovered


# ════════════════════════════════════════════════════════════════════════════════
# BuildersQRLabelsApp — main Tkinter UI (Phase 3)
# ════════════════════════════════════════════════════════════════════════════════

class BuildersQRLabelsApp:
    """
    Main application window.  Owns all widgets and coordinates backend calls.
    All UI modifications from background threads go through self._ui().
    """

    # ── Treeview column spec: (id, heading, width_px, anchor) ────────────────
    _COLS = (
        ("job_id",         "Job ID",         85,  "w"),
        ("stickers",       "Stickers",        75,  "center"),
        ("sticker_status", "Sticker Status",  115, "center"),
        ("cloud_status",   "Cloud Status",    115, "center"),
        ("progress",       "Progress",         55, "center"),
        ("timestamp",      "Last Updated",    155, "w"),
    )

    # ── Row tag → background colour ───────────────────────────────────────────
    _TAG_BG = {
        "done":       "#c8e6c9",   # sticker complete + uploaded
        "partial":    "#fff9c4",   # pending or complete-not-uploaded
        "cloud_only": "#bbdefb",   # exists in cloud but not locally
        "active":     "#bbdefb",   # in-progress (generating / uploading)
        "error":      "#ffcdd2",   # any error
    }

    # ── Sort key map: column id → JobInfo attribute or callable ───────────────
    _SORT_KEYS = {
        "job_id":         lambda j: j.job_id,
        "stickers":       lambda j: j.sticker_qty or 0,
        "sticker_status": lambda j: j.sticker_status,
        "cloud_status":   lambda j: j.cloud_status,
        "progress":       lambda j: j.cloud_progress,
        "timestamp":      lambda j: j.last_updated,
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Builders Connect — QR Labels")
        self.root.geometry("700x650")
        self.root.minsize(600, 450)

        # ── Backend objects ───────────────────────────────────────────────────
        self._config  = AppConfig()
        self._db      = JobDatabase(self._config.db_path)
        self._cloud: Optional[CloudProvider] = None
        self._manager = JobManager(self._db)
        self._engine  = StickerEngine(self._config)
        self._init_cloud()

        # ── Application state ─────────────────────────────────────────────────
        self._jobs: list[JobInfo] = []      # full list from last scan
        self._last_scan: str = ""
        self._session_errors: int = 0
        self._scan_lock = threading.Lock()
        self._refresh_id: Optional[str] = None

        # ── UI ────────────────────────────────────────────────────────────────
        self._icons: dict = {}
        self._load_icons()
        self._search_var = tk.StringVar()
        self._sort_col: str = "job_id"
        self._sort_rev: bool = False
        self._setup_ui()

        # ── Kick off initial scan + periodic refresh ──────────────────────────
        self._run(self._scan_task)
        self._schedule_refresh()

    # ── Threading helpers ─────────────────────────────────────────────────────

    def _run(self, target, *args) -> None:
        """Launch target(*args) in a daemon background thread."""
        threading.Thread(target=target, args=args, daemon=True).start()

    def _ui(self, fn, *args) -> None:
        """Schedule fn(*args) to run on the main thread via root.after(0, ...)."""
        self.root.after(0, fn, *args)

    # ── Backend init ──────────────────────────────────────────────────────────

    def _init_cloud(self) -> None:
        if self._config.cloud_provider == "onedrive":
            self._cloud = OneDriveProvider(self._config)
        else:
            self._cloud = DropboxProvider(self._config)
        self._manager._cloud = self._cloud

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        toolbar = tk.Frame(self.root, bg="#ececec", bd=1, relief=tk.FLAT)
        toolbar.pack(fill=tk.X, padx=2, pady=(3, 0))
        self._setup_toolbar(toolbar)

        search = tk.Frame(self.root, bg="#ffffff", bd=1, relief=tk.SUNKEN)
        search.pack(fill=tk.X, padx=2, pady=2)
        self._setup_search(search)

        tree_frame = tk.Frame(self.root)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=2)
        self._setup_treeview(tree_frame)

        status = tk.Frame(self.root, bg="#ececec", bd=1, relief=tk.SUNKEN)
        status.pack(fill=tk.X, side=tk.BOTTOM)
        self._setup_statusbar(status)

        banner = tk.Frame(self.root, bg="#ececec")
        banner.pack(fill=tk.X, side=tk.BOTTOM)
        self._setup_banner(banner)

    def _setup_toolbar(self, parent: tk.Frame) -> None:
        btn = dict(relief=tk.FLAT, bg="#ececec", padx=6, pady=3,
                   activebackground="#d0d0d0", cursor="hand2")
        left_buttons = [
            ("Select Folder", self._on_select_folder),
            ("Watch Folder",  self._on_watch_folder),
            ("Validate",      self._on_validate),
            ("Generate",      self._on_generate),
            ("Upload",        self._on_upload),
            ("Sync All",      self._on_sync_all),
        ]
        for label, cmd in left_buttons:
            tk.Button(parent, text=label, command=cmd, **btn).pack(
                side=tk.LEFT, padx=2, pady=2)

        tk.Button(parent, text="\u2699 Settings", command=self._on_settings, **btn
                  ).pack(side=tk.RIGHT, padx=2, pady=2)

    def _setup_search(self, parent: tk.Frame) -> None:
        PLACEHOLDER = "Search\u2026"

        self._search_entry = tk.Entry(parent, fg="#999", bg="white", relief=tk.FLAT,
                                       font=("TkDefaultFont", 10))
        self._search_entry.insert(0, PLACEHOLDER)

        def _focus_in(_e):
            if self._search_entry.get() == PLACEHOLDER:
                self._search_entry.delete(0, tk.END)
                self._search_entry.config(fg="#000")

        def _focus_out(_e):
            if not self._search_entry.get():
                self._search_entry.insert(0, PLACEHOLDER)
                self._search_entry.config(fg="#999")

        def _key_release(_e):
            q = self._search_entry.get()
            self._search_var.set("" if q == PLACEHOLDER else q)
            self._filter_tree()

        self._search_entry.bind("<FocusIn>",   _focus_in)
        self._search_entry.bind("<FocusOut>",  _focus_out)
        self._search_entry.bind("<KeyRelease>", _key_release)
        self._search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=3)

        def _clear():
            self._search_entry.delete(0, tk.END)
            self._search_entry.insert(0, PLACEHOLDER)
            self._search_entry.config(fg="#999")
            self._search_var.set("")
            self._filter_tree()

        tk.Button(parent, text="\u2715", relief=tk.FLAT, bg="white",
                  cursor="hand2", command=_clear).pack(side=tk.RIGHT, padx=2)

    def _setup_treeview(self, parent: tk.Frame) -> None:
        col_ids = [c[0] for c in self._COLS]
        self._tree = ttk.Treeview(parent, columns=col_ids,
                                   show="headings", selectmode="extended")

        for col_id, heading, width, anchor in self._COLS:
            self._tree.heading(col_id, text=heading,
                               command=lambda c=col_id: self._sort_by(c))
            stretch = col_id == "timestamp"
            self._tree.column(col_id, width=width, anchor=anchor, stretch=stretch)

        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL,   command=self._tree.yview)
        hsb = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        for tag, colour in self._TAG_BG.items():
            self._tree.tag_configure(tag, background=colour)

        self._tree.bind("<Button-3>",   self._show_context_menu)
        self._tree.bind("<Double-1>",   self._on_tree_double_click)
        self._tree.bind("<MouseWheel>", lambda e: self._tree.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

    def _setup_statusbar(self, parent: tk.Frame) -> None:
        lbl = dict(bg="#ececec", padx=6, pady=2, font=("TkDefaultFont", 8))
        sep = dict(bg="#ececec", fg="#bbb", font=("TkDefaultFont", 8))

        self._lbl_cloud  = tk.Label(parent, text="\u25cb Dropbox", **lbl)
        self._lbl_cloud.pack(side=tk.LEFT)
        tk.Label(parent, text="|", **sep).pack(side=tk.LEFT)

        self._lbl_scan   = tk.Label(parent, text="Not scanned", **lbl)
        self._lbl_scan.pack(side=tk.LEFT)
        tk.Label(parent, text="|", **sep).pack(side=tk.LEFT)

        self._lbl_count  = tk.Label(parent, text="0 jobs", **lbl)
        self._lbl_count.pack(side=tk.LEFT)

        self._lbl_errors = tk.Label(parent, text="", **lbl,
                                     fg="#c62828", cursor="hand2")
        self._lbl_errors.pack(side=tk.RIGHT, padx=6)
        self._lbl_errors.bind("<Button-1>", lambda _e: self._show_error_log())

    def _setup_banner(self, parent: tk.Frame) -> None:
        for fname in ("banner.png", "banner.jpg",
                      os.path.join("icons", "banner.png")):
            if os.path.isfile(fname):
                try:
                    from PIL import ImageTk as _ITK
                    img = _ITK.PhotoImage(file=fname)
                    lbl = tk.Label(parent, image=img, bg="#ececec")
                    lbl.image = img
                    lbl.pack()
                except Exception:
                    pass
                break

    def _load_icons(self) -> None:
        for name in ("folder", "watch", "validate", "generate",
                     "upload", "sync", "settings"):
            for ext in (".png", ".gif"):
                path = os.path.join("icons", name + ext)
                if os.path.isfile(path):
                    try:
                        self._icons[name] = tk.PhotoImage(file=path)
                    except Exception:
                        pass
                    break

    # ── Toolbar handlers ──────────────────────────────────────────────────────

    def _on_select_folder(self) -> None:
        current = self._db.get_setting("target_folder")
        folder = filedialog.askdirectory(
            title="Select Target / Jobs Folder",
            initialdir=current or os.path.expanduser("~"),
        )
        if folder:
            self._db.set_setting("target_folder", folder)
            log.info("Target folder set: %s", folder)
            self._run(self._scan_task)

    def _on_watch_folder(self) -> None:
        current = self._db.get_setting("watch_folder")
        folder = filedialog.askdirectory(
            title="Select Watch Folder",
            initialdir=current or os.path.expanduser("~"),
        )
        if folder:
            self._db.set_setting("watch_folder", folder)
            log.info("Watch folder set: %s", folder)
            self._run(self._scan_task)

    def _on_validate(self) -> None:
        self._run(self._scan_task)

    def _on_generate(self) -> None:
        selected = self._selected_job_ids()
        if not selected:
            messagebox.showinfo("Generate Stickers",
                                "Select one or more jobs first.")
            return
        eligible = [
            jid for jid in selected
            if (j := self._job_by_id(jid)) and j.sticker_status in ("Pending", "Error")
        ]
        if not eligible:
            messagebox.showinfo("Generate Stickers",
                                "No Pending or Error jobs in selection.")
            return
        self._mark_active(eligible)
        self._run(self._generate_task, eligible)

    def _on_upload(self) -> None:
        messagebox.showinfo("Upload to Cloud",
                            "Cloud upload coming in Phase 5.")

    def _on_sync_all(self) -> None:
        if not self._db.get_setting("target_folder"):
            messagebox.showwarning("Sync All", "Target folder is not configured.")
            return
        pending = [j.job_id for j in self._jobs if j.sticker_status == "Pending"]
        if pending:
            self._mark_active(pending)
            self._run(self._generate_task, pending)
        else:
            messagebox.showinfo("Sync All",
                                "No pending jobs to generate.\n"
                                "(Cloud upload coming in Phase 5.)")

    def _on_settings(self) -> None:
        messagebox.showinfo("Settings", "Settings dialog coming in Phase 4.")

    # ── Treeview management ───────────────────────────────────────────────────

    def _rebuild_tree(self, jobs: list[JobInfo]) -> None:
        """Clear and re-populate the tree, applying current search filter."""
        self._jobs = jobs
        self._tree.delete(*self._tree.get_children())
        query = self._search_var.get().strip().lower()
        for job in jobs:
            if query and not (
                query in job.job_id.lower()
                or query in job.sticker_status.lower()
                or query in job.cloud_status.lower()
            ):
                continue
            self._tree.insert(
                "", tk.END, iid=job.job_id,
                values=self._tree_values(job),
                tags=(self._tag_for_job(job),),
            )
        self._update_statusbar()

    def _update_tree_partial(self, fresh_jobs: list[JobInfo]) -> None:
        """Update only rows whose status changed — avoids full flicker on auto-refresh."""
        current_ids = {j.job_id for j in self._jobs}
        for job in fresh_jobs:
            if job.job_id not in current_ids:
                self._jobs.append(job)
                try:
                    self._tree.insert(
                        "", tk.END, iid=job.job_id,
                        values=self._tree_values(job),
                        tags=(self._tag_for_job(job),),
                    )
                except tk.TclError:
                    pass
            else:
                old = self._job_by_id(job.job_id)
                if old and (old.sticker_status != job.sticker_status
                            or old.cloud_status != job.cloud_status
                            or old.sticker_qty != job.sticker_qty):
                    old.sticker_status = job.sticker_status
                    old.cloud_status   = job.cloud_status
                    old.sticker_qty    = job.sticker_qty
                    old.cloud_progress = job.cloud_progress
                    old.last_updated   = job.last_updated
                    self._update_job_row(old)
        self._update_statusbar()

    def _update_job_row(self, job: JobInfo) -> None:
        """Refresh a single treeview row in-place. Safe to call from main thread only."""
        try:
            self._tree.item(job.job_id,
                            values=self._tree_values(job),
                            tags=(self._tag_for_job(job),))
        except tk.TclError:
            pass

    def _tag_for_job(self, job: JobInfo) -> str:
        if "Error" in (job.sticker_status, job.cloud_status):
            return "error"
        if job.sticker_status == "Completed" and job.cloud_status == "Uploaded":
            return "done"
        if job.cloud_status == "Cloud Only":
            return "cloud_only"
        return "partial"

    def _tree_values(self, job: JobInfo) -> tuple:
        if job.sticker_qty is not None:
            stickers_cell = str(job.sticker_qty)
        elif job.sticker_status == "Error":
            stickers_cell = "Missing: CSV"
        else:
            stickers_cell = "\u2014"
        ts = job.last_updated[:16].replace("T", " ") if job.last_updated else ""
        return (
            job.job_id,
            stickers_cell,
            job.sticker_status,
            job.cloud_status,
            job.cloud_progress,
            ts,
        )

    def _mark_active(self, job_ids: list[str]) -> None:
        for jid in job_ids:
            try:
                self._tree.item(jid, tags=("active",))
            except tk.TclError:
                pass

    def _filter_tree(self) -> None:
        if self._jobs:
            self._rebuild_tree(self._jobs)

    def _sort_by(self, col: str) -> None:
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False
        key_fn = self._SORT_KEYS.get(col, lambda j: j.job_id)
        self._jobs.sort(key=key_fn, reverse=self._sort_rev)
        self._rebuild_tree(self._jobs)

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_context_menu(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        self._tree.selection_set(iid)
        job = self._job_by_id(iid)
        if not job:
            return

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Open Job Folder",
                         command=lambda: self._open_job_folder(iid))
        menu.add_command(label="Revalidate Job",
                         command=lambda: self._run(self._revalidate_task, iid))
        menu.add_separator()

        if job.sticker_status in ("Pending", "Error"):
            menu.add_command(label="Generate Stickers",
                             command=self._on_generate)
        if job.cloud_status == "Local":
            menu.add_command(label="Upload to Cloud",
                             command=self._on_upload)
        if job.cloud_status == "Cloud Only":
            menu.add_command(label="Download from Cloud",
                             command=lambda: messagebox.showinfo(
                                 "Download", "Download coming in Phase 5."))
        menu.add_command(label="Open Cloud Link",
                         command=lambda: self._open_cloud_link(iid))
        menu.add_separator()
        menu.add_command(label="Copy Job ID",
                         command=lambda: self._copy_job_id(iid))
        menu.add_separator()
        menu.add_command(label="Refresh", command=self._on_validate)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_tree_double_click(self, event: tk.Event) -> None:
        iid = self._tree.identify_row(event.y)
        if iid:
            self._open_job_folder(iid)

    def _open_job_folder(self, job_id: str) -> None:
        target = self._db.get_setting("target_folder")
        if not target:
            messagebox.showwarning("Open Folder", "Target folder not configured.")
            return
        path = os.path.join(target, job_id)
        if os.path.isdir(path):
            try:
                os.startfile(path)
            except Exception:
                logging.exception("Failed to open folder: %s", path)
        else:
            messagebox.showwarning("Open Folder",
                                   f"Folder not found:\n{path}")

    def _open_cloud_link(self, job_id: str) -> None:
        messagebox.showinfo("Cloud Link",
                            "Cloud share links available after Phase 5.")

    def _copy_job_id(self, job_id: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(job_id)

    # ── Background tasks ──────────────────────────────────────────────────────

    def _scan_task(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            log.debug("Scan already in progress; skipping")
            return
        try:
            self._ui(lambda: self._lbl_scan.config(text="Scanning\u2026"))
            jobs = self._manager.scan_all()

            # Recover sticker tokens for completed jobs
            target = self._db.get_setting("target_folder")
            if target:
                for job in jobs:
                    if job.sticker_status == "Completed":
                        folder = os.path.join(target, job.job_id)
                        if os.path.isdir(folder):
                            self._engine.recover_tokens_from_summary(
                                folder, job.job_id)

            ts = datetime.now().strftime("%H:%M")
            self._last_scan = ts
            self._ui(self._rebuild_tree, jobs)
            self._ui(lambda: self._lbl_scan.config(text=f"Last scan: {ts}"))
        except Exception:
            logging.exception("Scan task failed")
            self._session_errors += 1
            self._ui(self._update_statusbar)
        finally:
            self._scan_lock.release()

    def _generate_task(self, job_ids: list[str]) -> None:
        target = self._db.get_setting("target_folder")
        if not target:
            self._ui(messagebox.showwarning, "Generate",
                     "Target folder is not configured.")
            return

        success, errors = 0, []
        for job_id in job_ids:
            job_path = os.path.join(target, job_id)
            try:
                self._engine.generate_pdf(job_path)
                qty = JobManager._read_qty_from_summary(
                    os.path.join(job_path, f"{job_id}_Stickers_Summary.txt"))

                job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                job.sticker_status = "Completed"
                job.sticker_qty    = qty
                if job.cloud_status in ("Pending", "CSV Only", "Incomplete"):
                    job.cloud_status   = "Local"
                    job.cloud_progress = "50%"
                job.last_updated = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                success += 1
                _j = job  # capture for lambda
                self._ui(lambda j=_j: self._update_job_row(j))
            except Exception as exc:
                logging.exception("Generate failed for %s", job_id)
                self._session_errors += 1
                errors.append(f"{job_id}: {exc}")
                err_job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                err_job.sticker_status = "Error"
                err_job.last_updated   = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(err_job)
                _ej = err_job
                self._ui(lambda j=_ej: self._update_job_row(j))

        self._ui(self._update_statusbar)
        msg = f"Generated {success} job(s) successfully."
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors)
            self._ui(messagebox.showwarning, "Generate Complete", msg)
        else:
            self._ui(messagebox.showinfo, "Generate Complete", msg)

    def _revalidate_task(self, job_id: str) -> None:
        try:
            status, qty = self._manager.classify_sticker_status(job_id)
            job = self._job_by_id(job_id)
            if job:
                job.sticker_status = status
                job.sticker_qty    = qty
                job.last_updated   = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                _j = job
                self._ui(lambda j=_j: self._update_job_row(j))
        except Exception:
            logging.exception("Revalidate failed for %s", job_id)

    # ── Auto-refresh ──────────────────────────────────────────────────────────

    def _schedule_refresh(self) -> None:
        try:
            interval_s = int(self._db.get_setting("auto_refresh_seconds", "30"))
        except ValueError:
            interval_s = 30
        self._refresh_id = self.root.after(interval_s * 1000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        """Called on main thread by root.after — kicks off DB read in background."""
        self._run(self._auto_refresh_task)
        self._schedule_refresh()

    def _auto_refresh_task(self) -> None:
        """Read DB and push only changed rows to the treeview."""
        try:
            fresh = self._db.get_all_jobs()
            self._ui(self._update_tree_partial, fresh)
        except Exception:
            logging.exception("Auto-refresh task failed")

    # ── Status bar ────────────────────────────────────────────────────────────

    def _update_statusbar(self) -> None:
        auth = False
        try:
            auth = self._cloud.is_authenticated() if self._cloud else False
        except Exception:
            pass
        provider = self._config.cloud_provider.capitalize()
        dot = "\u25cf" if auth else "\u25cb"
        self._lbl_cloud.config(text=f"{dot} {provider}")
        self._lbl_count.config(text=f"{len(self._jobs)} job(s)")
        if self._session_errors:
            self._lbl_errors.config(
                text=f"\u26a0 {self._session_errors} error(s) \u2014 View Log")
        else:
            self._lbl_errors.config(text="")

    def _show_error_log(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Error Log")
        win.geometry("620x420")
        win.transient(self.root)
        txt = scrolledtext.ScrolledText(win, state="normal", wrap=tk.WORD,
                                         font=("Courier", 9))
        txt.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        log_path = self._db.get_setting("log_path", "builders_qr_labels.log")
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    txt.insert(tk.END, fh.read())
                txt.see(tk.END)
            except Exception as exc:
                txt.insert(tk.END, f"Could not read log file: {exc}")
        else:
            txt.insert(tk.END,
                       "(No log file yet.\n"
                       "File logging is added in Phase 7 via Settings → Log Path.)")
        txt.config(state="disabled")

    # ── Misc helpers ──────────────────────────────────────────────────────────

    def _selected_job_ids(self) -> list[str]:
        return list(self._tree.selection())

    def _job_by_id(self, job_id: str) -> Optional[JobInfo]:
        return next((j for j in self._jobs if j.job_id == job_id), None)


# ════════════════════════════════════════════════════════════════════════════════
# generate_share_qr — standalone utility (Phase 6)
# ════════════════════════════════════════════════════════════════════════════════

def generate_share_qr(share_url: str, output_path: str) -> None:
    """Generate an SVG QR code for a cloud share URL. Called after successful upload."""
    qr = qrcode.QRCode(box_size=10, border=1)
    qr.add_data(share_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    img.save(output_path)
    log.info("Share QR saved: %s", output_path)


# ════════════════════════════════════════════════════════════════════════════════
# Entry point — Phase 3 adds BuildersQRLabelsApp (Tkinter UI)
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    root = tk.Tk()
    app = BuildersQRLabelsApp(root)
    root.mainloop()
