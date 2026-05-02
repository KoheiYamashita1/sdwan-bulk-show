"""End-to-end smoke driver for the local web UI.

This is *not* a pytest unit test; it's a one-shot synthetic run used by the
plan's ``live_test`` to-do. It boots ``webapp.main.app`` via Starlette's
``TestClient``, redirects the runner at a sandbox repo root, and asserts the
GET/POST flow that a real browser would exercise.

Run with:

    .venv/bin/python tests/_smoke_live.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _build_sandbox(root: Path) -> None:
    """Lay out a fake repo containing bulk-show.py and run_on_vmanage.py."""

    (root / "logs").mkdir(parents=True, exist_ok=True)
    # bulk-show.py just needs to exist so the runner's symlink check passes.
    (root / "bulk-show.py").write_text(
        "# fake bulk-show.py used by the smoke test\n",
        encoding="utf-8",
    )
    # run_on_vmanage.py is the fake CLI driver.
    fake_src = REPO_ROOT / "tests" / "fake_run_on_vmanage.py"
    shutil.copyfile(fake_src, root / "run_on_vmanage.py")
    (root / "run_on_vmanage.py").chmod(0o755)


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="sdwan-smoke-"))
    print(f"[smoke] sandbox = {sandbox}")
    _build_sandbox(sandbox)

    from webapp import main as webapp_main, runner, storage
    from fastapi.testclient import TestClient

    runner.REPO_ROOT = sandbox
    runner.LOGS_DIR = sandbox / "logs"
    runner.BULK_SCRIPT = sandbox / "bulk-show.py"
    runner.RUN_ON_VMANAGE = sandbox / "run_on_vmanage.py"
    storage.LOGS_DIR = sandbox / "logs"

    fake_ts = "20260502_010101"
    fake_password = "Cisco12345!"
    os.environ["FAKE_RUN_TS"] = fake_ts
    os.environ["FAKE_RUN_LEAK_PASSWORD"] = "1"  # so we can verify masking

    failures: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        if cond:
            print(f"[ ok ] {msg}")
        else:
            print(f"[FAIL] {msg}", file=sys.stderr)
            failures.append(msg)

    with TestClient(webapp_main.app) as client:
        # 1) liveness
        r = client.get("/healthz")
        expect(r.status_code == 200, f"GET /healthz returns 200 (got {r.status_code})")
        expect(r.json() == {"status": "ok"}, f"healthz body is {{status: ok}} (got {r.json()})")

        # 2) form page renders
        r = client.get("/")
        expect(r.status_code == 200, f"GET / returns 200 (got {r.status_code})")
        expect(
            "Run bulk show" in r.text or "vmanage_host" in r.text,
            "GET / contains the run form",
        )

        # 3) empty runs list
        r = client.get("/runs")
        expect(r.status_code == 200, f"GET /runs returns 200 (got {r.status_code})")
        expect("No runs yet" in r.text or "Past runs" in r.text, "GET /runs renders even when empty")

        # 4) reject empty / comment-only inputs
        r = client.post(
            "/run",
            data={
                "vmanage_host": "10.0.0.1",
                "user": "admin",
                "password": fake_password,
                "remote_dir": "/home/admin",
                "hosts_text": "# only a comment\n   \n",
                "commands_text": "show version\n",
                "download_outputs": "on",
            },
        )
        expect(
            r.status_code == 400,
            f"POST /run with comment-only hosts returns 400 (got {r.status_code})",
        )

        # 5) happy-path POST /run -> 303 -> /runs/<ts>
        r = client.post(
            "/run",
            data={
                "vmanage_host": "192.0.2.10",
                "user": "admin",
                "password": fake_password,
                "remote_dir": "/home/admin",
                "hosts_text": "10.0.0.1,user1,pw1\n10.0.0.2,user2,pw2\n",
                "commands_text": "show version\nshow ip route\n",
                "download_outputs": "on",
            },
            follow_redirects=False,
        )
        expect(
            r.status_code == 303,
            f"POST /run returns 303 redirect (got {r.status_code})",
        )
        expect(
            r.headers.get("location") == f"/runs/{fake_ts}",
            f"POST /run redirects to /runs/{fake_ts} (got {r.headers.get('location')!r})",
        )

        # 6) detail page
        r = client.get(f"/runs/{fake_ts}")
        expect(r.status_code == 200, f"GET /runs/{fake_ts} returns 200 (got {r.status_code})")
        expect(fake_ts in r.text, "detail page mentions the timestamp")
        expect("output_10.0.0.1.txt" in r.text, "detail page lists output_10.0.0.1.txt")
        expect("output_10.0.0.2.txt" in r.text, "detail page lists output_10.0.0.2.txt")
        expect("manifest.json" in r.text, "detail page lists manifest.json")
        expect("run.log" in r.text, "detail page lists run.log")

        # 7) file viewer renders the per-host output
        r = client.get(f"/runs/{fake_ts}/files/output_10.0.0.1.txt")
        expect(r.status_code == 200, f"GET output_10.0.0.1.txt returns 200 (got {r.status_code})")
        expect("fake output for 10.0.0.1" in r.text, "file viewer shows fake-script payload")

        # 8) run.log password masking
        r = client.get(f"/runs/{fake_ts}/files/run.log")
        expect(r.status_code == 200, f"GET run.log returns 200 (got {r.status_code})")
        expect(fake_password not in r.text, "run.log does NOT contain the cleartext password")
        expect("***" in r.text, "run.log contains masked password marker '***'")

        # 9) manifest contents
        manifest_path = sandbox / "logs" / fake_ts / "manifest.json"
        expect(manifest_path.is_file(), f"manifest.json exists at {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expect(manifest.get("timestamp") == fake_ts, "manifest.timestamp matches fake_ts")
        expect(manifest.get("vmanage_host") == "192.0.2.10", "manifest.vmanage_host matches")
        expect(manifest.get("vmanage_user") == "admin", "manifest.vmanage_user matches")
        expect(manifest.get("hosts_count") == 2, f"manifest.hosts_count == 2 (got {manifest.get('hosts_count')})")
        expect(manifest.get("commands_count") == 2, f"manifest.commands_count == 2 (got {manifest.get('commands_count')})")
        expect(manifest.get("returncode") == 0, f"manifest.returncode == 0 (got {manifest.get('returncode')})")
        expect(manifest.get("status") == "success", f"manifest.status == success (got {manifest.get('status')})")
        outputs = manifest.get("outputs") or []
        expect(
            sorted(outputs) == ["output_10.0.0.1.txt", "output_10.0.0.2.txt"],
            f"manifest.outputs lists both files (got {outputs})",
        )

        # 10) path traversal is refused
        r = client.get(f"/runs/{fake_ts}/files/..%2Fmanifest.json")
        expect(
            r.status_code == 404,
            f"path-traversal attempt returns 404 (got {r.status_code})",
        )

        # 11) /runs lists the new entry
        r = client.get("/runs")
        expect(r.status_code == 200, f"GET /runs (after run) returns 200 (got {r.status_code})")
        expect(fake_ts in r.text, "/runs index page now lists the new timestamp")

    if failures:
        print(f"\n[smoke] {len(failures)} assertion(s) failed:", file=sys.stderr)
        for msg in failures:
            print(f"  - {msg}", file=sys.stderr)
        return 1
    print("\n[smoke] all assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
