# Gmail Inbox Rules Automation

## 目的

このリポジトリは、Gmail の受信箱整理を毎日自動実行するための Python スクリプトと GitHub Actions ワークフローを提供します。

## ルール

1. 直近 `LOOKBACK_DAYS` 日の `INBOX` メールを走査する。
2. 送信元ドメインごとに `Sender/<domain>` ラベルを付与する。
3. セキュリティ、ログイン、権限、請求、決済、契約、障害、通信容量などのアカウント運用メールに `IMPORTANT` を付与する。
4. `CATEGORY_SOCIAL` または `CATEGORY_PROMOTIONS` に該当するメールはゴミ箱へ移動する。
5. `TRASH_NON_IMPORTANT=true` の場合、重要判定されなかった受信箱メールもゴミ箱へ移動する。

## GitHub Actions の実行時刻

`.github/workflows/gmail-inbox-rules.yml` は以下で設定しています。

```yaml
- cron: "0 22 * * *"
```

GitHub Actions の cron は UTC 基準です。日本時間の毎日 07:00 は UTC の前日 22:00 に相当します。

## Gmail OAuth Token の準備

Google Cloud Console で OAuth クライアントを作成し、Gmail API を有効化してください。必要スコープは以下です。

```text
https://www.googleapis.com/auth/gmail.modify
```

ローカル環境で `token.json` を作成した後、以下のように Base64 化して GitHub Secrets に登録します。

```bash
base64 -w 0 token.json
```

GitHub Secrets 名:

```text
GMAIL_TOKEN_JSON_B64
```

Windows PowerShell の例:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("token.json"))
```

## 初回運用

初回は `.github/workflows/gmail-inbox-rules.yml` の `DRY_RUN` を `"true"` にしてログを確認してください。問題がなければ `"false"` に変更します。
