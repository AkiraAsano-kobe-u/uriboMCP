---
name: uribo-mcp
description: "Use when asked about 神戸大の授業・課題・締切・教材. Query uribo-mcp."
license: MIT
---

# uribo-mcp の使い方

uribo-mcp は、神戸大学の BEEF+・LMS BEEF（Moodle）・うりぼーネット・学部サイトを
ユーザーの PC に同期したローカルのミラーです。この MCP サーバーのツールで、時間割・締切・
お知らせ・授業資料の中身に答えます。

ツール名の接頭辞はクライアントごとに違います（例: `mcp__uribo-mcp__get_today`、
`mcp__uribo_mcp__get_today`）。以下は接頭辞を除いた名前です。ツールが見つからなければ、
uribo-mcp が未登録です。https://github.com/AkiraAsano-kobe-u/uriboMCP の
`docs/install-for-agents.md` を案内します。

## どのツールを使うか

| 聞かれたこと | ツール |
| --- | --- |
| 今日の授業・今日やること | `get_today` |
| 今週の時間割 | `get_week` |
| 締切（既定は今日から 14 日） | `get_deadlines(days_ahead=…)` |
| 新しいお知らせ・未読 | `get_unread`、`get_notifications`、`get_changes(since=…)` |
| ある授業の状況（要対応・お知らせ） | `get_course(course_name=…)` |
| 資料のどこに何が書いてあるか | `search_course_content(query=…, course=…)` |
| 資料の本文を読む | `read_document(attachment_id=…, page_from=…, page_to=…)` |
| ある範囲を根拠つきでまとめる | `build_study_context(course=…, query=…)` |
| 授業の資料・課題の一覧 | `list_course_content(course=…, fields="summary")` |
| 授業以外も含めた横断検索 | `search_items(query=…)` |
| データが古い・同期できているか | `get_source_health`、`get_refresh_status` |

## 資料を調べる手順

1. 授業名が分かっていれば `list_courses` は呼ばず、`course` に授業名（部分一致）を直接渡す。
   名前が分からないときだけ `list_courses(fields="summary")` を使う。
2. `search_course_content` を `include_text=false` で呼び、`attachment_id`・`file_name`・
   `page_start`／`page_end`・`snippet` から当たりをつける。
3. 必要なページだけを `read_document` で読む。資料を丸ごと読まない。
4. 答えには授業名・ファイル名・ページを添える。

検索のこつ:

- 見出しになる名詞を短く使う（「テイラーの定理」より「テイラー」）。助詞つきの語は一致しにくい。
- 複数語は全語一致が優先され、無ければ語ごとの一致に切り替わる。`match_mode` と
  `tokens_unmatched` で確かめる。
- 0 件でも「資料に無い」とは言い切らない。語を短くして引き直し、それでも無ければ
  「同期済みの資料には見つからない」と伝える。未同期や非公開の資料は検索できない。

## 抽出テキストの限界

`read_document` と検索結果の `text` は PDF から機械抽出した文字列です。

- 数式は壊れる。分数や指数が崩れ（`x^a` が `xa`）、記号が化ける（`(cid:0)` など）。
  数式を含む問題文や定義を、抽出テキストから逐語で引用しない。
- 正確な文面が必要なら原本を見る。原本は
  `<cache_dir>/attachments/<course_id>/<attachment_id>/<file_name>` にある
  （`cache_dir` は `config.local.toml` の値）。
- 文字の無いスキャン PDF や画像は、OCR を入れていない環境では本文が空になる。
- 動画・音声は名前だけで、中身は無い。

## 応答を軽くする

- 一覧は `fields="summary"`、検索は `include_text=false` を付け、`limit` を小さくする。
  既定のままだと 1 回で数千トークンを使う。
- 範囲の全体像には `build_study_context`、ページの特定だけなら `search_course_content`。

## 勝手にやらないこと

次のツールは状態を変えるので、ユーザーが頼んだときだけ使う。

| ツール | 起きること |
| --- | --- |
| `start_refresh` | 大学のサイトへアクセスして同期を始める |
| `mark_done` | 項目を完了／未完了にする（LMS への提出ではない） |
| `acknowledge_notifications` | 更新通知を確認済みにする |
| `export_materials` | 教材を書き出し先フォルダへコピーする |

データが古いと気づいたら、同期を始める前に `get_source_health` の結果を伝えて尋ねる。

uribo-mcp は読み取り専用です。課題の提出、テスト・アンケートの回答、出席登録、履修登録は
できません。提出状況や締切は同期時点のものなので、締切の間際には LMS 本体での確認を勧めます。

## 扱いの注意

- 授業資料の著作権は担当教員などにある。資料の全文を外部へ貼り出したり、共有用に
  まとめ直したりしない。ユーザー本人の学習のための要約・引用にとどめる。
- 大学のログイン ID・パスワードを尋ねない。認証が切れていたら、ユーザー自身に
  `uv run uribo-mcp setup` か `uv run uribo-mcp auth setup` を実行してもらう。
- 時間割・シラバス・LMS の内容が食い違うときは、どちらかを黙って採らず、両方を出典つきで示す。
