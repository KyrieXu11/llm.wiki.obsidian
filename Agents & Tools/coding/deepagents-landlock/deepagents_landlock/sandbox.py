"""Landlock sandbox backend for Deep Agents.

Implements the BaseSandbox protocol using Linux Landlock LSM for filesystem
isolation.  Unlike container-based backends (OpenSandbox, Modal), this backend
runs commands **locally** but with kernel-enforced filesystem restrictions
applied to each subprocess.

Key design:
- Each ``execute()`` call forks a child process with Landlock restrictions
- ``upload_files()`` / ``download_files()`` operate on the local filesystem
  with application-level path validation (within workspace boundary)
- The parent process is never restricted, so it can manage multiple sandboxes
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from deepagents_landlock import landlock

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30 * 60  # 30 minutes, consistent with other backends

# System paths that should be read-only + executable for commands to work
_DEFAULT_RO_PATHS = [
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/opt",
    "/etc",
]

_DEFAULT_RO_EXEC_PATHS = [
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/opt",
]


class LandlockSandbox(BaseSandbox):
    """Deep Agents sandbox backend using Landlock LSM for local isolation.

    Each ``execute()`` call spawns a subprocess that is Landlock-restricted to
    only access the workspace and system read-only paths.  The parent process
    remains unrestricted.

    Parameters
    ----------
    workspace : str | Path
        The directory that the sandboxed process can read and write.
        Must be an absolute path.
    sandbox_id : str | None
        Unique identifier.  Auto-generated if not provided.
    default_timeout : int
        Default command timeout in seconds (default: 1800 = 30 min).
    extra_ro_paths : list[str] | None
        Additional read-only paths beyond the defaults.
    extra_rw_paths : list[str] | None
        Additional read-write paths beyond the workspace.
    enable_landlock : bool | None
        Force-enable or disable Landlock.  ``None`` (default) auto-detects.
    """

    def __init__(
        self,
        workspace: str | Path,
        *,
        sandbox_id: str | None = None,
        default_timeout: int = _DEFAULT_TIMEOUT,
        extra_ro_paths: list[str] | None = None,
        extra_rw_paths: list[str] | None = None,
        enable_landlock: bool | None = None,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._id = sandbox_id or f"landlock-{uuid.uuid4().hex[:12]}"
        self._default_timeout = default_timeout
        self._extra_ro_paths = extra_ro_paths or []
        self._extra_rw_paths = extra_rw_paths or []

        if enable_landlock is None:
            self._use_landlock = landlock.is_supported()
        else:
            self._use_landlock = enable_landlock

        # Ensure workspace exists
        self._workspace.mkdir(parents=True, exist_ok=True)

        if self._use_landlock:
            logger.info(
                "LandlockSandbox created: id=%s workspace=%s abi=v%d",
                self._id, self._workspace, landlock.get_abi_version(),
            )
        else:
            logger.warning(
                "LandlockSandbox created WITHOUT Landlock (unsupported): "
                "id=%s workspace=%s",
                self._id, self._workspace,
            )

    # -- Factory helpers -------------------------------------------------------

    @classmethod
    def create(
        cls,
        workspace: str | Path | None = None,
        **kwargs: Any,
    ) -> "LandlockSandbox":
        """Create a new Landlock sandbox.

        If no workspace is given, a temporary directory is created under
        ``/tmp/deepagents-landlock-<random>/``.
        """
        if workspace is None:
            workspace = Path(f"/tmp/deepagents-landlock-{uuid.uuid4().hex[:12]}")
        return cls(workspace, **kwargs)

    # -- BaseSandbox abstract interface ----------------------------------------

    @property
    def id(self) -> str:
        return self._id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command in a Landlock-restricted subprocess.

        The child process is restricted to:
        - Read-write: workspace + extra_rw_paths
        - Read-only + execute: system paths (/usr, /lib, /bin, etc.)
        - Read-only: /etc, /proc, /dev + extra_ro_paths
        - Everything else: denied at kernel level
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout

        # Build the Landlock rules dict for the child process
        rules = self._build_rules()

        # Build a wrapper script that applies Landlock then execs the command.
        # We use a Python one-liner passed to the same interpreter, which
        # applies Landlock in the child process before exec-ing bash.
        if self._use_landlock:
            wrapper_script = self._build_wrapper_script(command, rules)
            cmd = [sys.executable, "-c", wrapper_script]
        else:
            # No Landlock: run directly (macOS dev, old kernels)
            cmd = ["bash", "-c", command]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                cwd=str(self._workspace),
                env=self._build_env(),
            )
        except subprocess.TimeoutExpired:
            return ExecuteResponse(
                output=f"Command timed out after {effective_timeout}s",
                exit_code=124,
                truncated=True,
            )
        except Exception as exc:
            logger.warning("Command execution failed: %s", exc)
            return ExecuteResponse(output=str(exc), exit_code=1, truncated=False)

        # Merge stdout + stderr
        parts = []
        if proc.stdout:
            parts.append(proc.stdout.rstrip("\n"))
        if proc.stderr:
            parts.append(proc.stderr.rstrip("\n"))
        output = "\n".join(parts) if parts else ""

        return ExecuteResponse(
            output=output,
            exit_code=proc.returncode,
            truncated=False,
        )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Write files to the workspace filesystem.

        Paths are validated to be within the workspace boundary.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            error = self._validate_path(path)
            if error:
                responses.append(FileUploadResponse(path=path, error=error))
                continue
            try:
                full = self._resolve_path(path)
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(content)
                responses.append(FileUploadResponse(path=path, error=None))
            except PermissionError:
                responses.append(FileUploadResponse(path=path, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileUploadResponse(path=path, error="is_directory"))
            except Exception as exc:
                logger.warning("Upload failed for %s: %s", path, exc)
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
        return responses

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Read files from the workspace filesystem.

        Paths are validated to be within the workspace boundary.
        """
        responses: list[FileDownloadResponse] = []
        for path in paths:
            error = self._validate_path(path)
            if error:
                responses.append(FileDownloadResponse(path=path, content=None, error=error))
                continue
            try:
                full = self._resolve_path(path)
                content = full.read_bytes()
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except FileNotFoundError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, content=None, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileDownloadResponse(path=path, content=None, error="is_directory"))
            except Exception as exc:
                logger.warning("Download failed for %s: %s", path, exc)
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return responses

    # -- Lifecycle helpers -----------------------------------------------------

    @property
    def workspace(self) -> Path:
        """The workspace directory path."""
        return self._workspace

    @property
    def landlock_enabled(self) -> bool:
        """Whether Landlock restrictions are active."""
        return self._use_landlock

    def cleanup(self) -> None:
        """Remove the workspace directory.  Use with caution."""
        import shutil
        if self._workspace.exists():
            shutil.rmtree(self._workspace)
            logger.info("LandlockSandbox cleaned up: %s", self._workspace)

    def __enter__(self) -> "LandlockSandbox":
        return self

    def __exit__(self, *_: Any) -> None:
        pass  # Don't auto-cleanup; let caller decide

    # -- Internal helpers ------------------------------------------------------

    def _build_rules(self) -> dict[str, int]:
        """Build the Landlock rules dict."""
        rules: dict[str, int] = {}

        # Workspace: full read-write
        rules[str(self._workspace)] = landlock.FS_READ_WRITE

        # Extra rw paths
        for p in self._extra_rw_paths:
            rules[p] = landlock.FS_READ_WRITE

        # System paths: read + execute
        for p in _DEFAULT_RO_EXEC_PATHS:
            rules[p] = landlock.FS_READ_EXECUTE

        # Read-only paths
        for p in ["/etc", "/proc", "/dev"]:
            rules[p] = landlock.FS_READ

        # Extra ro paths
        for p in self._extra_ro_paths:
            rules[p] = landlock.FS_READ

        return rules

    def _build_env(self) -> dict[str, str]:
        """Build environment for the child process."""
        env = os.environ.copy()
        env["HOME"] = str(self._workspace)
        env["TMPDIR"] = str(self._workspace / ".tmp")
        # Ensure .tmp exists
        (self._workspace / ".tmp").mkdir(exist_ok=True)
        return env

    def _build_wrapper_script(self, command: str, rules: dict[str, int]) -> str:
        """Generate a Python one-shot script that applies Landlock then execs bash.

        The script is run as ``python -c <script>`` in the child process.
        It imports the landlock module, applies the rules, then uses
        ``os.execv`` to replace itself with ``bash -c <command>``.
        """
        import json as _json
        rules_json = _json.dumps(rules)
        # Escape for embedding in Python string
        escaped_command = command.replace("\\", "\\\\").replace("'", "\\'")

        return textwrap.dedent(f"""\
            import json, os, sys
            sys.path.insert(0, {repr(str(Path(__file__).resolve().parent.parent))})
            from deepagents_landlock.landlock import apply
            rules = json.loads({repr(rules_json)})
            apply(rules)
            os.execv("/bin/bash", ["bash", "-c", {repr(command)}])
        """)

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to the workspace."""
        p = Path(path)
        if p.is_absolute():
            return p.resolve()
        return (self._workspace / p).resolve()

    def _validate_path(self, path: str) -> FileOperationError | None:
        """Validate that a path is within the workspace or extra_rw_paths."""
        resolved = self._resolve_path(path)
        allowed_roots = [self._workspace] + [Path(p) for p in self._extra_rw_paths]
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                return None
            except ValueError:
                continue
        return "permission_denied"
