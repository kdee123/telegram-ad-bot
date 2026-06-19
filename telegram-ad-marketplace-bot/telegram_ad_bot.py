#!/usr/bin/env python3
"""Consent-based Telegram channel ad marketplace bot.

Channel owners register channels they control. Advertisers request placements.
Owners approve each request before the bot posts anything to a channel.
"""

from __future__ import annotations

import argparse
import html
import json
import os
# Fix for Render free tier
os.environ.setdefault('PORT', '10000')
import shlex
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ADMIN_STATUSES = {"creator", "administrator"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_channel(value: str) -> str:
    channel = value.strip()
    if channel.startswith("https://t.me/"):
        channel = "@" + channel.removeprefix("https://t.me/").strip("/")
    if channel.startswith("t.me/"):
        channel = "@" + channel.removeprefix("t.me/").strip("/")
    if not channel.startswith("@"):
        raise ValueError("Use a public channel username like @your_channel.")
    name = channel[1:]
    if not name or len(name) > 32:
        raise ValueError("Channel username length looks invalid.")
    if not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError("Channel username may only contain letters, numbers, and underscores.")
    return "@" + name


def parse_words(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


def parse_positive_int(value: str, label: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if number <= 0:
        raise ValueError(f"{label} must be greater than zero.")
    return number


def parse_user_ids(value: str) -> set[int]:
    user_ids: set[int] = set()
    for part in value.replace(";", ",").split(","):
        item = part.strip()
        if item:
            user_ids.add(parse_positive_int(item, "Platform admin ID"))
    return user_ids


def display_name(user: dict[str, Any]) -> str:
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    username = user.get("username")
    name = " ".join(part for part in (first, last) if part).strip()
    if username:
        return f"{name} (@{username})" if name else f"@{username}"
    return name or f"user {user.get('id')}"


class TelegramAPIError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org") -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.base_url = f"{self.api_base}/bot{token}"
        self._bot_id: int | None = None

    def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=75) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(f"Telegram {method} failed: {body}") from exc
        except urllib.error.URLError as exc:
            raise TelegramAPIError(f"Telegram {method} failed: {exc}") from exc

        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise TelegramAPIError(f"Telegram {method} failed: {parsed}")
        return parsed

    def get_bot_id(self) -> int:
        if self._bot_id is None:
            self._bot_id = int(self.call("getMe")["result"]["id"])
        return self._bot_id

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query", "pre_checkout_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload)["result"]

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = False,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload)

    def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if error_message:
            payload["error_message"] = error_message
        self.call("answerPreCheckoutQuery", payload)

    def send_invoice(
        self,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        currency: str,
        amount: int,
        label: str,
        provider_token: str = "",
    ) -> dict[str, Any]:
        return self.call(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": title[:32],
                "description": description[:255],
                "payload": payload,
                "provider_token": provider_token,
                "currency": currency,
                "prices": [{"label": label, "amount": amount}],
            },
        )


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS channels (
                handle TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                owner_user_id INTEGER NOT NULL,
                owner_chat_id INTEGER NOT NULL,
                price TEXT NOT NULL,
                stars_price INTEGER,
                ton_price INTEGER,
                crypto_price INTEGER,
                ton_wallet TEXT NOT NULL DEFAULT '',
                crypto_wallet TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                audience TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ad_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_handle TEXT NOT NULL REFERENCES channels(handle),
                advertiser_user_id INTEGER NOT NULL,
                advertiser_chat_id INTEGER NOT NULL,
                advertiser_name TEXT NOT NULL,
                ad_text TEXT NOT NULL,
                payment_method TEXT NOT NULL DEFAULT 'manual',
                payment_status TEXT NOT NULL DEFAULT 'unpaid',
                stars_amount INTEGER,
                payment_amount INTEGER,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT,
                crypto_txid TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS owner_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                request_id INTEGER NOT NULL UNIQUE REFERENCES ad_requests(id),
                channel_handle TEXT NOT NULL,
                currency TEXT NOT NULL,
                gross_amount INTEGER NOT NULL,
                platform_fee_amount INTEGER NOT NULL,
                owner_amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payout_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL,
                payout_details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'requested',
                created_at TEXT NOT NULL,
                paid_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_channels_owner
                ON channels(owner_user_id);
            CREATE INDEX IF NOT EXISTS idx_requests_channel_status
                ON ad_requests(channel_handle, status);
            CREATE INDEX IF NOT EXISTS idx_requests_advertiser
                ON ad_requests(advertiser_user_id);
            CREATE INDEX IF NOT EXISTS idx_earnings_owner
                ON owner_earnings(owner_user_id, currency, status);
            CREATE INDEX IF NOT EXISTS idx_payouts_owner
                ON payout_requests(owner_user_id, currency, status);
            """
        )
        self.add_column_if_missing("channels", "stars_price", "INTEGER")
        self.add_column_if_missing("channels", "ton_price", "INTEGER")
        self.add_column_if_missing("channels", "crypto_price", "INTEGER")
        self.add_column_if_missing("channels", "ton_wallet", "TEXT NOT NULL DEFAULT ''")
        self.add_column_if_missing("channels", "crypto_wallet", "TEXT NOT NULL DEFAULT ''")
        self.add_column_if_missing("ad_requests", "payment_method", "TEXT NOT NULL DEFAULT 'manual'")
        self.add_column_if_missing("ad_requests", "payment_status", "TEXT NOT NULL DEFAULT 'unpaid'")
        self.add_column_if_missing("ad_requests", "stars_amount", "INTEGER")
        self.add_column_if_missing("ad_requests", "payment_amount", "INTEGER")
        self.add_column_if_missing("ad_requests", "telegram_payment_charge_id", "TEXT")
        self.add_column_if_missing("ad_requests", "provider_payment_charge_id", "TEXT")
        self.add_column_if_missing("ad_requests", "crypto_txid", "TEXT")
        self.conn.commit()

    def add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_channel(
        self,
        handle: str,
        title: str,
        owner_user_id: int,
        owner_chat_id: int,
        price: str,
        category: str,
        audience: str,
    ) -> None:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO channels (
                handle, title, owner_user_id, owner_chat_id, price, category,
                audience, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(handle) DO UPDATE SET
                title = excluded.title,
                owner_user_id = excluded.owner_user_id,
                owner_chat_id = excluded.owner_chat_id,
                price = excluded.price,
                category = excluded.category,
                audience = excluded.audience,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (handle, title, owner_user_id, owner_chat_id, price, category, audience, now, now),
        )
        self.conn.commit()

    def list_channels(self, category: str | None = None) -> list[sqlite3.Row]:
        if category:
            cursor = self.conn.execute(
                """
                SELECT * FROM channels
                WHERE active = 1 AND lower(category) = lower(?)
                ORDER BY lower(category), lower(handle)
                """,
                (category,),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT * FROM channels
                WHERE active = 1
                ORDER BY lower(category), lower(handle)
                """
            )
        return list(cursor.fetchall())

    def get_channel(self, handle: str) -> sqlite3.Row | None:
        cursor = self.conn.execute("SELECT * FROM channels WHERE handle = ?", (handle,))
        return cursor.fetchone()

    def owner_channels(self, owner_user_id: int) -> list[sqlite3.Row]:
        cursor = self.conn.execute(
            "SELECT * FROM channels WHERE owner_user_id = ? ORDER BY lower(handle)",
            (owner_user_id,),
        )
        return list(cursor.fetchall())

    def set_channel_active(self, handle: str, owner_user_id: int, active: bool) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE channels
            SET active = ?, updated_at = ?
            WHERE handle = ? AND owner_user_id = ?
            """,
            (1 if active else 0, utc_now(), handle, owner_user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_channel_stars_price(self, handle: str, owner_user_id: int, stars_price: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE channels
            SET stars_price = ?, updated_at = ?
            WHERE handle = ? AND owner_user_id = ?
            """,
            (stars_price, utc_now(), handle, owner_user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_channel_payment_price(
        self,
        handle: str,
        owner_user_id: int,
        payment_method: str,
        amount: int,
    ) -> bool:
        column = "ton_price" if payment_method == "ton" else "crypto_price"
        cursor = self.conn.execute(
            f"""
            UPDATE channels
            SET {column} = ?, updated_at = ?
            WHERE handle = ? AND owner_user_id = ?
            """,
            (amount, utc_now(), handle, owner_user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def set_channel_wallet(
        self,
        handle: str,
        owner_user_id: int,
        wallet_kind: str,
        wallet_address: str,
    ) -> bool:
        column = "ton_wallet" if wallet_kind == "ton" else "crypto_wallet"
        cursor = self.conn.execute(
            f"""
            UPDATE channels
            SET {column} = ?, updated_at = ?
            WHERE handle = ? AND owner_user_id = ?
            """,
            (wallet_address, utc_now(), handle, owner_user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def create_request(
        self,
        channel_handle: str,
        advertiser_user_id: int,
        advertiser_chat_id: int,
        advertiser_name: str,
        ad_text: str,
        payment_method: str,
        stars_amount: int | None,
        payment_amount: int | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO ad_requests (
                channel_handle, advertiser_user_id, advertiser_chat_id,
                advertiser_name, ad_text, payment_method, payment_status,
                stars_amount, payment_amount, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'unpaid', ?, ?, 'pending', ?)
            """,
            (
                channel_handle,
                advertiser_user_id,
                advertiser_chat_id,
                advertiser_name,
                ad_text,
                payment_method,
                stars_amount,
                payment_amount,
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_request(self, request_id: int) -> sqlite3.Row | None:
        cursor = self.conn.execute(
            """
            SELECT r.*, c.owner_user_id, c.owner_chat_id, c.price, c.title,
                   c.stars_price, c.ton_wallet, c.crypto_wallet
            FROM ad_requests r
            JOIN channels c ON c.handle = r.channel_handle
            WHERE r.id = ?
            """,
            (request_id,),
        )
        return cursor.fetchone()

    def set_request_status(self, request_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE ad_requests SET status = ?, decided_at = ? WHERE id = ?",
            (status, utc_now(), request_id),
        )
        self.conn.commit()

    def mark_stars_paid(
        self,
        request_id: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE ad_requests
            SET payment_status = 'paid',
                telegram_payment_charge_id = ?,
                provider_payment_charge_id = ?
            WHERE id = ?
            """,
            (telegram_payment_charge_id, provider_payment_charge_id, request_id),
        )
        self.conn.commit()

    def mark_crypto_submitted(self, request_id: int, txid: str, advertiser_user_id: int) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE ad_requests
            SET payment_status = 'submitted',
                crypto_txid = ?
            WHERE id = ? AND advertiser_user_id = ? AND payment_method IN ('ton', 'crypto')
            """,
            (txid, request_id, advertiser_user_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def mark_manual_paid(self, request_id: int) -> None:
        self.conn.execute(
            "UPDATE ad_requests SET payment_status = 'paid' WHERE id = ?",
            (request_id,),
        )
        self.conn.commit()

    def credit_owner_earning(
        self,
        owner_user_id: int,
        request_id: int,
        channel_handle: str,
        currency: str,
        gross_amount: int,
        platform_fee_amount: int,
        owner_amount: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO owner_earnings (
                owner_user_id, request_id, channel_handle, currency,
                gross_amount, platform_fee_amount, owner_amount, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                owner_user_id,
                request_id,
                channel_handle,
                currency,
                gross_amount,
                platform_fee_amount,
                owner_amount,
                utc_now(),
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def owner_earnings_summary(self, owner_user_id: int) -> list[sqlite3.Row]:
        cursor = self.conn.execute(
            """
            SELECT
                e.currency,
                COALESCE(SUM(e.owner_amount), 0) AS earned_amount,
                COALESCE((
                    SELECT SUM(p.amount)
                    FROM payout_requests p
                    WHERE p.owner_user_id = e.owner_user_id
                      AND p.currency = e.currency
                      AND p.status IN ('requested', 'paid')
                ), 0) AS payout_amount
            FROM owner_earnings e
            WHERE e.owner_user_id = ?
            GROUP BY e.currency
            ORDER BY e.currency
            """,
            (owner_user_id,),
        )
        return list(cursor.fetchall())

    def owner_recent_earnings(self, owner_user_id: int) -> list[sqlite3.Row]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM owner_earnings
            WHERE owner_user_id = ?
            ORDER BY id DESC
            LIMIT 10
            """,
            (owner_user_id,),
        )
        return list(cursor.fetchall())

    def owner_available_balance(self, owner_user_id: int, currency: str) -> int:
        cursor = self.conn.execute(
            """
            SELECT
                COALESCE((SELECT SUM(owner_amount)
                          FROM owner_earnings
                          WHERE owner_user_id = ? AND currency = ?), 0)
                -
                COALESCE((SELECT SUM(amount)
                          FROM payout_requests
                          WHERE owner_user_id = ? AND currency = ?
                            AND status IN ('requested', 'paid')), 0)
                AS available
            """,
            (owner_user_id, currency, owner_user_id, currency),
        )
        row = cursor.fetchone()
        return int(row["available"] or 0)

    def create_payout_request(
        self,
        owner_user_id: int,
        currency: str,
        amount: int,
        payout_details: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO payout_requests (
                owner_user_id, currency, amount, payout_details, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner_user_id, currency, amount, payout_details, utc_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def owner_pending_requests(self, owner_user_id: int) -> list[sqlite3.Row]:
        cursor = self.conn.execute(
            """
            SELECT r.*, c.title, c.price
            FROM ad_requests r
            JOIN channels c ON c.handle = r.channel_handle
            WHERE c.owner_user_id = ? AND r.status = 'pending'
            ORDER BY r.id DESC
            LIMIT 20
            """,
            (owner_user_id,),
        )
        return list(cursor.fetchall())

    def advertiser_recent_requests(self, advertiser_user_id: int) -> list[sqlite3.Row]:
        cursor = self.conn.execute(
            """
            SELECT r.*, c.title, c.price
            FROM ad_requests r
            JOIN channels c ON c.handle = r.channel_handle
            WHERE r.advertiser_user_id = ?
            ORDER BY r.id DESC
            LIMIT 20
            """,
            (advertiser_user_id,),
        )
        return list(cursor.fetchall())


@dataclass
class BotConfig:
    brand_name: str = "StarReach Ads"
    disclosure_label: str = "Sponsored"
    max_ad_chars: int = 900
    platform_fee_percent: int = 10
    platform_ton_wallet: str = ""
    platform_crypto_wallet: str = ""
    platform_admin_ids: set[int] | None = None


class AdMarketplaceBot:
    def __init__(self, api: TelegramAPI, store: Store, config: BotConfig) -> None:
        self.api = api
        self.store = store
        self.config = config
        if self.config.platform_admin_ids is None:
            self.config.platform_admin_ids = set()

    def h(self, value: Any) -> str:
        return html.escape(str(value), quote=False)

    def code(self, value: Any) -> str:
        return f"<code>{html.escape(str(value), quote=False)}</code>"

    def panel(self, title: str, lines: list[str]) -> str:
        body = "\n".join(line for line in lines if line is not None)
        return f"<b>{self.h(title)}</b>\n----------\n{body}".strip()

    def field(self, label: str, value: Any) -> str:
        return f"<b>{self.h(label)}:</b> {self.h(value)}"

    def send_panel(
        self,
        chat_id: int | str,
        title: str,
        lines: list[str],
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = False,
    ) -> dict[str, Any]:
        return self.api.send_message(
            chat_id,
            self.panel(title, lines),
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
            parse_mode="HTML",
        )

    def success(self, chat_id: int | str, title: str, detail: str) -> None:
        self.send_panel(chat_id, title, [self.h(detail)])

    def usage(self, chat_id: int | str, title: str, command: str, example: str | None = None) -> None:
        lines = [f"Use: {self.code(command)}"]
        if example:
            lines.append(f"Example: {self.code(example)}")
        self.send_panel(chat_id, title, lines)

    def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            self.handle_message(update["message"])
        elif "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "pre_checkout_query" in update:
            self.handle_pre_checkout_query(update["pre_checkout_query"])

    def handle_message(self, message: dict[str, Any]) -> None:
        if "successful_payment" in message:
            self.handle_successful_payment(message)
            return

        text = message.get("text") or ""
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")

        if not text.startswith("/"):
            if chat_id:
                self.send_panel(chat_id, self.config.brand_name, [f"Send {self.code('/help')} to open the command menu."])
            return

        command, args = self.split_command(text)
        if command in {"start", "help"}:
            self.cmd_help(chat_id)
        elif command == "whoami":
            self.cmd_whoami(chat_id, user)
        elif command == "register_channel":
            self.cmd_register_channel(chat_id, user, args)
        elif command == "my_channels":
            self.cmd_my_channels(chat_id, user)
        elif command == "disable":
            self.cmd_set_active(chat_id, user, args, active=False)
        elif command == "enable":
            self.cmd_set_active(chat_id, user, args, active=True)
        elif command == "set_stars_price":
            self.cmd_set_stars_price(chat_id, user, args)
        elif command == "set_ton_price":
            self.cmd_set_payment_price(chat_id, user, args, payment_method="ton")
        elif command == "set_crypto_price":
            self.cmd_set_payment_price(chat_id, user, args, payment_method="crypto")
        elif command == "set_ton_wallet":
            self.cmd_set_wallet(chat_id, user, args, wallet_kind="ton")
        elif command == "set_crypto_wallet":
            self.cmd_set_wallet(chat_id, user, args, wallet_kind="crypto")
        elif command == "list":
            self.cmd_list(chat_id, args)
        elif command == "request":
            self.cmd_request(chat_id, user, args)
        elif command == "confirm_crypto":
            self.cmd_confirm_crypto(chat_id, user, args)
        elif command == "earnings":
            self.cmd_earnings(chat_id, user)
        elif command == "request_payout":
            self.cmd_request_payout(chat_id, user, args)
        elif command == "requests":
            self.cmd_requests(chat_id, user)
        else:
            self.send_panel(chat_id, "Unknown Command", [f"Send {self.code('/help')} for the command menu."])

    def split_command(self, text: str) -> tuple[str, str]:
        first, _, rest = text.strip().partition(" ")
        command = first[1:].split("@", 1)[0].lower()
        return command, rest.strip()

    def cmd_help(self, chat_id: int) -> None:
        self.send_panel(
            chat_id,
            self.config.brand_name,
            [
                "<b>Owner tools</b>",
                f"{self.code('/register_channel @channel price category audience')}",
                f"{self.code('/my_channels')}  {self.code('/earnings')}  {self.code('/requests')}",
                f"{self.code('/set_stars_price @channel 250')}",
                f"{self.code('/set_ton_price @channel 25')}",
                f"{self.code('/set_crypto_price @channel 30')}",
                f"{self.code('/set_ton_wallet @channel wallet_address')}",
                f"{self.code('/set_crypto_wallet @channel wallet_or_payment_instructions')}",
                f"{self.code('/request_payout XTR payout_account_or_notes')}",
                f"{self.code('/disable @channel')}  {self.code('/enable @channel')}",
                "",
                "<b>Advertiser tools</b>",
                f"{self.code('/list')}  {self.code('/list tech')}",
                f"{self.code('/request @channel stars Your ad text here')}",
                f"{self.code('/request @channel ton Your ad text here')}",
                f"{self.code('/request @channel crypto Your ad text here')}",
                f"{self.code('/confirm_crypto request_id transaction_id')}",
                f"{self.code('/whoami')}",
                "",
                "Paid placements stay approval-based. Earnings and payout requests are tracked automatically.",
            ],
        )

    def cmd_whoami(self, chat_id: int, user: dict[str, Any]) -> None:
        self.send_panel(chat_id, "Your Account", [self.field("Telegram user ID", user["id"])])

    def cmd_register_channel(self, chat_id: int, user: dict[str, Any], args: str) -> None:
        parts = parse_words(args)
        if len(parts) < 3:
            self.usage(
                chat_id,
                "Register Channel",
                "/register_channel @channel price category audience",
                "/register_channel @dailytech $50 tech 25000_subscribers",
            )
            return

        try:
            handle = normalize_channel(parts[0])
        except ValueError as exc:
            self.send_panel(chat_id, "Channel Error", [self.h(exc)])
            return

        price = parts[1]
        category = parts[2]
        audience = " ".join(parts[3:]) if len(parts) > 3 else "Audience details not provided"
        user_id = int(user["id"])

        try:
            chat_info = self.api.call("getChat", {"chat_id": handle})["result"]
            member = self.api.call("getChatMember", {"chat_id": handle, "user_id": user_id})["result"]
            if member.get("status") not in ADMIN_STATUSES:
                self.send_panel(chat_id, "Admin Required", [f"You must be an admin of {self.code(handle)} to register it."])
                return

            bot_member = self.api.call(
                "getChatMember",
                {"chat_id": handle, "user_id": self.api.get_bot_id()},
            )["result"]
            can_post = bot_member.get("status") == "creator" or bool(bot_member.get("can_post_messages"))
            if bot_member.get("status") not in ADMIN_STATUSES or not can_post:
                self.send_panel(
                    chat_id,
                    "Bot Permission Needed",
                    [f"Add this bot as an admin in {self.code(handle)} with permission to post messages, then try again."],
                )
                return
        except TelegramAPIError as exc:
            self.send_panel(
                chat_id,
                "Verification Failed",
                [
                    "Make sure the channel is public, the username is correct, and this bot is an admin.",
                    self.field("Telegram response", exc),
                ],
            )
            return

        title = chat_info.get("title") or handle
        self.store.upsert_channel(handle, title, user_id, chat_id, price, category, audience)
        self.send_panel(
            chat_id,
            "Channel Registered",
            [
                self.field("Channel", f"{title} ({handle})"),
                f"Advertisers can now request placements with {self.code('/request ' + handle + ' stars Your ad text')}.",
            ],
        )

    def cmd_my_channels(self, chat_id: int, user: dict[str, Any]) -> None:
        rows = self.store.owner_channels(int(user["id"]))
        if not rows:
            self.send_panel(chat_id, "Your Channels", [f"No channels yet. Start with {self.code('/register_channel @channel price category audience')}."])
            return

        lines: list[str] = []
        for row in rows:
            status = "active" if row["active"] else "disabled"
            stars = f"{row['stars_price']} Stars" if row["stars_price"] else "Stars unset"
            ton_price = f"{row['ton_price']} TON units" if row["ton_price"] else "TON price unset"
            crypto_price = f"{row['crypto_price']} crypto units" if row["crypto_price"] else "crypto price unset"
            ton = "TON wallet set" if row["ton_wallet"] else "TON wallet unset"
            crypto = "crypto wallet set" if row["crypto_wallet"] else "crypto wallet unset"
            lines.extend(
                [
                    f"<b>{self.h(row['title'])}</b> {self.code(row['handle'])}",
                    self.field("Status", status),
                    self.field("Category", row["category"]),
                    self.field("Public price", row["price"]),
                    self.field("Payments", f"{stars}; {ton_price}; {crypto_price}"),
                    self.field("Wallets", f"{ton}; {crypto}"),
                    "",
                ]
            )
        self.send_panel(chat_id, "Your Channels", lines)

    def cmd_set_stars_price(self, chat_id: int, user: dict[str, Any], args: str) -> None:
        parts = parse_words(args)
        if len(parts) != 2:
            self.api.send_message(chat_id, "Usage: /set_stars_price @channel 250")
            return

        try:
            handle = normalize_channel(parts[0])
            stars_price = parse_positive_int(parts[1], "Stars price")
        except ValueError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        changed = self.store.set_channel_stars_price(handle, int(user["id"]), stars_price)
        if not changed:
            self.api.send_message(chat_id, f"I did not find {handle} among your registered channels.")
            return
        self.api.send_message(chat_id, f"{handle} now accepts Telegram Stars at {stars_price} Stars per ad.")

    def cmd_set_payment_price(
        self,
        chat_id: int,
        user: dict[str, Any],
        args: str,
        payment_method: str,
    ) -> None:
        parts = parse_words(args)
        command = "set_ton_price" if payment_method == "ton" else "set_crypto_price"
        label = "TON" if payment_method == "ton" else "crypto"
        if len(parts) != 2:
            self.api.send_message(chat_id, f"Usage: /{command} @channel 25")
            return

        try:
            handle = normalize_channel(parts[0])
            amount = parse_positive_int(parts[1], f"{label} price")
        except ValueError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        changed = self.store.set_channel_payment_price(handle, int(user["id"]), payment_method, amount)
        if not changed:
            self.api.send_message(chat_id, f"I did not find {handle} among your registered channels.")
            return
        self.api.send_message(chat_id, f"{handle} now accepts {label} payments at {amount} units per ad.")

    def cmd_set_wallet(
        self,
        chat_id: int,
        user: dict[str, Any],
        args: str,
        wallet_kind: str,
    ) -> None:
        first, _, wallet_address = args.partition(" ")
        command = "set_ton_wallet" if wallet_kind == "ton" else "set_crypto_wallet"
        if not first or not wallet_address.strip():
            self.api.send_message(chat_id, f"Usage: /{command} @channel wallet_address")
            return

        try:
            handle = normalize_channel(first)
        except ValueError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        changed = self.store.set_channel_wallet(
            handle,
            int(user["id"]),
            wallet_kind,
            wallet_address.strip(),
        )
        if not changed:
            self.api.send_message(chat_id, f"I did not find {handle} among your registered channels.")
            return

        label = "TON" if wallet_kind == "ton" else "crypto"
        self.api.send_message(chat_id, f"{label} payment instructions saved for {handle}.")

    def cmd_set_active(self, chat_id: int, user: dict[str, Any], args: str, active: bool) -> None:
        parts = parse_words(args)
        if not parts:
            verb = "enable" if active else "disable"
            self.api.send_message(chat_id, f"Usage: /{verb} @channel")
            return

        try:
            handle = normalize_channel(parts[0])
        except ValueError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        changed = self.store.set_channel_active(handle, int(user["id"]), active)
        if not changed:
            self.api.send_message(chat_id, f"I did not find {handle} among your registered channels.")
            return

        state = "enabled" if active else "disabled"
        self.api.send_message(chat_id, f"{handle} is now {state}.")

    def cmd_list(self, chat_id: int, args: str) -> None:
        category = args.strip() or None
        rows = self.store.list_channels(category)
        if not rows:
            if category:
                self.api.send_message(chat_id, f"No active channels found in category '{category}'.")
            else:
                self.api.send_message(chat_id, "No active channels are registered yet.")
            return

        lines = ["Available placements:"]
        for row in rows[:40]:
            methods = ["manual"]
            if row["stars_price"]:
                methods.append(f"stars:{row['stars_price']}")
            if self.payment_wallet(row, "ton") and row["ton_price"]:
                methods.append(f"ton:{row['ton_price']}")
            if self.payment_wallet(row, "crypto") and row["crypto_price"]:
                methods.append(f"crypto:{row['crypto_price']}")
            lines.append(
                f"{row['handle']} - {row['title']} - {row['price']} - {row['category']} - {row['audience']} - pay: {', '.join(methods)}"
            )
        lines.append("")
        lines.append("Request one with: /request @channel stars Your ad text here")
        self.api.send_message(chat_id, "\n".join(lines), disable_web_page_preview=True)

    def cmd_request(self, chat_id: int, user: dict[str, Any], args: str) -> None:
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            self.api.send_message(chat_id, "Usage: /request @channel stars|ton|crypto|manual Your ad text here")
            return

        try:
            handle = normalize_channel(parts[0])
        except ValueError as exc:
            self.api.send_message(chat_id, str(exc))
            return

        payment_method = parts[1].lower()
        if payment_method not in {"stars", "ton", "crypto", "manual"}:
            self.api.send_message(chat_id, "Payment method must be stars, ton, crypto, or manual.")
            return

        channel = self.store.get_channel(handle)
        if not channel or not channel["active"]:
            self.api.send_message(chat_id, f"{handle} is not active in this marketplace.")
            return
        if payment_method == "stars" and not channel["stars_price"]:
            self.api.send_message(chat_id, f"{handle} has not enabled Telegram Stars payments.")
            return
        if payment_method == "ton" and (not self.payment_wallet(channel, "ton") or not channel["ton_price"]):
            self.api.send_message(chat_id, f"{handle} has not enabled TON payments with a price.")
            return
        if payment_method == "crypto" and (not self.payment_wallet(channel, "crypto") or not channel["crypto_price"]):
            self.api.send_message(chat_id, f"{handle} has not enabled crypto payments with a price.")
            return

        ad_text = parts[2].strip()
        if len(ad_text) > self.config.max_ad_chars:
            self.api.send_message(
                chat_id,
                f"Please keep ad copy under {self.config.max_ad_chars} characters.",
            )
            return

        request_id = self.store.create_request(
            handle,
            int(user["id"]),
            chat_id,
            display_name(user),
            ad_text,
            payment_method,
            int(channel["stars_price"]) if payment_method == "stars" else None,
            self.request_payment_amount(channel, payment_method),
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"approve:{request_id}"},
                    {"text": "Reject", "callback_data": f"reject:{request_id}"},
                ]
            ]
        }
        amount_line = self.payment_summary(channel, payment_method)
        owner_text = "\n".join(
            [
                f"New ad request #{request_id} for {handle}",
                f"Advertiser: {display_name(user)}",
                f"Listed price: {channel['price']}",
                f"Payment: {amount_line}",
                "",
                "Ad copy:",
                ad_text,
            ]
        )
        self.api.send_message(channel["owner_chat_id"], owner_text, reply_markup=keyboard)
        self.api.send_message(
            chat_id,
            f"Request #{request_id} sent to the owner of {handle}. You will be notified after approval or rejection.",
        )

    def payment_summary(self, channel: sqlite3.Row, payment_method: str) -> str:
        if payment_method == "stars":
            return f"{channel['stars_price']} Telegram Stars"
        if payment_method == "ton":
            target = "platform escrow wallet" if self.config.platform_ton_wallet else "owner wallet"
            return f"{channel['ton_price']} TON units to {target}"
        if payment_method == "crypto":
            target = "platform escrow wallet" if self.config.platform_crypto_wallet else "owner wallet"
            return f"{channel['crypto_price']} crypto units to {target}"
        return "manual payment outside the bot"

    def request_payment_amount(self, channel: sqlite3.Row, payment_method: str) -> int | None:
        if payment_method == "stars":
            return int(channel["stars_price"])
        if payment_method == "ton":
            return int(channel["ton_price"])
        if payment_method == "crypto":
            return int(channel["crypto_price"])
        return None

    def payment_wallet(self, channel: sqlite3.Row, payment_method: str) -> str:
        if payment_method == "ton":
            return self.config.platform_ton_wallet or channel["ton_wallet"]
        if payment_method == "crypto":
            return self.config.platform_crypto_wallet or channel["crypto_wallet"]
        return ""

    def crypto_payment_uses_platform_wallet(self, payment_method: str) -> bool:
        return (
            payment_method == "ton" and bool(self.config.platform_ton_wallet)
        ) or (
            payment_method == "crypto" and bool(self.config.platform_crypto_wallet)
        )

    def cmd_confirm_crypto(self, chat_id: int, user: dict[str, Any], args: str) -> None:
        parts = parse_words(args)
        if len(parts) < 2:
            self.api.send_message(chat_id, "Usage: /confirm_crypto request_id transaction_id")
            return
        if not parts[0].isdigit():
            self.api.send_message(chat_id, "Request ID must be a number.")
            return

        request_id = int(parts[0])
        txid = " ".join(parts[1:]).strip()
        changed = self.store.mark_crypto_submitted(request_id, txid, int(user["id"]))
        request = self.store.get_request(request_id)
        if not changed or not request:
            self.api.send_message(chat_id, "I could not find a TON/crypto request with that ID for you.")
            return

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Mark paid and post", "callback_data": f"paid:{request_id}"},
                    {"text": "Reject", "callback_data": f"reject:{request_id}"},
                ]
            ]
        }
        verification_chat_id = self.payment_verification_chat_id(request)
        verifier_label = "platform admin" if verification_chat_id != request["owner_chat_id"] else "owner"
        self.api.send_message(
            verification_chat_id,
            "\n".join(
                [
                    f"Payment submitted for request #{request_id}",
                    f"Channel: {request['channel_handle']}",
                    f"Method: {request['payment_method']}",
                    f"Amount: {request['payment_amount']} {self.earning_currency(request['payment_method'])}",
                    f"Transaction/reference: {txid}",
                    "",
                    "Only mark paid after checking your wallet.",
                ]
            ),
            reply_markup=keyboard,
        )
        self.api.send_message(chat_id, f"Payment reference submitted. The {verifier_label} will verify it.")

    def payment_verification_chat_id(self, request: sqlite3.Row) -> int:
        if self.crypto_payment_uses_platform_wallet(request["payment_method"]) and self.config.platform_admin_ids:
            return next(iter(self.config.platform_admin_ids))
        return int(request["owner_chat_id"])

    def cmd_earnings(self, chat_id: int, user: dict[str, Any]) -> None:
        owner_user_id = int(user["id"])
        summary_rows = self.store.owner_earnings_summary(owner_user_id)
        recent_rows = self.store.owner_recent_earnings(owner_user_id)
        if not summary_rows:
            self.api.send_message(
                chat_id,
                "No tracked earnings yet. You earn when a paid ad is posted on one of your channels.",
            )
            return

        lines = ["Your tracked earnings:"]
        for row in summary_rows:
            earned = int(row["earned_amount"] or 0)
            payouts = int(row["payout_amount"] or 0)
            available = earned - payouts
            lines.append(
                f"{row['currency']}: earned {earned}, payout requested/paid {payouts}, available {available}"
            )

        if recent_rows:
            lines.append("")
            lines.append("Recent earnings:")
            for row in recent_rows:
                lines.append(
                    f"#{row['request_id']} - {row['channel_handle']} - "
                    f"{row['owner_amount']} {row['currency']} after {row['platform_fee_amount']} fee"
                )

        lines.append("")
        lines.append("Request payout with: /request_payout XTR payout_account_or_notes")
        self.api.send_message(chat_id, "\n".join(lines))

    def cmd_request_payout(self, chat_id: int, user: dict[str, Any], args: str) -> None:
        currency, _, payout_details = args.partition(" ")
        currency = currency.upper().strip()
        payout_details = payout_details.strip()
        if not currency or not payout_details:
            self.api.send_message(chat_id, "Usage: /request_payout XTR payout_account_or_notes")
            return

        owner_user_id = int(user["id"])
        available = self.store.owner_available_balance(owner_user_id, currency)
        if available <= 0:
            self.api.send_message(chat_id, f"You do not have available {currency} earnings to request yet.")
            return

        payout_id = self.store.create_payout_request(owner_user_id, currency, available, payout_details)
        self.api.send_message(
            chat_id,
            f"Payout request #{payout_id} created for {available} {currency}. Mark it paid after sending funds manually.",
        )

    def cmd_requests(self, chat_id: int, user: dict[str, Any]) -> None:
        user_id = int(user["id"])
        owner_rows = self.store.owner_pending_requests(user_id)
        advertiser_rows = self.store.advertiser_recent_requests(user_id)

        lines: list[str] = []
        if owner_rows:
            lines.append("Pending requests for your channels:")
            for row in owner_rows:
                lines.append(
                    f"#{row['id']} - {row['channel_handle']} - {row['advertiser_name']} - "
                    f"{row['payment_method']} - {row['payment_status']}"
                )
            lines.append("")

        if advertiser_rows:
            lines.append("Your recent ad requests:")
            for row in advertiser_rows:
                lines.append(
                    f"#{row['id']} - {row['channel_handle']} - {row['status']} - "
                    f"{row['payment_method']} - {row['payment_status']}"
                )

        if not lines:
            self.api.send_message(chat_id, "No requests found.")
            return
        self.api.send_message(chat_id, "\n".join(lines))

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data") or ""
        callback_id = callback.get("id")
        user = callback.get("from") or {}
        user_id = int(user["id"])

        action, _, raw_request_id = data.partition(":")
        if action not in {"approve", "reject", "paid"} or not raw_request_id.isdigit():
            if callback_id:
                self.api.answer_callback_query(callback_id, "Unknown action.")
            return

        request_id = int(raw_request_id)
        request = self.store.get_request(request_id)
        if not request:
            self.api.answer_callback_query(callback_id, "Request not found.")
            return
        if not self.can_use_request_action(user_id, action, request):
            self.api.answer_callback_query(callback_id, "You cannot decide this request.")
            return
        if action == "paid" and request["status"] != "approved_pending_payment":
            self.api.answer_callback_query(callback_id, f"Not waiting for payment. Current status: {request['status']}.")
            return
        if action != "paid" and request["status"] != "pending":
            self.api.answer_callback_query(callback_id, f"Already {request['status']}.")
            return

        if action == "reject":
            self.store.set_request_status(request_id, "rejected")
            self.api.answer_callback_query(callback_id, "Rejected.")
            self.api.send_message(
                request["advertiser_chat_id"],
                f"Your ad request #{request_id} for {request['channel_handle']} was rejected.",
            )
            return

        if action == "paid":
            if request["payment_method"] not in {"ton", "crypto"}:
                self.api.answer_callback_query(callback_id, "This request is not a crypto payment.")
                return
            self.store.mark_manual_paid(request_id)
            self.post_approved_request(request_id, callback_id)
            return

        if request["payment_method"] == "stars" and request["payment_status"] != "paid":
            payload = f"ad_request:{request_id}"
            try:
                self.api.send_invoice(
                    int(request["advertiser_chat_id"]),
                    "Channel ad placement",
                    f"Ad placement on {request['channel_handle']}",
                    payload,
                    "XTR",
                    int(request["stars_amount"]),
                    "Ad placement",
                )
            except TelegramAPIError as exc:
                self.api.answer_callback_query(callback_id, "Invoice failed.")
                self.api.send_message(request["owner_chat_id"], f"I could not send the Stars invoice: {exc}")
                return
            self.store.set_request_status(request_id, "approved_pending_payment")
            self.api.answer_callback_query(callback_id, "Stars invoice sent.")
            self.api.send_message(
                request["owner_chat_id"],
                f"Approved request #{request_id}. I sent the advertiser a Telegram Stars invoice.",
            )
            return

        if request["payment_method"] in {"ton", "crypto"} and request["payment_status"] != "paid":
            wallet = self.payment_wallet(request, request["payment_method"])
            self.store.set_request_status(request_id, "approved_pending_payment")
            self.api.answer_callback_query(callback_id, "Payment instructions sent.")
            self.api.send_message(
                request["advertiser_chat_id"],
                "\n".join(
                    [
                        f"Your ad request #{request_id} was approved.",
                        f"Amount: {request['payment_amount']} {self.earning_currency(request['payment_method'])}",
                        f"Pay with {request['payment_method'].upper()} to:",
                        wallet,
                        "",
                        f"Use memo/reference: ad_request:{request_id}",
                        "After paying, send:",
                        f"/confirm_crypto {request_id} transaction_id",
                    ]
                ),
            )
            self.api.send_message(
                request["owner_chat_id"],
                f"Approved request #{request_id}. Waiting for advertiser payment reference.",
            )
            return

        self.post_approved_request(request_id, callback_id)

    def can_use_request_action(self, user_id: int, action: str, request: sqlite3.Row) -> bool:
        if action == "paid" and self.crypto_payment_uses_platform_wallet(request["payment_method"]):
            admin_ids = self.config.platform_admin_ids or set()
            return user_id in admin_ids if admin_ids else int(request["owner_user_id"]) == user_id
        return int(request["owner_user_id"]) == user_id

    def post_approved_request(self, request_id: int, callback_id: str | None = None) -> None:
        request = self.store.get_request(request_id)
        if not request:
            if callback_id:
                self.api.answer_callback_query(callback_id, "Request not found.")
            return

        post_text = f"{self.config.disclosure_label}\n\n{request['ad_text']}"
        try:
            self.api.send_message(request["channel_handle"], post_text)
        except TelegramAPIError as exc:
            if callback_id:
                self.api.answer_callback_query(callback_id, "Posting failed.")
            self.api.send_message(
                request["owner_chat_id"],
                f"I could not post request #{request_id}. Check that I still have posting permission.\n\n{exc}",
            )
            return

        self.store.set_request_status(request_id, "posted")
        self.credit_owner_after_post(request)
        if callback_id:
            self.api.answer_callback_query(callback_id, "Posted.")
        self.api.send_message(
            request["owner_chat_id"],
            f"Posted request #{request_id} to {request['channel_handle']}.",
        )
        self.api.send_message(
            request["advertiser_chat_id"],
            f"Your ad request #{request_id} was approved and posted to {request['channel_handle']}.",
        )

    def credit_owner_after_post(self, request: sqlite3.Row) -> None:
        if request["payment_status"] != "paid":
            return

        currency = self.earning_currency(request["payment_method"])
        gross_amount = self.earning_gross_amount(request)
        if gross_amount <= 0:
            return

        platform_fee = gross_amount * self.config.platform_fee_percent // 100
        owner_amount = gross_amount - platform_fee
        credited = self.store.credit_owner_earning(
            int(request["owner_user_id"]),
            int(request["id"]),
            request["channel_handle"],
            currency,
            gross_amount,
            platform_fee,
            owner_amount,
        )
        if credited:
            self.api.send_message(
                request["owner_chat_id"],
                f"Earned {owner_amount} {currency} from request #{request['id']} "
                f"after {platform_fee} {currency} platform fee.",
            )

    def earning_currency(self, payment_method: str) -> str:
        if payment_method == "stars":
            return "XTR"
        if payment_method == "ton":
            return "TON"
        if payment_method == "crypto":
            return "CRYPTO"
        return "MANUAL"

    def earning_gross_amount(self, request: sqlite3.Row) -> int:
        if request["payment_method"] == "stars":
            return int(request["stars_amount"] or 0)
        if request["payment_method"] in {"ton", "crypto"} and self.crypto_payment_uses_platform_wallet(request["payment_method"]):
            return int(request["payment_amount"] or 0)
        return 0

    def handle_pre_checkout_query(self, query: dict[str, Any]) -> None:
        payload = query.get("invoice_payload") or ""
        request_id = self.request_id_from_payload(payload)
        request = self.store.get_request(request_id) if request_id else None
        if not request or request["status"] != "approved_pending_payment":
            self.api.answer_pre_checkout_query(
                query["id"],
                False,
                "This ad request is not ready for payment.",
            )
            return
        if request["payment_method"] != "stars" or query.get("currency") != "XTR":
            self.api.answer_pre_checkout_query(query["id"], False, "Unsupported payment method.")
            return
        if int(query.get("total_amount", 0)) != int(request["stars_amount"]):
            self.api.answer_pre_checkout_query(query["id"], False, "The invoice amount does not match.")
            return
        self.api.answer_pre_checkout_query(query["id"], True)

    def handle_successful_payment(self, message: dict[str, Any]) -> None:
        payment = message["successful_payment"]
        request_id = self.request_id_from_payload(payment.get("invoice_payload") or "")
        request = self.store.get_request(request_id) if request_id else None
        chat_id = message.get("chat", {}).get("id")
        if not request:
            if chat_id:
                self.api.send_message(chat_id, "Payment received, but I could not match it to an ad request.")
            return

        self.store.mark_stars_paid(
            request_id,
            payment.get("telegram_payment_charge_id", ""),
            payment.get("provider_payment_charge_id", ""),
        )
        self.post_approved_request(request_id)

    def request_id_from_payload(self, payload: str) -> int | None:
        prefix = "ad_request:"
        if not payload.startswith(prefix):
            return None
        raw = payload.removeprefix(prefix)
        return int(raw) if raw.isdigit() else None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Telegram ad marketplace bot.")
    parser.add_argument(
        "--db",
        default=os.getenv("AD_BOT_DB", "ad_marketplace.sqlite3"),
        help="SQLite database path. Defaults to AD_BOT_DB or ad_marketplace.sqlite3.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=int(os.getenv("POLL_TIMEOUT", "30")),
        help="Telegram long-poll timeout in seconds.",
    )
    return parser


def main() -> int:
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN. Create .env or set the environment variable.", file=sys.stderr)
        return 2

    config = BotConfig(
        brand_name=os.getenv("BOT_BRAND_NAME", "StarReach Ads"),
        disclosure_label=os.getenv("AD_DISCLOSURE_LABEL", "Sponsored"),
        max_ad_chars=int(os.getenv("MAX_AD_CHARS", "900")),
        platform_fee_percent=int(os.getenv("PLATFORM_FEE_PERCENT", "10")),
        platform_ton_wallet=os.getenv("PLATFORM_TON_WALLET", "").strip(),
        platform_crypto_wallet=os.getenv("PLATFORM_CRYPTO_WALLET", "").strip(),
        platform_admin_ids=parse_user_ids(os.getenv("PLATFORM_ADMIN_IDS", "")),
    )
    api = TelegramAPI(token)
    store = Store(args.db)
    bot = AdMarketplaceBot(api, store, config)

    offset: int | None = None
    print(f"{config.brand_name} is running. Press Ctrl+C to stop.")
    while True:
        try:
            for update in api.get_updates(offset, args.poll_timeout):
                offset = int(update["update_id"]) + 1
                try:
                    bot.handle_update(update)
                except Exception as exc:  # Keep one bad update from stopping the bot.
                    print(f"Update {update.get('update_id')} failed: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except TelegramAPIError as exc:
            print(exc, file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
