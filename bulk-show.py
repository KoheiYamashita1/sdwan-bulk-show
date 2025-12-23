import paramiko
import argparse
import time
import ipaddress
import concurrent.futures
import socket

def is_valid_ip(ip_address):
    try:
        ipaddress.IPv4Address(ip_address)
        return True
    except ipaddress.AddressValueError:
        return False

def connect_and_execute(router_ip, username, password, commands_file, output_filename, allow_unknown_hosts=False):
    # Create an SSH client
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    if allow_unknown_hosts:
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

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
            except socket.timeout:
                if now - last_data >= idle_timeout:
                    break
        return "".join(chunks)

    try:
        # Connect to the router using the NETCONF port (port 830)
        ssh.connect(router_ip, port=830, username=username, password=password, timeout=10)

        # Start an interactive shell
        shell = ssh.invoke_shell()

        # Send a command to invoke the shell (e.g., 'shell' command)
        shell.send("shell\n")

        # Wait for the command to execute and receive the output
        output = read_channel(shell, idle_timeout=1.0, max_wait=5.0)

        # Check if the password prompt is present
        if "password:" in output.lower():
            # Send the password again to authenticate
            shell.send(f"{password}\n")
            output += read_channel(shell, idle_timeout=1.0, max_wait=5.0)

        # Set terminal length to 0 to disable pagination
        shell.send("terminal length 0\n")

        # Wait for the command to execute and receive the output
        output = read_channel(shell, idle_timeout=1.0, max_wait=5.0)

        # Read commands from the file and send them one by one
        with open(commands_file, "r") as file, open(output_filename, "a") as output_file:
            for line in file:
                command = line.strip()
                if not command or command.startswith("#"):
                    continue
                shell.send(f"{command}\n")
                command_output = read_channel(shell, idle_timeout=1.0, max_wait=10.0)
                output_file.write(command_output)

        # Close the SSH connection
    except (paramiko.AuthenticationException, paramiko.SSHException, socket.timeout, OSError) as ex:
        print(f"Error while connecting to router {router_ip}: {ex}")
    finally:
        try:
            ssh.close()
        except Exception:
            pass

if __name__ == "__main__":
    # Create argument parser
    parser = argparse.ArgumentParser(description="Connect to Cisco SD-WAN routers and execute commands.")
    parser.add_argument("hosts_file", help="The file containing the list of hosts (IP, username, password)")
    parser.add_argument("commands_file", help="The file containing the list of commands")
    parser.add_argument(
        "--accept-unknown-hosts",
        action="store_true",
        help="Allow hosts not present in known_hosts (auto-add).",
    )
    args = parser.parse_args()

    # Read hosts file and execute commands for each valid router using parallel processing
    with open(args.hosts_file, "r") as hosts_file:
        host_lines = hosts_file.readlines()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []
        for line in host_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = [p.strip() for p in stripped.split(",")]
            if len(parts) != 3:
                print(f"Invalid host entry: {line.strip()}. Expected 'ip,username,password'. Skipping.")
                continue
            router_ip, username, password = parts

            # Check if the IP address is valid
            if not is_valid_ip(router_ip.strip()):
                print(f"Invalid IP address: {router_ip.strip()}. Skipping this host.")
                continue

            output_filename = f"output_{router_ip.strip()}.txt"
            future = executor.submit(
                connect_and_execute,
                router_ip.strip(),
                username.strip(),
                password.strip(),
                args.commands_file,
                output_filename,
                args.accept_unknown_hosts,
            )
            futures.append(future)

        # Wait for all futures to complete
        for future in concurrent.futures.as_completed(futures):
            future.result()
