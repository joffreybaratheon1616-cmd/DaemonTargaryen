# DaemonTargaryen

A Python Telegram digital-products marketplace bot using python-telegram-bot and MongoDB.

## Features

- `/start` main menu
- Account page
- Ownership verification of the Telegram user currently interacting with the bot
- Digital-product categories
- MongoDB-backed products
- Atomic wallet deduction during purchases
- Order history
- MongoDB indexes
- Environment-based secrets
- Render/polling friendly

## Important

This project does not collect, store, validate, or distribute Telegram session strings,
raw auth-key hex, OTP codes, passwords, or other Telegram login credentials.

## Environment

Copy `.env.example` values into your hosting provider's environment variables:

- `BOT_TOKEN`
- `MONGO_URI`
- `DB_NAME`

## Add a product

Insert a document into the `products` collection:

```json
{
  "name": "Premium Digital Package",
  "description": "Example legitimate digital product.",
  "category": "digital",
  "price": 100,
  "active": true
}
```

Valid categories used by the menu:

- `digital`
- `software`
- `other`

## Run

```bash
pip install -r requirements.txt
python main.py
```
