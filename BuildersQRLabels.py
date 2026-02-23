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
    config = AppConfig()
    db = JobDatabase(config.db_path)
    log.info("DB available: %s  path: %s", db.is_available(), db._path)
    log.info("BuildersQRLabels — Phase 2 core classes loaded. UI coming in Phase 3.")
