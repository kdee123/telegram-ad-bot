# StarReach Ads

A consent-based Telegram ad marketplace bot for matching advertisers with channel owners.
Channel owners register channels they control, advertisers submit ad copy, and
owners approve each request before the bot posts anything.

## Features

- Channel owner registration with admin verification.
- Marketplace listing by category.
- Advertiser placement requests.
- Telegram Stars invoices for paid ad placements.
- TON and crypto payment instructions with owner confirmation.
- Owner earnings ledger for Stars payments.
- Payout requests for channel owners.
- Inline owner approval or rejection.
- SQLite storage.
- No external Python dependencies.

## Setup

1. Create a Telegram bot with BotFather and copy the token.
2. Copy `.env.example` to `.env`.
3. Put your token in `TELEGRAM_BOT_TOKEN`.
4. Optional: change `BOT_BRAND_NAME` if you want a different public name.
5. Run:

```powershell
python telegram_ad_bot.py
```

On Windows PowerShell, you can also set the token directly:

```powershell
$env:TELEGRAM_BOT_TOKEN="123456789:replace_with_your_bot_token"
python telegram_ad_bot.py
```

## Channel Owner Flow

1. Add the bot as an admin to your Telegram channel.
2. Make sure the bot has permission to post messages.
3. Message the bot privately:

```text
/register_channel @your_channel $50 tech 25000_subscribers
```

Useful owner commands:

```text
/set_stars_price @your_channel 250
/set_ton_wallet @your_channel EQxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
/set_crypto_wallet @your_channel USDT-TRC20: Txxxxxxxxxxxxxxxxxxxxxxxxxxxxx
/earnings
/request_payout XTR your_payout_account_or_notes
/my_channels
/disable @your_channel
/enable @your_channel
/requests
```

`/set_stars_price` enables automatic Telegram Stars invoices. The amount is in
Stars, so `250` means the advertiser pays 250 Stars for that placement.

TON and other crypto payments are intentionally manual in this starter version:
the bot sends your wallet details to the advertiser, records their transaction
reference, and asks you to mark the payment received before the ad is posted.

## How Channel Owners Make Money

Channel owners earn from approved paid ads on their channels.

- Telegram Stars: advertiser pays the bot invoice, the ad posts after Telegram
  confirms payment, and the owner balance is credited.
- TON and crypto: advertiser pays the channel owner's wallet directly, so the
  owner receives funds outside the bot.
- Manual: the owner handles payment outside the bot.

The platform can keep a commission on Stars payments with:

```env
PLATFORM_FEE_PERCENT=10
```

For example, if a channel charges 250 Stars and the platform fee is 10%, the
owner receives 225 XTR in their tracked balance and the platform keeps 25 XTR.

Owners can check and request payout:

```text
/earnings
/request_payout XTR your_payout_account_or_notes
```

The starter bot records payout requests, but the actual payout is manual. This
is because Telegram Stars revenue is held by the bot/platform account, not
automatically split to every channel owner by this script.

## Advertiser Flow

Advertisers message the bot privately:

```text
/list
/list tech
/request @your_channel stars Your ad copy goes here
/request @your_channel ton Your ad copy goes here
/request @your_channel crypto Your ad copy goes here
/confirm_crypto 12 transaction_id_or_wallet_reference
/requests
```

For Stars requests, the owner approves the ad copy first, then the advertiser
gets a Telegram invoice. After Telegram confirms payment, the bot posts the ad.

For TON or crypto requests, the owner approves the ad copy first, the advertiser
pays the wallet shown by the bot, then sends `/confirm_crypto`. The owner should
check their wallet and tap `Mark paid and post`.

## Important Notes

- This bot does not post to random channels.
- A channel must be registered by an admin, and the bot must be added to that
  channel with posting permission.
- Telegram Stars payments are handled through Telegram invoices with currency
  `XTR`.
- TON and crypto payments are not automatically verified on-chain in this
  starter version. Treat `/confirm_crypto` as a submitted reference, not proof
  of funds.
- Public `@channel` usernames are supported in this starter version.

## Run As A Service

For production, run the script on a VPS with a process manager such as systemd,
PM2, Docker, or a hosting platform that supports long-running Python processes.
Keep `.env` private and back up `ad_marketplace.sqlite3`.
