import argparse
import sys
from pathlib import Path
from typing import Callable

from backup import BackupError, run_backup
from check_files import verify_and_create_paths
from logger import log

StepHandler = Callable[[Path, dict[str, Path]], int]


def parse_args() -> argparse.Namespace:
    """Parses command-line interface flags for MCS-MS."""
    parser = argparse.ArgumentParser(
        prog="MCSMS",
        description="Minecraft Server Management System (MCS-MS)"
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(".env"),
        help="Path to the .env configuration file (default: .env)"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Run the world backup procedure to Google Drive."
    )
    parser.add_argument(
        "--backup-all",
        action="store_true",
        help="Run a full server directory backup to Google Drive."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Start the full MCS-MS management and server runtime."
    )

    args = parser.parse_args()
    if not (args.backup or args.backup_all or args.run):
        parser.error("At least one action flag (--run, --backup, --backup-all) must be specified.")

    return args


def handle_backup(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the world-only backup procedure (--backup).
    Designed as an isolated step to easily prepend/append RCON or notification hooks later.

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--backup] procedure...", module="MCSMS")
    try:
        result = run_backup(env_path=env_path, full_backup=False)
        log(
            "INFO",
            f"World backup finished ({result['archive_name']}). Detailed log: {result['log_file']}",
            module="MCSMS"
        )
        return 0
    except BackupError as e:
        log("ERROR", f"World backup aborted due to fatal error: {e}", module="MCSMS")
        return 1
    except Exception as e:
        log("FATAL", f"Unexpected crash during world backup: {e}", module="MCSMS")
        return 1


def handle_backup_all(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the full server directory backup procedure (--backup-all).
    Kept separate from handle_backup to allow distinct pre/post steps (e.g., server stop/start).

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--backup-all] procedure...", module="MCSMS")
    try:
        result = run_backup(env_path=env_path, full_backup=True)
        log(
            "INFO",
            f"Full server backup finished ({result['archive_name']}). Detailed log: {result['log_file']}",
            module="MCSMS"
        )
        return 0
    except BackupError as e:
        log("ERROR", f"Full server backup aborted due to fatal error: {e}", module="MCSMS")
        return 1
    except Exception as e:
        log("FATAL", f"Unexpected crash during full server backup: {e}", module="MCSMS")
        return 1


def handle_run(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the main server runtime and management loop (--run).

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--run] procedure...", module="MCSMS")
    log("INFO", f"Server root ready at: {paths['server_dir']}", module="MCSMS")
    log("INFO", "Runtime loop placeholder ready for server process & RCON integration.", module="MCSMS")
    return 0


def main() -> int:
    """Entry point for the Minecraft Server Management System (MCS-MS)."""
    args = parse_args()
    log("INFO", "Starting MCS-MS...", module="MCSMS")

    try:
        paths = verify_and_create_paths(args.env)
    except FileNotFoundError:
        log("FATAL", "Initialization aborted due to missing configuration file.", module="MCSMS")
        return 1

    # Execution order is defined directly by the sequence below
    pipeline: list[tuple[str, bool, StepHandler]] = [
        ("--backup", args.backup, handle_backup),
        ("--backup-all", args.backup_all, handle_backup_all),
        ("--run", args.run, handle_run),
    ]

    for flag_name, is_enabled, handler in pipeline:
        if is_enabled:
            exit_code = handler(args.env, paths)
            if exit_code != 0:
                log("ERROR", f"Pipeline halted because {flag_name} exited with code {exit_code}.", module="MCSMS")
                return exit_code

    return 0


if __name__ == "__main__":
    sys.exit(main())