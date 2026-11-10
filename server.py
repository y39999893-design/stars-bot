import os, hmac, hashlib, json, time
from urllib.parse import parse_qsl

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Update, Message, CallbackQuery,
                           InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)

# Render'dagi Environment Variables'dan olinadi
TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_ID"])
APP_URL = os.environ["APP_URL"]                 # https://y39999893-design.github.io/Stars-market/
BASE_URL = os.environ.get("BASE_URL") or os.environ["RENDER_EXTERNAL_URL"]
ORIGIN = os.environ.get("ORIGIN", "https://y39999893-design.github.io")
CARD = os.environ["CARD"]
CARD_OWNER = os.environ.get("CARD_OWNER", "")
SB_URL = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1"
SB_KEY = os.environ["SUPABASE_KEY"]
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

bot = Bot(TOKEN)
dp = Dispatcher()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN],
                   allow_methods=["*"], allow_headers=["*"])

STATUSES = ("Kutilmoqda", "Tekshirilmoqda", "Bajarildi", "Rad etildi")


async def sb(method, path, body=None, params=None, upsert=False):
    """Supabase (PostgREST) ga so'rov."""
    h = {"apikey": SB_KEY, "Content-Type": "application/json",
         "Prefer": "return=representation" + (",resolution=merge-duplicates" if upsert else "")}
    if not SB_KEY.startswith("sb_"):
        h["Authorization"] = "Bearer " + SB_KEY
    async with aiohttp.ClientSession() as s:
        async with s.request(method, SB_URL + path, headers=h, json=body, params=params) as r:
            t = await r.text()
            if r.status >= 300:
                raise RuntimeError(t)
            return json.loads(t) if t else []


def check_init(init_data: str):
    """Telegram Mini App initData imzosini tekshiradi."""
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(key, s.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, got):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        return None
    return json.loads(data["user"])


def admin_only(b: dict):
    u = check_init(b.get("initData", ""))
    if not u or u["id"] != ADMIN:
        raise HTTPException(403)


@app.get("/")
async def root():
    return {"ok": True}


@app.on_event("startup")
async def startup():
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=SECRET,
                          drop_pending_updates=True)


@app.post("/webhook")
async def webhook(req: Request):
    if req.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        raise HTTPException(403)
    update = Update.model_validate(await req.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


# ---------- Mijoz API ----------
@app.get("/api/prices")
async def prices():
    return await sb("GET", "/prices", params={"select": "*", "active": "eq.true",
                                              "order": "cat,price"})


@app.post("/api/order")
async def order(req: Request):
    b = await req.json()
    u = check_init(b.get("initData", ""))
    if not u:
        raise HTTPException(401)
    oid = str(int(time.time()))[-6:] + str(u["id"])[-3:]
    o = {"id": oid, "uid": u["id"], "item": str(b["item"])[:60],
         "price": int(b["price"]), "to_user": str(b["to"])[:33]}
    try:
        await bot.send_message(
            u["id"],
            f"Buyurtma #{oid}\n{o['item']} → @{o['to_user']}\n"
            f"Summa: {o['price']:,} soʻm\n\n"
            f"Toʻlov kartasi:\n{CARD}\n{CARD_OWNER}\n\n"
            f"Pulni oʻtkazing va chek (skrinshot) rasmini shu yerga yuboring.")
    except Exception:
        return {"ok": False, "err": "start"}   # mijoz botda /start bosmagan
    try:
        await sb("POST", "/orders", o)
    except Exception:
        return {"ok": False, "err": "db"}
    return {"ok": True, "id": oid}


@app.post("/api/my_orders")
async def my_orders(req: Request):
    u = check_init((await req.json()).get("initData", ""))
    if not u:
        raise HTTPException(401)
    return await sb("GET", "/orders", params={"uid": f"eq.{u['id']}", "select": "*",
                                              "order": "created_at.desc", "limit": "50"})


# ---------- Admin API ----------
@app.post("/api/admin/stats")
async def a_stats(req: Request):
    admin_only(await req.json())
    rows = await sb("GET", "/orders", params={"select": "uid,price,status", "limit": "10000"})
    done = [r for r in rows if r["status"] == "Bajarildi"]
    return {"orders": len(rows), "done": len(done),
            "sum": sum(r["price"] for r in done),
            "customers": len({r["uid"] for r in rows}),
            "waiting": sum(1 for r in rows if r["status"] in ("Kutilmoqda", "Tekshirilmoqda"))}


@app.post("/api/admin/orders")
async def a_orders(req: Request):
    admin_only(await req.json())
    return await sb("GET", "/orders", params={"select": "*", "order": "created_at.desc",
                                              "limit": "200"})


@app.post("/api/admin/status")
async def a_status(req: Request):
    b = await req.json()
    admin_only(b)
    st = b.get("status")
    if st not in STATUSES:
        raise HTTPException(400)
    rows = await sb("PATCH", "/orders", {"status": st}, params={"id": "eq." + str(b["id"])})
    if rows and st in ("Bajarildi", "Rad etildi"):
        try:
            await bot.send_message(rows[0]["uid"], f"Buyurtma #{rows[0]['id']}: {st}")
        except Exception:
            pass
    return {"ok": True}


@app.post("/api/admin/prices")
async def a_prices(req: Request):
    b = await req.json()
    admin_only(b)
    items = b.get("items")
    if items:
        clean = [{"id": str(i["id"])[:30], "cat": str(i["cat"])[:20],
                  "name": str(i["name"])[:60], "price": int(i["price"]),
                  "active": bool(i.get("active", True))} for i in items]
        await sb("POST", "/prices", clean, params={"on_conflict": "id"}, upsert=True)
    return await sb("GET", "/prices", params={"select": "*", "order": "cat,price"})


# ---------- Bot ----------
@dp.message(CommandStart())
async def start(m: Message):
    rows = [[InlineKeyboardButton(text="🛍 Doʻkon", web_app=WebAppInfo(url=APP_URL))]]
    if m.from_user.id == ADMIN:
        rows.append([InlineKeyboardButton(text="⚙️ Admin panel",
                                          web_app=WebAppInfo(url=APP_URL + "admin.html"))])
    await m.answer("Xush kelibsiz! Doʻkonni ochish uchun tugmani bosing.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(F.photo)
async def receipt(m: Message):
    rows = await sb("GET", "/orders", params={"uid": f"eq.{m.from_user.id}",
                                              "status": "eq.Kutilmoqda", "select": "*",
                                              "order": "created_at.desc", "limit": "1"})
    if not rows:
        return await m.answer("Avval doʻkondan buyurtma bering.")
    o = rows[0]
    await sb("PATCH", "/orders", {"status": "Tekshirilmoqda"}, params={"id": "eq." + o["id"]})
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{o['id']}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"no:{o['id']}")]])
    await bot.send_photo(
        ADMIN, m.photo[-1].file_id, reply_markup=kb,
        caption=(f"Buyurtma #{o['id']}\n{o['item']} → @{o['to_user']}\n"
                 f"{o['price']:,} soʻm (chekdagi summa bilan solishtiring)\n"
                 f"Mijoz: {m.from_user.full_name} (id {m.from_user.id})"))
    await m.answer("Chek qabul qilindi. Admin tekshirib, tez orada javob beradi.")


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decide(c: CallbackQuery):
    if c.from_user.id != ADMIN:
        return await c.answer("Ruxsat yoʻq", show_alert=True)
    act, oid = c.data.split(":")
    st = "Bajarildi" if act == "ok" else "Rad etildi"
    rows = await sb("PATCH", "/orders", {"status": st}, params={"id": "eq." + oid})
    await c.message.edit_caption(caption=c.message.caption + f"\n\n{st}")
    if rows:
        await bot.send_message(rows[0]["uid"], f"Buyurtma #{oid}: {st}")
    await c.answer()
