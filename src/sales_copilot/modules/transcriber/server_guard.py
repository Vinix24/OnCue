"""Parent-death guard for ``whisper-server``.

Run as ``python -m sales_copilot.modules.transcriber.server_guard -- <server command>``.
The guard starts the server as its own child and reads stdin until EOF. The
parent holds the write end of that pipe, so the kernel closes it when the
parent dies for any reason (also SIGKILL). On EOF the guard stops the server
and exits. macOS has no PR_SET_PDEATHSIG, and this works on every platform.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading

TERMINATE_GRACE_S = 3.0


def stop_server(server: subprocess.Popen[bytes], grace_s: float = TERMINATE_GRACE_S) -> None:
    """Terminate the server, escalating to kill after ``grace_s``."""
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def _wait_for_parent_eof(server: subprocess.Popen[bytes]) -> None:
    stream = sys.stdin.buffer
    while stream.read(4096):
        pass
    stop_server(server)


def run(command: list[str]) -> int:
    server = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _on_signal(_signum: int, _frame: object) -> None:
        stop_server(server)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    threading.Thread(target=_wait_for_parent_eof, args=(server,), daemon=True).start()
    return server.wait()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("server_guard: no server command given", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
