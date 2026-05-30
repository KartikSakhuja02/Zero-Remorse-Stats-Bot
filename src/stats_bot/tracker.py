from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class TrackerProfile:
    title_slug: str
    platform: str
    identifier: str
    display_name: str
    profile_url: str
    stats: dict[str, str]
    raw: dict[str, Any]


class TrackerClient:
    def __init__(self, api_key: str, title_slug: str, platform: str, base_url: str = "https://public-api.tracker.gg/v2") -> None:
        self.api_key = api_key
        self.title_slug = title_slug
        self.platform = platform
        self.base_url = base_url.rstrip("/")

    def build_profile_url(self, identifier: str, platform: str | None = None) -> str:
        resolved_platform = platform or self.platform
        return f"{self.base_url}/{self.title_slug}/standard/profile/{quote(resolved_platform, safe='')}/{quote(identifier, safe='')}"

    async def fetch_profile(self, identifier: str, platform: str | None = None) -> TrackerProfile:
        url = self.build_profile_url(identifier, platform)
        payload = await asyncio.to_thread(self._fetch_json, url)
        data = payload.get("data") or {}

        platform_info = data.get("platformInfo") or {}
        user_info = data.get("userInfo") or {}
        display_name = (
            platform_info.get("platformUserHandle")
            or user_info.get("username")
            or platform_info.get("platformUserIdentifier")
            or identifier
        )

        stats = self._extract_overview_stats(data)
        return TrackerProfile(
            title_slug=self.title_slug,
            platform=platform or self.platform,
            identifier=identifier,
            display_name=str(display_name),
            profile_url=url,
            stats=stats,
            raw=payload,
        )

    def _fetch_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "TRN-Api-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "Stats Bot",
            },
        )

        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
            raise RuntimeError(f"Tracker API error {exc.code}: {body or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"Tracker API request failed: {exc.reason}") from exc

    @staticmethod
    def _extract_overview_stats(payload: dict[str, Any]) -> dict[str, str]:
        segments = payload.get("segments") or []
        if not isinstance(segments, list):
            return {}

        overview_segment = next(
            (segment for segment in segments if isinstance(segment, dict) and segment.get("type") == "overview"),
            None,
        )
        if overview_segment is None:
            overview_segment = next((segment for segment in segments if isinstance(segment, dict)), None)
        if overview_segment is None:
            return {}

        stats = overview_segment.get("stats") or {}
        if not isinstance(stats, dict):
            return {}

        extracted: dict[str, str] = {}
        for key, stat in stats.items():
            if not isinstance(stat, dict):
                continue
            display_value = stat.get("displayValue")
            if display_value is not None:
                extracted[str(key)] = str(display_value)
                continue
            value = stat.get("value")
            if value is not None:
                extracted[str(key)] = str(value)
        return extracted