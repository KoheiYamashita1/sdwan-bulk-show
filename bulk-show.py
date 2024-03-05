import paramiko
import argparse
import time
import ipaddress
import concurrent.futures

def is_valid_ip(ip_address):
    try:
        ipaddress.IPv4Address(ip_address)
        return True
    except ipaddress.AddressValueError:
        return False

def connect_and_execute(router_ip, username, password, commands_file, output_filename):
    # Create an SSH client
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Connect to the router using the NETCONF port (port 830)
        ssh.connect(router_ip, port=830, username=username, password=password)

        # Start an interactive shell
        shell = ssh.invoke_shell()

        # Send a command to invoke the shell (e.g., 'shell' command)
        shell.send("shell\n")

        # Wait for the command to execute and receive the output
        time.sleep(1)  # Add a small delay to ensure the password prompt is received
        output = ""
        while shell.recv_ready():
            output += shell.recv(1024).decode()

        # Check if the password prompt is present
        if "password:" in output.lower():
            # Send the password again to authenticate
            shell.send(f"{password}\n")

        # Set terminal length to 0 to disable pagination
        shell.send("terminal length 0\n")

        # Wait for the command to execute and receive the output
        time.sleep(2)  # Add a small delay to ensure the command output is received
        output = ""
        while shell.recv_ready():
            output += shell.recv(1024).decode()

        # Read commands from the file and send them one by one
        with open(commands_file, "r") as file:
            for line in file:
                command = line.strip()
                shell.send(f"{command}\n")
                time.sleep(1)  # Add a small delay between commands

                # Receive and store the command output in the output_file
                command_output = ""
                while shell.recv_ready():
                    command_output += shell.recv(1024).decode()

                with open(output_filename, "a") as output_file:
                    output_file.write(command_output)

                time.sleep(1)  # Add a small delay to allow the router to respond with the final output

        # Close the SSH connection
        ssh.close()

    except paramiko.AuthenticationException:
        print(f"Authentication failed for router {router_ip}. Please check your credentials.")
    except paramiko.SSHException as ssh_ex:
        print(f"Error while connecting to router {router_ip}: {ssh_ex}")

if __name__ == "__main__":
    # Create argument parser
    parser = argparse.ArgumentParser(description="Connect to Cisco SD-WAN routers and execute commands.")
    parser.add_argument("hosts_file", help="The file containing the list of hosts (IP, username, password)")
    parser.add_argument("commands_file", help="The file containing the list of commands")
    args = parser.parse_args()

    # Read hosts file and execute commands for each valid router using parallel processing
    with open(args.hosts_file, "r") as hosts_file:
        host_lines = hosts_file.readlines()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = []
        for line in host_lines:
            router_ip, username, password = line.strip().split(",")

            # Check if the IP address is valid
            if not is_valid_ip(router_ip.strip()):
                print(f"Invalid IP address: {router_ip.strip()}. Skipping this host.")
                continue

            output_filename = f"output_{router_ip.strip()}.txt"
            future = executor.submit(connect_and_execute, router_ip.strip(), username.strip(), password.strip(), args.commands_file, output_filename)
            futures.append(future)

        # Wait for all futures to complete
        for future in concurrent.futures.as_completed(futures):
            future.result()

