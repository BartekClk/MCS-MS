import io
import os
import stat
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Iterable, Tuple, Any

from dotenv import load_dotenv
from stream_zip import NO_COMPRESSION_32, NO_COMPRESSION_64, stream_zip
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaUpload

from logger import MCSLogger, log

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class BackupError(Exception):
    """Base exception for all fatal errors occurring during the backup process."""


class BackupConfigError(BackupError):
    """Raised when configuration values or required paths are missing or invalid."""


class BackupAuthError(BackupError):
    """Raised when OAuth2 credentials are missing, invalid, or cannot be refreshed."""


class BackupUploadError(BackupError):
    """Raised when archive streaming or Google Drive upload fails."""


class _StreamingZipUpload(MediaUpload):
    """Resumable Google Drive MediaUpload adapter for on-the-fly byte generators of unknown total size."""

    def __init__(self, generator: Iterator[bytes], chunksize: int, mimetype: str = "application/zip"):
        self._gen = generator
        self._chunksize = chunksize
        self._mimetype = mimetype
        self._buffer = bytearray()
        self._buffer_start = 0
        self._stream_pos = 0
        self._eof = False

    def chunksize(self) -> int:
        return self._chunksize

    def mimetype(self) -> str:
        return self._mimetype

    def size(self) -> int | None:
        return None

    def resumable(self) -> bool:
        return True

    def getbytes(self, begin: int, length: int) -> bytes:
        """Returns a byte slice starting at offset `begin`, advancing the ZIP generator as needed."""
        if begin < self._buffer_start:
            raise BackupUploadError(
                f"Cannot rewind non-seekable ZIP stream (requested offset {begin}, current buffer start {self._buffer_start})."
            )

        if begin > self._buffer_start:
            if begin <= self._stream_pos:
                drop = begin - self._buffer_start
                del self._buffer[:drop]
                self._buffer_start = begin
            else:
                self._buffer.clear()
                to_skip = begin - self._stream_pos
                while to_skip > 0 and not self._eof:
                    try:
                        chunk = next(self._gen)
                        self._stream_pos += len(chunk)
                        if len(chunk) <= to_skip:
                            to_skip -= len(chunk)
                        else:
                            self._buffer.extend(chunk[to_skip:])
                            to_skip = 0
                    except StopIteration:
                        self._eof = True
                        break
                self._buffer_start = begin

        while len(self._buffer) < length and not self._eof:
            try:
                chunk = next(self._gen)
                self._buffer.extend(chunk)
                self._stream_pos += len(chunk)
            except StopIteration:
                self._eof = True
                break

        return bytes(self._buffer[:length])


def _get_oauth_credentials(token_path: Path, logger: MCSLogger) -> Credentials:
    """
    Loads existing OAuth2 user credentials from token_path and refreshes them if expired.
    Raises BackupAuthError if the token file is missing or cannot be refreshed in a headless environment.
    """
    if not token_path.exists():
        raise BackupAuthError(
            f"OAuth2 token file not found at: {token_path}. Generate it locally and upload to the server."
        )

    logger.write("INFO", f"Loading OAuth2 token from {token_path}")
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as e:
        raise BackupAuthError(f"Failed to parse OAuth2 token file {token_path}: {e}") from e

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.write("INFO", "OAuth2 token expired. Refreshing access token...")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise BackupAuthError(f"Failed to refresh OAuth2 token: {e}") from e

            token_path.write_text(creds.to_json(), encoding="utf-8")
            logger.write("SUCCESS", f"OAuth2 token refreshed and saved to {token_path}")
        else:
            raise BackupAuthError(
                "OAuth2 token is invalid or lacks a refresh_token. Manual re-authorization required."
            )

    return creds


def _iter_directory_files(
    source_dir: Path,
    logger: MCSLogger,
    stats: dict[str, int],
    use_zip64: bool = True,
    exclude_dirs: set[str] | None = None
) -> Iterable[Tuple[str, datetime, int, object, Iterable[bytes]]]:
    """
    Yields file metadata and byte-chunk iterators for on-the-fly ZIP generation.
    Logs individual files to the backup log file only and updates live stats.
    """
    exclude_dirs = exclude_dirs or set()
    method = NO_COMPRESSION_64 if use_zip64 else NO_COMPRESSION_32
    mode = stat.S_IFREG | 0o644

    def _file_chunks(file_path: Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            with open(file_path, "rb") as f:
                while chunk := f.read(chunk_size):
                    yield chunk
        except OSError as e:
            raise BackupUploadError(f"Fatal I/O error while reading file {file_path}: {e}") from e

    for root, dirs, files in os.walk(source_dir):
        ignored = [d for d in dirs if d in exclude_dirs]
        for d in ignored:
            logger.write("DEBUG", f"Skipping excluded directory: {Path(root) / d}")
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for filename in files:
            if filename == "session.lock":
                logger.write("DEBUG", f"Skipping Minecraft lock file: {Path(root) / filename}")
                continue

            full_path = Path(root) / filename
            try:
                st = full_path.stat()
                mtime = datetime.fromtimestamp(st.st_mtime)
                rel_path = full_path.relative_to(source_dir).as_posix()

                stats["files"] += 1
                logger.write("PACKING", f"Streaming file into archive: {rel_path}")
                yield (
                    rel_path,
                    mtime,
                    mode,
                    method,
                    _file_chunks(full_path)
                )
            except FileNotFoundError:
                logger.write("WARN", f"Transient file disappeared before packing: {full_path}")
            except PermissionError as e:
                raise BackupUploadError(f"Permission denied accessing file {full_path}: {e}") from e


def _cleanup_old_backups(
    service: Any,
    gdrive_folder_id: str,
    archive_prefix: str,
    retention_days: int,
    logger: MCSLogger
) -> int:
    """
    Permanently deletes backup archives matching archive_prefix in the target Google Drive folder
    that are older than retention_days.

    :param service: Authenticated Google Drive API v3 resource.
    :param gdrive_folder_id: Target Google Drive folder ID.
    :param archive_prefix: Filename prefix used to identify backup archives.
    :param retention_days: Maximum age in days before a backup is deleted (0 disables cleanup).
    :param logger: Active MCSLogger instance for recording cleanup actions.
    :return: Number of successfully deleted old backup files.
    """
    if retention_days <= 0:
        logger.write("DEBUG", "Automatic backup retention cleanup is disabled (BACKUP_RETENTION_DAYS <= 0).")
        return 0

    cutoff_utc = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_rfc3339 = cutoff_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_prefix = archive_prefix.replace("'", "\\'")

    query = (
        f"'{gdrive_folder_id}' in parents and "
        f"trashed = false and "
        f"mimeType = 'application/zip' and "
        f"name contains '{safe_prefix}_' and "
        f"createdTime < '{cutoff_rfc3339}'"
    )

    logger.write("INFO", f"Checking for backups older than {retention_days} days (before {cutoff_rfc3339})...")

    deleted_count = 0
    freed_bytes = 0
    page_token: str | None = None

    try:
        while True:
            response = service.files().list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, createdTime, size)",
                pageToken=page_token
            ).execute()

            for item in response.get("files", []):
                file_id = item["id"]
                file_name = item["name"]
                created_time = item.get("createdTime", "unknown")
                file_size = int(item.get("size", 0))

                try:
                    service.files().delete(fileId=file_id).execute()
                    deleted_count += 1
                    freed_bytes += file_size
                    logger.write(
                        "INFO",
                        f"Deleted expired backup: {file_name} (Created: {created_time}, Freed: {file_size / (1024 * 1024):.2f} MB)"
                    )
                except HttpError as e:
                    logger.write("WARN", f"Failed to delete expired backup {file_name} (ID: {file_id}): {e}")

            page_token = response.get("nextPageToken")
            if not page_token:
                break

    except HttpError as e:
        logger.write("WARN", f"Failed to query Google Drive for expired backups: {e}")
        return deleted_count

    if deleted_count > 0:
        freed_mb = freed_bytes / (1024 * 1024)
        summary = f"Retention cleanup finished: removed {deleted_count} old backup(s), freed {freed_mb:.2f} MB."
        logger.write("SUCCESS", summary)
        log("INFO", summary, module="BACKUP")
    else:
        logger.write("INFO", "No expired backups found for deletion.")

    return deleted_count


def run_backup(env_path: str | Path = ".env", full_backup: bool = False) -> dict[str, Any]:
    """
    Streams an uncompressed ZIP64 backup of either the world folder or the entire server directory
    directly to Google Drive via OAuth2, cleans up expired backups based on BACKUP_RETENTION_DAYS,
    logs detailed progress, and renders a live console status.

    :param env_path: Path to the .env configuration file.
    :param full_backup: If False, backs up only the world folder inside SERVER_DIR.
                        If True, backs up the entire SERVER_DIR with a '_full_' archive tag.
    :return: Status dictionary on success ('status', 'backup_type', 'message', 'file_id', 'archive_name',
             'files_count', 'size_bytes', 'deleted_old_backups', 'log_file').
    :raises BackupConfigError: If configuration or target paths are missing/invalid.
    :raises BackupAuthError: If OAuth2 authentication fails.
    :raises BackupUploadError: If archiving or Google Drive upload fails.
    """
    env_file = Path(env_path)
    if env_file.exists():
        load_dotenv(dotenv_path=env_file)

    logs_dir = Path(os.getenv("LOGS_DIR", "./mcsms_data/logs")).expanduser().resolve()
    backup_logger = MCSLogger(prefix="backup", logs_dir=logs_dir, per_run=True, default_module="BACKUP")

    backup_mode_label = "FULL SERVER" if full_backup else "WORLD"
    backup_logger.write("INFO", f"Starting MCS-MS {backup_mode_label} backup procedure...")

    try:
        if not env_file.exists():
            raise BackupConfigError(f"Configuration file not found: {env_file.resolve()}")

        server_dir_raw = os.getenv("SERVER_DIR", "").strip()
        world_folder_name = os.getenv("WORLD_FOLDER_NAME", "").strip()
        archive_prefix = os.getenv("BACKUP_ARCHIVE_PREFIX", "").strip()
        gdrive_folder_id = os.getenv("GDRIVE_FOLDER_ID", "").strip()
        token_path_raw = os.getenv("GDRIVE_TOKEN_PATH", "").strip()

        if not all([server_dir_raw, world_folder_name, archive_prefix, gdrive_folder_id, token_path_raw]):
            raise BackupConfigError("One or more required backup variables are missing or empty in .env.")

        server_dir = Path(server_dir_raw).expanduser().resolve()
        token_path = Path(token_path_raw).expanduser().resolve()

        try:
            chunk_size_mb = int(os.getenv("BACKUP_CHUNK_SIZE_MB", "10"))
            if chunk_size_mb <= 0:
                raise ValueError("Chunk size must be greater than 0.")
        except ValueError as e:
            raise BackupConfigError(f"Invalid BACKUP_CHUNK_SIZE_MB value: {e}") from e

        try:
            retention_days = int(os.getenv("BACKUP_RETENTION_DAYS", "0"))
            if retention_days < 0:
                raise ValueError("Retention days cannot be negative.")
        except ValueError as e:
            raise BackupConfigError(f"Invalid BACKUP_RETENTION_DAYS value: {e}") from e

        raw_excludes = os.getenv("BACKUP_EXCLUDE_DIRS", "")
        exclude_dirs = {item.strip() for item in raw_excludes.split(",") if item.strip()}

        if not server_dir.is_dir():
            raise BackupConfigError(f"Server root directory does not exist: {server_dir}")

        if full_backup:
            target_dir = server_dir
            archive_tag = "full"
            if logs_dir.is_relative_to(target_dir):
                exclude_dirs.add(logs_dir.relative_to(target_dir).parts[0])
        else:
            target_dir = (server_dir / world_folder_name).resolve()
            archive_tag = "world"

        if not target_dir.is_dir():
            raise BackupConfigError(f"Target backup directory does not exist: {target_dir}")

        timestamp_eu = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        archive_name = f"{archive_prefix}_{archive_tag}_{timestamp_eu}.zip"

        backup_logger.write("INFO", f"Target ({backup_mode_label}): {target_dir} -> Archive: {archive_name}")
        backup_logger.write("DEBUG", f"Excluded directories: {exclude_dirs if exclude_dirs else 'None'}")

        creds = _get_oauth_credentials(token_path, backup_logger)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        stats = {"files": 0}
        files_iterable = _iter_directory_files(
            target_dir,
            logger=backup_logger,
            stats=stats,
            use_zip64=True,
            exclude_dirs=exclude_dirs
        )
        zip_generator = stream_zip(files_iterable, chunk_size=1024 * 1024)

        file_metadata = {
            "name": archive_name,
            "parents": [gdrive_folder_id]
        }

        upload_chunk_size = chunk_size_mb * 1024 * 1024
        media = _StreamingZipUpload(
            generator=zip_generator,
            chunksize=upload_chunk_size,
            mimetype="application/zip"
        )

        backup_logger.write("INFO", f"Streaming archive directly to Google Drive (folder: {gdrive_folder_id})...")
        start_time = time.monotonic()

        try:
            request = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, size"
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    mb_uploaded = status.resumable_progress / (1024 * 1024)
                    elapsed = max(time.monotonic() - start_time, 0.001)
                    speed_mbps = mb_uploaded / elapsed

                    backup_logger.write(
                        "UPLOAD",
                        f"Files: {stats['files']} | Uploaded: {mb_uploaded:.2f} MB | Speed: {speed_mbps:.2f} MB/s"
                    )
                    backup_logger.progress(
                        f"[{backup_mode_label}] {stats['files']} files | {mb_uploaded:.2f} MB sent | {speed_mbps:.2f} MB/s"
                    )

        except HttpError as e:
            raise BackupUploadError(f"Google Drive API error during upload (HTTP {e.resp.status}): {e}") from e
        except BackupError:
            raise
        except Exception as e:
            raise BackupUploadError(f"Unexpected stream/network failure during upload: {e}") from e

        elapsed_total = max(time.monotonic() - start_time, 0.001)
        file_id = response.get("id")
        file_size_mb = int(response.get("size", 0)) / (1024 * 1024)
        avg_speed = file_size_mb / elapsed_total

        summary = (
            f"{backup_mode_label} backup completed! File: {archive_name} | Files: {stats['files']} | "
            f"Size: {file_size_mb:.2f} MB | Avg Speed: {avg_speed:.2f} MB/s | Drive ID: {file_id}"
        )
        backup_logger.write("SUCCESS", summary)
        log("SUCCESS", summary, module="BACKUP")

        # Clean up expired backups only after the new archive has been uploaded safely
        deleted_old_count = _cleanup_old_backups(
            service=service,
            gdrive_folder_id=gdrive_folder_id,
            archive_prefix=archive_prefix,
            retention_days=retention_days,
            logger=backup_logger
        )

        return {
            "status": "SUCCESS",
            "backup_type": archive_tag.upper(),
            "message": "Backup archived and uploaded successfully.",
            "file_id": file_id,
            "archive_name": archive_name,
            "files_count": stats["files"],
            "size_bytes": int(response.get("size", 0)),
            "deleted_old_backups": deleted_old_count,
            "log_file": str(backup_logger.log_file)
        }

    except BackupError as e:
        err_msg = f"[{type(e).__name__}] {e}"
        backup_logger.write("ERROR", err_msg)
        log("ERROR", f"{backup_mode_label} backup failed: {err_msg}", module="BACKUP")
        raise
    except Exception as e:
        err_msg = f"Unhandled exception [{type(e).__name__}]: {e}"
        backup_logger.write("FATAL", f"{err_msg}\n{traceback.format_exc()}")
        log("FATAL", err_msg, module="BACKUP")
        raise