# uriboMCP

神戸大学システム情報学部の学生向けの、ローカルで動く MCP サーバーです。
BEEF+ の授業資料・課題・お知らせ、うりぼーネットの掲示、学部サイトのお知らせなどを
自分の PC に同期し、Claude Code・Claude Desktop・Codex から
「今日の時間割は？」「締切の近い課題は？」「第 3 回の資料の要点は？」のように聞けるようにします。

- 対象: 神戸大学システム情報学部の学生（Windows 11）
- 大学非公式の個人開発ツールです。神戸大学とは関係がありません
- 同期したデータは自分の PC にだけ保存します

## インストール

### AI に任せる（おすすめ）

Claude Code や Codex など、PC 上でコマンドを実行できる AI エージェントに次の文を貼ります。

```text
https://github.com/AkiraAsano-kobe-u/uriboMCP の docs/install-for-agents.md を読んで、
その手順どおりに uriboMCP をこの PC にインストールして。
```

AI がインストール先とデータの保存先を尋ね、途中でブラウザに設定フォームを開きます。
大学のログイン ID とパスワードはそのフォームに自分で入力します（AI には渡りません）。

### 手動でインストール

必要なもの: [Git](https://git-scm.com/)、[uv](https://docs.astral.sh/uv/)、Google Chrome。
Python は uv が自動で用意します。

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e

git clone https://github.com/AkiraAsano-kobe-u/uriboMCP.git "$env:USERPROFILE\uriboMCP"
cd "$env:USERPROFILE\uriboMCP"
uv sync

uv run uribo-mcp setup            # 保存先・同期する情報・大学アカウントを設定
uv run uribo-mcp doctor           # 環境の確認
uv run uribo-mcp refresh --wait   # 最初の同期（初回は数十分かかることがあります）
```

続けて [MCP クライアントへ登録](#mcp-クライアントへ登録) します。

## セットアップ

`uv run uribo-mcp setup` はブラウザに設定フォームを開きます。

- データの保存先と、教材の書き出し先
- 同期する情報
- 神戸大学アカウント（OS の資格情報ストアに保存）

保存先は OneDrive などクラウド同期の配下を避けてください。データベースが壊れることがあります。
設定は `config.local.toml` に書かれ、同じコマンドでいつでも変更できます。

フォームを使わずに指定することもできます。

```powershell
uv run uribo-mcp setup --no-ui --storage-dir "D:\uriboMCP-data" --sources beef,uribo,csi
uv run uribo-mcp auth setup       # 大学アカウントをターミナルで入力
```

| オプション | 内容 | 既定 |
| --- | --- | --- |
| `--storage-dir` | すべてのデータの基準フォルダ | `%USERPROFILE%\uriboMCP-data` |
| `--data-dir` | データベースとログ | `<storage>\data` |
| `--cache-dir` | ダウンロードした教材 | `<data>\cache` |
| `--export-dir` | 教材の書き出し先 | `<storage>\exports` |
| `--credentials-dir` | ログイン Cookie と OAuth ファイル | `<storage>\credentials` |
| `--sources` | 同期する情報（カンマ区切り） | 下表の「有効」 |
| `--force` | 既存の設定を置き換える（元のファイルは `.bak` に残る） | |

同期できる情報:

| ID | 内容 | 既定 |
| --- | --- | --- |
| `beef` | BEEF+ の授業資料・課題・お知らせ・時間割 | 常に有効 |
| `uribo` | うりぼーネットの掲示 | 有効 |
| `csi` | システム情報学部サイトの在学生向けお知らせ | 有効 |
| `pdf_calendar` | 学年暦 | 有効 |
| `newsnet` | 神戸大学ニュースネット | 有効 |
| `venture` | BEEF+ Venture（特定の 1 コースのみ対応） | 無効 |
| `moodle` | LMS BEEF（Moodle）の教材・課題・お知らせ | 有効 |
| `mail` | 大学メール | 無効 |

大学メールを同期するには、Google Cloud で自分用の Desktop OAuth クライアントを作り、
`<credentials>\google_client_secret.json` に置いてから `uv run uribo-mcp auth google` を一度実行します。

OCR は任意です。画像やスキャン PDF を読むには Tesseract（日本語データ）、PDF の OCR には
加えて Poppler の `pdftoppm` を PATH に置きます。無くても文字入り PDF と Office 文書は読めます。

## MCP クライアントへ登録

`uv run uribo-mcp setup` の出力にある `config_path` を使います。

```powershell
# Claude Code
claude mcp add --transport stdio uribo-mcp --scope user `
  --env "URIBO_MCP_CONFIG=C:\Users\you\uriboMCP\config.local.toml" -- `
  uv --directory "C:\Users\you\uriboMCP" run uribo-mcp serve

# Codex
codex mcp add uribo-mcp `
  --env "URIBO_MCP_CONFIG=C:\Users\you\uriboMCP\config.local.toml" -- `
  uv --directory "C:\Users\you\uriboMCP" run uribo-mcp serve
```

Claude Desktop と MCPB バンドルの手順は [docs/client-setup.md](docs/client-setup.md) にあります。

## 定期同期

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-ScheduledSync.ps1
```

Windows タスクスケジューラに、全情報を毎日 7:00、BEEF+ を 8:00〜23:00 の 3 時間ごとに
同期するタスク（`UriboMCP-AllSync`、`UriboMCP-BeefSync`）を登録します。
ウィンドウは表示されず、入力フォーカスも奪いません。Windows にログオンしている間だけ動きます。
変更のない教材は再取得しません。ログは `<data>\logs` に残ります。

## 安全のための設計

- 自分のアカウントだけを対象にし、読み取りしかしません。課題の提出、テスト・アンケートの
  開始や回答、出席登録、フォーラムへの投稿は行いません（一覧上の状態と期限だけを記録します）
- 大学のログイン ID とパスワードは OS の資格情報ストアに、ログイン Cookie は Windows の DPAPI で
  暗号化して保存します。設定ファイルや MCP クライアントの設定には書きません
- 大学のサーバーへのリクエストは既定で 3 秒の間隔を空け、ログインの試行は Cookie が切れたときの 1 回だけです
- 教材ページを開くため、BEEF+ 側に閲覧記録が残ります。「閲覧で完了」の活動は完了扱いになることがあります

## 利用上の注意

- 神戸大学の情報セキュリティポリシーと各システムの利用規則に従って、自己責任で使ってください
- 授業資料の著作権は担当教員などにあります。同期した資料を許可なく共有・公開しないでください
- AI クライアントに資料を読ませると、その内容は利用している AI サービスに送られます。
  各サービスのデータの扱いを確認してください
- 大学側のページ構成が変わると同期できなくなることがあります。`get_source_health` と
  `<data>\logs` で状態を確認できます

## MCP ツール

時間割・締切・お知らせ:
`get_today` `get_week` `get_deadlines` `get_unread` `get_news` `get_notes` `get_item`
`get_course` `search_items` `mark_done` `get_source_health`

授業と教材:
`list_courses` `list_course_content` `get_course_content` `search_course_content`
`read_document` `build_study_context` `export_materials` `get_changes`
`get_notifications` `acknowledge_notifications` `start_refresh` `get_refresh_status`

検索と応答サイズの仕様は [docs/tool-contract.md](docs/tool-contract.md) にあります。

## LMS BEEF（Moodle）の同期

BEEF+ とは別の Moodle サイト `https://beef.center.kobe-u.ac.jp/<年度>/` の登録コースから、
教材・課題・お知らせ・締切を取り込めます。既定で有効です（不要ならセットアップで `moodle` を外します）。

- 読み取り専用です。小テスト・アンケート・出席はページを開かず、名前と期限だけを記録します
- 動画・音声ファイルは名前だけを記録し、ダウンロードしません
- 同じ年度・時間割コードの BEEF+ コースが 1 件だけあれば、その授業にまとめて保存します
- 提出状況は記録するだけで、完了チェックは変更しません
- 保存済み Cookie を優先し、切れていれば KNOSSOS に 1 回だけログインします
- フォーラム（お知らせ）の読み取りは、投稿のある実ページでは未検証です

## 開発

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
```

不具合の報告や改善の提案は Issue へどうぞ。脆弱性は [SECURITY.md](SECURITY.md) の方法で報告してください。`csi` 以外は学部に依存しない作りですが、
システム情報学部以外では未検証です。

## ライセンス

[MIT License](LICENSE)
