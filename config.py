"""Environment-based application configuration."""

import os
from pathlib import Path


def _load_local_env() -> None:
    """Load simple KEY=VALUE entries from the project-local .env file."""

    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return

    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name:
            os.environ.setdefault(name, value)


def _as_bool(value: str | None, default: bool = False) -> bool:
    """Convert common environment representations to a boolean."""

    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


_load_local_env()


class Config:
    """Application settings sourced from environment variables."""

    GITLAB_URL = os.getenv("GITLAB_URL")
    GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
    RULES_DIRECTORY = os.getenv("RULES_DIRECTORY", "rules")

    FLASK_ENV = os.getenv("FLASK_ENV", "development")
    DEBUG = _as_bool(os.getenv("FLASK_DEBUG"), default=False)
    PORT = int(os.getenv("PORT", "5000"))
