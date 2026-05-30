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
        data_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are OCR for a game profile screenshot. "
                        "Extract only the player profile name. "
                        "Return only the exact name and nothing else."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract the profile name from this screenshot.",
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

        return self._clean_extracted_text(str(content))

    @staticmethod
    def _clean_extracted_text(text: str) -> str:
        cleaned = text.strip().strip("`").strip('"').strip("'")
        cleaned = re.sub(r"^(player\s*name|name)\s*[:\-]\s*", "", cleaned, flags=re.IGNORECASE).strip()
        first_line = cleaned.splitlines()[0].strip() if cleaned else ""
        return first_line.strip("`").strip('"').strip("'")