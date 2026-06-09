"""Unit tests for :mod:`webapp.runner`.

These tests never exercise SSH; ``run_on_vmanage.py`` is replaced by
:mod:`tests.fake_run_on_vmanage`, which manipulates a fake ``logs/<ts>/``
tree so we can verify the runner's timestamp detection, manifest
generation, password masking, timeout handling, and concurrency lock.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
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
        controller_commands_text="",
        edge_commands_text="",
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
            {
                "download_outputs": True,
                "verbose": False,
                "reject_unknown_hosts": False,
                "retries": 0,
                "max_workers": None,
                "output_formats": ["text"],
                "controller_port": 22,
            },
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


# ---------------------------------------------------------------------------
# Progress parsing (RunJob.update_from_line)
# ---------------------------------------------------------------------------


class ProgressParsingTests(unittest.TestCase):
    def _job(self) -> runner.RunJob:
        # 2 hosts x 2 commands = 4 total command milestones. ``commands_total``
        # is now the accurate denominator directly (not multiplied by hosts).
        return runner.RunJob.new(hosts_total=2, commands_total=4)

    def test_milestone_percent_and_phase_transitions(self) -> None:
        job = self._job()

        job.update_from_line("[vmanage] connecting...")
        self.assertEqual(job.percent, 5)
        self.assertEqual(job.phase, "Connecting to vManage")

        job.update_from_line("[vmanage] connected")
        self.assertEqual(job.percent, 10)
        self.assertEqual(job.phase, "Connected to vManage")

        job.update_from_line("[vmanage] uploading files to /home/admin")
        self.assertEqual(job.percent, 20)
        self.assertEqual(job.phase, "Uploading files")

        job.update_from_line("[vmanage] running via vshell session")
        self.assertEqual(job.percent, 25)
        self.assertEqual(job.phase, "Running on vManage")

    def test_command_done_counter_and_fine_grained_percent(self) -> None:
        job = self._job()
        job.update_from_line("[vmanage] running via vshell session")
        job.update_from_line("[main] starting 2 host(s) x 2 command(s)")
        self.assertEqual(job.hosts_total, 2)

        # An edge "connected" line must not regress the bar from 25.
        job.update_from_line("[10.0.0.1] connected")
        self.assertEqual(job.percent, 25)

        # Four commands complete -> 25 + 65 * (n/4).
        job.update_from_line("[10.0.0.1] done: show version")
        self.assertEqual(job.commands_done, 1)
        self.assertEqual(job.percent, 25 + int(65 * (1 / 4)))  # 41

        job.update_from_line("[10.0.0.1] done: show ip route")
        self.assertEqual(job.commands_done, 2)
        self.assertEqual(job.percent, 25 + int(65 * (2 / 4)))  # 57

        job.update_from_line("[10.0.0.2] done: show version")
        job.update_from_line("[10.0.0.2] done: show ip route")
        self.assertEqual(job.commands_done, 4)
        self.assertEqual(job.percent, 90)

    def test_main_done_does_not_increment_command_counter(self) -> None:
        job = self._job()
        job.update_from_line("[10.0.0.1] done: show version")
        self.assertEqual(job.commands_done, 1)

        job.update_from_line("[main] done: success=2, failed=0")
        self.assertEqual(job.commands_done, 1, "[main] done: must NOT count as a command")
        self.assertEqual(job.percent, 92)
        self.assertEqual(job.phase, "Finalizing run")

    def test_download_and_vmanage_done_milestones(self) -> None:
        job = self._job()
        job.update_from_line("[main] done: success=2, failed=0")
        job.update_from_line("[vmanage] downloading output_10.0.0.1.txt -> logs/...")
        self.assertEqual(job.percent, 95)
        self.assertEqual(job.phase, "Downloading outputs")

        job.update_from_line("[vmanage] done")
        self.assertEqual(job.percent, 98)
        self.assertEqual(job.phase, "Wrapping up")

    def test_percent_is_monotonic(self) -> None:
        job = self._job()
        job.update_from_line("[vmanage] running via vshell session")  # 25
        job.update_from_line("[vmanage] connecting...")  # would be 5
        self.assertEqual(job.percent, 25, "percent must never move backwards")

    def test_log_tail_is_capped_and_stores_given_lines(self) -> None:
        job = self._job()
        for i in range(runner.LOG_TAIL_MAX + 25):
            job.update_from_line(f"line {i}")
        snap = job.snapshot()
        self.assertEqual(len(snap["log_tail"]), runner.LOG_TAIL_MAX)
        # Oldest lines were dropped; newest retained.
        self.assertEqual(snap["log_tail"][-1], f"line {runner.LOG_TAIL_MAX + 24}")

    def test_masked_line_keeps_password_out_of_log_tail(self) -> None:
        job = self._job()
        secret = "hunter2"
        # update_from_line is always fed already-masked lines by the runner.
        masked = runner._mask_password(f"[x] password is {secret}", secret)
        job.update_from_line(masked)
        snap = job.snapshot()
        joined = "\n".join(snap["log_tail"])
        self.assertNotIn(secret, joined)
        self.assertIn("***", joined)


# ---------------------------------------------------------------------------
# Asynchronous execution (start_run_async + job registry)
# ---------------------------------------------------------------------------


class StartRunAsyncTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-runner-async-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def _start(self, **overrides) -> str:
        repo = overrides.pop("repo_root", None) or self._make_repo()
        env_overrides = overrides.pop("env", {})
        form = overrides.pop("form", _form())

        # The worker thread reads these env vars asynchronously, so we must
        # keep them set past this call; restore via addCleanup, not a finally.
        previous_env = {k: os.environ.get(k) for k in env_overrides}

        def _restore() -> None:
            for k, original in previous_env.items():
                if original is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = original

        os.environ.update({k: v for k, v in env_overrides.items() if v is not None})
        self.addCleanup(_restore)
        return runner.start_run_async(
            form,
            repo_root=repo,
            bulk_script=REAL_BULK_SCRIPT,
            run_on_vmanage=FAKE_RUN_ON_VMANAGE,
            python_executable=sys.executable,
            timeout=30.0,
        )

    def _wait_for_finish(self, job_id: str, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        snap = runner.job_snapshot(job_id)
        while snap and snap["status"] == "running":
            if time.monotonic() > deadline:
                self.fail(f"job {job_id} did not finish within {timeout}s")
            time.sleep(0.05)
            snap = runner.job_snapshot(job_id)
        self.assertIsNotNone(snap, "job snapshot disappeared")
        return snap

    def test_happy_path_sets_success_timestamp_and_releases_lock(self) -> None:
        repo = self._make_repo()
        job_id = self._start(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260201_010101", "FAKE_RUN_LEAK_PASSWORD": "1"},
            form=_form(password="topSecret!42"),
        )
        self.assertTrue(job_id)

        snap = self._wait_for_finish(job_id)
        self.assertEqual(snap["status"], "success")
        self.assertEqual(snap["timestamp"], "20260201_010101")
        self.assertEqual(snap["percent"], 100)
        self.assertIsNotNone(snap["ended_at"])

        # The promoted outputs really landed on disk.
        run_dir = repo / "logs" / "20260201_010101"
        self.assertTrue((run_dir / "manifest.json").is_file())

        # The lock is released once the worker thread finishes.
        self.assertFalse(runner.RUN_LOCK.locked(), "RUN_LOCK must be released")

        # The streamed log tail is password-masked, never raw.
        joined = "\n".join(snap["log_tail"])
        self.assertNotIn("topSecret!42", joined)

    def test_unknown_job_id_snapshot_is_none(self) -> None:
        self.assertIsNone(runner.job_snapshot("does-not-exist"))

    def test_busy_lock_makes_start_run_async_raise(self) -> None:
        repo = self._make_repo()
        self.assertTrue(runner.RUN_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(runner.RunBusyError):
                self._start(repo_root=repo, env={"FAKE_RUN_TS": "20260202_020202"})
        finally:
            runner.RUN_LOCK.release()

        # After releasing, an async run succeeds and releases the lock again.
        job_id = self._start(
            repo_root=repo, env={"FAKE_RUN_TS": "20260202_020202"}
        )
        snap = self._wait_for_finish(job_id)
        self.assertEqual(snap["status"], "success")
        self.assertFalse(runner.RUN_LOCK.locked())

    def test_request_cancel_marks_job_cancelled(self) -> None:
        """A hung run can be cancelled; status flips to 'cancelled' (B3)."""

        repo = self._make_repo()
        job_id = self._start(
            repo_root=repo, env={"FAKE_RUN_HANG": "1"}
        )

        # Wait until the worker has spawned the subprocess and recorded it.
        job = runner.get_job(job_id)
        deadline = time.monotonic() + 5.0
        while job.proc is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(job.proc, "subprocess handle never recorded")

        self.assertEqual(runner.request_cancel(job_id), "cancelled")

        snap = self._wait_for_finish(job_id)
        self.assertEqual(snap["status"], "cancelled")
        self.assertEqual(snap["percent"], 100)
        self.assertFalse(runner.RUN_LOCK.locked(), "RUN_LOCK must be released")

        # Partial run.log + a manifest with status 'cancelled' are preserved.
        ts = snap["timestamp"]
        run_dir = repo / "logs" / ts
        self.assertTrue((run_dir / "run.log").is_file())
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "cancelled")

    def test_request_cancel_unknown_job_returns_none(self) -> None:
        self.assertIsNone(runner.request_cancel("does-not-exist"))

    def test_request_cancel_finished_job_returns_terminal_status(self) -> None:
        repo = self._make_repo()
        job_id = self._start(
            repo_root=repo, env={"FAKE_RUN_TS": "20260203_030303"}
        )
        snap = self._wait_for_finish(job_id)
        self.assertEqual(snap["status"], "success")
        # Cancelling an already-finished job reports its terminal status.
        self.assertEqual(runner.request_cancel(job_id), "success")


# ---------------------------------------------------------------------------
# Split command lists: fallback rule, argv, manifest counts, progress total
# ---------------------------------------------------------------------------


class SplitCommandsTests(unittest.TestCase):
    def test_each_type_uses_its_own_box(self) -> None:
        form = _form(
            commands_text="",
            controller_commands_text="show control connections\n",
            edge_commands_text="show ip route\n",
        )
        self.assertEqual(form.controller_commands(), "show control connections\n")
        self.assertEqual(form.edge_commands(), "show ip route\n")
        self.assertEqual(form.controller_commands_count(), 1)
        self.assertEqual(form.edge_commands_count(), 1)

    def test_empty_box_falls_back_to_other_box(self) -> None:
        form = _form(
            commands_text="",
            controller_commands_text="",
            edge_commands_text="show ip route\nshow version\n",
        )
        # Controllers have no list of their own, so they reuse the edge box.
        self.assertEqual(form.controller_commands(), "show ip route\nshow version\n")
        self.assertEqual(form.edge_commands(), "show ip route\nshow version\n")

    def test_legacy_commands_text_seeds_both_types(self) -> None:
        form = _form(
            commands_text="show version\n",
            controller_commands_text="",
            edge_commands_text="",
        )
        self.assertEqual(form.controller_commands(), "show version\n")
        self.assertEqual(form.edge_commands(), "show version\n")
        # base_commands prefers the legacy box for the positional file.
        self.assertEqual(form.base_commands(), "show version\n")

    def test_validate_rejects_all_command_boxes_empty(self) -> None:
        with self.assertRaises(runner.RunInputError):
            runner.validate_form(
                _form(commands_text="", controller_commands_text="",
                      edge_commands_text="")
            )

    def test_validate_accepts_only_controller_box(self) -> None:
        runner.validate_form(
            _form(commands_text="", controller_commands_text="show foo\n",
                  edge_commands_text="")
        )

    def test_build_argv_includes_split_flags_when_named(self) -> None:
        argv = runner._build_argv(
            python_executable="py",
            run_on_vmanage=Path("/x/run_on_vmanage.py"),
            form=_form(),
            tempdir=Path("/tmp/x"),
            hosts_name="host.txt",
            commands_name="command.txt",
            bulk_name="bulk-show.py",
            controller_commands_name="controller_command.txt",
            edge_commands_name="edge_command.txt",
        )
        self.assertIn("--controller-commands", argv)
        self.assertIn("controller_command.txt", argv)
        self.assertIn("--edge-commands", argv)
        self.assertIn("edge_command.txt", argv)

    def test_build_argv_omits_split_flags_when_absent(self) -> None:
        argv = runner._build_argv(
            python_executable="py",
            run_on_vmanage=Path("/x/run_on_vmanage.py"),
            form=_form(),
            tempdir=Path("/tmp/x"),
            hosts_name="host.txt",
            commands_name="command.txt",
            bulk_name="bulk-show.py",
        )
        self.assertNotIn("--controller-commands", argv)
        self.assertNotIn("--edge-commands", argv)

    def test_progress_total_sums_per_host_type_counts(self) -> None:
        # One controller (2 cmds) + one edge (1 cmd) -> 3 milestones total.
        form = _form(
            hosts_text="10.0.0.1,admin,p1,type=controller\n10.0.0.2,admin,p2\n",
            commands_text="",
            controller_commands_text="show a\nshow b\n",
            edge_commands_text="show c\n",
        )
        self.assertEqual(form.progress_command_total(REAL_BULK_SCRIPT), 3)


class SplitCommandsRunTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-split-test-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def test_manifest_records_both_command_counts(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260301_030303"},
            form=_form(
                commands_text="",
                controller_commands_text="show a\nshow b\n",
                edge_commands_text="show c\n",
            ),
        )
        manifest = json.loads(
            (repo / "logs" / result.timestamp / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["controller_commands_count"], 2)
        self.assertEqual(manifest["edge_commands_count"], 1)
        # Backward-compatible single figure stays present (max of both).
        self.assertEqual(manifest["commands_count"], 2)


# ---------------------------------------------------------------------------
# C3: CLI-option argv forwarding + json/csv output promotion
# ---------------------------------------------------------------------------


class KnobForwardingArgvTests(unittest.TestCase):
    def _argv(self, **form_overrides) -> list[str]:
        return runner._build_argv(
            python_executable="py",
            run_on_vmanage=Path("/x/run_on_vmanage.py"),
            form=_form(**form_overrides),
            tempdir=Path("/tmp/x"),
            hosts_name="host.txt",
            commands_name="command.txt",
            bulk_name="bulk-show.py",
        )

    def test_forwards_set_knobs(self) -> None:
        argv = self._argv(
            retries=3,
            max_workers=4,
            output_formats=["text", "json"],
            controller_port=2222,
        )
        self.assertEqual(argv[argv.index("--retries") + 1], "3")
        self.assertEqual(argv[argv.index("--max-workers") + 1], "4")
        self.assertEqual(argv[argv.index("--controller-port") + 1], "2222")
        self.assertEqual(argv[argv.index("--output-format") + 1], "text,json")

    def test_omits_default_optional_knobs_but_keeps_explicit_ones(self) -> None:
        argv = self._argv()  # retries=0, max_workers=None, formats=["text"]
        self.assertNotIn("--retries", argv)
        self.assertNotIn("--max-workers", argv)
        # controller-port and output-format are always forwarded explicitly.
        self.assertEqual(argv[argv.index("--controller-port") + 1], "22")
        self.assertEqual(argv[argv.index("--output-format") + 1], "text")


class OutputFormatPromotionTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-fmt-test-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def test_json_and_csv_outputs_are_promoted_and_recorded(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_TS": "20260701_010101"},
            form=_form(output_formats=["text", "json", "csv"]),
        )
        run_dir = repo / "logs" / result.timestamp
        names = sorted(
            p.name for p in run_dir.iterdir() if p.name.startswith("output_")
        )
        for expected in (
            "output_10.0.0.1.txt",
            "output_10.0.0.1.json",
            "output_10.0.0.1.csv",
            "output_10.0.0.2.json",
        ):
            self.assertIn(expected, names)

        manifest = json.loads(
            (run_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["options"]["output_formats"], ["text", "json", "csv"]
        )
        self.assertEqual(manifest["options"]["controller_port"], 22)
        self.assertEqual(manifest["options"]["retries"], 0)


# ---------------------------------------------------------------------------
# B1: process-group kill on timeout
# ---------------------------------------------------------------------------


class ProcessGroupKillTests(_IsolatedRepoMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-killpg-test-")
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self) -> None:
        if runner.RUN_LOCK.locked():
            try:
                runner.RUN_LOCK.release()
            except RuntimeError:
                pass

    def test_timeout_kills_the_whole_process_tree(self) -> None:
        repo = self._make_repo()
        result = self._run(
            repo_root=repo,
            env={"FAKE_RUN_SPAWN_CHILD": "1", "FAKE_RUN_HANG": "1"},
            timeout=1.5,
        )
        self.assertTrue(result.timed_out)

        match = re.search(r"child pid: (\d+)", result.log)
        self.assertIsNotNone(match, f"child pid not found in log:\n{result.log}")
        child_pid = int(match.group(1))

        # The grandchild lived in the wrapper's process group, so killpg must
        # have reaped it. Poll briefly for the OS to finish tearing it down.
        deadline = time.monotonic() + 5.0
        gone = False
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except (ProcessLookupError, OSError):
                gone = True
                break
            time.sleep(0.05)
        self.assertTrue(gone, f"grandchild {child_pid} survived the killpg")


# ---------------------------------------------------------------------------
# B2: job registry cap / TTL eviction
# ---------------------------------------------------------------------------


class JobRegistryEvictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_max = runner.MAX_JOBS
        self._saved_ttl = runner.JOB_TTL_SECONDS
        with runner._JOBS_LOCK:
            self._saved_jobs = dict(runner._JOBS)
            runner._JOBS.clear()

    def tearDown(self) -> None:
        runner.MAX_JOBS = self._saved_max
        runner.JOB_TTL_SECONDS = self._saved_ttl
        with runner._JOBS_LOCK:
            runner._JOBS.clear()
            runner._JOBS.update(self._saved_jobs)

    def _terminal_job(self) -> runner.RunJob:
        job = runner.RunJob.new(hosts_total=1, commands_total=1)
        job.status = "success"
        return job

    def test_cap_evicts_oldest_jobs(self) -> None:
        runner.MAX_JOBS = 5
        runner.JOB_TTL_SECONDS = 1e9  # disable TTL pass for this test
        ids = []
        for _ in range(8):
            job = self._terminal_job()
            runner._register_job(job)
            ids.append(job.job_id)
        with runner._JOBS_LOCK:
            self.assertEqual(len(runner._JOBS), 5)
        # The three oldest were evicted; the newest survive.
        self.assertIsNone(runner.get_job(ids[0]))
        self.assertIsNone(runner.get_job(ids[2]))
        self.assertIsNotNone(runner.get_job(ids[-1]))

    def test_ttl_evicts_old_terminal_jobs(self) -> None:
        runner.JOB_TTL_SECONDS = 0.0  # any terminal job is "expired"
        old = self._terminal_job()
        runner._register_job(old)
        # Registering a fresh job triggers eviction; the old terminal job goes.
        fresh = runner.RunJob.new(hosts_total=1, commands_total=1)  # running
        runner._register_job(fresh)
        self.assertIsNone(runner.get_job(old.job_id))
        self.assertIsNotNone(runner.get_job(fresh.job_id))


# ---------------------------------------------------------------------------
# C5: per-host manifest roll-up
# ---------------------------------------------------------------------------


class CollectHostResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="webapp-hostresults-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_parses_text_session_end_marker(self) -> None:
        (self.dir / "output_10.0.0.1_20260101_010101.txt").write_text(
            "===== session begin: 10.0.0.1 user=admin port=830 started=t =====\n"
            "show version output...\n"
            "===== session end:   10.0.0.1 status=success ended=t duration=1.00s =====\n",
            encoding="utf-8",
        )
        results = runner.collect_host_results("", self.dir)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["host"], "10.0.0.1")
        self.assertEqual(results[0]["status"], "success")

    def test_json_is_preferred_and_carries_device_type(self) -> None:
        (self.dir / "output_10.0.0.2_20260101_010101.json").write_text(
            json.dumps(
                {
                    "host": "10.0.0.2",
                    "device_type": "controller",
                    "status": "auth_error_ssh",
                    "error": "auth error (ssh): bad",
                }
            ),
            encoding="utf-8",
        )
        results = runner.collect_host_results("", self.dir)
        self.assertEqual(results[0]["device_type"], "controller")
        self.assertEqual(results[0]["status"], "auth_error_ssh")
        self.assertIn("auth error", results[0]["error"])

    def test_stdout_error_fallback_for_missing_file(self) -> None:
        results = runner.collect_host_results(
            "[10.0.0.9] auth error (ssh): nope\n", self.dir
        )
        self.assertEqual(results[0]["host"], "10.0.0.9")
        self.assertEqual(results[0]["status"], "error")

    def test_host_counts_from_main_done_line(self) -> None:
        ok, failed = runner._host_counts(
            "[main] done: success=3, failed=1\n", []
        )
        self.assertEqual((ok, failed), (3, 1))

    def test_host_counts_fallback_from_rows(self) -> None:
        rows = [
            {"host": "a", "status": "success"},
            {"host": "b", "status": "auth_error_ssh"},
        ]
        self.assertEqual(runner._host_counts("", rows), (1, 1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
