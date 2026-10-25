# A-1 ローカル実行

人工CSVの検査にはPython標準ライブラリのみを使用する。入力CSVは読み取り専用で、レポートは毎回新しいパスへ出力する。`--csv` は `--workspace` を基準とした相対パスで指定する。

```powershell
py a1_quality.py --workspace tests/fixtures --csv synthetic_btc_1m.csv --output report-new.json --dataset-id synthetic-btc-1m-001 --instrument-id BTC_SYNTHETIC --source-kind SYNTHETIC --timeframe-seconds 60 --expected-start-utc 2026-10-01T12:00:00Z --expected-end-utc 2026-10-01T12:03:00Z
```

終了コードは品質VALIDが0、品質INVALIDかつレポート作成成功が1、処理障害が2。既存レポートへの上書きは拒否する。公開後に一時ファイルの削除が失敗した場合は、完成したレポートと成功終了コードを維持し、標準エラー出力に清掃警告を表示する。残った `.a1-*.tmp` は別名の一時ファイルである。入力とレポートはローカルで管理し、実市場データや秘密情報をGitに含めない。

```powershell
py -m unittest discover -s tests -v
```

`mock_b.receive` は同一呼び出し処理の `RunResult`、その実行で生成したレポートパス、許可済み作業ディレクトリ、期待設定、判断時刻を受け取る試験用関数。戻り値は利用可能な足開始時刻の一覧。A-1の全量品質判定には未来の行が含まれるため、過去時点の先読みのない品質判定には使えない。市場状態判断、取引、外部データ取得は実装していない。
