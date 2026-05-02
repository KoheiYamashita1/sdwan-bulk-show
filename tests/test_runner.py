"""Unit tests for :mod:`webapp.runner`.

These tests never exercise SSH; ``run_on_vmanage.py`` is replaced by
:mod:`tests.fake_run_on_vmanage`, which manipulates a fake ``logs/<ts>/``
tree so we can verify the runner's timestamp detection, manifest
generation, password masking, timeout handling, and concurrency lock.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from threading import Thread

from webapp import runner

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_RUN_ON_VMANAGE = Path(__file__).resolve().parent / "fake_run_on_vmanage.py"
REAL_BULK_SCRIPT = REPO_ROOT / "bulk-show.py"


def _form(**overrides) -> runner.RunForm:
    """Return a populated :class:`RunForm` suitable for the fake subprocess."""

    defaults = dict(
        vmanage_host="vmanage.test",
        user="admin",
        password="s3cretP@ss!",
        remote_dir="/home/admin",
        hosts_text="10.0.0.1,admin,p1\n10.0.0.2,admin,p2\n",
        commands_text="show version\nshow ip route summary\n",
        download_outputs=True,
        verbose=False,
        reject_unknown_hosts=False,
    )
    defaults.update(overrides)
    return runner.RunForm(**defaults)


class _IsolatedRepoMixin:
    """Provide a per-test ``repo_root`` so we don't litter the real ``logs/``."""

    def _make_repo(self) -> Path:
        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        (repo / "logs").mkdir()
        return repo

    def _run(self, **overrides) -> runner.RunResult:
        repo = overrides.pop("repo_root", None) or self._make_repo()
        env_overrides = overrides.pop("env", {})
        timeout = overrides.pop("timeout", 30.0)
        form = overrides.pop("form", _form())

        previous_env = {k: os.environ.get(k) for k in env_overrides}
        os.environ.update({k: v for k, v in env_overrides.items() if v is not None})
        for k, v in env_overrides.items():
            if v is None and k in os.environ:
                del os.environ[k]

        try:
            return runner.run_via_vmanage(
                form,
                repo_root=repo,
                bulk_script=REAL_BULK_SCRIPT,
                run_on_vmanage=FAKE_RUN_ON_VMANAGE,
                python_executable=sys.executable,
                timeout=timeout,
            )
        finally:
            for k, original in previous_env.items():
                if original is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = original


# ---------------------------------------------------------------------------
# validate_form
# ---------------------------------------------------------------------------


class ValidateFormTests(unittest.TestCase):
    def test_rejects_empty_vmanage_host(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(vmanage_host=" "))

    def test_rejects_empty_user(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(user=""))

    def test_rejects_empty_password(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(password=""))

    def test_rejects_empty_remote_dir(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(remote_dir=""))

    def test_rejects_blank_hosts_text(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(hosts_text="   \n# comment-only\n"))

    def test_rejects_blank_commands_text(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(commands_text="\n\n"))

    def test_rejects_oversized_hosts_text(self) -> None:
        oversized = "a" * (runner.MAX_INPUT_BYTES + 1)
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(hosts_text=oversized))

    def test_rejects_oversized_commands_text(self) -> None:
        oversized = "b" * (runner.MAX_INPUT_BYTES + 1)
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(_form(commands_text=oversized))

    def test_accepts_minimum_valid_form(self) -> None:
        runner.validate_form(_form())  # must not raise


# ---------------------------------------------------------------------------
# run_via_vmanage happy / fallback paths
# ---------------------------------------------------------------------------


class RunViaVManageTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-runner-test-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        # Defensive: make sure we don't leak the lock across tests if a body
        # raised before run_via_vmanage's finally fired.
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def test_happy_path_writes_outputs_and_manifest(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260101_010101"},
        )

        self.assertEqual(result.timestamp, "20260101_010101")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertGreaterEqual(result.duration_sec, 0.0)

        run_dir = repo / "logs" / "20260101_010101"
        self.assertTrue(run_dir.is_dir(), f"run dir missing: {run_dir}")

        # Output files for the two hosts in the form should be promoted.
        self.assertEqual(
            sorted(p.name for p in run_dir.iterdir() if p.name.startswith("output_")),
            ["output_10.0.0.1.txt", "output_10.0.0.2.txt"],
        )
        self.assertEqual(
            sorted(result.output_files), ["output_10.0.0.1.txt", "output_10.0.0.2.txt"]
        )

        # run.log mirrors the masked stdout the runner returned.
        run_log = (run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("using remote dir:", run_log)
        self.assertEqual(run_log, result.log)

        # manifest carries every expected field.
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["timestamp"], "20260101_010101")
        self.assertEqual(manifest["vmanage_host"], "vmanage.test")
        self.assertEqual(manifest["vmanage_user"], "admin")
        self.assertEqual(manifest["hosts_count"], 2)
        self.assertEqual(manifest["commands_count"], 2)
        self.assertEqual(manifest["outputs_count"], 2)
        self.assertEqual(manifest["status"], "success")
        self.assertEqual(manifest["returncode"], 0)
        self.assertEqual(
            manifest["options"],
            {"download_outputs": True, "verbose": False, "reject_unknown_hosts": False},
        )

    def test_password_is_masked_in_run_log(self) -> None:
        repo = self._make_repo()
        secret = "topSecret!42"
        result = self._run(
            repo_root=repo,
            form=_form(password=secret),
            env={"FAKE_RUN_TS": "20260102_020202", "FAKE_RUN_LEAK_PASSWORD": "1"},
        )

        run_log = (repo / "logs" / result.timestamp / "run.log").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(secret, run_log, "raw password leaked into run.log")
        self.assertIn("***", run_log, "masked sentinel missing from run.log")

    def test_timestamp_falls_back_to_stdout_regex(self) -> None:
        """If the subprocess produces no logs/ subdir, stdout is parsed."""

        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260103_030303", "FAKE_RUN_NO_LOGS": "1"},
        )

        self.assertEqual(result.timestamp, "20260103_030303")
        # No outputs were promoted (the fake didn't create logs/<ts>/).
        self.assertEqual(result.output_files, [])
        self.assertTrue(
            (repo / "logs" / "20260103_030303" / "run.log").is_file(),
            "run.log must still be written even when no outputs land",
        )
        manifest = json.loads(
            (repo / "logs" / "20260103_030303" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["outputs_count"], 0)
        self.assertEqual(manifest["status"], "success")

    def test_failure_returncode_marks_status_failed(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260104_040404", "FAKE_RUN_FAIL": "1"},
        )

        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.timed_out)
        manifest = json.loads(
            (repo / "logs" / "20260104_040404" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["returncode"], 2)

    def test_timeout_kills_subprocess_and_marks_status(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_HANG": "1"},
            timeout=1.5,
        )

        self.assertTrue(result.timed_out, "timed_out flag should be set")
        run_dir = repo / "logs" / result.timestamp
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "timeout")
        run_log = (run_dir / "run.log").read_text(encoding="utf-8")
        self.assertIn("subprocess timed out", run_log)


# ---------------------------------------------------------------------------
# Concurrency lock
# ---------------------------------------------------------------------------


class RunLockTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-runner-lock-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def test_busy_lock_raises_run_busy_error(self) -> None:
        # Reuse a single repo dir so both attempts (the rejected one and the
        # follow-up success) write into the same isolated workspace.
        repo = self._make_repo()

        # Simulate an in-flight run by holding the lock manually.
        self.assertTrue(runner.RUN_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(runner.RunBusyError):
                self._run(repo_root=repo, env={"FAKE_RUN_TS": "20260105_050505"})
        finally:
            runner.RUN_LOCK.release()

        # After releasing, a fresh run must succeed.
        result = self._run(repo_root=repo, env={"FAKE_RUN_TS": "20260105_050505"})
        self.assertEqual(result.timestamp, "20260105_050505")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
