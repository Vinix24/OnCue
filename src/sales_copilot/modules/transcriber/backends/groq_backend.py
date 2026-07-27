from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx
import numpy as np

from sales_copilot.modules.transcriber.wav_utils import encode_wav_bytes

logger = logging.getLogger(__name__)

GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"
GROQ_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GroqBackend:
    """Cloud transcription via Groq's ``whisper-large-v3-turbo`` endpoint.

    CLOUD backend: every audio chunk leaves the machine as a WAV upload to
    Groq's API. Measured ~5x faster than the local whisper.cpp path at
    comparable NL transcription quality, but it is never the default —
    whisper.cpp stays the privacy-first default backend. Selecting this
    backend is an explicit opt-in (dashboard dropdown or
    ``WHISPER_BACKEND=groq``).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = GROQ_WHISPER_MODEL,
        base_url: str | None = None,
        language: str = "nl",
        timeout_s: float = 30.0,
    ) -> None:
        key = (api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")).strip()
        if not key:
            raise ValueError(
                "Transcription backend 'groq' is selected but GROQ_API_KEY is empty. "
                "Set GROQ_API_KEY in your .env, or switch WHISPER_BACKEND to whisper.cpp."
            )
        self._api_key = key
        self._model = model
        resolved_base = (base_url or os.getenv("GROQ_BASE_URL", GROQ_DEFAULT_BASE_URL)).rstrip("/")
        self._url = f"{resolved_base}/audio/transcriptions"
        self._language = language
        self._timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def start(self, _stop_event: asyncio.Event) -> None:
        self._client = httpx.AsyncClient(timeout=self._timeout_s)

    async def warmup(self) -> None:
        # Groq hosts the model remotely — nothing local to load or warm up.
        return None

    async def transcribe(self, audio: np.ndarray) -> str:
        return await self._post(encode_wav_bytes(audio, 16000))

    async def transcribe_file(self, path: Path) -> str:
        return await self._post(Path(path).read_bytes())

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, wav_bytes: bytes) -> str:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_s)
        try:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
                data={
                    "model": self._model,
                    "language": self._language,
                    "response_format": "json",
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Groq transcription request failed: %s", exc)
            raise RuntimeError(f"Groq transcription request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        payload = response.json()
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        return text.strip() if isinstance(text, str) else ""
