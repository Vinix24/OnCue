from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import numpy as np


class TranscriptionBackend(Protocol):
    async def start(self, stop_event: asyncio.Event) -> None: ...

    async def warmup(self) -> None: ...

    async def transcribe(self, audio: np.ndarray) -> str: ...

    async def transcribe_file(self, path: Path) -> str: ...

    async def stop(self) -> None: ...
