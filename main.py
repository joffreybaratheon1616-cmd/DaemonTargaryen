import logging
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("DaemonTargaryen")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "DaemonTargaryen")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing.")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]

users = db["users"]
products = db["products"]
orders = db["orders"]
verifications = db["ownership_verifications"]


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Verify Account", callback_data="verify")],
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("📦 Products", callback_data="products")],
        [InlineKeyboardButton("📜 Orders", callback_data="orders")],
        [InlineKeyboardButton("🛟 Support", callback_data="support")],
    ])


def product_categories():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Digital Products", callback_data="digital")],
        [InlineKeyboardButton("💻 Software & Tools", callback_data="software")],
        [InlineKeyboardButton("🎁 Other Products", callback_data="other")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
    ])


async def ensure_user(update: Update):
    user = update.effective_user
    await users.update_one(
        {"telegram_user_id": user.id},
        {
            "$set": {
                "telegram_user_id": user.id,
                "first_name": user.first_name or "",
                "username": user.username or "",
                "updated_at": datetime.now(timezone.utc),
            },
            "$setOnInsert": {
                "balance": 0.0,
                "created_at": datetime.now(timezone.utc),
            },
        },
        upsert=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update)
    await update.message.reply_text(
        "💎 *DaemonTargaryen Bot*\n\n"
        f"Welcome back, {update.effective_user.first_name}! 👋\n\n"
        "🆔 Account: Connected\n"
        "🛡️ Verification: Check the Verify Account button\n"
        "📦 Status: Ready",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


async def products_menu(query):
    await query.edit_message_text(
        "🛒 *Products*\n\nChoose a category:",
        parse_mode="Markdown",
        reply_markup=product_categories(),
    )


async def show_category(query, category):
    cursor = products.find({"category": category, "active": True}).sort("created_at", -1)
    items = await cursor.to_list(length=20)

    if not items:
        await query.edit_message_text(
            "📦 *No products available*\n\n"
            "There are currently no products in this category.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="products")]
            ]),
        )
        return

    buttons = []
    for item in items:
        buttons.append([
            InlineKeyboardButton(
                f"{item.get('name', 'Product')} — ₹{item.get('price', 0):.2f}",
                callback_data=f"product:{item['_id']}",
            )
        ])

    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="products")])

    await query.edit_message_text(
        f"🛒 *{category.replace('_', ' ').title()}*\n\n"
        "Select a product:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_product(query, product_id):
    from bson import ObjectId

    try:
        oid = ObjectId(product_id)
    except Exception:
        await query.answer("Invalid product.", show_alert=True)
        return

    item = await products.find_one({"_id": oid, "active": True})
    if not item:
        await query.answer("Product is unavailable.", show_alert=True)
        return

    text = (
        f"📦 *{item.get('name', 'Product')}*\n\n"
        f"{item.get('description', 'No description available.')}\n\n"
        f"💰 Price: ₹{item.get('price', 0):.2f}\n"
        f"🟢 Status: Available"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Buy", callback_data=f"buy:{item['_id']}")],
        [InlineKeyboardButton("◀️ Back", callback_data=f"category:{item.get('category', 'digital')}")],
    ])

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)


async def buy_product(query, product_id):
    from bson import ObjectId

    try:
        oid = ObjectId(product_id)
    except Exception:
        await query.answer("Invalid product.", show_alert=True)
        return

    item = await products.find_one({"_id": oid, "active": True})
    if not item:
        await query.answer("Product is unavailable.", show_alert=True)
        return

    user_id = query.from_user.id
    user = await users.find_one({"telegram_user_id": user_id})

    if not user:
        await query.answer("Please use /start first.", show_alert=True)
        return

    price = float(item.get("price", 0))
    balance = float(user.get("balance", 0))

    if balance < price:
        await query.answer(
            f"Insufficient balance. Required ₹{price:.2f}.",
            show_alert=True,
        )
        return

    # Atomic balance deduction prevents two simultaneous purchases
    # from spending the same balance.
    result = await users.update_one(
        {
            "telegram_user_id": user_id,
            "balance": {"$gte": price},
        },
        {
            "$inc": {"balance": -price},
            "$set": {"updated_at": datetime.now(timezone.utc)},
        },
    )

    if result.modified_count != 1:
        await query.answer("Purchase could not be completed. Try again.", show_alert=True)
        return

    await orders.insert_one({
        "telegram_user_id": user_id,
        "product_id": oid,
        "product_name": item.get("name", "Product"),
        "price": price,
        "created_at": datetime.now(timezone.utc),
        "status": "paid",
    })

    await query.edit_message_text(
        "✅ *Purchase Successful!*\n\n"
        f"📦 Product: {item.get('name', 'Product')}\n"
        f"💰 Paid: ₹{price:.2f}\n\n"
        "Your digital product will be delivered according to the product's configured delivery method.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 My Orders", callback_data="orders")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
        ]),
    )


async def account_menu(query):
    user = await users.find_one({"telegram_user_id": query.from_user.id})
    verification = await verifications.find_one(
        {"telegram_user_id": query.from_user.id}
    )

    balance = float((user or {}).get("balance", 0))
    status = (verification or {}).get("verification_status", "not_verified")

    await query.edit_message_text(
        "👤 *My Account*\n\n"
        f"🆔 Telegram ID: `{query.from_user.id}`\n"
        f"💰 Balance: ₹{balance:.2f}\n"
        f"🛡️ Ownership verification: `{status}`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 Verify Account", callback_data="verify")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
        ]),
    )


async def begin_verification(query):
    # This verifies control of the Telegram account currently interacting
    # with the bot. It does not collect session strings, auth keys, OTPs,
    # passwords, or credentials.
    challenge = os.urandom(24).hex()
    now = datetime.now(timezone.utc)

    await verifications.update_one(
        {"telegram_user_id": query.from_user.id},
        {
            "$set": {
                "telegram_user_id": query.from_user.id,
                "verification_status": "verified",
                "verification_method": "bot_interaction",
                "verification_challenge": challenge,
                "verified_at": now,
            }
        },
        upsert=True,
    )

    await query.edit_message_text(
        "✅ *Account Verified*\n\n"
        "Your ownership of the Telegram account currently interacting with this bot "
        "has been verified.\n\n"
        "No Telegram login/session credentials were collected.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 My Account", callback_data="account")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
        ]),
    )


async def orders_menu(query):
    cursor = orders.find(
        {"telegram_user_id": query.from_user.id}
    ).sort("created_at", -1)

    items = await cursor.to_list(length=10)

    if not items:
        text = "📜 *Orders*\n\nNo orders yet."
    else:
        lines = ["📜 *Orders*\n"]
        for item in items:
            lines.append(
                f"• {item.get('product_name', 'Product')} — "
                f"₹{float(item.get('price', 0)):.2f} — {item.get('status', 'unknown')}"
            )
        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]
        ]),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "home":
        await query.edit_message_text(
            "💎 *DaemonTargaryen Bot*\n\n"
            f"Welcome back, {query.from_user.first_name}! 👋",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
    elif data == "products":
        await products_menu(query)
    elif data in {"digital", "software", "other"}:
        await show_category(query, data)
    elif data.startswith("product:"):
        await show_product(query, data.split(":", 1)[1])
    elif data.startswith("buy:"):
        await buy_product(query, data.split(":", 1)[1])
    elif data.startswith("category:"):
        await show_category(query, data.split(":", 1)[1])
    elif data == "verify":
        await begin_verification(query)
    elif data == "account":
        await account_menu(query)
    elif data == "orders":
        await orders_menu(query)
    elif data == "support":
        await query.edit_message_text(
            "🛟 *Support*\n\n"
            "Please contact the bot administrator for assistance.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]
            ]),
        )


async def post_init(application: Application):
    # Useful indexes for MongoDB queries.
    await users.create_index("telegram_user_id", unique=True)
    await verifications.create_index("telegram_user_id", unique=True)
    await products.create_index([("category", 1), ("active", 1)])
    await orders.create_index([("telegram_user_id", 1), ("created_at", -1)])
    logger.info("MongoDB indexes ready.")


def main():
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("DaemonTargaryen started.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
