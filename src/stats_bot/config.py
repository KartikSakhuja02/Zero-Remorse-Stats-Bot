from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    guild_id: int | None
    database_url: str
    log_level: str
    profile_submission_channel_id: int | None
    profile_stats_channel_id: int | None
    profile_screenshot_dir: Path
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_base_url: str
    openrouter_app_name: str
    openrouter_site_url: str | None


def load_settings() -> Settings:
    _load_dotenv_file(Path(".env"))

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")

    guild_id_raw = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id = int(guild_id_raw) if guild_id_raw else None

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    profile_submission_channel_id_raw = os.getenv("PROFILE_SUBMISSION_CHANNEL_ID", "").strip()
    profile_submission_channel_id = int(profile_submission_channel_id_raw) if profile_submission_channel_id_raw else None

    profile_stats_channel_id_raw = os.getenv("PROFILE_STATS_CHANNEL_ID", "").strip()
    profile_stats_channel_id = int(profile_stats_channel_id_raw) if profile_stats_channel_id_raw else None

    profile_screenshot_dir = Path(os.getenv("PROFILE_SCREENSHOT_DIR", "data/profile_screenshots")).expanduser()

    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None
    openrouter_model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"
    openrouter_base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip() or "https://openrouter.ai/api/v1"
    openrouter_app_name = os.getenv("OPENROUTER_APP_NAME", "Stats Bot").strip() or "Stats Bot"
    openrouter_site_url = os.getenv("OPENROUTER_SITE_URL", "").strip() or None

    if profile_submission_channel_id is not None and not openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required when PROFILE_SUBMISSION_CHANNEL_ID is set")

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return Settings(
        token=token,
        guild_id=guild_id,
        database_url=database_url,
        log_level=log_level,
        profile_submission_channel_id=profile_submission_channel_id,
        profile_stats_channel_id=profile_stats_channel_id,
        profile_screenshot_dir=profile_screenshot_dir,
        openrouter_api_key=openrouter_api_key,
        openrouter_model=openrouter_model,
        openrouter_base_url=openrouter_base_url,
        openrouter_app_name=openrouter_app_name,
        openrouter_site_url=openrouter_site_url,
    )


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
