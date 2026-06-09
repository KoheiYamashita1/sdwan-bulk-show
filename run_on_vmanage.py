import argparse
import getpass
import os
import pathlib
import re
import sys
import time

import paramiko

VERBOSE = False

# vManage CLI mode prompt: hostname + (# or >). Excludes ':' to avoid colliding
# with vshell prompts like "vmanage:~#".
# Examples that match: "vmanage#", "vmanage-01#", "primary-vmanage>"
CLI_PROMPT_RE = re.compile(r"(?:^|\n)[^\s:]+[#>]\s*\Z")

# vshell (Linux shell) prompt: hostname + ":~" + ($ or #).
# Examples that match: "vmanage:~$", "vmanage-01:~#"
SHELL_PROMPT_RE = re.compile(r"(?:^|\n)\S+:~[#$]\s*\Z")


def log(message):
    print(message, flush=True)


def vlog(message):
    if VERBOSE:
        print(message, flush=True)


def log_errors_only(output):
    lines = []
    for line in output.splitlines():
        if any(tag in line for tag in ("Error", "ERROR", "error", "failed", "invalid", "% ")):
            lines.append(line)
    if lines:
        print("\n".join(lines), file=sys.stderr, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Upload bulk-show.py + inputs to vManage and run remotely via SSH.",
        epilog=(
            "Examples:\n"
            "  Password auth:\n"
            "    python run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin \\\n"
            "      --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs\n"
            "  SSH key auth:\n"
            "    python run_on_vmanage.py 10.71.131.72 --user sdwan --key ~/.ssh/id_rsa \\\n"
            "      --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs\n"
            "  Notes:\n"
            "    - A timestamped subdirectory is created under --remote-dir for each run.\n"
            "    - Output logs are downloaded to ./logs/<timestamp>/.\n"
            "    - Remote logs are written under <remote-dir>/<timestamp>/logs.\n"
            "    - Use --verbose for detailed remote output, or --quiet for minimal logs.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("vmanage_host", help="vManage hostname or IP")
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--user", required=True, help="SSH username")
    parser.add_argument(
        "--password",
        help="SSH password (if omitted and --key is not set, prompt)",
    )
    parser.add_argument(
        "--key",
        help="Path to SSH private key (optional, use instead of password)",
    )
    parser.add_argument(
        "--remote-dir",
        default="~/sdwan-bulk-show",
        help="Remote working directory (default: ~/sdwan-bulk-show)",
    )
    parser.add_argument(
        "--local-dir",
        default=".",
        help="Local directory containing bulk-show.py and input files (default: .)",
    )
    parser.add_argument(
        "--hosts",
        default="hosts.txt",
        help="Hosts file name inside local dir (default: hosts.txt)",
    )
    parser.add_argument(
        "--commands",
        default="commands.txt",
        help="Default/fallback commands file name inside local dir "
             "(default: commands.txt). Applied to any device type without a "
             "more specific list below.",
    )
    parser.add_argument(
        "--controller-commands",
        default=None,
        help="Optional commands file name inside local dir applied only to "
             "controller hosts (vBond/vSmart/vEdge). Uploaded and forwarded "
             "to bulk-show.py as --controller-commands.",
    )
    parser.add_argument(
        "--edge-commands",
        default=None,
        help="Optional commands file name inside local dir applied only to "
             "edge hosts (cEdge/IOS-XE). Uploaded and forwarded to "
             "bulk-show.py as --edge-commands.",
    )
    parser.add_argument(
        "--bulk-script",
        default="bulk-show.py",
        help="Bulk script file name inside local dir (default: bulk-show.py)",
    )
    parser.add_argument(
        "--download-outputs",
        action="store_true",
        help="Download output_*.txt files after completion",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed remote output",
    )
    parser.add_argument(
        "--reject-unknown-hosts",
        action="store_true",
        help="Reject the SSH connection if the vManage host key is not in known_hosts "
             "(safer; protects against MITM). Default: auto-add unknown keys.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print essential logs",
    )
    return parser.parse_args()


def resolve_remote_dir(sftp, remote_dir):
    """Expand leading '~' and resolve to an absolute path on the SFTP server.

    SFTP itself does not interpret '~' (it's a shell construct), so we expand
    it explicitly using the SFTP session's working directory, which on most
    servers is the user's home directory at login time. This guarantees that
    subsequent sftp.mkdir/stat/put receive a real absolute path rather than
    the literal "~" character.

    - "~"            -> "<home>"
    - "~/<rest>"     -> "<home>/<rest>"
    - "/abs/path"    -> returned as-is
    - "rel/path"     -> "<home>/rel/path" (treated as relative to home)
    """
    if remote_dir == "~":
        return sftp.normalize(".")
    if remote_dir.startswith("~/"):
        return f"{sftp.normalize('.')}/{remote_dir[2:]}"
    if remote_dir.startswith("/"):
        return remote_dir
    # Relative path: anchor to home for predictable behavior
    return f"{sftp.normalize('.')}/{remote_dir}"


def sftp_mkdir_p(sftp, remote_dir):
    """Create remote_dir and any missing parent directories (idempotent).

    `remote_dir` MUST be an absolute path. Use resolve_remote_dir() first
    to expand '~' or relative paths before calling this function.
    """
    if not remote_dir.startswith("/"):
        raise ValueError(
            f"sftp_mkdir_p requires an absolute path (got: {remote_dir!r}). "
            "Call resolve_remote_dir(sftp, ...) first to expand '~' or relative paths."
        )
    parts = pathlib.PurePosixPath(remote_dir).parts  # e.g., ('/', 'home', 'sdwan')
    path = ""
    for part in parts:
        if part == "/":
            path = "/"
            continue
        path = f"{path}{part}" if path == "/" else f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def read_until_re(channel, prompt_re, max_wait=30.0):
    """Read from `channel` until `prompt_re` matches the buffer tail or `max_wait` elapses.

    Matching only the tail (last 256 chars) avoids accidental matches against earlier
    output (e.g., command echo) and keeps the regex cheap on large buffers.
    Returns (buffer, matched_bool).
    """
    end_time = time.monotonic() + max_wait
    buffer = ""
    while time.monotonic() < end_time:
        if channel.recv_ready():
            data = channel.recv(4096).decode(errors="replace")
            buffer += data
            tail = buffer[-256:] if len(buffer) > 256 else buffer
            if prompt_re.search(tail):
                return buffer, True
        else:
            time.sleep(0.2)
    return buffer, False


def run_vshell_command(channel, command, prompt_re, max_wait=60.0):
    channel.send(f"{command}\n")
    output, _ = read_until_re(channel, prompt_re, max_wait=max_wait)
    return output


def main():
    args = parse_args()
    global VERBOSE
    if args.quiet and args.verbose:
        print("Error: --quiet and --verbose cannot be used together.", file=sys.stderr)
        sys.exit(2)
    VERBOSE = args.verbose

    local_dir = pathlib.Path(args.local_dir).resolve()
    bulk_script = local_dir / args.bulk_script
    hosts_file = local_dir / args.hosts
    commands_file = local_dir / args.commands
    controller_commands_file = (
        local_dir / args.controller_commands if args.controller_commands else None
    )
    edge_commands_file = (
        local_dir / args.edge_commands if args.edge_commands else None
    )

    required_files = [bulk_script, hosts_file, commands_file]
    for path in (controller_commands_file, edge_commands_file):
        if path is not None:
            required_files.append(path)
    for path in required_files:
        if not path.exists():
            log(f"Missing local file: {path}")
            sys.exit(1)

    if not args.password and not args.key:
        args.password = getpass.getpass("SSH password: ")

    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    if args.reject_unknown_hosts:
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        print(
            f"[WARN] Auto-accepting unknown SSH host key for {args.vmanage_host} "
            "(MITM risk). Re-run with --reject-unknown-hosts after the host is "
            "registered in ~/.ssh/known_hosts to enforce verification.",
            file=sys.stderr,
            flush=True,
        )
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": args.vmanage_host,
        "port": args.port,
        "username": args.user,
        "timeout": 15,
    }
    if args.key:
        connect_kwargs["key_filename"] = args.key
    else:
        connect_kwargs["password"] = args.password

    if not args.quiet:
        log(f"[{args.vmanage_host}] connecting...")
    try:
        ssh.connect(**connect_kwargs)
    except Exception as exc:
        print(f"[{args.vmanage_host}] connect error: {exc}", file=sys.stderr)
        sys.exit(1)
    if not args.quiet:
        log(f"[{args.vmanage_host}] connected")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    local_logs_dir = local_dir / "logs" / timestamp
    sftp = ssh.open_sftp()
    try:
        # Expand '~' / relative paths against the SFTP server's home directory
        # before any sftp.mkdir/stat call (SFTP does not interpret '~' itself).
        remote_base = resolve_remote_dir(sftp, args.remote_dir)
        remote_dir = f"{remote_base}/{timestamp}"
        if not args.quiet:
            log(f"[{args.vmanage_host}] using remote dir: {remote_dir}")
        sftp_mkdir_p(sftp, remote_dir)
    finally:
        sftp.close()

    sftp = ssh.open_sftp()
    try:
        if not args.quiet:
            log(f"[{args.vmanage_host}] preparing to upload files to {remote_dir}")
        remote_bulk = f"{remote_dir}/{bulk_script.name}"
        remote_hosts = f"{remote_dir}/{hosts_file.name}"
        remote_commands = f"{remote_dir}/{commands_file.name}"
        if not args.quiet:
            log(f"[{args.vmanage_host}] uploading files to {remote_dir}")
        sftp.put(str(bulk_script), remote_bulk)
        sftp.put(str(hosts_file), remote_hosts)
        sftp.put(str(commands_file), remote_commands)
        if controller_commands_file is not None:
            sftp.put(
                str(controller_commands_file),
                f"{remote_dir}/{controller_commands_file.name}",
            )
        if edge_commands_file is not None:
            sftp.put(
                str(edge_commands_file),
                f"{remote_dir}/{edge_commands_file.name}",
            )
    finally:
        sftp.close()

    remote_logs_dir = f"{remote_dir}/logs"
    remote_cmd = (
        f"python3 {remote_dir}/{bulk_script.name} "
        f"{remote_dir}/{hosts_file.name} {remote_dir}/{commands_file.name} "
        f"--logs-dir {remote_logs_dir}"
    )
    if controller_commands_file is not None:
        remote_cmd += (
            f" --controller-commands {remote_dir}/{controller_commands_file.name}"
        )
    if edge_commands_file is not None:
        remote_cmd += f" --edge-commands {remote_dir}/{edge_commands_file.name}"
    if not args.quiet:
        log(f"[{args.vmanage_host}] running via vshell session")
    shell = ssh.invoke_shell()
    read_until_re(shell, CLI_PROMPT_RE, max_wait=10.0)
    run_vshell_command(shell, "vshell", SHELL_PROMPT_RE, max_wait=10.0)

    try:
        out = run_vshell_command(
            shell,
            remote_cmd,
            SHELL_PROMPT_RE,
            max_wait=600.0,
        )
    except Exception as exc:
        print(f"[{args.vmanage_host}] remote command error: {exc}", file=sys.stderr)
        sys.exit(1)
    if out.strip():
        for line in out.splitlines():
            vlog(line.rstrip())
        if args.quiet:
            log_errors_only(out)

    if VERBOSE:
        log(f"[{args.vmanage_host}] latest remote logs:")
        logs_out = ""
        try:
            logs_out = run_vshell_command(
                shell,
                f"ls -lt {remote_logs_dir} | head -n 5",
                SHELL_PROMPT_RE,
                max_wait=10.0,
            )
        except Exception as exc:
            print(
                f"[{args.vmanage_host}] remote logs warning: {exc} (continuing)",
                file=sys.stderr,
            )
        if logs_out.strip():
            for line in logs_out.splitlines():
                vlog(line.rstrip())
        else:
            vlog("(no logs found)")
    else:
        if not args.quiet:
            log(f"[{args.vmanage_host}] remote logs: (use --verbose to show)")

    run_vshell_command(shell, "exit", CLI_PROMPT_RE, max_wait=10.0)
    shell.close()

    if args.download_outputs:
        try:
            sftp = ssh.open_sftp()
            try:
                os.makedirs(local_logs_dir, exist_ok=True)
                try:
                    entries = sftp.listdir(remote_logs_dir)
                    remote_source = remote_logs_dir
                except FileNotFoundError:
                    entries = sftp.listdir(remote_dir)
                    remote_source = remote_dir
                if not args.quiet:
                    log(f"[{args.vmanage_host}] downloading output_*.txt -> {local_logs_dir}")
                for entry in entries:
                    if entry.startswith("output_") and entry.endswith(".txt"):
                        local_path = local_logs_dir / entry
                        sftp.get(f"{remote_source}/{entry}", str(local_path))
            finally:
                sftp.close()
        except Exception as exc:
            print(f"[{args.vmanage_host}] download error: {exc}", file=sys.stderr)
            sys.exit(1)

    ssh.close()
    if not args.quiet:
        log(f"[{args.vmanage_host}] done")


if __name__ == "__main__":
    main()
