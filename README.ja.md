# sdwan-bulk-show
このサンプルは、複数のSD-WAN機器に対して複数のshowコマンドを実行して結果を取得します。

English: README.md
Japanese: README.ja.md

# 使い方
hostsファイルとcommandファイルを同じディレクトリに置きます。

hostsファイルには「IPアドレス(system-ip), ユーザ名, パスワード」を記載します。

```bash
$ more hosts.txt
2.1.1.1,admin,admin
3.1.1.1,admin,admin
4.1.1.1,admin,admin
```

commandファイルには実行したいshowコマンドを記載します。

commandファイルの例:
```bash
show version
show ip int bri
show ip route
show sdwan control connections
```

コマンド例:
```bash
python3 bulk-show.py hosts.txt commands.txt
```

# 出力ログ
ログは ./logs にタイムスタンプ付きで保存されます。
--logs-dir で保存先を指定できます。

# クリーンなPCでのセットアップ (Python/venvなし)
1) Python 3 をインストールします (推奨: 3.10以降)。
2) 仮想環境を作成します。
```bash
python3 -m venv .venv
```
3) venvを有効化して依存関係を入れます。
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
4) venvを有効化したまま実行します。
```bash
python bulk-show.py hosts.txt commands.txt
python run_on_vmanage.py <vmanage_ip> --user <user> --password <pass> --remote-dir <remote_dir> --download-outputs
```

# run_on_vmanage.py
bulk-show.py と入力ファイルをvManageにアップロードしてリモート実行し、
必要に応じて output_*.txt をPC側へダウンロードします。

動作フロー:
1) ローカルPCからvManageへSSH接続します。
2) --remote-dir 配下にタイムスタンプ付きの作業ディレクトリを作成します。
3) bulk-show.py、hostsファイル、commandsファイルをアップロードします。
4) vshellに入り、vManage上で bulk-show.py を実行します。
5) bulk-show.py が hosts に記載された各機器へ接続し、command の各コマンドを実行してログに書き込みます。
6) output_*.txt を ./logs/<timestamp>/ にダウンロードします。

フロー図:
```
ローカルPC -> SSH -> vManage -> vshell -> bulk-show.py -> SD-WAN機器
                                         -> <remote-dir>/<timestamp>/logs
                                         -> ダウンロード -> ./logs/<timestamp>/
```

ログ出力:
- リモート側: <remote-dir>/<timestamp>/logs/output_<ip>_<YYYYmmdd_HHMMSS>.txt
- ローカル側: ./logs/<YYYYmmdd_HHMMSS>/output_<ip>_<YYYYmmdd_HHMMSS>.txt

使用方法:
```bash
python3 run_on_vmanage.py <vmanage_ip> --user <user> [--password <pass> | --key <key_path>] \
  --remote-dir <remote_dir> --hosts <hosts_file> --commands <commands_file> [--download-outputs] [--verbose] [--quiet]
```

例 (パスワード):
```bash
python3 run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin \
  --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs --verbose
```

例 (SSH鍵):
```bash
python3 run_on_vmanage.py 10.71.131.72 --user sdwan --key ~/.ssh/id_rsa \
  --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs --verbose
```

注:
実行ごとに --remote-dir 配下にタイムスタンプのサブディレクトリを作成し、
そこにアップロードと実行を行います。ログは <remote-dir>/<timestamp>/logs に作成されます。
--verbose は詳細ログ、--quiet は最小限のログを表示します。

# フルコマンドサンプル
## ローカル実行 (bulk-show.py)
hosts/commandsの作成:
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

実行:
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

出力:
```
./logs/output_<ip>_<YYYYmmdd_HHMMSS>.txt
```

## vManageラッパー (run_on_vmanage.py)
パスワード認証:
```bash
python3 run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin \
  --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell):
```powershell
python run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin `
  --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell, 1行):
```powershell
python run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs
```

Windows (cmd):
```bat
python run_on_vmanage.py 10.71.131.72 --user sdwan --password sdwanadmin --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs
```

SSH鍵認証:
```bash
python3 run_on_vmanage.py 10.71.131.72 --user sdwan --key ~/.ssh/id_rsa \
  --remote-dir /home/sdwan --hosts host.txt --commands command.txt --download-outputs
```

ダウンロード先:
```
./logs/<YYYYmmdd_HHMMSS>/output_<ip>_<YYYYmmdd_HHMMSS>.txt
```
