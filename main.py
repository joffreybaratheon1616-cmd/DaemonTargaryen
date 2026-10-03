import os
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from motor.motor_asyncio import AsyncIOMotorClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("DaemonTargaryen")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "DaemonTargaryen").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

TOPUP_USERNAME = "serbryndentully"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing or invalid")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]

users = db.users
products = db.products
orders = db.orders
transactions = db.transactions
coupons = db.coupons
tickets = db.tickets
verifications = db.ownership_verifications

# Conversation states
(
    ADD_PRODUCT_NAME,
    ADD_PRODUCT_PRICE,
    ADD_PRODUCT_CATEGORY,
    ADD_PRODUCT_DESCRIPTION,
    ADD_PRODUCT_HOWTO,
    ADD_PRODUCT_CODE,
    ADD_BALANCE_USER,
    ADD_BALANCE_AMOUNT,
    COUPON_CODE,
    COUPON_DISCOUNT,
    BROADCAST_TEXT,
    TICKET_TEXT,
) = range(12)


def now():
    return datetime.now(timezone.utc)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def money(value) -> float:
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


async def ensure_user(user):
    await users.update_one(
        {"telegram_user_id": user.id},
        {
            "$set": {
                "first_name": user.first_name or "",
                "username": user.username or "",
                "updated_at": now(),
            },
            "$setOnInsert": {
                "telegram_user_id": user.id,
                "balance": 0.0,
                "created_at": now(),
            },
        },
        upsert=True,
    )


async def get_user(user_id: int):
    return await users.find_one({"telegram_user_id": user_id})


def main_menu(user_id: int):
    rows = [
        [
            InlineKeyboardButton("🔐 Verify Account", callback_data="verify"),
            InlineKeyboardButton("👤 My Account", callback_data="account"),
        ],
        [
            InlineKeyboardButton("🛒 Products", callback_data="products"),
            InlineKeyboardButton("📜 Orders", callback_data="orders"),
        ],
        [
            InlineKeyboardButton("💳 Top Up Balance", callback_data="topup"),
            InlineKeyboardButton("🎟️ Coupons", callback_data="coupon_help"),
        ],
        [InlineKeyboardButton("🛟 Support", callback_data="support")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user)
    await update.message.reply_text(
        "💎 <b>DaemonTargaryen</b>\n\n"
        "Welcome back! 👋\n\n"
        "Choose an option below:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(user.id),
    )


async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = await get_user(q.from_user.id)
    balance = money((user or {}).get("balance", 0))
    verified = (await verifications.find_one(
        {"telegram_user_id": q.from_user.id}
    )) is not None
    await q.edit_message_text(
        f"👤 <b>My Account</b>\n\n"
        f"🆔 ID: <code>{q.from_user.id}</code>\n"
        f"💰 Balance: <b>₹{balance:.2f}</b>\n"
        f"🛡️ Verification: {'✅ Verified' if verified else '❌ Not verified'}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        ),
    )


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await verifications.update_one(
        {"telegram_user_id": q.from_user.id},
        {
            "$set": {
                "telegram_user_id": q.from_user.id,
                "username": q.from_user.username or "",
                "verified_at": now(),
            }
        },
        upsert=True,
    )
    await q.edit_message_text(
        "🛡️ <b>Verification Complete</b>\n\n"
        "Your current Telegram account has been marked as verified.\n\n"
        "This verification does not request or store passwords, OTPs, "
        "session strings, or authentication keys.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        ),
    )


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "💳 <b>Top Up Balance</b>\n\n"
        f"To add balance, please message @{TOPUP_USERNAME}.\n\n"
        "Send your Telegram ID and the amount you want to add. "
        "An admin can then process the top-up manually.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💬 Message Top-Up Admin",
                        url=f"https://t.me/{TOPUP_USERNAME}",
                    )
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
            ]
        ),
    )


async def products_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cursor = products.find({"active": True}).sort("created_at", -1).limit(30)
    items = await cursor.to_list(length=30)
    if not items:
        text = "🛒 <b>Products</b>\n\nNo products are available right now."
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        )
    else:
        rows = []
        for p in items:
            price = money(p.get("price", 0))
            rows.append(
                [InlineKeyboardButton(
                    f"{p.get('name','Product')} — ₹{price:.2f}",
                    callback_data=f"product:{p['_id']}",
                )]
            )
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
        text = "🛒 <b>Products</b>\n\nChoose a product:"
        markup = InlineKeyboardMarkup(rows)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    from bson import ObjectId

    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid product.", show_alert=True)

    p = await products.find_one({"_id": pid, "active": True})
    if not p:
        return await q.answer("Product unavailable.", show_alert=True)

    await q.edit_message_text(
        f"🛍️ <b>{p.get('name','Product')}</b>\n\n"
        f"{p.get('description','')}\n\n"
        f"💰 Price: <b>₹{money(p.get('price',0)):.2f}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy:{pid}")],
                [InlineKeyboardButton("◀️ Products", callback_data="products")],
            ]
        ),
    )


async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    from bson import ObjectId

    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid product.", show_alert=True)

    p = await products.find_one({"_id": pid, "active": True})
    if not p:
        return await q.answer("Product unavailable.", show_alert=True)

    price = money(p.get("price", 0))
    user = await get_user(q.from_user.id)
    balance = money((user or {}).get("balance", 0))

    if balance < price:
        return await q.answer(
            f"Insufficient balance. Need ₹{price - balance:.2f} more.",
            show_alert=True,
        )

    # Atomic balance deduction prevents negative balance from concurrent purchases.
    result = await users.update_one(
        {
            "telegram_user_id": q.from_user.id,
            "balance": {"$gte": price},
        },
        {"$inc": {"balance": -price}},
    )
    if result.modified_count != 1:
        return await q.answer("Purchase could not be completed.", show_alert=True)

    order = {
        "telegram_user_id": q.from_user.id,
        "product_id": pid,
        "product_name": p.get("name", "Product"),
        "amount": price,
        "status": "paid",
        "created_at": now(),
    }
    order_result = await orders.insert_one(order)

    await transactions.insert_one(
        {
            "telegram_user_id": q.from_user.id,
            "type": "purchase",
            "amount": -price,
            "order_id": order_result.inserted_id,
            "description": f"Purchase: {p.get('name','Product')}",
            "created_at": now(),
        }
    )

    code = p.get("license_code", "").strip()
    delivery = (
        f"\n\n🔑 <b>License / Activation Code:</b>\n<code>{code}</code>"
        if code and code.upper() != "NONE"
        else ""
    )
    howto = p.get("how_to_use", "").strip()
    instructions = f"\n\n📘 <b>How to Use:</b>\n{howto}" if howto else ""

    await q.edit_message_text(
        f"✅ <b>Purchase Successful</b>\n\n"
        f"📦 {p.get('name','Product')}\n"
        f"🧾 Order ID: <code>{order_result.inserted_id}</code>\n"
        f"💰 Paid: ₹{price:.2f}"
        f"{delivery}{instructions}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        ),
    )


async def orders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cursor = orders.find(
        {"telegram_user_id": q.from_user.id}
    ).sort("created_at", -1).limit(20)
    items = await cursor.to_list(length=20)

    if not items:
        text = "📜 <b>Orders</b>\n\nYou have no orders yet."
    else:
        lines = ["📜 <b>Order History</b>\n"]
        for o in items:
            lines.append(
                f"• {o.get('product_name','Product')} — "
                f"₹{money(o.get('amount',0)):.2f} — {o.get('status','unknown')}"
            )
        text = "\n".join(lines)

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        ),
    )


async def coupon_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🎟️ <b>Coupons</b>\n\n"
        "If you have a coupon code, use:\n"
        "<code>/coupon YOURCODE</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        ),
    )


async def use_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    if not context.args:
        return await update.message.reply_text(
            "Usage: /coupon CODE"
        )

    code = context.args[0].strip().upper()
    c = await coupons.find_one({"code": code, "active": True})
    if not c:
        return await update.message.reply_text("❌ Invalid or inactive coupon.")

    existing = await transactions.find_one(
        {"telegram_user_id": update.effective_user.id, "coupon_code": code}
    )
    if existing:
        return await update.message.reply_text("❌ You already used this coupon.")

    discount = money(c.get("amount", 0))
    await users.update_one(
        {"telegram_user_id": update.effective_user.id},
        {"$inc": {"balance": discount}},
    )
    await transactions.insert_one(
        {
            "telegram_user_id": update.effective_user.id,
            "type": "coupon",
            "amount": discount,
            "coupon_code": code,
            "description": "Coupon credit",
            "created_at": now(),
        }
    )
    await update.message.reply_text(
        f"✅ Coupon applied. ₹{discount:.2f} has been added to your balance."
    )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🛟 <b>Support</b>\n\n"
        "Use the button below to open a support ticket.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🎫 Create Ticket", callback_data="new_ticket")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
            ]
        ),
    )


async def new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🎫 Send your support message in your next message."
    )
    return TICKET_TEXT


async def receive_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ticket = {
        "telegram_user_id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "message": text,
        "status": "open",
        "created_at": now(),
    }
    result = await tickets.insert_one(ticket)
    await update.message.reply_text(
        f"🎫 Ticket created.\nTicket ID: <code>{result.inserted_id}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )
    try:
        await context.bot.send_message(
            ADMIN_ID,
            f"🛟 <b>New Support Ticket</b>\n\n"
            f"ID: <code>{result.inserted_id}</code>\n"
            f"User: <code>{update.effective_user.id}</code>\n"
            f"@{update.effective_user.username or 'no_username'}\n\n"
            f"{text}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Could not notify admin about ticket")
    return ConversationHandler.END


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return await q.answer("Admin only.", show_alert=True)

    await q.edit_message_text(
        "⚙️ <b>Admin Panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add Product", callback_data="adm_add_product")],
                [InlineKeyboardButton("🛠 Manage Products", callback_data="adm_products")],
                [InlineKeyboardButton("💰 Add Balance", callback_data="adm_balance")],
                [InlineKeyboardButton("🎟 Create Coupon", callback_data="adm_coupon")],
                [InlineKeyboardButton("📊 Statistics", callback_data="adm_stats")],
                [InlineKeyboardButton("👤 User Lookup", callback_data="adm_user")],
                [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
                [InlineKeyboardButton("🎫 Open Tickets", callback_data="adm_tickets")],
                [InlineKeyboardButton("🧾 Transactions", callback_data="adm_transactions")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
            ]
        ),
    )


async def admin_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    context.user_data.clear()
    await q.edit_message_text("➕ Enter product name:")
    return ADD_PRODUCT_NAME


async def add_product_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["product_name"] = update.message.text.strip()
    await update.message.reply_text("Enter price in ₹:")
    return ADD_PRODUCT_PRICE


async def add_product_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = money(update.message.text.strip())
        if price < 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Enter a valid price, e.g. 250")
        return ADD_PRODUCT_PRICE
    context.user_data["price"] = price
    await update.message.reply_text("Category: digital / software / other")
    return ADD_PRODUCT_CATEGORY


async def add_product_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = update.message.text.strip().lower()
    if category not in {"digital", "software", "other"}:
        await update.message.reply_text("Use: digital, software, or other")
        return ADD_PRODUCT_CATEGORY
    context.user_data["category"] = category
    await update.message.reply_text("Enter product description:")
    return ADD_PRODUCT_DESCRIPTION


async def add_product_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text.strip()
    await update.message.reply_text("Enter 'How to Use' instructions:")
    return ADD_PRODUCT_HOWTO


async def add_product_howto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["how_to_use"] = update.message.text.strip()
    await update.message.reply_text(
        "Enter the product license/activation code.\n"
        "If there is none, type NONE."
    )
    return ADD_PRODUCT_CODE


async def add_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["license_code"] = update.message.text.strip()
    data = context.user_data

    await products.insert_one(
        {
            "name": data["product_name"],
            "price": data["price"],
            "category": data["category"],
            "description": data["description"],
            "how_to_use": data["how_to_use"],
            "license_code": data["license_code"],
            "active": True,
            "created_at": now(),
        }
    )
    context.user_data.clear()
    await update.message.reply_text(
        "✅ Product added successfully.",
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def admin_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.edit_message_text("💰 Enter the user's Telegram numeric ID:")
    return ADD_BALANCE_USER


async def add_balance_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value.isdigit():
        await update.message.reply_text("❌ Enter a numeric Telegram user ID.")
        return ADD_BALANCE_USER
    context.user_data["balance_user"] = int(value)
    await update.message.reply_text("Enter amount to add in ₹:")
    return ADD_BALANCE_AMOUNT


async def add_balance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = money(update.message.text.strip())
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return ADD_BALANCE_AMOUNT

    uid = context.user_data["balance_user"]
    await users.update_one(
        {"telegram_user_id": uid},
        {
            "$inc": {"balance": amount},
            "$setOnInsert": {
                "telegram_user_id": uid,
                "created_at": now(),
            },
            "$set": {"updated_at": now()},
        },
        upsert=True,
    )
    await transactions.insert_one(
        {
            "telegram_user_id": uid,
            "type": "topup",
            "amount": amount,
            "description": "Manual admin top-up",
            "created_at": now(),
            "admin_id": update.effective_user.id,
        }
    )
    try:
        await context.bot.send_message(
            uid,
            f"💰 Your balance was topped up by ₹{amount:.2f}.\n"
            "You can now use it for purchases.",
        )
    except Exception:
        pass

    context.user_data.clear()
    await update.message.reply_text(
        f"✅ ₹{amount:.2f} added to user <code>{uid}</code>.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    cursor = products.find({}).sort("created_at", -1).limit(30)
    items = await cursor.to_list(length=30)
    rows = []
    for p in items:
        state = "🟢" if p.get("active") else "🔴"
        rows.append([
            InlineKeyboardButton(
                f"{state} {p.get('name','Product')}",
                callback_data=f"toggle:{p['_id']}",
            ),
            InlineKeyboardButton(
                "🗑️",
                callback_data=f"delete:{p['_id']}",
            ),
        ])
    rows.append([InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")])
    await q.edit_message_text(
        "🛠 <b>Manage Products</b>\n\nTap a product to enable/disable it. "
        "Use 🗑️ to delete.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def toggle_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    from bson import ObjectId
    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return
    p = await products.find_one({"_id": pid})
    if not p:
        return
    await products.update_one(
        {"_id": pid}, {"$set": {"active": not p.get("active", False)}}
    )
    await manage_products(update, context)


async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    from bson import ObjectId
    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return
    await products.delete_one({"_id": pid})
    await manage_products(update, context)


async def admin_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.edit_message_text("🎟 Enter coupon code:")
    return COUPON_CODE


async def coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    if not code or " " in code:
        await update.message.reply_text("❌ Use a simple code without spaces.")
        return COUPON_CODE
    context.user_data["coupon_code"] = code
    await update.message.reply_text("Enter the balance credit amount in ₹:")
    return COUPON_DISCOUNT


async def coupon_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = money(update.message.text.strip())
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be greater than 0.")
        return COUPON_DISCOUNT
    await coupons.update_one(
        {"code": context.user_data["coupon_code"]},
        {
            "$set": {
                "code": context.user_data["coupon_code"],
                "amount": amount,
                "active": True,
                "created_at": now(),
            }
        },
        upsert=True,
    )
    code = context.user_data["coupon_code"]
    context.user_data.clear()
    await update.message.reply_text(
        f"✅ Coupon <code>{code}</code> created for ₹{amount:.2f}.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    total_users = await users.count_documents({})
    total_products = await products.count_documents({})
    active_products = await products.count_documents({"active": True})
    total_orders = await orders.count_documents({})
    total_tickets = await tickets.count_documents({"status": "open"})

    pipeline = [
        {"$match": {"type": "purchase"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    sales = 0.0
    async for row in transactions.aggregate(pipeline):
        sales = money(row.get("total", 0))

    await q.edit_message_text(
        f"📊 <b>Statistics</b>\n\n"
        f"👤 Users: {total_users}\n"
        f"📦 Products: {total_products} ({active_products} active)\n"
        f"🧾 Orders: {total_orders}\n"
        f"💰 Sales: ₹{sales:.2f}\n"
        f"🎫 Open tickets: {total_tickets}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")]]
        ),
    )


async def admin_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    await q.edit_message_text("Send the user's Telegram numeric ID:")
    context.user_data["admin_lookup"] = True


async def admin_lookup_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.user_data.get("admin_lookup"):
        return
    text = update.message.text.strip()
    if not text.isdigit():
        return await update.message.reply_text("Send a numeric user ID.")
    uid = int(text)
    user = await get_user(uid)
    if not user:
        return await update.message.reply_text("User not found.")
    order_count = await orders.count_documents({"telegram_user_id": uid})
    await update.message.reply_text(
        f"👤 User <code>{uid}</code>\n"
        f"💰 Balance: ₹{money(user.get('balance',0)):.2f}\n"
        f"📦 Orders: {order_count}\n"
        f"Username: @{user.get('username') or 'none'}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )
    context.user_data.pop("admin_lookup", None)


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    await q.edit_message_text("📢 Send the broadcast text:")
    return BROADCAST_TEXT


async def broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text
    cursor = users.find({}, {"telegram_user_id": 1})
    sent = failed = 0
    async for u in cursor:
        uid = u["telegram_user_id"]
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast finished.\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}",
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def admin_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    cursor = tickets.find({"status": "open"}).sort("created_at", -1).limit(20)
    items = await cursor.to_list(length=20)
    if not items:
        text = "🎫 No open tickets."
    else:
        lines = ["🎫 <b>Open Tickets</b>\n"]
        for t in items:
            lines.append(
                f"<code>{t['_id']}</code> — "
                f"user <code>{t['telegram_user_id']}</code>\n"
                f"{t.get('message','')[:150]}\n"
            )
        text = "\n".join(lines)
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")]]
        ),
    )


async def admin_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    cursor = transactions.find({}).sort("created_at", -1).limit(20)
    items = await cursor.to_list(length=20)
    if not items:
        text = "🧾 No transactions."
    else:
        lines = ["🧾 <b>Recent Transactions</b>\n"]
        for t in items:
            lines.append(
                f"• <code>{t.get('telegram_user_id')}</code> | "
                f"{t.get('type')} | ₹{money(t.get('amount',0)):.2f}"
            )
        text = "\n".join(lines)
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")]]
        ),
    )


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "💎 <b>DaemonTargaryen</b>\n\nChoose an option:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(q.from_user.id),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=main_menu(update.effective_user.id),
    )
    return ConversationHandler.END


async def post_init(application: Application):
    await users.create_index("telegram_user_id", unique=True)
    await verifications.create_index("telegram_user_id", unique=True)
    await products.create_index([("category", 1), ("active", 1)])
    await orders.create_index([("telegram_user_id", 1), ("created_at", -1)])
    await transactions.create_index([("telegram_user_id", 1), ("created_at", -1)])
    await coupons.create_index("code", unique=True)
    await tickets.create_index([("status", 1), ("created_at", -1)])


def build_application():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("coupon", use_coupon))

    # Product creation
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_add_product_start, "^adm_add_product$")],
            states={
                ADD_PRODUCT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)],
                ADD_PRODUCT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)],
                ADD_PRODUCT_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_category)],
                ADD_PRODUCT_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_description)],
                ADD_PRODUCT_HOWTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_howto)],
                ADD_PRODUCT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_code)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    # Admin balance
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_balance_start, "^adm_balance$")],
            states={
                ADD_BALANCE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_balance_user)],
                ADD_BALANCE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_balance_amount)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    # Coupon creation
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_coupon_start, "^adm_coupon$")],
            states={
                COUPON_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_code)],
                COUPON_DISCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_discount)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    # Broadcast
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(admin_broadcast_start, "^adm_broadcast$")],
            states={
                BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_text)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    # Support ticket
    app.add_handler(
        ConversationHandler(
            entry_points=[CallbackQueryHandler(new_ticket, "^new_ticket$")],
            states={
                TICKET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(CallbackQueryHandler(account, "^account$"))
    app.add_handler(CallbackQueryHandler(verify, "^verify$"))
    app.add_handler(CallbackQueryHandler(topup, "^topup$"))
    app.add_handler(CallbackQueryHandler(products_menu, "^products$"))
    app.add_handler(CallbackQueryHandler(product_detail, "^product:"))
    app.add_handler(CallbackQueryHandler(buy_product, "^buy:"))
    app.add_handler(CallbackQueryHandler(orders_menu, "^orders$"))
    app.add_handler(CallbackQueryHandler(coupon_help, "^coupon_help$"))
    app.add_handler(CallbackQueryHandler(support, "^support$"))
    app.add_handler(CallbackQueryHandler(admin_panel, "^admin$"))
    app.add_handler(CallbackQueryHandler(manage_products, "^adm_products$"))
    app.add_handler(CallbackQueryHandler(toggle_product, "^toggle:"))
    app.add_handler(CallbackQueryHandler(delete_product, "^delete:"))
    app.add_handler(CallbackQueryHandler(admin_stats, "^adm_stats$"))
    app.add_handler(CallbackQueryHandler(admin_user_start, "^adm_user$"))
    app.add_handler(CallbackQueryHandler(admin_tickets, "^adm_tickets$"))
    app.add_handler(CallbackQueryHandler(admin_transactions, "^adm_transactions$"))
    app.add_handler(CallbackQueryHandler(home, "^home$"))

    # Admin user lookup is deliberately after conversation handlers.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_lookup_message))

    return app


if __name__ == "__main__":
    application = build_application()
    logger.info("DaemonTargaryen starting...")
    application.run_polling(drop_pending_updates=True)
