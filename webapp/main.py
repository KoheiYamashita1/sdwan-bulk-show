"""FastAPI front door for the local web UI.

The app is intentionally tiny: every heavy bit (subprocess execution, log
scanning, path safety) lives in :mod:`webapp.runner` / :mod:`webapp.storage`,
so this module only translates HTTP requests into those calls and renders
Jinja2 templates.

Run modes
---------

* ``python -m webapp`` (or ``python -m webapp.main``) — boots uvicorn on
  ``127.0.0.1:8000``. Override with ``--host`` / ``--port`` if you need
  to expose it elsewhere; we log a warning whenever the bind address is
  not loopback.
* ``uvicorn webapp.main:app`` — for production-style ASGI runners.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import runner, storage

logger = logging.getLogger(__name__)

WEBAPP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEBAPP_DIR / "templates"
STATIC_DIR = WEBAPP_DIR / "static"

DEFAULT_REMOTE_DIR = "/home/admin"

app = FastAPI(
    title="sdwan-bulk-show Web UI",
    description=(
        "Local Mac-side wrapper around run_on_vmanage.py. Drives bulk show "
        "collection via vManage's vshell from the browser without exposing "
        "credentials to disk."
    ),
    docs_url=None,  # docs are an attack surface for a local-only tool
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Trivial liveness probe used by smoke tests and ``curl``."""

    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Render the run-form landing page."""

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "default_remote_dir": DEFAULT_REMOTE_DIR,
            "form": None,
            "error": None,
        },
    )


@app.post("/run")
def submit_run(
    request: Request,
    vmanage_host: str = Form(...),
    user: str = Form(...),
    password: str = Form(...),
    remote_dir: str = Form(DEFAULT_REMOTE_DIR),
    hosts_text: str = Form(""),
    commands_text: str = Form(""),
    controller_commands_text: str = Form(""),
    edge_commands_text: str = Form(""),
    download_outputs: Optional[str] = Form(None),
    verbose: Optional[str] = Form(None),
    reject_unknown_hosts: Optional[str] = Form(None),
):
    """Receive the form and kick off ``run_on_vmanage.py`` asynchronously.

    Two response shapes:

    * **AJAX** (``X-Requested-With: XMLHttpRequest`` or an ``application/json``
      Accept header): return JSON ``{"job_id": "..."}`` (200) on success, or
      ``{"error": "..."}`` with 400/409/500 on failure. The index page renders
      progress and results inline without navigating away.
    * **Plain form POST** (no-JS fallback): 303-redirect to the standalone
      ``/runs/active/{job_id}`` progress page, or re-render the index with the
      submitted (non-secret) values on error.
    """

    form = runner.RunForm(
        vmanage_host=vmanage_host.strip(),
        user=user.strip(),
        password=password,  # never trim a password
        remote_dir=remote_dir.strip() or DEFAULT_REMOTE_DIR,
        hosts_text=hosts_text,
        commands_text=commands_text,
        controller_commands_text=controller_commands_text,
        edge_commands_text=edge_commands_text,
        download_outputs=_checkbox(download_outputs),
        verbose=_checkbox(verbose),
        reject_unknown_hosts=_checkbox(reject_unknown_hosts),
    )

    wants_json = _wants_json(request)
    try:
        job_id = runner.start_run_async(form)
    except runner.RunInputError as exc:
        return _run_error(request, form, str(exc), status.HTTP_400_BAD_REQUEST, wants_json)
    except runner.RunBusyError as exc:
        return _run_error(request, form, str(exc), status.HTTP_409_CONFLICT, wants_json)
    except Exception as exc:  # noqa: BLE001 — we genuinely want the wide net
        logger.exception("start_run_async crashed")
        return _run_error(
            request,
            form,
            f"Unexpected error while starting run_on_vmanage.py: {exc}",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            wants_json,
        )

    if wants_json:
        return JSONResponse({"job_id": job_id})
    # 303 sends the browser to GET the progress page so a refresh doesn't
    # accidentally re-submit (POST/Redirect/GET pattern).
    return RedirectResponse(
        url=f"/runs/active/{job_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/runs/active/{job_id}", response_class=HTMLResponse)
def run_progress(request: Request, job_id: str) -> HTMLResponse:
    """Live progress page for an in-flight (or just-finished) async run."""

    snapshot = runner.job_snapshot(job_id)
    status_code = status.HTTP_200_OK if snapshot else status.HTTP_404_NOT_FOUND
    return templates.TemplateResponse(
        request,
        "run_progress.html",
        {
            "job_id": job_id,
            "job": snapshot,
        },
        status_code=status_code,
    )


@app.get("/api/progress/{job_id}")
def api_progress(job_id: str) -> JSONResponse:
    """Polling endpoint: the :class:`RunJob` snapshot as JSON (masked only)."""

    snapshot = runner.job_snapshot(job_id)
    if snapshot is None:
        return JSONResponse(
            {"error": "unknown job_id"}, status_code=status.HTTP_404_NOT_FOUND
        )
    return JSONResponse(snapshot)


@app.get("/runs", response_class=HTMLResponse)
def runs_list(request: Request) -> HTMLResponse:
    """Past runs newest-first."""

    runs = storage.list_runs(limit=200)
    return templates.TemplateResponse(
        request,
        "runs_list.html",
        {"runs": runs},
    )


@app.get("/runs/{timestamp}", response_class=HTMLResponse)
def run_detail(request: Request, timestamp: str) -> HTMLResponse:
    """Single-run detail: manifest + per-host output file index."""

    try:
        run = storage.get_run(timestamp)
        files = storage.list_run_files(timestamp)
    except storage.StorageError as exc:
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {
                "timestamp": timestamp,
                "run": None,
                "files": [],
                "output_files": [],
                "error": str(exc),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    output_files = [name for name in files if name.startswith("output_")]
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "timestamp": timestamp,
            "run": run,
            "files": files,
            "output_files": output_files,
            "error": None,
        },
    )


@app.get("/runs/{timestamp}/files/{filename}", response_class=HTMLResponse)
def view_file(request: Request, timestamp: str, filename: str) -> HTMLResponse:
    """Render a single file from the run dir as plain text in HTML."""

    try:
        text, truncated = storage.read_file_text(timestamp, filename)
    except storage.StorageError as exc:
        return templates.TemplateResponse(
            request,
            "file_view.html",
            {
                "timestamp": timestamp,
                "filename": filename,
                "content": "",
                "truncated": False,
                "max_bytes": storage.MAX_VIEW_BYTES,
                "error": str(exc),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return templates.TemplateResponse(
        request,
        "file_view.html",
        {
            "timestamp": timestamp,
            "filename": filename,
            "content": text,
            "truncated": truncated,
            "max_bytes": storage.MAX_VIEW_BYTES,
            "error": None,
        },
    )


@app.get("/api/runs/{timestamp}/file")
def api_run_file(timestamp: str, name: str) -> JSONResponse:
    """Raw file content as JSON for the compare panes.

    ``{"name": ..., "content": <text>, "truncated": <bool>}`` on success;
    404 ``{"error": ...}`` for an unknown/invalid run or filename. Path safety
    is delegated to :func:`storage.read_file_text`.
    """

    try:
        text, truncated = storage.read_file_text(timestamp, name)
    except storage.StorageError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND
        )
    return JSONResponse({"name": name, "content": text, "truncated": truncated})


@app.get("/api/runs/{timestamp}/diff")
def api_run_diff(timestamp: str, a: str, b: str) -> JSONResponse:
    """Unified diff of two files in a run as JSON.

    Both ``a`` and ``b`` are resolved through :func:`storage.diff_files`,
    which reads each file via the same safe path mechanism as
    :func:`storage.read_file_text` (path-traversal safe, ``MAX_VIEW_BYTES``
    bounded). Returns ``404`` if either file is missing or unsafe. The shape
    is ``{"a", "b", "a_truncated", "b_truncated", "diff": [...], "identical"}``
    where ``diff`` is a list of plain-text unified-diff lines the client
    colourises by leading character.
    """

    try:
        payload = storage.diff_files(timestamp, a, b)
    except storage.StorageError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND
        )
    return JSONResponse(payload)


@app.get("/runs/{timestamp}/compare", response_class=HTMLResponse)
def run_compare(request: Request, timestamp: str) -> HTMLResponse:
    """Pick-two-and-diff view over a run's ``output_*`` files."""

    try:
        run = storage.get_run(timestamp)
        files = storage.list_run_files(timestamp)
    except storage.StorageError as exc:
        return templates.TemplateResponse(
            request,
            "run_compare.html",
            {
                "timestamp": timestamp,
                "run": None,
                "files": [],
                "error": str(exc),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    output_files = [name for name in files if name.startswith("output_")]
    return templates.TemplateResponse(
        request,
        "run_compare.html",
        {
            "timestamp": timestamp,
            "run": run,
            "files": output_files,
            "error": None,
        },
    )


@app.post("/runs/{timestamp}/open")
async def open_run_dir(request: Request, timestamp: str) -> JSONResponse:
    """Open a run's log folder — or a single file in it — locally (macOS only).

    Two modes, both driven off server-resolved absolute paths only:

    * **Folder** (no ``name`` field): ``target`` selects Finder (``open <dir>``)
      or Terminal (``open -a Terminal <dir>``). The directory is resolved via
      :func:`storage.safe_run_dir`.
    * **Single file** (``name`` present): the file is resolved via
      :func:`storage.safe_file_path` (same path-safety as ``read_file_text``)
      and opened in its default app with ``open <file>``.

    Security: the timestamp shape and every path are validated before any
    spawn, and only the server-resolved absolute path is ever handed to
    ``subprocess.run`` as an argv list — never a shell string. The actual
    spawn is gated behind ``sys.platform == "darwin"``; path-validation errors
    (404/400) are returned regardless of platform.
    """

    target, name = await _extract_open_fields(request)

    # Resolve (and thereby validate) the run dir first so a bad timestamp is a
    # 404 on every platform, before we even consider spawning anything.
    try:
        run_dir = storage.safe_run_dir(timestamp)
    except storage.StorageError as exc:
        return JSONResponse(
            {"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND
        )

    if name:
        # Per-file open: resolve through the same safe mechanism as the
        # readers so traversal / symlinks are refused (404) before any spawn.
        try:
            file_path = storage.safe_file_path(timestamp, name)
        except storage.StorageError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND
            )
        argv = ["open", str(file_path)]
        what = f"file {name}"
    else:
        if target not in {"finder", "terminal"}:
            return JSONResponse(
                {"error": "target must be 'finder' or 'terminal'."},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if target == "finder":
            argv = ["open", str(run_dir)]
        else:
            argv = ["open", "-a", "Terminal", str(run_dir)]
        what = "folder"

    if sys.platform != "darwin":
        return JSONResponse(
            {"error": "Opening is only supported on macOS."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        subprocess.run(
            argv, check=False, capture_output=True, timeout=10, shell=False
        )
    except Exception as exc:  # noqa: BLE001 — surface any spawn failure as JSON
        logger.exception("failed to open %s for run %s", what, timestamp)
        return JSONResponse(
            {"error": f"could not open {what}: {exc}"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checkbox(value: Optional[str]) -> bool:
    """Convert a Starlette form checkbox value to a bool.

    HTML checkboxes only send the field if checked. Any non-empty string
    we receive therefore means "on"; a missing field means "off".
    """

    if value is None:
        return False
    return value.lower() in {"on", "true", "1", "yes"}


def _wants_json(request: Request) -> bool:
    """True when the client expects a JSON response (the inline-UI fetch path)."""

    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return True
    return "application/json" in request.headers.get("accept", "").lower()


async def _extract_open_fields(
    request: Request,
) -> tuple[Optional[str], Optional[str]]:
    """Read ``(target, name)`` from a JSON or form-encoded request body.

    ``target`` selects the folder open mode (Finder/Terminal); ``name``, when
    present, switches to single-file open. Either may be ``None``/absent.
    """

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON -> nothing
            return None, None
        if not isinstance(body, dict):
            return None, None
        target = body.get("target")
        name = body.get("name")
        return (
            target if isinstance(target, str) else None,
            name if isinstance(name, str) else None,
        )
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - malformed body -> nothing
        return None, None
    target = form.get("target")
    name = form.get("name")
    return (
        target if isinstance(target, str) else None,
        name if isinstance(name, str) else None,
    )


def _run_error(
    request: Request,
    form: runner.RunForm,
    error: str,
    status_code: int,
    wants_json: bool,
):
    """Return a JSON error (AJAX) or re-rendered index (no-JS) for /run."""

    if wants_json:
        return JSONResponse({"error": error}, status_code=status_code)
    return _render_index_error(request, form, error, status_code)


def _render_index_error(
    request: Request,
    form: runner.RunForm,
    error: str,
    status_code: int,
) -> HTMLResponse:
    """Re-render the index with the submitted (non-secret) values."""

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "default_remote_dir": DEFAULT_REMOTE_DIR,
            "form": {
                "vmanage_host": form.vmanage_host,
                "user": form.user,
                "remote_dir": form.remote_dir,
                "hosts_text": form.hosts_text,
                "commands_text": form.commands_text,
                "controller_commands_text": form.controller_commands_text,
                "edge_commands_text": form.edge_commands_text,
                "download_outputs": form.download_outputs,
                "verbose": form.verbose,
                "reject_unknown_hosts": form.reject_unknown_hosts,
            },
            "error": error,
        },
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webapp",
        description=(
            "Run the sdwan-bulk-show local web UI on the loopback interface."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; warns if not loopback).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="TCP port to listen on (default: 8000).",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (development only).",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default: info).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> None:
    """Console-script entrypoint used by ``python -m webapp``."""

    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("WEBAPP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "Binding on %s:%d exposes the UI beyond loopback. "
            "Make sure your firewall + auth story is sound.",
            args.host,
            args.port,
        )

    # Imported lazily so ``import webapp.main`` for tests doesn't drag
    # uvicorn into the path unnecessarily.
    import uvicorn

    uvicorn.run(
        "webapp.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
