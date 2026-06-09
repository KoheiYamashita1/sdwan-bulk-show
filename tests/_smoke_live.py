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
import time
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

        # 4) reject empty / comment-only inputs (no-JS path: re-rendered HTML)
        r = client.post(
            "/run",
            data={
                "vmanage_host": "10.0.0.1",
                "user": "admin",
                "password": fake_password,
                "remote_dir": "/home/admin",
                "hosts_text": "# only a comment\n   \n",
                "controller_commands_text": "show version\n",
                "edge_commands_text": "",
                "download_outputs": "on",
            },
        )
        expect(
            r.status_code == 400,
            f"POST /run with comment-only hosts returns 400 (got {r.status_code})",
        )

        # 4a) AJAX validation error returns JSON {error} with the same status
        r = client.post(
            "/run",
            data={
                "vmanage_host": "10.0.0.1",
                "user": "admin",
                "password": fake_password,
                "remote_dir": "/home/admin",
                "hosts_text": "10.0.0.1,admin\n",
                "controller_commands_text": "",
                "edge_commands_text": "",
                "download_outputs": "on",
            },
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        expect(
            r.status_code == 400,
            f"AJAX POST /run with no commands returns 400 (got {r.status_code})",
        )
        expect(
            "error" in r.json(),
            f"AJAX validation error returns JSON error (got {r.text!r})",
        )

        # 5) happy-path AJAX POST /run -> JSON {job_id} (no navigation)
        r = client.post(
            "/run",
            data={
                "vmanage_host": "192.0.2.10",
                "user": "admin",
                "password": fake_password,
                "remote_dir": "/home/admin",
                "hosts_text": "10.0.0.1,user1,pw1\n10.0.0.2,user2,pw2\n",
                "controller_commands_text": "show version\nshow control connections\n",
                "edge_commands_text": "show ip route\n",
                "download_outputs": "on",
            },
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        )
        expect(
            r.status_code == 200,
            f"AJAX POST /run returns 200 (got {r.status_code})",
        )
        body = r.json()
        job_id = body.get("job_id") or ""
        expect(bool(job_id), f"AJAX POST /run returns a job_id (got {body!r})")
        expect(
            fake_password not in r.text,
            "AJAX /run response does NOT contain the cleartext password",
        )

        # 5a) progress page still renders for the no-JS fallback
        r = client.get(f"/runs/active/{job_id}")
        expect(
            r.status_code == 200,
            f"GET /runs/active/{{job_id}} returns 200 (got {r.status_code})",
        )

        # 5b) JSON progress endpoint exposes status/percent and no password
        r = client.get(f"/api/progress/{job_id}")
        expect(
            r.status_code == 200,
            f"GET /api/progress/{{job_id}} returns 200 (got {r.status_code})",
        )
        prog = r.json()
        expect("percent" in prog, f"progress JSON has a 'percent' field (got {list(prog)})")
        expect("status" in prog, f"progress JSON has a 'status' field (got {list(prog)})")
        expect(
            fake_password not in r.text,
            "progress JSON does NOT contain the cleartext password",
        )

        # 5c) poll until the async job finishes, then assert final state
        deadline = time.monotonic() + 15.0
        while prog.get("status") == "running":
            if time.monotonic() > deadline:
                break
            time.sleep(0.1)
            prog = client.get(f"/api/progress/{job_id}").json()
        expect(
            prog.get("status") == "success",
            f"async job finishes with status=success (got {prog.get('status')!r})",
        )
        expect(
            prog.get("timestamp") == fake_ts,
            f"finished job snapshot carries timestamp {fake_ts} (got {prog.get('timestamp')!r})",
        )
        expect(prog.get("percent") == 100, f"finished job is at 100% (got {prog.get('percent')!r})")
        expect(
            fake_password not in json.dumps(prog),
            "finished progress snapshot has no cleartext password",
        )

        # 6) detail page (reachable once the async run wrote logs/<ts>/)
        r = client.get(f"/runs/{fake_ts}")
        expect(r.status_code == 200, f"GET /runs/{fake_ts} returns 200 (got {r.status_code})")
        expect(fake_ts in r.text, "detail page mentions the timestamp")
        expect("output_10.0.0.1.txt" in r.text, "detail page lists output_10.0.0.1.txt")
        expect("output_10.0.0.2.txt" in r.text, "detail page lists output_10.0.0.2.txt")
        expect("manifest.json" in r.text, "detail page lists manifest.json")
        expect("run.log" in r.text, "detail page lists run.log")
        # New model: detail page wires up the pick-two-and-diff widget and must
        # NOT dump raw file bodies inline.
        expect(
            'id="detail-filediff"' in r.text and "/static/filediff.js" in r.text,
            "detail page mounts the pick-two-and-diff widget",
        )
        expect(
            "fake output for 10.0.0.1" not in r.text,
            "detail page does NOT dump raw file bodies inline",
        )

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
        expect(
            manifest.get("controller_commands_count") == 2,
            f"manifest.controller_commands_count == 2 (got {manifest.get('controller_commands_count')})",
        )
        expect(
            manifest.get("edge_commands_count") == 1,
            f"manifest.edge_commands_count == 1 (got {manifest.get('edge_commands_count')})",
        )
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

        # 10a) raw-content JSON endpoint for compare panes
        r = client.get(f"/api/runs/{fake_ts}/file?name=output_10.0.0.1.txt")
        expect(
            r.status_code == 200,
            f"GET /api/runs/{{ts}}/file returns 200 (got {r.status_code})",
        )
        file_json = r.json()
        expect(
            "fake output for 10.0.0.1" in file_json.get("content", ""),
            "file JSON endpoint returns the file content",
        )
        expect(
            "truncated" in file_json,
            f"file JSON endpoint reports truncation (got {list(file_json)})",
        )

        # 10b) file JSON masks the password (run.log) and rejects bad names
        r = client.get(f"/api/runs/{fake_ts}/file?name=run.log")
        expect(r.status_code == 200, f"GET /api/runs/{{ts}}/file run.log 200 (got {r.status_code})")
        expect(
            fake_password not in r.text,
            "file JSON endpoint does NOT contain the cleartext password",
        )
        r = client.get(f"/api/runs/{fake_ts}/file?name=..%2Fmanifest.json")
        expect(
            r.status_code == 404,
            f"file JSON endpoint refuses traversal (got {r.status_code})",
        )

        # 10c) compare page lists output files with select-two + Diff (no raw)
        r = client.get(f"/runs/{fake_ts}/compare")
        expect(r.status_code == 200, f"GET /runs/{{ts}}/compare returns 200 (got {r.status_code})")
        expect(fake_ts in r.text, "compare page mentions the timestamp")
        expect("output_10.0.0.1.txt" in r.text, "compare page references output_10.0.0.1.txt")
        expect("output_10.0.0.2.txt" in r.text, "compare page references output_10.0.0.2.txt")
        expect(
            'id="compare-filediff"' in r.text and "/static/filediff.js" in r.text,
            "compare page mounts the pick-two-and-diff widget",
        )
        expect(
            "fake output for 10.0.0.1" not in r.text,
            "compare page does NOT dump raw file bodies inline",
        )
        expect(
            fake_password not in r.text,
            "compare page does NOT contain the cleartext password",
        )

        # 10d) server-side diff endpoint: differing files
        r = client.get(
            f"/api/runs/{fake_ts}/diff"
            "?a=output_10.0.0.1.txt&b=output_10.0.0.2.txt"
        )
        expect(r.status_code == 200, f"GET /api/runs/{{ts}}/diff 200 (got {r.status_code})")
        diff_json = r.json()
        for key in ("a", "b", "a_truncated", "b_truncated", "diff", "identical"):
            expect(key in diff_json, f"diff JSON has '{key}' (got {list(diff_json)})")
        expect(isinstance(diff_json.get("diff"), list), "diff JSON 'diff' is a list")
        expect(
            diff_json.get("identical") is False,
            f"two different files are NOT identical (got {diff_json.get('identical')!r})",
        )
        expect(
            any(line.startswith("+") for line in diff_json.get("diff", [])),
            "diff of differing files contains an added line",
        )

        # 10e) diffing a file against ITSELF yields identical: true, empty diff
        r = client.get(
            f"/api/runs/{fake_ts}/diff"
            "?a=output_10.0.0.1.txt&b=output_10.0.0.1.txt"
        )
        expect(r.status_code == 200, f"GET /api/runs/{{ts}}/diff self 200 (got {r.status_code})")
        self_json = r.json()
        expect(
            self_json.get("identical") is True,
            f"file diffed against itself is identical (got {self_json.get('identical')!r})",
        )
        expect(
            self_json.get("diff") == [],
            f"self-diff has an empty diff list (got {self_json.get('diff')!r})",
        )

        # 10f) diff endpoint refuses traversal / unknown files (404)
        r = client.get(
            f"/api/runs/{fake_ts}/diff"
            "?a=..%2Fmanifest.json&b=output_10.0.0.1.txt"
        )
        expect(r.status_code == 404, f"diff endpoint refuses traversal (got {r.status_code})")
        r = client.get(
            f"/api/runs/{fake_ts}/diff?a=output_10.0.0.1.txt&b=nope.txt"
        )
        expect(r.status_code == 404, f"diff endpoint 404s a missing file (got {r.status_code})")

        # 10g) open endpoint: bad-input / 404 paths only (never spawn an app)
        r = client.post(
            f"/runs/{fake_ts}/open",
            json={"target": "bogus"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        expect(
            r.status_code == 400,
            f"open with a bogus target returns 400 (got {r.status_code})",
        )
        r = client.post(
            f"/runs/{fake_ts}/open",
            json={"name": "../manifest.json"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        expect(
            r.status_code == 404,
            f"open with a traversal name returns 404 (got {r.status_code})",
        )
        r = client.post(
            "/runs/19990101_000000/open",
            json={"target": "finder"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        expect(
            r.status_code == 404,
            f"open with an unknown run returns 404 (got {r.status_code})",
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
