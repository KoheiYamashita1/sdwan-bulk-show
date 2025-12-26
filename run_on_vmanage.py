import argparse
import getpass
import os
import pathlib
import sys
import time

import paramiko

VERBOSE = False


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
        help="Commands file name inside local dir (default: commands.txt)",
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
        "--quiet",
        action="store_true",
        help="Only print essential logs",
    )
    return parser.parse_args()


def expand_remote_path(remote_path):
    if remote_path.startswith("~/"):
        return remote_path
    if remote_path == "~":
        return remote_path
    return remote_path


def sftp_mkdir_p(sftp, remote_dir):
    parts = pathlib.PurePosixPath(remote_dir).parts
    path = ""
    for part in parts:
        if part == "~":
            path = "~"
            continue
        if path in ("", "/"):
            path = f"/{part}"
        elif path == "~":
            path = f"~/{part}"
        else:
            path = f"{path}/{part}"
        try:
            sftp.stat(path)
        except FileNotFoundError:
            sftp.mkdir(path)


def remote_exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def confirm_overwrite(remote_paths, force):
    if not remote_paths:
        return True
    return True


def read_channel(channel, idle_timeout=1.0, max_wait=10.0):
    channel.settimeout(idle_timeout)
    chunks = []
    start = time.monotonic()
    last_data = start
    while True:
        now = time.monotonic()
        if now - start >= max_wait:
            break
        try:
            data = channel.recv(4096)
            if not data:
                break
            chunks.append(data.decode(errors="replace"))
            last_data = now
        except Exception:
            if now - last_data >= idle_timeout:
                break
    return "".join(chunks)


def run_remote_command(ssh, command, get_pty=False):
    stdin, stdout, stderr = ssh.exec_command(command, get_pty=get_pty)
    stdin.close()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return out, err, exit_status


def read_until_any(channel, patterns, max_wait=30.0):
    end_time = time.monotonic() + max_wait
    buffer = ""
    while time.monotonic() < end_time:
        if channel.recv_ready():
            data = channel.recv(4096).decode(errors="replace")
            buffer += data
            for pattern in patterns:
                if pattern in buffer:
                    return buffer, pattern
        else:
            time.sleep(0.2)
    return buffer, None


def run_vshell_command(channel, command, prompt_patterns, max_wait=60.0):
    channel.send(f"{command}\n")
    output, _ = read_until_any(channel, prompt_patterns, max_wait=max_wait)
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

    for path in (bulk_script, hosts_file, commands_file):
        if not path.exists():
            log(f"Missing local file: {path}")
            sys.exit(1)

    if not args.password and not args.key:
        args.password = getpass.getpass("SSH password: ")

    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
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

    remote_base = expand_remote_path(args.remote_dir)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    remote_dir = f"{remote_base}/{timestamp}"
    if not args.quiet:
        log(f"[{args.vmanage_host}] using remote dir: {remote_dir}")
    local_logs_dir = local_dir / "logs" / timestamp
    sftp = ssh.open_sftp()
    try:
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
        existing = [
            path
            for path in (remote_bulk, remote_hosts, remote_commands)
            if remote_exists(sftp, path)
        ]
        if not confirm_overwrite(existing, True):
            log("Upload cancelled.")
            return
        if not args.quiet:
            log(f"[{args.vmanage_host}] uploading files to {remote_dir}")
        sftp.put(str(bulk_script), remote_bulk)
        sftp.put(str(hosts_file), remote_hosts)
        sftp.put(str(commands_file), remote_commands)
    finally:
        sftp.close()

    remote_logs_dir = f"{remote_dir}/logs"
    remote_cmd = (
        f"python3 {remote_dir}/{bulk_script.name} "
        f"{remote_dir}/{hosts_file.name} {remote_dir}/{commands_file.name} "
        f"--logs-dir {remote_logs_dir}"
    )
    if not args.quiet:
        log(f"[{args.vmanage_host}] running via vshell session")
    shell = ssh.invoke_shell()
    read_until_any(shell, ["vmanage#", "vmanage>"], max_wait=10.0)
    run_vshell_command(shell, "vshell", ["vmanage:~$", "vmanage:~#"], max_wait=10.0)

    try:
        out = run_vshell_command(
            shell,
            remote_cmd,
            ["vmanage:~$", "vmanage:~#"],
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
    try:
        logs_out = run_vshell_command(
            shell,
            f"ls -lt {remote_logs_dir} | head -n 5",
            ["vmanage:~$", "vmanage:~#"],
            max_wait=10.0,
        )
    except Exception as exc:
        print(f"[{args.vmanage_host}] remote logs error: {exc}", file=sys.stderr)
        sys.exit(1)
        if logs_out.strip():
            for line in logs_out.splitlines():
                vlog(line.rstrip())
        else:
            vlog("(no logs found)")
    else:
        if not args.quiet:
            log(f"[{args.vmanage_host}] remote logs: (use --verbose to show)")

    run_vshell_command(shell, "exit", ["vmanage#", "vmanage>"], max_wait=10.0)
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
