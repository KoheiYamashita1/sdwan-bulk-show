"""Filesystem helpers for the web UI.

The web UI never owns its own database; it just leans on the existing
``logs/<timestamp>/`` layout produced by ``run_on_vmanage.py`` and
augments it with a small ``manifest.json`` per run. This module isolates
that filesystem layer so the FastAPI handlers stay short and testable.

Security note: every path coming in from the browser is normalised through
:func:`safe_run_dir` / :func:`safe_file_path`, which resolve symlinks and
verify the resolved location is still inside ``logs/``. This stops a
crafted ``..`` or symlink from reading arbitrary files.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .runner import LOGS_DIR, REPO_ROOT

# A run dir is named like ``20260502_031530`` (UTC-naive local timestamp).
# We refuse to even glance at directories that don't match this shape;
# anything else under ``logs/`` is by definition foreign and unsafe.
TIMESTAMP_RE = re.compile(r"^\d{8}_\d{6}$")

# Bytes cap when streaming an output file into the browser. 5 MiB keeps
# the UI snappy and prevents accidental DoS from a runaway show command
# (the largest realistic ``show ip route`` capture sits well under this).
MAX_VIEW_BYTES = 5 * 1024 * 1024


class StorageError(Exception):
    """Raised when a request resolves outside the ``logs/`` sandbox."""


@dataclass
class RunSummary:
    """Lightweight view of a single ``logs/<timestamp>/`` directory."""

    timestamp: str
    path: Path
    manifest: Optional[dict]
    file_count: int

    @property
    def status(self) -> str:
        if self.manifest is None:
            # Old runs that pre-date the web UI never get a manifest.
            return "legacy"
        return str(self.manifest.get("status", "unknown"))

    @property
    def vmanage_host(self) -> str:
        if not self.manifest:
            return ""
        return str(self.manifest.get("vmanage_host", ""))

    @property
    def returncode(self) -> Optional[int]:
        if not self.manifest:
            return None
        rc = self.manifest.get("returncode")
        return int(rc) if isinstance(rc, int) else None


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------


def list_runs(*, limit: Optional[int] = None) -> list[RunSummary]:
    """Return every run dir, newest first.

    Non-conforming directories (anything outside ``YYYYMMDD_HHMMSS``) are
    silently skipped. ``limit`` caps the result for the index page.
    """

    if not LOGS_DIR.is_dir():
        return []
    summaries: list[RunSummary] = []
    for entry in sorted(LOGS_DIR.iterdir(), reverse=True):
        if not entry.is_dir():
            continue
        if entry.is_symlink():
            # Don't follow symlinks - they could escape `logs/`.
            continue
        if not TIMESTAMP_RE.match(entry.name):
            continue
        summaries.append(_summarise_run(entry))
        if limit is not None and len(summaries) >= limit:
            break
    return summaries


def get_run(timestamp: str) -> RunSummary:
    """Return the :class:`RunSummary` for ``timestamp`` or raise."""

    run_dir = safe_run_dir(timestamp)
    return _summarise_run(run_dir)


def safe_run_dir(timestamp: str) -> Path:
    """Resolve ``logs/<timestamp>/`` while refusing path traversal."""

    if not timestamp or not TIMESTAMP_RE.match(timestamp):
        raise StorageError(f"invalid timestamp: {timestamp!r}")
    candidate = LOGS_DIR / timestamp
    base = LOGS_DIR.resolve()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise StorageError(f"no such run: {timestamp}") from exc
    if not _is_inside(resolved, base):
        raise StorageError(f"run dir escapes logs/: {timestamp}")
    if not resolved.is_dir():
        raise StorageError(f"run path is not a directory: {timestamp}")
    return resolved


def list_run_files(timestamp: str) -> list[str]:
    """Return every regular file in ``logs/<timestamp>/`` (sorted)."""

    run_dir = safe_run_dir(timestamp)
    files: list[str] = []
    for entry in sorted(run_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        files.append(entry.name)
    return files


# ---------------------------------------------------------------------------
# File viewing
# ---------------------------------------------------------------------------


def safe_file_path(timestamp: str, filename: str) -> Path:
    """Resolve ``logs/<timestamp>/<filename>`` with strict path checks."""

    if not filename:
        raise StorageError("filename is required")
    # Filenames coming from the URL are kept syntactic only; we explicitly
    # forbid path separators and parent-dir refs before touching the FS.
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise StorageError(f"invalid filename: {filename!r}")
    run_dir = safe_run_dir(timestamp)
    target = run_dir / filename
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError as exc:
        raise StorageError(f"no such file: {filename}") from exc
    if not _is_inside(resolved, run_dir):
        raise StorageError(f"file escapes run dir: {filename}")
    if resolved.is_symlink():
        raise StorageError(f"symlink not allowed: {filename}")
    if not resolved.is_file():
        raise StorageError(f"not a regular file: {filename}")
    return resolved


def read_file_text(timestamp: str, filename: str, *, max_bytes: int = MAX_VIEW_BYTES) -> tuple[str, bool]:
    """Return ``(text, truncated)`` for safe in-browser display."""

    path = safe_file_path(timestamp, filename)
    raw = path.read_bytes()
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes]
        truncated = True
    # Replace undecodable bytes so the template never blows up on weird
    # bytes from a flaky session capture.
    return raw.decode("utf-8", errors="replace"), truncated


def build_unified_diff(
    a_name: str,
    a_text: str,
    b_name: str,
    b_text: str,
    *,
    a_truncated: bool = False,
    b_truncated: bool = False,
) -> dict:
    """Build the JSON payload for a unified diff of two already-read files.

    Pure (no filesystem / network access) so it can be unit-tested in
    isolation. ``a_text`` / ``b_text`` are the file bodies as returned by
    :func:`read_file_text` (already bounded by ``MAX_VIEW_BYTES``). The
    returned ``diff`` list holds plain-text unified-diff lines; the browser
    colourises them by leading character. ``identical`` is ``True`` when the
    two bodies are byte-for-byte equal (i.e. ``unified_diff`` yields nothing).
    """

    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()
    diff = list(
        difflib.unified_diff(
            a_lines, b_lines, fromfile=a_name, tofile=b_name, lineterm=""
        )
    )
    return {
        "a": a_name,
        "b": b_name,
        "a_truncated": bool(a_truncated),
        "b_truncated": bool(b_truncated),
        "diff": diff,
        "identical": not diff,
    }


def diff_files(
    timestamp: str,
    a_name: str,
    b_name: str,
    *,
    max_bytes: int = MAX_VIEW_BYTES,
) -> dict:
    """Resolve, read, and diff two files in ``logs/<timestamp>/``.

    Both files are resolved through :func:`read_file_text`, so path-traversal
    safety and ``MAX_VIEW_BYTES`` truncation are inherited. Raises
    :class:`StorageError` if either file is missing or unsafe.
    """

    a_text, a_truncated = read_file_text(timestamp, a_name, max_bytes=max_bytes)
    b_text, b_truncated = read_file_text(timestamp, b_name, max_bytes=max_bytes)
    return build_unified_diff(
        a_name,
        a_text,
        b_name,
        b_text,
        a_truncated=a_truncated,
        b_truncated=b_truncated,
    )


def read_manifest(timestamp: str) -> Optional[dict]:
    """Return the parsed ``manifest.json`` for ``timestamp`` or ``None``."""

    run_dir = safe_run_dir(timestamp)
    manifest = run_dir / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _summarise_run(run_dir: Path) -> RunSummary:
    manifest_path = run_dir / "manifest.json"
    manifest: Optional[dict] = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = None
    file_count = sum(
        1
        for entry in run_dir.iterdir()
        if entry.is_file() and not entry.is_symlink()
    )
    return RunSummary(
        timestamp=run_dir.name,
        path=run_dir,
        manifest=manifest,
        file_count=file_count,
    )


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


__all__ = [
    "MAX_VIEW_BYTES",
    "RunSummary",
    "StorageError",
    "TIMESTAMP_RE",
    "build_unified_diff",
    "diff_files",
    "get_run",
    "list_run_files",
    "list_runs",
    "read_file_text",
    "read_manifest",
    "safe_file_path",
    "safe_run_dir",
]
