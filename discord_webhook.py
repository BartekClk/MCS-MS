import json
import os
import urllib.error
import urllib.request
import socket
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from github_release import ReleaseInfo
from logger import MCSLogger, log


class DiscordWebhookError(Exception):
    """Base exception for all Discord webhook configuration and delivery errors."""


class DiscordConfigError(DiscordWebhookError):
    """Raised when Discord webhook URL or template file in .env is missing or invalid."""


class DiscordDeliveryError(DiscordWebhookError):
    """Raised when Discord API rejects the webhook payload or a network error occurs."""


class DiscordWebhookClient:
    """
    Loads an editable JSON embed template from disk, substitutes GitHub release
    placeholders safely on parsed JSON values, and posts it to a Discord webhook.
    """

    def __init__(
        self,
        webhook_url: str,
        template_path: Path,
        logs_dir: Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not webhook_url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            raise DiscordConfigError("Invalid DISCORD_WEBHOOK_URL format.")
        if not template_path.is_file():
            raise DiscordConfigError(f"Discord template file not found: {template_path}")

        self.webhook_url = webhook_url
        self.template_path = template_path
        self.timeout = timeout

        resolved_logs = logs_dir or Path("./mcsms_data/logs").resolve()
        self._logger = MCSLogger(
            prefix="discord",
            logs_dir=resolved_logs,
            per_run=False,
            default_module="DISCORD",
        )

    @classmethod
    def from_env(
        cls,
        env_path: str | Path = ".env",
        timeout: float = 15.0,
    ) -> "DiscordWebhookClient":
        env_file = Path(env_path)
        if not env_file.exists():
            raise DiscordConfigError(f"Configuration file not found: {env_file.resolve()}")

        load_dotenv(dotenv_path=env_file)

        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        template_raw = os.getenv("DISCORD_TEMPLATE_PATH", "").strip()
        logs_dir_raw = os.getenv("LOGS_DIR", "").strip()

        if not all([webhook_url, template_raw, logs_dir_raw]):
            raise DiscordConfigError(
                "Required variables (DISCORD_WEBHOOK_URL, DISCORD_TEMPLATE_PATH, LOGS_DIR) are missing or empty in .env."
            )

        template_path = Path(template_raw).expanduser().resolve()
        logs_dir = Path(logs_dir_raw).expanduser().resolve()

        return cls(
            webhook_url=webhook_url,
            template_path=template_path,
            logs_dir=logs_dir,
            timeout=timeout,
        )

    @classmethod
    def for_admin_from_env(cls, env_path: str | Path = ".env", timeout: float = 15.0) -> "DiscordWebhookClient":
        env_file = Path(env_path)
        if not env_file.exists():
            raise DiscordConfigError(f"Configuration file not found: {env_file.resolve()}")

        load_dotenv(dotenv_path=env_file)
        webhook_url = os.getenv("DISCORD_ADMIN_WEBHOOK_URL", "").strip()
        template_raw = os.getenv("DISCORD_ADMIN_TEMPLATE_PATH", "").strip()
        logs_dir_raw = os.getenv("LOGS_DIR", "").strip()

        if not all([webhook_url, template_raw, logs_dir_raw]):
            raise DiscordConfigError("Missing DISCORD_ADMIN_WEBHOOK_URL, DISCORD_ADMIN_TEMPLATE_PATH, or LOGS_DIR in .env.")

        return cls(
            webhook_url=webhook_url,
            template_path=Path(template_raw).expanduser().resolve(),
            logs_dir=Path(logs_dir_raw).expanduser().resolve(),
            timeout=timeout,
        )

    @staticmethod
    def _render_node(node: Any, variables: dict[str, str], current_key: str = "") -> Any:
        if isinstance(node, str):
            rendered = node
            for placeholder, value in variables.items():
                rendered = rendered.replace(placeholder, value)

            # Enforce Discord Embed field value (1024 chars) and title (256 chars) limits
            if current_key == "value" and len(rendered) > 1024:
                rendered = rendered[:1021].rstrip() + "..."
            elif current_key == "title" and len(rendered) > 256:
                rendered = rendered[:253].rstrip() + "..."
            return rendered

        if isinstance(node, list):
            return [DiscordWebhookClient._render_node(item, variables, current_key) for item in node]

        if isinstance(node, dict):
            return {
                k: DiscordWebhookClient._render_node(v, variables, k)
                for k, v in node.items()
            }

        return node

    def build_payload(self, release: ReleaseInfo) -> dict[str, Any]:
        try:
            raw_template = json.loads(self.template_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise DiscordConfigError(f"Failed to parse Discord JSON template '{self.template_path}': {e}") from e

        changelog = release.body.strip() if release.body and release.body.strip() else "Brak opisu zmian."
        variables = {
            "%pack_version%": release.tag_name,
            "%release_name%": release.name,
            "%changelog%": changelog,
            "%release_url%": release.html_url,
            "%published_at%": release.published_at,
        }

        payload = self._render_node(raw_template, variables)
        if isinstance(payload, dict) and not payload.get("avatar_url"):
            payload.pop("avatar_url", None)

        return payload

    def send_release_notification(self, release: ReleaseInfo) -> None:
        payload = self.build_payload(release)
        encoded_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            self.webhook_url,
            data=encoded_body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "MCS-MS-DiscordNotifier/1.0",
            },
            method="POST",
        )

        log("INFO", f"Sending Discord webhook notification for release '{release.tag_name}'...", module="DISCORD")
        self._logger.write("INFO", f"Posting release '{release.tag_name}' embed to Discord webhook...")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.getcode()
                if status not in (200, 204):
                    raise DiscordDeliveryError(f"Unexpected HTTP status from Discord webhook: {status}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            msg = f"Discord webhook HTTP {e.code}: {detail}"
            self._logger.write("ERROR", msg)
            raise DiscordDeliveryError(msg) from e
        except urllib.error.URLError as e:
            msg = f"Network error while sending Discord webhook: {e.reason}"
            self._logger.write("ERROR", msg)
            raise DiscordDeliveryError(msg) from e

        self._logger.write("SUCCESS", f"Discord notification delivered for release '{release.tag_name}'.")
        log("SUCCESS", "Discord release notification sent successfully.", module="DISCORD")

    def send_admin_report(
        self,
        command: str,
        success: bool,
        exit_code: int,
        duration_sec: float,
        details: str,
        attachment_path: Path | None = None,
    ) -> None:
        raw_template = json.loads(self.template_path.read_text(encoding="utf-8"))
        variables = {
            "%status_icon%": "✅" if success else "❌",
            "%status_text%": "SUCCESS" if success else "ERROR",
            "%command%": command,
            "%exit_code%": str(exit_code),
            "%duration%": f"{duration_sec:.1f}s",
            "%details%": details.strip() or "Brak dodatkowych szczegółów.",
            "%hostname%": socket.gethostname(),
        }

        payload = self._render_node(raw_template, variables)
        if isinstance(payload, dict) and payload.get("embeds"):
            payload["embeds"][0]["color"] = 3066993 if success else 15158332

        json_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        if attachment_path and attachment_path.is_file():
            boundary = f"----MCSMSBoundary{uuid.uuid4().hex}"
            body_parts = [
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="payload_json"\r\n'
                "Content-Type: application/json; charset=utf-8\r\n\r\n".encode("utf-8"),
                json_bytes,
                f"\r\n--{boundary}\r\n"
                f'Content-Disposition: form-data; name="files[0]"; filename="{attachment_path.name}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n".encode("utf-8"),
                attachment_path.read_bytes(),
                f"\r\n--{boundary}--\r\n".encode("utf-8"),
            ]
            data = b"".join(body_parts)
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            data = json_bytes
            content_type = "application/json; charset=utf-8"

        req = urllib.request.Request(
            self.webhook_url,
            data=data,
            headers={"Content-Type": content_type, "User-Agent": "MCS-MS-AdminNotifier/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.getcode() not in (200, 204):
                raise DiscordDeliveryError(f"Unexpected HTTP status from admin webhook: {resp.getcode()}")