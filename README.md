# sdwan-bulk-show
This repo provides a wrapper to run bulk-show.py on vManage and collect logs from multiple SD-WAN devices.

English: README.md
Japanese: README.ja.md

# run_on_vmanage.py (recommended)
The wrapper uploads bulk-show.py + input files to vManage, runs the script remotely, and optionally downloads output logs.

Execution flow (overview):
1) From your local PC, the wrapper connects to vManage over SSH.
2) A timestamped working directory is created under --remote-dir.
3) bulk-show.py, hosts file, and commands file are uploaded into that directory.
4) The wrapper enters vshell and runs bulk-show.py on vManage.
5) bulk-show.py logs into each SD-WAN device listed in hosts, runs each command, and writes output logs.
6) The wrapper downloads the generated output_*.txt files to ./logs/<timestamp>/.

Flow diagram:
```
Local PC -> SSH -> vManage -> vshell -> bulk-show.py -> SD-WAN devices
                                     -> <remote-dir>/<timestamp>/logs
                                     -> download -> ./logs/<timestamp>/
```

Log output:
- Remote logs are created at: <remote-dir>/<timestamp>/logs/output_<ip>_<YYYYmmdd_HHMMSS>.txt
- Downloaded logs are stored at: ./logs/<YYYYmmdd_HHMMSS>/output_<ip>_<YYYYmmdd_HHMMSS>.txt

Usage:
```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> [--password <password> | --key <key_path>] \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs [--verbose] [--quiet]
```

Example (password):
```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

Example (SSH key):
```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

Notes:
The script creates a timestamped subdirectory under --remote-dir for each run, uploads files there,
and writes logs to <remote-dir>/<timestamp>/logs.
Use --verbose for detailed remote output, or --quiet for minimal logs.

# bulk-show.py (direct)
Put the hosts file and command file in the same directory.

The hosts file contains: IP address (system-ip), username, password.

```bash
$ more hosts.txt
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

Example:
```bash
python3 bulk-show.py hosts.txt commands.txt
```

# Output logs
Logs are saved under ./logs with timestamps in the file name.
You can override the log directory with --logs-dir.

# Setup on a clean PC (no Python/venv)
1) Install Python 3 (3.10+ recommended).
2) Create a virtual environment:
```bash
python3 -m venv .venv
```
3) Activate the venv and install dependencies:
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
4) Run the scripts from the activated venv:
```bash
python bulk-show.py hosts.txt commands.txt
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> --remote-dir /home/<username> --download-outputs
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
Prepare hosts/commands:
```bash
cat > host.txt <<'EOF'
2.1.1.1,admin,admin
2.1.1.4,admin,admin
2.1.1.5,admin,admin
EOF

cat > command.txt <<'EOF'
show ip route
show omp route
show ip int bri
EOF
```

Run:
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
