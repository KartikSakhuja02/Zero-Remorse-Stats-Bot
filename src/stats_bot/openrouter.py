from __future__ import annotations

import base64
import json
import re

import aiohttp


class OpenRouterOCRClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_name: str = "Stats Bot",
        site_url: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.app_name = app_name
        self.site_url = site_url

    async def extract_player_name(self, image_bytes: bytes, mime_type: str = "image/png") -> str:
        content = await self._chat_completion_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            system_prompt=(
                "You are OCR for a game profile screenshot. "
                "Extract only the player profile name. "
                "Return only the exact name and nothing else."
            ),
            user_prompt="Extract the profile name from this screenshot.",
        )
        return self._clean_extracted_text(content)

    async def extract_kills_for_player(self, image_bytes: bytes, player_name: str, mime_type: str = "image/png") -> int:
        content = await self._chat_completion_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            system_prompt=(
                "You are OCR for a Rainbow Six Mobile match screenshot. "
                "The user already gave you the registered player IGN. "
                "Find that player in the image and return only the kills number from that row. "
                "Return only the number and nothing else."
            ),
            user_prompt=f"Find the row for {player_name} and return only the kills value.",
        )
        cleaned = self._clean_extracted_text(content)
        match = re.search(r"-?\d+", cleaned)
        if match is None:
            raise RuntimeError(f"Could not extract kills from OCR output: {content}")

        return int(match.group(0))

    async def extract_match_rows(
        self,
        image_bytes: bytes,
        kills_symbol_label: str = "kills",
        score_symbol_label: str = "score",
        mime_type: str = "image/png",
    ) -> list[dict]:
        """
        Extracts scoreboard rows from a Rainbow Six Mobile match screenshot.

        The method asks the OCR model to return a JSON array of objects with the
        fields: team ('your'|'enemy'), player (string), kills (int), score (int).
        This allows downstream code to pick kills under the kills-symbol and
        scores under the score-symbol and compute MVPs by highest score.
        """
        system_prompt = (
            "You are OCR for a Rainbow Six Mobile match screenshot.\n"
            "Locate the scoreboard table and return ONLY a JSON array (no extra text). "
            "Each item must have the keys: 'team' (either 'your' or 'enemy'), 'player' (string), 'kills' (int), 'score' (int).\n"
            "Use numeric values for kills and score. Do not include any other keys.\n"
            "If you are unsure about a value, return null for that field. "
        )

        user_prompt = (
            f"Identify the player rows and the numeric values under the columns for the {kills_symbol_label} and {score_symbol_label} symbols. "
            "Output a JSON array like: [{" + "\"team\": \"your\", \"player\": \"Name\", \"kills\": 8, \"score\": 3235}, ...]."
        )

        content = await self._chat_completion_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

        cleaned = self._clean_extracted_text(content)

        # Some models may still include trailing commentary; attempt to find JSON
        try:
            obj_start = cleaned.index("[")
            obj_text = cleaned[obj_start:]
        except ValueError:
            obj_text = cleaned

        try:
            data = json.loads(obj_text)
        except Exception:
            # As a fallback, try to extract lines like: Player | kills | score
            rows: list[dict] = []
            for line in obj_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                # try to capture: name <sep> kills <sep> score
                parts = re.split(r"\s*[\|,;]\s*|\s{2,}", line)
                if len(parts) >= 3:
                    name = parts[0].strip()
                    kills_match = re.search(r"-?\d+", parts[1])
                    score_match = re.search(r"-?\d+", parts[2])
                    try:
                        kills = int(kills_match.group(0)) if kills_match else None
                        score = int(score_match.group(0)) if score_match else None
                    except Exception:
                        kills = None
                        score = None
                    # Heuristics: assume left column is 'your' until we see a separator
                    rows.append({"team": "your", "player": name, "kills": kills, "score": score})

            return rows

        # Normalize and ensure integer types
        parsed: list[dict] = []
        for item in data:
            try:
                team = (item.get("team") or "your").lower()
                player = (item.get("player") or "").strip()
                kills = item.get("kills")
                score = item.get("score")
                if isinstance(kills, (int, float)):
                    kills = int(kills)
                else:
                    kills = None if kills is None else int(re.search(r"-?\d+", str(kills)).group(0))

                if isinstance(score, (int, float)):
                    score = int(score)
                else:
                    score = None if score is None else int(re.search(r"-?\d+", str(score)).group(0))

                parsed.append({"team": team, "player": player, "kills": kills, "score": score})
            except Exception:
                continue

        return parsed

    async def _chat_completion_text(self, *, image_bytes: bytes, mime_type: str, system_prompt: str, user_prompt: str) -> str:
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_prompt,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": data_url,
                            },
                        },
                    ],
                },
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": self.app_name,
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url

        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.base_url}/chat/completions", json=payload, headers=headers) as response:
                response_text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"OpenRouter request failed with {response.status}: {response_text}")

                data = await response.json()

        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    @staticmethod
    def _clean_extracted_text(text: str) -> str:
        cleaned = text.strip().strip("`").strip('"').strip("'")
        cleaned = re.sub(r"^(player\s*name|name)\s*[:\-]\s*", "", cleaned, flags=re.IGNORECASE).strip()
        first_line = cleaned.splitlines()[0].strip() if cleaned else ""
        return first_line.strip("`").strip('"').strip("'")