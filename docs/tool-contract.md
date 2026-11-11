# 検索と応答サイズの仕様

長い同期は `start_refresh` がすぐに job ID を返します。完了は `get_refresh_status` で確認します。

一覧の `fields` は後方互換のため `"full"` が既定です。必要な基本項目だけに
絞る場合は `fields="summary"` を明示してください。検索の `include_text` も
既定は `true`（全文）です。`include_text=false` を指定すると、文書結果は
原文snippetと `text_chars` を保ったまま全文 `text` を省略します。
`build_study_context` の引用本文はこの軽量化に置き換わりません。

公開するitemの `metadata.attachment_manifest` からは、内部ダウンロード用の
`object_name` を除去します。これは意図的な応答互換性変更で、`fields="full"` や
全文検索でも同キーは返しません。同キーを参照していたクライアントは、公開の
attachment IDと `read_document` 等を使うよう変更してください。DBに保存された
manifest、同期処理、教材ファイルは変更しません。この除去はmanifest内の辞書項目に
限定され、他のmetadataを再帰的に秘匿化するものではありません。

検索はNFKC whole-string正規化・casefold・空白正規化による部分文字列一致です。
複数語で全語一致があればそれのみを返し、無ければANY（一致語数→score）へ
フォールバックします。これは旧来の厳密ANDのみの検索からの意図的な意味変更です。
`tokens_unmatched` はlimit適用前の検索スコープ（授業・種類・公開状態等）内で
無一致の語です。他授業・未同期・非公開資料を含む資料不存在の証明ではありません。
重複語は一致語数には数えませんが、従来同様scoreには重複分を反映します。
多数語はSQL式サイズを制御してPython scanに切り替え、語を切り捨てません。

readonly接続は全経路で `query_only=1`、reader timeout 5秒を適用します。
未作成・旧schema・不正versionでは初期化用writerが必要ですが、callerへのyield前に
readerへ切り替えます。schema検査のロック・I/O・破損例外はmigrationへ逃がしません。
