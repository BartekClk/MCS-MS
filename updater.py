import os
import re
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from logger import MCSLogger, log
from rcon_connection import RconConnection, RconError, ServerTarget
from github_release import GitHubReleaseClient, GitHubReleaseError
from discord_webhook import DiscordWebhookClient, DiscordWebhookError

_FATAL_LOG_MARKERS = (
    "---- Minecraft Crash Report ----",
    "Exception in server tick loop",
    "Failed to start the minecraft server",
    "Missing or unsupported mandatory dependencies",
    "Mod Loading has failed",
    "/FATAL]",
)

_READY_LOG_MARKER = "Done ("


class UpdateError(Exception):
    """Base exception for all fatal server update and canary verification errors."""


class UpdateConfigError(UpdateError):
    """Raised when required updater configuration in .env is missing or invalid."""


class ServerLifecycleError(UpdateError):
    """Raised when a server fails to stop or start cleanly within the configured timeout."""


class PackwizUpdateError(UpdateError):
    """Raised when packwiz-installer-bootstrap fails to synchronize the modpack."""


class CanaryValidationError(UpdateError):
    """Raised when the canary server fails to boot cleanly after a modpack update."""


@dataclass(frozen=True)
class UpdaterConfig:
    """Validated runtime settings loaded strictly from .env for the canary update pipeline."""

    main_server_dir: Path
    canary_server_dir: Path
    motd_template_path: Path
    main_screen_session: str
    canary_screen_session: str
    main_start_cmd: str
    canary_start_cmd: str
    java_bin: str
    packwiz_bootstrap_jar: str
    packwiz_pack_url: str
    main_rcon_host: str
    main_rcon_port: int
    canary_rcon_host: str
    canary_rcon_port: int
    stop_timeout: int
    canary_boot_timeout: int
    main_boot_timeout: int
    canary_stop_after_test: bool
    logs_dir: Path


class ServerUpdater:
    """
    Orchestrates safe modpack rollouts by shutting down the main server, updating and
    boot-testing the canary environment first, and promoting the update to production
    only after canary verification succeeds.
    """

    def __init__(self, config: UpdaterConfig, env_path: Path) -> None:
        self.cfg = config
        self.env_path = env_path
        self._logger = MCSLogger(
            prefix="updater",
            logs_dir=self.cfg.logs_dir,
            per_run=True,
            default_module="UPDATER",
        )

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "ServerUpdater":
        """
        Loads and strictly validates all updater configuration parameters from .env.

        :param env_path: Path to the .env configuration file.
        :return: Configured ServerUpdater instance.
        :raises UpdateConfigError: If .env or any required variable is missing or invalid.
        """
        env_file = Path(env_path)
        if not env_file.exists():
            raise UpdateConfigError(f"Configuration file not found: {env_file.resolve()}")

        load_dotenv(dotenv_path=env_file)

        def req_str(key: str) -> str:
            val = os.getenv(key, "").strip()
            if not val:
                raise UpdateConfigError(f"Required environment variable '{key}' is missing or empty.")
            return val

        def req_int(key: str) -> int:
            raw = req_str(key)
            try:
                parsed = int(raw)
                if parsed <= 0:
                    raise ValueError("Must be positive")
                return parsed
            except ValueError as e:
                raise UpdateConfigError(f"Invalid positive integer for '{key}': {raw}") from e

        stop_after_raw = req_str("CANARY_STOP_AFTER_TEST").lower()
        if stop_after_raw not in ("true", "false"):
            raise UpdateConfigError(
                f"Invalid boolean for 'CANARY_STOP_AFTER_TEST': {stop_after_raw} (expected true/false)"
            )

        main_dir = Path(req_str("SERVER_DIR")).expanduser().resolve()
        canary_dir = Path(req_str("CANARY_SERVER_DIR")).expanduser().resolve()
        logs_dir = Path(req_str("LOGS_DIR")).expanduser().resolve()
        motd_template_path = Path(req_str("MOTD_TEMPLATE_PATH")).expanduser().resolve()

        if not main_dir.is_dir():
            raise UpdateConfigError(f"Main SERVER_DIR does not exist: {main_dir}")
        if not canary_dir.is_dir():
            raise UpdateConfigError(f"CANARY_SERVER_DIR does not exist: {canary_dir}")
        if not motd_template_path.is_file():
            raise UpdateConfigError(f"MOTD_TEMPLATE_PATH does not exist: {motd_template_path}")

        config = UpdaterConfig(
            main_server_dir=main_dir,
            canary_server_dir=canary_dir,
            motd_template_path=motd_template_path,
            main_screen_session=req_str("MAIN_SCREEN_SESSION"),
            canary_screen_session=req_str("CANARY_SCREEN_SESSION"),
            main_start_cmd=req_str("MAIN_START_CMD"),
            canary_start_cmd=req_str("CANARY_START_CMD"),
            java_bin=req_str("JAVA_BIN"),
            packwiz_bootstrap_jar=req_str("PACKWIZ_BOOTSTRAP_JAR"),
            packwiz_pack_url=req_str("PACKWIZ_PACK_URL"),
            main_rcon_host=req_str("MAIN_RCON_HOST"),
            main_rcon_port=req_int("MAIN_RCON_PORT"),
            canary_rcon_host=req_str("CANARY_RCON_HOST"),
            canary_rcon_port=req_int("CANARY_RCON_PORT"),
            stop_timeout=req_int("SERVER_STOP_TIMEOUT"),
            canary_boot_timeout=req_int("CANARY_BOOT_TIMEOUT"),
            main_boot_timeout=req_int("MAIN_BOOT_TIMEOUT"),
            canary_stop_after_test=(stop_after_raw == "true"),
            logs_dir=logs_dir,
        )
        return cls(config=config, env_path=env_file)

    @staticmethod
    def _is_tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _is_screen_alive(session_name: str) -> bool:
        result = subprocess.run(
            ["screen", "-ls"],
            capture_output=True,
            text=True,
            check=False,
        )
        pattern = re.compile(rf"\b\d+\.{re.escape(session_name)}\b")
        return bool(pattern.search(result.stdout))

    def _is_server_running(self, target: ServerTarget) -> bool:
        if target == "main":
            return self._is_screen_alive(self.cfg.main_screen_session) or self._is_tcp_open(
                self.cfg.main_rcon_host, self.cfg.main_rcon_port
            )
        return self._is_screen_alive(self.cfg.canary_screen_session) or self._is_tcp_open(
            self.cfg.canary_rcon_host, self.cfg.canary_rcon_port
        )

    def _wait_until_stopped(self, target: ServerTarget, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._is_server_running(target):
                return
            time.sleep(1.5)

        raise ServerLifecycleError(
            f"Server '{target.upper()}' failed to shut down within {timeout}s."
        )

    def stop_server(self, target: ServerTarget, graceful_countdown: bool = True) -> None:
        """
        Stops the specified server via RCON and blocks until both RCON and screen terminate.

        :param target: Target server profile ('main' or 'canary').
        :param graceful_countdown: If True, runs the full RCON shutdown_countdown script;
                                   otherwise sends an immediate 'stop' command.
        :raises ServerLifecycleError: If the server does not stop within SERVER_STOP_TIMEOUT.
        """
        label = target.upper()
        if not self._is_server_running(target):
            self._logger.write("INFO", f"{label} server is already offline.")
            log("INFO", f"{label} server is already stopped.", module="UPDATER")
            return

        log("INFO", f"Stopping {label} server via RCON...", module="UPDATER")
        self._logger.write("INFO", f"Initiating shutdown for {label} server (countdown={graceful_countdown})...")

        try:
            rcon = RconConnection.from_env(target=target, env_path=self.env_path)
            if graceful_countdown:
                rcon.shutdown_countdown(test=False)
            else:
                rcon.send_command("stop")
        except RconError as e:
            self._logger.write("WARN", f"RCON notice during {label} shutdown: {e}")

        self._wait_until_stopped(target, self.cfg.stop_timeout)
        self._logger.write("SUCCESS", f"{label} server stopped cleanly.")
        log("SUCCESS", f"{label} server is now offline.", module="UPDATER")

    def _resolve_bootstrap_jar(self, server_dir: Path) -> Path:
        jar_path = Path(self.cfg.packwiz_bootstrap_jar).expanduser()
        if not jar_path.is_absolute():
            jar_path = (server_dir / jar_path).resolve()
        if not jar_path.is_file():
            raise PackwizUpdateError(f"Packwiz bootstrap JAR not found: {jar_path}")
        return jar_path

    def update_modpack(self, target: ServerTarget) -> None:
        """
        Executes packwiz-installer-bootstrap in server mode inside the target server directory.

        :param target: Target server profile ('main' or 'canary').
        :raises PackwizUpdateError: If the bootstrap jar is missing or Packwiz exits non-zero.
        """
        label = target.upper()
        server_dir = self.cfg.main_server_dir if target == "main" else self.cfg.canary_server_dir

        if self._is_server_running(target):
            raise PackwizUpdateError(
                f"Refusing to update modpack while {label} server is still running."
            )

        bootstrap_jar = self._resolve_bootstrap_jar(server_dir)
        cmd = [
            self.cfg.java_bin,
            "-jar",
            str(bootstrap_jar),
            "-g",
            "-s",
            "server",
            self.cfg.packwiz_pack_url,
        ]

        log("INFO", f"Updating {label} modpack via Packwiz...", module="UPDATER")
        self._logger.write("INFO", f"Running Packwiz in {server_dir}: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            cwd=str(server_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        assert process.stdout is not None
        output_lines: list[str] = []
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            if line:
                output_lines.append(line)
                self._logger.write("PACKWIZ", f"[{label}] {line}")

        return_code = process.wait()
        if return_code != 0:
            tail = " | ".join(output_lines[-5:]) if output_lines else "no output"
            err = f"Packwiz update failed for {label} (exit code {return_code}): {tail}"
            self._logger.write("ERROR", err)
            raise PackwizUpdateError(err)

        self._logger.write("SUCCESS", f"Packwiz update completed for {label}.")
        log("SUCCESS", f"{label} modpack updated successfully.", module="UPDATER")

    def start_and_verify(self, target: ServerTarget, boot_timeout: int) -> None:
        """
        Starts the target server inside a detached GNU screen session and verifies that
        Forge boots cleanly without fatal errors and responds to RCON commands.

        :param target: Target server profile ('main' or 'canary').
        :param boot_timeout: Maximum seconds to wait for full server startup and RCON readiness.
        :raises ServerLifecycleError: If the server crashes, logs a fatal error, or times out.
        """
        label = target.upper()
        if target == "main":
            server_dir = self.cfg.main_server_dir
            session_name = self.cfg.main_screen_session
            start_cmd = self.cfg.main_start_cmd
        else:
            server_dir = self.cfg.canary_server_dir
            session_name = self.cfg.canary_screen_session
            start_cmd = self.cfg.canary_start_cmd

        if self._is_server_running(target):
            raise ServerLifecycleError(f"Cannot start {label}: server or screen '{session_name}' is already active.")

        latest_log = server_dir / "logs" / "latest.log"
        crash_dir = server_dir / "crash-reports"

        prev_inode = latest_log.stat().st_ino if latest_log.exists() else None
        log_offset = latest_log.stat().st_size if latest_log.exists() else 0
        existing_crashes = {p.name for p in crash_dir.glob("*")} if crash_dir.is_dir() else set()

        shell_cmd = f"cd {shlex.quote(str(server_dir))} && {start_cmd}"
        log("INFO", f"Starting {label} server in screen session '{session_name}'...", module="UPDATER")
        self._logger.write("INFO", f"Launching {label} via: screen -dmS {session_name} bash -c {shell_cmd!r}")

        try:
            subprocess.run(
                ["screen", "-dmS", session_name, "bash", "-c", shell_cmd],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            raise ServerLifecycleError(f"Failed to create screen session '{session_name}': {e.stderr.strip()}") from e

        time.sleep(2.0)

        deadline = time.monotonic() + boot_timeout
        saw_done_marker = False
        recent_lines: list[str] = []

        while time.monotonic() < deadline:
            if crash_dir.is_dir():
                new_crashes = [p.name for p in crash_dir.glob("*") if p.name not in existing_crashes]
                if new_crashes:
                    err = f"{label} generated crash report(s) during startup: {', '.join(new_crashes)}"
                    self._logger.write("ERROR", err)
                    raise ServerLifecycleError(err)

            if latest_log.exists():
                st = latest_log.stat()
                if st.st_ino != prev_inode or st.st_size < log_offset:
                    prev_inode = st.st_ino
                    log_offset = 0

                if st.st_size > log_offset:
                    with latest_log.open("rb") as f:
                        f.seek(log_offset)
                        chunk = f.read(st.st_size - log_offset)
                        log_offset = st.st_size

                    for line in chunk.decode("utf-8", errors="replace").splitlines():
                        clean = line.strip()
                        if not clean:
                            continue
                        recent_lines.append(clean)
                        if len(recent_lines) > 30:
                            recent_lines.pop(0)

                        if any(marker in clean for marker in _FATAL_LOG_MARKERS):
                            self._logger.write("ERROR", f"[{label} LOG FATAL] {clean}")
                            raise ServerLifecycleError(
                                f"{label} encountered a fatal startup error: {clean}"
                            )

                        if "/ERROR]" in clean:
                            self._logger.write("WARN", f"[{label} LOG ERROR] {clean}")

                        if _READY_LOG_MARKER in clean:
                            saw_done_marker = True
                            self._logger.write("INFO", f"{label} reported world load completion: {clean}")

            if not self._is_screen_alive(session_name):
                tail = " | ".join(recent_lines[-5:]) if recent_lines else "no log lines captured"
                err = f"{label} screen session '{session_name}' terminated unexpectedly during startup. Tail: {tail}"
                self._logger.write("ERROR", err)
                raise ServerLifecycleError(err)

            if saw_done_marker:
                try:
                    rcon = RconConnection.from_env(target=target, env_path=self.env_path, timeout=5.0)
                    list_resp = rcon.send_command("list")
                    self._logger.write("SUCCESS", f"{label} RCON healthcheck passed: {list_resp.strip()}")
                    log("SUCCESS", f"{label} server booted cleanly and passed RCON verification.", module="UPDATER")
                    return
                except RconError:
                    pass

            time.sleep(2.0)

        raise ServerLifecycleError(
            f"{label} server did not reach ready state within {boot_timeout}s."
        )

    def run_canary_verified_update(self) -> dict[str, str]:
        """
        Executes the complete canary-gated update pipeline:
        1. Gracefully shuts down the main server via RCON countdown.
        2. Stops canary if running, updates canary modpack via Packwiz, and boots canary.
        3. Verifies canary boots without fatal errors and responds to RCON.
        4. If canary succeeds: updates main modpack via Packwiz and starts main server.
        5. If canary fails: raises CanaryValidationError without modifying the main server.

        :return: Dictionary summarizing update execution status and log file path.
        :raises CanaryValidationError: If canary update or startup verification fails.
        :raises UpdateError: If stopping/updating/starting the main server fails.
        """
        self._logger.write("INFO", "=== Starting Canary-Verified Server Update Pipeline ===")
        log("INFO", "Starting canary-verified server update workflow...", module="UPDATER")

        self.stop_server(target="main", graceful_countdown=True)

        try:
            gh_client = GitHubReleaseClient.from_env(env_path=self.env_path)
            latest_release = gh_client.get_latest_release()
        except GitHubReleaseError as e:
            err = f"Failed to resolve latest modpack release from GitHub: {e}"
            self._logger.write("ERROR", err)
            raise UpdateError(err) from e
        
        try:
            self.stop_server(target="canary", graceful_countdown=False)
            self.update_modpack(target="canary")
            self.start_and_verify(target="canary", boot_timeout=self.cfg.canary_boot_timeout)
        except UpdateError as e:
            err = f"Canary verification failed; main server will NOT be updated: {e}"
            self._logger.write("ERROR", err)
            log("ERROR", err, module="UPDATER")
            if self._is_server_running("canary"):
                try:
                    self.stop_server(target="canary", graceful_countdown=False)
                except UpdateError:
                    pass
            raise CanaryValidationError(err) from e

        if self.cfg.canary_stop_after_test:
            log("INFO", "Stopping verified canary server to free system resources...", module="UPDATER")
            self.stop_server(target="canary", graceful_countdown=False)

        log("INFO", "Canary verification passed. Updating and starting MAIN server...", module="UPDATER")
        self.update_modpack(target="main")
        self.inject_motd(pack_version=latest_release.tag_name)
        self.start_and_verify(target="main", boot_timeout=self.cfg.main_boot_timeout)
        gh_client.save_installed_release(latest_release)
        try:
            discord_client = DiscordWebhookClient.from_env(env_path=self.env_path)
            discord_client.send_release_notification(latest_release)
        except DiscordWebhookError as e:
            warn_msg = f"Main server updated successfully, but Discord notification failed: {e}"
            self._logger.write("ERROR", warn_msg)
            log("ERROR", warn_msg, module="UPDATER")

        self._logger.write("SUCCESS", "=== Canary-Verified Server Update Pipeline Completed ===")
        return {
            "status": "success",
            "log_file": str(self._logger.log_file),
            "pack_version": latest_release.tag_name,
        }

    def inject_motd(self, pack_version: str) -> str:
        """
        Renders modt_template.txt by replacing %pack_version% with the latest release tag
        and writes the result into motd= inside the main server's server.properties.
        """
        if not self.cfg.motd_template_path.is_file():
            raise UpdateError(f"MOTD template file not found: {self.cfg.motd_template_path}")

        server_properties = self.cfg.main_server_dir / "server.properties"
        if not server_properties.is_file():
            raise UpdateError(f"Main server.properties not found: {server_properties}")

        raw_template = self.cfg.motd_template_path.read_text(encoding="utf-8").strip("\r\n")
        rendered_motd = (
            raw_template
            .replace("%pack_version%", pack_version)
            .replace("\r\n", "\n")
            .replace("\n", "\\n")
        )
        rendered_motd = re.sub(r"^( +)", lambda m: "\\ "*len(m.group(1)), rendered_motd)

        lines = server_properties.read_text(encoding="utf-8").splitlines()
        updated_lines: list[str] = []
        motd_replaced = False

        for line in lines:
            if line.lstrip().startswith("motd="):
                updated_lines.append(f"motd={rendered_motd}")
                motd_replaced = True
            else:
                updated_lines.append(line)

        if not motd_replaced:
            updated_lines.append(f"motd={rendered_motd}")

        server_properties.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
        self._logger.write("SUCCESS", f"Injected MOTD into {server_properties}: {rendered_motd}")
        log("SUCCESS", f"Updated main server MOTD with pack version '{pack_version}'.", module="UPDATER")
        return rendered_motd


def run_canary_update(env_path: str | Path = ".env") -> dict[str, str]:
    """
    Convenience entry point to execute the canary-verified update workflow from .env.

    :param env_path: Path to the .env configuration file.
    :return: Dictionary containing execution status and updater log file path.
    :raises UpdateError: If any stage of the update or verification fails.
    """
    updater = ServerUpdater.from_env(env_path=env_path)
    return updater.run_canary_verified_update()