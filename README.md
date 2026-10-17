# AI・メイカーイベント管理

[English](README.en.md)

東京・近郊のAI開発、AI創作、電子工作、作品展示、制作者交流のイベントを収集し、参加予定を管理する個人用Webアプリです。
Python・Waitress・SQLiteで動き、毎日の処理にLLMは使いません。

## できること

- TECH PLAYの公式RSSとLumaのTokyo公式iCalから候補を収集。
- 日本語対応は明記された根拠で確認し、根拠がなければ要確認。
- 主催者の確認根拠を保存し、未確認の候補は通常一覧と分離。
- 勤務時間と移動時間から参加が難しい候補を通常一覧で除外。
- 土日祝だけの表示、複数日にわたる展示にも対応。
- 「興味なし」と「不参加」を別ボタン・別データで記録し、取消も可能。
- 参加状態、メモ、言語の本人確認を、自動収集と別に保存。
- 公式の日時変更・中止、本人の判断と変更履歴を永続保存。
- ブラウザの戻る・進む、URLによる絞り込みの復元。
- JSONエクスポートとSQLiteバックアップ。

申込み・キャンセルは公式サイトで行います。アプリの状態変更で外部の申込みは行いません。
不参加の保存値は既存データとの互換性のため「見送り」です。不参加から関心の有無を推定しません。

## ローカルで動かす

Python 3.12で検証しています。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python server.py serve --port 8798
```

`http://127.0.0.1:8798/`を開きます。ローカル開発モードはループバックからだけ接続できます。
初期状態に個人のイベントやメモは含まれません。収集後、主催者要確認の候補から確認してください。

```sh
.venv/bin/python server.py daily
```

日次処理は通常のプログラムです。定期実行は利用環境のスケジューラーでこのコマンドを実行します。
同時実行はロックで防ぎ、取得に失敗しても保存済みデータを消しません。

## 接続と設定

スマホから利用する場合はHTTPSと本人認証を設定してください。Cloudflare Tunnel＋Accessに対応します。
`access.example.json`を`data/access.json`へコピーして自分のホスト・チーム・Audience・許可メールを設定します。

```sh
.venv/bin/python server.py serve --port 8798 --access-config data/access.json
```

Tunnelを`http://127.0.0.1:8798`に接続し、専用ホストに本人限定のAccessポリシーを設定します。
アプリもJWTの署名・Issuer・Audience・期限・本人メールを検証し、ローカル接続でも省略しません。
Accessを設定せずにローカル開発モードをインターネットへ中継しないでください。

勤務時間の初期値は平日18時終業、移動45分、余裕15分です。イベント詳細で移動時間を変更できます。
祝日は内閣府の公式CSVを使い、未取得の年は未確認として扱います。サーバー停止中は利用できません。

## 取得元

- [TECH PLAY公式RSS](https://techplay.jp/information/236)
- [Luma公式iCal](https://help.luma.com/p/ical-syncing)
- [connpass API](https://help.connpass.com/api/)：審査・キーが必要。`EVENTS_CONNPASS_API_KEY`が未設定なら接続しません。
- [Doorkeeper API](https://www.doorkeeper.jp/developer/api)：任意。`EVENTS_DOORKEEPER_API_TOKEN`が未設定なら接続しません。
- Peatix：公式URLの手動登録。

掲載者名だけで主催者を承認せず、確認先と根拠を保存します。`organizer-evidence.json`の初期値は空です。
同名の別イベントに承認を引き継ぎません。APIや配信が提供しない締切・中止情報は要確認です。
スクレイピングや非公開APIの巡回は行いません。

## データと検証

データは`data/events.sqlite3`です。取得事実、本人状態、興味なし、主催者確認と変更履歴を分けます。
日次でローカルバックアップを14世代保持します。同じディスクのコピーなので、ディスク故障対策は別途必要です。
データ・設定・鍵はGitの管理対象に含めません。

```sh
.venv/bin/python -m unittest discover -s . -p 'test*.py'
.venv/bin/python server.py check
.venv/bin/python server.py export
```

Googleカレンダーへの書込みは未実装です。

## ライセンス

[MIT License](LICENSE)。依存パッケージと取得元サービスには、それぞれのライセンス・利用条件が適用されます。
