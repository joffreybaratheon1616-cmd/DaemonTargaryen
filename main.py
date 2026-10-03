import os
import logging
from html import escape
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
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
    ApplicationHandlerStop,
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

# Users must be members of both communities to use the bot.
REQUIRED_GROUP = "@marketproducts01"
REQUIRED_CHANNEL = "@marketproducts02"
REQUIRED_GROUP_LINK = "https://t.me/marketproducts01"
REQUIRED_CHANNEL_LINK = "https://t.me/marketproducts02"

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
                "banned": False,
                "created_at": now(),
            },
        },
        upsert=True,
    )


async def get_user(user_id: int):
    return await users.find_one({"telegram_user_id": user_id})


async def is_banned(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    user = await get_user(user_id)
    return bool(user and user.get("banned", False))


async def is_required_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Check that a user is currently a member of both required chats."""
    try:
        group_member = await context.bot.get_chat_member(REQUIRED_GROUP, user_id)
        channel_member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
    except Exception:
        logger.exception("Unable to verify required Telegram membership")
        return False

    allowed = {"creator", "administrator", "member"}
    group_ok = group_member.status in allowed
    channel_ok = channel_member.status in allowed

    # Restricted users are valid only when Telegram still marks them as members.
    if group_member.status == "restricted":
        group_ok = bool(getattr(group_member, "is_member", False))
    if channel_member.status == "restricted":
        channel_ok = bool(getattr(channel_member, "is_member", False))

    return group_ok and channel_ok


async def membership_message(update: Update):
    message = update.effective_message
    if message:
        await message.reply_text(
            "🔒 <b>Membership Required</b>\n\n"
            "You must be a member of both the required group and channel "
            "to use this bot.\n\n"
            f"👥 Group: {REQUIRED_GROUP_LINK}\n"
            f"📢 Channel: {REQUIRED_CHANNEL_LINK}\n\n"
            "After joining both, send /start again.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def membership_message_callback(update: Update):
    q = update.callback_query
    if q:
        await q.answer(
            "🔒 Join the required group and channel to use this bot.",
            show_alert=True,
        )


async def banned_message(update: Update):
    message = update.effective_message
    if message:
        await message.reply_text("🚫 Your account is banned from using this bot.")


async def banned_message_callback(update: Update):
    q = update.callback_query
    if q:
        await q.answer("🚫 Your account is banned from using this bot.", show_alert=True)


async def access_guard_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    await ensure_user(user)

    if await is_banned(user.id):
        await banned_message(update)
        raise ApplicationHandlerStop

    if not await is_required_member(context, user.id):
        await membership_message(update)
        raise ApplicationHandlerStop


async def access_guard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return

    await ensure_user(q.from_user)

    if await is_banned(q.from_user.id):
        await banned_message_callback(update)
        raise ApplicationHandlerStop

    if not await is_required_member(context, q.from_user.id):
        await membership_message_callback(update)
        raise ApplicationHandlerStop


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
    verified = (
        await verifications.find_one({"telegram_user_id": q.from_user.id})
    ) is not None
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
    items = await products.find({"active": True}).sort(
        "created_at", -1
    ).limit(30).to_list(length=30)

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
                [
                    InlineKeyboardButton(
                        f"{p.get('name', 'Product')} — ₹{price:.2f}",
                        callback_data=f"product:{p['_id']}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
        text = "🛒 <b>Products</b>\n\nChoose a product:"
        markup = InlineKeyboardMarkup(rows)

    await q.edit_message_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup
    )


async def product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid product.", show_alert=True)

    p = await products.find_one({"_id": pid, "active": True})
    if not p:
        return await q.answer("Product unavailable.", show_alert=True)

    await q.edit_message_text(
        f"🛍️ <b>{p.get('name', 'Product')}</b>\n\n"
        f"{p.get('description', '')}\n\n"
        f"💰 Price: <b>₹{money(p.get('price', 0)):.2f}</b>",
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

    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid product.", show_alert=True)

    # Re-read active inventory at purchase time.
    p = await products.find_one({"_id": pid, "active": True})
    if not p:
        return await q.answer("Product unavailable.", show_alert=True)

    price = money(p.get("price", 0))
    if price < 0:
        return await q.answer("Invalid product price.", show_alert=True)

    user = await get_user(q.from_user.id)
    balance = money((user or {}).get("balance", 0))
    if balance < price:
        return await q.answer(
            f"Insufficient balance. Need ₹{price - balance:.2f} more.",
            show_alert=True,
        )

    # First deduct the balance atomically.
    balance_result = await users.update_one(
        {
            "telegram_user_id": q.from_user.id,
            "balance": {"$gte": price},
            "banned": {"$ne": True},
        },
        {"$inc": {"balance": -price}, "$set": {"updated_at": now()}},
    )
    if balance_result.modified_count != 1:
        return await q.answer(
            "Purchase could not be completed.", show_alert=True
        )

    # Claim the product atomically. Only one buyer can successfully
    # change active=True to active=False.
    claim_result = await products.update_one(
        {"_id": pid, "active": True},
        {
            "$set": {
                "active": False,
                "sold": True,
                "sold_to": q.from_user.id,
                "sold_at": now(),
            }
        },
    )

    if claim_result.modified_count != 1:
        # Product was taken by another buyer after the balance deduction.
        # Refund the exact amount and do not create an order.
        await users.update_one(
            {"telegram_user_id": q.from_user.id},
            {"$inc": {"balance": price}, "$set": {"updated_at": now()}},
        )
        return await q.answer(
            "This product was just sold to another user. Your balance was refunded.",
            show_alert=True,
        )

    try:
        order = {
            "telegram_user_id": q.from_user.id,
            "product_id": pid,
            # Snapshot these fields so Order History still has the details
            # after the product is removed from active inventory.
            "product_name": p.get("name", "Product"),
            "product_description": p.get("description", ""),
            "product_category": p.get("category", ""),
            "product_how_to_use": p.get("how_to_use", ""),
            "license_code": p.get("license_code", ""),
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
                "description": f"Purchase: {p.get('name', 'Product')}",
                "created_at": now(),
            }
        )
    except Exception:
        # If order/transaction persistence fails after inventory was claimed,
        # restore both inventory and the user's balance.
        await products.update_one(
            {
                "_id": pid,
                "sold": True,
                "sold_to": q.from_user.id,
            },
            {
                "$set": {"active": True},
                "$unset": {
                    "sold": "",
                    "sold_to": "",
                    "sold_at": "",
                },
            },
        )
        await users.update_one(
            {"telegram_user_id": q.from_user.id},
            {"$inc": {"balance": price}, "$set": {"updated_at": now()}},
        )
        logger.exception("Purchase persistence failed; purchase rolled back")
        return await q.answer(
            "Purchase failed and your balance was refunded.",
            show_alert=True,
        )

    # Notify both required communities. Notification failure is logged but
    # never rolls back an already successful purchase.
    order_id_text = str(order_result.inserted_id)
    buyer_username = f"@{q.from_user.username}" if q.from_user.username else "No username"
    purchased_at = order.get("created_at")
    if isinstance(purchased_at, datetime):
        if purchased_at.tzinfo is None:
            purchased_at = purchased_at.replace(tzinfo=timezone.utc)
        local_time = purchased_at.astimezone()
        date_text = local_time.strftime("%d %b %Y")
        time_text = local_time.strftime("%I:%M %p")
    else:
        date_text = "Unknown"
        time_text = "Unknown"

    notification = (
        "🛒 <b>NEW ORDER</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Product: {escape(str(p.get('name', 'Product')))}\n"
        f"🆔 Order ID: #{escape(order_id_text)}\n"
        f"💰 Amount: ₹{price:.2f}\n\n"
        "👤 <b>Buyer Information</b>\n"
        f"• User ID: <code>{q.from_user.id}</code>\n"
        f"• Username: {escape(buyer_username)}\n\n"
        f"📅 Date: {date_text}\n"
        f"⏰ Time: {time_text}\n\n"
        "✅ Payment Status: PAID\n"
        "📦 Order Status: COMPLETED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 Automated Order Notification"
    )

    for destination in (REQUIRED_GROUP, REQUIRED_CHANNEL):
        try:
            await context.bot.send_message(
                chat_id=destination,
                text=notification,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Failed to send purchase notification to %s", destination)

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
        f"📦 {p.get('name', 'Product')}\n"
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
    items = await orders.find(
        {"telegram_user_id": q.from_user.id}
    ).sort("created_at", -1).limit(20).to_list(length=20)

    if not items:
        text = "📜 <b>Orders</b>\n\nYou have no orders yet."
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]
        )
    else:
        lines = [
            "📜 <b>Order History</b>",
            "",
            "Select an order to view its full product details:",
            "",
        ]
        rows = []
        for o in items:
            name = str(o.get("product_name", "Product"))
            amount = money(o.get("amount", 0))
            status = str(o.get("status", "unknown")).upper()
            lines.append(f"• {escape(name)} — ₹{amount:.2f} — {escape(status)}")
            rows.append([
                InlineKeyboardButton(
                    f"📦 {name[:45]} — ₹{amount:.2f}",
                    callback_data=f"order:{o['_id']}",
                )
            ])
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(rows)

    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


async def order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    try:
        oid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid order.", show_alert=True)

    order = await orders.find_one({
        "_id": oid,
        "telegram_user_id": q.from_user.id,
    })
    if not order:
        return await q.answer("Order not found.", show_alert=True)

    created = order.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        created_text = created.astimezone().strftime("%d %b %Y, %I:%M %p")
    else:
        created_text = "Unknown"

    name = escape(str(order.get("product_name", "Product")))
    description = escape(str(order.get("product_description", "")).strip())
    category = escape(str(order.get("product_category", "")).strip())
    howto = escape(str(order.get("product_how_to_use", "")).strip())
    code = str(order.get("license_code", "")).strip()

    parts = [
        "📦 <b>Order Details</b>",
        "",
        f"📦 <b>Product:</b> {name}",
        f"🆔 <b>Order ID:</b> <code>{order['_id']}</code>",
        f"💰 <b>Amount:</b> ₹{money(order.get('amount', 0)):.2f}",
        f"📅 <b>Purchased:</b> {created_text}",
        f"📌 <b>Status:</b> {escape(str(order.get('status', 'unknown')).upper())}",
    ]
    if category:
        parts.append(f"🏷️ <b>Category:</b> {category}")
    if description:
        parts.extend(["", "📝 <b>Description:</b>", description])
    if howto:
        parts.extend(["", "📘 <b>How to Use:</b>", howto])
    if code and code.upper() != "NONE":
        parts.extend(["", "🔑 <b>License / Activation Code:</b>", f"<code>{escape(code)}</code>"])

    await q.edit_message_text(
        "\n".join(parts),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Order History", callback_data="orders")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
        ]),
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
        return await update.message.reply_text("Usage: /coupon CODE")

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
        "⚙️ <b>Admin Panel</b>\n\n"
        "User management is available through the commands:\n"
        "<code>/deduct USER_ID AMOUNT</code>\n"
        "<code>/ban USER_ID</code>\n"
        "<code>/unban USER_ID</code>\n"
        "<code>/showbal</code>",
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
    price = money(update.message.text.strip())
    if price < 0:
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
            "sold": False,
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

    uid = int(value)
    if uid == ADMIN_ID:
        await update.message.reply_text("❌ The admin account cannot be modified.")
        return ADD_BALANCE_USER

    context.user_data["balance_user"] = uid
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
                "balance": 0.0,
                "banned": False,
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


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 2:
        return await update.message.reply_text(
            "Usage: /deduct USER_ID AMOUNT"
        )

    if not context.args[0].isdigit():
        return await update.message.reply_text("❌ USER_ID must be numeric.")

    uid = int(context.args[0])
    if uid == ADMIN_ID:
        return await update.message.reply_text(
            "❌ The admin account is protected from deductions."
        )

    amount = money(context.args[1])
    if amount <= 0:
        return await update.message.reply_text(
            "❌ Amount must be greater than 0."
        )

    result = await users.update_one(
        {
            "telegram_user_id": uid,
            "balance": {"$gte": amount},
        },
        {
            "$inc": {"balance": -amount},
            "$set": {"updated_at": now()},
        },
    )

    if result.modified_count != 1:
        user = await get_user(uid)
        if not user:
            return await update.message.reply_text("❌ User not found.")
        return await update.message.reply_text(
            "❌ Deduction failed: user does not have enough balance."
        )

    await transactions.insert_one(
        {
            "telegram_user_id": uid,
            "type": "deduct",
            "amount": -amount,
            "description": "Manual admin deduction",
            "admin_id": update.effective_user.id,
            "created_at": now(),
        }
    )

    await update.message.reply_text(
        f"✅ ₹{amount:.2f} deducted from user <code>{uid}</code>.",
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_message(
            uid,
            f"💳 ₹{amount:.2f} was deducted from your balance by an admin.\n"
            "Please contact support if you believe this is incorrect.",
        )
    except Exception:
        pass


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /ban USER_ID")

    uid = int(context.args[0])
    if uid == ADMIN_ID:
        return await update.message.reply_text(
            "❌ The admin account cannot be banned."
        )

    user = await get_user(uid)
    if not user:
        return await update.message.reply_text("❌ User not found.")

    await users.update_one(
        {"telegram_user_id": uid},
        {"$set": {"banned": True, "updated_at": now()}},
    )

    await update.message.reply_text(
        f"🚫 User <code>{uid}</code> has been banned.",
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_message(
            uid,
            "🚫 Your account has been banned from using this bot.",
        )
    except Exception:
        pass


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /unban USER_ID")

    uid = int(context.args[0])
    if uid == ADMIN_ID:
        return await update.message.reply_text(
            "ℹ️ The admin account is always protected."
        )

    user = await get_user(uid)
    if not user:
        return await update.message.reply_text("❌ User not found.")

    await users.update_one(
        {"telegram_user_id": uid},
        {"$set": {"banned": False, "updated_at": now()}},
    )

    await update.message.reply_text(
        f"✅ User <code>{uid}</code> has been unbanned.",
        parse_mode=ParseMode.HTML,
    )

    try:
        await context.bot.send_message(
            uid,
            "✅ Your account has been unbanned. You can use the bot again.",
        )
    except Exception:
        pass


async def showbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    lines = ["👥 <b>Registered Users</b>\n"]
    cursor = users.find({}).sort("telegram_user_id", 1)

    count = 0
    async for user in cursor:
        uid = user.get("telegram_user_id")
        username = user.get("username") or "none"
        balance = money(user.get("balance", 0))
        status = "🚫 Banned" if user.get("banned", False) else "✅ Active"

        lines.append(
            f"🆔 <code>{uid}</code>\n"
            f"👤 @{username}\n"
            f"💰 ₹{balance:.2f}\n"
            f"📌 {status}\n"
        )
        count += 1

        # Telegram message limit protection.
        if sum(len(x) for x in lines) > 3500:
            await update.message.reply_text(
                "\n".join(lines),
                parse_mode=ParseMode.HTML,
            )
            lines = ["👥 <b>Registered Users (continued)</b>\n"]

    if count == 0:
        return await update.message.reply_text("No registered users.")

    if len(lines) > 1:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )


async def manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    items = await products.find({}).sort(
        "created_at", -1
    ).limit(30).to_list(length=30)

    rows = []
    for p in items:
        if p.get("active"):
            state = "🟢"
        elif p.get("sold"):
            state = "💰"
        else:
            state = "🔴"

        rows.append(
            [
                InlineKeyboardButton(
                    f"{state} {p.get('name', 'Product')}",
                    callback_data=f"toggle:{p['_id']}",
                ),
                InlineKeyboardButton(
                    "🗑️",
                    callback_data=f"delete:{p['_id']}",
                ),
            ]
        )

    rows.append([InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")])

    await q.edit_message_text(
        "🛠 <b>Manage Products</b>\n\n"
        "🟢 Available  •  💰 Sold  •  🔴 Disabled\n\n"
        "A successfully sold product is automatically removed from "
        "the available product list.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def toggle_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return

    p = await products.find_one({"_id": pid})
    if not p:
        return

    if p.get("sold"):
        return await q.answer(
            "Sold products cannot be re-enabled. Add a new inventory item.",
            show_alert=True,
        )

    await products.update_one(
        {"_id": pid},
        {"$set": {"active": not p.get("active", False)}},
    )
    await manage_products(update, context)


async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

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
    sold_products = await products.count_documents({"sold": True})
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
        f"📦 Products: {total_products}\n"
        f"🟢 Available: {active_products}\n"
        f"💰 Sold: {sold_products}\n"
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
        f"💰 Balance: ₹{money(user.get('balance', 0)):.2f}\n"
        f"📦 Orders: {order_count}\n"
        f"Username: @{user.get('username') or 'none'}\n"
        f"Status: {'🚫 Banned' if user.get('banned') else '✅ Active'}",
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

    items = await tickets.find(
        {"status": "open"}
    ).sort("created_at", -1).limit(20).to_list(length=20)

    if not items:
        text = "🎫 No open tickets."
    else:
        lines = ["🎫 <b>Open Tickets</b>\n"]
        for t in items:
            lines.append(
                f"<code>{t['_id']}</code> — "
                f"user <code>{t['telegram_user_id']}</code>\n"
                f"{t.get('message', '')[:150]}\n"
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

    items = await transactions.find({}).sort(
        "created_at", -1
    ).limit(20).to_list(length=20)

    if not items:
        text = "🧾 No transactions."
    else:
        lines = ["🧾 <b>Recent Transactions</b>\n"]
        for t in items:
            lines.append(
                f"• <code>{t.get('telegram_user_id')}</code> | "
                f"{t.get('type')} | ₹{money(t.get('amount', 0)):.2f}"
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
    await users.create_index("banned")
    await verifications.create_index("telegram_user_id", unique=True)
    await products.create_index([("category", 1), ("active", 1)])
    await products.create_index([("active", 1), ("created_at", -1)])
    await orders.create_index([("telegram_user_id", 1), ("created_at", -1)])
    await orders.create_index([("product_id", 1)])
    await transactions.create_index(
        [("telegram_user_id", 1), ("created_at", -1)]
    )
    await coupons.create_index("code", unique=True)
    await tickets.create_index([("status", 1), ("created_at", -1)])


def build_application():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Stop processing entirely when the user is banned or is not a member
    # of both required communities. The admin remains protected from the
    # ban/deduction commands, but must also satisfy the membership gate.
    app.add_handler(
        MessageHandler(filters.ALL, access_guard_message),
        group=-1,
    )
    app.add_handler(
        CallbackQueryHandler(access_guard_callback, pattern=r".*"),
        group=-1,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("coupon", use_coupon))

    # Requested admin user-management commands.
    # There is intentionally NO /balance command.
    app.add_handler(CommandHandler("deduct", deduct_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("showbal", showbal_command))

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    admin_add_product_start, "^adm_add_product$"
                )
            ],
            states={
                ADD_PRODUCT_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_name)
                ],
                ADD_PRODUCT_PRICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_price)
                ],
                ADD_PRODUCT_CATEGORY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_category)
                ],
                ADD_PRODUCT_DESCRIPTION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_description)
                ],
                ADD_PRODUCT_HOWTO: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_howto)
                ],
                ADD_PRODUCT_CODE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_product_code)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(admin_balance_start, "^adm_balance$")
            ],
            states={
                ADD_BALANCE_USER: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_balance_user)
                ],
                ADD_BALANCE_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, add_balance_amount)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(admin_coupon_start, "^adm_coupon$")
            ],
            states={
                COUPON_CODE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_code)
                ],
                COUPON_DISCOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, coupon_discount)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(admin_broadcast_start, "^adm_broadcast$")
            ],
            states={
                BROADCAST_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_text)
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
            per_user=True,
            per_chat=True,
        )
    )

    app.add_handler(
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(new_ticket, "^new_ticket$")
            ],
            states={
                TICKET_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket)
                ],
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
    app.add_handler(CallbackQueryHandler(order_detail, "^order:"))
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

    # Admin user lookup is after conversation handlers.
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, admin_lookup_message)
    )

    return app


if __name__ == "__main__":
    application = build_application()
    logger.info("DaemonTargaryen starting...")
    application.run_polling(drop_pending_updates=True)
