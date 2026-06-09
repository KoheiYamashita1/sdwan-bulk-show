"""Tests for the Wave 2 frontend surfaces wired in main.py.

Covers the new cross-run compare HTML route (``GET /runs/compare-across``),
the index page rendering the new option fields, and the defensive parsing of
the new RunForm knobs in ``POST /run`` (a malformed numeric knob must be a
friendly 400, not a 422, and must not spawn a subprocess).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webapp import main as webapp_main
from webapp import storage

TS_A = "20260601_120000"
TS_B = "20260602_120000"


class CompareAcrossRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-across-")
        self.addCleanup(self._tmp.cleanup)
        logs = Path(self._tmp.name) / "logs"
        (logs / TS_A).mkdir(parents=True)
        (logs / TS_B).mkdir(parents=True)
        self._orig_logs = storage.LOGS_DIR
        storage.LOGS_DIR = logs
        self.addCleanup(self._restore)
        self.client = TestClient(webapp_main.app)

    def _restore(self) -> None:
        storage.LOGS_DIR = self._orig_logs

    def test_no_params_renders_empty_state(self) -> None:
        r = self.client.get("/runs/compare-across")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Compare a host across two runs", r.text)

    def test_valid_runs_render_200(self) -> None:
        r = self.client.get(f"/runs/compare-across?a={TS_A}&b={TS_B}")
        self.assertEqual(r.status_code, 200)
        self.assertIn('id="hostpick"', r.text)
        self.assertIn("/static/filediff.js", r.text)

    def test_unknown_run_is_404(self) -> None:
        r = self.client.get(f"/runs/compare-across?a=19990101_000000&b={TS_B}")
        self.assertEqual(r.status_code, 404)

    def test_route_not_shadowed_by_timestamp_detail(self) -> None:
        # The literal /runs/compare-across must not be captured as a timestamp.
        r = self.client.get("/runs/compare-across")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("invalid timestamp", r.text)


class IndexFormFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(webapp_main.app)

    def test_index_renders_new_option_fields(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        for marker in (
            'name="retries"',
            'name="max_workers"',
            'name="output_formats"',
            'name="controller_port"',
            'id="stepper"',
            'id="cancel-btn"',
        ):
            self.assertIn(marker, r.text, marker)

    def test_reject_unknown_hosts_checked_by_default(self) -> None:
        r = self.client.get("/")
        # The checkbox must be pre-checked (Wave 1 backend defaults it ON).
        self.assertRegex(
            r.text, r'name="reject_unknown_hosts"[^>]*\n?[^>]*checked'
        )


class SubmitRunKnobParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(webapp_main.app)

    def test_malformed_max_workers_is_400_not_422(self) -> None:
        r = self.client.post(
            "/run",
            data={
                "vmanage_host": "10.0.0.1",
                "user": "admin",
                "password": "pw",
                "remote_dir": "/home/admin",
                "hosts_text": "10.0.0.1,admin\n",
                "controller_commands_text": "show version\n",
                "max_workers": "not-a-number",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
