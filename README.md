# sdwan-bulk-show

This repo provides a wrapper to run bulk-show.py on vManage and collect logs from multiple SD-WAN devices.

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
| `--port PORT` | `830` | SSH TCP port used for every host. |
| `--reject-unknown-hosts` | off (auto-add + WARN) | Reject host keys not present in `~/.ssh/known_hosts` (MITM protection). |
| `--password-prompt` | off | Prompt once for a shared password and override any embedded passwords. |
| `--logs-dir LOGS_DIR` | `logs` | Directory where output files are written. |
| `--max-workers N` | `min(8, hosts)` | Cap on concurrent SSH sessions. Raise to fan out faster; lower to reduce load on the network and the targets. |
| `--retries N` | `0` | Additional SSH connect attempts on transient network/SSH errors. Authentication failures are NEVER retried. |
| `--retry-delay SECS` | `5.0` | Seconds to sleep between connect attempts. |
| `--output-format LIST` | `text` | Comma-separated; combine any of `text,json,csv`. Each format produces an additional per-host file. |

Examples (new options):

```bash
# Default SSH port (830) with bounded parallelism.
python3 bulk-show.py host.txt command.txt --max-workers 4

# Switch to standard SSH port.
python3 bulk-show.py host.txt command.txt --port 22

# Retry transient connect failures up to 3 times, 10s apart (auth failures
# are intentionally excluded from retries).
python3 bulk-show.py host.txt command.txt --retries 3 --retry-delay 10

# Emit text + JSON + CSV per host for downstream automation.
python3 bulk-show.py host.txt command.txt --output-format text,json,csv
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

