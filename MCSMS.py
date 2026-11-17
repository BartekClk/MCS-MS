import argparse
from email import parser
import sys
from pathlib import Path
from typing import Callable
import time

from backup import BackupError, run_backup
from check_files import verify_and_create_paths
from logger import log
from rcon_connection import RconAuthError, RconConnection, RconConnectionError, RconError, ServerTarget
from updater import UpdateError, run_canary_update
from discord_webhook import DiscordWebhookClient, DiscordWebhookError
from daemon import run_daemon

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
    parser.add_argument(
        "--shutdown-test",
        nargs="?",
        const="main",
        default=None,
        type=str.lower,
        choices=["main", "canary"],
        help="Run the RCON shutdown countdown test on the target server (default: main)."
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Start the persistent HTTP webhook listener and daily backup scheduler.",
    )

    args = parser.parse_args()
    if not any([args.run, args.backup, args.backup_all, args.shutdown_test is not None, args.daemon]):
        parser.error("At least one action flag (--run, --backup, --backup-all, --shutdown_test, --daemon) must be specified.")

    return args


def handle_backup(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the world-only backup procedure (--backup).
    Temporarily disables world auto-saving and flushes chunks via RCON if the main
    server is online, guaranteeing 'save-on' is restored in a finally block.

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--backup] procedure...", module="MCSMS")
    rcon = RconConnection.from_env(target="main", env_path=env_path)
    saves_disabled = False

    try:
        try:
            with rcon:
                rcon.send_command("save-off")
                saves_disabled = True
                rcon.send_command("save-all flush")
            log("INFO", "Disabled world auto-save and flushed chunks via RCON.", module="MCSMS")
            time.sleep(2)
        except RconAuthError as e:
            log("ERROR", f"RCON authentication failed before backup: {e}", module="MCSMS")
            return 1
        except RconConnectionError as e:
            log("WARN", f"Main server offline or unreachable via RCON ({e}); proceeding with offline backup.", module="MCSMS")

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
    finally:
        if saves_disabled:
            try:
                rcon.send_command("save-on")
                log("INFO", "Re-enabled world auto-save via RCON.", module="MCSMS")
            except RconError as e:
                log("ERROR", f"CRITICAL: Failed to re-enable auto-save ('save-on') via RCON: {e}", module="MCSMS")


def handle_backup_all(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the full server directory backup procedure (--backup-all).
    Temporarily disables world auto-saving and flushes chunks via RCON if the main
    server is online, guaranteeing 'save-on' is restored in a finally block.

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--backup-all] procedure...", module="MCSMS")
    rcon = RconConnection.from_env(target="main", env_path=env_path)
    saves_disabled = False

    try:
        try:
            with rcon:
                rcon.send_command("save-off")
                saves_disabled = True
                rcon.send_command("save-all flush")
            log("INFO", "Disabled world auto-save and flushed chunks via RCON.", module="MCSMS")
            time.sleep(2)
        except RconAuthError as e:
            log("ERROR", f"RCON authentication failed before full backup: {e}", module="MCSMS")
            return 1
        except RconConnectionError as e:
            log(
                "WARN",
                f"Main server offline or unreachable via RCON ({e}); proceeding with offline full backup.",
                module="MCSMS",
            )

        result = run_backup(env_path=env_path, full_backup=True)
        log(
            "INFO",
            f"Full server backup finished ({result['archive_name']}). Detailed log: {result['log_file']}",
            module="MCSMS",
        )
        return 0
    except BackupError as e:
        log("ERROR", f"Full server backup aborted due to fatal error: {e}", module="MCSMS")
        return 1
    except Exception as e:
        log("FATAL", f"Unexpected crash during full server backup: {e}", module="MCSMS")
        return 1
    finally:
        if saves_disabled:
            try:
                rcon.send_command("save-on")
                log("INFO", "Re-enabled world auto-save via RCON.", module="MCSMS")
            except RconError as e:
                log("ERROR", f"CRITICAL: Failed to re-enable auto-save ('save-on') via RCON: {e}", module="MCSMS")

def handle_shutdown(
    env_path: str | Path,
    paths: dict[str, Path],
    target: ServerTarget = "main",
    test: bool = False
) -> int:
    """
    Executes the RCON shutdown countdown sequence on the specified server target.

    :param env_path: Path to the .env configuration file.
    :param paths: Validated project paths dictionary returned by verify_and_create_paths.
    :param target: Target server profile ('main' or 'canary').
    :param test: If True, runs as an isolated CLI diagnostic test rather than a production pipeline step.
    :return: Process exit code (0 on success, 1 on failure).
    """
    mode_label = f"SHUTDOWN TEST ({target.upper()})" if test else f"SERVER SHUTDOWN ({target.upper()})"
    log("INFO", f"Initiating {mode_label} via RCON...", module="MCSMS")

    try:
        rcon = RconConnection.from_env(target=target, env_path=env_path)
        rcon.shutdown_countdown()
        log("SUCCESS", f"{mode_label} completed successfully.", module="MCSMS")
        return 0
    except RconError as e:
        log("ERROR", f"{mode_label} failed [{type(e).__name__}]: {e}", module="MCSMS")
        return 1
    except Exception as e:
        log("FATAL", f"Unexpected crash during {mode_label.lower()}: {e}", module="MCSMS")
        return 1

def handle_update(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the canary-verified modpack update workflow: shuts down main, updates and
    validates the canary server, and updates + starts main only if canary boots cleanly.

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing canary-verified server update procedure...", module="MCSMS")
    try:
        result = run_canary_update(env_path=env_path)
        log(
            "SUCCESS",
            f"Server update completed (version: {result['pack_version']}). Detailed log: {result['log_file']}",
            module="MCSMS",
        )
        return 0
    except UpdateError as e:
        log("ERROR", f"Server update aborted due to fatal error: {e}", module="MCSMS")
        return 1
    except Exception as e:
        log("FATAL", f"Unexpected crash during server update: {e}", module="MCSMS")
        return 1

def handle_run(env_path: Path, paths: dict[str, Path]) -> int:
    """
    Executes the full automated maintenance and deployment pipeline (--run):
    1. World backup to Google Drive (with RCON save-off / save-all flush / save-on).
    2. Graceful shutdown of the main server via RCON countdown.
    3. Modpack update and startup healthcheck on the canary server.
    4. Conditional modpack update and startup of the main server if canary passes.

    :param env_path: Path to the .env configuration file.
    :param paths: Verified system paths dictionary returned by check_files.
    :return: Exit status code (0 on success, 1 on failure).
    """
    log("INFO", "Executing [--run] full maintenance pipeline...", module="MCSMS")

    # Etap 1: Backup świata głównego serwera przed wyłączeniem
    log("INFO", "[Step 1/2] Starting pre-update world backup...", module="MCSMS")
    backup_code = handle_backup(env_path, paths)
    if backup_code != 0:
        log(
            "ERROR",
            "Aborting [--run] pipeline: pre-update backup failed. Main server remains untouched.",
            module="MCSMS",
        )
        return backup_code

    # Etap 2: Wyłączenie main -> update & test canary -> warunkowy update & start main
    log("INFO", "[Step 2/2] Starting canary-verified update & restart sequence...", module="MCSMS")
    update_code = handle_update(env_path, paths)
    if update_code != 0:
        log(
            "ERROR",
            "Aborting [--run] pipeline: canary verification or server lifecycle step failed.",
            module="MCSMS",
        )
        return update_code

    log("SUCCESS", f"[--run] pipeline completed. Main server is online at: {paths['server_dir']}", module="MCSMS")
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

    if args.daemon:
        return run_daemon(env_path=args.env)

    # Execution order is defined directly by the sequence below
    pipeline: list[tuple[str, bool, StepHandler]] = [
        (
            "--shutdown_test",
            args.shutdown_test is not None,
            lambda env, p: handle_shutdown(env, p, target=args.shutdown_test, test=True)
        ),
        ("--backup", args.backup and not args.run, handle_backup),
        ("--backup-all", args.backup_all, handle_backup_all),
        ("--run", args.run, handle_run),
    ]

    started_at = time.monotonic()
    executed_flags = [flag for flag, enabled, _ in pipeline if enabled]
    cmd_label = " ".join(executed_flags) if executed_flags else " ".join(sys.argv[1:])

    final_code = 0
    report_details = "Wszystkie etapy zakończyły się powodzeniem."

    for flag_name, is_enabled, handler in pipeline:
        if is_enabled:
            exit_code = handler(args.env, paths)
            if exit_code != 0:
                final_code = exit_code
                report_details = (
                    f"Pipeline przerwany na etapie `{flag_name}` (kod wyjścia: `{exit_code}`). "
                    "Sprawdź załączony log."
                )
                log("ERROR", f"Pipeline halted because {flag_name} exited with code {exit_code}.", module="MCSMS")
                break

    latest_log = max(paths["logs_dir"].glob("*.log"), key=lambda p: p.stat().st_mtime, default=None)

    try:
        admin_notifier = DiscordWebhookClient.for_admin_from_env(env_path=args.env)
        admin_notifier.send_admin_report(
            command=cmd_label,
            success=(final_code == 0),
            exit_code=final_code,
            duration_sec=time.monotonic() - started_at,
            details=report_details,
            attachment_path=latest_log if final_code != 0 else None,
        )
    except DiscordWebhookError as e:
        log("WARN", f"Failed to send admin Discord report: {e}", module="MCSMS")

    return final_code


if __name__ == "__main__":
    sys.exit(main())