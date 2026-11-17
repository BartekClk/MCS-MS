import argparse
import hashlib
import hmac
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from logger import MCSLogger, log

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MiB


@dataclass(frozen=True)
class DaemonConfig:
    """Validated runtime settings loaded strictly from .env for the MCS-MS daemon."""

    host: str
    port: int
    webhook_secret: str
    backup_hour: int
    backup_minute: int
    logs_dir: Path
    env_path: Path
    project_dir: Path


def load_daemon_config(env_path: Path) -> DaemonConfig:
    """Loads and validates daemon configuration from the specified .env file."""
    if not env_path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {env_path.resolve()}")

    load_dotenv(dotenv_path=env_path)

    def req_str(key: str) -> str:
        val = os.getenv(key, "").strip()
        if not val:
            raise ValueError(f"Missing or empty required variable in .env: {key}")
        return val

    host = req_str("DAEMON_HOST")
    port_raw = req_str("DAEMON_PORT")
    try:
        port = int(port_raw)
        if not (1 <= port <= 65535):
            raise ValueError("Out of TCP port range 1-65535")
    except ValueError as e:
        raise ValueError(f"Invalid DAEMON_PORT in .env: {port_raw}") from e

    webhook_secret = req_str("DAEMON_WEBHOOK_SECRET")
    if len(webhook_secret) < 16:
        raise ValueError("DAEMON_WEBHOOK_SECRET in .env must be at least 16 characters long.")

    backup_time_raw = req_str("DAILY_BACKUP_TIME")
    time_match = _TIME_PATTERN.match(backup_time_raw)
    if not time_match:
        raise ValueError(f"Invalid DAILY_BACKUP_TIME in .env (expected HH:MM 24h format): {backup_time_raw}")

    logs_dir = Path(req_str("LOGS_DIR")).expanduser().resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)

    return DaemonConfig(
        host=host,
        port=port,
        webhook_secret=webhook_secret,
        backup_hour=int(time_match.group(1)),
        backup_minute=int(time_match.group(2)),
        logs_dir=logs_dir,
        env_path=env_path.resolve(),
        project_dir=Path(__file__).resolve().parent,
    )


class TaskCoordinator:
    """
    Thread-safe FIFO task queue and single-process worker ensuring scheduled backups
    and webhook-triggered updates never run concurrently.
    """

    def __init__(self, config: DaemonConfig, logger: MCSLogger) -> None:
        self.cfg = config
        self._logger = logger
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._lock = threading.Lock()
        self._pending_flags: set[str] = set()
        self._current_task: str | None = None
        self._last_result: dict[str, Any] | None = None
        self._stop_event = threading.Event()

    def enqueue(self, flag: str, source: str) -> tuple[bool, str]:
        """Adds an MCSMS CLI flag to the execution queue if not already queued or active."""
        with self._lock:
            if self._current_task == flag or flag in self._pending_flags:
                msg = f"Task '{flag}' is already queued or currently executing."
                self._logger.write("INFO", f"Skipped duplicate enqueue from {source}: {msg}")
                return False, msg

            self._pending_flags.add(flag)
            self._queue.put((flag, source))

        msg = f"Task '{flag}' queued (source={source})."
        self._logger.write("INFO", msg)
        log("INFO", msg, module="DAEMON")
        return True, msg

    def status_snapshot(self) -> dict[str, Any]:
        """Returns current worker state for the /health endpoint."""
        with self._lock:
            return {
                "status": "ok",
                "current_task": self._current_task,
                "queued_tasks": sorted(self._pending_flags),
                "daily_backup_time": f"{self.cfg.backup_hour:02d}:{self.cfg.backup_minute:02d}",
                "last_result": self._last_result,
            }

    def stop(self) -> None:
        """Signals background threads to finish and exit cleanly."""
        self._stop_event.set()

    def worker_loop(self) -> None:
        """Executes queued MCSMS.py commands sequentially in a dedicated worker thread."""
        mcsms_script = self.cfg.project_dir / "MCSMS.py"

        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                flag, source = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            with self._lock:
                self._pending_flags.discard(flag)
                self._current_task = flag

            cmd = [sys.executable, str(mcsms_script), "--env", str(self.cfg.env_path), flag]
            self._logger.write("INFO", f"Executing task '{flag}' (source={source}): {' '.join(cmd)}")
            log("INFO", f"Starting queued task '{flag}' (source={source})...", module="DAEMON")

            started = datetime.now().astimezone()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.cfg.project_dir),
                    check=False,
                )
                exit_code = proc.returncode
            except Exception as e:
                exit_code = 1
                self._logger.write("ERROR", f"Subprocess error while running '{flag}': {e}")
                log("ERROR", f"Subprocess error while running '{flag}': {e}", module="DAEMON")

            finished = datetime.now().astimezone()
            duration = (finished - started).total_seconds()

            level = "SUCCESS" if exit_code == 0 else "ERROR"
            self._logger.write(
                level,
                f"Task '{flag}' finished with exit_code={exit_code} in {duration:.1f}s.",
            )
            log(level, f"Task '{flag}' finished (exit_code={exit_code}, {duration:.1f}s).", module="DAEMON")

            with self._lock:
                self._current_task = None
                self._last_result = {
                    "task": flag,
                    "source": source,
                    "exit_code": exit_code,
                    "duration_sec": round(duration, 1),
                    "finished_at": finished.isoformat(),
                }

            self._queue.task_done()

    def scheduler_loop(self) -> None:
        """Triggers the daily --backup task once per day at DAILY_BACKUP_TIME."""
        last_triggered_date: str | None = None

        while not self._stop_event.is_set():
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")

            if (
                now.hour == self.cfg.backup_hour
                and now.minute == self.cfg.backup_minute
                and last_triggered_date != today_str
            ):
                last_triggered_date = today_str
                self.enqueue("--backup", source="daily-scheduler")

            self._stop_event.wait(10.0)


class MCSMSHttpServer(ThreadingHTTPServer):
    """HTTP server carrying shared DaemonConfig, TaskCoordinator, and MCSLogger."""

    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        config: DaemonConfig,
        coordinator: TaskCoordinator,
        logger: MCSLogger,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.cfg = config
        self.coordinator = coordinator
        self.logger = logger


class WebhookRequestHandler(BaseHTTPRequestHandler):
    """Handles /health status checks and authenticated GitHub /webhook triggers."""

    server: MCSMSHttpServer

    def log_message(self, format: str, *args: Any) -> None:
        self.server.logger.write("HTTP", f"{self.address_string()} - {format % args}")

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self, raw_body: bytes) -> bool:
        secret = self.server.cfg.webhook_secret

        # 1. Native GitHub Webhook signature: X-Hub-Signature-256: sha256=<hex>
        gh_sig = self.headers.get("X-Hub-Signature-256", "").strip()
        if gh_sig.startswith("sha256="):
            digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            expected = f"sha256={digest}"
            return hmac.compare_digest(expected, gh_sig)

        # 2. GitHub Actions / curl Bearer token: Authorization: Bearer <secret>
        auth_header = self.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            return hmac.compare_digest(secret, token)

        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, self.server.coordinator.status_snapshot())
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/webhook":
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "Invalid Content-Length header"})
            return

        if content_length < 0 or content_length > _MAX_PAYLOAD_BYTES:
            self._send_json(413, {"error": "Payload too large"})
            return

        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        if not self._is_authorized(raw_body):
            self.server.logger.write("WARN", f"Rejected unauthorized webhook request from {self.address_string()}")
            log("WARN", f"Rejected unauthorized webhook request from {self.address_string()}", module="DAEMON")
            self._send_json(401, {"error": "Unauthorized"})
            return

        if raw_body:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                if isinstance(payload, dict) and "action" in payload:
                    action = str(payload.get("action", ""))
                    if action != "published":
                        self._send_json(
                            200,
                            {"status": "ignored", "reason": f"Ignored GitHub release action '{action}'"},
                        )
                        return
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "Malformed JSON body"})
                return

        queued, message = self.server.coordinator.enqueue("--run", source="github-webhook")
        self._send_json(
            202,
            {
                "status": "accepted" if queued else "already_queued",
                "message": message,
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCS-MS HTTP Webhook & Daily Backup Daemon")
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to the .env configuration file (default: ./.env)",
    )
    return parser.parse_args()


def run_daemon(env_path: Path = Path(".env")) -> int:
    """Starts the MCS-MS HTTP webhook server, task worker, and daily backup scheduler."""
    try:
        config = load_daemon_config(env_path)
    except Exception as e:
        log("FATAL", f"Daemon startup aborted due to configuration error: {e}", module="DAEMON")
        return 1

    logger = MCSLogger(
        prefix="daemon",
        logs_dir=config.logs_dir,
        per_run=False,
        default_module="DAEMON",
    )
    coordinator = TaskCoordinator(config=config, logger=logger)
    httpd = MCSMSHttpServer(
        (config.host, config.port),
        WebhookRequestHandler,
        config=config,
        coordinator=coordinator,
        logger=logger,
    )

    worker_thread = threading.Thread(target=coordinator.worker_loop, name="mcsms-worker", daemon=False)
    scheduler_thread = threading.Thread(target=coordinator.scheduler_loop, name="mcsms-scheduler", daemon=True)

    shutdown_initiated = threading.Event()

    def handle_signal(signum: int, _frame: Any) -> None:
        if shutdown_initiated.is_set():
            return
        shutdown_initiated.set()
        log("INFO", f"Received signal {signum}; shutting down MCS-MS daemon...", module="DAEMON")
        logger.write("INFO", f"Received signal {signum}; shutting down HTTP server and worker...")
        coordinator.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    worker_thread.start()
    scheduler_thread.start()

    startup_msg = (
        f"MCS-MS daemon listening on http://{config.host}:{config.port} "
        f"(daily backup at {config.backup_hour:02d}:{config.backup_minute:02d})"
    )
    logger.write("SUCCESS", startup_msg)
    log("SUCCESS", startup_msg, module="DAEMON")

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        coordinator.stop()
        worker_thread.join()
        logger.write("INFO", "MCS-MS daemon stopped cleanly.")
        log("INFO", "MCS-MS daemon stopped cleanly.", module="DAEMON")

    return 0


def main() -> int:
    args = parse_args()
    return run_daemon(env_path=args.env)


if __name__ == "__main__":
    sys.exit(main())