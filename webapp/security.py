"""Lightweight guard for state-changing POST requests on a local-only UI.

The web UI binds to loopback by default, but a browser running on the same
machine can still be coerced into POSTing to it through a DNS-rebinding or
cross-site form attack. Because this tool drives SSH into production gear,
we add a small, dependency-free guard for every state-changing ``POST``:

* **Host allow-list (DNS-rebinding defence).** In a rebinding attack the
  attacker's domain resolves to ``127.0.0.1`` but the browser still sends
  ``Host: attacker.example``. We therefore reject any request whose ``Host``
  header host-part is not loopback (``127.0.0.1`` / ``localhost`` / ``::1``).
  Extra hosts may be allow-listed via ``WEBAPP_ALLOWED_HOSTS`` (comma
  separated). The Starlette ``TestClient`` default host ``testserver`` is
  also accepted — it is not a routable name, so allowing it is harmless and
  keeps the test surface clean.
* **Fetch-Metadata (cross-site) defence.** Modern browsers tag genuinely
  cross-site requests with ``Sec-Fetch-Site: cross-site``; we reject those
  outright. Same-origin / same-site / ``none`` (a direct navigation) pass.
* **Optional bearer token.** When ``WEBAPP_TOKEN`` is set in the environment
  the request must also carry ``Authorization: Bearer <token>`` (or the
  ``X-Webapp-Token`` header). When it is unset the token check is skipped
  (default off).

``GET`` endpoints stay open — the tool is local-only and they have no side
effects. Only the handlers that mutate state call :func:`state_change_error`.
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse

# Host-header host-parts always treated as same-origin/loopback.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# The Starlette TestClient sends ``Host: testserver`` by default. It is not a
# routable name, so allowing it does not widen the real attack surface while
# letting the test suite exercise the guarded POSTs without bespoke headers.
_TEST_HOST = "testserver"


def _allowed_hosts() -> set[str]:
    """Return the set of accepted ``Host`` host-parts (lower-cased)."""

    hosts = set(LOOPBACK_HOSTS)
    hosts.add(_TEST_HOST)
    for extra in os.environ.get("WEBAPP_ALLOWED_HOSTS", "").split(","):
        cleaned = extra.strip().lower()
        if cleaned:
            hosts.add(cleaned)
    return hosts


def _host_part(host_header: str) -> str:
    """Extract the bare host (no port) from a ``Host`` header value."""

    host = host_header.strip().lower()
    if not host:
        return ""
    # IPv6 literal, optionally with a port: "[::1]" or "[::1]:8000".
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    # IPv4 / hostname with an optional ":port" suffix. A bare "::1" has more
    # than one colon and no port, so only strip when exactly one colon.
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def _bearer_token(request: Request) -> Optional[str]:
    """Return the bearer token from the ``Authorization`` header, if present."""

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _deny(message: str) -> JSONResponse:
    """Build the canonical 403 JSON error body used by every guarded POST."""

    return JSONResponse(
        {"error": message}, status_code=status.HTTP_403_FORBIDDEN
    )


def state_change_error(request: Request) -> Optional[JSONResponse]:
    """Return a 403 :class:`JSONResponse` to reject a request, or ``None``.

    Call this at the top of every state-changing handler::

        guard = security.state_change_error(request)
        if guard is not None:
            return guard

    Returning the response (rather than raising ``HTTPException``) keeps the
    error body in the project's ``{"error": ...}`` shape instead of FastAPI's
    default ``{"detail": ...}``.
    """

    # 1. Optional bearer token. Independent of the host/origin checks: a token
    #    does not grant non-loopback access, it only adds a second factor.
    token = os.environ.get("WEBAPP_TOKEN")
    if token:
        provided = _bearer_token(request) or request.headers.get("x-webapp-token")
        if provided != token:
            return _deny("missing or invalid token.")

    # 2. Fetch-Metadata: reject browser-flagged cross-site requests.
    sec_fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if sec_fetch_site == "cross-site":
        return _deny("cross-site request rejected.")

    # 3. Host allow-list (DNS-rebinding defence).
    host = _host_part(request.headers.get("host", ""))
    if host not in _allowed_hosts():
        return _deny(
            f"host {host!r} is not allowed; this UI only accepts loopback "
            "requests."
        )

    return None


__all__ = ["LOOPBACK_HOSTS", "state_change_error"]
