import os
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "DaemonTargaryen")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo[DB_NAME]
users = db.users
products = db.products
orders = db.orders
verifications = db.ownership_verifications

def menu(uid):
    b = [
        [InlineKeyboardButton("🔐 Verify Account", callback_data="verify")],
        [InlineKeyboardButton("👤 My Account", callback_data="account")],
        [InlineKeyboardButton("📦 Products", callback_data="products")],
        [InlineKeyboardButton("📜 Orders", callback_data="orders")],
        [InlineKeyboardButton("🛟 Support", callback_data="support")],
    ]
    if uid == ADMIN_ID and ADMIN_ID:
        b.insert(-1, [InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(b)

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add")],
        [InlineKeyboardButton("📦 Manage Products", callback_data="admin_manage")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="home")]
    ])

def is_admin(uid):
    return bool(ADMIN_ID) and uid == ADMIN_ID

async def ensure_user(u):
    await users.update_one(
        {"telegram_user_id": u.id},
        {"$set": {"username": u.username, "first_name": u.first_name,
                  "updated_at": datetime.now(timezone.utc)},
         "$setOnInsert": {"telegram_user_id": u.id, "balance": 0.0,
                          "created_at": datetime.now(timezone.utc)}},
        upsert=True
    )

async def start(update, context):
    await ensure_user(update.effective_user)
    await update.message.reply_text(
        "💎 *DaemonTargaryen Bot*\n\nWelcome back! 👋\n\n"
        "🆔 Account: Connected\n📦 Status: Ready",
        parse_mode="Markdown", reply_markup=menu(update.effective_user.id)
    )

async def show_products(q):
    cats = await products.distinct("category", {"active": True})
    labels = {"digital": "📱 Digital Products", "software": "💻 Software & Tools", "other": "🎁 Other Products"}
    b = [[InlineKeyboardButton(labels.get(c, c.title()), callback_data=f"cat:{c}")] for c in cats if c in labels]
    b.append([InlineKeyboardButton("🏠 Main Menu", callback_data="home")])
    await q.edit_message_text("🛒 *Products*\n\nChoose a category:",
                              parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(b))

async def show_category(q, cat):
    items = await products.find({"active": True, "category": cat}).sort("created_at", -1).to_list(50)
    b = [[InlineKeyboardButton(f"{x['name']} — ₹{float(x['price']):.2f}",
                               callback_data=f"product:{x['_id']}")] for x in items]
    b.append([InlineKeyboardButton("◀️ Back", callback_data="products")])
    await q.edit_message_text(f"*{cat.title()} Products*", parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(b))

async def show_product(q, pid):
    try: item = await products.find_one({"_id": ObjectId(pid), "active": True})
    except Exception: item = None
    if not item:
        await q.answer("Product not found.", show_alert=True); return
    text = f"📦 *{item['name']}*\n\n{item.get('description','')}\n\n💰 Price: ₹{float(item['price']):.2f}"
    b = [[InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy:{pid}")],
         [InlineKeyboardButton("◀️ Back", callback_data=f"cat:{item.get('category','other')}")]]
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(b))

async def buy(q, pid):
    try: item = await products.find_one({"_id": ObjectId(pid), "active": True})
    except Exception: item = None
    if not item:
        await q.answer("Product unavailable.", show_alert=True); return
    price = float(item["price"])
    user = await users.find_one_and_update(
        {"telegram_user_id": q.from_user.id, "balance": {"$gte": price}},
        {"$inc": {"balance": -price}}, return_document=ReturnDocument.AFTER
    )
    if not user:
        await q.answer("Insufficient balance.", show_alert=True); return
    order = {"telegram_user_id": q.from_user.id, "product_id": item["_id"],
             "product_name": item["name"], "price": price, "status": "paid",
             "created_at": datetime.now(timezone.utc)}
    r = await orders.insert_one(order)
    code = item.get("license_code") or "Not provided"
    how = item.get("how_to_use") or "Instructions not provided."
    await q.edit_message_text(
        f"✅ *Purchase Successful!*\n\n📦 {item['name']}\n🧾 Order: `{r.inserted_id}`\n\n"
        f"🔑 *Code:*\n`{code}`\n\n📖 *How to Use:*\n{how}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="home")]])
    )

async def admin_panel(q):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True); return
    await q.edit_message_text("🛠️ *Admin Panel*\n\nChoose an action:",
                              parse_mode="Markdown", reply_markup=admin_menu())

async def manage(q):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True); return
    items = await products.find({}).sort("created_at", -1).to_list(50)
    if not items:
        await q.edit_message_text("📦 No products yet.", reply_markup=admin_menu()); return
    lines = ["📦 *Manage Products*\n"]; b = []
    for x in items:
        active = x.get("active", True); name = x["name"]
        lines.append(f"• {name} — ₹{float(x['price']):.2f} — {'ON' if active else 'OFF'}")
        b.append([InlineKeyboardButton(("⏸️ Disable: " if active else "▶️ Enable: ") + name,
                                       callback_data=f"toggle:{x['_id']}")])
        b.append([InlineKeyboardButton("🗑️ Delete: " + name, callback_data=f"delete:{x['_id']}")])
    b.append([InlineKeyboardButton("◀️ Admin Panel", callback_data="admin")])
    await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(b))

async def add_start(q, context):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only.", show_alert=True); return
    context.user_data["add"] = {"step": "name"}
    await q.edit_message_text("➕ *Add Product*\n\nSend product name.", parse_mode="Markdown")

async def admin_text(update, context):
    if not is_admin(update.effective_user.id): return
    s = context.user_data.get("add")
    if not s: return
    t = update.message.text.strip()
    if s["step"] == "name":
        s["name"] = t; s["step"] = "price"; await update.message.reply_text("💰 Send price.")
    elif s["step"] == "price":
        try: s["price"] = float(t)
        except ValueError: await update.message.reply_text("Send a valid number."); return
        s["step"] = "category"; await update.message.reply_text("📂 Category: digital, software, or other")
    elif s["step"] == "category":
        if t.lower() not in ("digital","software","other"):
            await update.message.reply_text("Use digital, software, or other."); return
        s["category"] = t.lower(); s["step"] = "description"; await update.message.reply_text("📝 Send description.")
    elif s["step"] == "description":
        s["description"] = t; s["step"] = "how"; await update.message.reply_text("📖 Send How to Use instructions.")
    elif s["step"] == "how":
        s["how"] = t; s["step"] = "code"; await update.message.reply_text("🔑 Send product code, or NONE.")
    elif s["step"] == "code":
        s["code"] = "" if t.upper() == "NONE" else t
        doc = {"name": s["name"], "price": s["price"], "category": s["category"],
               "description": s["description"], "how_to_use": s["how"],
               "license_code": s["code"], "active": True,
               "created_at": datetime.now(timezone.utc)}
        r = await products.insert_one(doc); context.user_data.pop("add", None)
        await update.message.reply_text(f"✅ *Product Added!*\n\n📦 {s['name']}\n🆔 `{r.inserted_id}`",
                                        parse_mode="Markdown", reply_markup=admin_menu())

async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    await update.message.reply_text("🛠️ *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu())

async def buttons(update, context):
    q = update.callback_query; await q.answer(); d = q.data
    if d == "home":
        await q.edit_message_text("💎 *DaemonTargaryen Bot*\n\nWelcome back! 👋",
                                  parse_mode="Markdown", reply_markup=menu(q.from_user.id))
    elif d == "products": await show_products(q)
    elif d.startswith("cat:"): await show_category(q, d.split(":",1)[1])
    elif d.startswith("product:"): await show_product(q, d.split(":",1)[1])
    elif d.startswith("buy:"): await buy(q, d.split(":",1)[1])
    elif d == "verify":
        await verifications.update_one({"telegram_user_id": q.from_user.id},
            {"$set": {"verified": True, "verified_at": datetime.now(timezone.utc)}}, upsert=True)
        await q.edit_message_text("✅ *Verification Complete*", parse_mode="Markdown",
                                  reply_markup=menu(q.from_user.id))
    elif d == "account":
        u = await users.find_one({"telegram_user_id": q.from_user.id}) or {}
        v = await verifications.find_one({"telegram_user_id": q.from_user.id})
        await q.edit_message_text(f"👤 *My Account*\n\n🆔 `{q.from_user.id}`\n"
                                  f"💰 Balance: ₹{float(u.get('balance',0)):.2f}\n"
                                  f"🛡️ Verified: {'Yes' if v and v.get('verified') else 'No'}",
                                  parse_mode="Markdown", reply_markup=menu(q.from_user.id))
    elif d == "orders":
        items = await orders.find({"telegram_user_id": q.from_user.id}).sort("created_at",-1).to_list(20)
        text = "📜 *Orders*\n\n" + ("\n".join(f"• {x['product_name']} — ₹{x['price']} — {x['status']}" for x in items)
                                       if items else "No orders yet.")
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=menu(q.from_user.id))
    elif d == "support":
        await q.edit_message_text("🛟 *Support*\n\nContact the administrator.",
                                  parse_mode="Markdown", reply_markup=menu(q.from_user.id))
    elif d == "admin": await admin_panel(q)
    elif d == "admin_add": await add_start(q, context)
    elif d == "admin_manage": await manage(q)
    elif d.startswith("toggle:"):
        if not is_admin(q.from_user.id): return
        oid = ObjectId(d.split(":",1)[1]); x = await products.find_one({"_id": oid})
        if x: await products.update_one({"_id":oid},{"$set":{"active":not x.get("active",True)}})
        await manage(q)
    elif d.startswith("delete:"):
        if not is_admin(q.from_user.id): return
        await products.delete_one({"_id": ObjectId(d.split(":",1)[1])}); await manage(q)

async def post_init(app):
    await users.create_index("telegram_user_id", unique=True)
    await verifications.create_index("telegram_user_id", unique=True)
    await products.create_index([("category",1),("active",1)])
    await orders.create_index([("telegram_user_id",1),("created_at",-1)])
    print("✅ MongoDB connected")
    print("✅ Bot started")

def main():
    if not BOT_TOKEN or not MONGO_URI:
        raise RuntimeError("BOT_TOKEN and MONGO_URI are required.")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text))
    app.run_polling()

if __name__ == "__main__":
    main()
