import os
from pathlib import Path
from dotenv import load_dotenv
from logger import log
import re


def _require_env_str(key: str) -> str:
    """Reads a required non-empty string variable from the environment."""
    value = os.getenv(key, "").strip()
    if not value:
        msg = f"Missing or empty required variable in .env: {key}"
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg)
    return value


def _require_env_path(key: str) -> Path:
    """Reads a required path variable from the environment and resolves it."""
    raw_path = _require_env_str(key)
    return Path(raw_path).expanduser().resolve()


def verify_and_create_paths(env_path: str | Path = ".env") -> dict[str, Path]:
    """
    Loads environment variables from the .env file, strictly validates all required paths
    and settings, and creates missing internal directories (such as BASE_DIR and LOGS_DIR).

    :param env_path: Path to the .env configuration file.
    :return: Dictionary mapping logical path names to resolved Path objects.
    :raises FileNotFoundError: If the .env file is missing.
    :raises ValueError: If any required configuration key in .env is missing or empty.
    """
    env_file = Path(env_path)
    if not env_file.exists():
        msg = f"Configuration file not found: {env_file.resolve()}"
        log("ERROR", msg, module="CHECK_FILES")
        raise FileNotFoundError(msg)

    load_dotenv(dotenv_path=env_file)

    # Strictly require all paths and key parameters from .env
    base_dir = _require_env_path("BASE_DIR")
    logs_dir = _require_env_path("LOGS_DIR")
    server_dir = _require_env_path("SERVER_DIR")
    world_folder_name = _require_env_str("WORLD_FOLDER_NAME")
    world_dir = (server_dir / world_folder_name).resolve()
    token_path = _require_env_path("GDRIVE_TOKEN_PATH")
    _require_env_str("GDRIVE_FOLDER_ID")

    for directory in (base_dir, logs_dir):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            log("INIT", f"Created directory: {directory}", module="CHECK_FILES")

    if not server_dir.exists():
        log("WARN", f"Server root directory does not exist yet: {server_dir}", module="CHECK_FILES")
    elif not world_dir.exists():
        log("WARN", f"World directory does not exist yet: {world_dir}", module="CHECK_FILES")

    if not token_path.exists():
        log("WARN", f"Google OAuth2 token file not found at: {token_path}", module="CHECK_FILES")

    retention_raw = _require_env_str("BACKUP_RETENTION_DAYS")
    try:
        if int(retention_raw) < 0:
            raise ValueError("Must be >= 0")
    except ValueError as e:
        msg = f"Invalid BACKUP_RETENTION_DAYS in .env (expected integer >= 0): {retention_raw}"
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg) from e

    # Validate RCON configuration for Main and Canary servers
    for prefix in ("MAIN_RCON", "CANARY_RCON"):
        _require_env_str(f"{prefix}_HOST")
        _require_env_str(f"{prefix}_PASSWORD")
        port_raw = _require_env_str(f"{prefix}_PORT")
        try:
            port_val = int(port_raw)
            if not (1 <= port_val <= 65535):
                raise ValueError("Port out of valid TCP range 1-65535")
        except ValueError as e:
            msg = f"Invalid {prefix}_PORT in .env (expected integer 1-65535): {port_raw}"
            log("ERROR", msg, module="CHECK_FILES")
            raise ValueError(msg) from e

    # Validate Canary server & Packwiz updater configuration
    canary_dir = _require_env_path("CANARY_SERVER_DIR")
    if not canary_dir.is_dir():
        msg = f"CANARY_SERVER_DIR does not exist or is not a directory: {canary_dir}"
        log("ERROR", msg, module="CHECK_FILES")
        raise FileNotFoundError(msg)

    for key in (
        "MAIN_SCREEN_SESSION",
        "CANARY_SCREEN_SESSION",
        "MAIN_START_CMD",
        "CANARY_START_CMD",
        "JAVA_BIN",
        "PACKWIZ_BOOTSTRAP_JAR",
        "PACKWIZ_PACK_URL",
    ):
        _require_env_str(key)

    for timeout_key in ("SERVER_STOP_TIMEOUT", "CANARY_BOOT_TIMEOUT", "MAIN_BOOT_TIMEOUT"):
        raw_val = _require_env_str(timeout_key)
        try:
            if int(raw_val) <= 0:
                raise ValueError("Must be greater than 0")
        except ValueError as e:
            msg = f"Invalid {timeout_key} in .env (expected positive integer): {raw_val}"
            log("ERROR", msg, module="CHECK_FILES")
            raise ValueError(msg) from e

    stop_after_raw = _require_env_str("CANARY_STOP_AFTER_TEST").lower()
    if stop_after_raw not in ("true", "false"):
        msg = f"Invalid CANARY_STOP_AFTER_TEST in .env (expected true/false): {stop_after_raw}"
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg)

    rcon_scripts_dir = _require_env_path("RCON_SCRIPTS_DIR")
    rcon_scripts_dir.mkdir(parents=True, exist_ok=True)
    _require_env_str("RCON_SHUTDOWN_SCRIPT")

    # Validate GitHub Release Tracker configuration
    github_repo = _require_env_str("GITHUB_REPO")
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", github_repo):
        msg = f"Invalid GITHUB_REPO in .env (expected 'owner/repo' format): {github_repo}"
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg)

    release_state_file = _require_env_path("RELEASE_STATE_FILE")
    release_state_file.parent.mkdir(parents=True, exist_ok=True)

    # Validate MOTD template file
    motd_template_path = _require_env_path("MOTD_TEMPLATE_PATH")
    if not motd_template_path.is_file():
        msg = f"MOTD_TEMPLATE_PATH does not exist or is not a file: {motd_template_path}"
        log("ERROR", msg, module="CHECK_FILES")
        raise FileNotFoundError(msg)

    # Validate Discord Webhooks configuration
    discord_webhook_url = _require_env_str("DISCORD_WEBHOOK_URL")
    if not discord_webhook_url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        msg = "Invalid DISCORD_WEBHOOK_URL in .env (expected https://discord.com/api/webhooks/...)."
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg)

    discord_template_path = _require_env_path("DISCORD_TEMPLATE_PATH")
    if not discord_template_path.is_file():
        msg = f"DISCORD_TEMPLATE_PATH does not exist or is not a file: {discord_template_path}"
        log("ERROR", msg, module="CHECK_FILES")
        raise FileNotFoundError(msg)
    
    admin_webhook_url = _require_env_str("DISCORD_ADMIN_WEBHOOK_URL")
    if not admin_webhook_url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
        msg = "Invalid DISCORD_ADMIN_WEBHOOK_URL in .env."
        log("ERROR", msg, module="CHECK_FILES")
        raise ValueError(msg)

    admin_template_path = _require_env_path("DISCORD_ADMIN_TEMPLATE_PATH")
    if not admin_template_path.is_file():
        msg = f"DISCORD_ADMIN_TEMPLATE_PATH does not exist or is not a file: {admin_template_path}"
        log("ERROR", msg, module="CHECK_FILES")
        raise FileNotFoundError(msg)

    log("SUCCESS", "Environment configuration and paths verified.", module="CHECK_FILES")

    return {
        "base_dir": base_dir,
        "logs_dir": logs_dir,
        "server_dir": server_dir,
        "canary_server_dir": canary_dir,
        "world_dir": world_dir,
        "token_path": token_path,
        "rcon_scripts_dir": rcon_scripts_dir,
        "release_state_file": release_state_file,
        "motd_template_path": motd_template_path,
        "discord_template_path": discord_template_path,
        "discord_admin_template_path": admin_template_path,
    }