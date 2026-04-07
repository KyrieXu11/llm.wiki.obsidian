"""OpenSandbox provider for the Deep Agents CLI sandbox factory.

This module implements ``SandboxProvider`` so that users can run::

    deepagents --sandbox opensandbox

To register this provider, the following changes are needed in
``libs/cli/deepagents_cli/integrations/sandbox_factory.py``:

1. Add ``"opensandbox": "/home/user"`` to ``_PROVIDER_TO_WORKING_DIR``.
2. Import and return ``OpenSandboxProvider`` from ``_get_provider()``.
3. Add ``"opensandbox": ("langchain_opensandbox", "opensandbox")`` to
   ``backend_modules`` in ``verify_sandbox_deps()``.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.sync.sandbox import SandboxSync

logger = logging.getLogger(__name__)

# Default sandbox image
_DEFAULT_IMAGE = "opensandbox/code-interpreter:v1.0.2"
_DEFAULT_ENTRYPOINT = ["/opt/opensandbox/code-interpreter.sh"]


class OpenSandboxProvider:
    """Sandbox lifecycle provider for OpenSandbox.

    Manages creating and deleting OpenSandbox instances for the Deep Agents CLI.
    Configuration is read from environment variables:

    - ``OPEN_SANDBOX_API_KEY`` – API key
    - ``OPEN_SANDBOX_DOMAIN`` – Server address (default: ``localhost:8080``)
    - ``OPEN_SANDBOX_PROTOCOL`` – ``http`` or ``https`` (default: ``http``)
    - ``OPEN_SANDBOX_IMAGE`` – Docker image (default: code-interpreter)
    - ``OPEN_SANDBOX_TIMEOUT`` – Sandbox TTL in minutes (default: 30)
    """

    def __init__(self) -> None:
        self._config = self._build_config()

    @staticmethod
    def _build_config() -> ConnectionConfigSync:
        kwargs: dict[str, Any] = {}
        api_key = os.environ.get("OPEN_SANDBOX_API_KEY")
        if api_key:
            kwargs["api_key"] = api_key
        domain = os.environ.get("OPEN_SANDBOX_DOMAIN")
        if domain:
            kwargs["domain"] = domain
        protocol = os.environ.get("OPEN_SANDBOX_PROTOCOL")
        if protocol:
            kwargs["protocol"] = protocol
        return ConnectionConfigSync(**kwargs)

    def get_or_create(self, sandbox_id: str | None = None) -> SandboxSync:
        """Return an existing sandbox or create a new one.

        Parameters
        ----------
        sandbox_id : str | None
            If provided, connect to this sandbox.  Otherwise create a new one.
        """
        if sandbox_id:
            logger.info("Connecting to existing OpenSandbox: %s", sandbox_id)
            return SandboxSync.connect(sandbox_id, connection_config=self._config)

        image = os.environ.get("OPEN_SANDBOX_IMAGE", _DEFAULT_IMAGE)
        timeout_minutes = int(os.environ.get("OPEN_SANDBOX_TIMEOUT", "30"))
        entrypoint_env = os.environ.get("OPEN_SANDBOX_ENTRYPOINT")
        entrypoint = entrypoint_env.split(",") if entrypoint_env else _DEFAULT_ENTRYPOINT

        sandbox = SandboxSync.create(
            image,
            entrypoint=entrypoint,
            timeout=timedelta(minutes=timeout_minutes),
            connection_config=self._config,
        )
        logger.info("Created OpenSandbox: id=%s image=%s", sandbox.id, image)
        return sandbox

    def delete(self, sandbox_id: str) -> None:
        """Terminate and clean up a sandbox."""
        sandbox = SandboxSync.connect(sandbox_id, connection_config=self._config)
        sandbox.kill()
        sandbox.close()
        logger.info("Deleted OpenSandbox: %s", sandbox_id)
