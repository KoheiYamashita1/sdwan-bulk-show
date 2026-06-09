"""Tests for the state-changing POST guard (:mod:`webapp.security`, A1).

The guard is exercised through ``POST /api/runs/<id>/cancel`` (an unknown job
id, so an *allowed* request lands on the 404 branch — that 404, rather than a
403, is exactly how we tell "the guard let it through"). The cancel/404 path
never spawns anything, so these tests are side-effect free.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from webapp import main as webapp_main

XHR = {"X-Requested-With": "XMLHttpRequest"}


class StateChangeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(webapp_main.app)
        # Ensure no stray token check leaks across tests.
        self._saved_token = os.environ.pop("WEBAPP_TOKEN", None)
        self.addCleanup(self._restore_token)

    def _restore_token(self) -> None:
        if self._saved_token is None:
            os.environ.pop("WEBAPP_TOKEN", None)
        else:
            os.environ["WEBAPP_TOKEN"] = self._saved_token

    # -- host allow-list (DNS-rebinding defence) -----------------------------

    def test_loopback_host_is_allowed(self) -> None:
        # Default TestClient host ("testserver") is allow-listed, so the guard
        # passes and we reach the 404 unknown-job branch.
        r = self.client.post("/api/runs/nope/cancel", headers=XHR)
        self.assertEqual(r.status_code, 404)

    def test_explicit_loopback_host_is_allowed(self) -> None:
        r = self.client.post(
            "/api/runs/nope/cancel", headers={"Host": "127.0.0.1:8000", **XHR}
        )
        self.assertEqual(r.status_code, 404)

    def test_non_loopback_host_is_forbidden(self) -> None:
        r = self.client.post(
            "/api/runs/nope/cancel", headers={"Host": "evil.example", **XHR}
        )
        self.assertEqual(r.status_code, 403)
        self.assertIn("error", r.json())

    def test_extra_allowed_host_via_env(self) -> None:
        os.environ["WEBAPP_ALLOWED_HOSTS"] = "myhost.local"
        self.addCleanup(lambda: os.environ.pop("WEBAPP_ALLOWED_HOSTS", None))
        r = self.client.post(
            "/api/runs/nope/cancel", headers={"Host": "myhost.local", **XHR}
        )
        self.assertEqual(r.status_code, 404)

    # -- Fetch-Metadata (cross-site) defence ---------------------------------

    def test_cross_site_is_forbidden(self) -> None:
        r = self.client.post(
            "/api/runs/nope/cancel",
            headers={"Sec-Fetch-Site": "cross-site", **XHR},
        )
        self.assertEqual(r.status_code, 403)

    def test_same_origin_sec_fetch_site_is_allowed(self) -> None:
        r = self.client.post(
            "/api/runs/nope/cancel",
            headers={"Sec-Fetch-Site": "same-origin", **XHR},
        )
        self.assertEqual(r.status_code, 404)

    # -- optional bearer token -----------------------------------------------

    def test_token_required_when_env_set(self) -> None:
        os.environ["WEBAPP_TOKEN"] = "s3kret"
        r = self.client.post("/api/runs/nope/cancel", headers=XHR)
        self.assertEqual(r.status_code, 403)

    def test_token_accepted_via_authorization_header(self) -> None:
        os.environ["WEBAPP_TOKEN"] = "s3kret"
        r = self.client.post(
            "/api/runs/nope/cancel",
            headers={"Authorization": "Bearer s3kret", **XHR},
        )
        self.assertEqual(r.status_code, 404)

    def test_token_accepted_via_x_webapp_token_header(self) -> None:
        os.environ["WEBAPP_TOKEN"] = "s3kret"
        r = self.client.post(
            "/api/runs/nope/cancel",
            headers={"X-Webapp-Token": "s3kret", **XHR},
        )
        self.assertEqual(r.status_code, 404)

    def test_wrong_token_is_forbidden(self) -> None:
        os.environ["WEBAPP_TOKEN"] = "s3kret"
        r = self.client.post(
            "/api/runs/nope/cancel",
            headers={"Authorization": "Bearer nope", **XHR},
        )
        self.assertEqual(r.status_code, 403)

    # -- GET endpoints stay open ---------------------------------------------

    def test_get_endpoints_are_not_guarded(self) -> None:
        # Even a hostile Host header must not block read-only GETs.
        r = self.client.get("/healthz", headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
