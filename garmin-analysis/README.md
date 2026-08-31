# Garmin デイリーレポート

Garmin Connect のデータ(睡眠・安静時心拍・ボディバッテリー・ストレス・歩数・体重・アクティビティ)を
自動で取得し、1枚のHTMLレポートにまとめるスクリプトです。

内部的には [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) と同じ
`garminconnect` / `garth` ライブラリを使って Garmin Connect にログインします
(garmin_mcp 自体はMCPサーバーですが、このスクリプトは同じ土台をシンプルなCLIとして
直接使っています)。

## セットアップ

```bash
cd garmin-analysis
pip install -r requirements.txt
```

### 認証情報

環境変数でGarmin Connectのログイン情報を渡します。**パスワードをコードやリポジトリに
書かないでください。**

| 変数 | 説明 |
|---|---|
| `GARMIN_EMAIL` | Garmin Connectのログインメールアドレス |
| `GARMIN_PASSWORD` | Garmin Connectのパスワード |
| `GARMIN_TOKENSTORE` | (任意) ログインセッションのキャッシュ先。省略時は `~/.garmin_tokens` |
| `GARMIN_MFA_CODE` | (任意) 2段階認証が有効な場合の6桁コード。対話実行時は未設定でも入力を求められます |

Claude Code on the web でこのリポジトリの環境を使っている場合は、
**環境設定 (Environments) 画面の「環境変数」**に `GARMIN_EMAIL` / `GARMIN_PASSWORD` を
追加してください。追加後は新しいセッション(コンテナ)から反映されます。

### 初回ログインと2段階認証について

- アカウントに2段階認証(MFA)が**設定されていない**場合、`GARMIN_EMAIL` /
  `GARMIN_PASSWORD` だけで毎回自動ログインできます。
- **設定されている**場合、自動実行中にMFAコードを入力することはできません。
  最初の1回はターミナルなどで対話的に実行してコードを入力し、ログインセッションを
  `GARMIN_TOKENSTORE`(既定 `~/.garmin_tokens`)にキャッシュしてください。
  以降はそのキャッシュが再利用され、MFAなしでログインできます(ただし、このリポジトリを
  クローンし直す/コンテナを作り直す実行環境では、このキャッシュはローカルディスクにしか
  残らないため、コンテナが作り直されるたびに再度MFAが必要になる場合があります)。

## 使い方

```bash
python3 garmin_report.py                     # 直近14日間のトレンド + 直近7日間のアクティビティ
python3 garmin_report.py --days 30            # トレンド期間を30日に
python3 garmin_report.py --activity-days 14   # アクティビティ一覧を14日分に
python3 garmin_report.py --out /tmp/report.html
```

実行すると:

- `reports/<YYYY-MM-DD>.html` … その日付のレポート(日次アーカイブ)
- `reports/latest.html` … 最新レポート(常に上書き)

が生成されます。`reports/` ディレクトリはレポート本体(個人の健康データ)を含むため
`.gitignore` でリポジトリへのコミット対象から除外しています。

## 取得しているデータ

- **睡眠**: 睡眠スコア、睡眠時間、深い睡眠/レム睡眠の内訳
- **心拍・ボディバッテリー・ストレス**: 安静時心拍、ボディバッテリーの充電量、平均ストレスレベル
- **歩数・体重**: 日次の歩数、体組成計で計測した体重(データがある日のみ)
- **アクティビティ**: 種目・距離・時間・ペース・平均心拍・消費カロリー

デバイスやアカウントの設定によっては取得できない項目もあり、その場合はレポート上に
「データなし」と表示されます(エラーにはなりません)。

## 自動化(毎日のレポート生成)

Claude Codeのセッションでこのスクリプトを毎日自動実行し、レポートをArtifactとして
公開・更新する運用を想定しています。Claudeに「毎日◯時にGarminレポートを生成して」と
伝えると、スケジュール実行(Routine)を設定できます。

自動化が正しく動くための前提:

1. 実行環境(Environment)に `GARMIN_EMAIL` / `GARMIN_PASSWORD` が設定されていること
2. Garmin Connectアカウントで2段階認証が無効になっているか、有効な場合は
   上記の手順で事前にトークンキャッシュを作成済みであること(実行コンテナが
   毎回作り直される構成では、2段階認証が有効なアカウントの完全自動化は
   保証できません)
