"""Paths and environment settings. `.env` at the repo root is loaded on import."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
ENV_PATH = REPO_ROOT / ".env"

DEFAULT_LLM_MODEL = "claude-sonnet-5-5"

load_dotenv(ENV_PATH, override=False, encoding="utf-8")

_WHERE_TO_GET = {
    "ANTHROPIC_API_KEY": "Get a key at https://console.anthropic.com/settings/keys",
    "TAVILY_API_KEY": "Get a key at https://app.tavily.com (only needed for `leadership collect`)",
    "SEC_USER_AGENT": (
        'Set it to your name and contact email, e.g. "Jane Doe jane@example.com" '
        "(SEC policy: https://www.sec.gov/os/accessing-edgar-data). "
        "Only needed for `leadership collect`"
    ),
}


class MissingSettingError(RuntimeError):
    """A required environment variable is not set."""

    def __init__(self, name: str) -> None:
        self.name = name
        hint = _WHERE_TO_GET.get(name, "")
        super().__init__(
            f"Missing {name}. Add it to {ENV_PATH} (copy .env.example to .env). {hint}".strip()
        )


def get_setting(name: str) -> str | None:
    """Return the env var's value, or None if unset or blank."""
    value = os.getenv(name, "").strip()
    return value or None


def require_setting(name: str) -> str:
    value = get_setting(name)
    if value is None:
        raise MissingSettingError(name)
    return value


def llm_model() -> str:
    return get_setting("LLM_MODEL") or DEFAULT_LLM_MODEL


COMPANIES_PATH = DATA_DIR / "companies.yaml"
COMPANY_SETTING_TYPES = {"name": str, "ticker": str, "private": bool, "max_pages": int}


def company_settings(domain: str, path: Path = COMPANIES_PATH) -> dict[str, Any]:
    """Collection settings for `domain` from data/companies.yaml ({} if none)."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of domain -> settings")
    for company, settings in data.items():  # check every company, not just the one asked for
        if not isinstance(settings, dict):
            raise ValueError(f"{path}: settings for {company} must be a mapping")
        for key, value in settings.items():
            want = COMPANY_SETTING_TYPES.get(key)
            if want is None or type(value) is not want:
                raise ValueError(f"{path}: bad setting {key}={value!r} for {company}")
    return dict(data.get(domain) or {})


def company_dir(domain: str) -> Path:
    return DATA_DIR / domain


def db_path(domain: str) -> Path:
    return company_dir(domain) / "leadership.db"
