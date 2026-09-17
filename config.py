"""Environment-based application configuration."""

import os
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool = False) -> bool:
    """Convert common environment representations to a boolean."""

    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


load_dotenv(Path(__file__).resolve().parent / ".env")


class Config:
    """Application settings sourced from environment variables."""

    GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.com")
    GITLAB_TOKEN = os.getenv("GITLAB_TOKEN")
    GITHUB_MODELS_TOKEN = os.getenv("GITHUB_MODELS_TOKEN")
    GITHUB_MODELS_ENDPOINT = os.getenv(
        "GITHUB_MODELS_ENDPOINT",
        "https://models.github.ai/inference",
    )
    GITHUB_MODELS_MODEL = os.getenv("GITHUB_MODELS_MODEL", "openai/gpt-4o-mini")
    REVIEW_RULES_FILE = os.getenv("REVIEW_RULES_FILE", "review_rules.yaml")

    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    DEBUG = _as_bool(os.getenv("FLASK_DEBUG"), default=False)
    FLASK_DEBUG = DEBUG
    FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
    SECRET_KEY = FLASK_SECRET_KEY
