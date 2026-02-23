"""
BuildersQRLabels.py — Builders Connect QR Label & Cloud Sync Manager
Builders Inc. — single-file desktop application (Windows only, Python 3.10+)

Entry point:  python BuildersQRLabels.py
Architecture: AppConfig → JobDatabase → JobManager → StickerEngine
              CloudProvider (ABC) → DropboxProvider / OneDriveProvider
              BuildersQRLabelsApp (Tkinter UI — Phase 3)
"""

__version__ = "0.9.0"

# ── Standard library ──────────────────────────────────────────────────────────
import json
import logging
import os
import re
import socket
import sqlite3
import tempfile
import threading
import time
import uuid
import webbrowser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

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
from qrcode.constants import ERROR_CORRECT_L
from qrcode.image.svg import SvgPathImage
import requests
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas as PDFCanvas
from reportlab.graphics import renderPDF
from svglib.svglib import svg2rlg

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

_log_file_handler: Optional[logging.FileHandler] = None


def configure_log_file(path: str) -> None:
    """
    Attach (or re-attach) a FileHandler to the root logger using *path*.
    Safe to call multiple times — the previous file handler is removed first.
    Does nothing if *path* is empty or the file cannot be opened.
    """
    global _log_file_handler
    root_logger = logging.getLogger()

    if _log_file_handler is not None:
        root_logger.removeHandler(_log_file_handler)
        _log_file_handler.close()
        _log_file_handler = None

    if not path:
        return

    try:
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        root_logger.addHandler(handler)
        _log_file_handler = handler
        log.info("Log file: %s", path)
    except OSError:
        log.warning("Cannot open log file: %s", path)

# ── Module-level constants ────────────────────────────────────────────────────
CONFIG_FILE = "config.json"
TOKEN_DROPBOX_FILE = "token_dropbox.json"
TOKEN_ONEDRIVE_FILE = "token_onedrive.json"
QR_SUFFIX = "_QR.svg"
DROPBOX_UPLOAD_ROOT = "/Cloud Manager Uploads"
JOB_ID_RE = re.compile(r"^\d{6}-\d{3}$")
GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/drive"


# ════════════════════════════════════════════════════════════════════════════════
# JobInfo — shared data record
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class JobInfo:
    """Single job record — persisted in the database and displayed in the UI."""
    job_id: str
    sticker_status: str = "Pending"    # Pending / Completed / Error
    cloud_status: str = "Pending"      # Pending / Provisioned / Local / Uploaded / Cloud Only / Error
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
    Shared path config (jobs_folder, log_path, etc.) lives in JobDatabase.settings.
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
# UpdateChecker — startup version check against network share
# ════════════════════════════════════════════════════════════════════════════════

class UpdateChecker:
    """
    Checks for a newer version by reading version.json from the same directory
    as the database file on the network share.

    version.json schema (IT maintains this file on the share):
      {
        "version": "0.9.1",
        "installer_path": "\\\\SERVER\\BuildersQRLabels\\BuildersConnect-0.9.1-Setup.exe",
        "release_notes": "Bug fixes and improvements"
      }

    Runs in a daemon thread — silently no-ops if the file is missing or the
    network is unavailable.  Calls notify_callback(data) on the main thread
    when a newer version is found.
    """

    VERSION_FILENAME = "version.json"

    def __init__(self, current_version: str, db_path: str, notify_callback) -> None:
        self._current = tuple(int(x) for x in current_version.split("."))
        version_dir = os.path.dirname(os.path.abspath(db_path)) if db_path else ""
        self._version_file = os.path.join(version_dir, self.VERSION_FILENAME) if version_dir else ""
        self._notify = notify_callback

    def check_async(self) -> None:
        """Start a background thread to check for updates."""
        t = threading.Thread(target=self._check, daemon=True)
        t.start()

    def _check(self) -> None:
        if not self._version_file:
            return
        try:
            with open(self._version_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            remote = tuple(int(x) for x in str(data["version"]).split("."))
            if remote > self._current:
                self._notify(data)
        except Exception:
            pass  # network unavailable or file missing — silent no-op


# ════════════════════════════════════════════════════════════════════════════════
# JobDatabase — SQLite wrapper (jobs + settings tables)
# ════════════════════════════════════════════════════════════════════════════════

class JobDatabase:
    """
    Wraps SQLite.  Owns two tables:
      jobs     — one row per job_id (sticker + cloud status)
      settings — key/value shared config (jobs_folder, log_path, etc.)

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

    def delete_job(self, job_id: str) -> None:
        if not self._available:
            return
        with self._lock:
            try:
                conn = self._open()
                conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                conn.commit()
                conn.close()
            except Exception:
                logging.exception("delete_job failed for %s", job_id)

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

    def reconnect(self, new_path: str) -> None:
        """Switch to a new database path (called when the user changes DB Path in Settings)."""
        with self._lock:
            self._path = new_path.strip() or self.LOCAL_FALLBACK
            self._available = False
        self._connect()


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

    def create_folder_and_get_link(self, job_id: str) -> str:
        """Create the cloud folder for job_id and return a public share URL."""
        cloud_folder = self.ensure_folder(job_id)
        return self.create_share_link(cloud_folder)


# ════════════════════════════════════════════════════════════════════════════════
# DropboxProvider
# ════════════════════════════════════════════════════════════════════════════════

class DropboxProvider(CloudProvider):
    """Dropbox implementation via the Dropbox Python SDK."""

    _CACHE_TTL = 5                       # seconds for job-list cache
    _CHUNK_SIZE = 150 * 1024 * 1024     # bytes — threshold for chunked upload session

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

    # ── Cloud operations ──────────────────────────────────────────────────────

    def list_jobs(self) -> dict[str, str]:
        """Return {job_id: cloud_folder_path} for all 7-digit job folders.  TTL-cached."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        now = time.time()
        if self._jobs_cache is not None and (now - self._jobs_cache_time) < self._CACHE_TTL:
            return self._jobs_cache
        jobs: dict[str, str] = {}
        try:
            res = self._dbx.files_list_folder(DROPBOX_UPLOAD_ROOT)
        except Exception as exc:
            raise RuntimeError(f"Failed to list Dropbox jobs folder: {exc}") from exc
        def _process(entries: list) -> None:
            for ent in entries:
                if isinstance(ent, dropbox.files.FolderMetadata):
                    name = ent.name.strip()
                    if JOB_ID_RE.match(name):
                        jobs[name] = f"{DROPBOX_UPLOAD_ROOT}/{name}"
        _process(res.entries)
        while res.has_more:
            res = self._dbx.files_list_folder_continue(res.cursor)
            _process(res.entries)
        self._jobs_cache = jobs
        self._jobs_cache_time = now
        return jobs

    def ensure_folder(self, job_id: str) -> str:
        """Return the cloud folder path, creating it if it does not exist."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        folder_path = f"{DROPBOX_UPLOAD_ROOT}/{job_id}"
        try:
            md = self._dbx.files_get_metadata(folder_path)
            if isinstance(md, dropbox.files.FolderMetadata):
                return folder_path
        except Exception:
            pass
        try:
            self._dbx.files_create_folder_v2(folder_path)
        except Exception as exc:
            raise RuntimeError(f"Cannot create Dropbox folder for job {job_id}: {exc}") from exc
        self._jobs_cache = None  # invalidate cache
        return folder_path

    def upload_file(self, local_path: str, cloud_folder: str) -> None:
        """Upload a single file.  Uses a chunked session for files > _CHUNK_SIZE."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        dest = f"{cloud_folder}/{os.path.basename(local_path)}"
        file_size = os.path.getsize(local_path)
        try:
            if file_size <= self._CHUNK_SIZE:
                with open(local_path, "rb") as fh:
                    self._dbx.files_upload(fh.read(), dest, mode=WriteMode("overwrite"))
            else:
                with open(local_path, "rb") as fh:
                    session = self._dbx.files_upload_session_start(fh.read(self._CHUNK_SIZE))
                    offset = self._CHUNK_SIZE
                    while offset < file_size:
                        cursor = dropbox.files.UploadSessionCursor(
                            session_id=session.session_id, offset=offset
                        )
                        chunk = fh.read(self._CHUNK_SIZE)
                        if offset + len(chunk) >= file_size:
                            commit = dropbox.files.CommitInfo(
                                path=dest, mode=WriteMode("overwrite")
                            )
                            self._dbx.files_upload_session_finish(chunk, cursor, commit)
                        else:
                            self._dbx.files_upload_session_append_v2(chunk, cursor)
                        offset += len(chunk)
        except Exception as exc:
            raise RuntimeError(
                f"Dropbox upload failed for {os.path.basename(local_path)}: {exc}"
            ) from exc
        log.info("Uploaded %s → %s", local_path, dest)

    def download_file(self, cloud_path: str, local_path: str) -> None:
        """Download a single file from Dropbox to local_path."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        try:
            _, res = self._dbx.files_download(cloud_path)
            parent = os.path.dirname(local_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(local_path, "wb") as fh:
                fh.write(res.content)
        except Exception as exc:
            raise RuntimeError(f"Dropbox download failed for {cloud_path}: {exc}") from exc
        log.info("Downloaded %s → %s", cloud_path, local_path)

    def create_share_link(self, cloud_folder: str) -> str:
        """Return an existing public link or create a new one for the folder."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        try:
            links = self._dbx.sharing_list_shared_links(
                path=cloud_folder, direct_only=True
            ).links
            if links:
                return links[0].url
        except Exception:
            pass
        try:
            settings = SharedLinkSettings(requested_visibility=RequestedVisibility.public)
            link = self._dbx.sharing_create_shared_link_with_settings(cloud_folder, settings)
            return link.url
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create Dropbox share link for {cloud_folder}: {exc}"
            ) from exc

    def list_folder_files(self, cloud_folder: str) -> list[str]:
        """Return filenames of all files (not folders) in the cloud folder."""
        if not self._dbx:
            raise RuntimeError("Not authenticated with Dropbox.")
        files: list[str] = []
        try:
            res = self._dbx.files_list_folder(cloud_folder)
            while True:
                for ent in res.entries:
                    if isinstance(ent, dropbox.files.FileMetadata):
                        files.append(ent.name)
                if not res.has_more:
                    break
                res = self._dbx.files_list_folder_continue(res.cursor)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to list Dropbox folder {cloud_folder}: {exc}"
            ) from exc
        return files


# ════════════════════════════════════════════════════════════════════════════════
# OneDriveProvider
# ════════════════════════════════════════════════════════════════════════════════

class OneDriveProvider(CloudProvider):
    """Microsoft OneDrive implementation via Graph API + MSAL device-code flow."""

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
        cache = self._msal_app.token_cache if self._msal_app else None
        if not isinstance(cache, msal.SerializableTokenCache):
            return
        if cache.has_state_changed:
            try:
                with open(self._token_file, "w", encoding="utf-8") as fh:
                    fh.write(cache.serialize())
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
        if not self._msal_app:
            return False
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

    # ── Graph API helper ──────────────────────────────────────────────────────

    def _graph_request(self, method: str, endpoint: str, **kwargs: Any) -> requests.Response:
        """Make an authenticated request to GRAPH_BASE + endpoint."""
        token = self._get_access_token()
        if not token:
            raise RuntimeError("Not authenticated with OneDrive.")
        headers: dict[str, str] = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(method, f"{GRAPH_BASE}{endpoint}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp

    # ── Cloud operations ──────────────────────────────────────────────────────

    def list_jobs(self) -> dict[str, str]:
        """Return {job_id: cloud_folder_path} for all 7-digit job folders."""
        try:
            resp = self._graph_request("GET", "/root:/Cloud Manager Uploads:/children")
            jobs: dict[str, str] = {}
            for item in resp.json().get("value", []):
                name = item.get("name", "").strip()
                if JOB_ID_RE.match(name) and "folder" in item:
                    jobs[name] = f"/Cloud Manager Uploads/{name}"
            return jobs
        except Exception as exc:
            raise RuntimeError(f"Failed to list OneDrive jobs: {exc}") from exc

    def ensure_folder(self, job_id: str) -> str:
        """Return the cloud folder path, creating it if it does not exist."""
        folder_path = f"/Cloud Manager Uploads/{job_id}"
        try:
            self._graph_request("GET", f"/root:{folder_path}:")
            return folder_path
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise RuntimeError(
                    f"Error checking OneDrive folder for job {job_id}: {exc}"
                ) from exc
        try:
            self._graph_request(
                "POST",
                "/root:/Cloud Manager Uploads:/children",
                json={"name": job_id, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
        except Exception as exc:
            raise RuntimeError(
                f"Cannot create OneDrive folder for job {job_id}: {exc}"
            ) from exc
        return folder_path

    def upload_file(self, local_path: str, cloud_folder: str) -> None:
        """Upload a single file via Graph API PUT (≤ 250 MB per request)."""
        filename = os.path.basename(local_path)
        try:
            with open(local_path, "rb") as fh:
                data = fh.read()
            self._graph_request(
                "PUT",
                f"/root:{cloud_folder}/{filename}:/content",
                data=data,
                headers={"Content-Type": "application/octet-stream"},
            )
        except Exception as exc:
            raise RuntimeError(f"OneDrive upload failed for {filename}: {exc}") from exc
        log.info("Uploaded %s → OneDrive%s/%s", local_path, cloud_folder, filename)

    def download_file(self, cloud_path: str, local_path: str) -> None:
        """Download a single file from OneDrive to local_path."""
        try:
            resp = self._graph_request("GET", f"/root:{cloud_path}:/content")
            parent = os.path.dirname(local_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(local_path, "wb") as fh:
                fh.write(resp.content)
        except Exception as exc:
            raise RuntimeError(f"OneDrive download failed for {cloud_path}: {exc}") from exc
        log.info("Downloaded OneDrive%s → %s", cloud_path, local_path)

    def create_share_link(self, cloud_folder: str) -> str:
        """Create an anonymous view link for the folder and return its URL."""
        try:
            item_resp = self._graph_request("GET", f"/root:{cloud_folder}:")
            item_id = item_resp.json()["id"]
            link_resp = self._graph_request(
                "POST",
                f"/items/{item_id}/createLink",
                json={"type": "view", "scope": "anonymous"},
            )
            return link_resp.json()["link"]["webUrl"]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to create OneDrive share link for {cloud_folder}: {exc}"
            ) from exc

    def list_folder_files(self, cloud_folder: str) -> list[str]:
        """Return filenames of all files (not folders) in the cloud folder."""
        try:
            resp = self._graph_request("GET", f"/root:{cloud_folder}:/children")
            return [
                item["name"]
                for item in resp.json().get("value", [])
                if "file" in item
            ]
        except Exception as exc:
            raise RuntimeError(
                f"Failed to list OneDrive folder {cloud_folder}: {exc}"
            ) from exc


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
    def jobs_folder(self) -> str:
        val = self._db.get_setting("jobs_folder")
        if not val:                               # migrate from old key on first run
            val = self._db.get_setting("target_folder")
            if val:
                self._db.set_setting("jobs_folder", val)
        return os.path.normpath(val) if val else val

    # ── Main scan ────────────────────────────────────────────────────────────

    def scan_all(self) -> list[JobInfo]:
        """
        Scan jobs_folder, classify all jobs, upsert to DB.
        Returns sorted list of JobInfo.
        """
        folder = self.jobs_folder
        jobs: dict[str, JobInfo] = {}

        if folder and os.path.isdir(folder):
            for name in os.listdir(folder):
                if JOB_ID_RE.match(name) and os.path.isdir(os.path.join(folder, name)):
                    status, qty = self.classify_sticker_status(name)
                    jobs[name] = JobInfo(job_id=name, sticker_status=status, sticker_qty=qty)

        # Cloud status
        cloud_jobs: dict[str, str] = {}
        if self._cloud and self._cloud.is_authenticated():
            try:
                cloud_jobs = self._cloud.list_jobs()
            except Exception:
                logging.exception("Cloud job list failed")

        for job_id, info in jobs.items():
            db_job = self._db.get_job(job_id)
            db_status = db_job.cloud_status if db_job else "Pending"
            info.cloud_status, info.cloud_progress = self._classify_cloud_status(
                job_id, cloud_jobs, db_status
            )
            self._db.upsert_job(info)

        # Prune DB records with no local folder.
        # Cloud-terminal statuses (Uploaded, Cloud Only) are kept — folder may be
        # intentionally absent after upload.  Everything else is an orphan.
        _KEEP_STATUSES = {"Uploaded", "Cloud Only"}
        pruned = 0
        for db_job in self._db.get_all_jobs():
            if db_job.job_id not in jobs and db_job.cloud_status not in _KEEP_STATUSES:
                self._db.delete_job(db_job.job_id)
                pruned += 1
                log.info("Pruned orphaned job from DB: %s", db_job.job_id)
        if pruned:
            log.info("Pruned %d orphaned job(s) from DB", pruned)

        result = sorted(jobs.values(), key=lambda j: j.job_id)
        log.info("Scan complete: %d jobs found", len(result))
        return result

    # ── Sticker status ────────────────────────────────────────────────────────

    def classify_sticker_status(self, job_id: str) -> tuple[str, Optional[int]]:
        """
        Return (status, qty_or_None) by inspecting the job folder contents.
        Requires jobs_folder to be set.
        """
        target = self.jobs_folder
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
        self, job_id: str, cloud_jobs: dict[str, str], db_status: str = "Pending"
    ) -> tuple[str, str]:
        """Return (cloud_status, progress_pct)."""
        folder = self.jobs_folder
        in_cloud = job_id in cloud_jobs
        has_local = bool(folder) and os.path.isdir(os.path.join(folder, job_id))

        if in_cloud and not has_local:
            return "Cloud Only", "100%"
        if in_cloud and db_status == "Uploaded":
            return "Uploaded", "100%"
        if not has_local:
            return "Pending", "0%"

        # Local only — granular progress
        files = os.listdir(os.path.join(folder, job_id))
        has_pdf  = f"Stickers_{job_id}.pdf" in files
        has_qr   = any("_qr.svg" in f.lower() for f in files)

        if in_cloud and has_pdf:
            return "Uploaded", "100%"
        if has_pdf:
            return "Local", "66%"
        if has_qr:
            return "Provisioned", "33%"
        return "Pending", "0%"

    # ── Cloud upload / download ───────────────────────────────────────────────

    def provision_job(self, job_id: str, cloud: CloudProvider) -> str:
        """
        Create the cloud folder for job_id, get the share URL, write {job_id}_QR.svg locally.
        Returns the share URL.  Raises on failure.
        Must be called before generate_pdf so the QR code is valid.
        """
        folder = self.jobs_folder
        if not folder:
            raise RuntimeError("Jobs folder is not configured.")
        job_folder = os.path.join(folder, job_id)
        if not os.path.isdir(job_folder):
            raise RuntimeError(f"Job folder not found: {job_folder}")

        share_url = cloud.create_folder_and_get_link(job_id)
        qr_path = os.path.join(job_folder, f"{job_id}{QR_SUFFIX}")
        generate_share_qr(share_url, qr_path)
        log.info("Provisioned job %s → %s", job_id, share_url)
        return share_url

    def upload_job(self, job_id: str, cloud: CloudProvider) -> None:
        """
        Upload all local files from the jobs folder to the already-provisioned cloud folder.
        Skips {job_id}_JOB_* files.
        """
        folder = self.jobs_folder
        if not folder:
            raise RuntimeError("Jobs folder is not configured.")
        job_folder = os.path.join(folder, job_id)
        if not os.path.isdir(job_folder):
            raise RuntimeError(f"Local job folder not found: {job_folder}")

        cloud_folder = cloud.ensure_folder(job_id)

        for fname in os.listdir(job_folder):
            if re.match(rf"{re.escape(job_id)}_JOB_", fname, flags=re.IGNORECASE):
                continue
            fpath = os.path.join(job_folder, fname)
            if os.path.isfile(fpath):
                log.info("Uploading %s for job %s", fname, job_id)
                cloud.upload_file(fpath, cloud_folder)

        log.info("Job %s uploaded to cloud.", job_id)

    def download_job(self, job_id: str, cloud: CloudProvider, cloud_folder: str) -> None:
        """
        Download CSV, PDF, SVG, and TXT files for job_id from cloud to the
        jobs folder.  Skips _JOB_* files.
        """
        folder = self.jobs_folder
        if not folder:
            raise RuntimeError("Jobs folder is not configured.")
        job_folder = os.path.join(folder, job_id)
        os.makedirs(job_folder, exist_ok=True)

        for fname in cloud.list_folder_files(cloud_folder):
            if re.match(rf"{re.escape(job_id)}_JOB_", fname, flags=re.IGNORECASE):
                continue
            if fname.lower().endswith((".pdf", ".csv", ".svg", ".txt")):
                cloud_path = f"{cloud_folder}/{fname}"
                local_path = os.path.join(job_folder, fname)
                log.info("Downloading %s for job %s", fname, job_id)
                try:
                    cloud.download_file(cloud_path, local_path)
                except Exception:
                    logging.exception("Failed to download %s", fname)

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

            # Filter to this job's rows — jobnumber stored as "XXXXXX-XXX" string
            df = df[df["jobnumber"].astype(str).str.strip() == job_id]
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
                error_correction=ERROR_CORRECT_L,
                box_size=10,
                border=1,
            )
            qr.add_data(qr_data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            fd, tmp_png = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            with open(tmp_png, "wb") as _f:
                img.save(_f)
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
        c.drawString(left_qr_x, row1_y, job_id)

        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(tx_center, row1_y, truss["customer"])

        c.setFont("Helvetica-Bold", 7)
        c.drawRightString(W - m, row1_y, sticker_type)

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


def _center_window(win: Union[tk.Tk, tk.Toplevel], w: int, h: int) -> None:
    """Position *win* at the center of the primary screen."""
    win.update_idletasks()
    x = (win.winfo_screenwidth()  - w) // 2
    y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"{w}x{h}+{x}+{y}")


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
        "done":        "#c8e6c9",  # sticker complete + uploaded
        "provisioned": "#fff9c4",  # provisioned (QR SVG ready), not yet generated
        "local":       "#ffcdd2",  # sticker generated but not uploaded (red = action needed)
        "cloud_only":  "#bbdefb",  # exists in cloud but not locally
        "active":      "#bbdefb",  # in-progress (generating / uploading)
        "error":       "#ffcdd2",  # any error
        "partial":     "#fff9c4",  # generic pending
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
        self.root.title(f"Builders Connect — QR Labels  v{__version__}")
        self.root.minsize(600, 450)
        _center_window(self.root, 700, 650)

        # ── Backend objects ───────────────────────────────────────────────────
        self._config  = AppConfig()
        self._db      = JobDatabase(self._config.db_path)
        self._cloud: Optional[CloudProvider] = None
        self._manager = JobManager(self._db)
        self._engine  = StickerEngine(self._config)
        self._init_cloud()

        # Attach file log handler using path from shared DB settings
        configure_log_file(self._db.get_setting("log_path"))

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

        # ── Check for updates (non-blocking, silent if network unavailable) ───
        self._update_bar: Optional[tk.Frame] = None
        UpdateChecker(
            __version__,
            self._config.db_path,
            lambda data: self._ui(self._show_update_banner, data),
        ).check_async()

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
        self._status_frame = status
        self._setup_statusbar(status)

        banner = tk.Frame(self.root, bg="#ececec")
        banner.pack(fill=tk.X, side=tk.BOTTOM)
        self._setup_banner(banner)

        self._bind_shortcuts()

    def _bind_shortcuts(self) -> None:
        """Register global keyboard shortcuts on the root window."""
        self.root.bind("<F5>",               lambda _e: self._on_validate())
        self.root.bind("<Control-p>",        lambda _e: self._on_provision())
        self.root.bind("<Control-g>",        lambda _e: self._on_generate())
        self.root.bind("<Control-u>",        lambda _e: self._on_upload())
        self.root.bind("<Control-S>",        lambda _e: self._on_process_all())   # Ctrl+Shift+S
        self.root.bind("<Control-comma>",    lambda _e: self._on_settings())
        self.root.bind("<Control-a>",        lambda _e: self._tree.selection_set(
                                                self._tree.get_children()))
        self.root.bind("<Escape>",           lambda _e: self._tree.selection_remove(
                                                self._tree.selection()))

    def _setup_toolbar(self, parent: tk.Frame) -> None:
        btn: dict[str, Any] = dict(relief=tk.FLAT, bg="#ececec", padx=6, pady=3,
                                   activebackground="#d0d0d0", cursor="hand2")
        left_buttons = [
            ("Refresh",       self._on_validate),
            ("Provision",     self._on_provision),
            ("Generate",      self._on_generate),
            ("Upload",        self._on_upload),
            ("Process All",   self._on_process_all),
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

    def _show_update_banner(self, data: dict) -> None:
        """Show a dismissible yellow update-available bar above the status bar."""
        if self._update_bar is not None:
            return  # already shown
        version = data.get("version", "?")
        installer_path = data.get("installer_path", "")
        notes = data.get("release_notes", "")
        msg = f"Update available: v{version}"
        if notes:
            msg += f" — {notes}"

        bar = tk.Frame(self.root, bg="#fff59d", bd=1, relief=tk.FLAT)
        bar.pack(fill=tk.X, side=tk.BOTTOM, before=self._status_frame)
        self._update_bar = bar

        tk.Label(bar, text=msg, bg="#fff59d", padx=8, pady=3,
                 font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT)

        if installer_path:
            installer_dir = os.path.dirname(installer_path)
            tk.Button(
                bar, text="Open Installer Folder",
                command=lambda: os.startfile(installer_dir),
                relief=tk.FLAT, bg="#f9a825", fg="#000",
                padx=6, pady=2, cursor="hand2",
            ).pack(side=tk.LEFT, padx=(4, 0), pady=2)

        tk.Button(
            bar, text="Dismiss",
            command=self._dismiss_update_banner,
            relief=tk.FLAT, bg="#fff59d", fg="#555",
            padx=6, pady=2, cursor="hand2",
        ).pack(side=tk.RIGHT, padx=4, pady=2)

    def _dismiss_update_banner(self) -> None:
        if self._update_bar is not None:
            self._update_bar.destroy()
            self._update_bar = None

    def _load_icons(self) -> None:
        for name in ("folder", "refresh", "provision", "generate",
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
        current = self._manager.jobs_folder
        folder = filedialog.askdirectory(
            title="Select Jobs Folder",
            initialdir=current or os.path.expanduser("~"),
        )
        if folder:
            folder = os.path.normpath(folder)
            self._db.set_setting("jobs_folder", folder)
            log.info("Jobs folder set: %s", folder)
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

    def _on_provision(self) -> None:
        if not self._cloud or not self._cloud.is_authenticated():
            messagebox.showwarning("Provision",
                                   "Cloud provider is not authenticated.\n"
                                   "Open Settings to connect.")
            return
        selected = self._selected_job_ids()
        if not selected:
            messagebox.showinfo("Provision", "Select one or more jobs first.")
            return
        eligible = [
            jid for jid in selected
            if (j := self._job_by_id(jid)) and j.cloud_status in ("Pending", "Error")
        ]
        if not eligible:
            messagebox.showinfo("Provision", "No Pending jobs in selection.")
            return
        self._mark_active(eligible)
        self._run(self._provision_task, eligible)

    def _on_upload(self) -> None:
        if not self._cloud or not self._cloud.is_authenticated():
            messagebox.showwarning("Upload",
                                   "Cloud provider is not authenticated.\n"
                                   "Open Settings to connect.")
            return
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("Upload", "Select one or more jobs to upload.")
            return
        eligible = [
            jid for jid in selected
            if (j := self._job_by_id(jid))
            and j.sticker_status == "Completed"
            and j.cloud_status != "Uploaded"
        ]
        if not eligible:
            messagebox.showinfo("Upload",
                                "No eligible jobs in selection.\n"
                                "Jobs must have Completed stickers and not already be Uploaded.")
            return
        self._mark_active(eligible)
        self._run(self._upload_task, eligible)

    def _on_process_all(self) -> None:
        if not self._manager.jobs_folder:
            messagebox.showwarning("Process All", "Jobs folder is not configured.")
            return
        cloud_ok = bool(self._cloud and self._cloud.is_authenticated())
        to_provision = (
            [j.job_id for j in self._jobs if j.cloud_status == "Pending"]
            if cloud_ok else []
        )
        to_generate = [
            j.job_id for j in self._jobs
            if j.sticker_status == "Pending"
            and j.cloud_status in ("Provisioned", "Local")
        ]
        to_upload = (
            [
                j.job_id for j in self._jobs
                if j.sticker_status == "Completed"
                and j.cloud_status not in ("Uploaded", "Cloud Only")
            ]
            if cloud_ok else []
        )
        if not to_provision and not to_generate and not to_upload:
            messagebox.showinfo("Process All", "All jobs are up to date.")
            return
        all_active = list(dict.fromkeys(to_provision + to_generate + to_upload))
        self._mark_active(all_active)
        self._run(self._process_all_task, to_provision, to_generate, to_upload)

    def _on_settings(self) -> None:
        dlg = SettingsDialog(self.root, self._config, self._db)
        dlg.wait()
        # Reinitialise cloud provider in case the user switched providers
        self._init_cloud()
        # Reattach log file in case the path changed
        configure_log_file(self._db.get_setting("log_path"))
        # Reschedule refresh with potentially updated interval
        if self._refresh_id:
            self.root.after_cancel(self._refresh_id)
            self._refresh_id = None
        self._schedule_refresh()
        self._update_statusbar()

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
        fresh_ids = {j.job_id for j in fresh_jobs}

        # Remove jobs no longer present in the DB (pruned by scan_all)
        for job in list(self._jobs):
            if job.job_id not in fresh_ids:
                self._jobs.remove(job)
                try:
                    self._tree.delete(job.job_id)
                except tk.TclError:
                    pass

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
        if job.cloud_status == "Local":
            return "local"
        if job.cloud_status == "Provisioned":
            return "provisioned"
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
                             command=lambda iid=iid: self._run(self._download_task, iid))
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
        target = self._manager.jobs_folder
        if not target:
            messagebox.showwarning("Open Folder", "Jobs folder not configured.")
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
        job = self._job_by_id(job_id)
        if not job or job.cloud_status not in ("Uploaded", "Cloud Only"):
            messagebox.showinfo("Cloud Link", "This job has not been uploaded yet.")
            return
        if not self._cloud or not self._cloud.is_authenticated():
            messagebox.showwarning("Cloud Link",
                                   "Cloud provider is not authenticated.\n"
                                   "Open Settings to connect.")
            return
        self._run(self._open_cloud_link_task, job_id)

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
            jobs_folder = self._manager.jobs_folder
            if jobs_folder:
                for job in jobs:
                    if job.sticker_status == "Completed":
                        folder = os.path.join(jobs_folder, job.job_id)
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

    def _provision_task(self, job_ids: list[str]) -> None:
        if self._cloud is None:
            self._ui(messagebox.showwarning, "Provision", "Cloud provider is not available.")
            return
        success, errors = 0, []
        for job_id in job_ids:
            try:
                self._manager.provision_job(job_id, self._cloud)
                job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                job.cloud_status   = "Provisioned"
                job.cloud_progress = "33%"
                job.last_updated   = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                success += 1
                _j = job
                self._ui(lambda j=_j: self._update_job_row(j))
            except Exception as exc:
                logging.exception("Provision failed for %s", job_id)
                self._session_errors += 1
                errors.append(f"{job_id}: {exc}")
                err_job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                err_job.cloud_status = "Error"
                err_job.last_updated = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(err_job)
                _ej = err_job
                self._ui(lambda j=_ej: self._update_job_row(j))
        self._ui(self._update_statusbar)
        msg = f"Provisioned {success} job(s) successfully."
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors)
            self._ui(messagebox.showwarning, "Provision Complete", msg)
        else:
            self._ui(messagebox.showinfo, "Provision Complete", msg)

    def _prompt_upload_after_generate(self, job_ids: list[str]) -> None:
        """Called on the main thread after a successful generate run."""
        msg = f"Generated {len(job_ids)} job(s).\n\nUpload to cloud now?"
        if messagebox.askyesno("Generate Complete", msg):
            if self._cloud and self._cloud.is_authenticated():
                self._mark_active(job_ids)
                self._run(self._upload_task, job_ids)
            else:
                messagebox.showwarning("Upload",
                                       "Cloud provider is not authenticated.\n"
                                       "Open Settings to connect.")

    def _process_all_task(
        self,
        to_provision: list[str],
        to_generate: list[str],
        to_upload: list[str],
    ) -> None:
        """Background runner for Process All — runs Provision → Generate → Upload in order."""
        if to_provision:
            if self._cloud is None:
                self._ui(messagebox.showwarning, "Process All",
                         "Cloud provider unavailable — skipping provision.")
            else:
                for job_id in to_provision:
                    try:
                        self._manager.provision_job(job_id, self._cloud)
                        job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                        job.cloud_status   = "Provisioned"
                        job.cloud_progress = "33%"
                        job.last_updated   = datetime.now().isoformat(timespec="seconds")
                        self._db.upsert_job(job)
                        _j = job
                        self._ui(lambda j=_j: self._update_job_row(j))
                    except Exception:
                        logging.exception("Provision failed for %s", job_id)
                        self._session_errors += 1

        # After provisioning, the freshly-provisioned jobs become eligible for generation
        all_to_generate = list(dict.fromkeys(to_provision + to_generate))
        folder = self._manager.jobs_folder
        for job_id in all_to_generate:
            if not folder:
                break
            job_path = os.path.join(folder, job_id)
            try:
                self._engine.generate_pdf(job_path)
                qty = JobManager._read_qty_from_summary(
                    os.path.join(job_path, f"{job_id}_Stickers_Summary.txt"))
                job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                job.sticker_status = "Completed"
                job.sticker_qty    = qty
                if job.cloud_status not in ("Uploaded", "Cloud Only"):
                    job.cloud_status   = "Local"
                    job.cloud_progress = "66%"
                job.last_updated = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                _j = job
                self._ui(lambda j=_j: self._update_job_row(j))
            except Exception:
                logging.exception("Generate failed for %s", job_id)
                self._session_errors += 1

        # Upload all eligible jobs (original to_upload list + newly generated)
        all_to_upload = list(dict.fromkeys(to_upload + all_to_generate))
        if all_to_upload and self._cloud and self._cloud.is_authenticated():
            for job_id in all_to_upload:
                try:
                    self._manager.upload_job(job_id, self._cloud)
                    job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                    job.cloud_status   = "Uploaded"
                    job.cloud_progress = "100%"
                    job.last_updated   = datetime.now().isoformat(timespec="seconds")
                    self._db.upsert_job(job)
                    _j = job
                    self._ui(lambda j=_j: self._update_job_row(j))
                except Exception:
                    logging.exception("Upload failed for %s", job_id)
                    self._session_errors += 1

        self._ui(self._update_statusbar)

    def _generate_task(self, job_ids: list[str]) -> None:
        folder = self._manager.jobs_folder
        if not folder:
            self._ui(messagebox.showwarning, "Generate",
                     "Jobs folder is not configured.")
            return

        success_ids, errors = [], []
        for job_id in job_ids:
            job_path = os.path.join(folder, job_id)
            try:
                self._engine.generate_pdf(job_path)
                qty = JobManager._read_qty_from_summary(
                    os.path.join(job_path, f"{job_id}_Stickers_Summary.txt"))

                job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                job.sticker_status = "Completed"
                job.sticker_qty    = qty
                if job.cloud_status not in ("Uploaded", "Cloud Only"):
                    job.cloud_status   = "Local"
                    job.cloud_progress = "66%"
                job.last_updated = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                success_ids.append(job_id)
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
        if errors:
            msg = f"Generated {len(success_ids)} job(s).\n\nErrors ({len(errors)}):\n" + "\n".join(errors)
            self._ui(messagebox.showwarning, "Generate Complete", msg)
        elif success_ids:
            self._ui(self._prompt_upload_after_generate, success_ids)

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

    def _upload_task(self, job_ids: list[str]) -> None:
        if self._cloud is None:
            self._ui(messagebox.showwarning, "Upload", "Cloud provider is not available.")
            return
        success, errors = 0, []
        for job_id in job_ids:
            try:
                self._manager.upload_job(job_id, self._cloud)
                job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                job.cloud_status   = "Uploaded"
                job.cloud_progress = "100%"
                job.last_updated   = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(job)
                success += 1
                _j = job
                self._ui(lambda j=_j: self._update_job_row(j))
            except Exception as exc:
                logging.exception("Upload failed for %s", job_id)
                self._session_errors += 1
                errors.append(f"{job_id}: {exc}")
                err_job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
                err_job.cloud_status = "Error"
                err_job.last_updated = datetime.now().isoformat(timespec="seconds")
                self._db.upsert_job(err_job)
                _ej = err_job
                self._ui(lambda j=_ej: self._update_job_row(j))
        self._ui(self._update_statusbar)
        msg = f"Uploaded {success} job(s) successfully."
        if errors:
            msg += f"\n\nErrors ({len(errors)}):\n" + "\n".join(errors)
            self._ui(messagebox.showwarning, "Upload Complete", msg)
        else:
            self._ui(messagebox.showinfo, "Upload Complete", msg)

    def _download_task(self, job_id: str) -> None:
        if self._cloud is None:
            self._ui(messagebox.showwarning, "Download", "Cloud provider is not available.")
            return
        cloud_folder = f"{DROPBOX_UPLOAD_ROOT}/{job_id}"
        try:
            self._manager.download_job(job_id, self._cloud, cloud_folder)
            status, qty = self._manager.classify_sticker_status(job_id)
            job = self._job_by_id(job_id) or JobInfo(job_id=job_id)
            job.sticker_status = status
            job.sticker_qty    = qty
            job.cloud_status   = "Uploaded"
            job.cloud_progress = "100%"
            job.last_updated   = datetime.now().isoformat(timespec="seconds")
            self._db.upsert_job(job)
            _j = job
            self._ui(lambda j=_j: self._update_job_row(j))
            self._ui(messagebox.showinfo, "Download Complete",
                     f"Job {job_id} downloaded successfully.")
        except Exception as exc:
            logging.exception("Download failed for %s", job_id)
            self._session_errors += 1
            self._ui(messagebox.showerror, "Download Error",
                     f"Download failed for job {job_id}:\n{exc}")
            self._ui(self._update_statusbar)

    def _open_cloud_link_task(self, job_id: str) -> None:
        if self._cloud is None:
            return
        cloud_folder = f"{DROPBOX_UPLOAD_ROOT}/{job_id}"
        try:
            url = self._cloud.create_share_link(cloud_folder)
            self._ui(lambda u=url: webbrowser.open(u))
        except Exception as exc:
            logging.exception("Failed to get cloud link for %s", job_id)
            self._ui(messagebox.showerror, "Cloud Link Error",
                     f"Could not retrieve share link:\n{exc}")

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
        _center_window(win, 620, 420)
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
                       "(No log file found.\n"
                       "Set a Log Path in Settings to enable file logging.)")
        txt.config(state="disabled")

    # ── Misc helpers ──────────────────────────────────────────────────────────

    def _selected_job_ids(self) -> list[str]:
        return list(self._tree.selection())

    def _job_by_id(self, job_id: str) -> Optional[JobInfo]:
        return next((j for j in self._jobs if j.job_id == job_id), None)


# ════════════════════════════════════════════════════════════════════════════════
# SettingsDialog
# ════════════════════════════════════════════════════════════════════════════════

class SettingsDialog:
    """
    Modal settings dialog.  Three tabbed sections:
      Cloud   — provider radio, Dropbox credentials + auth, OneDrive credentials + auth
      Paths   — DB path, Jobs/Log folders, auto-refresh interval
      Company — company name and address

    On Save:
      Per-machine settings (cloud credentials, db_path, company) → config.json
      Shared settings (jobs_folder, log_path, auto_refresh_seconds)
        → DB settings table (propagated to all machines on next auto-refresh)
    """

    def __init__(self, parent: tk.Tk, config: AppConfig, db: JobDatabase) -> None:
        self._config = config
        self._db     = db

        self._top = tk.Toplevel(parent)
        self._top.title("Settings")
        _center_window(self._top, 520, 520)
        self._top.resizable(False, True)
        self._top.grab_set()
        self._top.focus_set()

        # ── StringVar for every editable field ───────────────────────────────
        self._provider_var   = tk.StringVar(value=config.cloud_provider)
        self._dbx_key_var    = tk.StringVar(value=config.dropbox_app_key)
        self._dbx_secret_var = tk.StringVar(value=config.dropbox_app_secret)
        self._od_client_var  = tk.StringVar(value=config.onedrive_client_id)
        self._od_tenant_var  = tk.StringVar(value=config.onedrive_tenant_id)
        self._db_path_var    = tk.StringVar(value=config.db_path)
        self._jobs_var       = tk.StringVar(value=db.get_setting("jobs_folder") or db.get_setting("target_folder"))
        self._log_var        = tk.StringVar(value=db.get_setting("log_path"))
        self._refresh_var    = tk.StringVar(value=db.get_setting("auto_refresh_seconds", "30"))
        self._company_var    = tk.StringVar(value=config.company_name)
        self._address_var    = tk.StringVar(value=config.company_address)

        # Status labels populated during _build_ui
        self._dbx_status_lbl: Optional[tk.Label] = None
        self._od_status_lbl:  Optional[tk.Label] = None
        self._db_status_lbl:  Optional[tk.Label] = None

        # LabelFrames kept as instance vars so _refresh_provider_sections can reach them
        self._dbx_lf: Optional[tk.LabelFrame] = None
        self._od_lf:  Optional[tk.LabelFrame] = None

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = tk.Frame(self._top, padx=10, pady=8)
        outer.pack(fill=tk.BOTH, expand=True)

        nb = ttk.Notebook(outer)
        nb.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        cloud_tab = tk.Frame(nb, padx=10, pady=8)
        nb.add(cloud_tab, text="  Cloud  ")
        self._build_cloud_tab(cloud_tab)

        paths_tab = tk.Frame(nb, padx=10, pady=8)
        nb.add(paths_tab, text="  Paths  ")
        self._build_paths_tab(paths_tab)

        co_tab = tk.Frame(nb, padx=10, pady=8)
        nb.add(co_tab, text="  Company  ")
        self._build_company_tab(co_tab)

        btn_row = tk.Frame(outer)
        btn_row.pack(anchor="e")
        tk.Button(btn_row, text="Cancel", width=10,
                  command=self._top.destroy).pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(btn_row, text="Save",   width=10,
                  command=self._save).pack(side=tk.LEFT)

        self._top.protocol("WM_DELETE_WINDOW", self._top.destroy)

    def _build_cloud_tab(self, parent: tk.Frame) -> None:
        prov_lf = tk.LabelFrame(parent, text="Provider", padx=8, pady=6)
        prov_lf.pack(fill=tk.X, pady=(0, 8))
        tk.Radiobutton(prov_lf, text="Dropbox",
                       variable=self._provider_var, value="dropbox",
                       command=self._refresh_provider_sections).pack(side=tk.LEFT, padx=(0, 24))
        tk.Radiobutton(prov_lf, text="OneDrive",
                       variable=self._provider_var, value="onedrive",
                       command=self._refresh_provider_sections).pack(side=tk.LEFT)

        # Dropbox credentials
        self._dbx_lf = tk.LabelFrame(parent, text="Dropbox", padx=8, pady=6)
        self._dbx_lf.pack(fill=tk.X, pady=(0, 8))
        self._add_entry(self._dbx_lf, "App Key:",    self._dbx_key_var,    row=0)
        self._add_entry(self._dbx_lf, "App Secret:", self._dbx_secret_var, row=1, show="*")
        dbx_btns = tk.Frame(self._dbx_lf)
        dbx_btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Button(dbx_btns, text="Authenticate with Dropbox",
                  command=self._auth_dropbox).pack(side=tk.LEFT, padx=(0, 10))
        self._dbx_status_lbl = tk.Label(dbx_btns, text="")
        self._dbx_status_lbl.pack(side=tk.LEFT)
        self._update_dbx_status()

        # OneDrive credentials
        self._od_lf = tk.LabelFrame(parent, text="OneDrive", padx=8, pady=6)
        self._od_lf.pack(fill=tk.X)
        self._add_entry(self._od_lf, "Client ID:", self._od_client_var, row=0)
        self._add_entry(self._od_lf, "Tenant ID:", self._od_tenant_var, row=1)
        od_btns = tk.Frame(self._od_lf)
        od_btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Button(od_btns, text="Authenticate with OneDrive",
                  command=self._auth_onedrive).pack(side=tk.LEFT, padx=(0, 10))
        self._od_status_lbl = tk.Label(od_btns, text="")
        self._od_status_lbl.pack(side=tk.LEFT)
        self._update_od_status()

        self._refresh_provider_sections()

    def _build_paths_tab(self, parent: tk.Frame) -> None:
        # DB path (per-machine bootstrap)
        db_lf = tk.LabelFrame(parent, text="Shared Database", padx=8, pady=6)
        db_lf.pack(fill=tk.X, pady=(0, 8))
        self._add_browse_entry(db_lf, "DB Path:", self._db_path_var, row=0,
                               browse_fn=self._browse_db)
        tk.Button(db_lf, text="Test Connection",
                  command=self._test_db_connection).grid(
                  row=1, column=1, sticky="w", padx=(6, 0), pady=(4, 0))
        db_status_row = tk.Frame(db_lf)
        db_status_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        tk.Label(db_status_row, text="Status:").pack(side=tk.LEFT, padx=(0, 4))
        self._db_status_lbl = tk.Label(db_status_row, text="")
        self._db_status_lbl.pack(side=tk.LEFT)
        self._test_db_connection()

        # Shared folder paths (go to DB settings table)
        folders_lf = tk.LabelFrame(
            parent,
            text="Shared Folders  \u2015  saved to database, visible on all machines",
            padx=8, pady=6,
        )
        folders_lf.pack(fill=tk.X, pady=(0, 8))
        self._add_browse_entry(folders_lf, "Jobs Folder:",   self._jobs_var,   row=0,
                               browse_fn=self._browse_dir)
        self._add_browse_entry(folders_lf, "Log Path:",      self._log_var,    row=1,
                               browse_fn=self._browse_log)

        # Auto-refresh spinbox
        ref_row = tk.Frame(parent)
        ref_row.pack(anchor="w", pady=(0, 4))
        tk.Label(ref_row, text="Auto-refresh every:").pack(side=tk.LEFT, padx=(0, 6))
        vcmd = (self._top.register(lambda s: s == "" or s.isdigit()), "%P")
        ttk.Spinbox(ref_row, textvariable=self._refresh_var,
                    from_=5, to=3600, width=6,
                    validate="key", validatecommand=vcmd).pack(side=tk.LEFT)
        tk.Label(ref_row, text="seconds").pack(side=tk.LEFT, padx=(6, 0))

    def _build_company_tab(self, parent: tk.Frame) -> None:
        co_lf = tk.LabelFrame(parent, text="Company Info", padx=8, pady=6)
        co_lf.pack(fill=tk.X)
        self._add_entry(co_lf, "Name:",    self._company_var, row=0)
        self._add_entry(co_lf, "Address:", self._address_var, row=1)

    # ── Grid helpers ──────────────────────────────────────────────────────────

    def _add_entry(self, parent: tk.Widget, label: str, var: tk.StringVar,
                   row: int, show: str = "") -> None:
        """Label + Entry in a grid-managed parent."""
        tk.Label(parent, text=label, anchor="e", width=12).grid(
            row=row, column=0, sticky="e", pady=3)
        tk.Entry(parent, textvariable=var, width=36, show=show).grid(
            row=row, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=3)
        parent.columnconfigure(1, weight=1)

    def _add_browse_entry(self, parent: tk.Widget, label: str, var: tk.StringVar,
                          row: int, browse_fn) -> None:
        """Label + Entry + Browse button in a grid-managed parent."""
        tk.Label(parent, text=label, anchor="e", width=14).grid(
            row=row, column=0, sticky="e", pady=3)
        tk.Entry(parent, textvariable=var, width=28).grid(
            row=row, column=1, sticky="ew", padx=(6, 0), pady=3)
        tk.Button(parent, text="Browse", width=7,
                  command=lambda v=var: browse_fn(v)).grid(
            row=row, column=2, padx=(4, 0), pady=3)
        parent.columnconfigure(1, weight=1)

    # ── Provider enable / disable ─────────────────────────────────────────────

    def _refresh_provider_sections(self) -> None:
        provider = self._provider_var.get()
        if self._dbx_lf:
            self._set_children_state(
                self._dbx_lf,
                tk.NORMAL if provider == "dropbox" else tk.DISABLED,
            )
        if self._od_lf:
            self._set_children_state(
                self._od_lf,
                tk.NORMAL if provider == "onedrive" else tk.DISABLED,
            )

    @staticmethod
    def _set_children_state(widget: tk.Widget, state) -> None:
        for child in widget.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass
            SettingsDialog._set_children_state(child, state)

    # ── Auth status labels ────────────────────────────────────────────────────

    def _update_dbx_status(self) -> None:
        ok = os.path.exists("token_dropbox.json")
        if self._dbx_status_lbl:
            self._dbx_status_lbl.config(
                text="\u2713 Authenticated" if ok else "\u2717 Not authenticated",
                fg="#2e7d32" if ok else "#b71c1c",
            )

    def _update_od_status(self) -> None:
        ok = os.path.exists("token_onedrive.json")
        if self._od_status_lbl:
            self._od_status_lbl.config(
                text="\u2713 Authenticated" if ok else "\u2717 Not authenticated",
                fg="#2e7d32" if ok else "#b71c1c",
            )

    # ── DB connection test ────────────────────────────────────────────────────

    def _test_db_connection(self) -> None:
        path = self._db_path_var.get().strip()
        if not path:
            self._set_db_status("No path configured", "#888888")
            return
        try:
            probe = JobDatabase(path)
            count = len(probe.get_all_jobs())
            self._set_db_status(f"\u25cf Connected  ({count} jobs)", "#2e7d32")
        except Exception as exc:
            self._set_db_status(f"\u25cf Error: {exc}", "#b71c1c")

    def _set_db_status(self, text: str, colour: str) -> None:
        if self._db_status_lbl:
            self._db_status_lbl.config(text=text, fg=colour)

    # ── Browse helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _browse_dir(var: tk.StringVar) -> None:
        path = filedialog.askdirectory(title="Select folder")
        if path:
            var.set(path)

    @staticmethod
    def _browse_db(var: tk.StringVar) -> None:
        path = filedialog.asksaveasfilename(
            title="Database file path",
            defaultextension=".db",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    @staticmethod
    def _browse_log(var: tk.StringVar) -> None:
        path = filedialog.asksaveasfilename(
            title="Log file path",
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    # ── Dropbox auth flow ─────────────────────────────────────────────────────

    def _auth_dropbox(self) -> None:
        key    = self._dbx_key_var.get().strip()
        secret = self._dbx_secret_var.get().strip()
        if not key or not secret:
            messagebox.showwarning("Dropbox", "Enter App Key and App Secret first.",
                                   parent=self._top)
            return

        try:
            import dropbox as _dbx
            flow = _dbx.DropboxOAuth2FlowNoRedirect(
                key, secret, token_access_type="offline"
            )
            auth_url = flow.start()
        except Exception as exc:
            messagebox.showerror("Dropbox", f"Could not start auth flow:\n{exc}",
                                 parent=self._top)
            return

        import webbrowser
        webbrowser.open(auth_url)

        code = self._ask_string(
            title="Dropbox \u2014 Paste Code",
            prompt="A browser window has opened.\n\n"
                   "Authorise the app, then paste the code Dropbox gives you:",
        )
        if not code:
            return

        try:
            result = flow.finish(code.strip())
            with open("token_dropbox.json", "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "access_token":  result.access_token,
                        "refresh_token": getattr(result, "refresh_token", None),
                    },
                    fh,
                    indent=2,
                )
            self._update_dbx_status()
            messagebox.showinfo("Dropbox", "\u2713 Authenticated successfully.",
                                parent=self._top)
        except Exception as exc:
            messagebox.showerror("Dropbox", f"Authentication failed:\n{exc}",
                                 parent=self._top)

    # ── OneDrive auth flow ────────────────────────────────────────────────────

    def _auth_onedrive(self) -> None:
        client_id = self._od_client_var.get().strip()
        tenant_id = self._od_tenant_var.get().strip() or "common"
        if not client_id:
            messagebox.showwarning("OneDrive", "Enter a Client ID first.",
                                   parent=self._top)
            return

        try:
            import msal as _msal
            msal_app = _msal.PublicClientApplication(
                client_id,
                authority=f"https://login.microsoftonline.com/{tenant_id}",
            )
            flow_data = msal_app.initiate_device_flow(
                scopes=["Files.ReadWrite", "offline_access"]
            )
        except Exception as exc:
            messagebox.showerror("OneDrive", f"Could not start device flow:\n{exc}",
                                 parent=self._top)
            return

        if "user_code" not in flow_data:
            messagebox.showerror(
                "OneDrive",
                f"Device flow error: {flow_data.get('error_description', str(flow_data))}",
                parent=self._top,
            )
            return

        # Instructions window with the device code displayed prominently
        code_win = tk.Toplevel(self._top)
        code_win.title("OneDrive \u2014 Sign In")
        _center_window(code_win, 430, 210)
        code_win.grab_set()

        tk.Label(
            code_win,
            text=(
                "1. Open a browser and go to:\n"
                "   https://microsoft.com/devicelogin\n\n"
                "2. Enter the code shown below, then sign in:"
            ),
            justify=tk.LEFT, padx=14, pady=10,
        ).pack(anchor="w")

        code_row = tk.Frame(code_win)
        code_row.pack(anchor="w", padx=14)
        tk.Label(code_row, text=flow_data["user_code"],
                 font=("Courier New", 18, "bold"), fg="#1565c0").pack(side=tk.LEFT)
        tk.Button(
            code_row, text="Copy",
            command=lambda: (
                code_win.clipboard_clear(),
                code_win.clipboard_append(flow_data["user_code"]),
            ),
        ).pack(side=tk.LEFT, padx=(12, 0))

        status_lbl = tk.Label(code_win, text="Waiting for sign-in\u2026", fg="#888888")
        status_lbl.pack(pady=(10, 0))

        result_box: list = []

        def _poll_thread() -> None:
            try:
                res = msal_app.acquire_token_by_device_flow(flow_data)
                result_box.append(res)
            except Exception as exc:
                result_box.append({"error": str(exc)})

        threading.Thread(target=_poll_thread, daemon=True).start()

        def _check() -> None:
            if not result_box:
                code_win.after(1000, _check)
                return
            res = result_box[0]
            if "access_token" in res:
                cache_data = msal_app.token_cache.serialize()
                with open("token_onedrive.json", "w", encoding="utf-8") as fh:
                    fh.write(cache_data)
                status_lbl.config(text="\u2713 Signed in!", fg="#2e7d32")
                code_win.after(1400, code_win.destroy)
                self._top.after(100, self._update_od_status)
                self._top.after(1600, lambda: messagebox.showinfo(
                    "OneDrive", "\u2713 Authenticated successfully.", parent=self._top))
            else:
                err = res.get("error_description") or res.get("error", "Unknown error")
                status_lbl.config(text=f"Failed: {err}", fg="#b71c1c")
                code_win.after(3000, code_win.destroy)

        code_win.after(1000, _check)

    # ── Simple string-entry prompt ────────────────────────────────────────────

    def _ask_string(self, title: str, prompt: str) -> Optional[str]:
        """Show a small modal dialog with one text entry; return value or None."""
        dlg = tk.Toplevel(self._top)
        dlg.title(title)
        _center_window(dlg, 420, 160)
        dlg.grab_set()
        tk.Label(dlg, text=prompt, justify=tk.LEFT,
                 wraplength=390, padx=12, pady=10).pack(anchor="w")
        val = tk.StringVar()
        tk.Entry(dlg, textvariable=val, width=48).pack(padx=12)
        result: list = []

        def _ok() -> None:
            result.append(val.get())
            dlg.destroy()

        btn_row = tk.Frame(dlg)
        btn_row.pack(pady=8)
        tk.Button(btn_row, text="Cancel", width=8, command=dlg.destroy).pack(side=tk.LEFT, padx=4)
        tk.Button(btn_row, text="OK",     width=8, command=_ok).pack(side=tk.LEFT, padx=4)
        dlg.bind("<Return>", lambda _e: _ok())
        dlg.wait_window()
        return result[0] if result else None

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            refresh_secs = int(self._refresh_var.get() or "30")
            if refresh_secs < 5:
                raise ValueError
        except ValueError:
            messagebox.showerror("Settings",
                                 "Auto-refresh must be a whole number \u2265 5.",
                                 parent=self._top)
            return

        # Per-machine settings → config.json
        self._config._data.update({
            "cloud_provider":     self._provider_var.get(),
            "dropbox_app_key":    self._dbx_key_var.get().strip(),
            "dropbox_app_secret": self._dbx_secret_var.get().strip(),
            "onedrive_client_id": self._od_client_var.get().strip(),
            "onedrive_tenant_id": self._od_tenant_var.get().strip() or "common",
            "db_path":            self._db_path_var.get().strip(),
            "company_name":       self._company_var.get().strip(),
            "company_address":    self._address_var.get().strip(),
        })
        self._config.save()

        # Reconnect DB if path changed
        new_path = self._db_path_var.get().strip()
        if new_path != self._db._path:
            self._db.reconnect(new_path)

        # Shared settings → DB settings table
        self._db.set_setting("jobs_folder",          os.path.normpath(self._jobs_var.get().strip()) if self._jobs_var.get().strip() else "")
        self._db.set_setting("log_path",             self._log_var.get().strip())
        self._db.set_setting("auto_refresh_seconds", str(refresh_secs))

        log.info("Settings saved")
        self._top.destroy()

    # ── Wait ──────────────────────────────────────────────────────────────────

    def wait(self) -> None:
        """Block until the dialog is closed (call from the main thread)."""
        self._top.wait_window()


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
