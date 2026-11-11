# uriboMCP インストール手順（AI エージェント向け）

この文書は、ユーザーの Windows PC に uriboMCP をインストールする AI エージェント
（Claude Code、Codex など）が読むための手順書です。上から順に実行してください。
人が手で行う場合は [README の「手動でインストール」](../README.md#手動でインストール) を使います。

## 守ること

- 大学のログイン ID とパスワードを、チャットで尋ねない・受け取らない・コマンド引数や
  ファイルに書かない。入力は手順 3 のブラウザフォームでユーザー本人が行う。
- 既存の MCP クライアント設定は上書きしない。`uribo-mcp` の項目を追加するだけにする。
- コマンドが失敗したら推測で先へ進まず、出力をそのままユーザーに見せる。
- 対象は Windows 11（Windows 10 は未検証）。それ以外の OS なら、対象外であることを伝えて止まる。
- `uv` や `uribo-mcp` は進捗を標準エラーに出す。標準エラーへの出力だけで失敗と判断せず、
  終了コードで判断する。

## 0. 先にユーザーへ確認する

次の 4 点を 1 回でまとめて聞く。括弧内が既定値。

1. インストール先（`%USERPROFILE%\uriboMCP`）
2. データの保存先（`%USERPROFILE%\uriboMCP-data`）。授業資料のキャッシュが入るので容量に余裕のある場所
3. 登録する MCP クライアント（Claude Code / Claude Desktop / Codex から 1 つ以上）
4. 定期同期を Windows タスクスケジューラに登録するか（する）

どちらのフォルダも OneDrive などクラウド同期の配下は避ける（データベースが壊れることがある）。
以下、決まった値を `<INSTALL_DIR>` と `<DATA_DIR>` と書く。コマンドには `%USERPROFILE%` のままではなく、
`$env:USERPROFILE` を展開した絶対パスを入れる。

## 1. 前提ソフトを確認する

```powershell
git --version
uv --version
```

無いものだけ入れる。管理者権限の確認（UAC）が出たら、ユーザーに許可を頼む。

```powershell
winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements
winget install --id astral-sh.uv -e --accept-package-agreements --accept-source-agreements
```

入れた後は、同じセッションで PATH を読み直す。

```powershell
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
```

Google Chrome も必要（大学のログインに使う）。無ければ同じオプションで
`winget install --id Google.Chrome -e`。Python は `uv` が自動で用意するので不要。

## 2. 取得して依存を入れる

```powershell
git clone https://github.com/AkiraAsano-kobe-u/uriboMCP.git "<INSTALL_DIR>"
cd "<INSTALL_DIR>"
uv sync
```

以降のコマンドはすべて `<INSTALL_DIR>` で実行する。

## 3. 保存先と大学アカウントを設定する

コマンドを実行する前に、ユーザーへ「これからブラウザに設定フォームが開きます。保存先と
同期する情報を確認し、大学のログイン ID とパスワードを入力して『設定を保存』を押してください」
と伝える。それから実行する。

```powershell
uv run uribo-mcp setup --storage-dir "<DATA_DIR>"
```

- コマンドはフォームが保存されるまで待つ（最長 15 分）。エージェント側のコマンド
  タイムアウトが短い場合は、タイムアウトを 10 分に延ばすかバックグラウンドで実行する。
  待ち時間は `--timeout 秒` で変えられる。
- ブラウザが開かなければ、標準エラーに出る `Setup form: http://127.0.0.1:...` の URL を
  ユーザーに渡して開いてもらう。最初から URL だけを出すには `--no-browser` を付ける。
- 終了すると標準出力に JSON が出る。`"ok": true` と `"kobe_credentials_stored": true` を確認する。
  `"reason": "timeout"` なら同じコマンドをやり直す。
- JSON の `config_path` と `mcp_server` は手順 6 で使う。

フォームを使えない環境では、保存先だけを設定し、パスワードはユーザー自身に
別のターミナルで入力してもらう。

```powershell
uv run uribo-mcp setup --no-ui --storage-dir "<DATA_DIR>"
# ユーザーが自分のターミナルで実行する（エージェントは実行しない）
uv run uribo-mcp auth setup
```

保存先を個別に分けたい場合は `--data-dir` `--cache-dir` `--export-dir`
`--credentials-dir` を、同期する情報を指定したい場合は `--sources` を付ける
（`uv run uribo-mcp setup --help`）。設定済みの環境でやり直すときは `--force`。

## 4. 環境を確認する

```powershell
uv run uribo-mcp doctor
```

`"ok": true` を確認する。`tesseract` と `pdftoppm` は任意（スキャン PDF と画像の OCR 用）
なので `false` でもよい。`chrome` が `false` なら Google Chrome を入れる。

## 5. 最初の同期をする

まず、ログインの要らない情報で動作を確かめる（数秒〜数十秒）。

```powershell
uv run uribo-mcp refresh --scope csi --wait
```

`csi` をフォームで外した場合は、`newsnet` など有効にした情報の ID に置き換える。
`"status": "completed"` を確認したら、全体の同期をバックグラウンドで始める。

```powershell
uv run uribo-mcp refresh
```

すぐにジョブ ID が返り、同期は裏で続く。30 秒ほど待って
`<DATA_DIR>\data\logs\<ジョブID>.log` に追記が続いていることを確かめる。止まっていたら、
ユーザー自身のターミナルで `uv run uribo-mcp refresh --wait` を実行してもらう。初回は授業資料をすべて取得するため
数十分かかることがある。完了を待たずに次へ進んでよい。進み具合は
`<DATA_DIR>\data\logs\<ジョブID>.log`、または登録後の MCP ツール
`get_refresh_status` と `get_source_health` で確認できる。

## 6. MCP クライアントに登録する

手順 3 の JSON にある `config_path` を `<CONFIG>` として使う。先に `claude mcp get uribo-mcp` や
`codex mcp list` で確かめ、`uribo-mcp` が登録済みなら変更せずにユーザーへ報告し、
置き換えるかどうかを尋ねる。

Claude Code:

```powershell
claude mcp add --transport stdio uribo-mcp --scope user --env "URIBO_MCP_CONFIG=<CONFIG>" -- uv --directory "<INSTALL_DIR>" run uribo-mcp serve
claude mcp get uribo-mcp
```

Codex:

```powershell
codex mcp add uribo-mcp --env "URIBO_MCP_CONFIG=<CONFIG>" -- uv --directory "<INSTALL_DIR>" run uribo-mcp serve
codex mcp list
```

Claude Desktop: `%APPDATA%\Claude\claude_desktop_config.json` の `mcpServers` に、
手順 3 の JSON の `mcp_server` の値を `"uribo-mcp"` というキーで追加する。
ファイルが無ければ `{"mcpServers": {"uribo-mcp": ...}}` で新しく作る。既存の項目は残す。
ファイルの場所は Claude Desktop の Settings > Developer > Edit Config でも確かめられる。
PowerShell で編集する場合は `ConvertTo-Json -Depth 10` を使い、BOM なしの UTF-8 で書く
（`[IO.File]::WriteAllText($path, $json, (New-Object Text.UTF8Encoding $false))`）。
保存後、Claude Desktop の再起動をユーザーに頼む。

## 7. 定期同期を登録する（ユーザーが希望した場合）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-ScheduledSync.ps1
```

全情報を毎日 7:00、BEEF+ を 8:00〜23:00 の 3 時間ごとに、ウィンドウを出さずに同期する
タスク `UriboMCP-AllSync` と `UriboMCP-BeefSync` が登録される。出力の `"Installed": true` を確認する。

## 8. ユーザーに報告する

- インストール先、データの保存先、登録したクライアント
- 初回同期が裏で進んでいること
- 新しいセッションで「今日の時間割は？」「締切の近い課題は？」のように聞けば使えること
- 授業資料は担当教員の許可なく共有・公開しないこと

## 更新とアンインストール

更新:

```powershell
cd "<INSTALL_DIR>"
git pull
uv sync
```

アンインストール:

```powershell
Unregister-ScheduledTask -TaskName UriboMCP-AllSync, UriboMCP-BeefSync -Confirm:$false
claude mcp remove uribo-mcp --scope user
```

その後 `<INSTALL_DIR>` と `<DATA_DIR>` を削除し、Windows の「資格情報マネージャー」
（Windows 資格情報）で `uribo-mcp` を含む項目を削除する。
