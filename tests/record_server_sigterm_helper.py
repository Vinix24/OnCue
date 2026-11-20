"""Runs scripts/record_server.py on in-memory streams. Usage: <sessions dir> <port>"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from sales_copilot.audio.recorder_engine import RecorderEngine

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "record_server.py"


class SineStream:
    """One frame per two reads, so every drain of the engine ends."""
    def __init__(self) -> None:
        self._give = False

    def start(self) -> None:
        return None

    stop = start

    def read(self) -> np.ndarray | None:
        self._give = not self._give
        if not self._give:
            return None
        return (0.1 * np.sin(np.linspace(0, 200, 1024))).astype(np.float32)


def main() -> int:
    sessions, port = sys.argv[1], sys.argv[2]
    spec = importlib.util.spec_from_file_location("record_server_helper", SCRIPT)
    server = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = server
    spec.loader.exec_module(server)
    server.OUT_DIR = sessions
    server.RecorderEngine = lambda **kw: RecorderEngine(
        out_dir=kw["out_dir"], streams={"prospect": SineStream(), "self": SineStream()}
    )
    server.transcribe_session_dir = lambda session_dir: session_dir / server.TRANSCRIPT_NAME
    sys.argv = [str(SCRIPT), "--port", port]
    return server.main()


if __name__ == "__main__":
    raise SystemExit(main())
