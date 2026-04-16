"""Integration tests for LandlockSandbox with real Landlock enforcement.

These tests MUST run on Linux >= 5.13 with Landlock enabled.
They verify that kernel-level filesystem isolation actually works.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from deepagents_landlock.landlock import (
    FS_READ,
    FS_READ_EXECUTE,
    FS_READ_WRITE,
    apply,
    get_abi_version,
    is_supported,
)
from deepagents_landlock.sandbox import LandlockSandbox

pytestmark = pytest.mark.skipif(
    not is_supported(),
    reason="Landlock not supported on this kernel",
)


# ---------------------------------------------------------------------------
# Low-level landlock module tests
# ---------------------------------------------------------------------------


class TestLandlockModule:
    def test_is_supported(self) -> None:
        assert is_supported() is True

    def test_abi_version(self) -> None:
        abi = get_abi_version()
        assert abi >= 1
        print(f"Landlock ABI version: {abi}")


# ---------------------------------------------------------------------------
# LandlockSandbox integration tests
# ---------------------------------------------------------------------------


class TestLandlockSandboxIntegration:
    @pytest.fixture
    def sandbox(self, tmp_path: Path) -> LandlockSandbox:
        ws = tmp_path / "workspace"
        return LandlockSandbox(ws, enable_landlock=True)

    # -- Basic execution with Landlock --

    def test_execute_echo(self, sandbox: LandlockSandbox) -> None:
        assert sandbox.landlock_enabled
        result = sandbox.execute("echo hello")
        assert result.exit_code == 0
        assert "hello" in result.output

    def test_execute_exit_code(self, sandbox: LandlockSandbox) -> None:
        result = sandbox.execute("exit 42")
        assert result.exit_code == 42

    def test_execute_cwd(self, sandbox: LandlockSandbox) -> None:
        result = sandbox.execute("pwd")
        assert str(sandbox.workspace) in result.output

    # -- Filesystem isolation verification --

    def test_cannot_read_root_home(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should not be able to read paths outside allowed list."""
        # /root is not in the default allowed paths
        result = sandbox.execute("ls /root/")
        assert result.exit_code != 0

    def test_cannot_write_outside_workspace(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should not be able to write outside workspace."""
        result = sandbox.execute("touch /tmp/escape_test_$$")
        assert result.exit_code != 0

    def test_cannot_read_other_tmp_dirs(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should not be able to list /tmp."""
        result = sandbox.execute("ls /tmp/")
        # Should fail because /tmp is not in the allowed paths
        assert result.exit_code != 0

    def test_can_read_system_libs(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should be able to read system libraries."""
        result = sandbox.execute("ls /usr/bin/ | head -3")
        assert result.exit_code == 0

    def test_can_read_etc_readonly(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should be able to read /etc (read-only)."""
        result = sandbox.execute("cat /etc/hostname")
        assert result.exit_code == 0

    def test_cannot_write_etc(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should not be able to write to /etc."""
        result = sandbox.execute("touch /etc/hacked")
        assert result.exit_code != 0

    # -- Workspace operations within sandbox --

    def test_can_write_in_workspace(self, sandbox: LandlockSandbox) -> None:
        """Sandboxed process should be able to write within workspace."""
        result = sandbox.execute("echo test > output.txt && cat output.txt")
        assert result.exit_code == 0
        assert "test" in result.output

    def test_can_create_dirs_in_workspace(self, sandbox: LandlockSandbox) -> None:
        result = sandbox.execute("mkdir -p subdir/nested && echo ok")
        assert result.exit_code == 0
        assert "ok" in result.output

    def test_workspace_file_persists(self, sandbox: LandlockSandbox) -> None:
        """Files written by execute() should be downloadable."""
        sandbox.execute("echo persisted > persist.txt")
        responses = sandbox.download_files(["persist.txt"])
        assert responses[0].error is None
        assert b"persisted" in responses[0].content

    # -- Upload then execute --

    def test_upload_then_execute(self, sandbox: LandlockSandbox) -> None:
        sandbox.upload_files([("script.sh", b"#!/bin/bash\necho script_output")])
        result = sandbox.execute("bash script.sh")
        assert result.exit_code == 0
        assert "script_output" in result.output

    def test_upload_python_then_execute(self, sandbox: LandlockSandbox) -> None:
        code = b"import os; print(os.getcwd())"
        sandbox.upload_files([("test.py", code)])
        result = sandbox.execute("python3 test.py")
        assert result.exit_code == 0
        assert str(sandbox.workspace) in result.output

    # -- Child process inherits restrictions --

    def test_child_process_inherits_restrictions(self, sandbox: LandlockSandbox) -> None:
        """Subprocess spawned by the command should also be restricted."""
        # /root is not in allowed paths, child bash should also be denied
        result = sandbox.execute("bash -c 'ls /root/'")
        assert result.exit_code != 0

    def test_python_subprocess_restricted(self, sandbox: LandlockSandbox) -> None:
        """Python subprocess should inherit Landlock restrictions."""
        code = (
            "import subprocess; "
            "r = subprocess.run(['ls', '/root/'], capture_output=True); "
            "print(r.returncode)"
        )
        result = sandbox.execute(f"python3 -c \"{code}\"")
        assert result.exit_code == 0
        # The inner ls should have failed with non-zero exit (EACCES)
        assert "2" in result.output

    # -- Timeout --

    def test_timeout(self, sandbox: LandlockSandbox) -> None:
        result = sandbox.execute("sleep 30", timeout=2)
        assert result.exit_code == 124
        assert result.truncated

    # -- Extra paths --

    def test_extra_rw_path(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        extra = tmp_path / "extra_rw"
        extra.mkdir()
        (extra / "data.txt").write_text("extra_data")

        sandbox = LandlockSandbox(
            ws,
            enable_landlock=True,
            extra_rw_paths=[str(extra)],
        )
        result = sandbox.execute(f"cat {extra}/data.txt")
        assert result.exit_code == 0
        assert "extra_data" in result.output

    def test_extra_ro_path(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        extra = tmp_path / "extra_ro"
        extra.mkdir()
        (extra / "readonly.txt").write_text("ro_content")

        sandbox = LandlockSandbox(
            ws,
            enable_landlock=True,
            extra_ro_paths=[str(extra)],
        )
        # Can read
        result = sandbox.execute(f"cat {extra}/readonly.txt")
        assert result.exit_code == 0
        assert "ro_content" in result.output
        # Cannot write
        result = sandbox.execute(f"touch {extra}/new_file")
        assert result.exit_code != 0
