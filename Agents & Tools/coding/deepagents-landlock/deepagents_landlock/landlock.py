"""Zero-dependency Landlock LSM wrapper using ctypes.

Provides kernel-level filesystem sandboxing on Linux >= 5.13.
On unsupported platforms (macOS, older kernels), ``is_supported()`` returns
False and ``apply()`` is a no-op.

Usage::

    from deepagents_landlock.landlock import apply, is_supported

    if is_supported():
        apply({
            "/tmp/workspace": FS_READ | FS_WRITE,
            "/usr": FS_READ | FS_EXECUTE,
        })
        # Current process + all children are now restricted
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Landlock access flags (filesystem)
# ---------------------------------------------------------------------------

FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13      # ABI v2, kernel 5.19+
FS_TRUNCATE = 1 << 14   # ABI v3, kernel 6.2+

# Convenience groups
FS_READ = FS_READ_FILE | FS_READ_DIR
FS_WRITE = FS_WRITE_FILE | FS_MAKE_REG | FS_MAKE_DIR | FS_MAKE_SYM
FS_READ_WRITE = FS_READ | FS_WRITE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_TRUNCATE | FS_REFER
FS_READ_EXECUTE = FS_READ | FS_EXECUTE

# All filesystem access types for v1 (13 types)
_FS_ALL_V1 = (
    FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_READ_DIR
    | FS_REMOVE_DIR | FS_REMOVE_FILE
    | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG
    | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM
)

_FS_ALL_V2 = _FS_ALL_V1 | FS_REFER
_FS_ALL_V3 = _FS_ALL_V2 | FS_TRUNCATE

# ---------------------------------------------------------------------------
# Syscall numbers (x86_64 / aarch64 — same since 5.13)
# ---------------------------------------------------------------------------

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_RULE_PATH_BENEATH = 1

_PR_SET_NO_NEW_PRIVS = 38

# ---------------------------------------------------------------------------
# ctypes structures matching kernel UAPI
# ---------------------------------------------------------------------------


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


# ---------------------------------------------------------------------------
# libc handle
# ---------------------------------------------------------------------------

_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        lib_name = ctypes.util.find_library("c")
        if lib_name is None:
            raise OSError("Cannot find libc")
        _libc = ctypes.CDLL(lib_name, use_errno=True)
    return _libc


def _check(ret: int, name: str) -> int:
    """Raise OSError if syscall returned an error."""
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"{name}: {os.strerror(errno)}")
    return ret


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_supported() -> bool:
    """Check if the running kernel supports Landlock.

    Probes by attempting ``landlock_create_ruleset`` with flags=1
    (LANDLOCK_CREATE_RULESET_VERSION query).  Returns False on non-Linux
    or if the kernel returns ENOSYS/EOPNOTSUPP.
    """
    if sys.platform != "linux":
        return False
    try:
        libc = _get_libc()
        ret = libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(1),  # LANDLOCK_CREATE_RULESET_VERSION
        )
        if ret >= 0:
            return True
        errno = ctypes.get_errno()
        # ENOSYS = not compiled in; EOPNOTSUPP = compiled but disabled
        return errno not in (38, 95)  # ENOSYS, EOPNOTSUPP
    except Exception:
        return False


def get_abi_version() -> int:
    """Return the Landlock ABI version, or 0 if unsupported."""
    if sys.platform != "linux":
        return 0
    try:
        libc = _get_libc()
        ret = libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(1),
        )
        return ret if ret > 0 else 0
    except Exception:
        return 0


def apply(rules: dict[str, int]) -> None:
    """Apply Landlock filesystem restrictions to the current process.

    Parameters
    ----------
    rules : dict[str, int]
        Mapping of ``{path: access_flags}``.  Only listed paths with their
        specified flags are allowed; everything else is denied.

    Raises
    ------
    OSError
        If a Landlock syscall fails.
    RuntimeError
        If called on an unsupported platform.

    Notes
    -----
    This call is **irreversible**: the current process and all future children
    will be permanently restricted.  Multiple calls further tighten the
    restrictions (intersection semantics).
    """
    if not rules:
        return

    if sys.platform != "linux":
        raise RuntimeError("Landlock is only available on Linux")

    libc = _get_libc()

    # Determine best ABI version
    abi = get_abi_version()
    if abi >= 3:
        handled = _FS_ALL_V3
    elif abi >= 2:
        handled = _FS_ALL_V2
    else:
        handled = _FS_ALL_V1

    # Step 1: create ruleset
    attr = _RulesetAttr(handled_access_fs=handled)
    ruleset_fd = _check(
        libc.syscall(
            _SYS_LANDLOCK_CREATE_RULESET,
            ctypes.byref(attr),
            ctypes.c_size_t(ctypes.sizeof(attr)),
            ctypes.c_uint32(0),
        ),
        "landlock_create_ruleset",
    )

    try:
        # Step 2: add rules for each path
        for path, access in rules.items():
            if not os.path.exists(path):
                logger.debug("Skipping non-existent path: %s", path)
                continue

            # Mask access to what the current ABI version handles
            effective_access = access & handled
            if effective_access == 0:
                continue

            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                path_attr = _PathBeneathAttr(
                    allowed_access=effective_access,
                    parent_fd=fd,
                )
                _check(
                    libc.syscall(
                        _SYS_LANDLOCK_ADD_RULE,
                        ctypes.c_int(ruleset_fd),
                        ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                        ctypes.byref(path_attr),
                        ctypes.c_uint32(0),
                    ),
                    f"landlock_add_rule({path})",
                )
            finally:
                os.close(fd)

        # Step 3: restrict self
        _check(
            libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0),
            "prctl(PR_SET_NO_NEW_PRIVS)",
        )
        _check(
            libc.syscall(
                _SYS_LANDLOCK_RESTRICT_SELF,
                ctypes.c_int(ruleset_fd),
                ctypes.c_uint32(0),
            ),
            "landlock_restrict_self",
        )
    finally:
        os.close(ruleset_fd)

    logger.info(
        "Landlock applied: %d rules, ABI v%d",
        len([p for p in rules if os.path.exists(p)]),
        abi,
    )
