from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="session")
def live_demo_origin() -> Iterator[str]:
    """Start the real ASGI demo app on an isolated loopback port."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root)])
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "demo_app.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{origin}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    else:
        process.terminate()
        raise RuntimeError("demo app did not become healthy")
    try:
        yield origin
    finally:
        process.terminate()
        process.wait(timeout=5)
