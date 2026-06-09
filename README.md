# sdwan-bulk-show

This repo provides three ways to run `bulk-show.py` against a fleet of SD-WAN
devices:

1. [`run_on_vmanage.py`](run_on_vmanage.py) — CLI wrapper that ships
   `bulk-show.py` to vManage, executes it inside `vshell`, and pulls the logs
   back to your laptop.
2. [`webapp/`](webapp/) — a small FastAPI + Uvicorn UI (`python -m webapp`)
   that drives the same wrapper from a browser on `127.0.0.1`. See
   [Web UI (local browser)](#web-ui-local-browser).
3. [`bulk-show.py`](bulk-show.py) — the underlying script you can run directly
   from anywhere that has SSH reachability to the SD-WAN edges.

English: README.md
Japanese: README.ja.md

# run_on_vmanage.py (recommended)

The wrapper uploads bulk-show.py + input files to vManage, runs the script remotely, and optionally downloads output logs.

Execution flow (overview):

1. From your local PC, the wrapper connects to vManage over SSH.
2. A timestamped working directory is created under --remote-dir.
3. bulk-show.py, hosts file, and commands file are uploaded into that directory.
4. The wrapper enters vshell and runs bulk-show.py on vManage.
5. bulk-show.py logs into each SD-WAN device listed in hosts, runs each command, and writes output logs.
6. The wrapper downloads the generated output_*.txt files to ./logs/<timestamp>/.

Flow diagram:

```
Local PC -> SSH -> vManage -> vshell -> bulk-show.py -> SD-WAN devices
                                     -> <remote-dir>/<timestamp>/logs
                                     -> download -> ./logs/<timestamp>/
```

Log output:

- Remote logs are created at: //logs/output__.txt
- Downloaded logs are stored at: ./logs//output__.txt

Usage:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> [--password <password> | --key <key_path>] \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs \
  [--reject-unknown-hosts] [--verbose] [--quiet]
```

Example (password, prompt):

```bash
# Omit --password to be prompted interactively (recommended; avoids password in shell history).
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

Example (password, inline):

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

Example (SSH key):

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

Example (strict host-key checking):

```bash
# After the vManage host key is registered in ~/.ssh/known_hosts, enforce verification.
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs \
  --reject-unknown-hosts
```

Notes:

- The script creates a timestamped subdirectory under --remote-dir for each run, uploads files there,
and writes logs to //logs.
- Use --verbose for detailed remote output, or --quiet for minimal logs.
- By default, unknown SSH host keys are auto-accepted and a warning is printed to stderr (MITM risk).
Add --reject-unknown-hosts after the first connection (or pre-register the host key) to enforce
strict verification in production.

# Web UI (local browser)

A small FastAPI + Uvicorn app under [`webapp/`](webapp/) wraps
`run_on_vmanage.py` so you can drive a run from a browser instead of the CLI.
The UI is intended for **single-user, local use on the operator's machine** —
the server binds to `127.0.0.1` only and there is no built-in authentication.

## Launching the web UI

```bash
cd /path/to/sdwan-bulk-show
python3 -m venv .venv                            # if you do not already have one
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt        # adds fastapi, uvicorn[standard], jinja2, python-multipart
python -m webapp                                  # starts http://127.0.0.1:8000
# Open http://127.0.0.1:8000/ in your browser.
```

Optional flags (see `python -m webapp --help`):

```bash
python -m webapp --port 8081                      # change the local port
python -m webapp --log-level warning              # quieter uvicorn logs
python -m webapp --reload                          # uvicorn auto-reload (dev only)
python -m webapp --host 0.0.0.0                   # NOT RECOMMENDED — see "Security notes" below
```

The webapp runs as a single foreground Uvicorn process. Hit `Ctrl-C` in the
same terminal to stop it.

## Routes

| Method | Path                              | Purpose |
| ------ | --------------------------------- | ------- |
| `GET`  | `/`                               | Run form: vManage host, SSH user, password, remote-dir, hosts text, commands text, options. |
| `POST` | `/run`                            | Validate inputs, write `host.txt` / `command.txt` to a private tempdir, spawn `run_on_vmanage.py` with the password piped via `stdin`, then `303 See Other` to `/runs/<timestamp>`. |
| `GET`  | `/runs`                           | List past runs (newest first) by scanning `logs/<timestamp>/`. |
| `GET`  | `/runs/<timestamp>`               | Per-run summary (vManage host, user, hosts/commands counts, returncode, status, duration) plus the list of `output_*.txt`, `manifest.json`, and `run.log`. |
| `GET`  | `/runs/<timestamp>/files/<name>` | View an individual log file with strict path-traversal guards. |
| `GET`  | `/healthz`                        | Liveness probe; returns `{"status": "ok"}`. |

## How the web UI maps to the CLI

The web UI is a thin wrapper — it does not replace `run_on_vmanage.py`. Each
form submission becomes a normal subprocess invocation roughly equivalent to:

```bash
python3 run_on_vmanage.py <vmanage-host> \
  --user <user> --remote-dir <remote-dir> \
  --local-dir <tempdir> --hosts host.txt --commands command.txt \
  --download-outputs \
  [--verbose] [--reject-unknown-hosts]
```

Behind the scenes the runner ([`webapp/runner.py`](webapp/runner.py)):

- Pipes the submitted password to the subprocess `stdin` (the existing
  `getpass` fallback reads from `stdin` when there is no TTY), so the password
  never lands on disk.
- Captures combined stdout/stderr, replaces every occurrence of the password
  with `***`, and writes the masked transcript to
  `logs/<timestamp>/run.log`.
- Generates a `manifest.json` next to the downloaded outputs (built by
  `webapp.runner._build_manifest`):

  ```json
  {
    "timestamp": "20260502_031530",
    "vmanage_host": "192.0.2.10",
    "vmanage_user": "admin",
    "remote_dir": "/home/admin",
    "hosts_count": 5,
    "commands_count": 3,
    "options": {
      "download_outputs": true,
      "verbose": false,
      "reject_unknown_hosts": false
    },
    "started_at": "2026-05-02T03:15:30+09:00",
    "ended_at": "2026-05-02T03:15:37+09:00",
    "duration_sec": 7.2,
    "returncode": 0,
    "outputs_count": 2,
    "outputs": ["output_2.1.1.1.txt", "output_2.1.1.2.txt"],
    "status": "success"
  }
  ```

  The `status` field is one of `success` (returncode 0), `failed` (non-zero
  returncode), or `timeout` (exceeded `DEFAULT_RUN_TIMEOUT`).

## Concurrency and limits

- v1 serializes runs with an in-process `threading.Lock`. While a run is in
  progress, a second `POST /run` returns HTTP `409 Conflict` and re-renders
  the form with an error banner.
- Hosts and commands text inputs are each capped at **1 MiB**
  (`webapp.runner.MAX_INPUT_BYTES`) before being written to disk.
- Each subprocess invocation has a default timeout of **1800 s**
  (`webapp.runner.DEFAULT_RUN_TIMEOUT`); on timeout the run is marked
  `timeout` and the partial transcript is saved.
- The file viewer caps responses at **5 MiB**
  (`webapp.storage.MAX_VIEW_BYTES`); larger files are truncated and a banner
  notes how many bytes were dropped.

## UI overview (ASCII wireframes)

`GET /` — run form:

```
+-------------------------------------------------------------+
| sdwan-bulk-show                              [Run] [History]|
+-------------------------------------------------------------+
| vManage host:  [vmanage.example.com                      ]   |
| SSH user:      [admin           ]  Password: [**********]    |
| Remote dir:    [/home/admin                              ]   |
|                                                              |
| Hosts (one per line, IP[,user[,password]]):                  |
| +----------------------------------------------------------+ |
| | 2.1.1.1,admin                                            | |
| | 2.1.1.2,admin                                            | |
| +----------------------------------------------------------+ |
|                                                              |
| Commands (one per line):                                     |
| +----------------------------------------------------------+ |
| | show version                                             | |
| | show sdwan control connections                           | |
| +----------------------------------------------------------+ |
|                                                              |
| [x] Download outputs   [ ] Verbose   [ ] Reject unknown hosts|
|                                                              |
|                                            [ Run on vManage ]|
+-------------------------------------------------------------+
```

`GET /runs/<timestamp>` — run detail:

```
+-------------------------------------------------------------+
| Run 20260502_031530   status: success   duration: 7.20 s    |
| vManage 192.0.2.10      user admin   hosts 2   commands 2   |
+-------------------------------------------------------------+
| Files                                                       |
|  - manifest.json                                            |
|  - run.log                                                  |
|  - output_2.1.1.1.txt                                       |
|  - output_2.1.1.2.txt                                       |
+-------------------------------------------------------------+
```

(Open the page in your browser to capture real screenshots; the layout is
intentionally minimal HTML/CSS so it works without JavaScript.)

## Security notes — read this before pointing the web UI at production

- **Local-only by default.** The default bind is `127.0.0.1:8000`. Do not
  pass `--host 0.0.0.0`. There is no authentication, no rate limiting, and
  no TLS in v1 — exposing the web UI on a network is roughly equivalent to
  handing out shell access plus your vManage credentials. If you must
  reach it from another host, prefer an SSH tunnel
  (`ssh -L 8000:127.0.0.1:8000 your-mac`) or a reverse proxy that adds
  authentication. The runner logs a `WARNING` whenever the bind address is
  not on the loopback interface.
- **Passwords stay in memory and are masked in logs.** The submitted password
  is piped to the subprocess `stdin` and is never written to disk. Before
  `run.log` is persisted, the runner replaces every occurrence of the
  password with `***`. Inspect `logs/<timestamp>/run.log` after a run to
  confirm there is no leakage.
- **Hosts/commands inputs live in a private tempdir.** The submitted text is
  staged in a `tempfile.TemporaryDirectory()` with `0o600` permissions for
  the lifetime of the subprocess, then deleted. The downloaded outputs in
  `logs/<timestamp>/` follow the same on-disk layout as the CLI.
- **Path traversal is blocked.** `/runs/<timestamp>/files/<name>` resolves
  the requested path inside `logs/<timestamp>/` and rejects anything that
  escapes the run directory or follows a symlink. Filenames containing `/`,
  `\`, or `..` return `404`.
- **Runs are serialized.** A `threading.Lock` allows one active run at a
  time. A second concurrent submit returns `409 Conflict` and re-renders the
  form with an error banner.
- **Browser autofill.** Modern browsers may offer to remember the vManage
  password. Decline if you do not want it persisted in your browser
  keychain.
- **Logs are not pruned automatically.** Old runs accumulate under `logs/`.
  Delete unused timestamp directories manually if disk usage becomes a
  concern; the web UI never deletes runs on your behalf.

See also the cross-cutting [Security recommendations](#security-recommendations)
section below, which covers the underlying CLI flags consumed by the web UI.

## Future extensions (not in v1)

- Live streaming of subprocess output (`/runs/<ts>/stream` over SSE).
- Named host inventories (`inventories/<name>.txt`) — store the hosts file
  but never the password.
- Parallel runs via an async job queue.
- Optional bearer-token auth gated by a `WEBAPP_TOKEN` environment variable.

# bulk-show.py (direct)

Put the hosts file and command file in the same directory.

The hosts file supports two formats. Lines starting with `#` and blank lines are ignored.

Two columns (recommended) — password is prompted once at startup and reused for all hosts:

```bash
$ more host.txt
# ip,username
2.1.1.1,admin
3.1.1.1,admin
4.1.1.1,admin
```

Three columns (legacy) — password is embedded per host. Avoid in shared/public repos.

```bash
$ more host.txt
# ip,username,password
2.1.1.1,admin,admin
3.1.1.1,admin,admin
4.1.1.1,admin,admin
```

## Targeting controllers (vBond / vSmart)

By default every host is treated as an **edge** (cEdge / IOS-XE SD-WAN):
connected on TCP/830, entering the device `shell`, and possibly re-prompting
for the password a second time.

To collect logs from **controllers** (vBond / vSmart) — typically with vManage
acting as a jump server — mark the host with a device type. Controllers are
connected on `--controller-port` (default **22**) with a **single** password
and **no** `shell` step; pagination is disabled with the viptela CLI
`paginate false`.

A device type can be given as a bare keyword (`controller`, `vsmart`, `vbond`)
or, unambiguously, as a `type=` token. Edges and controllers can be mixed in
the same hosts file:

```bash
$ more host.txt
# ip,username[,password][,type]   (type defaults to "edge")
2.1.1.1,admin
3.1.1.1,admin,secret
10.0.0.5,admin,controller
10.0.0.6,admin,secret,vsmart
10.0.0.7,admin,type=controller
```

Line by line:

| Entry | Meaning |
| --- | --- |
| `2.1.1.1,admin` | edge, password prompted once at startup |
| `3.1.1.1,admin,secret` | edge, password embedded (not recommended) |
| `10.0.0.5,admin,controller` | vBond/vSmart, password prompted at startup |
| `10.0.0.6,admin,secret,vsmart` | vBond/vSmart, password embedded |
| `10.0.0.7,admin,type=controller` | explicit device type (also: `type=edge`) |

> Inline `#` comments are **not** supported on host entries — only whole lines
> whose first non-space character is `#` are treated as comments. Keep
> annotations out of the host lines themselves.

Notes:

- The same shared password (from the `getpass` prompt or `--password-prompt`)
  is reused across edges and controllers, so a single mixed run works when the
  credentials match.
- In the rare case a literal password equals a type keyword (e.g. a password of
  `controller`), use the unambiguous `type=` form or the 4-column
  `ip,user,password,type` layout.
- Override the controller port with `--controller-port` only if your controllers
  do not use the conventional TCP/22.

The command file contains the show commands you want to run.

Example commands file:

```bash
show version
show ip int bri
show ip route
show sdwan control connections
```

Run examples:

```bash
# Two-column host file: prompts once for the shared password.
python3 bulk-show.py host.txt command.txt

# Force a single shared password for ALL hosts (overrides any embedded passwords).
python3 bulk-show.py host.txt command.txt --password-prompt

# Strict SSH host-key checking (after hosts are in ~/.ssh/known_hosts).
python3 bulk-show.py host.txt command.txt --reject-unknown-hosts
```

## CLI options

| Option | Default | Purpose |
| --- | --- | --- |
| `--port PORT` | `830` | SSH TCP port used for **edge** hosts. SD-WAN edges (cEdge / IOS-XE SD-WAN) expose the interactive SSH service used by vManage `vshell` on 830, **not** 22. Override only for non-SD-WAN devices. |
| `--controller-port PORT` | `22` | SSH TCP port used for hosts marked as **controllers** (vBond / vSmart). When reached through vManage as a jump server, controllers land directly in the viptela CLI on the conventional port 22 with a single password and no `shell` step. |
| `--reject-unknown-hosts` | off (auto-add + WARN) | Reject host keys not present in `~/.ssh/known_hosts` (MITM protection). |
| `--password-prompt` | off | Prompt once for a shared password and override any embedded passwords. |
| `--logs-dir LOGS_DIR` | `logs` | Directory where output files are written. |
| `--max-workers N` | `min(8, hosts)` | Cap on concurrent SSH sessions. Raise to fan out faster; lower to reduce load on the network and the targets. |
| `--retries N` | `0` | Additional SSH connect attempts on transient network/SSH errors. Authentication failures are NEVER retried. |
| `--retry-delay SECS` | `5.0` | Seconds to sleep between connect attempts. |
| `--output-format LIST` | `text` | Comma-separated; combine any of `text,json,csv`. Each format produces an additional per-host file. |

## SD-WAN authentication notes

### Edges (default)

When `bulk-show.py` is launched from the vManage `vshell` (the recommended
deployment via `run_on_vmanage.py`), each SD-WAN edge is reached on
**TCP/830** and the device asks for the password **twice**:

1. **SSH transport layer** — the password supplied via the hosts file or the
   `--password-prompt` flow is sent as part of the SSH handshake.
2. **Device sub-shell** — after the connection is established, the script
   sends `shell` to drop into the device shell and the device may re-prompt
   for the same password (`Password:`). `bulk-show.py` detects this prompt
   (`PASSWORD_PROMPT_RE`) and replays the same password automatically.

If the second prompt rejects the password, the session ends with status
`auth_error_shell` and an explicit message in the log. **Use the same
password for both prompts**; the script does not currently support distinct
transport- vs shell-level credentials.

### Controllers (vBond / vSmart)

Hosts marked as controllers (see [Targeting controllers](#targeting-controllers-vbond--vsmart))
behave differently. With vManage acting as a jump server, a controller is
reached on **TCP/22** and lands directly in the viptela CLI:

1. **SSH transport layer** — the password is sent during the SSH handshake.
   This is the **only** password prompt; there is no `shell` sub-process and
   therefore no second `Password:` prompt.
2. **Pagination** — the script sends the viptela CLI `paginate false` (instead
   of the IOS-XE `terminal length 0`) before running the show commands.

Examples (new options):

```bash
# Default SSH port (830, SD-WAN) with bounded parallelism.
python3 bulk-show.py host.txt command.txt --max-workers 4

# Override the SSH port (only needed for non-SD-WAN devices that
# use the conventional port 22).
python3 bulk-show.py host.txt command.txt --port 22

# Retry transient connect failures up to 3 times, 10s apart (auth failures
# are intentionally excluded from retries).
python3 bulk-show.py host.txt command.txt --retries 3 --retry-delay 10

# Emit text + JSON + CSV per host for downstream automation.
python3 bulk-show.py host.txt command.txt --output-format text,json,csv

# Mixed edges + controllers in host.txt (mark controllers with a type token);
# controllers use TCP/22 by default. Override the controller port if needed.
python3 bulk-show.py host.txt command.txt --controller-port 22
```

# Output logs

Logs are saved under ./logs with timestamps in the file name.
Use `--logs-dir` to change the destination and `--output-format` to choose formats.

Each session adds explicit boundary markers to the text output (Issue 9), making
it easy to split logs per host and per command:

```
=== SESSION BEGIN host=2.1.1.1 port=830 ts=2026-05-02T01:23:45+09:00 ===
--- COMMAND BEGIN cmd="show version" ts=... ---
... (command output) ...
--- COMMAND END   cmd="show version" status=ok duration=1.23s ts=... ---
=== SESSION END   host=2.1.1.1 status=success duration=4.56s ts=... ===
```

With `--output-format json`, an `output_<ip>_<ts>.json` file is generated
containing host, port, per-command start/end timestamps, status, and the full
output as structured data.
With `--output-format csv`, an `output_<ip>_<ts>.csv` file is generated with
one row per command (host, command, status, duration, output). Multi-line
outputs are properly CSV-quoted.

# Security recommendations

- Prefer the two-column `host.txt` format and let `getpass` prompt for the shared password,
so credentials never land in the file or in shell history.
- Use `--password-prompt` to override embedded passwords with a single interactive password.
- Add `--reject-unknown-hosts` (both scripts) once the target host keys are registered in
`~/.ssh/known_hosts`. The default `AutoAddPolicy` mode prints a `[WARN]` to stderr because
it is vulnerable to man-in-the-middle attacks on first connect.
- For `run_on_vmanage.py`, prefer SSH key authentication (`--key`) over `--password`, and avoid
passing `--password` on the command line (it appears in shell history and process listings).
Omit it to be prompted interactively.
- Never commit real `host.txt` (with passwords) to a public repository. See `PUBLIC_CHECKLIST.md`.

# Setup on a clean PC (no Python/venv)

1. Install Python 3 (3.10+ recommended).
2. Create a virtual environment:

```bash
python3 -m venv .venv
```

1. Activate the venv and install dependencies:
macOS/Linux:

```bash
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows (cmd):

```bat
.\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

1. Run the scripts from the activated venv:

```bash
python bulk-show.py host.txt command.txt
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --remote-dir /home/<username> \
  --hosts host.txt --commands command.txt --download-outputs
```

# Full command samples

## vManage wrapper (run_on_vmanage.py)

Password auth:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell):

```powershell
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> `
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell, single line):

```powershell
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (cmd):

```bat
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

SSH key auth:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

## Local bulk-show.py

Prepare hosts/commands (recommended: omit passwords and let the script prompt once):

```bash
cat > host.txt <<'EOF'
2.1.1.1,admin
2.1.1.4,admin
2.1.1.5,admin
EOF

cat > command.txt <<'EOF'
show ip route
show omp route
show ip int bri
EOF
```

Run (the script prompts once for the shared password):

```bash
python3 bulk-show.py host.txt command.txt
```

Windows (PowerShell):

```powershell
python bulk-show.py host.txt command.txt
```

Windows (cmd):

```bat
python bulk-show.py host.txt command.txt
```

Output:

```
./logs/output_<ip>_<YYYYmmdd_HHMMSS>.txt
```

