#!/usr/bin/env python3
"""Test double for ``run_on_vmanage.py``.

The real script SSHes into vManage, uploads ``bulk-show.py``, runs it, and
optionally SFTPs ``output_*.txt`` files back to ``--local-dir/logs/<ts>/``.

This stub does just enough of that to let us exercise
:mod:`webapp.runner` end-to-end without touching the network:

* It reads a single line of ``stdin`` so the runner's password-via-stdin path
  is exercised (mirrors the real ``getpass`` fallback behaviour).
* It honours ``--local-dir``/``--hosts`` so it can synthesise the same
  ``<local_dir>/logs/<timestamp>/output_<host>.txt`` layout the real script
  produces when ``--download-outputs`` is set.
* It prints ``using remote dir: <remote>/<timestamp>`` so the runner's stdout
  fallback regex has something to latch onto when we deliberately suppress
  the local logs directory.

Behaviour can be controlled with environment variables:

``FAKE_RUN_TS``
    Timestamp string to use (default: ``YYYYMMDD_HHMMSS`` of "now").

``FAKE_RUN_HANG``
    ``"1"`` → sleep forever before producing any output (used to test the
    runner's timeout/kill path).

``FAKE_RUN_NO_LOGS``
    ``"1"`` → skip creating ``logs/<ts>/`` so the runner has to fall back to
    the stdout regex for timestamp detection.

``FAKE_RUN_FAIL``
    ``"1"`` → exit with status 2 after writing outputs (used to test the
    non-zero ``returncode`` path).

``FAKE_RUN_LEAK_PASSWORD``
    ``"1"`` → echo the password to stdout, so we can verify the runner masks
    it before persisting ``run.log``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("vmanage_host")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--remote-dir", required=True)
    parser.add_argument("--local-dir", required=True)
    parser.add_argument("--hosts", default="host.txt")
    parser.add_argument("--commands", default="command.txt")
    parser.add_argument("--controller-commands", default=None)
    parser.add_argument("--edge-commands", default=None)
    parser.add_argument("--bulk-script", default="bulk-show.py")
    parser.add_argument("--download-outputs", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--reject-unknown-hosts", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    password = sys.stdin.readline().rstrip("\n") if args.password is None else args.password
    if os.environ.get("FAKE_RUN_LEAK_PASSWORD") == "1":
        print(f"received password: {password}")
    else:
        print(f"received password length: {len(password)}")

    if os.environ.get("FAKE_RUN_HANG") == "1":
        sys.stdout.flush()
        time.sleep(9999)
        return 0

    timestamp = os.environ.get(
        "FAKE_RUN_TS", datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    remote_dir = f"{args.remote_dir.rstrip('/')}/{timestamp}"
    print(f"using remote dir: {remote_dir}")

    if os.environ.get("FAKE_RUN_NO_LOGS") != "1":
        local_logs = Path(args.local_dir) / "logs" / timestamp
        local_logs.mkdir(parents=True, exist_ok=True)
        host_file = Path(args.local_dir) / args.hosts
        if host_file.is_file():
            for raw in host_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                ip = line.split(",", 1)[0].strip()
                if not ip:
                    continue
                (local_logs / f"output_{ip}.txt").write_text(
                    f"fake output for {ip}\n", encoding="utf-8"
                )

    if os.environ.get("FAKE_RUN_FAIL") == "1":
        print("simulated failure", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
