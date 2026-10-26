"""Runtime configuration for OpenZap.

Everything is resolved from environment variables so the same binary works on
a laptop, in a Docker container or on a VPS. The data directory (``OPENZAP_HOME``)
holds workflow JSON files, the SQLite database and any files written by the
``write_file`` / ``log`` actions.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import Any


def _truthy(value: str | None) -> bool:
    """Interpret common boolean spellings used in ``.env`` files."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer value {value!r}") from exc


@dataclass(frozen=True)
class SmtpConfig:
    """SMTP settings used by the ``email`` action."""

    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = "openzap@localhost"
    starttls: bool = True
    ssl: bool = False
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        """Whether the ``email`` action has anything to connect to."""
        return bool(self.host)


@dataclass
class Settings:
    """Resolved configuration for one OpenZap process."""

    home: pathlib.Path = field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OPENZAP_HOME", "~/.openzap")
        ).expanduser()
    )

    @property
    def workflows_dir(self) -> pathlib.Path:
        return self.home / "workflows"

    @property
    def logs_dir(self) -> pathlib.Path:
        return self.home / "logs"

    @property
    def db_path(self) -> pathlib.Path:
        return pathlib.Path(os.environ.get("OPENZAP_DB", str(self.home / "openzap.db")))

    @property
    def base_url(self) -> str:
        """Public base URL advertised for webhook endpoints."""
        return os.environ.get("OPENZAP_BASE_URL", "").rstrip("/")

    @property
    def host(self) -> str:
        return os.environ.get("OPENZAP_HOST", "127.0.0.1")

    @property
    def port(self) -> int:
        return _int(os.environ.get("OPENZAP_PORT"), 8080)

    @property
    def max_body_bytes(self) -> int:
        return _int(os.environ.get("OPENZAP_MAX_BODY_BYTES"), 1024 * 1024)

    @property
    def smtp(self) -> SmtpConfig:
        return SmtpConfig(
            host=os.environ.get("SMTP_HOST", ""),
            port=_int(os.environ.get("SMTP_PORT"), 587),
            user=os.environ.get("SMTP_USER", ""),
            password=os.environ.get("SMTP_PASS", ""),
            sender=os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "openzap@localhost",
            starttls=_truthy(os.environ.get("SMTP_STARTTLS", "true")),
            ssl=_truthy(os.environ.get("SMTP_SSL")),
            timeout=float(_int(os.environ.get("SMTP_TIMEOUT"), 30)),
        )

    def webhook_base(self) -> str:
        """Base URL used to print webhook URLs; falls back to localhost."""
        return self.base_url or f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        """Create the data directory layout if it does not exist yet."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def env_subset(self) -> dict[str, str]:
        """Subset of the environment that templates may reference as ``{{ env.* }}``.

        Only ``OPENZAP_*`` and ``SMTP_*`` variables are exposed, so arbitrary
        process environment noise (``PATH``, ``HOME``, ...) is never reachable
        from a workflow template.
        """
        return {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("OPENZAP_", "SMTP_"))
        }

    def as_dict(self) -> dict[str, Any]:
        """Safe, secret-free representation for diagnostic output."""
        return {
            "home": str(self.home),
            "workflows_dir": str(self.workflows_dir),
            "logs_dir": str(self.logs_dir),
            "db_path": str(self.db_path),
            "webhook_base": self.webhook_base(),
            "smtp_configured": self.smtp.configured,
        }


def load_settings(home: str | pathlib.Path | None = None) -> Settings:
    """Build :class:`Settings`, optionally overriding the data directory."""
    if home is None:
        return Settings()
    return Settings(home=pathlib.Path(home).expanduser())
