\
#!/usr/bin/env python3
"""
Gmail Inbox Rules Automation

Rules:
1. Scan recent INBOX messages.
2. Apply a sender-based label to each scanned message: Sender/<domain-or-service>.
3. Mark account-operation messages as IMPORTANT.
4. Trash Social and Promotions category messages.
5. Optionally trash non-important INBOX messages after classification.

Authentication:
- Put a Google OAuth token JSON into GitHub Actions secret `GMAIL_TOKEN_JSON_B64`.
- The token must include Gmail modify scope:
  https://www.googleapis.com/auth/gmail.modify
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Iterable, Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# 「アカウント情報」に該当するメールの判定キーワード。
# ログイン、権限、セキュリティ、請求、決済、契約、障害通知、通信容量など、
# アカウント運用に直接影響する語彙を中心に構成する。
ACCOUNT_KEYWORDS = [
    # Japanese
    "セキュリティ", "ログイン", "サインイン", "認証", "確認コード", "確認", "権限",
    "アクセス", "請求", "支払", "支払い", "決済", "カード", "利用詳細",
    "ご請求金額", "ポイント加算", "契約", "試用", "トライアル", "残データ容量",
    "通信速度", "障害", "失敗", "エラー", "failure", "failed",
    # English
    "security", "login", "sign-in", "signin", "verification", "permission",
    "permissions", "billing", "payment", "invoice", "receipt", "card",
    "subscription", "trial", "usage", "quota", "password", "alert",
]

# 既知の重要送信元。必要に応じて増減させる。
ACCOUNT_SENDERS = [
    "accounts.google.com",
    "google.com",
    "github.com",
    "stripe.com",
    "system.kddi-fs.com",
    "act.auone.jp",
    "auone.jp",
    "openai.com",
]


@dataclass(frozen=True)
class GmailMessage:
    id: str
    thread_id: str
    labels: set[str]
    sender: str
    subject: str
    snippet: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got: {value!r}")


def get_credentials() -> Credentials:
    token_b64 = os.getenv("GMAIL_TOKEN_JSON_B64")
    token_json = os.getenv("GMAIL_TOKEN_JSON")

    if token_b64:
        token_raw = base64.b64decode(token_b64).decode("utf-8")
    elif token_json:
        token_raw = token_json
    else:
        raise RuntimeError(
            "Missing Gmail OAuth token. Set GMAIL_TOKEN_JSON_B64 or GMAIL_TOKEN_JSON."
        )

    info = json.loads(token_raw)
    creds = Credentials.from_authorized_user_info(info, scopes=[GMAIL_MODIFY_SCOPE])

    if not creds.valid and not creds.refresh_token:
        raise RuntimeError("OAuth token is invalid and has no refresh_token.")

    return creds


def gmail_service():
    creds = get_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(service, query: str, max_results: int) -> list[str]:
    ids: list[str] = []
    page_token: Optional[str] = None

    while True:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(500, max_results - len(ids)),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(m["id"] for m in response.get("messages", []))

        if len(ids) >= max_results:
            break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return ids


def get_header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def read_message(service, message_id: str) -> GmailMessage:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Subject"])
        .execute()
    )
    headers = msg.get("payload", {}).get("headers", [])
    return GmailMessage(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        labels=set(msg.get("labelIds", [])),
        sender=get_header(headers, "From"),
        subject=get_header(headers, "Subject"),
        snippet=msg.get("snippet", ""),
    )


def sender_domain(sender: str) -> str:
    _, addr = parseaddr(sender)
    addr = addr.lower()
    if "@" in addr:
        return addr.split("@", 1)[1]
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", sender.strip().lower())
    return cleaned[:80] or "unknown"


def sender_label_name(sender: str) -> str:
    domain = sender_domain(sender)
    # Gmail label nameに使いやすい形へ正規化する。
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", domain).strip("-")
    return f"Sender/{safe or 'unknown'}"


def ensure_label(service, name: str, cache: dict[str, str]) -> str:
    if name in cache:
        return cache[name]

    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        cache[label["name"]] = label["id"]

    if name in cache:
        return cache[name]

    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    cache[name] = created["id"]
    return created["id"]


def is_account_message(message: GmailMessage) -> bool:
    text = f"{message.sender} {message.subject} {message.snippet}".lower()
    domain = sender_domain(message.sender)

    if any(s in domain for s in ACCOUNT_SENDERS):
        if any(k.lower() in text for k in ACCOUNT_KEYWORDS):
            return True

    return any(k.lower() in text for k in ACCOUNT_KEYWORDS)


def is_social_or_promotion(message: GmailMessage) -> bool:
    return "CATEGORY_SOCIAL" in message.labels or "CATEGORY_PROMOTIONS" in message.labels


def modify_labels(service, message_id: str, add: Iterable[str] = (), remove: Iterable[str] = ()):
    body = {
        "addLabelIds": list(add),
        "removeLabelIds": list(remove),
    }
    return service.users().messages().modify(userId="me", id=message_id, body=body).execute()


def trash_message(service, message_id: str):
    return service.users().messages().trash(userId="me", id=message_id).execute()


def main() -> int:
    dry_run = env_bool("DRY_RUN", True)
    lookback_days = env_int("LOOKBACK_DAYS", 2)
    max_messages = env_int("MAX_MESSAGES", 300)
    trash_non_important = env_bool("TRASH_NON_IMPORTANT", True)
    sleep_seconds = float(os.getenv("SLEEP_SECONDS", "0.15"))

    query = f"in:inbox newer_than:{lookback_days}d"
    service = gmail_service()
    label_cache: dict[str, str] = {}

    message_ids = list_message_ids(service, query=query, max_results=max_messages)

    stats = {
        "scanned": 0,
        "sender_labeled": 0,
        "important_marked": 0,
        "trashed_social_or_promotions": 0,
        "trashed_non_important": 0,
        "kept_important": 0,
        "dry_run": dry_run,
        "query": query,
    }

    print(json.dumps({"event": "start", **stats}, ensure_ascii=False))

    for message_id in message_ids:
        message = read_message(service, message_id)
        stats["scanned"] += 1

        label_name = sender_label_name(message.sender)
        sender_label_id = ensure_label(service, label_name, label_cache)

        account_message = is_account_message(message)
        category_delete = is_social_or_promotion(message)
        already_important = "IMPORTANT" in message.labels
        will_keep_as_important = account_message or already_important

        actions: list[str] = []

        if not dry_run:
            modify_labels(service, message.id, add=[sender_label_id])
        actions.append(f"label:{label_name}")
        stats["sender_labeled"] += 1

        if account_message:
            if not dry_run:
                modify_labels(service, message.id, add=["IMPORTANT"])
            actions.append("mark:IMPORTANT")
            stats["important_marked"] += 1

        if category_delete:
            if not dry_run:
                trash_message(service, message.id)
            actions.append("trash:category_social_or_promotions")
            stats["trashed_social_or_promotions"] += 1
        elif trash_non_important and not will_keep_as_important:
            if not dry_run:
                trash_message(service, message.id)
            actions.append("trash:non_important")
            stats["trashed_non_important"] += 1
        else:
            actions.append("keep:important")
            stats["kept_important"] += 1

        print(json.dumps({
            "message_id": message.id,
            "from": message.sender,
            "subject": message.subject,
            "labels": sorted(message.labels),
            "actions": actions,
        }, ensure_ascii=False))

        time.sleep(sleep_seconds)

    print(json.dumps({"event": "finish", **stats}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HttpError as e:
        print(json.dumps({"event": "gmail_api_error", "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise
    except Exception as e:
        print(json.dumps({"event": "fatal_error", "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        raise
