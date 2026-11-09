import os, hmac, hashlib, json, time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Update, Message, CallbackQuery,
                           InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)

# Render'dagi Environment Variables'dan olinadi (kodga yozilmaydi)
TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ["ADMIN_ID"])
APP_URL = os.environ["APP_URL"]        # https://y39999893-design.github.io/Stars-market/
BASE_URL = os.environ.get("BASE_URL") or os.environ["RENDER_EXTERNAL_URL"]  # Render avtomatik beradi
ORIGIN = os.environ.get("ORIGIN", "https://y39999893-design.github.io")
CARD = os.environ["CARD"]              # karta raqami
CARD_OWNER = os.environ.get("CARD_OWNER", "")
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

bot = Bot(TOKEN)
dp = Dispatcher()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN],
                   allow_methods=["*"], allow_headers=["*"])

ORDERS = {}   # buyurtmalar xotirada (Render qayta ishga tushsa tozalanadi)
LAST = {}     # mijoz -> oxirgi buyurtma raqami


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


@app.post("/api/order")
async def order(req: Request):
    b = await req.json()
    u = check_init(b.get("initData", ""))
    if not u:
        raise HTTPException(401)
    oid = str(int(time.time()))[-6:] + str(u["id"])[-3:]
    o = {"id": oid, "uid": u["id"], "name": str(b["item"])[:60],
         "price": int(b["price"]), "to": str(b["to"])[:33]}
    try:
        await bot.send_message(
            u["id"],
            f"Buyurtma #{oid}\n{o['name']} → @{o['to']}\n"
            f"Summa: {o['price']:,} soʻm\n\n"
            f"Toʻlov kartasi:\n{CARD}\n{CARD_OWNER}\n\n"
            f"Pulni oʻtkazing va chek (skrinshot) rasmini shu yerga yuboring.")
    except Exception:
        return {"ok": False, "err": "start"}   # mijoz botda /start bosmagan
    ORDERS[oid] = o
    LAST[u["id"]] = oid
    return {"ok": True, "id": oid}


@dp.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍 Doʻkon", web_app=WebAppInfo(url=APP_URL))]])
    await m.answer("Xush kelibsiz! Doʻkonni ochish uchun tugmani bosing.",
                   reply_markup=kb)


@dp.message(F.photo)
async def receipt(m: Message):
    o = ORDERS.get(LAST.get(m.from_user.id))
    if not o:
        return await m.answer("Avval doʻkondan buyurtma bering.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash",
                             callback_data=f"ok:{o['id']}:{o['uid']}"),
        InlineKeyboardButton(text="❌ Rad etish",
                             callback_data=f"no:{o['id']}:{o['uid']}")]])
    await bot.send_photo(
        ADMIN, m.photo[-1].file_id, reply_markup=kb,
        caption=(f"Buyurtma #{o['id']}\n{o['name']} → @{o['to']}\n"
                 f"{o['price']:,} soʻm (chekdagi summa bilan solishtiring)\n"
                 f"Mijoz: {m.from_user.full_name} (id {m.from_user.id})"))
    await m.answer("Chek qabul qilindi. Admin tekshirib, tez orada javob beradi.")


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decide(c: CallbackQuery):
    if c.from_user.id != ADMIN:
        return await c.answer("Ruxsat yoʻq", show_alert=True)
    act, oid, uid = c.data.split(":")
    status = "Bajarildi ✅" if act == "ok" else "Rad etildi ❌"
    await c.message.edit_caption(caption=c.message.caption + f"\n\n{status}")
    await bot.send_message(int(uid), f"Buyurtma #{oid}: {status}")
    await c.answer()
