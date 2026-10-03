import os
import re
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
    ApplicationHandlerStop,
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
TOPUP_USERNAME = os.getenv("TOPUP_USERNAME", "serbryndentully").strip().lstrip("@")

REQUIRED_GROUP = "@marketproducts01"
REQUIRED_CHANNEL = "@marketproducts02"
REQUIRED_GROUP_LINK = "https://t.me/marketproducts01"
REQUIRED_CHANNEL_LINK = "https://t.me/marketproducts02"

MAX_COUPON_AMOUNT = 10_000_000.00       # 1 crore
MAX_BALANCE = 1_000_000_000.00          # 100 crore

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
audit_logs = db.audit_logs

# Conversation states.
(
    ADD_PRODUCT_NAME, ADD_PRODUCT_PRICE, ADD_PRODUCT_CATEGORY,
    ADD_PRODUCT_DESCRIPTION, ADD_PRODUCT_HOWTO, ADD_PRODUCT_CODE,
    ADD_PRODUCT_CONTENT,
    ADD_BALANCE_USER, ADD_BALANCE_AMOUNT,
    COUPON_CODE, COUPON_DISCOUNT,
    BROADCAST_TEXT,
    TICKET_TEXT,
) = range(13)


def now():
    return datetime.now(timezone.utc)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def money(value) -> float:
    try:
        return float(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


def h(value) -> str:
    return escape(str(value or ""))


def within_balance_limit(value: float) -> bool:
    return 0 <= value <= MAX_BALANCE


def main_menu(user_id: int):
    rows = [
        [InlineKeyboardButton("🔐 Verify Account", callback_data="verify"),
         InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("🛒 Products", callback_data="products"),
         InlineKeyboardButton("📜 Orders", callback_data="orders")],
        [InlineKeyboardButton("💳 Top Up Balance", callback_data="topup"),
         InlineKeyboardButton("🎟️ Coupons", callback_data="coupon_help")],
        [InlineKeyboardButton("🛟 Support", callback_data="support")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


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
    user = await get_user(user_id)
    return bool(user and user.get("banned", False))


async def required_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        gm = await context.bot.get_chat_member(REQUIRED_GROUP, user_id)
        cm = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
    except Exception:
        logger.exception("Membership verification failed")
        return False
    allowed = {"creator", "administrator", "member"}
    group_ok = gm.status in allowed or (gm.status == "restricted" and bool(getattr(gm, "is_member", False)))
    channel_ok = cm.status in allowed or (cm.status == "restricted" and bool(getattr(cm, "is_member", False)))
    return group_ok and channel_ok


def join_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Join Group", url=REQUIRED_GROUP_LINK)],
        [InlineKeyboardButton("📢 Join Channel", url=REQUIRED_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Check Membership", callback_data="check_membership")],
    ])


async def membership_message(update: Update):
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "🔒 <b>Membership Required</b>\n\n"
            "You must be a member of <b>both</b> the required group and channel to use this bot.\n\n"
            "Join both, then tap <b>Check Membership</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_markup(),
        )


async def banned_message(update: Update):
    msg = update.effective_message
    if msg:
        await msg.reply_text("🚫 Your account is banned from using this bot.")


async def access_guard_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    await ensure_user(user)
    if await is_banned(user.id):
        await banned_message(update)
        raise ApplicationHandlerStop
    # Commands and messages are gated too; admins are also required to be members.
    if not await required_member(context, user.id):
        await membership_message(update)
        raise ApplicationHandlerStop


async def access_guard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.from_user:
        return
    if q.data == "check_membership":
        return
    await ensure_user(q.from_user)
    if await is_banned(q.from_user.id):
        await q.answer("🚫 Your account is banned.", show_alert=True)
        raise ApplicationHandlerStop
    if not await required_member(context, q.from_user.id):
        await q.answer("🔒 Join both required communities first.", show_alert=True)
        await q.message.reply_text("Join both communities, then check again.", reply_markup=join_markup())
        raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_user(update.effective_user)
    if not await required_member(context, update.effective_user.id):
        return await membership_message(update)
    await update.message.reply_text(
        "💎 <b>DaemonTargaryen</b>\n\nWelcome! Choose an option below:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(update.effective_user.id),
    )


async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await ensure_user(q.from_user)
    if await is_banned(q.from_user.id):
        return await q.answer("🚫 Your account is banned.", show_alert=True)
    if await required_member(context, q.from_user.id):
        await q.answer("✅ Membership verified!", show_alert=True)
        await q.message.edit_text("✅ <b>Membership verified!</b>\n\nYou can now use the bot.", parse_mode=ParseMode.HTML, reply_markup=main_menu(q.from_user.id))
    else:
        await q.answer("❌ You must join both communities.", show_alert=True)
        await q.message.reply_text("Please join both, then tap Check Membership.", reply_markup=join_markup())


async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = await get_user(q.from_user.id) or {}
    verified = await verifications.find_one({"telegram_user_id": q.from_user.id}) is not None
    await q.edit_message_text(
        f"👤 <b>My Account</b>\n\n🆔 ID: <code>{q.from_user.id}</code>\n"
        f"💰 Balance: <b>₹{money(user.get('balance', 0)):,.2f}</b>\n"
        f"🛡️ Verification: {'✅ Verified' if verified else '❌ Not verified'}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]),
    )


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await verifications.update_one(
        {"telegram_user_id": q.from_user.id},
        {"$set": {"telegram_user_id": q.from_user.id, "username": q.from_user.username or "", "verified_at": now()}},
        upsert=True,
    )
    await q.edit_message_text(
        "🛡️ <b>Verification Complete</b>\n\nYour Telegram account is marked as verified.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]),
    )


async def topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        f"💳 <b>Top Up Balance</b>\n\nMessage @{h(TOPUP_USERNAME)} with your Telegram ID and requested amount.\nAn admin will process it manually.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Message Top-Up Admin", url=f"https://t.me/{TOPUP_USERNAME}")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
        ]),
    )


async def products_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = await products.find({"active": True}).sort("created_at", -1).limit(30).to_list(length=30)
    if not items:
        return await q.edit_message_text("🛒 <b>Products</b>\n\nNo products are available.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))
    rows = []
    for p in items:
        rows.append([InlineKeyboardButton(f"{p.get('name','Product')} — ₹{money(p.get('price',0)):,.2f}", callback_data=f"product:{p['_id']}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    await q.edit_message_text("🛒 <b>Products</b>\n\nChoose a product:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


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
    content_count = len(p.get("content", []))
    extra = f"\n📎 Content items: {content_count}" if content_count else ""
    await q.edit_message_text(
        f"🛍️ <b>{h(p.get('name','Product'))}</b>\n\n{h(p.get('description',''))}\n\n"
        f"📂 Category: {h(p.get('category',''))}\n💰 Price: <b>₹{money(p.get('price',0)):,.2f}</b>{extra}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy:{pid}")],
            [InlineKeyboardButton("◀️ Products", callback_data="products")],
        ]),
    )


async def send_product_content(bot, chat_id, content):
    for item in content or []:
        typ = item.get("type")
        caption = item.get("caption") or None
        try:
            if typ == "photo":
                await bot.send_photo(chat_id, item["file_id"], caption=caption)
            elif typ == "video":
                await bot.send_video(chat_id, item["file_id"], caption=caption)
            elif typ == "document":
                await bot.send_document(chat_id, item["file_id"], caption=caption)
            elif typ == "audio":
                await bot.send_audio(chat_id, item["file_id"], caption=caption)
            elif typ == "voice":
                await bot.send_voice(chat_id, item["file_id"], caption=caption)
            elif typ == "animation":
                await bot.send_animation(chat_id, item["file_id"], caption=caption)
            elif typ == "text":
                await bot.send_message(chat_id, item.get("text", ""))
            elif typ == "link":
                url = item.get("url", "")
                label = item.get("label") or url
                await bot.send_message(chat_id, f"🔗 <a href=\"{h(url)}\">{h(label)}</a>", parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            elif typ == "copy":
                await bot.copy_message(chat_id=chat_id, from_chat_id=item["chat_id"], message_id=item["message_id"])
        except Exception:
            logger.exception("Failed delivering product content item")


async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        pid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid product.", show_alert=True)
    p = await products.find_one({"_id": pid, "active": True})
    if not p:
        return await q.answer("Product unavailable.", show_alert=True)
    price = money(p.get("price", 0))
    if price < 0:
        return await q.answer("Invalid product price.", show_alert=True)

    # Balance is deducted atomically.
    bal = await users.update_one(
        {"telegram_user_id": q.from_user.id, "balance": {"$gte": price}, "banned": {"$ne": True}},
        {"$inc": {"balance": -price}, "$set": {"updated_at": now()}},
    )
    if bal.modified_count != 1:
        return await q.answer("Insufficient balance or account unavailable.", show_alert=True)

    # Inventory is claimed atomically so only one buyer can own the item.
    claim = await products.update_one(
        {"_id": pid, "active": True},
        {"$set": {"active": False, "sold": True, "sold_to": q.from_user.id, "sold_at": now()}},
    )
    if claim.modified_count != 1:
        await users.update_one({"telegram_user_id": q.from_user.id}, {"$inc": {"balance": price}})
        return await q.answer("Product was just sold. Your balance was refunded.", show_alert=True)

    created = now()
    order = {
        "telegram_user_id": q.from_user.id,
        "username": q.from_user.username or "",
        "product_id": pid,
        "product_name": p.get("name", "Product"),
        "product_description": p.get("description", ""),
        "product_category": p.get("category", ""),
        "product_how_to_use": p.get("how_to_use", ""),
        "license_code": p.get("license_code", ""),
        "product_content": p.get("content", []),
        "amount": price,
        "status": "completed",
        "created_at": created,
    }
    try:
        result = await orders.insert_one(order)
        await transactions.insert_one({"telegram_user_id": q.from_user.id, "type": "purchase", "amount": -price, "order_id": result.inserted_id, "description": f"Purchase: {p.get('name','Product')}", "created_at": created})
    except Exception:
        await products.update_one({"_id": pid, "sold": True, "sold_to": q.from_user.id}, {"$set": {"active": True}, "$unset": {"sold": "", "sold_to": "", "sold_at": ""}})
        await users.update_one({"telegram_user_id": q.from_user.id}, {"$inc": {"balance": price}})
        logger.exception("Purchase persistence failed")
        return await q.answer("Purchase failed and your balance was refunded.", show_alert=True)

    oid = str(result.inserted_id)
    dt = created.astimezone()
    buyer = f"@{q.from_user.username}" if q.from_user.username else "No username"
    notification = (
        "🛒 <b>NEW ORDER</b>\n\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Product: {h(p.get('name','Product'))}\n"
        f"🆔 Order ID: #{h(oid)}\n"
        f"💰 Amount: ₹{price:.2f}\n\n"
        "👤 <b>Buyer Information</b>\n"
        f"• User ID: <code>{q.from_user.id}</code>\n"
        f"• Username: {h(buyer)}\n\n"
        f"📅 Date: {dt.strftime('%d %b %Y')}\n"
        f"⏰ Time: {dt.strftime('%I:%M %p')}\n\n"
        "✅ Payment Status: PAID\n📦 Order Status: COMPLETED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n🤖 Automated Order Notification"
    )
    for destination in (REQUIRED_GROUP, REQUIRED_CHANNEL):
        try:
            await context.bot.send_message(destination, notification, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Purchase notification failed for %s", destination)

    await q.edit_message_text(
        f"✅ <b>Purchase Successful</b>\n\n📦 {h(p.get('name','Product'))}\n"
        f"🧾 Order ID: <code>{oid}</code>\n💰 Paid: ₹{price:.2f}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 View Order", callback_data=f"order:{oid}")], [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]),
    )
    # Deliver the actual product after the order is committed.
    await send_product_content(context.bot, q.from_user.id, p.get("content", []))
    code = (p.get("license_code") or "").strip()
    if code and code.upper() != "NONE":
        await context.bot.send_message(q.from_user.id, f"🔑 <b>License / Activation Code</b>\n<code>{h(code)}</code>", parse_mode=ParseMode.HTML)
    howto = (p.get("how_to_use") or "").strip()
    if howto:
        await context.bot.send_message(q.from_user.id, f"📘 <b>How to Use</b>\n{h(howto)}", parse_mode=ParseMode.HTML)


async def orders_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = await orders.find({"telegram_user_id": q.from_user.id}).sort("created_at", -1).limit(30).to_list(length=30)
    if not items:
        return await q.edit_message_text("📜 <b>Order History</b>\n\nNo orders yet.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))
    rows = []
    lines = ["📜 <b>Order History</b>", "", "Select an order:", ""]
    for o in items:
        name = str(o.get("product_name", "Product"))
        amount = money(o.get("amount", 0))
        status = str(o.get("status", "unknown")).upper()
        lines.append(f"• {h(name)} — ₹{amount:.2f} — {h(status)}")
        rows.append([InlineKeyboardButton(f"📦 {name[:40]} — ₹{amount:.2f}", callback_data=f"order:{o['_id']}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid order.", show_alert=True)
    o = await orders.find_one({"_id": oid, "telegram_user_id": q.from_user.id})
    if not o:
        return await q.answer("Order not found.", show_alert=True)
    created = o.get("created_at")
    date_text = created.astimezone().strftime("%d %b %Y, %I:%M %p") if isinstance(created, datetime) else "Unknown"
    content_count = len(o.get("product_content", []))
    text = (
        f"📦 <b>{h(o.get('product_name','Product'))}</b>\n\n"
        f"🧾 Order ID: <code>{h(o['_id'])}</code>\n"
        f"💰 Amount: ₹{money(o.get('amount',0)):.2f}\n"
        f"📌 Status: {h(o.get('status','unknown').upper())}\n"
        f"📅 Purchased: {date_text}\n"
        f"📂 Category: {h(o.get('product_category',''))}\n"
        f"📝 Description: {h(o.get('product_description',''))}\n"
        f"📎 Stored content items: {content_count}"
    )
    how = (o.get("product_how_to_use") or "").strip()
    if how:
        text += f"\n\n📘 <b>How to Use</b>\n{h(how)}"
    code = (o.get("license_code") or "").strip()
    if code and code.upper() != "NONE":
        text += f"\n\n🔑 <b>License / Activation Code</b>\n<code>{h(code)}</code>"
    rows = [[InlineKeyboardButton("📦 Redeliver Product", callback_data=f"redeliver:{oid}")], [InlineKeyboardButton("◀️ Orders", callback_data="orders")]]
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def redeliver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = ObjectId(q.data.split(":", 1)[1])
    except Exception:
        return await q.answer("Invalid order.", show_alert=True)
    o = await orders.find_one({"_id": oid, "telegram_user_id": q.from_user.id})
    if not o:
        return await q.answer("Order not found.", show_alert=True)
    await send_product_content(context.bot, q.from_user.id, o.get("product_content", []))
    await q.answer("📦 Product content sent.", show_alert=True)


async def coupon_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🎟️ <b>Coupons</b>\n\nUse <code>/coupon CODE</code>. Each coupon can be redeemed successfully by only ONE person in the entire bot.", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]]))


async def use_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Usage: /coupon CODE")
    code = context.args[0].strip().upper()
    c = await coupons.find_one({"code": code, "active": True, "used": {"$ne": True}})
    if not c:
        return await update.message.reply_text("❌ Invalid, inactive, or already-used coupon.")
    discount = money(c.get("amount", 0))
    if discount <= 0 or discount > MAX_COUPON_AMOUNT:
        return await update.message.reply_text("❌ This coupon has an invalid value.")
    user = await get_user(update.effective_user.id) or {}
    current = money(user.get("balance", 0))
    if current + discount > MAX_BALANCE:
        return await update.message.reply_text("❌ Applying this coupon would exceed the 100 crore balance limit.")

    # Atomically reserve the coupon globally. Exactly one user can match this update.
    claim = await coupons.update_one(
        {"_id": c["_id"], "active": True, "used": {"$ne": True}},
        {"$set": {"used": True, "used_by": update.effective_user.id, "used_at": now()}},
    )
    if claim.modified_count != 1:
        return await update.message.reply_text("❌ This coupon was just used by another user.")

    # Balance update is bounded. If it cannot be applied, restore the coupon.
    credit = await users.update_one(
        {"telegram_user_id": update.effective_user.id, "balance": {"$lte": MAX_BALANCE - discount}},
        {"$inc": {"balance": discount}, "$set": {"updated_at": now()}},
    )
    if credit.modified_count != 1:
        await coupons.update_one({"_id": c["_id"], "used_by": update.effective_user.id}, {"$set": {"used": False}, "$unset": {"used_by": "", "used_at": ""}})
        return await update.message.reply_text("❌ Coupon could not be applied. Nothing was charged or credited.")

    await transactions.insert_one({"telegram_user_id": update.effective_user.id, "type": "coupon", "amount": discount, "coupon_code": code, "description": "Global single-use coupon credit", "created_at": now()})
    await update.message.reply_text(f"✅ Coupon applied. ₹{discount:,.2f} added to your balance.\n\n🔒 This coupon is now permanently used.")


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🛟 <b>Support</b>\n\nChoose an option:", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 New Ticket", callback_data="new_ticket")],
        [InlineKeyboardButton("📜 My Tickets", callback_data="my_tickets")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
    ]))


async def new_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🎫 Send your ticket message. You may write a refund request and include the Order ID.")
    return TICKET_TEXT


async def receive_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    ticket_type = "refund" if re.search(r"\brefund\b", text, re.I) else "support"
    order_id = None
    m = re.search(r"(?:#|order\s*id[:\s]*)?([0-9a-f]{24})", text, re.I)
    if m:
        order_id = m.group(1)
    doc = {
        "telegram_user_id": update.effective_user.id,
        "username": update.effective_user.username or "",
        "type": ticket_type,
        "message": text,
        "status": "open",
        "order_id": order_id,
        "messages": [{"sender": "user", "user_id": update.effective_user.id, "text": text, "created_at": now()}],
        "created_at": now(),
        "updated_at": now(),
    }
    result = await tickets.insert_one(doc)
    context.user_data["active_ticket_id"] = str(result.inserted_id)
    await update.message.reply_text(f"🎫 Ticket created: <code>{result.inserted_id}</code>\nYou can continue replying here until an admin closes it.", parse_mode=ParseMode.HTML, reply_markup=main_menu(update.effective_user.id))
    try:
        await context.bot.send_message(ADMIN_ID, f"🎫 <b>New {ticket_type.upper()} Ticket</b>\n\nID: <code>{result.inserted_id}</code>\nUser: <code>{update.effective_user.id}</code>\n\n{h(text)}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁 Open Ticket", callback_data=f"adm_ticket:{result.inserted_id}")]]))
    except Exception:
        logger.exception("Ticket admin notification failed")
    return ConversationHandler.END


async def ticket_user_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid = context.user_data.get("active_ticket_id")
    if not tid or not update.message or not update.message.text:
        return False
    try:
        oid = ObjectId(tid)
    except Exception:
        context.user_data.pop("active_ticket_id", None)
        return False
    t = await tickets.find_one({"_id": oid, "telegram_user_id": update.effective_user.id, "status": {"$ne": "closed"}})
    if not t:
        context.user_data.pop("active_ticket_id", None)
        return False
    text = update.message.text.strip()
    await tickets.update_one({"_id": oid}, {"$push": {"messages": {"sender": "user", "user_id": update.effective_user.id, "text": text, "created_at": now()}}, "$set": {"status": "open", "updated_at": now()}})
    try:
        await context.bot.send_message(ADMIN_ID, f"💬 <b>Ticket Reply</b>\nTicket: <code>{tid}</code>\nUser: <code>{update.effective_user.id}</code>\n\n{h(text)}", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👁 Open Ticket", callback_data=f"adm_ticket:{tid}")]]))
    except Exception:
        logger.exception("Ticket reply notification failed")
    await update.message.reply_text("💬 Your message was sent to the admin.")
    return True


async def my_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = await tickets.find({"telegram_user_id": q.from_user.id}).sort("updated_at", -1).limit(15).to_list(length=15)
    rows = []
    for t in items:
        rows.append([InlineKeyboardButton(f"🎫 {str(t['_id'])[-8:]} — {t.get('status','open').upper()}", callback_data=f"user_ticket:{t['_id']}")])
    rows.append([InlineKeyboardButton("◀️ Support", callback_data="support")])
    await q.edit_message_text("🎫 <b>My Tickets</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def user_ticket_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = ObjectId(q.data.split(":",1)[1])
    except Exception:
        return await q.answer("Invalid ticket.", show_alert=True)
    t = await tickets.find_one({"_id": oid, "telegram_user_id": q.from_user.id})
    if not t:
        return await q.answer("Ticket not found.", show_alert=True)
    lines = [f"🎫 <b>Ticket</b> <code>{oid}</code>", f"Type: {h(t.get('type','support'))}", f"Status: {h(t.get('status','open'))}", ""]
    for m in t.get("messages", [])[-8:]:
        who = "You" if m.get("sender") == "user" else "Admin"
        lines.append(f"<b>{who}:</b> {h(m.get('text',''))}")
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Reply", callback_data=f"user_reply:{oid}")], [InlineKeyboardButton("◀️ My Tickets", callback_data="my_tickets")]]))


async def user_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        oid = ObjectId(q.data.split(":",1)[1])
    except Exception:
        return await q.answer("Invalid ticket.", show_alert=True)
    t = await tickets.find_one({"_id": oid, "telegram_user_id": q.from_user.id, "status": {"$ne": "closed"}})
    if not t:
        return await q.answer("Ticket is closed or unavailable.", show_alert=True)
    context.user_data["active_ticket_id"] = str(oid)
    await q.message.reply_text("💬 Send your reply now. Your next text message will be sent to the admin.")


async def audit(admin_id, action, target_user_id=None, details=None):
    await audit_logs.insert_one({"admin_id": admin_id, "action": action, "target_user_id": target_user_id, "details": details or {}, "created_at": now()})


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return await q.answer("Admin only.", show_alert=True)
    await q.edit_message_text("⚙️ <b>ADMIN PANEL</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="adm_user"), InlineKeyboardButton("📦 Products", callback_data="adm_products")],
        [InlineKeyboardButton("💰 Add Balance", callback_data="adm_balance"), InlineKeyboardButton("🎟 Coupons", callback_data="adm_coupon")],
        [InlineKeyboardButton("🎫 Tickets", callback_data="adm_tickets"), InlineKeyboardButton("💸 Refunds", callback_data="adm_refunds")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast"), InlineKeyboardButton("📊 Statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("📝 Audit Logs", callback_data="adm_audit")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")],
    ]))


async def admin_add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id): return ConversationHandler.END
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
        await update.message.reply_text("❌ Enter a valid price.")
        return ADD_PRODUCT_PRICE
    context.user_data["price"] = price
    await update.message.reply_text("Enter category:")
    return ADD_PRODUCT_CATEGORY


async def add_product_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["category"] = update.message.text.strip()
    await update.message.reply_text("Enter product description:")
    return ADD_PRODUCT_DESCRIPTION


async def add_product_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["description"] = update.message.text.strip()
    await update.message.reply_text("Enter How-to-Use instructions, or type NONE:")
    return ADD_PRODUCT_HOWTO


async def add_product_howto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["how_to_use"] = "" if text.upper() == "NONE" else text
    await update.message.reply_text("Enter license/activation code, or type NONE:")
    return ADD_PRODUCT_CODE


async def add_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["license_code"] = "" if text.upper() == "NONE" else text
    context.user_data["product_content"] = []
    await update.message.reply_text(
        "📎 Now send the product content. You can send photos, videos, files/documents, audio, voice, animations, text, or links.\n\n"
        "Send as many items as needed, then send /done to save the product.\n"
        "If there is no content, send /done."
    )
    return ADD_PRODUCT_CONTENT


async def add_product_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    content = context.user_data.setdefault("product_content", [])
    caption = update.effective_message.caption or ""
    if update.message.photo:
        content.append({"type":"photo","file_id":update.message.photo[-1].file_id,"caption":caption})
    elif update.message.video:
        content.append({"type":"video","file_id":update.message.video.file_id,"caption":caption})
    elif update.message.document:
        content.append({"type":"document","file_id":update.message.document.file_id,"caption":caption})
    elif update.message.audio:
        content.append({"type":"audio","file_id":update.message.audio.file_id,"caption":caption})
    elif update.message.voice:
        content.append({"type":"voice","file_id":update.message.voice.file_id,"caption":caption})
    elif update.message.animation:
        content.append({"type":"animation","file_id":update.message.animation.file_id,"caption":caption})
    elif update.message.text:
        text = update.message.text.strip()
        if re.match(r"^https?://\S+$", text, re.I):
            content.append({"type":"link","url":text,"label":text})
        else:
            content.append({"type":"text","text":text})
    else:
        # Store the original Telegram message as a copy reference. This covers
        # other Telegram content types such as stickers, contacts, locations,
        # and other messages that the Bot API permits copying.
        content.append({
            "type": "copy",
            "chat_id": update.effective_chat.id,
            "message_id": update.message.message_id,
        })
    await update.message.reply_text(f"✅ Content added. Total items: {len(content)}. Send another item or /done.")
    return ADD_PRODUCT_CONTENT


async def finish_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    await products.insert_one({
        "name": data["product_name"], "price": data["price"], "category": data["category"],
        "description": data["description"], "how_to_use": data["how_to_use"], "license_code": data["license_code"],
        "content": data.get("product_content", []), "active": True, "sold": False, "created_at": now(),
    })
    await audit(update.effective_user.id, "product_created", details={"name": data["product_name"]})
    context.user_data.clear()
    await update.message.reply_text("✅ Product added successfully with all attached content.", reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END


async def admin_balance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return ConversationHandler.END
    await q.edit_message_text("💰 Enter user's Telegram ID:")
    return ADD_BALANCE_USER


async def add_balance_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value.isdigit():
        await update.message.reply_text("❌ Enter a numeric user ID."); return ADD_BALANCE_USER
    uid = int(value)
    if uid == ADMIN_ID:
        await update.message.reply_text("❌ Admin account cannot be modified."); return ADD_BALANCE_USER
    context.user_data["balance_user"] = uid
    await update.message.reply_text("Enter amount to add in ₹:")
    return ADD_BALANCE_AMOUNT


async def add_balance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = money(update.message.text.strip())
    if amount <= 0: await update.message.reply_text("❌ Amount must be greater than 0."); return ADD_BALANCE_AMOUNT
    if amount > MAX_BALANCE: await update.message.reply_text("❌ Amount exceeds 100 crore."); return ADD_BALANCE_AMOUNT
    uid = context.user_data["balance_user"]
    result = await users.update_one({"telegram_user_id": uid, "balance": {"$lte": MAX_BALANCE - amount}}, {"$inc": {"balance": amount}, "$set": {"updated_at": now()}}, upsert=False)
    if result.modified_count != 1:
        await update.message.reply_text("❌ User not found or balance limit would be exceeded."); return ADD_BALANCE_AMOUNT
    await transactions.insert_one({"telegram_user_id": uid, "type":"admin_credit", "amount":amount, "admin_id":ADMIN_ID, "created_at":now()})
    await audit(ADMIN_ID, "balance_credit", uid, {"amount": amount})
    context.user_data.clear()
    await update.message.reply_text(f"✅ ₹{amount:,.2f} added.", reply_markup=main_menu(ADMIN_ID))
    return ConversationHandler.END


async def deduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 2 or not context.args[0].isdigit(): return await update.message.reply_text("Usage: /deduct USER_ID AMOUNT")
    uid, amount = int(context.args[0]), money(context.args[1])
    if uid == ADMIN_ID: return await update.message.reply_text("❌ Admin account is protected.")
    if amount <= 0: return await update.message.reply_text("❌ Amount must be greater than 0.")
    r = await users.update_one({"telegram_user_id": uid, "balance": {"$gte": amount}}, {"$inc":{"balance":-amount}, "$set":{"updated_at":now()}})
    if r.modified_count != 1: return await update.message.reply_text("❌ User not found or insufficient balance.")
    await transactions.insert_one({"telegram_user_id":uid,"type":"admin_deduction","amount":-amount,"admin_id":ADMIN_ID,"created_at":now()})
    await audit(ADMIN_ID,"balance_deduction",uid,{"amount":amount})
    await update.message.reply_text(f"✅ Deducted ₹{amount:,.2f} from {uid}.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): return await update.message.reply_text("Usage: /ban USER_ID")
    uid=int(context.args[0])
    if uid == ADMIN_ID: return await update.message.reply_text("❌ Admin account is protected.")
    r=await users.update_one({"telegram_user_id":uid},{"$set":{"banned":True,"updated_at":now()}})
    if r.matched_count != 1: return await update.message.reply_text("❌ User not found.")
    await audit(ADMIN_ID,"user_banned",uid)
    await update.message.reply_text(f"🚫 User {uid} banned.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) != 1 or not context.args[0].isdigit(): return await update.message.reply_text("Usage: /unban USER_ID")
    uid=int(context.args[0])
    r=await users.update_one({"telegram_user_id":uid},{"$set":{"banned":False,"updated_at":now()}})
    if r.matched_count != 1: return await update.message.reply_text("❌ User not found.")
    await audit(ADMIN_ID,"user_unbanned",uid)
    await update.message.reply_text(f"✅ User {uid} unbanned.")


async def showbal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    items=await users.find({}).sort("telegram_user_id",1).limit(500).to_list(length=500)
    if not items: return await update.message.reply_text("No registered users.")
    lines=["👥 <b>Registered Users</b>",""]
    for u in items:
        username=f"@{u.get('username')}" if u.get('username') else "No username"
        lines.append(f"🆔 <code>{u.get('telegram_user_id')}</code> | {h(username)} | ₹{money(u.get('balance',0)):,.2f} | {'🚫 Banned' if u.get('banned') else '✅ Active'}")
    text="\n".join(lines)
    for i in range(0,len(text),3800):
        await update.message.reply_text(text[i:i+3800],parse_mode=ParseMode.HTML)


async def manage_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    items=await products.find({}).sort("created_at",-1).limit(30).to_list(length=30)
    rows=[[InlineKeyboardButton("➕ Add Product",callback_data="adm_add_product")]]
    for p in items:
        status="🟢" if p.get("active") else ("💰" if p.get("sold") else "🔴")
        rows.append([InlineKeyboardButton(f"{status} {p.get('name','Product')[:35]}",callback_data=f"adm_prod:{p['_id']}")])
    rows.append([InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")])
    await q.edit_message_text("🛠 <b>Manage Products</b>",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def admin_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: pid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    p=await products.find_one({"_id":pid})
    if not p: return await q.answer("Product not found.",show_alert=True)
    await q.edit_message_text(f"📦 <b>{h(p.get('name'))}</b>\n\nPrice: ₹{money(p.get('price',0)):,.2f}\nCategory: {h(p.get('category'))}\nStatus: {'AVAILABLE' if p.get('active') else ('SOLD' if p.get('sold') else 'DISABLED')}\nContent items: {len(p.get('content',[]))}",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔴 Disable",callback_data=f"disable:{pid}" if p.get('active') else f"enable:{pid}")],[InlineKeyboardButton("🗑 Delete",callback_data=f"delete:{pid}")],[InlineKeyboardButton("◀️ Products",callback_data="adm_products")]]))


async def toggle_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: pid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    p=await products.find_one({"_id":pid})
    if not p: return
    if p.get("sold"): return await q.answer("Sold items cannot be re-enabled.",show_alert=True)
    await products.update_one({"_id":pid},{"$set":{"active":q.data.startswith("enable:")}})
    await audit(ADMIN_ID,"product_status_changed",details={"product_id":str(pid),"active":q.data.startswith("enable:")})
    await admin_product_detail(update,context)


async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: pid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    await products.delete_one({"_id":pid})
    await audit(ADMIN_ID,"product_deleted",details={"product_id":str(pid)})
    await manage_products(update,context)


async def admin_coupon_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return ConversationHandler.END
    await q.edit_message_text("🎟 Enter coupon code. Each coupon will be globally single-use:")
    return COUPON_CODE


async def coupon_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code=update.message.text.strip().upper()
    if not code or " " in code: await update.message.reply_text("❌ Invalid code."); return COUPON_CODE
    context.user_data["coupon_code"]=code
    await update.message.reply_text("Enter coupon credit amount in ₹ (maximum ₹1 crore):")
    return COUPON_DISCOUNT


async def coupon_discount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount=money(update.message.text.strip())
    if amount<=0 or amount>MAX_COUPON_AMOUNT:
        await update.message.reply_text("❌ Coupon amount must be between ₹0.01 and ₹1 crore."); return COUPON_DISCOUNT
    code=context.user_data["coupon_code"]
    existing=await coupons.find_one({"code":code})
    if existing and existing.get("used"):
        await update.message.reply_text("❌ That coupon code is already used and cannot be recreated."); return COUPON_DISCOUNT
    await coupons.update_one({"code":code},{"$set":{"code":code,"amount":amount,"active":True,"used":False,"created_at":now()},"$unset":{"used_by":"","used_at":""}},upsert=True)
    await audit(ADMIN_ID,"coupon_created",details={"code":code,"amount":amount})
    context.user_data.clear()
    await update.message.reply_text(f"✅ Global single-use coupon <code>{h(code)}</code> created for ₹{amount:,.2f}.",parse_mode=ParseMode.HTML,reply_markup=main_menu(ADMIN_ID))
    return ConversationHandler.END


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    total_users=await users.count_documents({}); active=await products.count_documents({"active":True}); sold=await products.count_documents({"sold":True}); total_orders=await orders.count_documents({}); open_t=await tickets.count_documents({"status":{"$ne":"closed"}}); used_c=await coupons.count_documents({"used":True})
    pipeline=[{"$match":{"type":"purchase","amount":{"$lt":0}}},{"$group":{"_id":None,"total":{"$sum":{"$multiply":["$amount",-1]}}}}]
    sales=0
    async for row in transactions.aggregate(pipeline): sales=money(row.get("total",0))
    await q.edit_message_text(f"📊 <b>Statistics</b>\n\n👥 Users: {total_users}\n🟢 Available products: {active}\n💰 Sold products: {sold}\n🧾 Orders: {total_orders}\n💵 Sales: ₹{sales:,.2f}\n🎫 Open tickets: {open_t}\n🎟 Used coupons: {used_c}",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")]]))


async def admin_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    context.user_data["admin_lookup"]=True
    await q.message.reply_text("👤 Send the user's numeric Telegram ID:")


async def admin_lookup_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.user_data.get("admin_lookup"): return False
    text=update.message.text.strip()
    if not text.isdigit(): await update.message.reply_text("Send a numeric ID."); return True
    uid=int(text); u=await get_user(uid)
    if not u: await update.message.reply_text("User not found."); return True
    context.user_data.pop("admin_lookup",None)
    await update.message.reply_text(f"👤 <code>{uid}</code>\nUsername: @{h(u.get('username') or 'none')}\n💰 Balance: ₹{money(u.get('balance',0)):,.2f}\n📦 Orders: {await orders.count_documents({'telegram_user_id':uid})}\nStatus: {'🚫 Banned' if u.get('banned') else '✅ Active'}",parse_mode=ParseMode.HTML)
    return True


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return ConversationHandler.END
    await q.edit_message_text("📢 Send the broadcast message. Text only in this version; you can cancel with /cancel.")
    return BROADCAST_TEXT


async def broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    context.user_data["broadcast_text"]=update.message.text
    await update.message.reply_text(f"👁 <b>Broadcast Preview</b>\n\n{h(update.message.text)}\n\nSend /confirmbroadcast to send, or /cancel.",parse_mode=ParseMode.HTML)
    return BROADCAST_TEXT


async def confirm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    text=context.user_data.get("broadcast_text")
    if not text: return await update.message.reply_text("No broadcast is waiting for confirmation.")
    sent=failed=0
    async for u in users.find({}, {"telegram_user_id":1}):
        try:
            await context.bot.send_message(u["telegram_user_id"],text)
            sent+=1
        except Exception:
            failed+=1
    await audit(ADMIN_ID,"broadcast_sent",details={"sent":sent,"failed":failed})
    context.user_data.pop("broadcast_text",None)
    await update.message.reply_text(f"📢 Broadcast finished.\n\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def admin_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    items=await tickets.find({"status":{"$ne":"closed"}}).sort("updated_at",-1).limit(30).to_list(length=30)
    rows=[]
    for t in items:
        typ="💸" if t.get("type")=="refund" else "🎫"
        rows.append([InlineKeyboardButton(f"{typ} {str(t['_id'])[-8:]} — {t.get('status','open').upper()}",callback_data=f"adm_ticket:{t['_id']}")])
    rows.append([InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")])
    await q.edit_message_text("🎫 <b>Open Tickets</b>\n\nSelect a ticket:",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def admin_ticket_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: oid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    t=await tickets.find_one({"_id":oid})
    if not t: return await q.answer("Ticket not found.",show_alert=True)
    lines=[f"🎫 <b>Ticket</b> <code>{oid}</code>",f"👤 User: <code>{t.get('telegram_user_id')}</code>",f"Type: {h(t.get('type','support'))}",f"Status: {h(t.get('status','open'))}",""]
    for m in t.get("messages",[])[-10:]: lines.append(f"<b>{'User' if m.get('sender')=='user' else 'Admin'}:</b> {h(m.get('text',''))}")
    buttons=[[InlineKeyboardButton("💬 Reply",callback_data=f"adm_reply:{oid}")]]
    if t.get("type")=="refund" and t.get("status")!="refunded":
        buttons.append([InlineKeyboardButton("✅ Approve Refund",callback_data=f"refund_yes:{oid}"),InlineKeyboardButton("❌ Reject Refund",callback_data=f"refund_no:{oid}")])
    buttons.append([InlineKeyboardButton("🔒 Close Ticket",callback_data=f"close_ticket:{oid}")])
    buttons.append([InlineKeyboardButton("◀️ Tickets",callback_data="adm_tickets")])
    await q.edit_message_text("\n".join(lines),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(buttons))


async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    tid=q.data.split(":",1)[1]; context.user_data["admin_reply_ticket"]=tid
    await q.message.reply_text(f"💬 Send the reply for ticket <code>{h(tid)}</code>.",parse_mode=ParseMode.HTML)


async def admin_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid=context.user_data.get("admin_reply_ticket")
    if not tid or not update.message or not update.message.text: return False
    try: oid=ObjectId(tid)
    except Exception: context.user_data.pop("admin_reply_ticket",None); return False
    t=await tickets.find_one({"_id":oid,"status":{"$ne":"closed"}})
    if not t: context.user_data.pop("admin_reply_ticket",None); return False
    text=update.message.text.strip(); uid=t["telegram_user_id"]
    await tickets.update_one({"_id":oid},{"$push":{"messages":{"sender":"admin","user_id":ADMIN_ID,"text":text,"created_at":now()}},"$set":{"status":"admin_replied","updated_at":now()}})
    try: await context.bot.send_message(uid,f"💬 <b>Admin Reply</b>\n\n{h(text)}",parse_mode=ParseMode.HTML)
    except Exception: logger.exception("Failed to send admin ticket reply")
    await audit(ADMIN_ID,"ticket_reply",uid,{"ticket_id":tid})
    context.user_data.pop("admin_reply_ticket",None)
    await update.message.reply_text("✅ Reply sent.")
    return True


async def approve_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: oid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    t=await tickets.find_one({"_id":oid,"type":"refund"})
    if not t: return await q.answer("Refund ticket not found.",show_alert=True)
    order_oid=None
    if t.get("order_id"):
        try: order_oid=ObjectId(t["order_id"])
        except Exception: pass
    if not order_oid:
        # Try to locate an order ID in the ticket text.
        for m in t.get("messages",[]):
            mm=re.search(r"[0-9a-f]{24}",m.get("text", ""),re.I)
            if mm:
                try: order_oid=ObjectId(mm.group(0)); break
                except Exception: pass
    if not order_oid: return await q.answer("No valid Order ID found in this refund ticket.",show_alert=True)
    order=await orders.find_one({"_id":order_oid})
    if not order: return await q.answer("Order not found.",show_alert=True)
    if order.get("status")=="refunded": return await q.answer("Already refunded.",show_alert=True)
    amount=money(order.get("amount",0)); uid=order.get("telegram_user_id")
    # Atomic order status transition prevents double refunds.
    r=await orders.update_one({"_id":order_oid,"status":{"$ne":"refunded"}},{"$set":{"status":"refunded","refunded_at":now(),"refunded_by":ADMIN_ID}})
    if r.modified_count!=1: return await q.answer("Refund was already processed.",show_alert=True)
    bal=await users.update_one({"telegram_user_id":uid,"balance":{"$lte":MAX_BALANCE-amount}},{"$inc":{"balance":amount},"$set":{"updated_at":now()}})
    if bal.modified_count!=1:
        await orders.update_one({"_id":order_oid,"status":"refunded"},{"$set":{"status":"completed"},"$unset":{"refunded_at":"","refunded_by":""}})
        return await q.answer("Refund could not be credited because of the balance limit.",show_alert=True)
    await transactions.insert_one({"telegram_user_id":uid,"type":"refund","amount":amount,"order_id":order_oid,"admin_id":ADMIN_ID,"created_at":now()})
    await tickets.update_one({"_id":oid},{"$set":{"status":"refunded","refund_order_id":order_oid,"updated_at":now()}})
    await audit(ADMIN_ID,"refund_approved",uid,{"ticket_id":str(oid),"order_id":str(order_oid),"amount":amount})
    try: await context.bot.send_message(uid,f"✅ <b>Refund Approved</b>\n\nOrder: <code>{order_oid}</code>\nRefunded: ₹{amount:,.2f}",parse_mode=ParseMode.HTML)
    except Exception: pass
    await q.edit_message_text("✅ Refund approved and credited to the user.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Tickets",callback_data="adm_tickets")]]) )


async def reject_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: oid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    t=await tickets.find_one({"_id":oid,"type":"refund"})
    if not t: return
    await tickets.update_one({"_id":oid},{"$set":{"status":"refund_rejected","updated_at":now()}})
    uid=t.get("telegram_user_id")
    await audit(ADMIN_ID,"refund_rejected",uid,{"ticket_id":str(oid)})
    try: await context.bot.send_message(uid,"❌ <b>Refund Rejected</b>\n\nYour refund request was rejected by the admin.",parse_mode=ParseMode.HTML)
    except Exception: pass
    await q.edit_message_text("❌ Refund rejected.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Tickets",callback_data="adm_tickets")]]))


async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    try: oid=ObjectId(q.data.split(":",1)[1])
    except Exception: return
    t=await tickets.find_one({"_id":oid})
    if not t: return
    await tickets.update_one({"_id":oid},{"$set":{"status":"closed","closed_at":now(),"closed_by":ADMIN_ID,"updated_at":now()}})
    await audit(ADMIN_ID,"ticket_closed",t.get("telegram_user_id"),{"ticket_id":str(oid)})
    try: await context.bot.send_message(t.get("telegram_user_id"),f"🔒 Ticket <code>{oid}</code> has been closed by the admin.",parse_mode=ParseMode.HTML)
    except Exception: pass
    await q.edit_message_text("🔒 Ticket closed.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Tickets",callback_data="adm_tickets")]]))


async def admin_refunds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    items=await tickets.find({"type":"refund","status":"open"}).sort("created_at",-1).limit(30).to_list(length=30)
    rows=[[InlineKeyboardButton(f"💸 {str(t['_id'])[-8:]} — User {t.get('telegram_user_id')}",callback_data=f"adm_ticket:{t['_id']}")] for t in items]
    rows.append([InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")])
    await q.edit_message_text("💸 <b>Pending Refund Requests</b>",parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup(rows))


async def admin_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    items=await audit_logs.find({}).sort("created_at",-1).limit(30).to_list(length=30)
    lines=["📝 <b>Audit Logs</b>",""]
    for x in items:
        dt=x.get("created_at"); stamp=dt.astimezone().strftime("%d %b %H:%M") if isinstance(dt,datetime) else ""
        lines.append(f"• {h(stamp)} — <b>{h(x.get('action'))}</b> — target: <code>{h(x.get('target_user_id') or '-')}</code>")
    await q.edit_message_text("\n".join(lines),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")]]))


async def admin_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    if not is_admin(q.from_user.id): return
    items=await transactions.find({}).sort("created_at",-1).limit(30).to_list(length=30)
    lines=["🧾 <b>Recent Transactions</b>",""]
    for t in items: lines.append(f"• <code>{t.get('telegram_user_id')}</code> | {h(t.get('type'))} | ₹{money(t.get('amount',0)):,.2f}")
    await q.edit_message_text("\n".join(lines),parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Admin Panel",callback_data="admin")]]))


async def home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    await q.edit_message_text("💎 <b>DaemonTargaryen</b>\n\nChoose an option:",parse_mode=ParseMode.HTML,reply_markup=main_menu(q.from_user.id))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.",reply_markup=main_menu(update.effective_user.id))
    return ConversationHandler.END


async def post_init(application: Application):
    await users.create_index("telegram_user_id", unique=True)
    await users.create_index("banned")
    await products.create_index([("active",1),("created_at",-1)])
    await products.create_index([("category",1),("active",1)])
    await orders.create_index([("telegram_user_id",1),("created_at",-1)])
    await orders.create_index("product_id")
    await transactions.create_index([("telegram_user_id",1),("created_at",-1)])
    await coupons.create_index("code", unique=True)
    await tickets.create_index([("status",1),("updated_at",-1)])
    await audit_logs.create_index([("created_at",-1)])
    await verifications.create_index("telegram_user_id", unique=True)


def build_application():
    app=Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Global access gates run first. check_membership is deliberately exempt so a non-member can verify after joining.
    app.add_handler(MessageHandler(filters.ALL, access_guard_message), group=-10)
    app.add_handler(CallbackQueryHandler(access_guard_callback, pattern=r".*"), group=-10)

    app.add_handler(CommandHandler("start",start))
    app.add_handler(CommandHandler("coupon",use_coupon))
    app.add_handler(CommandHandler("deduct",deduct_command))
    app.add_handler(CommandHandler("ban",ban_command))
    app.add_handler(CommandHandler("unban",unban_command))
    app.add_handler(CommandHandler("showbal",showbal_command))
    app.add_handler(CommandHandler("confirmbroadcast",confirm_broadcast))

    product_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_product_start,pattern=r"^adm_add_product$")],
        states={
            ADD_PRODUCT_NAME:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_name)],
            ADD_PRODUCT_PRICE:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_price)],
            ADD_PRODUCT_CATEGORY:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_category)],
            ADD_PRODUCT_DESCRIPTION:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_description)],
            ADD_PRODUCT_HOWTO:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_howto)],
            ADD_PRODUCT_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_product_code)],
            ADD_PRODUCT_CONTENT:[
                CommandHandler("done",finish_product),
                MessageHandler(filters.ALL & ~filters.COMMAND,add_product_content),
            ],
        },
        fallbacks=[CommandHandler("cancel",cancel)], per_user=True, per_chat=True,
    )
    app.add_handler(product_conv)

    balance_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_balance_start,pattern=r"^adm_balance$")],
        states={ADD_BALANCE_USER:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_balance_user)],ADD_BALANCE_AMOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND,add_balance_amount)]},
        fallbacks=[CommandHandler("cancel",cancel)],per_user=True,per_chat=True,
    )
    app.add_handler(balance_conv)

    coupon_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_coupon_start,pattern=r"^adm_coupon$")],
        states={COUPON_CODE:[MessageHandler(filters.TEXT & ~filters.COMMAND,coupon_code)],COUPON_DISCOUNT:[MessageHandler(filters.TEXT & ~filters.COMMAND,coupon_discount)]},
        fallbacks=[CommandHandler("cancel",cancel)],per_user=True,per_chat=True,
    )
    app.add_handler(coupon_conv)

    broadcast_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start,pattern=r"^adm_broadcast$")],
        states={BROADCAST_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND,broadcast_text)]},
        fallbacks=[CommandHandler("cancel",cancel)],per_user=True,per_chat=True,
    )
    app.add_handler(broadcast_conv)

    ticket_conv=ConversationHandler(
        entry_points=[CallbackQueryHandler(new_ticket,pattern=r"^new_ticket$")],
        states={TICKET_TEXT:[MessageHandler(filters.TEXT & ~filters.COMMAND,receive_ticket)]},
        fallbacks=[CommandHandler("cancel",cancel)],per_user=True,per_chat=True,
    )
    app.add_handler(ticket_conv)

    # Commands and callbacks.
    app.add_handler(CallbackQueryHandler(check_membership,pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(account,pattern=r"^account$"))
    app.add_handler(CallbackQueryHandler(verify,pattern=r"^verify$"))
    app.add_handler(CallbackQueryHandler(topup,pattern=r"^topup$"))
    app.add_handler(CallbackQueryHandler(products_menu,pattern=r"^products$"))
    app.add_handler(CallbackQueryHandler(product_detail,pattern=r"^product:"))
    app.add_handler(CallbackQueryHandler(buy_product,pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(orders_menu,pattern=r"^orders$"))
    app.add_handler(CallbackQueryHandler(order_detail,pattern=r"^order:"))
    app.add_handler(CallbackQueryHandler(redeliver,pattern=r"^redeliver:"))
    app.add_handler(CallbackQueryHandler(coupon_help,pattern=r"^coupon_help$"))
    app.add_handler(CallbackQueryHandler(support,pattern=r"^support$"))
    app.add_handler(CallbackQueryHandler(new_ticket,pattern=r"^new_ticket$"))
    app.add_handler(CallbackQueryHandler(my_tickets,pattern=r"^my_tickets$"))
    app.add_handler(CallbackQueryHandler(user_ticket_detail,pattern=r"^user_ticket:"))
    app.add_handler(CallbackQueryHandler(user_reply_start,pattern=r"^user_reply:"))
    app.add_handler(CallbackQueryHandler(admin_panel,pattern=r"^admin$"))
    app.add_handler(CallbackQueryHandler(manage_products,pattern=r"^adm_products$"))
    app.add_handler(CallbackQueryHandler(admin_product_detail,pattern=r"^adm_prod:"))
    app.add_handler(CallbackQueryHandler(toggle_product,pattern=r"^(disable|enable):"))
    app.add_handler(CallbackQueryHandler(delete_product,pattern=r"^delete:"))
    app.add_handler(CallbackQueryHandler(admin_stats,pattern=r"^adm_stats$"))
    app.add_handler(CallbackQueryHandler(admin_user_start,pattern=r"^adm_user$"))
    app.add_handler(CallbackQueryHandler(admin_tickets,pattern=r"^adm_tickets$"))
    app.add_handler(CallbackQueryHandler(admin_ticket_detail,pattern=r"^adm_ticket:"))
    app.add_handler(CallbackQueryHandler(admin_reply_start,pattern=r"^adm_reply:"))
    app.add_handler(CallbackQueryHandler(approve_refund,pattern=r"^refund_yes:"))
    app.add_handler(CallbackQueryHandler(reject_refund,pattern=r"^refund_no:"))
    app.add_handler(CallbackQueryHandler(close_ticket,pattern=r"^close_ticket:"))
    app.add_handler(CallbackQueryHandler(admin_refunds,pattern=r"^adm_refunds$"))
    app.add_handler(CallbackQueryHandler(admin_audit,pattern=r"^adm_audit$"))
    app.add_handler(CallbackQueryHandler(admin_transactions,pattern=r"^adm_transactions$"))
    app.add_handler(CallbackQueryHandler(home,pattern=r"^home$"))

    # These text handlers let users/admins continue an existing ticket conversation.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ticket_user_reply), group=5)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_message), group=6)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_lookup_message), group=7)
    return app


if __name__ == "__main__":
    application=build_application()
    logger.info("DaemonTargaryen starting...")
    application.run_polling(drop_pending_updates=True)
