import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from logger import MCSLogger, log

_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_API_BASE = "https://api.github.com"


class GitHubReleaseError(Exception):
    """Base exception for all GitHub release query and download errors."""


class GitHubConfigError(GitHubReleaseError):
    """Raised when required GitHub settings in .env are missing or malformed."""


class GitHubApiError(GitHubReleaseError):
    """Raised when the GitHub REST API returns an HTTP or network error."""


class GitHubAssetNotFoundError(GitHubReleaseError):
    """Raised when a requested asset is not present in the latest GitHub release."""


@dataclass(frozen=True)
class ReleaseAsset:
    """Represents a single downloadable file attached to a GitHub release."""

    id: int
    name: str
    size: int
    content_type: str
    api_url: str
    browser_download_url: str


@dataclass(frozen=True)
class ReleaseInfo:
    """Structured metadata describing the latest published GitHub release."""

    tag_name: str
    name: str
    published_at: str
    html_url: str
    body: str
    prerelease: bool
    assets: list[ReleaseAsset]


class GitHubReleaseClient:
    """
    Queries the GitHub REST API for the latest release of a modpack repository,
    tracks the currently deployed version on disk, and downloads release assets.
    """

    def __init__(
        self,
        repo: str,
        state_file: Path,
        token: str = "",
        logs_dir: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not _REPO_PATTERN.match(repo):
            raise GitHubConfigError(f"Invalid GitHub repository format '{repo}'. Expected 'owner/repo'.")

        self.repo = repo
        self.state_file = state_file
        self.token = token.strip()
        self.timeout = timeout

        resolved_logs = logs_dir or Path("./mcsms_data/logs").resolve()
        self._logger = MCSLogger(
            prefix="github",
            logs_dir=resolved_logs,
            per_run=False,
            default_module="GITHUB",
        )

    @classmethod
    def from_env(
        cls,
        env_path: str | Path = ".env",
        timeout: float = 15.0,
    ) -> "GitHubReleaseClient":
        """
        Instantiates GitHubReleaseClient strictly from .env variables without hidden fallbacks.

        :param env_path: Path to the .env configuration file.
        :param timeout: HTTP request timeout in seconds.
        :return: Configured GitHubReleaseClient instance.
        :raises GitHubConfigError: If .env or any required GitHub variable is missing or invalid.
        """
        env_file = Path(env_path)
        if not env_file.exists():
            raise GitHubConfigError(f"Configuration file not found: {env_file.resolve()}")

        load_dotenv(dotenv_path=env_file)

        repo = os.getenv("GITHUB_REPO", "").strip()
        token = os.getenv("GITHUB_TOKEN", "").strip()
        state_file_raw = os.getenv("RELEASE_STATE_FILE", "").strip()
        logs_dir_raw = os.getenv("LOGS_DIR", "").strip()

        if not all([repo, state_file_raw, logs_dir_raw]):
            raise GitHubConfigError(
                "Required variables (GITHUB_REPO, RELEASE_STATE_FILE, LOGS_DIR) are missing or empty in .env."
            )

        if not _REPO_PATTERN.match(repo):
            raise GitHubConfigError(f"Invalid GITHUB_REPO '{repo}' in .env (expected 'owner/repo').")

        state_file = Path(state_file_raw).expanduser().resolve()
        logs_dir = Path(logs_dir_raw).expanduser().resolve()

        return cls(
            repo=repo,
            state_file=state_file,
            token=token,
            logs_dir=logs_dir,
            timeout=timeout,
        )

    def _build_headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "MCS-MS-ReleaseTracker/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get_latest_release(self) -> ReleaseInfo:
        """
        Fetches metadata for the latest non-draft, non-prerelease release from GitHub.

        :return: Populated ReleaseInfo dataclass for the latest release.
        :raises GitHubApiError: If the repository/release does not exist, auth fails, or network errors occur.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self.repo}/releases/latest"
        self._logger.write("INFO", f"Querying latest release for {self.repo} from GitHub API...")

        req = urllib.request.Request(url, headers=self._build_headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err_body = json.loads(e.read().decode("utf-8", errors="replace"))
                detail = err_body.get("message", "")
            except Exception:
                detail = e.reason

            if e.code == 404:
                msg = (
                    f"Latest release not found for '{self.repo}' (HTTP 404: {detail}). "
                    "Verify the repo exists, has at least one published release, and GITHUB_TOKEN is set if private."
                )
            else:
                msg = f"GitHub API HTTP {e.code} while fetching release for '{self.repo}': {detail}"

            self._logger.write("ERROR", msg)
            raise GitHubApiError(msg) from e
        except urllib.error.URLError as e:
            msg = f"Network error while contacting GitHub API for '{self.repo}': {e.reason}"
            self._logger.write("ERROR", msg)
            raise GitHubApiError(msg) from e

        try:
            data = json.loads(raw_payload)
            tag_name = str(data["tag_name"]).strip()
            if not tag_name:
                raise KeyError("Empty tag_name in GitHub API response")

            assets = [
                ReleaseAsset(
                    id=int(item["id"]),
                    name=str(item["name"]),
                    size=int(item["size"]),
                    content_type=str(item.get("content_type", "application/octet-stream")),
                    api_url=str(item["url"]),
                    browser_download_url=str(item["browser_download_url"]),
                )
                for item in data.get("assets", [])
            ]

            release = ReleaseInfo(
                tag_name=tag_name,
                name=str(data.get("name") or tag_name).strip(),
                published_at=str(data.get("published_at", "")),
                html_url=str(data.get("html_url", "")),
                body=str(data.get("body") or ""),
                prerelease=bool(data.get("prerelease", False)),
                assets=assets,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            msg = f"Malformed JSON payload received from GitHub API for '{self.repo}': {e}"
            self._logger.write("ERROR", msg)
            raise GitHubApiError(msg) from e

        self._logger.write(
            "SUCCESS",
            f"Fetched latest release '{release.tag_name}' ({release.name}, published {release.published_at}).",
        )
        log("INFO", f"Latest GitHub release for {self.repo}: {release.tag_name}", module="GITHUB")
        return release

    def get_installed_version(self) -> str | None:
        """
        Reads the currently deployed release tag from RELEASE_STATE_FILE, if recorded.

        :return: Installed tag string (e.g. 'v1.2.0') or None if no deployment has been recorded yet.
        """
        if not self.state_file.is_file():
            return None

        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            tag = str(data.get("tag_name", "")).strip()
            return tag if tag else None
        except (OSError, json.JSONDecodeError) as e:
            self._logger.write("WARN", f"Could not parse release state file {self.state_file}: {e}")
            return None

    def save_installed_release(self, release: ReleaseInfo) -> None:
        """
        Persists metadata of the successfully deployed release to RELEASE_STATE_FILE.

        :param release: ReleaseInfo object representing the version now running on main.
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "repo": self.repo,
            "tag_name": release.tag_name,
            "name": release.name,
            "published_at": release.published_at,
            "html_url": release.html_url,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "assets": [asdict(a) for a in release.assets],
        }
        self.state_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        self._logger.write("INFO", f"Saved deployed release '{release.tag_name}' to {self.state_file}.")

    def check_for_update(self) -> tuple[bool, ReleaseInfo]:
        """
        Compares the latest GitHub release tag against the locally recorded version.

        :return: Tuple of (is_newer_available, latest_release_info).
        :raises GitHubApiError: If querying the GitHub API fails.
        """
        latest = self.get_latest_release()
        installed = self.get_installed_version()

        if installed is None:
            self._logger.write("INFO", f"No local release state recorded; update needed -> {latest.tag_name}.")
            return True, latest

        if installed != latest.tag_name:
            self._logger.write("INFO", f"New release detected: installed={installed}, latest={latest.tag_name}.")
            return True, latest

        self._logger.write("INFO", f"Server is already on the latest release ({installed}).")
        return False, latest

    def download_asset(
        self,
        release: ReleaseInfo,
        dest_dir: str | Path,
        asset_name_pattern: str | None = None,
    ) -> Path:
        """
        Downloads a release asset from GitHub using the authenticated API endpoint
        (supports both public and private repositories).

        :param release: ReleaseInfo containing the list of release assets.
        :param dest_dir: Target directory where the downloaded file will be saved.
        :param asset_name_pattern: Optional regex or exact filename to match a specific asset.
                                   If omitted, selects the first attached asset.
        :return: Path to the downloaded file on disk.
        :raises GitHubAssetNotFoundError: If no matching asset exists in the release.
        :raises GitHubApiError: If the HTTP download fails.
        """
        if not release.assets:
            raise GitHubAssetNotFoundError(
                f"Release '{release.tag_name}' in '{self.repo}' has no downloadable assets attached."
            )

        selected: ReleaseAsset | None = None
        if asset_name_pattern is None:
            selected = release.assets[0]
        else:
            compiled = re.compile(asset_name_pattern)
            for asset in release.assets:
                if asset.name == asset_name_pattern or compiled.search(asset.name):
                    selected = asset
                    break

        if selected is None:
            available = ", ".join(a.name for a in release.assets)
            raise GitHubAssetNotFoundError(
                f"No asset matching '{asset_name_pattern}' in release '{release.tag_name}'. Available: [{available}]"
            )

        target_dir = Path(dest_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / selected.name

        log("INFO", f"Downloading release asset '{selected.name}' ({selected.size} B)...", module="GITHUB")
        self._logger.write("INFO", f"Downloading asset '{selected.name}' from {selected.api_url} -> {output_path}")

        req = urllib.request.Request(
            selected.api_url,
            headers=self._build_headers(accept="application/octet-stream"),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60.0)) as resp, output_path.open("wb") as out:
                shutil.copyfileobj(resp, out)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            msg = f"Failed to download asset '{selected.name}' from release '{release.tag_name}': {e}"
            self._logger.write("ERROR", msg)
            raise GitHubApiError(msg) from e

        self._logger.write("SUCCESS", f"Downloaded release asset to {output_path}.")
        log("SUCCESS", f"Asset '{selected.name}' downloaded successfully.", module="GITHUB")
        return output_path


def fetch_latest_release(env_path: str | Path = ".env") -> ReleaseInfo:
    """
    Convenience entry point to fetch the latest GitHub release metadata configured in .env.

    :param env_path: Path to the .env configuration file.
    :return: ReleaseInfo for the latest release.
    :raises GitHubReleaseError: If configuration or GitHub API query fails.
    """
    client = GitHubReleaseClient.from_env(env_path=env_path)
    return client.get_latest_release()