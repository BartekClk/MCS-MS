import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Statuses displayed on screen by default (verbose ones like PACKING or DEBUG stay in log files only)
DEFAULT_CONSOLE_STATUSES = {"INIT", "INFO", "WARN", "ERROR", "FATAL", "SUCCESS"}

_ANSI_COLORS = {
    "INIT": "\033[96m",     # Cyan
    "INFO": "\033[96m",     # Cyan
    "SUCCESS": "\033[92m",  # Green
    "WARN": "\033[93m",     # Yellow
    "ERROR": "\033[91m",    # Red
    "FATAL": "\033[91;1m",  # Bold Red
    "PROGRESS": "\033[94m", # Blue
}
_ANSI_RESET = "\033[0m"

# Global console lock and state shared across all MCSLogger instances to prevent line collisions
_console_lock = threading.Lock()
_has_active_progress = False


def _load_console_statuses() -> set[str]:
    """Loads allowed console log statuses from .env or falls back to DEFAULT_CONSOLE_STATUSES."""
    raw = os.getenv("CONSOLE_LOG_STATUSES", "").strip()
    if not raw:
        return set(DEFAULT_CONSOLE_STATUSES)
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


class MCSLogger:
    """Thread-safe file and console logger with severity filtering, ANSI colors, and live progress support."""

    def __init__(
        self,
        prefix: str = "mcsms",
        logs_dir: str | Path | None = None,
        per_run: bool = False,
        default_module: str = "SYSTEM"
    ):
        """
        Initializes a logger instance writing to a timestamped log file.

        :param prefix: Filename prefix (e.g., 'mcsms' -> mcsms-16-11-2026.log).
        :param logs_dir: Target directory for log files. Defaults to LOGS_DIR from .env or './mcsms_data/logs'.
        :param per_run: If True, includes time (HH-MM-SS) in the filename for per-execution logs.
        :param default_module: Default component tag included in log lines when not overridden.
        """
        if logs_dir is None:
            load_dotenv()
            logs_dir = os.getenv("LOGS_DIR", "./mcsms_data/logs")

        self.logs_dir = Path(logs_dir).expanduser().resolve()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.default_module = default_module.upper()
        self.console_statuses = _load_console_statuses()
        self._file_lock = threading.Lock()
        self._use_colors = sys.stdout.isatty()

        date_fmt = "%d-%m-%Y_%H-%M-%S" if per_run else "%d-%m-%Y"
        timestamp = datetime.now().strftime(date_fmt)
        self.log_file = self.logs_dir / f"{prefix}-{timestamp}.log"

    def write(
        self,
        status: str,
        message: str,
        module: str | None = None,
        console: bool | None = None
    ) -> None:
        """
        Writes a formatted log entry to the log file and prints it to stdout if important or explicitly forced.

        :param status: Severity or state label (e.g., 'INFO', 'WARN', 'ERROR', 'SUCCESS', 'PACKING').
        :param message: Log message content.
        :param module: Component name tag. Falls back to default_module.
        :param console: Explicit override for console output. If None, filters by console_statuses.
        """
        global _has_active_progress

        status_upper = status.upper()
        now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        mod_tag = (module or self.default_module).upper()

        plain_line = f"[{status_upper}] [{now_str}] [{mod_tag}] {message}"

        with self._file_lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(plain_line + "\n")

        should_print = console if console is not None else (status_upper in self.console_statuses)
        if should_print:
            if self._use_colors and status_upper in _ANSI_COLORS:
                colored_status = f"{_ANSI_COLORS[status_upper]}[{status_upper}]{_ANSI_RESET}"
                console_line = f"{colored_status} [{now_str}] [{mod_tag}] {message}"
            else:
                console_line = plain_line

            with _console_lock:
                if _has_active_progress:
                    sys.stdout.write("\r\033[K")
                    _has_active_progress = False
                print(console_line, flush=True)

    def progress(self, message: str, module: str | None = None) -> None:
        """
        Renders a single-line live status update in the terminal without creating new lines or writing to the log file.
        Automatically clears cleanly when a standard log message is printed.

        :param message: Live status text to display on screen.
        :param module: Component name tag. Falls back to default_module.
        """
        global _has_active_progress

        now_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        mod_tag = (module or self.default_module).upper()
        status_tag = "[PROGRESS]"

        if self._use_colors:
            status_tag = f"{_ANSI_COLORS['PROGRESS']}{status_tag}{_ANSI_RESET}"

        line = f"\r\033[K{status_tag} [{now_str}] [{mod_tag}] {message}"

        with _console_lock:
            sys.stdout.write(line)
            sys.stdout.flush()
            _has_active_progress = True


_system_logger: MCSLogger | None = None
_init_lock = threading.Lock()


def log(status: str, message: str, module: str = "MCSMS", console: bool | None = None) -> None:
    """
    Global logging function for general MCS-MS operations.
    Lazily initializes a shared daily logger writing to mcsms-DD-MM-YYYY.log.

    :param status: Severity or state label (e.g., 'INFO', 'WARN', 'ERROR', 'SUCCESS').
    :param message: Log message content.
    :param module: Calling module identifier (e.g., 'MCSMS', 'CHECK_FILES').
    :param console: Optional override to force or suppress stdout output.
    """
    global _system_logger
    if _system_logger is None:
        with _init_lock:
            if _system_logger is None:
                _system_logger = MCSLogger(prefix="mcsms", per_run=False, default_module="MCSMS")

    _system_logger.write(status=status, message=message, module=module, console=console)