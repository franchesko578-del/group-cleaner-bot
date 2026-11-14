"""
Group Cleaner + Auto Avatar bot  (Python 3.10+, aiogram 3, SQLite, Pillow)

- Guruhda bot admin qilinganda: xizmat xabarlarini o'chiradi va 1 marta avatar qo'yadi.
- Guruhda hech narsa yozmaydi (silent).
- Sozlamalar faqat OWNER uchun (private chatdagi panel).
"""
import asyncio
import hashlib
import io
import logging
import os
import random
import time
from html import escape

import aiosqlite
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllChatAdministrators,
                           BotCommandScopeAllPrivateChats,
                           BotCommandScopeDefault, BufferedInputFile,
                           CallbackQuery, ChatMemberUpdated, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

def _load_env(path: str = ".env"):
    """Oddiy .env o'qish (qo'shimcha kutubxonasiz)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
    except FileNotFoundError:
        pass


_load_env()

TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "7507192517"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
START_TS = time.time()
PAGE_SIZE = 8

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

router = Router()
db: aiosqlite.Connection
_tasks: set = set()


def spawn(coro):
    """Fon vazifasini GC dan saqlab ishga tushirish."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t

# --------------------------------------------------------------------------
# Xizmat xabarlari turlari
# --------------------------------------------------------------------------
_SERVICE_NAMES = [
    "NEW_CHAT_MEMBERS", "LEFT_CHAT_MEMBER", "NEW_CHAT_TITLE", "NEW_CHAT_PHOTO",
    "DELETE_CHAT_PHOTO", "GROUP_CHAT_CREATED", "SUPERGROUP_CHAT_CREATED",
    "CHANNEL_CHAT_CREATED", "MESSAGE_AUTO_DELETE_TIMER_CHANGED",
    "MIGRATE_TO_CHAT_ID", "MIGRATE_FROM_CHAT_ID", "PINNED_MESSAGE",
    "VIDEO_CHAT_SCHEDULED", "VIDEO_CHAT_STARTED", "VIDEO_CHAT_ENDED",
    "VIDEO_CHAT_PARTICIPANTS_INVITED", "FORUM_TOPIC_CREATED",
    "FORUM_TOPIC_EDITED", "FORUM_TOPIC_CLOSED", "FORUM_TOPIC_REOPENED",
    "GENERAL_FORUM_TOPIC_HIDDEN", "GENERAL_FORUM_TOPIC_UNHIDDEN",
    "WRITE_ACCESS_ALLOWED", "BOOST_ADDED", "CHAT_BACKGROUND_SET",
    "PROXIMITY_ALERT_TRIGGERED", "USERS_SHARED", "CHAT_SHARED",
    "GIVEAWAY_CREATED", "GIVEAWAY_COMPLETED", "PAID_MESSAGE_PRICE_CHANGED",
    "CHECKLIST_TASKS_DONE", "CHECKLIST_TASKS_ADDED",
    "DIRECT_MESSAGE_PRICE_CHANGED", "GIFT", "UNIQUE_GIFT",
]
SERVICE_TYPES = {getattr(ContentType, n) for n in _SERVICE_NAMES
                 if hasattr(ContentType, n)}
GROUPS = {"group", "supergroup"}
CHATS = GROUPS | {"channel"}

# --------------------------------------------------------------------------
# Baza
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
    chat_id INTEGER PRIMARY KEY,
    title TEXT DEFAULT '',
    cleaner INTEGER DEFAULT 1,
    avatar INTEGER DEFAULT 1,
    rename INTEGER DEFAULT 0,
    avatar_done INTEGER DEFAULT 0,
    is_admin INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    deleted INTEGER DEFAULT 0,
    added_at INTEGER DEFAULT 0,
    delay INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, text TEXT);
"""


async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    try:  # eski bazalar uchun
        await db.execute("ALTER TABLE chats ADD COLUMN delay INTEGER DEFAULT 0")
    except aiosqlite.OperationalError:
        pass
    for k, v in (("cleaner", "1"), ("avatar", "1"),
                 ("deleted_total", "0"), ("avatars_total", "0")):
        await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    await db.commit()


async def get_setting(key: str) -> str:
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
        r = await c.fetchone()
    return r["value"] if r else "0"


async def set_setting(key: str, value: str):
    await db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
    await db.commit()


async def incr_setting(key: str, n: int = 1):
    await set_setting(key, str(int(await get_setting(key)) + n))


async def log_error(text: str):
    log.error(text)
    await db.execute("INSERT INTO errors(ts,text) VALUES(?,?)",
                     (int(time.time()), text[:500]))
    await db.commit()


async def get_chat(chat_id: int):
    async with db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)) as c:
        return await c.fetchone()


async def ensure_chat(chat_id: int, title: str, is_admin: int | None = None):
    await db.execute(
        "INSERT OR IGNORE INTO chats(chat_id,title,added_at) VALUES(?,?,?)",
        (chat_id, title or "", int(time.time())))
    await db.execute("UPDATE chats SET title=?, active=1 WHERE chat_id=?",
                     (title or "", chat_id))
    if is_admin is not None:
        await db.execute("UPDATE chats SET is_admin=? WHERE chat_id=?",
                         (is_admin, chat_id))
    await db.commit()
    return await get_chat(chat_id)


async def effective(chat_id: int):
    row = await get_chat(chat_id)
    gc = await get_setting("cleaner") == "1"
    ga = await get_setting("avatar") == "1"
    return row, bool(gc and row and row["cleaner"]), bool(ga and row and row["avatar"])


# --------------------------------------------------------------------------
# Avatar generatsiya (Pillow)
# --------------------------------------------------------------------------
# (gradient 1, gradient 2, matn rangi) — yorqin, e'tibor tortadigan ranglar
PALETTES = [
    ((255, 23, 23), (255, 99, 71), (255, 255, 255)),     # qizil
    ((0, 200, 83), (0, 230, 118), (255, 255, 255)),      # yashil
    ((0, 112, 255), (0, 200, 255), (255, 255, 255)),     # ko'k
    ((255, 214, 0), (255, 160, 0), (40, 40, 40)),        # sariq
    ((170, 0, 255), (224, 64, 251), (255, 255, 255)),    # binafsha
    ((255, 0, 128), (255, 110, 199), (255, 255, 255)),   # pushti
    ((0, 229, 255), (0, 176, 255), (30, 30, 30)),        # firuza
    ((255, 109, 0), (255, 171, 0), (255, 255, 255)),     # to'q sariq
]
FONT_PATHS = [
    "assets/font.ttf",
    "/system/fonts/Roboto-Bold.ttf",
    "/system/fonts/Roboto-Regular.ttf",
    "/system/fonts/DroidSans-Bold.ttf",
    "/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _clean_title(title: str) -> str:
    t = "".join(ch for ch in (title or "") if ord(ch) <= 0xFFFF).strip()
    return t or "Group"


def make_avatar(title: str, idx: int) -> bytes:
    S = 640
    title = _clean_title(title)
    c1, c2, tc = PALETTES[idx % len(PALETTES)]

    img = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(img)
    for i in range(2 * S):
        t = i / (2 * S)
        col = tuple(int(c1[k] + (c2[k] - c1[k]) * t) for k in range(3))
        d.line([(i, 0), (0, i)], fill=col, width=3)

    over = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(over).ellipse([S // 2 - 170, 70, S // 2 + 170, 410],
                                 fill=(255, 255, 255, 50))
    img = Image.alpha_composite(img.convert("RGBA"), over)
    d = ImageDraw.Draw(img)

    words = [w for w in title.split() if any(c.isalnum() for c in w)]
    initials = "".join(next((c for c in w if c.isalnum()), "") for w in words[:2]).upper() or "G"
    f_big = _font(150 if len(initials) > 1 else 180)
    d.text((S // 2, 240), initials, font=f_big, fill=tc, anchor="mm")

    f_t = _font(46)
    maxw = 400
    lines, cur = [], ""
    for w in title.split():
        test = f"{cur} {w}".strip()
        if d.textlength(test, font=f_t) <= maxw:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > 2:
        lines = lines[:2]
        lines[1] = lines[1].rstrip() + "…"
    for i, ln in enumerate(lines):
        while d.textlength(ln, font=f_t) > maxw and len(ln) > 1:
            ln = ln[:-2] + "…"
        d.text((S // 2, 420 + i * 58), ln, font=f_t, fill=tc, anchor="mm")

    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


async def pick_color(chat_id: int) -> int:
    """Oxirgisidan farq qiladigan tasodifiy rang."""
    key = f"lc:{chat_id}"
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
        r = await c.fetchone()
    last = int(r["value"]) if r else -1
    idx = random.choice([i for i in range(len(PALETTES)) if i != last])
    await set_setting(key, str(idx))
    return idx


async def set_group_avatar(bot: Bot, chat_id: int, title: str) -> tuple[bool, str]:
    try:
        idx = await pick_color(chat_id)
        data = await asyncio.to_thread(make_avatar, title, idx)
        await bot.set_chat_photo(chat_id, BufferedInputFile(data, "avatar.png"))
    except TelegramAPIError as e:
        await log_error(f"avatar {chat_id}: {e}")
        return False, str(e)
    except Exception as e:  # noqa
        await log_error(f"avatar-gen {chat_id}: {e!r}")
        return False, repr(e)
    await db.execute("UPDATE chats SET avatar_done=1 WHERE chat_id=?", (chat_id,))
    await db.commit()
    await incr_setting("avatars_total")
    return True, "ok"


async def notify_owner(bot: Bot, text: str):
    try:
        await bot.send_message(OWNER_ID, text)
    except TelegramAPIError:
        pass  # owner botni hali /start qilmagan bo'lishi mumkin


# --------------------------------------------------------------------------
# Guruh: tozalash va avatar (guruhda hech narsa yozilmaydi)
# --------------------------------------------------------------------------
@router.my_chat_member(F.chat.type.in_(CHATS))
async def on_bot_status(u: ChatMemberUpdated, bot: Bot):
    chat, new = u.chat, u.new_chat_member
    if new.status in ("left", "kicked"):
        await db.execute("UPDATE chats SET active=0,is_admin=0 WHERE chat_id=?",
                         (chat.id,))
        await db.commit()
        return

    is_admin = new.status == "administrator"
    row = await ensure_chat(chat.id, chat.title, int(is_admin))
    if not is_admin:
        return

    _, _, avatar_on = await effective(chat.id)
    can_info = bool(getattr(new, "can_change_info", False))
    can_del = bool(getattr(new, "can_delete_messages", False))
    msg = f"✅ <b>{escape(chat.title or '')}</b>: bot admin qilindi."

    if avatar_on and not row["avatar_done"] and can_info:
        ok, err = await set_group_avatar(bot, chat.id, chat.title)
        msg += "\n🖼 Avatar qo'yildi." if ok else f"\n⚠️ Avatar xatosi: {escape(err)}"
    elif avatar_on and not row["avatar_done"]:
        msg += "\n⚠️ Avatar uchun «Guruh ma'lumotlarini o'zgartirish» huquqi kerak."
    if not can_del:
        msg += "\n⚠️ Tozalash uchun «Xabarlarni o'chirish» huquqi kerak."
    await notify_owner(bot, msg)


@router.message(F.chat.type.in_(GROUPS), F.content_type.in_(SERVICE_TYPES))
@router.channel_post(F.chat.type == "channel", F.content_type.in_(SERVICE_TYPES))
async def clean_service(m: Message, bot: Bot):
    if m.migrate_to_chat_id:  # oddiy guruh -> supergroup
        try:
            await db.execute("UPDATE chats SET chat_id=? WHERE chat_id=?",
                             (m.migrate_to_chat_id, m.chat.id))
            await db.commit()
        except Exception as e:  # noqa
            await log_error(f"migrate: {e!r}")
        return

    row = await get_chat(m.chat.id) or await ensure_chat(m.chat.id, m.chat.title)
    _, cleaner_on, avatar_on = await effective(m.chat.id)

    if cleaner_on:
        delay = int(row["delay"] or 0) if row else 0
        if delay > 0:
            spawn(delayed_delete(m, delay))
        else:
            await delete_counted(m)

    # Guruh nomi o'zgarsa va yoqilgan bo'lsa — yangi avatar
    if m.new_chat_title and avatar_on and row and row["rename"]:
        await ensure_chat(m.chat.id, m.new_chat_title)
        await set_group_avatar(bot, m.chat.id, m.new_chat_title)


async def delete_counted(m: Message):
    try:
        await m.delete()
    except TelegramAPIError as e:
        if "not found" not in str(e).lower():
            await log_error(f"delete {m.chat.id}: {e}")
        return
    await db.execute("UPDATE chats SET deleted=deleted+1 WHERE chat_id=?", (m.chat.id,))
    await db.commit()
    await incr_setting("deleted_total")


async def delayed_delete(m: Message, delay: int):
    await asyncio.sleep(delay)
    await delete_counted(m)


# --------------------------------------------------------------------------
# Adminlar uchun buyruqlar: /start va /photo (guruh va kanal)
# --------------------------------------------------------------------------
async def is_chat_admin(m: Message, bot: Bot) -> bool:
    if m.chat.type == "channel":
        return True  # kanalga faqat adminlar yozadi
    if m.sender_chat and m.sender_chat.id == m.chat.id:
        return True  # anonim admin
    if not m.from_user:
        return False
    try:
        mem = await bot.get_chat_member(m.chat.id, m.from_user.id)
    except TelegramAPIError:
        return False
    return mem.status in ("creator", "administrator")


async def flash(bot: Bot, chat_id: int, text: str, secs: int = 6):
    """Qisqa xabar, bir necha soniyadan keyin o'zi o'chadi."""
    try:
        msg = await bot.send_message(chat_id, text)
    except TelegramAPIError:
        return

    async def _rm():
        await asyncio.sleep(secs)
        try:
            await msg.delete()
        except TelegramAPIError:
            pass
    spawn(_rm())


@router.message(F.chat.type.in_(GROUPS), Command("start", "photo"))
@router.channel_post(F.chat.type == "channel", Command("start", "photo"))
async def admin_cmd(m: Message, bot: Bot, command: CommandObject):
    if not await is_chat_admin(m, bot):
        return  # oddiy a'zolarga hech narsa qilinmaydi
    try:
        await m.delete()
    except TelegramAPIError:
        pass
    chat = m.chat
    await ensure_chat(chat.id, chat.title)

    if command.command == "start":
        await db.execute("UPDATE chats SET cleaner=1 WHERE chat_id=?", (chat.id,))
        await db.commit()
        ok = False
        try:
            me = await bot.get_chat_member(chat.id, bot.id)
            ok = me.status == "administrator" and bool(getattr(me, "can_delete_messages", False))
        except TelegramAPIError:
            pass
        await flash(bot, chat.id,
                    "✅ Tozalash yoqildi. Doim tayyor." if ok else
                    "⚠️ Meni admin qiling va «Xabarlarni o'chirish» huquqini bering.")
    else:
        ok, _ = await set_group_avatar(bot, chat.id, chat.title or "")
        if not ok:
            await flash(bot, chat.id,
                        "⚠️ Rasm qo'yilmadi. Menga «Guruh ma'lumotlarini o'zgartirish» huquqini bering.", 8)


@router.message(F.chat.type == "private", CommandStart())
async def private_start(m: Message):
    await m.answer("👋 Meni guruh yoki kanalga qo'shib, <b>admin</b> qiling.\n\n"
                   "Guruh/kanal adminlari uchun:\n"
                   "/start — kirdi-chiqdi xabarlarini tozalash\n"
                   "/photo — yangi rasm qo'yish")


# --------------------------------------------------------------------------
# Owner paneli (faqat private + OWNER_ID)
# --------------------------------------------------------------------------
owner = Router()
owner.message.filter(F.chat.type == "private", F.from_user.id == OWNER_ID)
owner.callback_query.filter(F.from_user.id == OWNER_ID)

ICON = {"1": "🟢", "0": "🔴"}


def onoff(v) -> str:
    return "🟢 Yoqilgan" if str(v) == "1" else "🔴 O'chirilgan"


def main_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Statistika", callback_data="st")
    b.button(text="👥 Guruhlar", callback_data="gs:0")
    b.button(text="⚙️ Global sozlamalar", callback_data="gl")
    b.button(text="🩺 Bot holati", callback_data="hl")
    b.button(text="🧾 Xatolar", callback_data="er")
    b.adjust(2, 1, 2)
    return b.as_markup()


def back_kb(to="m"):
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Orqaga", callback_data=to)
    return b.as_markup()


MAIN_TEXT = ("🤖 <b>Boshqaruv paneli</b>\n\n"
             "Botni guruhga qo'shib, <b>admin</b> qiling — qolganini o'zi qiladi.")


async def safe_edit(cb: CallbackQuery, text: str, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


@owner.message(CommandStart())
@owner.message(Command("panel"))
async def cmd_start(m: Message):
    await m.answer(MAIN_TEXT, reply_markup=main_kb())


@owner.callback_query(F.data == "m")
async def cb_main(cb: CallbackQuery):
    await safe_edit(cb, MAIN_TEXT, main_kb())


@owner.callback_query(F.data == "st")
async def cb_stats(cb: CallbackQuery):
    async with db.execute("SELECT COUNT(*) n, SUM(is_admin) a FROM chats WHERE active=1") as c:
        r = await c.fetchone()
    async with db.execute("SELECT COUNT(*) n FROM errors") as c:
        e = await c.fetchone()
    text = ("📊 <b>Statistika</b>\n\n"
            f"👥 Faol guruhlar: <b>{r['n'] or 0}</b>\n"
            f"🛡 Admin bo'lgan guruhlar: <b>{r['a'] or 0}</b>\n"
            f"🧹 O'chirilgan xabarlar: <b>{await get_setting('deleted_total')}</b>\n"
            f"🖼 Yaratilgan avatarlar: <b>{await get_setting('avatars_total')}</b>\n"
            f"⚠️ Xatolar soni: <b>{e['n']}</b>")
    await safe_edit(cb, text, back_kb())


@owner.callback_query(F.data == "hl")
async def cb_health(cb: CallbackQuery, bot: Bot):
    t = time.perf_counter()
    me = await bot.get_me()
    ping = (time.perf_counter() - t) * 1000
    up = int(time.time() - START_TS)
    text = ("🩺 <b>Bot holati</b>\n\n"
            f"🤖 @{me.username}\n"
            f"✅ Ishlayapti\n"
            f"⏱ Ping: <b>{ping:.0f} ms</b>\n"
            f"🕒 Uptime: <b>{up // 3600}s {up % 3600 // 60}d</b>")
    await safe_edit(cb, text, back_kb())


@owner.callback_query(F.data == "er")
async def cb_errors(cb: CallbackQuery):
    async with db.execute("SELECT ts,text FROM errors ORDER BY id DESC LIMIT 10") as c:
        rows = await c.fetchall()
    if rows:
        body = "\n\n".join(
            f"<code>{time.strftime('%d.%m %H:%M', time.localtime(r['ts']))}</code> {escape(r['text'][:150])}"
            for r in rows)
    else:
        body = "Xatolar yo'q ✅"
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Tozalash", callback_data="erc")
    b.button(text="⬅️ Orqaga", callback_data="m")
    b.adjust(2)
    await safe_edit(cb, f"🧾 <b>So'nggi xatolar</b>\n\n{body}", b.as_markup())


@owner.callback_query(F.data == "erc")
async def cb_errors_clear(cb: CallbackQuery):
    await db.execute("DELETE FROM errors")
    await db.commit()
    await cb_errors(cb)


# ---- Global ----
async def global_view():
    c, a = await get_setting("cleaner"), await get_setting("avatar")
    b = InlineKeyboardBuilder()
    b.button(text=f"{ICON[c]} Automatic Cleaner", callback_data="gt:cleaner")
    b.button(text=f"{ICON[a]} Automatic Avatar", callback_data="gt:avatar")
    b.button(text="⬅️ Orqaga", callback_data="m")
    b.adjust(1)
    text = ("⚙️ <b>Global sozlamalar</b>\n\n"
            f"🧹 Cleaner: {onoff(c)}\n🖼 Avatar: {onoff(a)}\n\n"
            "<i>Global o'chirilsa, hamma guruhda funksiya to'xtaydi.</i>")
    return text, b.as_markup()


@owner.callback_query(F.data == "gl")
async def cb_global(cb: CallbackQuery):
    await safe_edit(cb, *await global_view())


@owner.callback_query(F.data.startswith("gt:"))
async def cb_global_toggle(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key in ("cleaner", "avatar"):
        cur = await get_setting(key)
        await set_setting(key, "0" if cur == "1" else "1")
    await safe_edit(cb, *await global_view())


# ---- Guruhlar ----
@owner.callback_query(F.data.startswith("gs:"))
async def cb_groups(cb: CallbackQuery):
    page = int(cb.data.split(":")[1])
    async with db.execute("SELECT COUNT(*) n FROM chats WHERE active=1") as c:
        total = (await c.fetchone())["n"]
    async with db.execute(
            "SELECT chat_id,title,is_admin FROM chats WHERE active=1 "
            "ORDER BY added_at DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE)) as c:
        rows = await c.fetchall()
    b = InlineKeyboardBuilder()
    for r in rows:
        mark = "🛡" if r["is_admin"] else "⚠️"
        b.button(text=f"{mark} {(r['title'] or str(r['chat_id']))[:35]}",
                 callback_data=f"g:{r['chat_id']}:{page}")
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=f"gs:{page - 1}")
    if (page + 1) * PAGE_SIZE < total:
        nav.button(text="▶️", callback_data=f"gs:{page + 1}")
    nav.button(text="⬅️ Orqaga", callback_data="m")
    b.attach(nav)
    text = f"👥 <b>Guruhlar</b> ({total})\n🛡 admin  ⚠️ admin emas" if total else \
        "👥 Hali guruh yo'q.\nBotni guruhga qo'shib, admin qiling."
    await safe_edit(cb, text, b.as_markup())


async def group_view(bot: Bot, chat_id: int, page: int):
    row = await get_chat(chat_id)
    if not row:
        return "Guruh topilmadi.", back_kb(f"gs:{page}")
    perm = "❓"
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
        if me.status == "administrator":
            perm = (f"🛡 Admin | o'chirish {'✅' if me.can_delete_messages else '❌'} | "
                    f"ma'lumot {'✅' if me.can_change_info else '❌'}")
        else:
            perm = "⚠️ Bot admin emas"
    except TelegramAPIError:
        perm = "❓ Tekshirib bo'lmadi"
    b = InlineKeyboardBuilder()
    b.button(text=f"{ICON[str(row['cleaner'])]} Cleaner", callback_data=f"t:{chat_id}:cleaner:{page}")
    b.button(text=f"{ICON[str(row['avatar'])]} Avatar", callback_data=f"t:{chat_id}:avatar:{page}")
    b.button(text=f"{ICON[str(row['rename'])]} Nom o'zgarsa yangi avatar",
             callback_data=f"t:{chat_id}:rename:{page}")
    b.button(text=f"⏱ O'chirish kechikishi: {row['delay'] or 0}s",
             callback_data=f"t:{chat_id}:delay:{page}")
    b.button(text="🖼 Avatarni hozir yaratish", callback_data=f"ra:{chat_id}:{page}")
    b.button(text="⬅️ Orqaga", callback_data=f"gs:{page}")
    b.adjust(2, 1, 1, 1, 1)
    text = (f"👥 <b>{escape(row['title'] or str(chat_id))}</b>\n"
            f"<code>{chat_id}</code>\n\n{perm}\n"
            f"🧹 O'chirilgan: <b>{row['deleted']}</b>\n"
            f"🖼 Avatar qo'yilgan: {'ha' if row['avatar_done'] else 'yo`q'}")
    return text, b.as_markup()


@owner.callback_query(F.data.startswith("g:"))
async def cb_group(cb: CallbackQuery, bot: Bot):
    _, cid, page = cb.data.split(":")
    await safe_edit(cb, *await group_view(bot, int(cid), int(page)))


@owner.callback_query(F.data.startswith("t:"))
async def cb_group_toggle(cb: CallbackQuery, bot: Bot):
    _, cid, field, page = cb.data.split(":")
    if field == "delay":
        row = await get_chat(int(cid))
        nxt = {0: 5, 5: 10, 10: 20}.get(int(row["delay"] or 0), 0)
        await db.execute("UPDATE chats SET delay=? WHERE chat_id=?", (nxt, int(cid)))
        await db.commit()
    elif field in ("cleaner", "avatar", "rename"):
        await db.execute(f"UPDATE chats SET {field}=1-{field} WHERE chat_id=?", (int(cid),))
        await db.commit()
    await safe_edit(cb, *await group_view(bot, int(cid), int(page)))


@owner.callback_query(F.data.startswith("ra:"))
async def cb_regen(cb: CallbackQuery, bot: Bot):
    _, cid, page = cb.data.split(":")
    row = await get_chat(int(cid))
    ok, err = await set_group_avatar(bot, int(cid), row["title"] if row else "")
    await cb.answer("✅ Avatar qo'yildi" if ok else f"❌ {err[:150]}", show_alert=not ok)
    await safe_edit(cb, *await group_view(bot, int(cid), int(page)))


# --------------------------------------------------------------------------
async def setup_commands(bot: Bot):
    """'/' menyusi: guruhda faqat adminlarga ko'rinadi."""
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            [BotCommand(command="start", description="Tozalashni yoqish"),
             BotCommand(command="photo", description="Yangi rasm qo'yish")],
            scope=BotCommandScopeAllChatAdministrators())
        await bot.set_my_commands(
            [BotCommand(command="start", description="Boshlash")],
            scope=BotCommandScopeAllPrivateChats())
    except TelegramAPIError as e:
        await log_error(f"commands: {e}")


async def health_server():
    """Render (Web Service) port kutadi; tashqi ping ham shu yerga uradi."""
    port = os.getenv("PORT")
    if not port:
        return None

    async def ok(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Health server: port %s", port)
    return runner


async def keep_alive():
    """Render bepul rejasi 15 daqiqada uxlamasligi uchun o'ziga ping."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(540)
        try:
            async with ClientSession(timeout=ClientTimeout(total=20)) as sess:
                async with sess.get(url) as r:
                    await r.read()
        except Exception as e:  # noqa
            log.warning("keep_alive: %r", e)


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi (.env faylni tekshiring)")
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(owner, router)
    log.info("Bot ishga tushdi. Owner: %s", OWNER_ID)
    runner = await health_server()
    spawn(keep_alive())
    await setup_commands(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if runner:
            await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
