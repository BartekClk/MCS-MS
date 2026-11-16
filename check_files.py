import os
from pathlib import Path
from dotenv import load_dotenv
from logger import log


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

    log("SUCCESS", "Environment configuration and paths verified.", module="CHECK_FILES")

    return {
        "base_dir": base_dir,
        "logs_dir": logs_dir,
        "server_dir": server_dir,
        "world_dir": world_dir,
        "token_path": token_path,
    }