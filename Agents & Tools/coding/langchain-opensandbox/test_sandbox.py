"""Unit tests for OpenSandboxBackend.

These tests mock the OpenSandbox SDK to verify the adapter logic without
requiring a running OpenSandbox server.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from langchain_opensandbox.sandbox import OpenSandboxBackend, _map_api_error


# -- Fixtures ------------------------------------------------------------------

def _make_execution(stdout_text: str = "", stderr_text: str = "", exit_code: int = 0):
    """Build a mock Execution object matching the OpenSandbox SDK shape."""
    stdout_msg = MagicMock()
    stdout_msg.text = stdout_text

    stderr_msg = MagicMock()
    stderr_msg.text = stderr_text

    logs = MagicMock()
    logs.stdout = [stdout_msg] if stdout_text else []
    logs.stderr = [stderr_msg] if stderr_text else []

    execution = MagicMock()
    execution.exit_code = exit_code
    execution.logs = logs
    return execution


def _make_backend(sandbox_id: str = "test-sandbox-001") -> tuple[OpenSandboxBackend, MagicMock]:
    """Create an OpenSandboxBackend with a mocked SandboxSync."""
    mock_sandbox = MagicMock()
    mock_sandbox.id = sandbox_id
    backend = OpenSandboxBackend(mock_sandbox)
    return backend, mock_sandbox


# -- Tests: id property -------------------------------------------------------

def test_id_returns_sandbox_id():
    backend, _ = _make_backend("my-sandbox-42")
    assert backend.id == "my-sandbox-42"


# -- Tests: execute ------------------------------------------------------------

def test_execute_basic():
    backend, mock_sandbox = _make_backend()
    mock_sandbox.commands.run.return_value = _make_execution(
        stdout_text="hello world", exit_code=0,
    )

    result = backend.execute("echo hello world")

    assert result.output == "hello world"
    assert result.exit_code == 0
    assert result.truncated is False
    mock_sandbox.commands.run.assert_called_once()


def test_execute_combines_stdout_stderr():
    backend, mock_sandbox = _make_backend()
    mock_sandbox.commands.run.return_value = _make_execution(
        stdout_text="out", stderr_text="err", exit_code=0,
    )

    result = backend.execute("some command")

    assert "out" in result.output
    assert "err" in result.output


def test_execute_with_exit_code():
    backend, mock_sandbox = _make_backend()
    mock_sandbox.commands.run.return_value = _make_execution(
        stderr_text="not found", exit_code=127,
    )

    result = backend.execute("nonexistent_cmd")

    assert result.exit_code == 127


def test_execute_sdk_exception():
    from opensandbox.exceptions import SandboxException

    backend, mock_sandbox = _make_backend()
    mock_sandbox.commands.run.side_effect = SandboxException("timeout")

    result = backend.execute("sleep 9999")

    assert result.exit_code == 1
    assert "timeout" in result.output


def test_execute_with_custom_timeout():
    backend, mock_sandbox = _make_backend()
    mock_sandbox.commands.run.return_value = _make_execution(exit_code=0)

    backend.execute("cmd", timeout=60)

    call_kwargs = mock_sandbox.commands.run.call_args
    opts = call_kwargs.kwargs.get("opts") or call_kwargs[1].get("opts")
    assert opts is not None


def test_execute_with_working_directory():
    mock_sandbox = MagicMock()
    mock_sandbox.id = "wd-test"
    backend = OpenSandboxBackend(mock_sandbox, working_directory="/workspace")
    mock_sandbox.commands.run.return_value = _make_execution(exit_code=0)

    backend.execute("ls")

    call_kwargs = mock_sandbox.commands.run.call_args
    opts = call_kwargs.kwargs.get("opts") or call_kwargs[1].get("opts")
    assert opts.working_directory == "/workspace"


# -- Tests: upload_files -------------------------------------------------------

def test_upload_files_success():
    backend, mock_sandbox = _make_backend()

    responses = backend.upload_files([
        ("/tmp/a.txt", b"content a"),
        ("/tmp/b.txt", b"content b"),
    ])

    assert len(responses) == 2
    assert all(r.error is None for r in responses)
    assert mock_sandbox.files.write_file.call_count == 2


def test_upload_files_partial_failure():
    from opensandbox.exceptions import SandboxApiException

    backend, mock_sandbox = _make_backend()
    mock_sandbox.files.write_file.side_effect = [
        None,  # first succeeds
        SandboxApiException("not found", status_code=404),  # second fails
    ]

    responses = backend.upload_files([
        ("/tmp/ok.txt", b"ok"),
        ("/tmp/fail.txt", b"fail"),
    ])

    assert responses[0].error is None
    assert responses[1].error == "file_not_found"


# -- Tests: download_files -----------------------------------------------------

def test_download_files_success():
    backend, mock_sandbox = _make_backend()
    mock_sandbox.files.read_bytes.return_value = b"file content"

    responses = backend.download_files(["/tmp/test.txt"])

    assert len(responses) == 1
    assert responses[0].content == b"file content"
    assert responses[0].error is None


def test_download_files_not_found():
    from opensandbox.exceptions import SandboxApiException

    backend, mock_sandbox = _make_backend()
    mock_sandbox.files.read_bytes.side_effect = SandboxApiException(
        "not found", status_code=404,
    )

    responses = backend.download_files(["/tmp/missing.txt"])

    assert responses[0].content is None
    assert responses[0].error == "file_not_found"


# -- Tests: lifecycle ----------------------------------------------------------

def test_kill():
    backend, mock_sandbox = _make_backend()
    backend.kill()
    mock_sandbox.kill.assert_called_once()


def test_close():
    backend, mock_sandbox = _make_backend()
    backend.close()
    mock_sandbox.close.assert_called_once()


def test_context_manager():
    backend, mock_sandbox = _make_backend()
    with backend:
        pass
    mock_sandbox.close.assert_called_once()


# -- Tests: error mapping ------------------------------------------------------

def test_map_api_error_404():
    exc = MagicMock()
    exc.status_code = 404
    exc.__str__ = lambda self: "Not Found"
    assert _map_api_error(exc) == "file_not_found"


def test_map_api_error_403():
    exc = MagicMock()
    exc.status_code = 403
    exc.__str__ = lambda self: "Permission denied"
    assert _map_api_error(exc) == "permission_denied"


def test_map_api_error_is_directory():
    exc = MagicMock()
    exc.status_code = 400
    exc.__str__ = lambda self: "path is a directory"
    assert _map_api_error(exc) == "is_directory"
