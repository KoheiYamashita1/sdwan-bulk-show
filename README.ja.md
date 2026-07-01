# sdwan-bulk-show

このリポジトリは、SD-WAN 機器に対して `bulk-show.py` を一括実行する3通りの
方法を提供します。

1. [`run_on_vmanage.py`](run_on_vmanage.py) — vManage に `bulk-show.py` を
   送り込み、`vshell` 内で実行してログを手元に回収する CLI ラッパー（推奨）。
2. [`webapp/`](webapp/) — `python -m webapp` で起動する FastAPI + Uvicorn 製の
   小さなローカル Web UI。`127.0.0.1` のブラウザから同じラッパーを呼び出します。
   詳細は [Web UI（ローカルブラウザ）](#web-uiローカルブラウザ) を参照してください。
3. [`bulk-show.py`](bulk-show.py) — SD-WAN エッジに直接 SSH 到達できる端末
   から動かす素のスクリプト。

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

# Web UI（ローカルブラウザ）

[`webapp/`](webapp/) には `run_on_vmanage.py` を CLI 越しではなくブラウザから
呼び出すための、FastAPI + Uvicorn 製の小さなアプリが入っています。
本 v1 は **オペレータ自身の Mac でローカルにのみ動かす単一ユーザ向け** であり、
サーバは `127.0.0.1` のみをバインドし、組み込み認証はありません。

## 起動方法

```bash
cd /path/to/sdwan-bulk-show
python3 -m venv .venv                            # まだ無い場合
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt        # fastapi / uvicorn[standard] / jinja2 / python-multipart が追加されます
python -m webapp                                  # http://127.0.0.1:8000 で起動
# ブラウザで http://127.0.0.1:8000/ を開いてください。
```

主なオプション（`python -m webapp --help` 参照）:

```bash
python -m webapp --port 8081                      # ローカルポート変更
python -m webapp --log-level warning              # uvicorn のログを抑制
python -m webapp --reload                          # uvicorn 自動リロード（開発用）
python -m webapp --host 0.0.0.0                   # 非推奨。下の「セキュリティ注意」参照
```

Web UI は単一プロセスのフォアグラウンド Uvicorn として動きます。停止は同じ
ターミナルで `Ctrl-C` を押してください。

## ルーティング

| メソッド | パス                              | 用途 |
| -------- | --------------------------------- | ---- |
| `GET`    | `/`                               | 実行フォーム。vManage host / SSH user / password / remote-dir / hosts テキスト / commands テキスト / オプション。 |
| `POST`   | `/run`                            | 入力検証 → tempdir に `host.txt` / `command.txt` を 0o600 で書き出し → `run_on_vmanage.py` を `stdin` 経由でパスワードを渡しながら起動 → `303 See Other` で `/runs/<timestamp>` へリダイレクト。 |
| `GET`    | `/runs`                           | `logs/<timestamp>/` をスキャンして実行履歴を新しい順に一覧表示。 |
| `GET`    | `/runs/<timestamp>`               | 1 ランのサマリ（vManage host, user, hosts/commands 数, returncode, ステータス, 所要時間）と `output_*.txt` / `manifest.json` / `run.log` の一覧。 |
| `GET`    | `/runs/<timestamp>/files/<name>` | 個別ログ表示。パストラバーサルとシンボリックリンクは厳格に拒否。 |
| `GET`    | `/healthz`                        | 動作確認。`{"status": "ok"}` を返します。 |

## CLI とのマッピング

Web UI は CLI を **置き換えるのではなくラップする** だけです。フォーム送信
1 回ごとに、概ね次と等価な subprocess が起動されます:

```bash
python3 run_on_vmanage.py <vmanage-host> \
  --user <user> --remote-dir <remote-dir> \
  --local-dir <tempdir> --hosts host.txt --commands command.txt \
  --download-outputs \
  [--verbose] [--reject-unknown-hosts]
```

裏側で [`webapp/runner.py`](webapp/runner.py) が以下を担います:

- パスワードはフォームから受け取った値を subprocess の `stdin` に流し込みます
  （TTY が無い場合 `getpass` は `stdin` から読むため成立）。**ディスクには
  書き出しません。**
- 標準出力 / 標準エラーをまとめて取得し、`form.password` 文字列を `***` に
  マスクしてから `logs/<timestamp>/run.log` に保存します。
- 取得した output と並べて `manifest.json` を生成します（実体は
  `webapp.runner._build_manifest` が組み立てています）:

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

  `status` フィールドは `success` (returncode 0)、`failed` (returncode 非 0)、
  `timeout` (`DEFAULT_RUN_TIMEOUT` 超過) のいずれかになります。

## 同時実行と上限

- v1 ではプロセス内 `threading.Lock` で実行をシリアライズします。実行中に
  もう一度 `POST /run` すると HTTP `409 Conflict` を返し、フォームに警告
  バナーを出して再描画します。
- hosts / commands テキストはそれぞれ **1 MiB** まで（`webapp.runner.MAX_INPUT_BYTES`）。
- 1 回の subprocess のタイムアウトは既定 **1800 秒**
  （`webapp.runner.DEFAULT_RUN_TIMEOUT`）。タイムアウト時は `timeout`
  ステータスで途中までの transcript を保存します。
- ファイルビューアは **5 MiB** まで表示（`webapp.storage.MAX_VIEW_BYTES`）。
  超過分は切り詰め、何バイト落としたかをバナー表示します。

## 画面構成（ASCII イメージ）

`GET /` — 実行フォーム:

```
+-------------------------------------------------------------+
| sdwan-bulk-show                              [Run] [履歴]   |
+-------------------------------------------------------------+
| vManage host: [vmanage.example.com                      ]    |
| SSH user:     [admin           ]  Password: [**********]     |
| Remote dir:   [/home/admin                              ]    |
|                                                              |
| Hosts (1 行 1 ホスト, IP[,user[,password]]):                 |
| +----------------------------------------------------------+ |
| | 2.1.1.1,admin                                            | |
| | 2.1.1.2,admin                                            | |
| +----------------------------------------------------------+ |
|                                                              |
| Commands (1 行 1 コマンド):                                  |
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

`GET /runs/<timestamp>` — 1 ラン詳細:

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

（実画面のスクリーンショットはブラウザで取得してください。レイアウトは
JavaScript なしで動く最小 HTML/CSS で構成しています。）

## セキュリティ注意 — Web UI を使う前に必読

- **既定はローカル限定。** バインドアドレスは `127.0.0.1:8000` です。
  `--host 0.0.0.0` は使わないでください。v1 は認証・レート制限・TLS の
  いずれも持ちません。Web UI をネットワークに晒すことは、シェルアクセスと
  vManage クレデンシャルを配るのとほぼ同じ意味になります。別ホストから
  触る必要がある場合は SSH トンネル
  （`ssh -L 8000:127.0.0.1:8000 your-mac`）か、認証を足したリバースプロキシ
  を前段に置いてください。バインド先がループバックでない時、ランナーは
  `WARNING` を出します。
- **パスワードはメモリ常駐 + ログマスク。** 受け取ったパスワードは
  subprocess の `stdin` に流し込むだけで、ディスクには書きません。
  `run.log` を保存する直前に `form.password` の出現箇所を `***` に
  置換しているので、`logs/<timestamp>/run.log` を開いて漏えいが無いことを
  必ず確認してください。
- **hosts / commands は private tempdir に置く。** 入力テキストは
  `tempfile.TemporaryDirectory()` 内に `0o600` で展開し、subprocess 終了と
  ともに削除されます。ダウンロードした outputs が `logs/<timestamp>/` に
  残るのは CLI と同じレイアウトです。
- **パストラバーサルは遮断。** `/runs/<timestamp>/files/<name>` は要求
  パスを `logs/<timestamp>/` 配下で resolve し、ディレクトリを抜ける
  パスやシンボリックリンクは拒否します。`/`, `\`, `..` を含むファイル名は
  `404` です。
- **実行はシリアル化。** 並行実行は許可しません。同時に `POST /run` する
  と 2 件目は `409 Conflict` でフォームに戻ります。
- **ブラウザのオートフィル。** モダンブラウザは vManage パスワードを保存
  する提案を出すことがあります。キーチェーンに残したくない場合は拒否して
  ください。
- **ログは自動削除しません。** `logs/` は実行ごとに増えます。容量が気に
  なる場合はタイムスタンプ付きディレクトリを手動で削除してください。
  Web UI 側からランを削除する操作はありません。

下の [セキュリティに関する推奨](#セキュリティに関する推奨) も併せて参照
してください。Web UI が内部で利用する CLI フラグの推奨設定を扱っています。

## 今後の拡張余地（v1 では未実装）

- subprocess 出力のライブストリーミング（`/runs/<ts>/stream` を SSE で配信）。
- 名前付きホストインベントリ（`inventories/<name>.txt` 保存。パスワードは
  保存しない選択肢を必ず残す）。
- 非同期ジョブキューによる並列実行。
- `WEBAPP_TOKEN` 環境変数を見て Bearer トークン認証を有効化。

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

## コントローラ (vBond / vSmart) を対象にする

既定では、すべてのホストを **エッジ** (cEdge / IOS-XE SD-WAN) として扱います。
エッジは TCP/830 で接続し、デバイス内 `shell` に入り、必要に応じて
パスワードを 2 回目に再要求されます。

**コントローラ** (vBond / vSmart) のログを取得する場合 — 通常は vManage を
踏み台 (Jump Server) として — ホストにデバイス種別を指定します。コントローラは
`--controller-port`（既定 **22**）で接続し、パスワードは **1 回だけ**、`shell`
には **入りません**。ページングは viptela CLI の `paginate false` で無効化します。

デバイス種別はキーワード（`controller` / `vsmart` / `vbond`）または、曖昧さを
避けるための `type=` トークンで指定できます。1 つの hosts ファイルにエッジと
コントローラを混在させることもできます:

```bash
$ more host.txt
# ip,username[,password][,type]   (type 省略時は "edge")
2.1.1.1,admin
3.1.1.1,admin,secret
10.0.0.5,admin,controller
10.0.0.6,admin,secret,vsmart
10.0.0.7,admin,type=controller
```

各行の意味:

| エントリ | 意味 |
| --- | --- |
| `2.1.1.1,admin` | エッジ、起動時にパスワードを1回プロンプト |
| `3.1.1.1,admin,secret` | エッジ、パスワード埋め込み（非推奨） |
| `10.0.0.5,admin,controller` | vBond/vSmart、起動時にプロンプト |
| `10.0.0.6,admin,secret,vsmart` | vBond/vSmart、パスワード埋め込み |
| `10.0.0.7,admin,type=controller` | 明示的な種別指定（`type=edge` も可） |

> ホスト行に**インラインの `#` コメントは使えません**。行頭（先頭の非空白文字）が
> `#` の行のみコメントとして扱われます。注釈はホスト行の外に書いてください。

注:

- `getpass` プロンプトや `--password-prompt` で入力した共通パスワードは、
  エッジとコントローラの両方に使い回されます。資格情報が一致していれば、
  混在した 1 回の実行で動作します。
- パスワードが種別キーワードと一致する稀なケース（例: パスワードが
  `controller`）では、曖昧さのない `type=` 形式、または
  `ip,user,password,type` の 4 列形式を使ってください。
- コントローラが慣例の TCP/22 を使わない場合のみ、`--controller-port` で
  ポートを上書きしてください。

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
| `--port PORT` | `830` | **エッジ** ホストへ接続する SSH TCP ポート。SD-WAN edges (cEdge / IOS-XE SD-WAN) は vManage `vshell` が使う対話 SSH サービスを **22 ではなく 830** で公開しています。non-SD-WAN 機器を叩くときだけ 22 等に上書きしてください。 |
| `--controller-port PORT` | `22` | **コントローラ** (vBond / vSmart) として指定したホストへ接続する SSH TCP ポート。vManage を踏み台にすると、コントローラは慣例の 22 で viptela CLI に直接入り、パスワードは 1 回のみ・`shell` 段階はありません。 |
| `--reject-unknown-hosts` | 無効（自動受け入れ + WARN） | `~/.ssh/known_hosts` に未登録のホスト鍵を拒否します（MITM 対策）。 |
| `--password-prompt` | 無効 | 起動時に共通パスワードを 1 回プロンプト入力し、ファイル内の埋め込みパスワードを上書きします。 |
| `--logs-dir LOGS_DIR` | `logs` | 出力先ディレクトリを指定します。 |
| `--max-workers N` | `min(8, ホスト数)` | 同時に張る SSH セッション数の上限。大きくするとファンアウトが速くなり、小さくすると相手側負荷を抑えられます。 |
| `--retries N` | `0` | SSH 接続フェーズの追加リトライ回数。一過性のネットワーク／SSH 失敗のみが対象で、認証失敗は決してリトライしません。 |
| `--retry-delay SECS` | `5.0` | リトライ間のスリープ秒数。 |
| `--output-format LIST` | `text` | カンマ区切りで `text,json,csv` を組み合わせ可能。指定した形式ごとにホスト単位のファイルが追加生成されます。 |

## SD-WAN 認証に関する注意

### エッジ（既定）

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

### コントローラ (vBond / vSmart)

コントローラとして指定したホスト（[コントローラを対象にする](#コントローラ-vbond--vsmart-を対象にする)参照）は
挙動が異なります。vManage を踏み台にすると、コントローラは **TCP/22** で接続され、
viptela CLI に直接入ります:

1. **SSH トランスポート層** — SSH ハンドシェイク時にパスワードを送信します。
   これが **唯一** のパスワード入力で、`shell` サブプロセスは無く、2 回目の
   `Password:` プロンプトも発生しません。
2. **ページング** — show コマンド実行前に、IOS-XE の `terminal length 0` ではなく
   viptela CLI の `paginate false` を送信します。

`vedge` もデバイス種別エイリアスとして使え、このコントローラプロファイル
（viptela CLI・`paginate false`・`shell` ステップ無し）で扱われます。

### ページングとプロンプトの扱い

- **ページング自動無効化。** エッジには `terminal length 0`、コントローラ / vEdge には
  `paginate false` を、コマンド実行前に自動投入します。
- **対話ページャのフォールバック。** 上記で無効化できない独自ページャを持つ場面が
  あります。代表例が IOS-XE の `config-transaction`（コンフィグ）モードで、
  `show configuration ...` の出力が `--More--` / `(END)` で停止します。
  `bulk-show.py` はこれらのプロンプトを検出し、「残りをページングせず一括表示」を
  指示して自動的に消化するため、全出力をクリーンに取得できます（保存ログからは
  ページャマーカーや `\r` による再描画ノイズを除去）。
- **遅延プロンプトの回復。** プロンプトの再描画が遅い場合は、改行を送って再確認する
  ため、遅れて出るプロンプトをタイムアウト誤判定せずに確定できます。

実行例（新オプション）:

```bash
# 既定の SSH ポート (830, SD-WAN 用) と並列度を抑えた実行
python3 bulk-show.py host.txt command.txt --max-workers 4

# SSH ポートを上書き（SD-WAN 以外の機器に対し、慣例の 22 を使う場合のみ）
python3 bulk-show.py host.txt command.txt --port 22

# エッジとコントローラを host.txt に混在（コントローラは種別トークンで指定）。
# コントローラは既定で TCP/22 を使用。必要ならポートを上書き。
python3 bulk-show.py host.txt command.txt --controller-port 22

# 一過性失敗を 3 回まで 10 秒間隔でリトライ（auth failure はリトライ対象外）
python3 bulk-show.py host.txt command.txt --retries 3 --retry-delay 10

# テキスト + JSON + CSV を同時出力（後段の解析しやすい構造化データ）
python3 bulk-show.py host.txt command.txt --output-format text,json,csv
```

# 出力ログ

ログは ./logs にタイムスタンプ付きで保存されます。
`--logs-dir` で保存先を、`--output-format` で形式を指定できます。

テキスト出力は連続したターミナルのトランスクリプトです。ホストのセッション全体は `session begin` / `session end` マーカーで囲みますが、コマンド自体は 1 本の SSH セッション上で連続的に実行され、コマンドごとの境界マーカーは付きません（プロンプトで手入力したのと同じ流れになります）。プロンプトに戻れなかったコマンド（アイドルタイムアウト等）には短い `!!` 注記を付けて失敗を隠しません。

```
===== session begin: 2.1.1.1 user=admin port=830 started=2026-05-02T01:23:45+09:00 =====
show version
... (コマンド出力) ...
2.1.1.1#show ip interface brief
... (コマンド出力) ...
2.1.1.1#
===== session end:   2.1.1.1 status=success ended=... duration=4.56s =====
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

