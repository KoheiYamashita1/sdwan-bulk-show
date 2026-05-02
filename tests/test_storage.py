"""Unit tests for :mod:`webapp.storage` (path safety + manifest reads)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from webapp import storage


def _write_manifest(run_dir: Path, **fields) -> None:
    payload = {
        "timestamp": run_dir.name,
        "vmanage_host": "vmanage.test",
        "vmanage_user": "admin",
        "remote_dir": "/home/admin",
        "hosts_count": 0,
        "commands_count": 0,
        "options": {
            "download_outputs": True,
            "verbose": False,
            "reject_unknown_hosts": False,
        },
        "started_at": "2026-01-01T00:00:00+09:00",
        "ended_at": "2026-01-01T00:00:01+09:00",
        "duration_sec": 1.0,
        "returncode": 0,
        "outputs_count": 0,
        "outputs": [],
        "status": "success",
    }
    payload.update(fields)
    (run_dir / "manifest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


class _LogsSandbox(unittest.TestCase):
    """Provide an isolated ``logs/`` directory for storage tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-storage-test-")
        self.addCleanup(self._tmp.cleanup)
        self.logs_dir = Path(self._tmp.name) / "logs"
        self.logs_dir.mkdir()
        # Patch the module-level constant so all storage helpers operate on
        # our sandbox rather than the real repo.
        self._original_logs_dir = storage.LOGS_DIR
        storage.LOGS_DIR = self.logs_dir
        self.addCleanup(self._restore_logs_dir)

    def _restore_logs_dir(self) -> None:
        storage.LOGS_DIR = self._original_logs_dir

    def _make_run(self, timestamp: str, *, files: dict[str, str] | None = None) -> Path:
        run_dir = self.logs_dir / timestamp
        run_dir.mkdir()
        for name, body in (files or {}).items():
            (run_dir / name).write_text(body, encoding="utf-8")
        return run_dir


# ---------------------------------------------------------------------------
# safe_run_dir
# ---------------------------------------------------------------------------


class SafeRunDirTests(_LogsSandbox):
    def test_rejects_empty_timestamp(self) -> None:
        with self.assertRaises(storage.StorageError):
            storage.safe_run_dir("")

    def test_rejects_invalid_timestamp_shape(self) -> None:
        with self.assertRaises(storage.StorageError):
            storage.safe_run_dir("not-a-timestamp")

    def test_rejects_path_traversal_in_timestamp(self) -> None:
        # The regex check rejects anything containing slashes/dots before
        # we ever touch the FS.
        with self.assertRaises(storage.StorageError):
            storage.safe_run_dir("../etc/passwd")
        with self.assertRaises(storage.StorageError):
            storage.safe_run_dir("20260101_010101/../..")

    def test_rejects_missing_run_dir(self) -> None:
        with self.assertRaises(storage.StorageError):
            storage.safe_run_dir("20260101_010101")

    def test_returns_resolved_path_for_valid_run(self) -> None:
        run_dir = self._make_run("20260101_010101")
        resolved = storage.safe_run_dir("20260101_010101")
        self.assertEqual(resolved, run_dir.resolve())


# ---------------------------------------------------------------------------
# safe_file_path
# ---------------------------------------------------------------------------


class SafeFilePathTests(_LogsSandbox):
    def test_rejects_empty_filename(self) -> None:
        self._make_run("20260101_010101")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "")

    def test_rejects_filename_with_separator(self) -> None:
        self._make_run("20260101_010101")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "subdir/file.txt")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "..\\evil.txt")

    def test_rejects_dot_filenames(self) -> None:
        self._make_run("20260101_010101")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", ".")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "..")

    def test_rejects_missing_file(self) -> None:
        self._make_run("20260101_010101")
        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "missing.txt")

    def test_rejects_symlink_inside_run_dir(self) -> None:
        run_dir = self._make_run("20260101_010101")
        target = self.logs_dir.parent / "outside.txt"
        target.write_text("escape", encoding="utf-8")
        symlink = run_dir / "evil.txt"
        try:
            os.symlink(target, symlink)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaises(storage.StorageError):
            storage.safe_file_path("20260101_010101", "evil.txt")

    def test_returns_resolved_path_for_valid_file(self) -> None:
        self._make_run(
            "20260101_010101",
            files={"output_10.0.0.1.txt": "hello"},
        )
        path = storage.safe_file_path("20260101_010101", "output_10.0.0.1.txt")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")


# ---------------------------------------------------------------------------
# read_file_text
# ---------------------------------------------------------------------------


class ReadFileTextTests(_LogsSandbox):
    def test_reads_file_within_cap(self) -> None:
        self._make_run("20260101_010101", files={"output.txt": "hello"})
        text, truncated = storage.read_file_text("20260101_010101", "output.txt")
        self.assertEqual(text, "hello")
        self.assertFalse(truncated)

    def test_truncates_at_max_bytes(self) -> None:
        body = "x" * 32
        self._make_run("20260101_010101", files={"output.txt": body})
        text, truncated = storage.read_file_text(
            "20260101_010101", "output.txt", max_bytes=10
        )
        self.assertEqual(text, "x" * 10)
        self.assertTrue(truncated)

    def test_replaces_undecodable_bytes(self) -> None:
        run_dir = self._make_run("20260101_010101")
        (run_dir / "binary.txt").write_bytes(b"\xff\xfeAB")
        text, truncated = storage.read_file_text("20260101_010101", "binary.txt")
        self.assertFalse(truncated)
        # The 0xff/0xfe bytes are invalid UTF-8 leads, so utf-8 decoding with
        # ``errors="replace"`` returns the U+FFFD replacement char.
        self.assertIn("\ufffd", text)
        self.assertIn("AB", text)


# ---------------------------------------------------------------------------
# list_runs / get_run / list_run_files
# ---------------------------------------------------------------------------


class ListRunsTests(_LogsSandbox):
    def test_returns_empty_when_logs_dir_missing(self) -> None:
        # Recreate logs_dir into a non-existent path and verify graceful empty.
        storage.LOGS_DIR = self.logs_dir.parent / "does-not-exist"
        self.assertEqual(storage.list_runs(), [])

    def test_returns_runs_newest_first(self) -> None:
        self._make_run("20260101_010101")
        self._make_run("20260102_020202")
        self._make_run("20260103_030303")
        names = [r.timestamp for r in storage.list_runs()]
        self.assertEqual(names, ["20260103_030303", "20260102_020202", "20260101_010101"])

    def test_skips_non_timestamp_directories(self) -> None:
        (self.logs_dir / "not-a-run").mkdir()
        self._make_run("20260101_010101")
        names = [r.timestamp for r in storage.list_runs()]
        self.assertEqual(names, ["20260101_010101"])

    def test_skips_symlinked_run_dirs(self) -> None:
        self._make_run("20260101_010101")
        try:
            os.symlink(
                self.logs_dir / "20260101_010101", self.logs_dir / "20260101_999999"
            )
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
            self.skipTest(f"symlink unavailable: {exc}")
        names = [r.timestamp for r in storage.list_runs()]
        self.assertEqual(names, ["20260101_010101"])

    def test_limit_caps_result(self) -> None:
        self._make_run("20260101_010101")
        self._make_run("20260102_020202")
        self._make_run("20260103_030303")
        names = [r.timestamp for r in storage.list_runs(limit=2)]
        self.assertEqual(names, ["20260103_030303", "20260102_020202"])

    def test_run_summary_carries_manifest_fields(self) -> None:
        run_dir = self._make_run(
            "20260101_010101",
            files={"output_a.txt": "ok"},
        )
        _write_manifest(run_dir, status="success", returncode=0, vmanage_host="v1")
        summary = storage.get_run("20260101_010101")
        self.assertEqual(summary.status, "success")
        self.assertEqual(summary.vmanage_host, "v1")
        self.assertEqual(summary.returncode, 0)
        # output_a.txt + manifest.json
        self.assertEqual(summary.file_count, 2)

    def test_summary_status_is_legacy_without_manifest(self) -> None:
        self._make_run("20260101_010101", files={"output.txt": "x"})
        summary = storage.get_run("20260101_010101")
        self.assertEqual(summary.status, "legacy")
        self.assertEqual(summary.vmanage_host, "")
        self.assertIsNone(summary.returncode)

    def test_list_run_files_skips_symlinks(self) -> None:
        run_dir = self._make_run(
            "20260101_010101",
            files={"output_a.txt": "a", "output_b.txt": "b"},
        )
        target = self.logs_dir.parent / "outside.txt"
        target.write_text("nope", encoding="utf-8")
        try:
            os.symlink(target, run_dir / "linked.txt")
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows
            self.skipTest(f"symlink unavailable: {exc}")
        files = storage.list_run_files("20260101_010101")
        self.assertEqual(files, ["output_a.txt", "output_b.txt"])


# ---------------------------------------------------------------------------
# read_manifest
# ---------------------------------------------------------------------------


class ReadManifestTests(_LogsSandbox):
    def test_returns_dict_for_valid_manifest(self) -> None:
        run_dir = self._make_run("20260101_010101")
        _write_manifest(run_dir, status="failed", returncode=2)
        manifest = storage.read_manifest("20260101_010101")
        self.assertIsNotNone(manifest)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["returncode"], 2)

    def test_returns_none_when_manifest_missing(self) -> None:
        self._make_run("20260101_010101")
        self.assertIsNone(storage.read_manifest("20260101_010101"))

    def test_returns_none_for_invalid_json(self) -> None:
        run_dir = self._make_run("20260101_010101")
        (run_dir / "manifest.json").write_text("{not-json", encoding="utf-8")
        self.assertIsNone(storage.read_manifest("20260101_010101"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
