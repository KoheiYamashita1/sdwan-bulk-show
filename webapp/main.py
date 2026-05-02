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
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
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


@app.post("/run", response_class=HTMLResponse)
def submit_run(
    request: Request,
    vmanage_host: str = Form(...),
    user: str = Form(...),
    password: str = Form(...),
    remote_dir: str = Form(DEFAULT_REMOTE_DIR),
    hosts_text: str = Form(""),
    commands_text: str = Form(""),
    download_outputs: Optional[str] = Form(None),
    verbose: Optional[str] = Form(None),
    reject_unknown_hosts: Optional[str] = Form(None),
):
    """Receive the form, spawn ``run_on_vmanage.py``, redirect to detail page.

    On any validation or busy/timeout error we re-render the index with the
    submitted values (minus the password) so the user can fix and retry.
    """

    form = runner.RunForm(
        vmanage_host=vmanage_host.strip(),
        user=user.strip(),
        password=password,  # never trim a password
        remote_dir=remote_dir.strip() or DEFAULT_REMOTE_DIR,
        hosts_text=hosts_text,
        commands_text=commands_text,
        download_outputs=_checkbox(download_outputs),
        verbose=_checkbox(verbose),
        reject_unknown_hosts=_checkbox(reject_unknown_hosts),
    )

    try:
        result = runner.run_via_vmanage(form)
    except runner.RunInputError as exc:
        return _render_index_error(request, form, str(exc), status.HTTP_400_BAD_REQUEST)
    except runner.RunBusyError as exc:
        return _render_index_error(request, form, str(exc), status.HTTP_409_CONFLICT)
    except Exception as exc:  # noqa: BLE001 — we genuinely want the wide net
        logger.exception("run_via_vmanage crashed")
        return _render_index_error(
            request,
            form,
            f"Unexpected error while running run_on_vmanage.py: {exc}",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # 303 sends the browser to GET the detail page so a refresh doesn't
    # accidentally re-submit (POST/Redirect/GET pattern).
    return RedirectResponse(
        url=f"/runs/{result.timestamp}", status_code=status.HTTP_303_SEE_OTHER
    )


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
                "error": str(exc),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "timestamp": timestamp,
            "run": run,
            "files": files,
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
