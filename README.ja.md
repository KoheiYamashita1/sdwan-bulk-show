# sdwan-bulk-show

このリポジトリは、vManage上で bulk-show.py を実行して複数のSD-WAN機器のログを収集するラッパーを提供します。

English: README.md
Japanese: README.ja.md

# run_on_vmanage.py (推奨)

bulk-show.py と入力ファイルをvManageにアップロードしてリモート実行し、
必要に応じて output_*.txt をPC側へダウンロードします。

動作フロー:

1. ローカルPCからvManageへSSH接続します。
2. --remote-dir 配下にタイムスタンプ付きの作業ディレクトリを作成します。
3. bulk-show.py、hostsファイル、commandsファイルをアップロードします。
4. vshellに入り、vManage上で bulk-show.py を実行します。
5. bulk-show.py が hosts に記載された各機器へ接続し、command の各コマンドを実行してログに書き込みます。
6. output_*.txt を ./logs/<timestamp>/ にダウンロードします。

フロー図:

```
ローカルPC -> SSH -> vManage -> vshell -> bulk-show.py -> SD-WAN機器
                                         -> <remote-dir>/<timestamp>/logs
                                         -> ダウンロード -> ./logs/<timestamp>/
```

ログ出力:

- リモート側: //logs/output__.txt
- ローカル側: ./logs//output__.txt

使用方法:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> [--password <password> | --key <key_path>] \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs \
  [--reject-unknown-hosts] [--verbose] [--quiet]
```

例 (パスワード, プロンプト):

```bash
# --password を省略すると対話的にプロンプト入力（推奨。シェル履歴に残らない）。
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

例 (パスワード, インライン):

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

例 (SSH鍵):

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs --verbose
```

例 (ホストキー厳格チェック):

```bash
# vManage のホストキーを ~/.ssh/known_hosts に登録した後、検証を強制します。
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs \
  --reject-unknown-hosts
```

注:

- 実行ごとに --remote-dir 配下にタイムスタンプのサブディレクトリを作成し、
そこにアップロードと実行を行います。ログは //logs に作成されます。
- --verbose は詳細ログ、--quiet は最小限のログを表示します。
- 既定では未知のSSHホストキーを自動受け入れし、警告を stderr に出力します（中間者攻撃のリスクあり）。
本番環境では、初回接続後（またはホストキーを事前登録した上で）--reject-unknown-hosts を付与して
厳格な検証を有効にしてください。

# bulk-show.py (直接実行)

hostsファイルとcommandファイルを同じディレクトリに置きます。

hostsファイルは2種類のフォーマットをサポートします。`#` で始まる行と空行は無視されます。

2列形式（推奨）— 起動時に共通パスワードを1回プロンプト入力し、全ホストに使い回します。

```bash
$ more host.txt
# ip,username
2.1.1.1,admin
3.1.1.1,admin
4.1.1.1,admin
```

3列形式（旧式）— ホストごとにパスワードをファイル内に保存します。共有・公開リポジトリでは避けてください。

```bash
$ more host.txt
# ip,username,password
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

実行例:

```bash
# 2列形式: 起動時に共通パスワードを1回プロンプト入力します。
python3 bulk-show.py host.txt command.txt

# 全ホストに対して共通パスワードを強制（ファイルに記載されたパスワードを上書き）。
python3 bulk-show.py host.txt command.txt --password-prompt

# SSHホストキーの厳格チェック（事前に ~/.ssh/known_hosts へ登録しておくこと）。
python3 bulk-show.py host.txt command.txt --reject-unknown-hosts
```

## CLI オプション一覧

| オプション | 既定値 | 用途 |
| --- | --- | --- |
| `--port PORT` | `830` | 全ホストへ接続する SSH TCP ポート。SD-WAN edges (cEdge / IOS-XE SD-WAN) は vManage `vshell` が使う対話 SSH サービスを **22 ではなく 830** で公開しています。non-SD-WAN 機器を叩くときだけ 22 等に上書きしてください。 |
| `--reject-unknown-hosts` | 無効（自動受け入れ + WARN） | `~/.ssh/known_hosts` に未登録のホスト鍵を拒否します（MITM 対策）。 |
| `--password-prompt` | 無効 | 起動時に共通パスワードを 1 回プロンプト入力し、ファイル内の埋め込みパスワードを上書きします。 |
| `--logs-dir LOGS_DIR` | `logs` | 出力先ディレクトリを指定します。 |
| `--max-workers N` | `min(8, ホスト数)` | 同時に張る SSH セッション数の上限。大きくするとファンアウトが速くなり、小さくすると相手側負荷を抑えられます。 |
| `--retries N` | `0` | SSH 接続フェーズの追加リトライ回数。一過性のネットワーク／SSH 失敗のみが対象で、認証失敗は決してリトライしません。 |
| `--retry-delay SECS` | `5.0` | リトライ間のスリープ秒数。 |
| `--output-format LIST` | `text` | カンマ区切りで `text,json,csv` を組み合わせ可能。指定した形式ごとにホスト単位のファイルが追加生成されます。 |

## SD-WAN 認証に関する注意

`bulk-show.py` を vManage の `vshell` から起動する（推奨経路：`run_on_vmanage.py`）場合、
各 SD-WAN edge への接続は **TCP/830** を使い、デバイス側でパスワードを **2 回**
要求する仕様になっています:

1. **SSH トランスポート層** — hosts ファイルまたは `--password-prompt` で渡したパスワードを、
   SSH ハンドシェイクの一部として送信します。
2. **デバイス内サブシェル** — 接続が確立したあと、スクリプトは `shell` コマンドでデバイス内
   シェルに入ります。このときデバイスが `Password:` を再度要求することがあり、
   `bulk-show.py` は `PASSWORD_PROMPT_RE` でこれを検出し、**同じパスワードを自動で再送**
   します。

2 回目のプロンプトでパスワードが拒否された場合、セッションは `auth_error_shell`
ステータスで終了し、ログに明示的なメッセージが残ります。**両方のプロンプトで
同じパスワードを使ってください**。現状、トランスポート層とシェル層で異なる
資格情報を使い分ける機能はありません。

実行例（新オプション）:

```bash
# 既定の SSH ポート (830, SD-WAN 用) と並列度を抑えた実行
python3 bulk-show.py host.txt command.txt --max-workers 4

# SSH ポートを上書き（SD-WAN 以外の機器に対し、慣例の 22 を使う場合のみ）
python3 bulk-show.py host.txt command.txt --port 22

# 一過性失敗を 3 回まで 10 秒間隔でリトライ（auth failure はリトライ対象外）
python3 bulk-show.py host.txt command.txt --retries 3 --retry-delay 10

# テキスト + JSON + CSV を同時出力（後段の解析しやすい構造化データ）
python3 bulk-show.py host.txt command.txt --output-format text,json,csv
```

# 出力ログ

ログは ./logs にタイムスタンプ付きで保存されます。
`--logs-dir` で保存先を、`--output-format` で形式を指定できます。

各セッションは下記のような境界マーカーをテキスト出力に追加します（議題9）:

```
=== SESSION BEGIN host=2.1.1.1 port=830 ts=2026-05-02T01:23:45+09:00 ===
--- COMMAND BEGIN cmd="show version" ts=... ---
... (コマンド出力) ...
--- COMMAND END   cmd="show version" status=ok duration=1.23s ts=... ---
=== SESSION END   host=2.1.1.1 status=success duration=4.56s ts=... ===
```

`--output-format json` を指定すると `output_<ip>_<ts>.json` が生成され、ホスト・ポート・各コマンドの開始終了時刻・ステータス・全出力を含む構造化データが得られます。
`--output-format csv` を指定すると `output_<ip>_<ts>.csv` が生成され、ホスト名・コマンド・ステータス・所要時間・出力が 1 行 1 コマンドで表形式に整形されます（複数行出力は CSV クォートされます）。

# セキュリティに関する推奨

- 2列形式の `host.txt` を使い、`getpass` プロンプトで共通パスワードを入力する方式を推奨します。
ファイルやシェル履歴に資格情報が残りません。
- ファイル内の埋め込みパスワードを上書きしたい場合は `--password-prompt` で1回だけ対話入力できます。
- 対象ホストのキーを `~/.ssh/known_hosts` に登録した後は、両スクリプトに `--reject-unknown-hosts`
を付与してください。既定の `AutoAddPolicy` モードでは初回接続時に中間者攻撃のリスクがあるため、
`[WARN]` を stderr に出力するようにしています。
- `run_on_vmanage.py` ではパスワードよりも SSH 鍵認証 (`--key`) を優先してください。`--password` を
コマンドラインで渡すとシェル履歴やプロセス一覧に残るため、省略して対話プロンプトに任せる方が安全です。
- 本物のパスワードを含む `host.txt` を公開リポジトリへコミットしないでください。
`PUBLIC_CHECKLIST.md` も参照してください。

# クリーンなPCでのセットアップ (Python/venvなし)

1. Python 3 をインストールします (推奨: 3.10以降)。
2. 仮想環境を作成します。

```bash
python3 -m venv .venv
```

1. venvを有効化して依存関係を入れます。
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

1. venvを有効化したまま実行します。

```bash
python bulk-show.py host.txt command.txt
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --remote-dir /home/<username> \
  --hosts host.txt --commands command.txt --download-outputs
```

# フルコマンドサンプル

## vManageラッパー (run_on_vmanage.py)

パスワード認証:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell):

```powershell
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> `
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (PowerShell, 1行):

```powershell
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

Windows (cmd):

```bat
python run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --password <password> --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

SSH鍵認証:

```bash
python3 run_on_vmanage.py <vManage FQDN/IPaddress> --user <username> --key ~/.ssh/id_rsa \
  --remote-dir /home/<username> --hosts host.txt --commands command.txt --download-outputs
```

## ローカル実行 (bulk-show.py)

hosts/commandsの作成（推奨: パスワードを省略し、起動時にプロンプト入力させる）:

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

実行（起動時に共通パスワードを1回プロンプト入力）:

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

