"""Start the API and its browser interface from any working directory.

Only Python's standard library is needed to run this launcher. Dependencies are
installed in this project's own .venv, never in the system Python environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
MIN_PYTHON = (3, 12)


class StartupError(RuntimeError):
    """A startup problem with an actionable explanation for the user."""


def prepare_environment(project_root: Path) -> Path:
    """Create a local environment and refresh dependencies when they change."""
    environment = project_root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = project_root / "requirements.txt"
    if not requirements.is_file():
        raise StartupError("requirements.txt is missing. Extract or clone the complete project.")
    if not python.is_file():
        print("First launch: creating this project's Python environment...", flush=True)
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                cwd=project_root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise StartupError(
                "Could not create .venv. Install Python 3.12 or newer from python.org "
                "with the venv and pip components, then try again."
            ) from exc

    version_check = subprocess.run(
        [str(python), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"],
        cwd=project_root,
        check=False,
    )
    if version_check.returncode:
        raise StartupError(
            "The existing .venv uses an older Python. Rename or remove the .venv folder "
            "and launch again with Python 3.12 or newer. Your data is stored separately in data/."
        )

    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = environment / ".geospatial-requirements.sha256"
    if not stamp.is_file() or stamp.read_text(encoding="utf-8").strip() != fingerprint:
        print(
            "Installing application dependencies (internet is needed on first launch)...",
            flush=True,
        )
        try:
            subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(requirements)],
                cwd=project_root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise StartupError(
                "Dependency installation failed. Check the pip error above and your internet "
                "connection, then run the launcher again. The server was not started."
            ) from exc
        stamp.write_text(fingerprint + "\n", encoding="utf-8")
    return python


def available_port(preferred: int, attempts: int = 10) -> int:
    """Find an unused loopback port without opening an unrelated running app."""
    for port in range(preferred, min(preferred + attempts, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise StartupError(
        f"No available port near {preferred}. Try another port: python start.py --port 8100"
    )


def wait_until_ready(process: subprocess.Popen, url: str, timeout: float = 45) -> None:
    """Wait for the new server; never open a browser if startup fails."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StartupError(
                "The server stopped during startup. Check the error above. "
                "If the port became busy, run the launcher again."
            )
        try:
            with opener.open(url + "health", timeout=1) as response:
                healthy = response.status == 200 and json.load(response).get("status") == "ok"
            if healthy:
                # Give a failed bind time to surface before opening the browser.
                time.sleep(0.25)
                if process.poll() is None:
                    return
        except (OSError, urllib.error.URLError, ValueError, AttributeError):
            pass
        time.sleep(0.2)
    raise StartupError("The server did not become ready within 45 seconds. Check its output above.")


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open the Geospatial File Measurement web app.")
    parser.add_argument("--port", type=int, default=8000, help="preferred port (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="start without opening a browser")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if sys.version_info < MIN_PYTHON:
        print(
            "Python 3.12 or newer is required. Download it from https://www.python.org/downloads/."
        )
        return 1

    process = None
    try:
        port = available_port(args.port)
        python = prepare_environment(PROJECT_ROOT)
        # Installation may take a while; check availability again immediately before launch.
        port = available_port(port)
        url = f"http://127.0.0.1:{port}/"
        print(f"\nStarting Geospatial File Measurement at {url}", flush=True)
        print("Keep this window open. Press Ctrl+C to stop the server.\n", flush=True)
        options = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=PROJECT_ROOT,
            **options,
        )
        wait_until_ready(process, url)
        print(f"Ready: {url}", flush=True)
        if not args.no_browser:
            try:
                if not webbrowser.open(url):
                    print(f"Open this address in your browser: {url}", flush=True)
            except webbrowser.Error:
                print(f"Open this address in your browser: {url}", flush=True)
        return process.wait()
    except KeyboardInterrupt:
        print("\nStopping the server...", flush=True)
        return 0
    except (StartupError, OSError) as exc:
        print(f"\nCould not start the application: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if process is not None:
            stop_server(process)


if __name__ == "__main__":
    raise SystemExit(main())
