import os
import re
import socket
import struct
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from logger import MCSLogger, log

# Minecraft / Source RCON packet types
_SERVERDATA_RESPONSE_VALUE = 0
_SERVERDATA_EXECCOMMAND = 2
_SERVERDATA_AUTH = 3

_SLEEP_PATTERN = re.compile(r"^sleep\(\s*([+-]?\d+(?:\.\d+)?)\s*\)$", re.IGNORECASE)

ServerTarget = Literal["main", "canary"]


class RconError(Exception):
    """Base exception for all fatal RCON configuration, connection, and script errors."""


class RconConfigError(RconError):
    """Raised when required RCON variables in .env are missing or invalid."""


class RconConnectionError(RconError):
    """Raised when TCP socket connection or packet transmission fails."""


class RconAuthError(RconError):
    """Raised when RCON authentication is rejected by the Minecraft server."""


class RconScriptError(RconError):
    """Raised when an RCON command script file is missing or contains invalid syntax."""


class RconConnection:
    """
    Manages TCP RCON communication with a Minecraft server, supporting single-command
    execution and scripted command sequences with sleep() intervals.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        scripts_dir: Path,
        shutdown_script: str,
        server_label: str = "MAIN",
        logs_dir: Path | None = None,
        timeout: float = 10.0
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.scripts_dir = scripts_dir
        self.shutdown_script = shutdown_script
        self.server_label = server_label.upper()
        self.timeout = timeout

        resolved_logs = logs_dir or Path("./mcsms_data/logs").resolve()
        self._logger = MCSLogger(
            prefix="rcon",
            logs_dir=resolved_logs,
            per_run=False,
            default_module=f"RCON:{self.server_label}"
        )

        self._sock: socket.socket | None = None
        self._request_counter = 0
        self._in_context = False

    @classmethod
    def from_env(
        cls,
        target: ServerTarget = "main",
        env_path: str | Path = ".env",
        timeout: float = 10.0
    ) -> "RconConnection":
        """
        Instantiates an RconConnection strictly from .env variables without hidden fallbacks.

        :param target: Target server profile ('main' for production/Test1 or 'canary' for Test2).
        :param env_path: Path to the .env configuration file.
        :param timeout: Socket timeout in seconds.
        :return: Configured RconConnection instance.
        :raises RconConfigError: If .env or any required RCON parameter is missing or malformed.
        """
        env_file = Path(env_path)
        if not env_file.exists():
            raise RconConfigError(f"Configuration file not found: {env_file.resolve()}")

        load_dotenv(dotenv_path=env_file)

        normalized_target = target.strip().lower()
        if normalized_target not in ("main", "canary"):
            raise RconConfigError(f"Invalid RCON target '{target}'. Expected 'main' or 'canary'.")

        prefix = "MAIN_RCON" if normalized_target == "main" else "CANARY_RCON"

        host = os.getenv(f"{prefix}_HOST", "").strip()
        port_raw = os.getenv(f"{prefix}_PORT", "").strip()
        password = os.getenv(f"{prefix}_PASSWORD", "").strip()
        scripts_dir_raw = os.getenv("RCON_SCRIPTS_DIR", "").strip()
        shutdown_script = os.getenv("RCON_SHUTDOWN_SCRIPT", "").strip()
        logs_dir_raw = os.getenv("LOGS_DIR", "").strip()

        if not all([host, port_raw, password, scripts_dir_raw, shutdown_script, logs_dir_raw]):
            raise RconConfigError(
                f"One or more required RCON variables ({prefix}_HOST, {prefix}_PORT, {prefix}_PASSWORD, "
                f"RCON_SCRIPTS_DIR, RCON_SHUTDOWN_SCRIPT, LOGS_DIR) are missing or empty in .env."
            )

        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError("Port must be between 1 and 65535.")
        except ValueError as e:
            raise RconConfigError(f"Invalid {prefix}_PORT value '{port_raw}': {e}") from e

        scripts_dir = Path(scripts_dir_raw).expanduser().resolve()
        logs_dir = Path(logs_dir_raw).expanduser().resolve()

        return cls(
            host=host,
            port=port,
            password=password,
            scripts_dir=scripts_dir,
            shutdown_script=shutdown_script,
            server_label=normalized_target.upper(),
            logs_dir=logs_dir,
            timeout=timeout
        )

    def __enter__(self) -> "RconConnection":
        self._in_context = True
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._in_context = False
        self.close()

    def _next_request_id(self) -> int:
        self._request_counter = (self._request_counter % 2_000_000_000) + 1
        return self._request_counter

    def _send_packet(self, request_id: int, packet_type: int, payload: str) -> None:
        if self._sock is None:
            raise RconConnectionError("Cannot send packet: RCON socket is not connected.")

        payload_bytes = payload.encode("utf-8")
        # Packet body: request_id (4B) + packet_type (4B) + payload + 2 null bytes
        body = struct.pack("<ii", request_id, packet_type) + payload_bytes + b"\x00\x00"
        packet = struct.pack("<i", len(body)) + body
        self._sock.sendall(packet)

    def _recv_exact(self, num_bytes: int) -> bytes:
        if self._sock is None:
            raise RconConnectionError("Cannot read packet: RCON socket is not connected.")

        chunks = bytearray()
        while len(chunks) < num_bytes:
            data = self._sock.recv(num_bytes - len(chunks))
            if not data:
                raise RconConnectionError("RCON connection closed unexpectedly by the remote server.")
            chunks.extend(data)
        return bytes(chunks)

    def _read_packet(self) -> tuple[int, int, str]:
        header = self._recv_exact(4)
        (packet_len,) = struct.unpack("<i", header)
        if packet_len < 10:
            raise RconConnectionError(f"Malformed RCON packet length received: {packet_len}")

        body = self._recv_exact(packet_len)
        resp_id, resp_type = struct.unpack("<ii", body[:8])
        payload = body[8:-2].decode("utf-8", errors="replace")
        return resp_id, resp_type, payload

    def connect(self) -> None:
        """
        Opens a TCP socket to the configured RCON endpoint and authenticates using RCON password.

        :raises RconConnectionError: If TCP connection fails or times out.
        :raises RconAuthError: If the server rejects the RCON password.
        """
        if self._sock is not None:
            return

        self._logger.write("INFO", f"Connecting to RCON at {self.host}:{self.port}...")
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            self._sock = sock
        except OSError as e:
            self._sock = None
            err = f"Failed to connect to RCON server {self.host}:{self.port}: {e}"
            self._logger.write("ERROR", err)
            raise RconConnectionError(err) from e

        try:
            auth_id = self._next_request_id()
            self._send_packet(auth_id, _SERVERDATA_AUTH, self.password)

            resp_id, resp_type, _ = self._read_packet()
            # Minecraft sends an empty SERVERDATA_RESPONSE_VALUE before SERVERDATA_AUTH_RESPONSE
            if resp_type == _SERVERDATA_RESPONSE_VALUE:
                resp_id, resp_type, _ = self._read_packet()

            if resp_id == -1 or resp_id != auth_id:
                raise RconAuthError(
                    f"RCON authentication failed for {self.host}:{self.port} (invalid password)."
                )

            self._logger.write("SUCCESS", f"Authenticated with RCON server {self.host}:{self.port}.")
        except RconAuthError:
            self.close()
            self._logger.write("ERROR", f"RCON authentication rejected by {self.host}:{self.port}.")
            raise
        except OSError as e:
            self.close()
            err = f"Socket error during RCON authentication with {self.host}:{self.port}: {e}"
            self._logger.write("ERROR", err)
            raise RconConnectionError(err) from e

    def close(self) -> None:
        """Closes the active RCON TCP socket if open."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None
                self._logger.write("DEBUG", f"Closed RCON socket for {self.host}:{self.port}.")

    def _transmit_command(self, command: str) -> str:
        cmd_id = self._next_request_id()
        probe_id = self._next_request_id()

        # Sending an empty SERVERDATA_RESPONSE_VALUE probe allows clean multi-packet reassembly
        self._send_packet(cmd_id, _SERVERDATA_EXECCOMMAND, command)
        self._send_packet(probe_id, _SERVERDATA_RESPONSE_VALUE, "")

        response_parts: list[str] = []
        while True:
            resp_id, _, payload = self._read_packet()
            if resp_id == probe_id:
                break
            if resp_id == cmd_id:
                response_parts.append(payload)

        return "".join(response_parts)

    def send_command(self, command: str) -> str:
        """
        Sends a single command to the Minecraft server via RCON and returns the response.
        Automatically manages connection lifecycle if called outside a context manager,
        and transparently reconnects once if an idle persistent socket was dropped.

        :param command: Minecraft server command string to execute.
        :return: Decoded text response returned by the server.
        :raises RconConnectionError: If command transmission fails.
        :raises RconAuthError: If authentication fails during connection.
        """
        clean_cmd = command.strip()
        if not clean_cmd:
            self._logger.write("DEBUG", "Ignoring empty RCON command.")
            return ""

        opened_temporarily = False
        if self._sock is None:
            self.connect()
            opened_temporarily = not self._in_context

        try:
            self._logger.write("CMD", f"-> {clean_cmd}")
            try:
                response = self._transmit_command(clean_cmd)
            except (OSError, RconConnectionError):
                # Reconnect once in case the server closed an idle socket during a long sleep()
                if not opened_temporarily:
                    self._logger.write("WARN", "RCON socket dropped; reconnecting and retrying command...")
                    self.close()
                    self.connect()
                    response = self._transmit_command(clean_cmd)
                else:
                    raise

            clean_resp = response.strip()
            self._logger.write("RESP", f"<- {clean_resp if clean_resp else '<empty>'}")
            return response
        except RconError:
            raise
        except OSError as e:
            err = f"Failed to execute RCON command '{clean_cmd}': {e}"
            self._logger.write("ERROR", err)
            raise RconConnectionError(err) from e
        finally:
            if opened_temporarily:
                self.close()

    def execute_script(
        self,
        script_filename: str | Path,
        blocked_commands: set[str] | None = None
    ) -> list[str]:
        """
        Reads a .txt command script from RCON_SCRIPTS_DIR and executes each line sequentially.
        Supports blank lines, '#' or '//' comments, and 'sleep(<seconds>)' pause directives.

        :param script_filename: Filename inside RCON_SCRIPTS_DIR (or explicit Path).
        :param blocked_commands: Optional set of base command names (without leading '/') to block.
        :return: List of non-empty server responses returned by executed commands.
        :raises RconScriptError: If the script file does not exist or has invalid sleep() syntax.
        :raises RconError: If RCON connection or command execution fails.
        """
        script_path = Path(script_filename)
        if not script_path.is_absolute():
            script_path = (self.scripts_dir / script_path).resolve()

        if not script_path.is_file():
            err = f"RCON script file not found: {script_path}"
            self._logger.write("ERROR", err)
            raise RconScriptError(err)

        normalized_blocked = {cmd.lstrip("/").lower() for cmd in (blocked_commands or set())}

        self._logger.write("INFO", f"Executing RCON script: {script_path.name}")
        log("INFO", f"Running RCON script '{script_path.name}' on {self.server_label} server...", module="RCON")

        lines = script_path.read_text(encoding="utf-8").splitlines()
        responses: list[str] = []

        was_connected = self._sock is not None
        if not was_connected:
            self.connect()

        try:
            for line_num, raw_line in enumerate(lines, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue

                if line.lower().startswith("sleep("):
                    match = _SLEEP_PATTERN.match(line)
                    if not match:
                        raise RconScriptError(
                            f"Invalid sleep() syntax in {script_path.name} at line {line_num}: '{line}'"
                        )
                    delay_seconds = float(match.group(1))
                    if delay_seconds <= 0:
                        raise RconScriptError(
                            f"Sleep duration must be greater than 0 in {script_path.name} at line {line_num}: '{line}'"
                        )

                    self._logger.write("SLEEP", f"Pausing RCON script for {delay_seconds:g}s (line {line_num})...")
                    time.sleep(delay_seconds)
                    continue

                base_cmd = line.lstrip("/").split()[0].lower()
                if base_cmd in normalized_blocked:
                    self._logger.write("WARN", f"[SAFEGUARD] Blocked command at line {line_num}: '{line}'")
                    log(
                        "WARN",
                        f"Blocked destructive command '{line}' on {self.server_label} server (safe/test mode).",
                        module="RCON"
                    )
                    continue

                resp = self.send_command(line)
                if resp.strip():
                    responses.append(resp.strip())
        finally:
            if not was_connected and not self._in_context:
                self.close()

        self._logger.write("SUCCESS", f"Finished RCON script: {script_path.name}")
        return responses

    def shutdown_countdown(self, test: bool = False) -> list[str]:
        """
        Executes the configured server shutdown countdown script from RCON_SCRIPTS_DIR.
        When test=True, all 'stop' commands are strictly blocked so the server stays online.

        :param test: If True, blocks server shutdown commands and broadcasts a test completion notice.
        :return: List of server responses collected during the countdown sequence.
        :raises RconScriptError: If the shutdown script is missing or malformed.
        :raises RconError: If RCON communication fails.
        """
        if not test:
            return self.execute_script(self.shutdown_script)

        self._logger.write("INFO", "Running shutdown countdown in SAFE TEST MODE (stop commands blocked).")
        with self:
            responses = self.execute_script(
                self.shutdown_script,
                blocked_commands={"stop", "minecraft:stop"}
            )
            notice_resp = self.send_command(
                "say [MCS-MS] [TEST] Odliczanie zakonczone - komenda STOP zostala zablokowana, serwer pozostaje online."
            )
            if notice_resp.strip():
                responses.append(notice_resp.strip())

        return responses