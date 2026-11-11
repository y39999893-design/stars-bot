import os, re, asyncio, hmac, hashlib, json, time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
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
REMIND_TEXT = os.environ.get("REMIND_TEXT",
    "Salom! Doʻkonimizda Telegram Stars, Premium va giftlar bor. Buyurtma berish uchun tugmani bosing 👇")
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]

bot = Bot(TOKEN)
dp = Dispatcher()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN],
                   allow_methods=["*"], allow_headers=["*"])

STATUSES = ("Kutilmoqda", "Tekshirilmoqda", "Bajarildi", "Rad etildi", "Bekor qilindi")


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
    t = asyncio.create_task(background())
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)


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


# Baza boʻsh boʻlsa, doʻkondagi namuna narxlar (STAR = 220 soʻm)
DEFAULTS = (
    [(f"s{n}", "stars", f"{n} Stars", n * 220) for n in (50, 100, 250, 500, 1000, 2500)]
    + [("p3", "premium", "3 oy", 170000), ("p6", "premium", "6 oy", 230000),
       ("p12", "premium", "12 oy", 400000)]
    + [(f"g{i}", "gifts", f"{st} Stars", st * 220)
       for i, st in enumerate((15, 15, 25, 25, 50, 50, 50, 100))]
    + [(f"n{n}", "nft", f"NFT gift #{n}", 900000 * n) for n in (1, 2, 3, 4)]
)
PENDING = "in.(Kutilmoqda,Tekshirilmoqda)"


def strip_emoji(n: str) -> str:
    return re.sub(r"^[^\w]+", "", n).strip()


async def catalog():
    rows = await sb("GET", "/prices", params={"select": "*", "active": "eq.true"})
    if rows:
        return [{"cat": r["cat"], "name": r["name"], "price": int(r["price"])} for r in rows]
    return [{"cat": c, "name": n, "price": p} for _, c, n, p in DEFAULTS]


def price_ok(item: str, price: int, items) -> bool:
    """Mijoz yuborgan narx serverdagi narxga mos kelishini tekshiradi."""
    for i in items:
        if item in (i["name"], strip_emoji(i["name"])) and i["price"] == price:
            return True
    m = re.fullmatch(r"(\d{2,6}) Stars", item)
    if m and 50 <= int(m.group(1)) <= 100000:
        best = None
        for i in items:
            k = re.match(r"\d+", i["name"])
            if i["cat"] == "stars" and k and int(k.group()) > 0:
                if best is None or i["price"] < best[1]:
                    best = (int(k.group()), i["price"])
        rate = best[1] / best[0] if best else 220
        return abs(price - int(m.group(1)) * rate) <= 1
    return False


@app.post("/api/order")
async def order(req: Request):
    b = await req.json()
    u = check_init(b.get("initData", ""))
    if not u:
        raise HTTPException(401)
    try:
        item = str(b["item"]).strip()[:60]
        price = int(b["price"])
        to = str(b["to"]).strip().lstrip("@")
    except Exception:
        raise HTTPException(400)
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", to):
        raise HTTPException(400)
    try:
        items = await catalog()
    except Exception:
        return {"ok": False, "err": "db"}
    if not price_ok(item, price, items):
        return {"ok": False, "err": "price"}
    pending = await sb_safe("GET", "/orders", params={
        "select": "id", "uid": f"eq.{u['id']}", "status": PENDING})
    if len(pending) >= 3:
        try:
            await bot.send_message(u["id"], "Sizda 3 ta tugallanmagan buyurtma bor. Avval "
                                   "ularning chekini yuboring yoki bekor qiling.")
        except Exception:
            pass
        return {"ok": False, "err": "limit"}
    oid = str(int(time.time()))[-6:] + str(u["id"])[-3:]
    o = {"id": oid, "uid": u["id"], "item": item, "price": price, "to_user": to}
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data=f"cx:{oid}")]])
    try:
        await bot.send_message(
            u["id"],
            f"Buyurtma #{oid}\n{item} → @{to}\n"
            f"Summa: {price:,} soʻm\n\n"
            f"Toʻlov kartasi:\n{CARD}\n{CARD_OWNER}\n\n"
            f"Pulni oʻtkazing va chek (rasm yoki fayl) ni shu yerga yuboring.\n"
            f"48 soat ichida chek yuborilmasa, buyurtma bekor boʻladi.", reply_markup=kb)
    except Exception:
        return {"ok": False, "err": "start"}   # mijoz botda /start bosmagan
    try:
        await sb("POST", "/orders", o)
    except Exception:
        return {"ok": False, "err": "db"}
    name = " ".join(filter(None, [u.get("first_name"), u.get("last_name")]))
    await save_user(u["id"], name, u.get("username"))
    try:
        await bot.send_message(ADMIN, f"🆕 Yangi buyurtma #{oid}\n{item} → @{to}\n"
                                      f"{price:,} soʻm\nMijoz: {name} (id {u['id']})")
    except Exception:
        pass
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
    if b.get("delete"):
        await sb("DELETE", "/prices", params={"id": "eq." + str(b["delete"])})
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


# ---------- Rejali xabarlar, eslatma, uygʻoq turish ----------
def shop_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛍 Doʻkon", web_app=WebAppInfo(url=APP_URL))]])


async def audience():
    users = await sb_safe("GET", "/users", params={"select": "uid,blocked", "limit": "20000"})
    orders = await sb_safe("GET", "/orders", params={"select": "uid", "limit": "20000"})
    blocked = {u["uid"] for u in users if u.get("blocked")}
    return sorted(({u["uid"] for u in users} | {o["uid"] for o in orders}) - blocked)


async def run_reminders():
    """/start bosib, 24 soat ichida buyurtma bermaganlarga bir marta eslatma."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    users = await sb_safe("GET", "/users", params={
        "select": "uid", "reminded": "eq.false", "blocked": "eq.false",
        "created_at": "lt." + cutoff, "limit": "200"})
    if not users:
        return
    ids = [u["uid"] for u in users]
    orders = await sb_safe("GET", "/orders", params={
        "select": "uid", "uid": "in.(" + ",".join(map(str, ids)) + ")"})
    ordered = {o["uid"] for o in orders}
    for uid in ids:
        await sb_safe("PATCH", "/users", {"reminded": True}, params={"uid": f"eq.{uid}"})
        if uid in ordered or uid == ADMIN:
            continue
        try:
            await bot.send_message(uid, REMIND_TEXT, reply_markup=shop_kb())
        except TelegramForbiddenError:
            await sb_safe("PATCH", "/users", {"blocked": True}, params={"uid": f"eq.{uid}"})
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def run_scheduled():
    now = datetime.now(timezone.utc).isoformat()
    rows = await sb_safe("GET", "/scheduled", params={
        "select": "*", "sent": "eq.false", "send_at": "lte." + now,
        "order": "send_at", "limit": "5"})
    for r in rows:
        await sb_safe("PATCH", "/scheduled", {"sent": True}, params={"id": f"eq.{r['id']}"})
        await run_broadcast(r["text"], await audience())


async def run_expire():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    rows = await sb_safe("PATCH", "/orders", {"status": "Bekor qilindi"},
                         params={"status": "eq.Kutilmoqda", "created_at": "lt." + cutoff})
    for r in rows:
        try:
            await bot.send_message(r["uid"], f"Buyurtma #{r['id']} 48 soat ichida chek "
                                   f"yuborilmagani uchun bekor qilindi.")
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def background():
    """Har 5 daqiqada: oʻziga soʻrov (Render uxlab qolmasin), eslatma, rejali xabar."""
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(BASE_URL + "/", timeout=aiohttp.ClientTimeout(total=20))
        except Exception:
            pass
        try:
            await run_reminders()
        except Exception:
            pass
        try:
            await run_scheduled()
        except Exception:
            pass
        try:
            await run_expire()
        except Exception:
            pass
        await asyncio.sleep(300)


@app.post("/api/admin/schedule")
async def a_schedule(req: Request):
    b = await req.json()
    admin_only(b)
    text = str(b.get("text", "")).strip()[:1000]
    try:
        at = datetime.fromisoformat(str(b["at"]).replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400)
    if not text:
        raise HTTPException(400)
    await sb("POST", "/scheduled", {"text": text, "send_at": at.isoformat()})
    return {"ok": True}


@app.post("/api/admin/scheduled")
async def a_scheduled(req: Request):
    admin_only(await req.json())
    return await sb("GET", "/scheduled", params={"select": "*", "sent": "eq.false",
                                                 "order": "send_at"})


@app.post("/api/admin/schedule_del")
async def a_schedule_del(req: Request):
    b = await req.json()
    admin_only(b)
    await sb("DELETE", "/scheduled", params={"id": "eq." + str(int(b["id"]))})
    return {"ok": True}


# ---------- Bot ----------
HELP = os.environ.get("HELP_TEXT") or (
    "📖 Qoidalar\n\n"
    "1. /start bosing va Doʻkonni oching\n"
    "2. Mahsulot va qabul qiluvchi username'ni tanlab buyurtma bering\n"
    "3. Bot karta raqamini yuboradi, pulni oʻtkazing\n"
    "4. Chekni (rasm yoki fayl) shu botga yuboring\n"
    "5. Admin tekshirib tasdiqlaydi va mahsulot yetkaziladi\n\n"
    "Chek 48 soat ichida yuborilmasa, buyurtma avtomatik bekor boʻladi.\n"
    "Savol boʻlsa: @Murodov1805")


@dp.message(CommandStart())
async def start(m: Message):
    await save_user(m.from_user.id, m.from_user.full_name, m.from_user.username)
    rows = [[InlineKeyboardButton(text="🛍 Doʻkon", web_app=WebAppInfo(url=APP_URL))]]
    if m.from_user.id == ADMIN:
        rows.append([InlineKeyboardButton(text="⚙️ Admin panel",
                                          web_app=WebAppInfo(url=APP_URL + "panel2.html"))])
    await m.answer("Xush kelibsiz! Doʻkonni ochish uchun tugmani bosing.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(HELP, reply_markup=shop_kb())


@dp.message(F.photo | F.document)
async def receipt(m: Message):
    try:
        rows = await sb("GET", "/orders", params={
            "uid": f"eq.{m.from_user.id}", "status": "eq.Kutilmoqda", "select": "*",
            "order": "created_at.desc", "limit": "5"})
    except Exception:
        return await m.answer("Xatolik yuz berdi. Birozdan keyin qayta yuboring.")
    if not rows:
        return await m.answer("Kutilayotgan buyurtma yoʻq. Avval doʻkondan buyurtma bering.")
    o = rows[0]
    mm = re.search(r"\d{9}", m.caption or "")
    if mm:
        o = next((r for r in rows if r["id"] == mm.group()), o)
    await sb("PATCH", "/orders", {"status": "Tekshirilmoqda"}, params={"id": "eq." + o["id"]})
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{o['id']}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"no:{o['id']}")]])
    cap = (f"Buyurtma #{o['id']}\n{o['item']} → @{o['to_user']}\n"
           f"{o['price']:,} soʻm (chekdagi summa bilan solishtiring)\n"
           f"Mijoz: {m.from_user.full_name} (id {m.from_user.id})")
    if m.photo:
        await bot.send_photo(ADMIN, m.photo[-1].file_id, caption=cap, reply_markup=kb)
    else:
        await bot.send_document(ADMIN, m.document.file_id, caption=cap, reply_markup=kb)
    await m.answer(f"Buyurtma #{o['id']} uchun chek qabul qilindi. "
                   f"Admin tekshirib, tez orada javob beradi.")


@dp.callback_query(F.data.startswith("cx:"))
async def cancel(c: CallbackQuery):
    oid = c.data[3:]
    rows = await sb_safe("PATCH", "/orders", {"status": "Bekor qilindi"}, params={
        "id": "eq." + oid, "uid": f"eq.{c.from_user.id}", "status": "eq.Kutilmoqda"})
    if not rows:
        return await c.answer("Bu buyurtmani bekor qilib boʻlmaydi", show_alert=True)
    try:
        await c.message.edit_text(c.message.text + "\n\nBekor qilindi ❌")
    except Exception:
        pass
    await c.answer("Bekor qilindi")


@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def decide(c: CallbackQuery):
    if c.from_user.id != ADMIN:
        return await c.answer("Ruxsat yoʻq", show_alert=True)
    act, oid = c.data.split(":")
    st = "Bajarildi" if act == "ok" else "Rad etildi"
    rows = await sb("PATCH", "/orders", {"status": st},
                    params={"id": "eq." + oid, "status": PENDING})
    if not rows:
        return await c.answer("Bu buyurtma allaqachon yopilgan", show_alert=True)
    await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{st}")
    await bot.send_message(rows[0]["uid"], f"Buyurtma #{oid}: {st}")
    await c.answer()


@dp.message(F.text)
async def fallback(m: Message):
    await m.answer(HELP, reply_markup=shop_kb())
