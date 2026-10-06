import hashlib
import os
import subprocess
from unittest.mock import MagicMock, Mock

import pytest

import start


def test_launcher_skips_occupied_port(monkeypatch):
    socket = MagicMock()
    probe = socket.return_value.__enter__.return_value
    probe.bind.side_effect = [OSError("Address already in use"), None]
    monkeypatch.setattr(start.socket, "socket", socket)
    assert start.available_port(8000) == 8001
    assert probe.bind.call_args_list[0].args[0] == ("127.0.0.1", 8000)
    assert probe.bind.call_args_list[1].args[0] == ("127.0.0.1", 8001)


def test_launcher_reports_no_available_port(monkeypatch):
    socket = MagicMock()
    socket.return_value.__enter__.return_value.bind.side_effect = OSError("Address already in use")
    monkeypatch.setattr(start.socket, "socket", socket)
    with pytest.raises(start.StartupError, match="No available port"):
        start.available_port(65535)
    assert socket.call_count == 1


def test_launcher_installs_changed_dependencies_and_uses_absolute_paths(tmp_path, monkeypatch):
    root = tmp_path / "project with spaces"
    root.mkdir()
    requirements = root / "requirements.txt"
    requirements.write_text("fastapi==1.0\n")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(start.subprocess, "run", run)
    monkeypatch.chdir(tmp_path)

    assert start.prepare_environment(root) == python
    assert run.call_args_list[-1].args[0] == [
        str(python),
        "-m",
        "pip",
        "install",
        "-r",
        str(requirements),
    ]
    assert run.call_args_list[-1].kwargs["cwd"] == root
    stamp = root / ".venv" / ".geospatial-requirements.sha256"
    assert stamp.read_text().strip() == hashlib.sha256(requirements.read_bytes()).hexdigest()

    run.reset_mock()
    start.prepare_environment(root)
    assert run.call_count == 1  # Version check only; no second pip install.
    requirements.write_text("fastapi==1.1\n")
    start.prepare_environment(root)
    assert run.call_args.args[0][1:4] == ["-m", "pip", "install"]


def test_failed_install_is_retryable_and_never_marked_complete(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("fastapi==1.0\n")
    python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        start.subprocess,
        "run",
        Mock(
            side_effect=[subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(1, [])]
        ),
    )
    with pytest.raises(start.StartupError, match="Dependency installation failed"):
        start.prepare_environment(tmp_path)
    assert not (tmp_path / ".venv" / ".geospatial-requirements.sha256").exists()


def test_failed_server_does_not_open_browser(monkeypatch):
    monkeypatch.setattr(start, "available_port", lambda port: port)
    monkeypatch.setattr(start, "prepare_environment", lambda _: "/project/.venv/bin/python")
    process = Mock()
    process.poll.return_value = 1
    monkeypatch.setattr(start.subprocess, "Popen", Mock(return_value=process))
    browser = Mock()
    monkeypatch.setattr(start.webbrowser, "open", browser)

    assert start.main([]) == 1
    browser.assert_not_called()


def test_running_server_uses_project_directory_and_requested_port(monkeypatch):
    monkeypatch.setattr(start, "available_port", lambda port: port + 1)
    monkeypatch.setattr(start, "prepare_environment", lambda _: "/project/.venv/bin/python")
    monkeypatch.setattr(start, "wait_until_ready", Mock())
    monkeypatch.setattr(start, "stop_server", Mock())
    process = Mock()
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(start.subprocess, "Popen", popen)
    browser = Mock()
    monkeypatch.setattr(start.webbrowser, "open", browser)

    assert start.main(["--port", "8100"]) == 0
    assert popen.call_args.kwargs["cwd"] == start.PROJECT_ROOT
    assert popen.call_args.args[0][-1] == "8102"
    browser.assert_called_once_with("http://127.0.0.1:8102/")
