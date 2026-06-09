import argparse
import sys
import time
import ipaddress
import concurrent.futures
import socket
import threading
import os
import re
import csv
import json
import getpass
from datetime import datetime

print_lock = threading.Lock()

# Matches IOS-XE style prompts like "host_2111#", "host_2111(config)#",
# or "host_2111>" at the end of the buffer. Trailing whitespace is allowed.
DEFAULT_PROMPT_RE = re.compile(r"(?:^|\n)\S+[#>]\s*\Z")

# Matches a "Password:" re-authentication prompt at the tail of the buffer.
PASSWORD_PROMPT_RE = re.compile(r"password:\s*\Z", re.IGNORECASE)

# Matches ANSI / VT100 escape sequences: CSI sequences such as "\x1b[?7h"
# (DEC autowrap), "\x1b[0m" (SGR), and simple two-character escapes. The
# viptela CLI on vBond / vSmart emits a line-wrap escape ("\x1b[?7h")
# immediately before its command prompt; left in place it gets captured as
# part of the prompt (e.g. "\x1b[?7hvsmart#") and breaks prompt detection on
# every subsequent command. Stripping these also keeps the saved logs clean.
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text):
    """Remove ANSI / VT100 escape sequences from ``text``."""
    return ANSI_ESCAPE_RE.sub("", text)

# Patterns indicating shell-level password authentication failure.
# Matched case-insensitively against the buffer received after sending
# a password to the device's "shell" sub-process.
AUTH_FAILURE_RE = re.compile(
    r"(login\s+incorrect|"
    r"authentication\s+failed|"
    r"access\s+denied|"
    r"%\s*Bad\s+password|"
    r"permission\s+denied|"
    r"too\s+many\s+authentication\s+failures)",
    re.IGNORECASE,
)

# Controllers (vBond / vSmart) use a single password supplied during the SSH
# handshake and never enter a "shell" sub-process. So any tail that asks for a
# password again, or reports an auth failure, means the login did NOT settle
# into the viptela CLI. Combine both signals into one expect pattern for the
# controller's initial read so we can fail clearly instead of sending show
# commands into a password prompt and falsely reporting success.
CONTROLLER_REAUTH_RE = re.compile(
    r"(?:" + PASSWORD_PROMPT_RE.pattern + r")|(?:" + AUTH_FAILURE_RE.pattern + r")",
    re.IGNORECASE,
)

# Match types returned by read_channel.
MATCH_PROMPT = "prompt"
MATCH_EXPECT = "expect"
MATCH_IDLE = "idle"
MATCH_MAX_WAIT = "max_wait"
MATCH_EOF = "eof"

# ---------------------------------------------------------------------------
# Output formats and boundary markers (Issues 9 and 14)
# ---------------------------------------------------------------------------

OUTPUT_FORMAT_TEXT = "text"
OUTPUT_FORMAT_JSON = "json"
OUTPUT_FORMAT_CSV = "csv"
ALL_OUTPUT_FORMATS = (OUTPUT_FORMAT_TEXT, OUTPUT_FORMAT_JSON, OUTPUT_FORMAT_CSV)

# Per-host session status codes (recorded in session_result["status"]).
SESSION_OK = "success"
SESSION_AUTH_SSH = "auth_error_ssh"
SESSION_AUTH_SHELL = "auth_error_shell"
SESSION_CONNECT_ERR = "connect_error"
SESSION_SHELL_ERR = "shell_error"
SESSION_OTHER_ERR = "error"

# Per-command status codes (recorded in command_result["status"]).
CMD_OK = "ok"
CMD_TIMEOUT = "timeout"

# ---------------------------------------------------------------------------
# Device types / connection profiles
# ---------------------------------------------------------------------------
#
# Two connection profiles are supported:
#
#   edge       (default) Cisco SD-WAN edges (cEdge / IOS-XE SD-WAN). Reached
#              on TCP/830, enter the device "shell" sub-process which may
#              re-prompt for the SAME password a second time, then run
#              IOS-XE style commands (pagination off via 'terminal length 0').
#
#   controller vBond / vSmart (and vManage). When reached through vManage
#              acting as a jump server, the SSH transport drops you straight
#              into the viptela CLI on the conventional TCP/22 -- there is no
#              'shell' sub-process and the password is asked only ONCE.
#              Pagination is disabled with the viptela 'paginate false'.
DEVICE_EDGE = "edge"
DEVICE_CONTROLLER = "controller"

# User-facing aliases accepted in the hosts file (case-insensitive), mapped to
# the canonical device type above.
DEVICE_TYPE_ALIASES = {
    "edge": DEVICE_EDGE,
    "cedge": DEVICE_EDGE,
    "controller": DEVICE_CONTROLLER,
    "ctrl": DEVICE_CONTROLLER,
    "vsmart": DEVICE_CONTROLLER,
    "vbond": DEVICE_CONTROLLER,
    "vmanage": DEVICE_CONTROLLER,
}


def normalize_device_type(token):
    """Map a user-supplied device-type token to a canonical type or None.

    Comparison is case-insensitive and surrounding whitespace is ignored.
    Returns ``DEVICE_EDGE`` / ``DEVICE_CONTROLLER`` or ``None`` if the token
    is not a recognized alias.
    """
    if token is None:
        return None
    return DEVICE_TYPE_ALIASES.get(token.strip().lower())


def parse_host_line(line):
    """Parse one hosts-file line into ``(ip, username, password, device_type)``.

    Returns ``None`` for blank lines and comment lines (first non-space char
    ``#``). Raises :class:`ValueError` with a human-readable reason for
    malformed lines so the caller can print it and skip the host.

    Supported comma-separated forms (whitespace around fields is stripped)::

        ip,user                         -> edge,       password prompted later
        ip,user,password                -> edge,       password embedded
        ip,user,controller              -> controller, password prompted later
        ip,user,password,controller     -> controller, password embedded
        ip,user,type=controller         -> controller, password prompted later
        ip,user,password,type=controller-> controller, password embedded

    The device type may be given either as an explicit ``type=<value>`` token
    (recommended; unambiguous, allowed anywhere after the IP) or as a bare
    keyword alias (``edge``/``controller``/``vsmart``/``vbond``/...). When the
    bare-keyword form is used, a password whose literal value collides with a
    type alias would be misread; use the ``type=`` form (or the 4-column form)
    in that rare case.

    ``device_type`` defaults to :data:`DEVICE_EDGE` when not specified.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None

    parts = [p.strip() for p in stripped.split(",")]

    # First pass: pull out any explicit ``type=<value>`` token(s). These are
    # unambiguous, so they win over a trailing bare keyword.
    device_type = None
    positional = []
    for field in parts:
        if field.lower().startswith("type="):
            value = field.split("=", 1)[1]
            mapped = normalize_device_type(value)
            if mapped is None:
                raise ValueError(
                    f"unknown device type {value!r} "
                    f"(valid: {', '.join(sorted(set(DEVICE_TYPE_ALIASES)))})"
                )
            # Reject contradictory explicit types (e.g. type=edge,type=vsmart).
            # Repeating the SAME canonical type is harmless and allowed.
            if device_type is not None and device_type != mapped:
                raise ValueError(
                    f"conflicting device types {device_type!r} and {mapped!r}"
                )
            device_type = mapped
        else:
            positional.append(field)

    if len(positional) < 2:
        raise ValueError(
            "expected at least 'ip,user' "
            "(optionally ',password' and/or a device type)"
        )

    router_ip = positional[0]
    username = positional[1]
    password = None

    extras = positional[2:]
    if device_type is None and extras:
        # The last positional field may be a bare device-type keyword. Only
        # consume it as a type when it actually maps to one; otherwise it is
        # treated as (part of) the password column.
        mapped_last = normalize_device_type(extras[-1])
        if mapped_last is not None:
            if len(extras) == 1:
                # Ambiguous 3-column form "ip,user,<keyword>": the trailing
                # field doubles as both a possible password and a device-type
                # alias. We keep the legacy behavior (treat it as a device
                # type, dropping it from the password column) but make the
                # inference explicit so a real password that happens to collide
                # with an alias is not silently swallowed.
                print(
                    f"[WARN] host '{router_ip}': treating '{extras[-1]}' as "
                    f"device type '{mapped_last}' (password left empty); if "
                    f"'{extras[-1]}' was meant to be a password, use the "
                    f"'type=' form (e.g. "
                    f"'{router_ip},{username},{extras[-1]},type=edge') or the "
                    f"'ip,user,password,type' layout",
                    file=sys.stderr,
                    flush=True,
                )
            device_type = mapped_last
            extras = extras[:-1]

    if len(extras) > 1:
        raise ValueError(
            "too many fields; expected 'ip,user[,password][,type]'"
        )
    if extras:
        password = extras[0] or None

    if device_type is None:
        device_type = DEVICE_EDGE

    if not router_ip:
        raise ValueError("missing IP address")
    if not username:
        raise ValueError("missing username")

    return router_ip, username, password, device_type


def resolve_commands_file(device_type, base, controller_file, edge_file):
    """Pick the commands file a host should run, based on its device type.

    Controllers (vBond/vSmart/vEdge) prefer ``controller_file`` and edges
    (cEdge / IOS-XE) prefer ``edge_file``. When the type-specific file is not
    provided (``None``/empty) the positional ``base`` commands file is used as
    a shared fallback. Returns the chosen path or ``None`` when no file is
    available for the host (in which case the host connects but runs no
    commands).
    """
    if device_type == DEVICE_CONTROLLER:
        return controller_file or base
    if device_type == DEVICE_EDGE:
        return edge_file or base
    return base

# Boundary markers used in text output. They are designed to be easy to grep
# (`^=====`) and to carry enough metadata for downstream tooling.
SESSION_BEGIN_FMT = (
    "===== session begin: {host} user={user} port={port} started={ts} ====="
)
SESSION_END_FMT = (
    "===== session end:   {host} status={status} ended={ts} duration={dur:.2f}s ====="
)
COMMAND_BEGIN_FMT = "===== begin: {cmd} @ {ts} ====="
COMMAND_END_FMT = (
    "===== end:   {cmd} status={status} duration={dur:.2f}s exit={kind} ====="
)


def now_iso():
    """Return an ISO-8601 timestamp with the local timezone offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log_message(message):
    with print_lock:
        print(message, flush=True)


def is_valid_ip(ip_address):
    try:
        ipaddress.IPv4Address(ip_address)
        return True
    except ipaddress.AddressValueError:
        return False


def read_channel(
    channel,
    prompt_re=DEFAULT_PROMPT_RE,
    expect_re=None,
    idle_timeout=1.0,
    max_wait=60.0,
    poll_interval=0.1,
):
    """
    Read from the SSH channel until prompt_re or expect_re matches the tail
    of the buffer, or idle_timeout / max_wait expires.

    Args:
        channel: paramiko Channel object.
        prompt_re: regex matched against the tail to detect a command prompt.
                   Pass None to disable prompt detection.
        expect_re: optional regex (e.g., for "password:" or auth-failure
                   markers). Matched against the tail in addition to
                   prompt_re; whichever matches first wins.
        idle_timeout: seconds with no new data before returning (only after
                      at least one chunk has been received).
        max_wait: hard upper bound for the entire read.
        poll_interval: short channel.recv timeout. Keeps idle accounting
                       precise without busy-spinning.

    Returns:
        Tuple (buffer, match_kind). match_kind is one of:
          MATCH_PROMPT, MATCH_EXPECT, MATCH_IDLE, MATCH_MAX_WAIT, MATCH_EOF.
    """
    channel.settimeout(poll_interval)
    chunks = []
    start = time.monotonic()
    last_data = start
    while True:
        now = time.monotonic()
        if now - start >= max_wait:
            return strip_ansi("".join(chunks)), MATCH_MAX_WAIT
        try:
            data = channel.recv(4096)
            if not data:
                # EOF on the channel.
                return strip_ansi("".join(chunks)), MATCH_EOF
            chunks.append(data.decode(errors="replace"))
            last_data = now
        except socket.timeout:
            # No data this poll; loop and re-check patterns / idle.
            pass
        except OSError:
            # Other socket-level errors are treated as EOF for our purposes.
            return strip_ansi("".join(chunks)), MATCH_EOF

        # Check matches whether we received data this poll or not, so that
        # idle exits still get a final chance to confirm the tail. Strip ANSI
        # escapes first so embedded sequences (e.g. the viptela "\x1b[?7h"
        # emitted before the prompt) do not defeat the prompt/expect regexes.
        tail = strip_ansi("".join(chunks))[-1024:]
        if expect_re is not None and expect_re.search(tail):
            return strip_ansi("".join(chunks)), MATCH_EXPECT
        if prompt_re is not None and prompt_re.search(tail):
            return strip_ansi("".join(chunks)), MATCH_PROMPT

        # Idle exit: at least some data has been received and nothing new
        # has arrived for idle_timeout seconds.
        if chunks and now - last_data >= idle_timeout:
            return strip_ansi("".join(chunks)), MATCH_IDLE


def extract_prompt(buffer):
    """
    Extract the device's command prompt from a buffer that ends with one.

    Returns the captured prompt string (e.g. "host_2111#") or None
    if no usable prompt is detected on the last non-empty line.
    """
    buffer = strip_ansi(buffer)
    for line in reversed(buffer.splitlines()):
        line = line.rstrip()
        if not line:
            continue
        m = re.match(r"\S+[#>]\s*\Z", line)
        if m:
            return m.group(0).rstrip()
        # First non-empty line from the end is not a prompt -> give up.
        return None
    return None


def build_command_prompt_re(base_prompt):
    """
    Given a captured base prompt like "host_2111#" or "host_2111>", return
    a regex matching the same hostname's prompt at the tail of the buffer,
    including sub-modes like "host_2111(config)#".

    Falls back to DEFAULT_PROMPT_RE when no usable base prompt is given.
    """
    if not base_prompt:
        return DEFAULT_PROMPT_RE
    head = base_prompt.rstrip("#>").rstrip()
    if not head:
        return DEFAULT_PROMPT_RE
    return re.compile(
        r"(?:^|\n)" + re.escape(head) + r"(?:\([^)]+\))?[#>]\s*\Z"
    )


def _write_outputs(session_result, output_paths):
    """
    Materialize a session_result dict to disk in one or more formats.

    Args:
        session_result: dict produced by connect_and_execute. See the
            docstring of connect_and_execute for the schema.
        output_paths: dict mapping format name (one of OUTPUT_FORMAT_TEXT,
            OUTPUT_FORMAT_JSON, OUTPUT_FORMAT_CSV) to destination file path.
            Only the formats present in this dict are written.
    """
    if OUTPUT_FORMAT_TEXT in output_paths:
        _write_text(session_result, output_paths[OUTPUT_FORMAT_TEXT])
    if OUTPUT_FORMAT_JSON in output_paths:
        _write_json(session_result, output_paths[OUTPUT_FORMAT_JSON])
    if OUTPUT_FORMAT_CSV in output_paths:
        _write_csv(session_result, output_paths[OUTPUT_FORMAT_CSV])


def _write_text(session_result, path):
    """Write a per-host text log with session/command boundary markers."""
    with open(path, "w") as f:
        f.write(
            SESSION_BEGIN_FMT.format(
                host=session_result["host"],
                user=session_result["username"],
                port=session_result["port"],
                ts=session_result["started_at"],
            )
            + "\n"
        )
        # Surface a connect/auth-level error before any commands so that a
        # text reader does not have to guess why the file is otherwise empty.
        if session_result.get("error"):
            f.write(f"!! {session_result['error']}\n")
        for cmd in session_result["commands"]:
            f.write(
                COMMAND_BEGIN_FMT.format(
                    cmd=cmd["command"],
                    ts=cmd["started_at"],
                )
                + "\n"
            )
            f.write(cmd["output"])
            if not cmd["output"].endswith("\n"):
                f.write("\n")
            f.write(
                COMMAND_END_FMT.format(
                    cmd=cmd["command"],
                    status=cmd["status"],
                    dur=cmd["duration_s"],
                    kind=cmd["exit_kind"],
                )
                + "\n"
            )
        f.write(
            SESSION_END_FMT.format(
                host=session_result["host"],
                status=session_result["status"],
                ts=session_result["ended_at"],
                dur=session_result["duration_s"],
            )
            + "\n"
        )


def _write_json(session_result, path):
    """Write the session_result as pretty-printed UTF-8 JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(session_result, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _write_csv(session_result, path):
    """Write per-command rows as CSV. Multi-line outputs are quoted by csv."""
    fieldnames = [
        "seq",
        "host",
        "command",
        "started_at",
        "duration_s",
        "exit_kind",
        "status",
        "output",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        # If no commands ran (e.g. early auth failure), emit a single error
        # row so that aggregated CSVs still record the failed host.
        if not session_result["commands"]:
            writer.writerow(
                {
                    "seq": 0,
                    "host": session_result["host"],
                    "command": "",
                    "started_at": session_result["started_at"],
                    "duration_s": session_result["duration_s"],
                    "exit_kind": session_result["status"],
                    "status": session_result["status"],
                    "output": session_result.get("error") or "",
                }
            )
            return
        for i, cmd in enumerate(session_result["commands"], 1):
            writer.writerow(
                {
                    "seq": i,
                    "host": session_result["host"],
                    "command": cmd["command"],
                    "started_at": cmd["started_at"],
                    "duration_s": f"{cmd['duration_s']:.3f}",
                    "exit_kind": cmd["exit_kind"],
                    "status": cmd["status"],
                    "output": cmd["output"],
                }
            )


def connect_and_execute(
    router_ip,
    username,
    password,
    commands_file,
    output_paths,
    allow_unknown_hosts=True,
    port=830,
    retries=0,
    retry_delay=5.0,
    device_type=DEVICE_EDGE,
):
    """
    Connect to a single host, run the user's commands, and write per-host
    output files in the requested formats.

    Args:
        router_ip, username, password: SSH credentials.
        commands_file: path to a file with one command per line. Lines
            starting with '#' and blank lines are skipped. May be ``None``
            (or empty), in which case the host is connected but no commands
            are run -- useful when split controller/edge command lists leave
            one device type without a list.
        output_paths: dict mapping output format -> destination file path.
            Use OUTPUT_FORMAT_TEXT/JSON/CSV as keys. May contain multiple.
        allow_unknown_hosts: if False, unknown SSH host keys are rejected.
        port: SSH TCP port to connect to (Issue 11). Default 830 because
            Cisco SD-WAN edges (cEdge / IOS-XE SD-WAN) expose the
            interactive SSH service for ``vshell``-initiated sessions on
            port 830, not the conventional 22. Override with --port when
            connecting to non-SD-WAN devices.
        retries: number of additional SSH connect attempts after the first
            one fails on a transient network/SSH error (Issue 12). Auth
            failures are NEVER retried because they will not fix themselves.
        retry_delay: seconds to sleep between connect attempts.
        device_type: connection profile, one of DEVICE_EDGE (default) or
            DEVICE_CONTROLLER. Edges enter the device "shell" sub-process
            (and may re-prompt for the password a second time); controllers
            (vBond/vSmart reached through vManage) land directly in the
            viptela CLI with a single password and no "shell" step.

    Returns:
        session_result: dict with the schema:
            {
              "host": str, "username": str, "port": int,
              "device_type": str,
              "started_at": iso, "ended_at": iso, "duration_s": float,
              "status": one of SESSION_*,
              "error": str | None,
              "commands": [
                  {"command": str, "started_at": iso,
                   "duration_s": float, "exit_kind": str,
                   "status": one of CMD_*, "output": str},
                  ...
              ],
            }
    """
    # Imported lazily so that the pure-parser helpers in this module remain
    # importable (and unit-testable) on systems without paramiko installed.
    import paramiko

    started_wall = now_iso()
    started_mono = time.monotonic()
    session_result = {
        "host": router_ip,
        "username": username,
        "port": port,
        "device_type": device_type,
        "started_at": started_wall,
        "ended_at": started_wall,  # updated in finally
        "duration_s": 0.0,
        "status": SESSION_OTHER_ERR,
        "error": None,
        "commands": [],
    }

    # Create an SSH client.
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    if allow_unknown_hosts:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

    # Accumulated buffer during shell entry; useful for diagnostics and for
    # extracting the device prompt. NEVER overwritten -- always appended.
    auth_banner = []

    try:
        # Phase 1: SSH transport with optional retry on transient failures.
        # AuthenticationException is intentionally NOT retried.
        attempt = 0
        while True:
            try:
                ssh.connect(
                    router_ip,
                    port=port,
                    username=username,
                    password=password,
                    timeout=10,
                )
                break
            except paramiko.AuthenticationException as ex:
                session_result["status"] = SESSION_AUTH_SSH
                session_result["error"] = f"auth error (ssh): {ex}"
                log_message(f"[{router_ip}] {session_result['error']}")
                return session_result
            except (paramiko.SSHException, socket.timeout, OSError) as ex:
                if attempt < retries:
                    attempt += 1
                    log_message(
                        f"[{router_ip}] connect attempt {attempt} failed: "
                        f"{ex}; retrying in {retry_delay:.1f}s "
                        f"({attempt}/{retries})"
                    )
                    time.sleep(retry_delay)
                    continue
                session_result["status"] = SESSION_CONNECT_ERR
                session_result["error"] = f"connect error: {ex}"
                log_message(f"[{router_ip}] {session_result['error']}")
                return session_result
        log_message(f"[{router_ip}] connected")

        # Phase 2: Open an interactive shell and settle on a usable command
        # prompt. Edges and controllers differ here, so branch on the
        # connection profile.
        shell = ssh.invoke_shell()

        if device_type == DEVICE_CONTROLLER:
            # Controllers (vBond / vSmart) reached through vManage land us
            # directly in the viptela CLI on the SSH transport: there is no
            # "shell" sub-process and the password was already supplied during
            # the SSH handshake (asked only once). Just wait for the CLI
            # prompt to appear.
            buf, kind = read_channel(
                shell,
                prompt_re=DEFAULT_PROMPT_RE,
                expect_re=CONTROLLER_REAUTH_RE,
                idle_timeout=1.0,
                max_wait=10.0,
            )
            auth_banner.append(buf)
            if kind == MATCH_EXPECT:
                # The controller unexpectedly asked for a password again (or
                # reported an auth failure). The controller profile assumes a
                # single password was already supplied during the SSH
                # handshake, so we deliberately do NOT resend it here: doing so
                # could leak the password into command output and would mask the
                # real problem. Fail clearly instead.
                if AUTH_FAILURE_RE.search(buf):
                    session_result["error"] = (
                        "auth error (shell): controller rejected the password"
                    )
                else:
                    session_result["error"] = (
                        "auth error (shell): controller re-prompted for a "
                        "password (single-password profile does not resend); "
                        "aborting"
                    )
                session_result["status"] = SESSION_AUTH_SHELL
                log_message(f"[{router_ip}] {session_result['error']}")
                return session_result
            if kind != MATCH_PROMPT:
                # A long login banner may push the prompt past the window;
                # warn but continue and rely on per-command max_wait.
                log_message(
                    f"[{router_ip}] warning: no CLI prompt after login "
                    f"({kind}); proceeding anyway"
                )

            # Phase 3: Capture the device's prompt for accurate completion
            # detection. Fall back to the default regex if extraction fails.
            captured_prompt = extract_prompt("".join(auth_banner))
            cmd_prompt_re = build_command_prompt_re(captured_prompt)
            if captured_prompt:
                log_message(f"[{router_ip}] prompt: {captured_prompt}")
            else:
                log_message(
                    f"[{router_ip}] prompt: <not captured, using default regex>"
                )

            # Phase 4: Disable pagination in the viptela CLI.
            shell.send("paginate false\n")
            _, page_kind = read_channel(
                shell,
                prompt_re=cmd_prompt_re,
                idle_timeout=1.0,
                max_wait=5.0,
            )
            if page_kind != MATCH_PROMPT:
                log_message(
                    f"[{router_ip}] warning: 'paginate false' did not "
                    f"return a prompt ({page_kind}); first command output may "
                    "include residual data"
                )
        else:
            # Edge profile: enter the device "shell" sub-process. Wait for
            # either a re-auth password prompt or the device's command prompt
            # -- whichever comes first.
            shell.send("shell\n")
            log_message(f"[{router_ip}] entered shell")

            buf, kind = read_channel(
                shell,
                prompt_re=DEFAULT_PROMPT_RE,
                expect_re=PASSWORD_PROMPT_RE,
                idle_timeout=1.0,
                max_wait=10.0,
            )
            auth_banner.append(buf)

            if kind == MATCH_EXPECT:
                # Re-authentication requested by the device.
                shell.send(f"{password}\n")
                buf, kind = read_channel(
                    shell,
                    prompt_re=DEFAULT_PROMPT_RE,
                    expect_re=AUTH_FAILURE_RE,
                    idle_timeout=1.0,
                    max_wait=10.0,
                )
                auth_banner.append(buf)
                if kind == MATCH_EXPECT:
                    session_result["status"] = SESSION_AUTH_SHELL
                    session_result["error"] = (
                        "auth error (shell): device rejected the password"
                    )
                    log_message(f"[{router_ip}] {session_result['error']}")
                    return session_result
                if kind != MATCH_PROMPT:
                    session_result["status"] = SESSION_SHELL_ERR
                    session_result["error"] = (
                        f"shell did not return a prompt after password "
                        f"(got {kind}); aborting"
                    )
                    log_message(f"[{router_ip}] {session_result['error']}")
                    return session_result
            elif kind != MATCH_PROMPT:
                # Neither a prompt nor a password request appeared in the
                # initial window. Some platforms emit a long banner first;
                # we log a warning but continue and rely on the per-command
                # max_wait to recover.
                log_message(
                    f"[{router_ip}] warning: shell entry returned no prompt "
                    f"({kind}); proceeding anyway"
                )

            # Phase 3: Capture the device's prompt for accurate completion
            # detection. Fall back to the default regex if extraction fails.
            captured_prompt = extract_prompt("".join(auth_banner))
            cmd_prompt_re = build_command_prompt_re(captured_prompt)
            if captured_prompt:
                log_message(f"[{router_ip}] prompt: {captured_prompt}")
            else:
                log_message(
                    f"[{router_ip}] prompt: <not captured, using default regex>"
                )

            # Phase 4: Disable pagination. Wait for the captured prompt to
            # ensure the shell is settled before user commands start.
            shell.send("terminal length 0\n")
            _, term_kind = read_channel(
                shell,
                prompt_re=cmd_prompt_re,
                idle_timeout=1.0,
                max_wait=5.0,
            )
            if term_kind != MATCH_PROMPT:
                log_message(
                    f"[{router_ip}] warning: 'terminal length 0' did not "
                    f"return a prompt ({term_kind}); first command output may "
                    "include residual data"
                )

        # Phase 5: Run the user's commands. Each command captures its own
        # metadata (start time, duration, exit kind) so that downstream
        # writers can render boundaries (Issue 9) and structured outputs
        # (Issue 14). A missing/empty commands_file (e.g. a device type with
        # no split list) leaves the session connected but command-free.
        if not commands_file:
            log_message(
                f"[{router_ip}] no commands file for device type "
                f"{device_type}; connected but running no commands"
            )
            session_result["status"] = SESSION_OK
            return session_result
        with open(commands_file, "r") as file:
            for line in file:
                command = line.strip()
                if not command or command.startswith("#"):
                    continue
                log_message(f"[{router_ip}] running: {command}")
                cmd_started_wall = now_iso()
                cmd_started_mono = time.monotonic()
                shell.send(f"{command}\n")
                # Prompt detection short-circuits short commands; max_wait
                # is the safety upper bound for long commands like
                # 'show tech-support'.
                command_output, cmd_kind = read_channel(
                    shell,
                    prompt_re=cmd_prompt_re,
                    idle_timeout=1.0,
                    max_wait=120.0,
                )
                cmd_status = CMD_OK if cmd_kind == MATCH_PROMPT else CMD_TIMEOUT
                session_result["commands"].append(
                    {
                        "command": command,
                        "started_at": cmd_started_wall,
                        "duration_s": time.monotonic() - cmd_started_mono,
                        "exit_kind": cmd_kind,
                        "status": cmd_status,
                        "output": command_output,
                    }
                )
                if cmd_status == CMD_OK:
                    log_message(f"[{router_ip}] done: {command}")
                else:
                    log_message(
                        f"[{router_ip}] command timeout ({cmd_kind}): "
                        f"{command}"
                    )

        session_result["status"] = SESSION_OK
    except (paramiko.SSHException, socket.timeout, OSError) as ex:
        session_result["status"] = SESSION_OTHER_ERR
        session_result["error"] = str(ex)
        log_message(f"[{router_ip}] error: {ex}")
    finally:
        try:
            ssh.close()
        except Exception:
            pass
        session_result["ended_at"] = now_iso()
        session_result["duration_s"] = time.monotonic() - started_mono
        try:
            _write_outputs(session_result, output_paths)
        except OSError as ex:
            log_message(f"[{router_ip}] failed to write output: {ex}")
    return session_result

def _parse_output_formats(arg_value):
    """Validate --output-format and return a list of unique format names."""
    raw = [p.strip().lower() for p in arg_value.split(",") if p.strip()]
    if not raw:
        raise argparse.ArgumentTypeError("--output-format requires at least one format")
    seen = []
    for fmt in raw:
        if fmt not in ALL_OUTPUT_FORMATS:
            raise argparse.ArgumentTypeError(
                f"unknown output format: {fmt!r}. "
                f"Valid: {', '.join(ALL_OUTPUT_FORMATS)}"
            )
        if fmt not in seen:
            seen.append(fmt)
    return seen


def _build_output_paths(logs_dir, router_ip, timestamp, formats):
    """Build a {format -> path} dict for one host run."""
    ext_map = {
        OUTPUT_FORMAT_TEXT: "txt",
        OUTPUT_FORMAT_JSON: "json",
        OUTPUT_FORMAT_CSV: "csv",
    }
    paths = {}
    for fmt in formats:
        ext = ext_map[fmt]
        paths[fmt] = os.path.join(logs_dir, f"output_{router_ip}_{timestamp}.{ext}")
    return paths


if __name__ == "__main__":
    # Create argument parser
    parser = argparse.ArgumentParser(
        description="Connect to Cisco SD-WAN routers and execute commands.",
        epilog=(
            "Examples:\n"
            "  Hosts file with embedded passwords (legacy):\n"
            "    python3 bulk-show.py hosts.txt commands.txt\n"
            "  Hosts file with 'ip,user' only; password prompted once:\n"
            "    python3 bulk-show.py hosts.txt commands.txt\n"
            "  Force a single shared password for all hosts:\n"
            "    python3 bulk-show.py hosts.txt commands.txt --password-prompt\n"
            "  Reject unknown SSH host keys (production):\n"
            "    python3 bulk-show.py hosts.txt commands.txt --reject-unknown-hosts\n"
            "  Override SSH port (e.g. non-SD-WAN device on 22) and cap parallelism:\n"
            "    python3 bulk-show.py hosts.txt commands.txt --port 22 --max-workers 8\n"
            "  Retry transient connect failures three times, 10s apart:\n"
            "    python3 bulk-show.py hosts.txt commands.txt --retries 3 --retry-delay 10\n"
            "  Emit text + JSON + CSV outputs per host:\n"
            "    python3 bulk-show.py hosts.txt commands.txt --output-format text,json,csv\n"
            "  Mix edges and controllers (vBond/vSmart) in one hosts file:\n"
            "    python3 bulk-show.py hosts.txt commands.txt\n"
            "  Separate command lists per device type (controller vs edge):\n"
            "    python3 bulk-show.py hosts.txt commands.txt \\\n"
            "      --controller-commands ctrl.txt --edge-commands edge.txt\n"
            "\n"
            "Hosts file format: one host per line, comma-separated.\n"
            "  ip,username                       edge,  password prompted at startup\n"
            "  ip,username,password              edge,  password embedded (not recommended)\n"
            "  ip,username,controller            controller, password prompted at startup\n"
            "  ip,username,password,controller   controller, password embedded\n"
            "  ip,username,type=controller       explicit device type (also: type=edge)\n"
            "Device type defaults to 'edge'. Controllers connect on --controller-port\n"
            "(default 22) with a single password and no 'shell' step; edges connect on\n"
            "--port (default 830) and may re-prompt for the password inside 'shell'.\n"
            "Lines starting with '#' and blank lines are ignored.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "hosts_file",
        help="File listing hosts. Each line: 'ip,username[,password]' with an "
             "optional device type ('controller'/'vsmart'/'vbond' or "
             "'type=controller'); defaults to edge.",
    )
    parser.add_argument(
        "commands_file",
        help="Default/fallback file with the list of commands. Applied to any "
             "device type that does not have a more specific list via "
             "--controller-commands / --edge-commands.",
    )
    parser.add_argument(
        "--controller-commands",
        default=None,
        help="Optional commands file applied only to controller hosts "
             "(vBond/vSmart/vEdge). Falls back to the positional commands "
             "file when omitted.",
    )
    parser.add_argument(
        "--edge-commands",
        default=None,
        help="Optional commands file applied only to edge hosts "
             "(cEdge/IOS-XE). Falls back to the positional commands file "
             "when omitted.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=830,
        help=(
            "SSH TCP port to connect to on every host (default: 830). "
            "Cisco SD-WAN edges expose the interactive SSH service used by "
            "vManage vshell on 830; override only when targeting "
            "non-SD-WAN devices that use the conventional 22."
        ),
    )
    parser.add_argument(
        "--controller-port",
        type=int,
        default=22,
        help=(
            "SSH TCP port for hosts marked as controllers (default: 22). "
            "vBond / vSmart reached through vManage land directly in the "
            "viptela CLI on the conventional port 22 with a single password "
            "(no 'shell' step). Mark a host as a controller in the hosts "
            "file with a 'type=controller' token or a bare keyword "
            "(controller/vsmart/vbond)."
        ),
    )
    parser.add_argument(
        "--reject-unknown-hosts",
        action="store_true",
        help="Reject hosts not present in known_hosts (safer; protects against MITM). "
             "Default: auto-add unknown keys with a warning.",
    )
    parser.add_argument(
        "--password-prompt",
        action="store_true",
        help="Prompt once for a password and apply it to ALL hosts, ignoring any "
             "password embedded in the hosts file.",
    )
    parser.add_argument(
        "--logs-dir",
        default="logs",
        help="Directory to store output logs (default: logs)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Maximum concurrent SSH sessions. Default: min(8, number of hosts). "
             "Set higher to fan out faster, lower to reduce load on the network.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        help="Additional SSH connect attempts after a transient network/SSH "
             "failure (default: 0). Authentication failures are NEVER retried.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Seconds to sleep between connect attempts (default: 5.0).",
    )
    parser.add_argument(
        "--output-format",
        type=_parse_output_formats,
        default=[OUTPUT_FORMAT_TEXT],
        help="Comma-separated output formats per host: text, json, csv. "
             "Default: text. Multiple formats produce multiple files per host.",
    )
    args = parser.parse_args()

    # Validate numeric arguments early so misuse fails before any I/O.
    if args.port < 1 or args.port > 65535:
        print(
            f"--port must be between 1 and 65535 (got {args.port})",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.controller_port < 1 or args.controller_port > 65535:
        print(
            f"--controller-port must be between 1 and 65535 "
            f"(got {args.controller_port})",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.max_workers is not None and args.max_workers < 1:
        print(
            f"--max-workers must be >= 1 (got {args.max_workers})",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.retries < 0:
        print(
            f"--retries must be >= 0 (got {args.retries})",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.retry_delay < 0:
        print(
            f"--retry-delay must be >= 0 (got {args.retry_delay})",
            file=sys.stderr,
        )
        sys.exit(2)

    # Validate the command files that were actually provided. The positional
    # commands_file is always required; the split files are optional and only
    # checked when supplied.
    for label, path in (
        ("commands_file", args.commands_file),
        ("--controller-commands", args.controller_commands),
        ("--edge-commands", args.edge_commands),
    ):
        if path and not os.path.isfile(path):
            print(f"{label} not found: {path}", file=sys.stderr)
            sys.exit(2)

    # Warn once when MITM-risky AutoAddPolicy is in effect.
    if not args.reject_unknown_hosts:
        print(
            "[WARN] Auto-accepting unknown SSH host keys (MITM risk). "
            "Re-run with --reject-unknown-hosts after the hosts are registered "
            "in ~/.ssh/known_hosts to enforce verification.",
            file=sys.stderr,
            flush=True,
        )

    # Read hosts file and parse entries.
    # Supported formats per non-empty/non-comment line (see parse_host_line):
    #   "ip,user"                        -> edge,       shared prompt
    #   "ip,user,password"               -> edge,       embedded (legacy)
    #   "ip,user[,password],controller"  -> controller  (bare keyword)
    #   "ip,user[,password],type=...."   -> explicit device type
    with open(args.hosts_file, "r") as hosts_file:
        host_lines = hosts_file.readlines()

    # list of tuples: (router_ip, username, password_or_None, device_type)
    parsed_hosts = []
    needs_shared_password = False

    for line in host_lines:
        try:
            parsed = parse_host_line(line)
        except ValueError as exc:
            print(f"Invalid host entry: {line.strip()}. {exc}. Skipping.")
            continue
        if parsed is None:
            continue
        router_ip, username, password, device_type = parsed

        if not is_valid_ip(router_ip):
            print(f"Invalid IP address: {router_ip}. Skipping this host.")
            continue

        if password is None:
            needs_shared_password = True

        parsed_hosts.append((router_ip, username, password, device_type))

    if not parsed_hosts:
        print("No valid hosts found. Aborting.", file=sys.stderr)
        sys.exit(1)

    # Decide whether to prompt for a shared password:
    #   - Explicit --password-prompt always prompts (and overrides any embedded pw).
    #   - If at least one row omitted the password, prompt once and reuse it.
    shared_password = None
    if args.password_prompt or needs_shared_password:
        prompt_label = (
            "SSH password (used for ALL hosts, overriding the file): "
            if args.password_prompt
            else "SSH password (used for hosts without one in the file): "
        )
        try:
            shared_password = getpass.getpass(prompt_label)
        except (EOFError, KeyboardInterrupt):
            print("\nPassword entry aborted.", file=sys.stderr)
            sys.exit(1)
        if not shared_password:
            print("Empty password entered. Aborting.", file=sys.stderr)
            sys.exit(1)

    logs_dir = args.logs_dir
    os.makedirs(logs_dir, exist_ok=True)

    # Bound concurrency. Default: min(8, hosts) keeps small jobs serial-ish
    # while still benefiting from parallelism on big batches.
    if args.max_workers is None:
        max_workers = min(8, len(parsed_hosts))
    else:
        max_workers = args.max_workers
    log_message(
        f"[main] starting {len(parsed_hosts)} host(s) with up to "
        f"{max_workers} concurrent worker(s); output formats: "
        f"{','.join(args.output_format)}"
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for router_ip, username, password, device_type in parsed_hosts:
            # Choose effective password:
            #   --password-prompt -> shared overrides file
            #   missing in file   -> shared
            #   else              -> embedded password
            if args.password_prompt or password is None:
                effective_password = shared_password
            else:
                effective_password = password

            # Controllers default to TCP/22; edges to --port (830).
            if device_type == DEVICE_CONTROLLER:
                effective_port = args.controller_port
            else:
                effective_port = args.port

            # Resolve the command list this host should run from its type.
            commands_file = resolve_commands_file(
                device_type,
                args.commands_file,
                args.controller_commands,
                args.edge_commands,
            )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_paths = _build_output_paths(
                logs_dir, router_ip, timestamp, args.output_format
            )
            future = executor.submit(
                connect_and_execute,
                router_ip,
                username,
                effective_password,
                commands_file,
                output_paths,
                not args.reject_unknown_hosts,
                effective_port,
                args.retries,
                args.retry_delay,
                device_type,
            )
            futures.append(future)

        # Wait for all futures to complete; surface aggregate counts.
        ok = 0
        bad = 0
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result and result.get("status") == SESSION_OK:
                ok += 1
            else:
                bad += 1
        log_message(f"[main] done: success={ok}, failed={bad}")
