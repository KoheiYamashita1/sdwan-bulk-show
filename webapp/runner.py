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

import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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

# Allow-list validators for the two free-text fields that ultimately flow into
# the remote ``run_on_vmanage.py`` / ``bulk-show.py`` invocation. They are
# defence-in-depth: ``run_on_vmanage.py`` also ``shlex.quote``s these before
# building the remote vshell command, and we never hand user text to a shell
# on the Mac side (``shell=False`` everywhere).
#
# ``remote_dir`` must look like an absolute or home-relative POSIX path made of
# word characters, dots, slashes and hyphens — explicitly excluding shell
# metacharacters (``; | & $ `` and whitespace).
# ``\Z`` (not ``$``) so an embedded/trailing newline cannot sneak past — ``$``
# also matches just before a final newline.
REMOTE_DIR_RE = re.compile(r"^[~/][\w./-]*\Z")

# ``vmanage_host`` must be an IPv4 literal or a DNS hostname (letters, digits,
# dots and hyphens, 1-253 chars). This rejects spaces and shell metacharacters.
VMANAGE_HOST_RE = re.compile(
    r"^(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\Z"
)

# Recognised per-host output formats forwarded to bulk-show.py's
# ``--output-format``.
OUTPUT_FORMATS = ("text", "json", "csv")

# Upper bounds for the wired-through bulk-show.py knobs (sanity caps so a
# fat-fingered form can't spawn thousands of workers or retry forever).
MAX_RETRIES = 10
MAX_WORKERS_CAP = 100

# Job-registry bounds (B2): keep at most ``MAX_JOBS`` and evict terminal jobs
# older than ``JOB_TTL_SECONDS`` so a long-lived process does not accumulate
# unbounded job state. Both are module-level so tests can shrink them.
MAX_JOBS = 50
JOB_TTL_SECONDS = 3600.0

# Statuses a job can never leave once reached.
TERMINAL_STATUSES = frozenset(
    {"success", "failed", "timeout", "error", "cancelled"}
)

# Grace period between SIGTERM and SIGKILL when tearing down a run's process
# group (B1).
_KILL_GRACE_SECONDS = 3.0

# Used by `_detect_timestamp_dir` as a fall-back when no logs/ subdir was
# produced (e.g. the subprocess died before generating any output).
_TS_FROM_STDOUT_RE = re.compile(r"using remote dir:\s*\S+/(\d{8}_\d{6})")
_TS_FALLBACK_FORMAT = "%Y%m%d_%H%M%S"

# How many (masked) stdout lines a RunJob keeps for the live "log tail".
LOG_TAIL_MAX = 50

# Progress-parsing regexes. ``run_on_vmanage.py --verbose`` and the remote
# ``bulk-show.py`` emit recognisable milestone lines we map to a percentage.
#  * ``[main] starting <N> host(s) ...``  -> refine hosts_total
#  * ``[<ip>] done: <cmd>``               -> a single command finished
#  * ``[main] done: success=...``         -> bulk-show wrapped up
#  * ``[<vmanage>] done``                 -> run_on_vmanage wrapped up
# ``_RE_CMD_DONE`` deliberately excludes ``[main]`` so the per-command counter
# is not bumped by bulk-show's final ``[main] done:`` summary line.
_RE_STARTING = re.compile(r"starting\s+(\d+)\s+host", re.IGNORECASE)
_RE_CMD_DONE = re.compile(r"^\s*\[(?!main\])[^\]]+\]\s+done:\s")
_RE_MAIN_DONE = re.compile(r"\[main\]\s+done:")
_RE_VMANAGE_DONE = re.compile(r"\]\s+done\s*$")


def _now_iso() -> str:
    """Local-timezone ISO-8601 timestamp (seconds resolution)."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
    """Validated form submission ready to be turned into a subprocess call.

    Commands are split per device type: ``controller_commands_text`` runs on
    controllers (vBond/vSmart/vEdge) and ``edge_commands_text`` runs on edges
    (cEdge/IOS-XE). ``commands_text`` is retained for backward compatibility
    and acts as a shared fallback when one (or both) of the split boxes is
    empty.

    Fallback rule (per device type): use that type's own box; if it is empty,
    fall back to the other non-empty box; if both are empty, fall back to the
    legacy ``commands_text``. ``validate_form`` rejects submissions where all
    three are empty.
    """

    vmanage_host: str
    user: str
    password: str
    remote_dir: str
    hosts_text: str
    commands_text: str = ""
    controller_commands_text: str = ""
    edge_commands_text: str = ""
    download_outputs: bool = True
    verbose: bool = False
    # Default ON: host-key checking is the safe default. Wave 2 flips the UI
    # checkbox; the backend default is strict so callers that omit the flag
    # (and the forwarded remote invocation) verify SSH host keys.
    reject_unknown_hosts: bool = True
    # bulk-show.py knobs wired end to end (C3). ``output_formats`` is the
    # subset of {"text","json","csv"} to emit per host (default text only).
    retries: int = 0
    max_workers: Optional[int] = None
    output_formats: list[str] = field(default_factory=lambda: ["text"])
    controller_port: int = 22

    def hosts_count(self) -> int:
        return _count_non_empty_lines(self.hosts_text)

    def _effective(self, primary: str, secondary: str) -> str:
        """Apply the fallback rule: primary -> secondary -> legacy box."""

        for text in (primary, secondary, self.commands_text):
            if _count_non_empty_lines(text) > 0:
                return text
        return ""

    def controller_commands(self) -> str:
        """Effective command list controllers will run."""

        return self._effective(self.controller_commands_text, self.edge_commands_text)

    def edge_commands(self) -> str:
        """Effective command list edges will run."""

        return self._effective(self.edge_commands_text, self.controller_commands_text)

    def base_commands(self) -> str:
        """Shared fallback list written as bulk-show's positional commands file.

        Prefers the legacy ``commands_text`` so the positional file keeps its
        historical meaning, then the controller box, then the edge box. Always
        non-empty for a form that passed :func:`validate_form`.
        """

        for text in (
            self.commands_text,
            self.controller_commands_text,
            self.edge_commands_text,
        ):
            if _count_non_empty_lines(text) > 0:
                return text
        return ""

    def controller_commands_count(self) -> int:
        return _count_non_empty_lines(self.controller_commands())

    def edge_commands_count(self) -> int:
        return _count_non_empty_lines(self.edge_commands())

    def commands_count(self) -> int:
        """Backward-compatible "commands per host" figure (max of both types)."""

        return max(self.controller_commands_count(), self.edge_commands_count())

    def progress_command_total(self, bulk_script: Path | None = None) -> int:
        """Accurate progress denominator: sum over hosts of that host's count.

        Each host runs the command list matching its device type, so the total
        number of per-command "done" milestones is
        ``sum(commands_for(host.device_type) for host in hosts)``. Parsed with
        bulk-show's canonical ``parse_host_line`` when available; otherwise a
        best-effort estimate (hosts x max per-type count) is used.
        """

        bulk_script = bulk_script or BULK_SCRIPT
        controller_n = self.controller_commands_count()
        edge_n = self.edge_commands_count()
        parse = _load_parse_host_line(bulk_script)
        if parse is None:
            return self.hosts_count() * max(controller_n, edge_n, 1)
        total = 0
        for raw in self.hosts_text.splitlines():
            try:
                parsed = parse(raw)
            except Exception:  # noqa: BLE001 - malformed lines simply skipped
                continue
            if parsed is None:
                continue
            device_type = parsed[3]
            total += controller_n if device_type == "controller" else edge_n
        return total or self.hosts_count() * max(controller_n, edge_n, 1)


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
    cancelled: bool = False
    output_files: list[str] = field(default_factory=list)


@dataclass
class RunJob:
    """Live, mutable state of an asynchronous run.

    A worker thread mutates the job (via :meth:`update_from_line` while the
    subprocess streams, then :meth:`finish_from_result` / :meth:`fail` at the
    end); the polling endpoint reads it via :meth:`snapshot`. All access is
    guarded by ``_lock`` so the two threads never see a torn update.

    The job NEVER stores the raw password: ``update_from_line`` is only ever
    fed already-masked lines (see :func:`_stream_and_collect`).
    """

    job_id: str
    # running | success | failed | timeout | error | cancelled
    status: str = "running"
    phase: str = "Starting"
    percent: int = 0
    hosts_total: int = 0
    # Accurate progress denominator: the total number of per-command "done"
    # milestones expected across all hosts (sum of each host's per-type count).
    commands_total: int = 0
    commands_done: int = 0
    started_at: str = ""
    ended_at: Optional[str] = None
    message: str = ""
    log_tail: list[str] = field(default_factory=list)
    timestamp: Optional[str] = None
    returncode: Optional[int] = None
    error: Optional[str] = None
    # Cancellation plumbing (B3). ``cancel_requested`` is polled by the
    # streaming loop; ``proc`` is the live subprocess handle so
    # :func:`request_cancel` can kill the whole process group immediately.
    cancel_requested: bool = False
    proc: Optional["subprocess.Popen[str]"] = field(
        default=None, repr=False, compare=False
    )
    # Monotonic registration time, used by the registry eviction (B2).
    _registered_at: float = field(
        default_factory=time.monotonic, repr=False, compare=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @classmethod
    def new(cls, *, hosts_total: int, commands_total: int) -> "RunJob":
        """Create a fresh running job with a random ``job_id``."""

        return cls(
            job_id=uuid.uuid4().hex,
            started_at=_now_iso(),
            hosts_total=hosts_total,
            commands_total=commands_total,
        )

    # -- progress (called from the worker thread, one masked line at a time) --

    def update_from_line(self, line: str) -> None:
        """Fold a single (already password-masked) stdout line into progress.

        Best-effort and strictly monotonic: ``percent`` never moves backwards
        even if lines arrive out of the expected milestone order.
        """

        text = line.rstrip("\r\n")
        with self._lock:
            if text:
                self.log_tail.append(text)
                if len(self.log_tail) > LOG_TAIL_MAX:
                    del self.log_tail[:-LOG_TAIL_MAX]
                self.message = text

            match = _RE_STARTING.search(text)
            if match:
                try:
                    parsed = int(match.group(1))
                except ValueError:
                    parsed = 0
                if parsed > self.hosts_total:
                    self.hosts_total = parsed

            if _RE_CMD_DONE.search(text):
                self.commands_done += 1
                self._bump(self._fine_percent(), "Running on vManage")
                return
            if _RE_MAIN_DONE.search(text):
                self._bump(92, "Finalizing run")
                return

            low = text.lower()
            if "downloading" in low:
                self._bump(95, "Downloading outputs")
            elif _RE_VMANAGE_DONE.search(text):
                self._bump(98, "Wrapping up")
            elif "running via vshell" in low:
                self._bump(25, "Running on vManage")
            elif "uploading" in low:
                self._bump(20, "Uploading files")
            elif "connected" in low:
                self._bump(10, "Connected to vManage")
            elif "connecting" in low:
                self._bump(5, "Connecting to vManage")

    def _bump(self, percent: int, phase: str) -> None:
        """Advance ``percent``/``phase`` monotonically. Caller holds ``_lock``."""

        percent = max(0, min(100, int(percent)))
        if percent >= self.percent:
            self.percent = percent
            self.phase = phase

    def _fine_percent(self) -> int:
        """25%..90% scaled by how many commands have completed.

        ``commands_total`` is the accurate denominator (the sum over hosts of
        each host's per-type command count), so we use it directly rather than
        multiplying by ``hosts_total``.
        """

        total = max(1, self.commands_total)
        done = min(self.commands_done, total)
        return 25 + int(65 * (done / total))

    def set_proc(self, proc: "subprocess.Popen[str]") -> None:
        """Record the live subprocess handle for :func:`request_cancel`."""

        with self._lock:
            self.proc = proc

    # -- terminal transitions (called once when the subprocess exits) --------

    def finish_from_result(self, result: RunResult) -> None:
        """Mark the job done using the subprocess's :class:`RunResult`."""

        with self._lock:
            self.returncode = result.returncode
            self.timestamp = result.timestamp
            self.ended_at = result.ended_at
            self.proc = None
            if result.cancelled:
                self.status = "cancelled"
                self.phase = "Cancelled"
            elif result.timed_out:
                self.status = "timeout"
                self.phase = "Timed out"
            elif result.returncode == 0:
                self.status = "success"
                self.phase = "Done"
            else:
                self.status = "failed"
                self.phase = "Failed"
            # The subprocess has exited; only now do we claim 100%.
            self.percent = 100

    def fail(self, exc: BaseException, *, status: str = "error") -> None:
        """Mark the job as crashed (an exception escaped the worker)."""

        with self._lock:
            self.status = status
            self.error = str(exc)
            self.ended_at = _now_iso()
            self.phase = "Error"
            self.percent = 100
            self.proc = None

    def snapshot(self) -> dict:
        """Return a thread-safe, JSON-serialisable copy of the job state."""

        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "phase": self.phase,
                "percent": self.percent,
                "hosts_total": self.hosts_total,
                "commands_total": self.commands_total,
                "commands_done": self.commands_done,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "message": self.message,
                "log_tail": list(self.log_tail),
                "timestamp": self.timestamp,
                "returncode": self.returncode,
                "error": self.error,
            }


# ---------------------------------------------------------------------------
# In-memory job registry
# ---------------------------------------------------------------------------

# Maps ``job_id`` -> :class:`RunJob`. Lives only for the lifetime of the
# process (this is a local-only tool; runs are also persisted to disk under
# ``logs/<ts>/``). Guarded by ``_JOBS_LOCK`` for concurrent access from the
# worker threads and the polling endpoint.
_JOBS: dict[str, RunJob] = {}
_JOBS_LOCK = threading.Lock()


def _register_job(job: RunJob) -> None:
    with _JOBS_LOCK:
        _JOBS[job.job_id] = job
        _evict_jobs_locked()


def _evict_jobs_locked() -> None:
    """Bound the registry (B2). Caller MUST hold ``_JOBS_LOCK``.

    Two passes: first drop terminal jobs older than ``JOB_TTL_SECONDS``, then
    enforce the ``MAX_JOBS`` cap by discarding the oldest entries (dicts keep
    insertion order, so the first keys are the oldest registrations).
    """

    now = time.monotonic()
    for job_id, job in list(_JOBS.items()):
        with job._lock:
            is_terminal = job.status in TERMINAL_STATUSES
            age = now - job._registered_at
        if is_terminal and age > JOB_TTL_SECONDS:
            del _JOBS[job_id]

    while len(_JOBS) > MAX_JOBS:
        oldest_id = next(iter(_JOBS))
        del _JOBS[oldest_id]


def get_job(job_id: str) -> Optional[RunJob]:
    """Return the :class:`RunJob` for ``job_id`` or ``None`` if unknown."""

    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def request_cancel(job_id: str) -> Optional[str]:
    """Request cancellation of a running job (B3).

    Sets the job's ``cancel_requested`` flag and, if the subprocess is live,
    kills its whole process group immediately so the run stops promptly. The
    streaming loop notices the flag, drains, and the worker finalises the job
    with status ``"cancelled"`` (partial outputs / ``run.log`` preserved).

    Returns the resulting status string (``"cancelled"`` for a job we just
    asked to stop, or the existing terminal status for an already-finished
    job), or ``None`` when ``job_id`` is unknown.
    """

    job = get_job(job_id)
    if job is None:
        return None
    with job._lock:
        if job.status in TERMINAL_STATUSES:
            return job.status
        job.cancel_requested = True
        proc = job.proc
    if proc is not None:
        _terminate_process_group(proc)
    return "cancelled"


def job_snapshot(job_id: str) -> Optional[dict]:
    """Return a JSON-serialisable snapshot for ``job_id`` or ``None``."""

    job = get_job(job_id)
    return job.snapshot() if job is not None else None


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

    # Allow-list the two values that flow into the remote shell command (A2).
    if not VMANAGE_HOST_RE.match(form.vmanage_host.strip()):
        raise RunInputError(
            "vManage host must be an IPv4 address or DNS hostname "
            "(letters, digits, dots and hyphens only)."
        )
    if not REMOTE_DIR_RE.match(form.remote_dir.strip()):
        raise RunInputError(
            "Remote dir must be an absolute or '~'-relative POSIX path using "
            "only word characters, dots, slashes and hyphens (no spaces or "
            "shell metacharacters)."
        )

    # Range-check the wired-through bulk-show.py knobs (C3).
    if form.retries < 0 or form.retries > MAX_RETRIES:
        raise RunInputError(
            f"retries must be between 0 and {MAX_RETRIES}."
        )
    if form.max_workers is not None and not (
        1 <= form.max_workers <= MAX_WORKERS_CAP
    ):
        raise RunInputError(
            f"max workers must be between 1 and {MAX_WORKERS_CAP}."
        )
    if not (1 <= form.controller_port <= 65535):
        raise RunInputError("controller port must be between 1 and 65535.")
    if not form.output_formats:
        raise RunInputError("at least one output format is required.")
    for fmt in form.output_formats:
        if fmt not in OUTPUT_FORMATS:
            raise RunInputError(
                f"unknown output format {fmt!r} "
                f"(valid: {', '.join(OUTPUT_FORMATS)})."
            )

    if len(form.hosts_text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise RunInputError(
            f"hosts text exceeds the {MAX_INPUT_BYTES} byte safety cap."
        )
    for label, text in (
        ("commands", form.commands_text),
        ("controller commands", form.controller_commands_text),
        ("edge commands", form.edge_commands_text),
    ):
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise RunInputError(
                f"{label} text exceeds the {MAX_INPUT_BYTES} byte safety cap."
            )
    if form.hosts_count() == 0:
        raise RunInputError("hosts text must contain at least one IP,user,pass row.")
    if form.controller_commands_count() == 0 and form.edge_commands_count() == 0:
        raise RunInputError(
            "at least one of the controller or edge command lists must contain "
            "a CLI command."
        )


def run_via_vmanage(
    form: RunForm,
    *,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    repo_root: Path | None = None,
    bulk_script: Path | None = None,
    run_on_vmanage: Path | None = None,
    python_executable: str | None = None,
    progress: Optional[Callable[[str], None]] = None,
) -> RunResult:
    """Spawn ``run_on_vmanage.py``, block until it finishes, return the result.

    Parameters are kept overridable so the unit tests can point them at a
    fake script and an isolated repo root. ``progress``, when given, is
    invoked with each (password-masked) stdout line as it streams in. This
    function acquires ``RUN_LOCK`` for the duration of the run; the actual
    work lives in :func:`_run_blocking` so :func:`start_run_async` can reuse
    it from a worker thread that already holds the lock.
    """

    validate_form(form)

    if not RUN_LOCK.acquire(blocking=False):
        raise RunBusyError("Another run is currently in progress.")
    try:
        return _run_blocking(
            form,
            timeout=timeout,
            repo_root=repo_root,
            bulk_script=bulk_script,
            run_on_vmanage=run_on_vmanage,
            python_executable=python_executable,
            progress=progress,
        )
    finally:
        RUN_LOCK.release()


def start_run_async(
    form: RunForm,
    *,
    timeout: float = DEFAULT_RUN_TIMEOUT,
    repo_root: Path | None = None,
    bulk_script: Path | None = None,
    run_on_vmanage: Path | None = None,
    python_executable: str | None = None,
) -> str:
    """Kick off a run in a background thread and return its ``job_id``.

    Validation happens up-front so the caller still sees ``RunInputError`` /
    ``RunBusyError`` synchronously (mirroring :func:`run_via_vmanage`). On
    success a :class:`RunJob` is registered, ``RUN_LOCK`` is held by the
    worker thread until the subprocess exits, and the worker streams progress
    into the job. Poll :func:`job_snapshot` (or the ``/api/progress`` route)
    for live state.
    """

    validate_form(form)

    if not RUN_LOCK.acquire(blocking=False):
        raise RunBusyError("Another run is currently in progress.")

    job = RunJob.new(
        hosts_total=form.hosts_count(),
        commands_total=form.progress_command_total(bulk_script or BULK_SCRIPT),
    )
    _register_job(job)

    def _worker() -> None:
        try:
            result = _run_blocking(
                form,
                timeout=timeout,
                repo_root=repo_root,
                bulk_script=bulk_script,
                run_on_vmanage=run_on_vmanage,
                python_executable=python_executable,
                progress=job.update_from_line,
                job=job,
            )
            job.finish_from_result(result)
        except Exception as exc:  # noqa: BLE001 — surface any failure in the job
            logger.exception("async run %s crashed", job.job_id)
            job.fail(exc)
        finally:
            RUN_LOCK.release()

    threading.Thread(
        target=_worker, name=f"run-{job.job_id}", daemon=True
    ).start()
    return job.job_id


def _run_blocking(
    form: RunForm,
    *,
    timeout: float,
    repo_root: Path | None,
    bulk_script: Path | None,
    run_on_vmanage: Path | None,
    python_executable: str | None,
    progress: Optional[Callable[[str], None]],
    job: Optional[RunJob] = None,
) -> RunResult:
    """Do the actual subprocess work WITHOUT touching ``RUN_LOCK``.

    Both :func:`run_via_vmanage` (which holds the lock) and the
    :func:`start_run_async` worker (the lock is already held) call this.
    When ``job`` is supplied the live subprocess handle is recorded on it and
    its ``cancel_requested`` flag is honoured (B3).
    """

    repo_root = repo_root or REPO_ROOT
    bulk_script = bulk_script or BULK_SCRIPT
    run_on_vmanage = run_on_vmanage or RUN_ON_VMANAGE
    python_executable = python_executable or sys.executable
    logs_dir = repo_root / "logs"

    started_monotonic = time.monotonic()
    started_at = _now_iso()
    timed_out = False
    cancelled = False
    tempdir = Path(tempfile.mkdtemp(prefix="sdwan-webapp-"))

    try:
        hosts_path = tempdir / "host.txt"
        commands_path = tempdir / "command.txt"
        bulk_link = tempdir / bulk_script.name

        # The remote bulk-show.py runs non-interactively inside vManage's
        # vshell, so it CANNOT prompt (getpass) for a shared password. Any
        # host line that omits a password would hang the remote run. Inject
        # the form password into password-less lines so the single password
        # the user typed is reused for the devices, matching the form's
        # "used for vManage AND edges" contract.
        prepared_hosts = _inject_host_passwords(
            form.hosts_text, form.password, bulk_script
        )

        # 0o600 keeps the credentials embedded in host.txt off other shell
        # users. We write with mode=0o600 atomically by combining open + os.
        _write_secure_text(hosts_path, prepared_hosts)
        # The positional commands file carries the shared fallback list; the
        # split files are written only when the user filled the matching box.
        _write_secure_text(commands_path, form.base_commands())

        controller_commands_name: Optional[str] = None
        edge_commands_name: Optional[str] = None
        if _count_non_empty_lines(form.controller_commands_text) > 0:
            controller_path = tempdir / "controller_command.txt"
            _write_secure_text(controller_path, form.controller_commands_text)
            controller_commands_name = controller_path.name
        if _count_non_empty_lines(form.edge_commands_text) > 0:
            edge_path = tempdir / "edge_command.txt"
            _write_secure_text(edge_path, form.edge_commands_text)
            edge_commands_name = edge_path.name

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
            controller_commands_name=controller_commands_name,
            edge_commands_name=edge_commands_name,
        )

        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")

        # ``start_new_session=True`` puts the child in its own process group so
        # a timeout / cancel can kill the WHOLE tree (the wrapper plus any
        # paramiko helper it spawns) via ``killpg`` rather than just the
        # wrapper PID (B1).
        proc = subprocess.Popen(
            argv,
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
        if job is not None:
            job.set_proc(proc)

        # Collect every secret to mask (A4): the form password plus any
        # per-host passwords embedded in the hosts text. Lines without a
        # password are filled with the form password, so they are covered too.
        secrets = _collect_secrets(form, bulk_script)

        # Stream stdout line-by-line so progress is visible live; all secrets
        # are masked before any line reaches ``progress`` or the accumulator.
        masked_stdout, returncode, timed_out, cancelled = _stream_and_collect(
            proc,
            form.password,
            secrets,
            timeout,
            progress,
            should_cancel=(lambda: job.cancel_requested) if job is not None else None,
        )
        if timed_out:
            masked_stdout += (
                f"\n[webapp] subprocess timed out after {timeout:.0f} s; killed."
            )
        if cancelled:
            masked_stdout += "\n[webapp] run cancelled by user; process killed."

        ended_monotonic = time.monotonic()
        ended_at = _now_iso()
        duration_sec = round(ended_monotonic - started_monotonic, 3)

        timestamp = _detect_timestamp_dir(tempdir / "logs", masked_stdout)
        target_dir = logs_dir / timestamp
        target_dir.mkdir(parents=True, exist_ok=True)

        output_files = _promote_outputs(tempdir / "logs" / timestamp, target_dir)

        run_log_path = target_dir / "run.log"
        run_log_path.write_text(masked_stdout, encoding="utf-8")

        # Best-effort per-host roll-up for the manifest (C5).
        host_results = collect_host_results(masked_stdout, target_dir)
        hosts_ok, hosts_failed = _host_counts(masked_stdout, host_results)

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
            cancelled=cancelled,
            host_results=host_results,
            hosts_ok=hosts_ok,
            hosts_failed=hosts_failed,
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
            cancelled=cancelled,
            output_files=output_files,
        )
    finally:
        # Tempdir is best-effort cleanup; we do NOT want a stale tempdir to
        # persist the password embedded in host.txt across runs.
        shutil.rmtree(tempdir, ignore_errors=True)


def _stream_and_collect(
    proc: "subprocess.Popen[str]",
    password: str,
    secrets: list[str],
    timeout: float,
    progress: Optional[Callable[[str], None]],
    *,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[str, int, bool, bool]:
    """Feed the password, stream masked stdout, enforce deadline / cancel.

    Returns ``(masked_stdout, returncode, timed_out, cancelled)``. A background
    reader thread pulls lines (so we can impose ``timeout`` even when the
    subprocess blocks without producing output); the main loop polls
    ``should_cancel`` so a cancellation request stops the run promptly. On
    deadline OR cancel we kill the whole process group, which closes the pipe
    and lets the reader drain and exit. Every string in ``secrets`` is masked
    (longest-first) before any line is recorded or forwarded to ``progress``.
    """

    chunks: list[str] = []

    def _reader() -> None:
        stdout = proc.stdout
        if stdout is None:  # pragma: no cover - we always pipe stdout
            return
        for raw in iter(stdout.readline, ""):
            masked = _mask_secrets(raw, secrets)
            chunks.append(masked)
            if progress is not None:
                try:
                    progress(masked)
                except Exception:  # noqa: BLE001 - progress must never break a run
                    logger.debug("progress callback raised", exc_info=True)

    # Send the password the way the manual `printf 'pw' | run_on_vmanage.py`
    # workflow does, then close stdin so the child sees EOF.
    if proc.stdin is not None:
        try:
            proc.stdin.write(password + "\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (BrokenPipeError, OSError):  # pragma: no cover - child died early
            pass

    reader = threading.Thread(target=_reader, name="run-stdout", daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False
    poll = 0.2
    while reader.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        reader.join(min(poll, remaining))

    # A cancel may have killed the process group out from under us (so the
    # reader hit EOF and the loop exited) before our poll noticed; re-check.
    if not timed_out and should_cancel is not None and should_cancel():
        cancelled = True

    if timed_out or cancelled:
        _terminate_process_group(proc)
        reader.join(10.0)

    try:
        returncode = proc.wait(timeout=10.0)
    except subprocess.TimeoutExpired:  # pragma: no cover - child ignored kill
        _terminate_process_group(proc)
        try:
            returncode = proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            returncode = -1
    if returncode is None:  # pragma: no cover - defensive
        returncode = -1

    if proc.stdout is not None:
        proc.stdout.close()

    return "".join(chunks), returncode, timed_out, cancelled


def _terminate_process_group(proc: "subprocess.Popen[str]") -> None:
    """Kill the subprocess's whole process group (SIGTERM, then SIGKILL).

    The child is started with ``start_new_session=True``, so SIGTERM/SIGKILL
    to its process group reaps the wrapper AND anything it spawned (B1). On
    platforms without ``os.killpg`` (e.g. Windows) we fall back to killing the
    single process. Already-exited processes are a no-op.
    """

    if proc.poll() is not None:
        return

    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    pgid = None
    if killpg is not None and getpgid is not None:
        try:
            pgid = getpgid(proc.pid)
        except (ProcessLookupError, OSError):  # pragma: no cover - race
            pgid = None

    def _signal(sig: int) -> None:
        if pgid is not None and killpg is not None:
            try:
                killpg(pgid, sig)
                return
            except (ProcessLookupError, OSError):  # pragma: no cover - race
                pass
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):  # pragma: no cover - race
            pass

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal(getattr(signal, "SIGKILL", signal.SIGTERM))


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
    controller_commands_name: Optional[str] = None,
    edge_commands_name: Optional[str] = None,
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
    if controller_commands_name:
        argv += ["--controller-commands", controller_commands_name]
    if edge_commands_name:
        argv += ["--edge-commands", edge_commands_name]
    # bulk-show.py knobs wired end to end (C3). ``run_on_vmanage.py`` forwards
    # each of these to the remote bulk-show.py invocation.
    if form.retries:
        argv += ["--retries", str(form.retries)]
    if form.max_workers is not None:
        argv += ["--max-workers", str(form.max_workers)]
    argv += ["--controller-port", str(form.controller_port)]
    argv += ["--output-format", ",".join(form.output_formats)]
    if form.download_outputs:
        argv.append("--download-outputs")
    if form.verbose:
        argv.append("--verbose")
    if form.reject_unknown_hosts:
        argv.append("--reject-unknown-hosts")
    return argv


def _load_parse_host_line(bulk_script: Path):
    """Return ``bulk-show.py``'s canonical ``parse_host_line`` or ``None``.

    The script is loaded by path (its name has a hyphen, so it is not a normal
    importable module). ``paramiko`` is lazy-imported inside
    ``connect_and_execute``, so importing the module here does not require SSH
    dependencies. Returns ``None`` if the script cannot be loaded or does not
    expose ``parse_host_line`` (e.g. the fake script used by the smoke test),
    in which case the caller falls back to leaving host lines untouched.
    """

    try:
        spec = importlib.util.spec_from_file_location(
            "_bulk_show_for_runner", bulk_script
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "parse_host_line", None)
    except Exception as exc:  # noqa: BLE001 - best-effort; degrade gracefully
        logger.debug("could not load parse_host_line from %s: %s", bulk_script, exc)
        return None


def _inject_host_passwords(hosts_text: str, password: str, bulk_script: Path) -> str:
    """Embed ``password`` into host lines that omit one.

    Blank lines, comment lines, and lines that already carry a password are
    preserved verbatim. Password-less lines are rewritten in the unambiguous
    ``ip,user,password,type=<device_type>`` form. If the canonical parser is
    unavailable, the original text is returned unchanged (best effort).
    """

    parse_host_line = _load_parse_host_line(bulk_script)
    if parse_host_line is None:
        return hosts_text

    out_lines: list[str] = []
    for raw in hosts_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw)
            continue
        try:
            parsed = parse_host_line(raw)
        except Exception:  # noqa: BLE001 - leave malformed lines for bulk-show
            out_lines.append(raw)
            continue
        if parsed is None:
            out_lines.append(raw)
            continue
        ip, user, pw, device_type = parsed
        if pw is None:
            pw = password
        out_lines.append(f"{ip},{user},{pw},type={device_type}")

    result = "\n".join(out_lines)
    if hosts_text.endswith("\n"):
        result += "\n"
    return result


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


# Output files promoted into ``logs/<ts>/`` — bulk-show.py can emit text,
# JSON and CSV per host (C3), so accept all three extensions.
_OUTPUT_SUFFIXES = (".txt", ".json", ".csv")


def _promote_outputs(source_dir: Path, target_dir: Path) -> list[str]:
    """Move ``output_*.{txt,json,csv}`` files into the canonical logs dir."""

    moved: list[str] = []
    if not source_dir.is_dir():
        return moved
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.startswith("output_"):
            continue
        if entry.suffix.lower() not in _OUTPUT_SUFFIXES:
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
    cancelled: bool = False,
    host_results: Optional[list[dict]] = None,
    hosts_ok: int = 0,
    hosts_failed: int = 0,
) -> dict:
    if cancelled:
        status = "cancelled"
    elif timed_out:
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
        "controller_commands_count": form.controller_commands_count(),
        "edge_commands_count": form.edge_commands_count(),
        "options": {
            "download_outputs": form.download_outputs,
            "verbose": form.verbose,
            "reject_unknown_hosts": form.reject_unknown_hosts,
            "retries": form.retries,
            "max_workers": form.max_workers,
            "output_formats": list(form.output_formats),
            "controller_port": form.controller_port,
        },
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_sec": duration_sec,
        "returncode": returncode,
        "outputs_count": len(output_files),
        "outputs": output_files,
        "host_results": host_results or [],
        "hosts_ok": hosts_ok,
        "hosts_failed": hosts_failed,
        "status": status,
    }


def _mask_password(text: str, password: str) -> str:
    """Replace ``password`` with ``***`` everywhere in ``text``.

    Retained for the single-secret callers/tests; :func:`_mask_secrets` is the
    multi-secret generalisation used by the streaming path (A4).
    """

    if not password:
        return text
    # An empty password should never reach us (validate_form blocks it) but
    # ``str.replace`` on "" would loop forever in some implementations, so
    # be defensive.
    return text.replace(password, "***")


def _mask_secrets(text: str, secrets: list[str]) -> str:
    """Replace every secret in ``secrets`` with ``***`` (longest-first).

    Masking longest-first prevents a short secret that is a substring of a
    longer one from leaving the longer secret partially visible.
    """

    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _collect_secrets(form: RunForm, bulk_script: Path) -> list[str]:
    """Gather every secret to mask: the form password + per-host passwords.

    Per-host passwords are parsed from ``hosts_text`` with bulk-show.py's
    canonical ``parse_host_line`` (the same loader :func:`_inject_host_passwords`
    uses). Returns a de-duplicated list sorted longest-first so substrings of a
    longer secret never leak (A4).
    """

    secrets: set[str] = set()
    if form.password:
        secrets.add(form.password)

    parse_host_line = _load_parse_host_line(bulk_script)
    if parse_host_line is not None:
        for raw in form.hosts_text.splitlines():
            try:
                parsed = parse_host_line(raw)
            except Exception:  # noqa: BLE001 - malformed lines simply skipped
                continue
            if not parsed:
                continue
            pw = parsed[2]
            if pw:
                secrets.add(pw)

    return sorted(secrets, key=len, reverse=True)


# Boundary marker emitted by bulk-show.py's text writer:
#   "===== session end:   <host> status=<status> ended=... duration=... ====="
_SESSION_END_RE = re.compile(
    r"=====\s*session end:\s+(?P<host>\S+)\s+status=(?P<status>\S+)"
)

# bulk-show.py per-host log lines look like "[<ip>] <message>"; the final
# roll-up is "[main] done: success=N, failed=M".
_HOST_LINE_RE = re.compile(r"^\s*\[(?P<host>(?!main\])[^\]]+)\]\s+(?P<rest>.*)$")
_MAIN_DONE_COUNTS_RE = re.compile(
    r"\[main\]\s+done:\s*success=(\d+),\s*failed=(\d+)"
)


def collect_host_results(stdout: str, source_dir: Path) -> list[dict]:
    """Best-effort per-host roll-up for the manifest (C5).

    Produces ``[{"host", "device_type", "status", "error"}, ...]`` by, in
    order of preference:

    1. parsing ``output_*.json`` files (richest: carries ``device_type`` and
       the per-host ``error``),
    2. parsing the ``===== session end: ... status=... =====`` marker in
       ``output_*.txt`` files for hosts not already covered, and
    3. scanning the merged stdout for ``[<ip>] ...`` error lines to flag hosts
       that failed before any output file was written.

    Returns the list sorted by host. Never raises — a parse failure for one
    source just yields fewer rows.
    """

    results: dict[str, dict] = {}

    if source_dir.is_dir():
        for entry in sorted(source_dir.iterdir()):
            if not entry.is_file() or not entry.name.startswith("output_"):
                continue
            if entry.suffix.lower() == ".json":
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                host = data.get("host")
                if not host:
                    continue
                results[host] = {
                    "host": host,
                    "device_type": data.get("device_type"),
                    "status": data.get("status"),
                    "error": data.get("error"),
                }

        for entry in sorted(source_dir.iterdir()):
            if not entry.is_file():
                continue
            if not (entry.name.startswith("output_") and entry.suffix.lower() == ".txt"):
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = _SESSION_END_RE.search(text)
            if not match:
                continue
            host = match.group("host")
            if host in results:
                continue
            error_match = re.search(r"^!!\s*(.+)$", text, re.MULTILINE)
            results[host] = {
                "host": host,
                "device_type": None,
                "status": match.group("status"),
                "error": error_match.group(1).strip() if error_match else None,
            }

    # stdout fallback: flag hosts that errored before producing a file.
    for raw in stdout.splitlines():
        line_match = _HOST_LINE_RE.match(raw)
        if not line_match:
            continue
        host = line_match.group("host").strip()
        rest = line_match.group("rest")
        if host in results:
            continue
        low = rest.lower()
        if "error" in low or "rejected" in low or "failed" in low:
            results[host] = {
                "host": host,
                "device_type": None,
                "status": "error",
                "error": rest.strip(),
            }

    return sorted(results.values(), key=lambda row: str(row.get("host")))


def _host_counts(stdout: str, host_results: list[dict]) -> tuple[int, int]:
    """Return ``(hosts_ok, hosts_failed)`` from the stdout summary or rows."""

    match = _MAIN_DONE_COUNTS_RE.search(stdout)
    if match:
        return int(match.group(1)), int(match.group(2))
    ok = sum(1 for row in host_results if row.get("status") == "success")
    failed = sum(
        1 for row in host_results if row.get("status") not in (None, "success")
    )
    return ok, failed


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
    "JOB_TTL_SECONDS",
    "LOG_TAIL_MAX",
    "LOGS_DIR",
    "MAX_INPUT_BYTES",
    "MAX_JOBS",
    "OUTPUT_FORMATS",
    "REMOTE_DIR_RE",
    "REPO_ROOT",
    "RUN_LOCK",
    "VMANAGE_HOST_RE",
    "RunBusyError",
    "RunForm",
    "RunInputError",
    "RunJob",
    "RunResult",
    "collect_host_results",
    "get_job",
    "job_snapshot",
    "request_cancel",
    "run_via_vmanage",
    "start_run_async",
    "validate_form",
]
