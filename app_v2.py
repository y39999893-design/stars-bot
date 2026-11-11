import os, asyncio, hmac, hashlib, json, time
from urllib.parse import parse_qsl

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
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


TASKS = set()   # fon vazifalari (xabar tarqatish)


async def sb_safe(*a, **k):
    try:
        return await sb(*a, **k)
    except Exception:
        return []


async def save_user(uid, name, username):
    """Mijozni users jadvaliga yozadi (xato boʻlsa, jim oʻtib ketadi)."""
    try:
        await sb("POST", "/users",
                 [{"uid": uid, "name": (name or "")[:100],
                   "username": (username or "")[:40], "blocked": False}],
                 params={"on_conflict": "uid"}, upsert=True)
    except Exception:
        pass


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
    await save_user(u["id"], " ".join(filter(None, [u.get("first_name"), u.get("last_name")])),
                    u.get("username"))
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


@app.post("/api/admin/customers")
async def a_customers(req: Request):
    admin_only(await req.json())
    users = await sb_safe("GET", "/users", params={"select": "*", "limit": "20000"})
    orders = await sb("GET", "/orders", params={"select": "uid,price,status,created_at",
                                                "limit": "20000"})
    C = {u["uid"]: {"uid": u["uid"], "name": u.get("name"), "username": u.get("username"),
                    "orders": 0, "spent": 0, "last": None} for u in users}
    for o in orders:
        c = C.setdefault(o["uid"], {"uid": o["uid"], "name": None, "username": None,
                                    "orders": 0, "spent": 0, "last": None})
        c["orders"] += 1
        if o["status"] == "Bajarildi":
            c["spent"] += o["price"]
        if not c["last"] or o["created_at"] > c["last"]:
            c["last"] = o["created_at"]
    return sorted(C.values(), key=lambda c: (c["last"] or "", c["uid"]), reverse=True)


async def run_broadcast(text, uids):
    ok = fail = 0
    for uid in uids:
        try:
            try:
                await bot.send_message(uid, text)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.send_message(uid, text)
            ok += 1
        except TelegramForbiddenError:
            fail += 1
            await sb_safe("PATCH", "/users", {"blocked": True}, params={"uid": f"eq.{uid}"})
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    try:
        await bot.send_message(ADMIN, f"Xabar tarqatish tugadi.\nYetkazildi: {ok}\nYetmadi: {fail}")
    except Exception:
        pass


@app.post("/api/admin/broadcast")
async def a_broadcast(req: Request):
    b = await req.json()
    admin_only(b)
    text = str(b.get("text", "")).strip()[:1000]
    if not text:
        raise HTTPException(400)
    users = await sb_safe("GET", "/users", params={"select": "uid,blocked", "limit": "20000"})
    orders = await sb("GET", "/orders", params={"select": "uid", "limit": "20000"})
    blocked = {u["uid"] for u in users if u.get("blocked")}
    uids = ({u["uid"] for u in users} | {o["uid"] for o in orders}) - blocked
    t = asyncio.create_task(run_broadcast(text, sorted(uids)))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return {"ok": True, "total": len(uids)}


# ---------- Bot ----------
@dp.message(CommandStart())
async def start(m: Message):
    await save_user(m.from_user.id, m.from_user.full_name, m.from_user.username)
    rows = [[InlineKeyboardButton(text="🛍 Doʻkon", web_app=WebAppInfo(url=APP_URL))]]
    if m.from_user.id == ADMIN:
        rows.append([InlineKeyboardButton(text="⚙️ Admin panel",
                                          web_app=WebAppInfo(url=APP_URL + "panel.html"))])
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
