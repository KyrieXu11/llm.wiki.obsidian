"""Unit tests for LandlockSandbox.

These tests run on any platform (macOS/Linux) by disabling Landlock
(``enable_landlock=False``), testing only the sandbox adapter logic:
command execution, file upload/download, path validation, etc.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from deepagents_landlock.sandbox import LandlockSandbox


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def sandbox(workspace: Path) -> LandlockSandbox:
    return LandlockSandbox(workspace, enable_landlock=False)


# -- id -----------------------------------------------------------------------


def test_id_auto_generated(sandbox: LandlockSandbox) -> None:
    assert sandbox.id.startswith("landlock-")
    assert len(sandbox.id) > len("landlock-")


def test_id_custom() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = LandlockSandbox(d, sandbox_id="my-sandbox", enable_landlock=False)
        assert s.id == "my-sandbox"


# -- execute -------------------------------------------------------------------


def test_execute_echo(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.output


def test_execute_exit_code(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("exit 42")
    assert result.exit_code == 42


def test_execute_stderr(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo err >&2")
    assert "err" in result.output


def test_execute_stdout_stderr_merged(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo out && echo err >&2")
    assert "out" in result.output
    assert "err" in result.output


def test_execute_cwd_is_workspace(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("pwd")
    assert result.exit_code == 0
    assert str(sandbox.workspace) in result.output


def test_execute_multiline(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo line1 && echo line2")
    assert "line1" in result.output
    assert "line2" in result.output


def test_execute_timeout(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("sleep 10", timeout=1)
    assert result.exit_code == 124
    assert result.truncated


def test_execute_env_home(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo $HOME")
    assert str(sandbox.workspace) in result.output


def test_execute_env_tmpdir(sandbox: LandlockSandbox) -> None:
    result = sandbox.execute("echo $TMPDIR")
    assert ".tmp" in result.output


# -- upload_files --------------------------------------------------------------


def test_upload_single_file(sandbox: LandlockSandbox) -> None:
    responses = sandbox.upload_files([("test.txt", b"hello")])
    assert len(responses) == 1
    assert responses[0].error is None
    assert (sandbox.workspace / "test.txt").read_bytes() == b"hello"


def test_upload_nested_path(sandbox: LandlockSandbox) -> None:
    responses = sandbox.upload_files([("sub/dir/file.py", b"code")])
    assert responses[0].error is None
    assert (sandbox.workspace / "sub/dir/file.py").read_bytes() == b"code"


def test_upload_absolute_within_workspace(sandbox: LandlockSandbox) -> None:
    path = str(sandbox.workspace / "abs.txt")
    responses = sandbox.upload_files([(path, b"data")])
    assert responses[0].error is None


def test_upload_outside_workspace_denied(sandbox: LandlockSandbox) -> None:
    responses = sandbox.upload_files([("/etc/shadow", b"hack")])
    assert responses[0].error == "permission_denied"


def test_upload_multiple_files(sandbox: LandlockSandbox) -> None:
    files = [("a.txt", b"aaa"), ("b.txt", b"bbb")]
    responses = sandbox.upload_files(files)
    assert all(r.error is None for r in responses)


def test_upload_binary(sandbox: LandlockSandbox) -> None:
    data = bytes(range(256))
    responses = sandbox.upload_files([("binary.bin", data)])
    assert responses[0].error is None
    assert (sandbox.workspace / "binary.bin").read_bytes() == data


# -- download_files ------------------------------------------------------------


def test_download_existing_file(sandbox: LandlockSandbox) -> None:
    (sandbox.workspace / "data.txt").write_bytes(b"content")
    responses = sandbox.download_files(["data.txt"])
    assert responses[0].content == b"content"
    assert responses[0].error is None


def test_download_nonexistent_file(sandbox: LandlockSandbox) -> None:
    responses = sandbox.download_files(["nope.txt"])
    assert responses[0].error == "file_not_found"
    assert responses[0].content is None


def test_download_outside_workspace_denied(sandbox: LandlockSandbox) -> None:
    responses = sandbox.download_files(["/etc/passwd"])
    assert responses[0].error == "permission_denied"


def test_download_multiple(sandbox: LandlockSandbox) -> None:
    (sandbox.workspace / "a.txt").write_bytes(b"a")
    (sandbox.workspace / "b.txt").write_bytes(b"b")
    responses = sandbox.download_files(["a.txt", "b.txt"])
    assert responses[0].content == b"a"
    assert responses[1].content == b"b"


# -- upload + download roundtrip -----------------------------------------------


def test_roundtrip(sandbox: LandlockSandbox) -> None:
    data = b"roundtrip test data"
    sandbox.upload_files([("rt.bin", data)])
    responses = sandbox.download_files(["rt.bin"])
    assert responses[0].content == data


# -- execute + file ops integration --------------------------------------------


def test_execute_reads_uploaded_file(sandbox: LandlockSandbox) -> None:
    sandbox.upload_files([("msg.txt", b"hello from file")])
    result = sandbox.execute("cat msg.txt")
    assert "hello from file" in result.output


def test_execute_writes_then_download(sandbox: LandlockSandbox) -> None:
    sandbox.execute("echo written > output.txt")
    responses = sandbox.download_files(["output.txt"])
    assert responses[0].content is not None
    assert b"written" in responses[0].content


# -- factory -------------------------------------------------------------------


def test_create_auto_workspace() -> None:
    s = LandlockSandbox.create(enable_landlock=False)
    assert s.workspace.exists()
    s.cleanup()


def test_create_custom_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "custom"
    s = LandlockSandbox.create(workspace=ws, enable_landlock=False)
    assert s.workspace == ws.resolve()
    assert ws.exists()


# -- cleanup -------------------------------------------------------------------


def test_cleanup(sandbox: LandlockSandbox) -> None:
    sandbox.upload_files([("file.txt", b"data")])
    assert sandbox.workspace.exists()
    sandbox.cleanup()
    assert not sandbox.workspace.exists()


# -- context manager -----------------------------------------------------------


def test_context_manager(workspace: Path) -> None:
    with LandlockSandbox(workspace, enable_landlock=False) as s:
        result = s.execute("echo ok")
        assert result.exit_code == 0
    # Workspace still exists (no auto-cleanup)
    assert workspace.exists()
