"""Subprocess wrapper around ``run_on_vmanage.py``.

The web UI never speaks SSH itself; it spawns the existing CLI in a
subprocess, pipes the password through ``stdin`` (mirroring the manual
``printf 'pw' | python run_on_vmanage.py ...`` workflow), and then promotes
the resulting log directory into the canonical ``logs/<timestamp>/`` layout
that the rest of the project already uses.

Notable design choices:

* **No password on disk.** ``hosts.txt`` and ``commands.txt`` are written to
  a private ``tempfile.mkdtemp`` directory with mode ``0o600``; the password
  itself is sent over the subprocess's stdin and never touches the file
  system.
* **bulk-show.py is symlinked** into the tempdir so ``run_on_vmanage.py``
  can find it next to the input files without us copying bytes around.
* **Single concurrent run.** ``RUN_LOCK`` serialises calls so two browser
  submits never race on the same vManage shell.
* **Timestamp comes from the subprocess.** ``run_on_vmanage.py`` generates
  the ``%Y%m%d_%H%M%S`` directory name itself; we read it back from the
  tempdir's ``logs/`` listing rather than guessing or parsing stdout.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
BULK_SCRIPT = REPO_ROOT / "bulk-show.py"
RUN_ON_VMANAGE = REPO_ROOT / "run_on_vmanage.py"

# Default subprocess wall-clock timeout (seconds). Generous enough for ~50
# edges with a 600 s per-host script timeout but still finite so the lock is
# not held forever if vManage hangs.
DEFAULT_RUN_TIMEOUT = 1800.0

# Per-input safety cap so a copy/paste accident cannot fill the disk.
MAX_INPUT_BYTES = 1 * 1024 * 1024  # 1 MiB

# Used by `_detect_timestamp_dir` as a fall-back when no logs/ subdir was
# produced (e.g. the subprocess died before generating any output).
_TS_FROM_STDOUT_RE = re.compile(r"using remote dir:\s*\S+/(\d{8}_\d{6})")
_TS_FALLBACK_FORMAT = "%Y%m%d_%H%M%S"


# ---------------------------------------------------------------------------
# Concurrency primitive
# ---------------------------------------------------------------------------

# Serialise runs so that two browser submits cannot race the same vManage
# shell. ``acquire(blocking=False)`` lets the FastAPI handler answer with a
# friendly "busy" message instead of stacking requests indefinitely.
RUN_LOCK = threading.Lock()


class RunBusyError(RuntimeError):
    """Raised when another run is already in progress."""


class RunInputError(ValueError):
    """Raised when the submitted form fails server-side validation."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunForm:
    """Validated form submission ready to be turned into a subprocess call."""

    vmanage_host: str
    user: str
    password: str
    remote_dir: str
    hosts_text: str
    commands_text: str
    download_outputs: bool = True
    verbose: bool = False
    reject_unknown_hosts: bool = False

    def hosts_count(self) -> int:
        return _count_non_empty_lines(self.hosts_text)

    def commands_count(self) -> int:
        return _count_non_empty_lines(self.commands_text)


@dataclass
class RunResult:
    """Outcome of a single subprocess invocation."""

    timestamp: str
    returncode: int
    log: str
    manifest_path: Path
    log_dir: Path
    duration_sec: float
    started_at: str
    ended_at: str
    timed_out: bool = False
    output_files: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_form(form: RunForm) -> None:
    """Reject obviously broken submissions before we spawn anything."""

    if not form.vmanage_host or not form.vmanage_host.strip():
        raise RunInputError("vManage host is required.")
    if not form.user or not form.user.strip():
        raise RunInputError("SSH user is required.")
    if not form.password:
        raise RunInputError("SSH password is required.")
    if not form.remote_dir or not form.remote_dir.strip():
        raise RunInputError("Remote dir is required.")

    if len(form.hosts_text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RunInputError(
            f"hosts text exceeds the {MAX_INPUT_BYTES} byte safety cap."
        )
    if len(form.commands_text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RunInputError(
            f"commands text exceeds the {MAX_INPUT_BYTES} byte safety cap."
        )
    if form.hosts_count() == 0:
        raise RunInputError("hosts text must contain at least one IP,user,pass row.")
    if form.commands_count() == 0:
        raise RunInputError("commands text must contain at least one CLI command.")


def run_via_vmanage(
    form: RunForm,
    *,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    repo_root: Path | None = None,
    bulk_script: Path | None = None,
    run_on_vmanage: Path | None = None,
    python_executable: str | None = None,
) -> RunResult:
    """Spawn ``run_on_vmanage.py`` and wait for it to finish.

    Parameters are kept overridable so the unit tests can point them at a
    fake script and an isolated repo root.
    """

    repo_root = repo_root or REPO_ROOT
    bulk_script = bulk_script or BULK_SCRIPT
    run_on_vmanage = run_on_vmanage or RUN_ON_VMANAGE
    python_executable = python_executable or sys.executable
    logs_dir = repo_root / "logs"

    validate_form(form)

    if not RUN_LOCK.acquire(blocking=False):
        raise RunBusyError("Another run is currently in progress.")

    started_monotonic = time.monotonic()
    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    timed_out = False
    tempdir = Path(tempfile.mkdtemp(prefix="sdwan-webapp-"))

    try:
        hosts_path = tempdir / "host.txt"
        commands_path = tempdir / "command.txt"
        bulk_link = tempdir / bulk_script.name

        # 0o600 keeps the credentials embedded in host.txt off other shell
        # users. We write with mode=0o600 atomically by combining open + os.
        _write_secure_text(hosts_path, form.hosts_text)
        _write_secure_text(commands_path, form.commands_text)

        # Symlink avoids copying the script every run while letting
        # run_on_vmanage.py treat the tempdir as a normal --local-dir.
        if not bulk_script.exists():
            raise RunInputError(
                f"bulk-show.py not found at {bulk_script}; cannot run remote."
            )
        try:
            os.symlink(bulk_script, bulk_link)
        except OSError as exc:  # pragma: no cover - extremely rare on Mac
            shutil.copyfile(bulk_script, bulk_link)
            logger.debug("symlink failed (%s); copied bulk-show.py instead", exc)

        argv = _build_argv(
            python_executable=python_executable,
            run_on_vmanage=run_on_vmanage,
            form=form,
            tempdir=tempdir,
            hosts_name=hosts_path.name,
            commands_name=commands_path.name,
            bulk_name=bulk_link.name,
        )

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")

        proc = subprocess.Popen(
            argv,
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        try:
            stdout, _ = proc.communicate(input=form.password + "\n", timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                stdout, _ = proc.communicate(timeout=10.0)
            except subprocess.TimeoutExpired:
                stdout = ""
            returncode = proc.returncode if proc.returncode is not None else -1
            stdout = (stdout or "") + (
                f"\n[webapp] subprocess timed out after {timeout:.0f} s; killed."
            )

        # Mask the password before we hand any text back to callers / write
        # it to disk. Even with stdin piping, an unlucky stack trace could
        # echo it; defence in depth.
        masked_stdout = _mask_password(stdout, form.password)

        ended_monotonic = time.monotonic()
        ended_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        duration_sec = round(ended_monotonic - started_monotonic, 3)

        timestamp = _detect_timestamp_dir(tempdir / "logs", masked_stdout)
        target_dir = logs_dir / timestamp
        target_dir.mkdir(parents=True, exist_ok=True)

        output_files = _promote_outputs(tempdir / "logs" / timestamp, target_dir)

        run_log_path = target_dir / "run.log"
        run_log_path.write_text(masked_stdout, encoding="utf-8")

        manifest_path = target_dir / "manifest.json"
        manifest = _build_manifest(
            timestamp=timestamp,
            form=form,
            returncode=returncode,
            duration_sec=duration_sec,
            started_at=started_at,
            ended_at=ended_at,
            output_files=output_files,
            timed_out=timed_out,
        )
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return RunResult(
            timestamp=timestamp,
            returncode=returncode,
            log=masked_stdout,
            manifest_path=manifest_path,
            log_dir=target_dir,
            duration_sec=duration_sec,
            started_at=started_at,
            ended_at=ended_at,
            timed_out=timed_out,
            output_files=output_files,
        )
    finally:
        # Tempdir is best-effort cleanup; we do NOT want a stale tempdir to
        # persist the password embedded in host.txt across runs.
        try:
            shutil.rmtree(tempdir, ignore_errors=True)
        finally:
            RUN_LOCK.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_argv(
    *,
    python_executable: str,
    run_on_vmanage: Path,
    form: RunForm,
    tempdir: Path,
    hosts_name: str,
    commands_name: str,
    bulk_name: str,
) -> list[str]:
    argv = [
        python_executable,
        str(run_on_vmanage),
        form.vmanage_host.strip(),
        "--user",
        form.user.strip(),
        "--remote-dir",
        form.remote_dir.strip(),
        "--local-dir",
        str(tempdir),
        "--hosts",
        hosts_name,
        "--commands",
        commands_name,
        "--bulk-script",
        bulk_name,
    ]
    if form.download_outputs:
        argv.append("--download-outputs")
    if form.verbose:
        argv.append("--verbose")
    if form.reject_unknown_hosts:
        argv.append("--reject-unknown-hosts")
    return argv


def _write_secure_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600 (owner read/write only)."""

    # ``os.open`` lets us pass the mode atomically; ``Path.write_text``
    # would create with default umask first, which briefly leaks the
    # password to other local users on the box.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        # If write fails after fd opened we still need to close it; fdopen
        # owns the fd so we only get here on write errors.
        raise


def _detect_timestamp_dir(local_logs_root: Path, stdout: str) -> str:
    """Determine which ``%Y%m%d_%H%M%S`` directory the subprocess produced.

    Order of preference:
    1. Single subdirectory under ``<tempdir>/logs/`` (most reliable).
    2. ``using remote dir: ...`` line in the merged stdout/stderr.
    3. Fresh local timestamp (the run failed before producing artifacts;
       we still want a folder to file the run.log under).
    """

    if local_logs_root.is_dir():
        candidates = sorted(
            (entry.name for entry in local_logs_root.iterdir() if entry.is_dir()),
            reverse=True,
        )
        if candidates:
            return candidates[0]

    match = _TS_FROM_STDOUT_RE.search(stdout)
    if match:
        return match.group(1)

    return time.strftime(_TS_FALLBACK_FORMAT)


def _promote_outputs(source_dir: Path, target_dir: Path) -> list[str]:
    """Move ``output_*`` files from the tempdir into the canonical logs dir."""

    moved: list[str] = []
    if not source_dir.is_dir():
        return moved
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.startswith("output_"):
            continue
        dest = target_dir / entry.name
        # ``shutil.move`` falls back to copy+unlink across filesystems, so
        # tempdir / logs/ on different volumes still works.
        shutil.move(str(entry), str(dest))
        moved.append(entry.name)
    return moved


def _build_manifest(
    *,
    timestamp: str,
    form: RunForm,
    returncode: int,
    duration_sec: float,
    started_at: str,
    ended_at: str,
    output_files: list[str],
    timed_out: bool,
) -> dict:
    if timed_out:
        status = "timeout"
    elif returncode == 0:
        status = "success"
    else:
        status = "failed"
    return {
        "timestamp": timestamp,
        "vmanage_host": form.vmanage_host.strip(),
        "vmanage_user": form.user.strip(),
        "remote_dir": form.remote_dir.strip(),
        "hosts_count": form.hosts_count(),
        "commands_count": form.commands_count(),
        "options": {
            "download_outputs": form.download_outputs,
            "verbose": form.verbose,
            "reject_unknown_hosts": form.reject_unknown_hosts,
        },
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": duration_sec,
        "returncode": returncode,
        "outputs_count": len(output_files),
        "outputs": output_files,
        "status": status,
    }


def _mask_password(text: str, password: str) -> str:
    """Replace ``password`` with ``***`` everywhere in ``text``."""

    if not password:
        return text
    # An empty password should never reach us (validate_form blocks it) but
    # ``str.replace`` on "" would loop forever in some implementations, so
    # be defensive.
    return text.replace(password, "***")


def _count_non_empty_lines(text: str) -> int:
    """Count rows that the CLI would treat as real input.

    Both ``bulk-show.py`` and ``run_on_vmanage.py`` skip blank lines and any
    line whose first non-whitespace character is ``#``. Mirroring that here
    means a paste of "just comments" is correctly rejected at validation time
    instead of spawning a subprocess that processes zero hosts/commands.
    """

    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Re-exports for tests
# ---------------------------------------------------------------------------


def _serialise_result(result: RunResult) -> dict:
    """Return ``result`` as a JSON-friendly dict (handy for debugging)."""

    payload = asdict(result)
    payload["manifest_path"] = str(result.manifest_path)
    payload["log_dir"] = str(result.log_dir)
    return payload


__all__ = [
    "DEFAULT_RUN_TIMEOUT",
    "LOGS_DIR",
    "MAX_INPUT_BYTES",
    "REPO_ROOT",
    "RUN_LOCK",
    "RunBusyError",
    "RunForm",
    "RunInputError",
    "RunResult",
    "run_via_vmanage",
    "validate_form",
]
