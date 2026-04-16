"""Landlock provider for the Deep Agents CLI sandbox factory.

This module implements the provider interface so that users can run::

    deepagents --sandbox landlock

To register this provider, add the following to
``libs/cli/deepagents_cli/integrations/sandbox_factory.py``:

1. Add ``"landlock": "/workspace"`` to ``_PROVIDER_TO_WORKING_DIR``.
2. Import and return ``LandlockProvider`` from ``_get_provider()``.
3. Add ``"landlock": ("deepagents_landlock",)`` to ``backend_modules``
   in ``verify_sandbox_deps()``.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from deepagents_landlock.sandbox import LandlockSandbox

logger = logging.getLogger(__name__)

_DEFAULT_WORKSPACE_ROOT = "/tmp/deepagents-landlock"


class LandlockProvider:
    """Sandbox lifecycle provider for Landlock-based local sandboxes.

    Configuration via environment variables:

    - ``LANDLOCK_WORKSPACE_ROOT`` -- Parent dir for sandbox workspaces
      (default: ``/tmp/deepagents-landlock``)
    - ``LANDLOCK_EXTRA_RO_PATHS`` -- Comma-separated extra read-only paths
    - ``LANDLOCK_EXTRA_RW_PATHS`` -- Comma-separated extra read-write paths
    - ``LANDLOCK_ENABLED`` -- ``"0"`` to force-disable Landlock
    """

    def __init__(self) -> None:
        self._root = Path(
            os.environ.get("LANDLOCK_WORKSPACE_ROOT", _DEFAULT_WORKSPACE_ROOT)
        )
        self._root.mkdir(parents=True, exist_ok=True)

    def get_or_create(self, sandbox_id: str | None = None) -> LandlockSandbox:
        """Return an existing sandbox or create a new one."""
        if sandbox_id:
            workspace = self._root / sandbox_id
            if not workspace.exists():
                raise FileNotFoundError(
                    f"Sandbox workspace not found: {workspace}"
                )
            logger.info("Connecting to existing Landlock sandbox: %s", sandbox_id)
            return LandlockSandbox(
                workspace,
                sandbox_id=sandbox_id,
                **self._common_kwargs(),
            )

        sid = f"ll-{uuid.uuid4().hex[:12]}"
        workspace = self._root / sid
        logger.info("Creating Landlock sandbox: %s at %s", sid, workspace)
        return LandlockSandbox(
            workspace,
            sandbox_id=sid,
            **self._common_kwargs(),
        )

    def delete(self, sandbox_id: str) -> None:
        """Remove a sandbox workspace."""
        workspace = self._root / sandbox_id
        if workspace.exists():
            shutil.rmtree(workspace)
            logger.info("Deleted Landlock sandbox: %s", sandbox_id)
        else:
            logger.warning("Sandbox not found for deletion: %s", sandbox_id)

    def _common_kwargs(self) -> dict:
        kwargs: dict = {}
        extra_ro = os.environ.get("LANDLOCK_EXTRA_RO_PATHS")
        if extra_ro:
            kwargs["extra_ro_paths"] = [p.strip() for p in extra_ro.split(",")]
        extra_rw = os.environ.get("LANDLOCK_EXTRA_RW_PATHS")
        if extra_rw:
            kwargs["extra_rw_paths"] = [p.strip() for p in extra_rw.split(",")]
        enabled_env = os.environ.get("LANDLOCK_ENABLED")
        if enabled_env is not None:
            kwargs["enable_landlock"] = enabled_env != "0"
        return kwargs
