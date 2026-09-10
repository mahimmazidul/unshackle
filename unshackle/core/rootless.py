"""Rootless / seedbox environment detection and setup.

Runs once at startup. It (a) detects whether unshackle is running as root,
(b) makes sure every writable directory it will use exists, (c) points Python's
``tempfile`` module at a user-local cache dir instead of the shared ``/tmp``
(seedboxes often purge ``/tmp`` or give it very little space), and (d) reports
the available external tools and any limits that matter on shared hosts.

Nothing here raises: a missing tool or an unwritable path downgrades to a
warning so the CLI always starts. This keeps one codebase working both with
and without root, without a fork.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from unshackle.core.config import config

log = logging.getLogger("rootless")

# Core external binaries the downloader depends on. Cheap PATH lookups only.
_CORE_BINARIES = (
    ("ffmpeg", ("ffmpeg",)),
    ("ffprobe", ("ffprobe",)),
    ("mkvmerge", ("mkvmerge",)),
    ("mkvpropedit", ("mkvpropedit",)),
    ("shaka-packager", ("shaka-packager", "packager")),
    ("git", ("git",)),
)

# Writable runtime directories (all overridable via unshackle.yaml).
_WRITABLE_DIRS = (
    "downloads",
    "temp",
    "cache",
    "cookies",
    "logs",
    "exports",
    "wvds",
    "prds",
    "dcsl",
)


def _first_which(names: tuple[str, ...]) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def rootless_check() -> dict:
    """Detect the environment and prepare rootless-friendly paths. Returns a facts dict.

    Never raises: every check is guarded so the CLI always starts.
    """
    facts: dict = {}

    # 1. Privilege level.
    try:
        facts["uid"] = os.geteuid()
    except AttributeError:  # geteuid is not available on Windows
        facts["uid"] = None
    facts["is_root"] = facts.get("uid") == 0
    facts["rootless_mode"] = not facts["is_root"]

    # 2. Make sure the writable directories exist.
    unwritable = []
    for name in _WRITABLE_DIRS:
        path = getattr(config.directories, name, None)
        if not isinstance(path, Path):
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            unwritable.append(f"{name} ({path}): {e}")
    for entry in unwritable:
        log.warning("Directory unavailable, some features may fail: %s", entry)
    facts["unwritable_dirs"] = unwritable

    # 3. Point tempfile at a user-local cache dir so nothing lands in shared /tmp.
    try:
        tmp_dir = Path(config.directories.cache) / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(tmp_dir)
        facts["tempdir"] = str(tmp_dir)
    except OSError as e:
        log.warning("Could not redirect temp files to the user cache dir: %s", e)

    # 4. Available external tools.
    tools: dict[str, str | None] = {}
    for label, names in _CORE_BINARIES:
        tools[label] = _first_which(names)
    missing_tools = [label for label, path in tools.items() if not path]
    if missing_tools:
        log.warning(
            "Missing core tools: %s. Install them on your PATH (see ROOTLESS_SETUP.md).",
            ", ".join(missing_tools),
        )
    facts["tools"] = tools

    # 5. Process-count limit (shared hosts often cap RLIMIT_NPROC).
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
        facts["nproc_soft"] = soft
        if soft != resource.RLIM_INFINITY and soft < 256:
            log.warning("RLIMIT_NPROC is low (%s); many concurrent downloads may fail.", soft)
    except (ImportError, OSError, ValueError):
        pass

    # Resolved paths are visible on demand via `unshackle env check`/`env info`;
    # keep them out of normal startup output (debug-only).
    log.debug(
        "Environment: %s (uid=%s) - config=%s, data=%s, cache=%s, downloads=%s",
        "rootless" if facts["rootless_mode"] else "root",
        facts.get("uid"),
        getattr(config.directories, "user_configs", None),
        getattr(config.directories, "data", None),
        getattr(config.directories, "cache", None),
        getattr(config.directories, "downloads", None),
    )
    return facts


__all__ = ("rootless_check",)
