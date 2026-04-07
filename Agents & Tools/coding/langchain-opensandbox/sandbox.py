"""OpenSandbox backend for Deep Agents.

Implements the BaseSandbox protocol by delegating to the OpenSandbox Python SDK
(``opensandbox`` package).  Only four methods need a concrete implementation:

- ``id``              – sandbox identifier
- ``execute()``       – run a shell command
- ``upload_files()``  – write files into the sandbox
- ``download_files()``– read files from the sandbox

Everything else (``ls``, ``read``, ``write``, ``edit``, ``grep``, ``glob``) is
inherited from ``BaseSandbox`` which composes them from the four primitives above.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileOperationError,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.exceptions import SandboxApiException, SandboxException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.sync.sandbox import SandboxSync

logger = logging.getLogger(__name__)

# Default command timeout: 30 minutes (consistent with other Deep Agents backends)
_DEFAULT_TIMEOUT = 30 * 60


class OpenSandboxBackend(BaseSandbox):
    """Deep Agents sandbox backend powered by OpenSandbox.

    Parameters
    ----------
    sandbox : SandboxSync
        A *connected* synchronous sandbox instance.  The caller is responsible
        for creating and eventually killing/closing it.
    default_timeout : int
        Default command timeout in seconds (default 1800 = 30 min).
    working_directory : str | None
        If set, every ``execute()`` call runs in this directory.
    """

    def __init__(
        self,
        sandbox: SandboxSync,
        *,
        default_timeout: int = _DEFAULT_TIMEOUT,
        working_directory: str | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._default_timeout = default_timeout
        self._working_directory = working_directory

    # -- Factory helpers -------------------------------------------------------

    @classmethod
    def create(
        cls,
        image: str = "opensandbox/code-interpreter:v1.0.2",
        *,
        timeout: timedelta = timedelta(minutes=30),
        api_key: str | None = None,
        domain: str | None = None,
        protocol: str | None = None,
        entrypoint: list[str] | None = None,
        resource: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        use_server_proxy: bool = False,
        default_timeout: int = _DEFAULT_TIMEOUT,
        working_directory: str | None = None,
        **sandbox_kwargs: Any,
    ) -> "OpenSandboxBackend":
        """Create a new OpenSandbox instance and wrap it as a Deep Agents backend.

        Parameters
        ----------
        image : str
            Docker image for the sandbox.
        timeout : timedelta
            Sandbox TTL before automatic termination.
        api_key : str | None
            OpenSandbox API key.  Falls back to ``OPEN_SANDBOX_API_KEY`` env var.
        domain : str | None
            OpenSandbox server address.  Falls back to ``OPEN_SANDBOX_DOMAIN`` env var.
        protocol : str | None
            ``"http"`` or ``"https"``.
        entrypoint : list[str] | None
            Container entrypoint override.
        resource : dict | None
            Resource limits, e.g. ``{"cpu": "1", "memory": "2Gi"}``.
        env : dict | None
            Environment variables injected into the sandbox.
        use_server_proxy : bool
            If True, route execd calls through server proxy (required for K8s).
        default_timeout : int
            Default per-command timeout in seconds.
        working_directory : str | None
            Default working directory for command execution.
        **sandbox_kwargs
            Extra keyword arguments forwarded to ``SandboxSync.create()``.
        """
        config_kwargs: dict[str, Any] = {}
        if api_key is not None:
            config_kwargs["api_key"] = api_key
        if domain is not None:
            config_kwargs["domain"] = domain
        if protocol is not None:
            config_kwargs["protocol"] = protocol
        if use_server_proxy:
            config_kwargs["use_server_proxy"] = True
        config = ConnectionConfigSync(**config_kwargs)

        create_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "connection_config": config,
        }
        if entrypoint is not None:
            create_kwargs["entrypoint"] = entrypoint
        if resource is not None:
            create_kwargs["resource"] = resource
        if env is not None:
            create_kwargs["env"] = env
        create_kwargs.update(sandbox_kwargs)

        sandbox = SandboxSync.create(image, **create_kwargs)
        logger.info("OpenSandbox created: id=%s image=%s", sandbox.id, image)
        return cls(
            sandbox,
            default_timeout=default_timeout,
            working_directory=working_directory,
        )

    @classmethod
    def connect(
        cls,
        sandbox_id: str,
        *,
        api_key: str | None = None,
        domain: str | None = None,
        protocol: str | None = None,
        use_server_proxy: bool = False,
        default_timeout: int = _DEFAULT_TIMEOUT,
        working_directory: str | None = None,
    ) -> "OpenSandboxBackend":
        """Connect to an existing OpenSandbox instance.

        Parameters
        ----------
        use_server_proxy : bool
            If True, route all execd calls (commands, files) through the server
            proxy API.  Required for K8s deployments where the SDK cannot reach
            sandbox pod IPs directly.
        """
        config_kwargs: dict[str, Any] = {}
        if api_key is not None:
            config_kwargs["api_key"] = api_key
        if domain is not None:
            config_kwargs["domain"] = domain
        if protocol is not None:
            config_kwargs["protocol"] = protocol
        if use_server_proxy:
            config_kwargs["use_server_proxy"] = True
        config = ConnectionConfigSync(**config_kwargs)

        sandbox = SandboxSync.connect(sandbox_id, connection_config=config)
        logger.info("OpenSandbox connected: id=%s", sandbox.id)
        return cls(
            sandbox,
            default_timeout=default_timeout,
            working_directory=working_directory,
        )

    # -- BaseSandbox abstract interface ----------------------------------------

    @property
    def id(self) -> str:
        """Unique identifier of the underlying OpenSandbox instance."""
        return self._sandbox.id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """Execute a shell command inside the sandbox.

        Parameters
        ----------
        command : str
            Shell command string (interpreted by ``bash -c``).
        timeout : int | None
            Per-command timeout in seconds.  Defaults to ``self._default_timeout``.

        Returns
        -------
        ExecuteResponse
            Combined stdout+stderr output, exit code, and truncation flag.
        """
        effective_timeout = timeout if timeout is not None else self._default_timeout

        opts = RunCommandOpts(
            timeout=timedelta(seconds=effective_timeout),
        )
        if self._working_directory:
            opts.working_directory = self._working_directory

        try:
            result = self._sandbox.commands.run(command, opts=opts)
        except SandboxException as exc:
            logger.warning("Command execution failed: %s", exc)
            return ExecuteResponse(
                output=str(exc),
                exit_code=1,
                truncated=False,
            )

        # Combine stdout and stderr into a single output string
        output_parts: list[str] = []
        stdout_text = "\n".join(
            msg.text.rstrip("\n") for msg in result.logs.stdout
        )
        stderr_text = "\n".join(
            msg.text.rstrip("\n") for msg in result.logs.stderr
        )
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(stderr_text)

        output = "\n".join(output_parts) if output_parts else ""

        return ExecuteResponse(
            output=output,
            exit_code=result.exit_code,
            truncated=False,
        )

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        """Upload files into the sandbox.

        Ensures parent directories exist before writing.

        Parameters
        ----------
        files : list[tuple[str, bytes]]
            List of ``(path, content)`` pairs.

        Returns
        -------
        list[FileUploadResponse]
            Per-file results; partial success is supported.
        """
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                self._sandbox.files.write_file(path, content)
                responses.append(FileUploadResponse(path=path, error=None))
            except SandboxApiException as exc:
                error = _map_api_error(exc)
                logger.warning("Upload failed for %s: %s", path, exc)
                responses.append(FileUploadResponse(path=path, error=error))
            except SandboxException as exc:
                logger.warning("Upload failed for %s: %s", path, exc)
                responses.append(
                    FileUploadResponse(path=path, error="permission_denied")
                )
        return responses

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        """Download files from the sandbox.

        Parameters
        ----------
        paths : list[str]
            Absolute paths to read.

        Returns
        -------
        list[FileDownloadResponse]
            Per-file results; partial success is supported.
        """
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = self._sandbox.files.read_bytes(path)
                responses.append(
                    FileDownloadResponse(path=path, content=content, error=None)
                )
            except SandboxApiException as exc:
                error = _map_api_error(exc)
                logger.warning("Download failed for %s: %s", path, exc)
                responses.append(
                    FileDownloadResponse(path=path, content=None, error=error)
                )
            except SandboxException as exc:
                logger.warning("Download failed for %s: %s", path, exc)
                responses.append(
                    FileDownloadResponse(
                        path=path, content=None, error="file_not_found"
                    )
                )
        return responses

    # -- Lifecycle helpers -----------------------------------------------------

    def kill(self) -> None:
        """Terminate the remote sandbox instance (irreversible)."""
        self._sandbox.kill()
        logger.info("OpenSandbox killed: id=%s", self.id)

    def close(self) -> None:
        """Close local HTTP resources.  Does NOT terminate the remote sandbox."""
        self._sandbox.close()

    def __enter__(self) -> "OpenSandboxBackend":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


# -- Helpers -------------------------------------------------------------------


def _map_api_error(exc: SandboxApiException) -> FileOperationError:
    """Best-effort mapping of OpenSandbox HTTP errors to Deep Agents error codes."""
    status = getattr(exc, "status_code", None)
    msg = str(exc).lower()
    if status == 404 or "not found" in msg:
        return "file_not_found"
    if status == 403 or "permission" in msg or "denied" in msg:
        return "permission_denied"
    if "is a directory" in msg or "is_directory" in msg:
        return "is_directory"
    if "invalid" in msg:
        return "invalid_path"
    return "file_not_found"
