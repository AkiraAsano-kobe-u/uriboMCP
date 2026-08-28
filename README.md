# uriboMCP

神戸大学の個人向け情報をローカルに同期し、Claude Code、Claude Desktop、
Codex Desktopから同じstdio MCPサーバーとして参照する独立プロジェクトです。
旧 `KOBEDAI_dash` のデータや資格情報には実行時依存しません。

主な機能:

- BEEF+、BEEF+ Venture、システム情報学部、学年暦PDF、Newsnet、
  うりぼーネット、大学メールの取得ロジック
- 時間割、締切、お知らせ、教材、課題詳細のローカルSQLite保存
- PDF、DOCX、PPTX、XLSX、TXT、CSV、画像の本文抽出と横断検索
- Tesseract/Popplerがある環境でのスキャンPDF・画像OCR
- 新規／更新／非公開化の履歴とローカル通知
- ページ引用つき学習コンテキスト作成、教材の整理エクスポート
- 複数デスクトップクライアントで共有できるバックグラウンド同期ジョブ

## 安全境界

このサーバーは設定した本人のアカウントだけを対象とするローカルツールです。
テスト、アンケート、出席は一覧上の状態と期限だけを保存し、開始・回答・登録URLを
呼びません。課題は詳細ページをGETで読むだけで、提出操作を行いません。
認証試行はCookie復元失敗後の1回だけです。

大学IDとパスワードはOSの資格情報ストアに保存します。LMS CookieはWindowsでは
DPAPIで暗号化します。Google OAuthトークン、DB、教材キャッシュ、エクスポートは
すべてgit管理外です。

## セットアップ

Python 3.11以上と `uv` を前提にしています。

```powershell
uv sync --extra dev
Copy-Item config.example.toml config.local.toml
uv run uribo-mcp auth setup
uv run uribo-mcp doctor
```

大学メールも同期する場合は、Google Cloudで作成したDesktop OAuthクライアントを
`credentials/google_client_secret.json` に置き、対話端末で次を一度実行します。

```powershell
uv run uribo-mcp auth google
```

初回同期:

```powershell
uv run uribo-mcp refresh --wait
```

OCRは任意です。画像・スキャンPDFを読むにはTesseract（日本語言語データ）を、
PDF OCRには加えてPopplerの `pdftoppm` をPATH上に用意します。未導入でも、
文字レイヤー付きPDFとOffice文書の抽出は動作します。

## MCPクライアントへの登録

日本語や空白を含むこのプロジェクトの絶対パスを安全に登録するため、設定生成
スクリプトを利用してください。実際のユーザー設定は自動変更しません。

```powershell
pwsh -File .\scripts\New-ClientConfigs.ps1
```

Claude Code、Claude Desktop、Codex Desktopそれぞれの登録方法とMCPB作成方法は
[docs/client-setup.md](docs/client-setup.md) を参照してください。

## MCPツール

旧互換ツール:

`get_today`、`get_deadlines`、`search_items`、`mark_done`、`get_unread`、
`get_notes`、`get_news`、`get_item`、`get_week`、`get_course`、
`get_source_health`

授業・教材ツール:

`list_courses`、`list_course_content`、`get_course_content`、
`search_course_content`、`read_document`、`get_changes`、
`get_notifications`、`acknowledge_notifications`、`build_study_context`、
`export_materials`、`start_refresh`、`get_refresh_status`

長い同期は `start_refresh` が即座にjob IDを返します。
完了確認は `get_refresh_status` をポーリングしてください。

## Windowsでの定期同期

Windowsタスクスケジューラへ、全ソースを毎日07:00、BEEF+を
08:00から23:00まで3時間ごとに同期するタスクを登録できます。
`pythonw.exe` と非表示ワーカーを使用し、PDF変換・OCR・ChromeDriverにも
`CREATE_NO_WINDOW` と `SW_HIDE` を適用するため、実行時にコンソール
ウィンドウは表示されず、入力フォーカスも変更しません。

```powershell
pwsh -File .\scripts\Install-ScheduledSync.ps1
```

登録されるタスクは `UriboMCP-AllSync` と `UriboMCP-BeefSync` です。
重複した同期は開始されず、起動履歴は `data/logs/scheduler.log`、
各同期の詳細ログは `data/logs/<job-id>.log` に記録されます。
教材キャッシュはBEEF+の `objectName` とローカルファイルのサイズを確認し、
新規・更新・欠損時だけ再取得します。変更のない教材はダウンロード、本文抽出、
ファイル保存を省略するため、OneDrive上の全教材を毎回書き換えません。
同期結果の `attachments_reused` で再利用件数を確認できます。
パスワードを保存しない構成のため、タスクは現在のユーザーが
Windowsへログオン中にバックグラウンド実行されます。

## 旧データの移行

旧DBは読み取り専用で開き、データだけを新DBへコピーします。認証情報やCookieは
コピーしません。

```powershell
uv run uribo-mcp import-legacy --from "C:\path\to\KOBEDAI_dash\data\app.db"
```

## 開発時の検証

```powershell
uv run pytest
uv run ruff check .
uv build
```

実サイトのHTML構造やSSO状態は変わりうるため、初回実機同期後は
`get_source_health` と `data/logs/<job-id>.log` を確認してください。
