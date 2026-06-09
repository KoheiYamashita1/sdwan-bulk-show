"""Tests for the server-side diff: pure helper + the JSON endpoint.

These never touch SSH. The pure :func:`storage.build_unified_diff` is tested
in isolation, and the ``GET /api/runs/<ts>/diff`` endpoint is exercised over a
sandbox ``logs/<ts>/`` directory via Starlette's ``TestClient`` (mirroring the
sandbox setup used by ``tests/_smoke_live.py``).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webapp import main as webapp_main
from webapp import storage

TS = "20260601_120000"


# ---------------------------------------------------------------------------
# Pure unified-diff JSON helper
# ---------------------------------------------------------------------------


class BuildUnifiedDiffTests(unittest.TestCase):
    def test_identical_inputs_report_identical_and_empty_diff(self) -> None:
        payload = storage.build_unified_diff(
            "a.txt", "line1\nline2\n", "b.txt", "line1\nline2\n"
        )
        self.assertTrue(payload["identical"])
        self.assertEqual(payload["diff"], [])
        self.assertEqual(payload["a"], "a.txt")
        self.assertEqual(payload["b"], "b.txt")
        self.assertFalse(payload["a_truncated"])
        self.assertFalse(payload["b_truncated"])

    def test_differing_inputs_emit_add_remove_lines(self) -> None:
        payload = storage.build_unified_diff(
            "old.txt", "alpha\nbravo\n", "new.txt", "alpha\ncharlie\n"
        )
        self.assertFalse(payload["identical"])
        self.assertIsInstance(payload["diff"], list)
        self.assertTrue(any(line.startswith("-bravo") for line in payload["diff"]))
        self.assertTrue(any(line.startswith("+charlie") for line in payload["diff"]))
        # The fromfile / tofile names are threaded into the unified-diff header.
        self.assertTrue(any("old.txt" in line for line in payload["diff"]))
        self.assertTrue(any("new.txt" in line for line in payload["diff"]))

    def test_truncation_flags_passed_through(self) -> None:
        payload = storage.build_unified_diff(
            "a.txt", "x\n", "b.txt", "y\n", a_truncated=True, b_truncated=False
        )
        self.assertTrue(payload["a_truncated"])
        self.assertFalse(payload["b_truncated"])

    def test_diff_lines_have_no_trailing_eol_artifacts(self) -> None:
        # lineterm="" means lines must not carry trailing newlines.
        payload = storage.build_unified_diff(
            "a.txt", "one\ntwo\n", "b.txt", "one\nTWO\n"
        )
        for line in payload["diff"]:
            self.assertFalse(line.endswith("\n"), f"line had trailing newline: {line!r}")

    def test_payload_includes_side_by_side_rows(self) -> None:
        payload = storage.build_unified_diff(
            "a.txt", "alpha\nbravo\n", "b.txt", "alpha\ncharlie\n"
        )
        self.assertIn("rows", payload)
        tags = [row["tag"] for row in payload["rows"]]
        self.assertEqual(tags[0], "equal")
        self.assertIn("replace", tags)

    def test_payload_includes_stats(self) -> None:
        # alpha (equal), bravo->charlie (replace), delta (delete), echo (insert)
        payload = storage.build_unified_diff(
            "a.txt", "alpha\nbravo\ndelta\n", "b.txt", "alpha\ncharlie\necho\n"
        )
        self.assertIn("stats", payload)
        stats = payload["stats"]
        self.assertEqual(set(stats), {"added", "removed", "changed", "unchanged"})
        self.assertEqual(stats["unchanged"], 1)
        self.assertGreaterEqual(stats["changed"], 1)


class BuildSideBySideTests(unittest.TestCase):
    def test_equal_lines_pair_left_and_right(self) -> None:
        rows = storage.build_side_by_side(["x", "y"], ["x", "y"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["tag"] == "equal" for r in rows))
        self.assertEqual(rows[0], {"tag": "equal", "ln": 1, "left": "x", "rn": 1, "right": "x"})

    def test_replace_pairs_left_and_right(self) -> None:
        rows = storage.build_side_by_side(["old"], ["new"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], "replace")
        self.assertEqual(rows[0]["left"], "old")
        self.assertEqual(rows[0]["right"], "new")

    def test_delete_has_empty_right(self) -> None:
        rows = storage.build_side_by_side(["keep", "gone"], ["keep"])
        deleted = [r for r in rows if r["tag"] == "delete"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["left"], "gone")
        self.assertIsNone(deleted[0]["right"])
        self.assertIsNone(deleted[0]["rn"])

    def test_insert_has_empty_left(self) -> None:
        rows = storage.build_side_by_side(["keep"], ["keep", "added"])
        inserted = [r for r in rows if r["tag"] == "insert"]
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["right"], "added")
        self.assertIsNone(inserted[0]["left"])
        self.assertIsNone(inserted[0]["ln"])

    def test_uneven_replace_surplus_becomes_insert(self) -> None:
        rows = storage.build_side_by_side(["a"], ["b", "c"])
        self.assertEqual(rows[0]["tag"], "replace")
        self.assertEqual(rows[1]["tag"], "insert")
        self.assertEqual(rows[1]["right"], "c")

    def test_replace_rows_carry_intra_line_segments(self) -> None:
        # "ip mtu 1500" -> "ip mtu 9000": the shared prefix is unchanged, the
        # number is the changed segment.
        rows = storage.build_side_by_side(["ip mtu 1500"], ["ip mtu 9000"])
        self.assertEqual(rows[0]["tag"], "replace")
        self.assertIn("left_segments", rows[0])
        self.assertIn("right_segments", rows[0])
        # Reassembling the segments reproduces the original line text.
        self.assertEqual(
            "".join(seg["text"] for seg in rows[0]["left_segments"]), "ip mtu 1500"
        )
        self.assertEqual(
            "".join(seg["text"] for seg in rows[0]["right_segments"]), "ip mtu 9000"
        )
        # There is at least one unchanged run and one changed run per side.
        self.assertTrue(any(not s["change"] for s in rows[0]["left_segments"]))
        self.assertTrue(any(s["change"] for s in rows[0]["right_segments"]))

    def test_equal_rows_have_no_segments(self) -> None:
        rows = storage.build_side_by_side(["same"], ["same"])
        self.assertNotIn("left_segments", rows[0])
        self.assertNotIn("right_segments", rows[0])

    def test_very_long_lines_skip_segments(self) -> None:
        long_a = "x" * (storage.MAX_SEGMENT_LINE_LEN + 1)
        long_b = "y" * (storage.MAX_SEGMENT_LINE_LEN + 1)
        rows = storage.build_side_by_side([long_a], [long_b])
        self.assertEqual(rows[0]["tag"], "replace")
        # Over the cap: base row shape is preserved but no segments are added.
        self.assertNotIn("left_segments", rows[0])


# ---------------------------------------------------------------------------
# GET /api/runs/<ts>/diff endpoint
# ---------------------------------------------------------------------------


class DiffEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-diff-test-")
        self.addCleanup(self._tmp.cleanup)
        sandbox = Path(self._tmp.name)
        self._run_dir = sandbox / "logs" / TS
        self._run_dir.mkdir(parents=True)
        (self._run_dir / "output_a.txt").write_text("alpha\nbravo\n", encoding="utf-8")
        (self._run_dir / "output_b.txt").write_text("alpha\ncharlie\n", encoding="utf-8")

        # Point storage at the sandbox logs dir and restore afterwards.
        self._orig_logs = storage.LOGS_DIR
        storage.LOGS_DIR = sandbox / "logs"
        self.addCleanup(self._restore_logs)

        self.client = TestClient(webapp_main.app)

    def _restore_logs(self) -> None:
        storage.LOGS_DIR = self._orig_logs

    def test_diff_of_two_files(self) -> None:
        r = self.client.get(
            f"/api/runs/{TS}/diff?a=output_a.txt&b=output_b.txt"
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["a"], "output_a.txt")
        self.assertEqual(data["b"], "output_b.txt")
        self.assertFalse(data["identical"])
        self.assertTrue(any(line.startswith("+charlie") for line in data["diff"]))
        # Side-by-side rows are present for the two-column renderer.
        self.assertIn("rows", data)
        self.assertTrue(any(row["tag"] == "replace" for row in data["rows"]))

    def test_self_diff_is_identical(self) -> None:
        r = self.client.get(
            f"/api/runs/{TS}/diff?a=output_a.txt&b=output_a.txt"
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["identical"])
        self.assertEqual(data["diff"], [])

    def test_missing_file_is_404(self) -> None:
        r = self.client.get(
            f"/api/runs/{TS}/diff?a=output_a.txt&b=nope.txt"
        )
        self.assertEqual(r.status_code, 404)

    def test_traversal_is_refused(self) -> None:
        r = self.client.get(
            f"/api/runs/{TS}/diff?a=..%2Fmanifest.json&b=output_a.txt"
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_run_is_404(self) -> None:
        r = self.client.get(
            "/api/runs/19990101_000000/diff?a=output_a.txt&b=output_b.txt"
        )
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------
# POST /runs/<ts>/open — bad-input / 404 paths only (never spawn an app)
# ---------------------------------------------------------------------------


class OpenEndpointInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-open-test-")
        self.addCleanup(self._tmp.cleanup)
        sandbox = Path(self._tmp.name)
        run_dir = sandbox / "logs" / TS
        run_dir.mkdir(parents=True)
        (run_dir / "output_a.txt").write_text("alpha\n", encoding="utf-8")
        # A path-safe file that is NOT on the open allow-list (A3).
        (run_dir / "notes.txt").write_text("secret-ish\n", encoding="utf-8")

        self._orig_logs = storage.LOGS_DIR
        storage.LOGS_DIR = sandbox / "logs"
        self.addCleanup(self._restore_logs)
        self.client = TestClient(webapp_main.app)

    def _restore_logs(self) -> None:
        storage.LOGS_DIR = self._orig_logs

    def test_bogus_target_without_name_is_400(self) -> None:
        r = self.client.post(
            f"/runs/{TS}/open",
            json={"target": "bogus"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 400)

    def test_traversal_name_is_404(self) -> None:
        r = self.client.post(
            f"/runs/{TS}/open",
            json={"name": "../manifest.json"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 404)

    def test_unknown_run_is_404(self) -> None:
        r = self.client.post(
            "/runs/19990101_000000/open",
            json={"target": "finder"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 404)

    def test_disallowed_but_path_safe_name_is_403(self) -> None:
        # notes.txt resolves safely inside the run dir but is not on the open
        # allow-list (output_*, run.log, manifest.json), so it is refused.
        r = self.client.post(
            f"/runs/{TS}/open",
            json={"name": "notes.txt"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(r.status_code, 403)

    def test_allowed_output_name_passes_allowlist(self) -> None:
        # output_a.txt is allow-listed; on non-macOS it stops at the platform
        # guard (400) rather than the allow-list (403). Either way it is NOT a
        # 403, proving the allow-list let it through.
        r = self.client.post(
            f"/runs/{TS}/open",
            json={"name": "output_a.txt"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertNotEqual(r.status_code, 403)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
