"""Tests for the cross-run (same-host-across-two-runs) diff feature (C2).

Exercises the pure storage helpers and the two new endpoints
(``GET /api/runs/diff-across`` and ``GET /api/runs/common-hosts``) over a
sandbox ``logs/`` directory laid out with realistic
``output_<ip>_<innerts>.txt`` filenames.
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


class CrossRunStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-crossrun-")
        self.addCleanup(self._tmp.cleanup)
        sandbox = Path(self._tmp.name)
        logs = sandbox / "logs"
        (logs / TS_A).mkdir(parents=True)
        (logs / TS_B).mkdir(parents=True)
        # Host .1 exists in both runs (with differing content); .2 only in A;
        # .10 only in B (and must NOT be matched by the .1 prefix).
        (logs / TS_A / "output_10.0.0.1_20260601_120001.txt").write_text(
            "alpha\nbravo\n", encoding="utf-8"
        )
        (logs / TS_B / "output_10.0.0.1_20260602_120001.txt").write_text(
            "alpha\ncharlie\n", encoding="utf-8"
        )
        (logs / TS_A / "output_10.0.0.2_20260601_120002.txt").write_text(
            "only-in-a\n", encoding="utf-8"
        )
        (logs / TS_B / "output_10.0.0.10_20260602_120010.txt").write_text(
            "only-in-b\n", encoding="utf-8"
        )

        self._orig_logs = storage.LOGS_DIR
        storage.LOGS_DIR = logs
        self.addCleanup(self._restore)
        self.client = TestClient(webapp_main.app)

    def _restore(self) -> None:
        storage.LOGS_DIR = self._orig_logs

    # -- find_host_output ----------------------------------------------------

    def test_find_host_output_matches_exact_ip_prefix(self) -> None:
        self.assertEqual(
            storage.find_host_output(TS_A, "10.0.0.1"),
            "output_10.0.0.1_20260601_120001.txt",
        )

    def test_find_host_output_does_not_prefix_collide(self) -> None:
        # 10.0.0.1 must not match 10.0.0.10's file in run B.
        self.assertIsNone(storage.find_host_output(TS_A, "10.0.0.10"))

    def test_find_host_output_rejects_bad_host_token(self) -> None:
        with self.assertRaises(storage.StorageError):
            storage.find_host_output(TS_A, "../etc")

    # -- common_hosts --------------------------------------------------------

    def test_common_hosts_intersection(self) -> None:
        self.assertEqual(storage.common_hosts(TS_A, TS_B), ["10.0.0.1"])

    def test_hosts_in_run(self) -> None:
        self.assertEqual(storage.hosts_in_run(TS_A), ["10.0.0.1", "10.0.0.2"])

    # -- diff_across_runs ----------------------------------------------------

    def test_diff_across_runs_labels_and_content(self) -> None:
        payload = storage.diff_across_runs(TS_A, TS_B, "10.0.0.1")
        self.assertEqual(payload["a_run"], TS_A)
        self.assertEqual(payload["b_run"], TS_B)
        self.assertEqual(payload["host"], "10.0.0.1")
        self.assertTrue(payload["a"].startswith(TS_A + "/"))
        self.assertFalse(payload["identical"])
        self.assertTrue(any(row["tag"] == "replace" for row in payload["rows"]))

    def test_diff_across_runs_missing_host_raises(self) -> None:
        with self.assertRaises(storage.StorageError):
            storage.diff_across_runs(TS_A, TS_B, "10.0.0.2")

    # -- endpoints -----------------------------------------------------------

    def test_diff_across_endpoint(self) -> None:
        r = self.client.get(
            f"/api/runs/diff-across?a={TS_A}&b={TS_B}&host=10.0.0.1"
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["host"], "10.0.0.1")
        self.assertIn("stats", data)
        self.assertFalse(data["identical"])

    def test_diff_across_endpoint_404_for_missing_host(self) -> None:
        r = self.client.get(
            f"/api/runs/diff-across?a={TS_A}&b={TS_B}&host=10.0.0.2"
        )
        self.assertEqual(r.status_code, 404)

    def test_diff_across_endpoint_404_for_unknown_run(self) -> None:
        r = self.client.get(
            f"/api/runs/diff-across?a=19990101_000000&b={TS_B}&host=10.0.0.1"
        )
        self.assertEqual(r.status_code, 404)

    def test_common_hosts_endpoint(self) -> None:
        r = self.client.get(f"/api/runs/common-hosts?a={TS_A}&b={TS_B}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["a"], TS_A)
        self.assertEqual(data["b"], TS_B)
        self.assertEqual(data["hosts"], ["10.0.0.1"])

    def test_common_hosts_endpoint_404_for_unknown_run(self) -> None:
        r = self.client.get(
            f"/api/runs/common-hosts?a=19990101_000000&b={TS_B}"
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
