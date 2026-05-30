from __future__ import annotations

import base64
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