"""
«Полночный Аркан» — таро в Telegram: сервер Mini App + бот в одном процессе.

Что делает:
  • раздаёт Mini App (папка webapp/) и картинки карт (кэширует с Wikimedia Commons)
  • выдаёт расклады с учётом лимитов: бесплатные в день, купленные, подписка «Безлимит»
  • оплата Telegram Stars: пакеты раскладов и ежемесячная подписка
  • рефералка, ежедневная «Карта дня», картинка расклада для сторис

Запуск: см. README.md. Настройки — переменные окружения (или файл .env рядом).
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import random
import secrets
import sqlite3
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent


# ───────────────────────── настройки ─────────────────────────
def load_dotenv() -> None:
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_dotenv()
DEV = os.getenv("DEV") == "1" or "--dev" in sys.argv  # локальный тест без Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# адрес сервера; на Render подставляется сам (RENDER_EXTERNAL_URL)
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080").rstrip("/") + "/"
PORT = int(os.getenv("PORT", "8080"))
BOT_MODE = os.getenv("BOT_MODE", "polling")              # webhook — на сервере, polling — локально
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or secrets.token_urlsafe(24)
DAILY_HOUR = int(os.getenv("DAILY_HOUR", "9"))        # во сколько рассылать «Карту дня»
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
FREE_PER_DAY = int(os.getenv("FREE_PER_DAY", "3"))    # бесплатных простых раскладов в день
START_BONUS = int(os.getenv("START_BONUS", "2"))      # раскладов в подарок новичку
REF_BONUS = int(os.getenv("REF_BONUS", "3"))          # раскладов за приглашённого друга (и другу тоже)
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "@your_support")
# Telegram id админов через запятую (узнать свой id: @userinfobot). Им доступна команда /admin.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

# пакеты раскладов (разовая покупка) и подписка — цены в звёздах Telegram
PACKS = {
    "s5": {"spreads": 5, "stars": 49, "title": "5 раскладов"},
    "s15": {"spreads": 15, "stars": 119, "title": "15 раскладов"},
    "s50": {"spreads": 50, "stars": 299, "title": "50 раскладов"},
}
SUB = {"stars": int(os.getenv("SUB_STARS", "199")), "days": 30, "title": "Безлимит на 30 дней"}

if not DEV and not BOT_TOKEN:
    raise SystemExit("Нужен BOT_TOKEN (или --dev для локального теста)")

DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))
CARD_CACHE = ROOT / "webapp" / "cards"  # все 78 карт лежат в репозитории
SHARES = DATA / "shares"
ADS_DIR = DATA / "ads"
for d in (DATA, CARD_CACHE, SHARES, ADS_DIR):
    d.mkdir(exist_ok=True)

log = logging.getLogger("tarot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ───────────────────────── колода и расклады ─────────────────────────
MAJOR = [
    ("Шут", "Fool"), ("Маг", "Magician"), ("Верховная Жрица", "High Priestess"), ("Императрица", "Empress"),
    ("Император", "Emperor"), ("Иерофант", "Hierophant"), ("Влюблённые", "Lovers"), ("Колесница", "Chariot"),
    ("Сила", "Strength"), ("Отшельник", "Hermit"), ("Колесо Фортуны", "Wheel of Fortune"), ("Справедливость", "Justice"),
    ("Повешенный", "Hanged Man"), ("Смерть", "Death"), ("Умеренность", "Temperance"), ("Дьявол", "Devil"),
    ("Башня", "Tower"), ("Звезда", "Star"), ("Луна", "Moon"), ("Солнце", "Sun"), ("Суд", "Judgement"), ("Мир", "World"),
]
SUITS = [("wands", "Жезлов", "Wands"), ("cups", "Кубков", "Cups"), ("swords", "Мечей", "Swords"), ("pentacles", "Пентаклей", "Pents")]
RANKS = ["Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка", "Шестёрка", "Семёрка", "Восьмёрка", "Девятка", "Десятка",
         "Паж", "Рыцарь", "Королева", "Король"]

CARDS: dict[str, dict] = {}
for i, (ru, en) in enumerate(MAJOR):
    CARDS[f"m{i}"] = {"name": ru, "file": f"RWS Tarot {i:02d} {en}.jpg"}
for key, ru_suit, file_suit in SUITS:
    for i, rank in enumerate(RANKS):
        CARDS[f"{key}{i}"] = {"name": f"{rank} {ru_suit}", "file": f"{file_suit}{i + 1:02d}.jpg"}

# id расклада → (число карт, уровень). basic — входит в бесплатный дневной лимит; premium — только за расклад/подписку.
# Названия и позиции — в webapp/index.html (число карт должно совпадать).
SPREADS = {
    "day": (1, "basic"), "yesno": (1, "basic"), "three": (3, "basic"),
    "work": (4, "premium"), "week": (7, "premium"), "love": (3, "premium"),
    "thinks": (3, "premium"), "sit": (4, "premium"), "celtic": (10, "premium"),
}


# ───────────────────────── база данных ─────────────────────────
# Локально — SQLite (data/tarot.db). На сервере — PostgreSQL (Neon), если задан DATABASE_URL:
# диск бесплатного Render стирается при перезапуске, а база должна жить.
DATABASE_URL = os.getenv("DATABASE_URL", "")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, name TEXT, credits INTEGER DEFAULT 0, daily INTEGER DEFAULT 1,
  ref_by BIGINT, refs INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0, created TEXT,
  sub_until TEXT, free_date TEXT, free_used INTEGER DEFAULT 0, username TEXT);
CREATE TABLE IF NOT EXISTS payments(
  rid {SERIAL}, charge_id TEXT UNIQUE, user_id BIGINT, item TEXT, stars INTEGER, created TEXT, refunded TEXT);
CREATE TABLE IF NOT EXISTS draws(
  id {SERIAL}, user_id BIGINT, spread TEXT, cards TEXT, paid_by TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ads(
  id {SERIAL}, text TEXT, image TEXT, url TEXT, button TEXT,
  active INTEGER DEFAULT 0, views INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0, created TEXT);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS users_username ON users(username);
CREATE INDEX IF NOT EXISTS draws_created ON draws(created);
"""


class Row(dict):
    """Строка результата: доступ и по имени колонки, и по номеру."""
    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else super().__getitem__(k)


class Database:
    """Тонкая обёртка: одинаковые запросы (с ?) работают и в SQLite, и в PostgreSQL."""

    def __init__(self) -> None:
        self.pg = DATABASE_URL.startswith(("postgres://", "postgresql://"))
        self._connect()
        if self.pg:
            for stmt in (SCHEMA.replace("{SERIAL}", "BIGSERIAL PRIMARY KEY") + INDEXES).split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
        else:
            self.conn.executescript(SCHEMA.replace("{SERIAL}", "INTEGER PRIMARY KEY AUTOINCREMENT"))
            self._sqlite_upgrade()
            self.conn.executescript(INDEXES)
            self.conn.commit()

    def _connect(self) -> None:
        if self.pg:
            import psycopg
            def factory(cur):
                names = [c.name for c in cur.description] if cur.description else []
                return lambda values: Row(zip(names, values))
            self.conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=factory)
        else:
            self.conn = sqlite3.connect(DATA / "tarot.db", check_same_thread=False)
            self.conn.row_factory = lambda cur, values: Row(zip([c[0] for c in cur.description], values))

    def _sqlite_upgrade(self) -> None:
        """Старые локальные базы: добавить новые колонки, перенести платежи на колонку rid."""
        for table, col in (("users", "sub_until TEXT"), ("users", "free_date TEXT"),
                           ("users", "free_used INTEGER DEFAULT 0"), ("users", "username TEXT"), ("payments", "refunded TEXT")):
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(payments)").fetchall()]
        if "rid" not in cols:
            self.conn.executescript("""
              ALTER TABLE payments RENAME TO payments_old;
              CREATE TABLE payments(rid INTEGER PRIMARY KEY AUTOINCREMENT, charge_id TEXT UNIQUE, user_id BIGINT,
                                    item TEXT, stars INTEGER, created TEXT, refunded TEXT);
              INSERT INTO payments(charge_id,user_id,item,stars,created,refunded)
                SELECT charge_id,user_id,item,stars,created,refunded FROM payments_old ORDER BY rowid;
              DROP TABLE payments_old;""")

    def execute(self, sql: str, params: tuple | list = ()):
        if not self.pg:
            return self.conn.execute(sql, params)
        sql = sql.replace("?", "%s")
        try:
            return self.conn.execute(sql, params)
        except Exception as e:  # Neon закрывает простаивающие соединения — переподключаемся один раз
            import psycopg
            if not isinstance(e, psycopg.OperationalError):
                raise
            log.warning("БД: переподключение (%s)", e)
            self._connect()
            return self.conn.execute(sql, params)

    def commit(self) -> None:
        if not self.pg:
            self.conn.commit()


db = Database()
log.info("База: %s", "PostgreSQL" if db.pg else "SQLite")


def remember_username(uid: int, username: str | None) -> None:
    """Запоминаем @username, чтобы админ мог найти человека по нему."""
    db.execute("UPDATE users SET username=? WHERE id=?", ((username or "").lower() or None, uid))
    db.commit()


def active_ad() -> sqlite3.Row | None:
    return db.execute("SELECT * FROM ads WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()


def ad_public(ad: sqlite3.Row) -> dict:
    return {"id": ad["id"], "text": ad["text"] or "", "url": ad["url"] or "", "button": ad["button"] or "",
            "image": f"ads/{ad['id']}.jpg" if ad["image"] else ""}


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def user_row(uid: int) -> sqlite3.Row:
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_user(uid: int, name: str = "", ref_by: int | None = None) -> tuple[sqlite3.Row, bool]:
    """Возвращает пользователя; создаёт нового (с бонусами) при первом визите."""
    row = user_row(uid)
    if row:
        return row, False
    if ref_by == uid or (ref_by and not user_row(ref_by)):
        ref_by = None
    bonus = START_BONUS + (REF_BONUS if ref_by else 0)
    db.execute("INSERT INTO users(id,name,credits,ref_by,created) VALUES(?,?,?,?,?)",
               (uid, name, bonus, ref_by, utcnow().isoformat()))
    if ref_by:
        db.execute("UPDATE users SET credits=credits+?, refs=refs+1 WHERE id=?", (REF_BONUS, ref_by))
    db.commit()
    return user_row(uid), True


def sub_active(u: sqlite3.Row) -> bool:
    return bool(u["sub_until"]) and dt.datetime.fromisoformat(u["sub_until"]) > utcnow()


def free_left(u: sqlite3.Row) -> int:
    used = u["free_used"] if u["free_date"] == today() else 0
    return max(0, FREE_PER_DAY - used)


def extend_sub(uid: int, until: dt.datetime | None = None) -> None:
    u = user_row(uid)
    if until is None:
        base = max(utcnow(), dt.datetime.fromisoformat(u["sub_until"])) if u["sub_until"] else utcnow()
        until = base + dt.timedelta(days=SUB["days"])
    db.execute("UPDATE users SET sub_until=? WHERE id=?", (until.isoformat(), uid))
    db.commit()


def profile(u: sqlite3.Row) -> dict:
    return {
        "id": u["id"], "credits": u["credits"], "freeLeft": free_left(u), "freePerDay": FREE_PER_DAY,
        "sub": sub_active(u), "subUntil": u["sub_until"] if sub_active(u) else None,
        "daily": bool(u["daily"]), "refs": u["refs"], "refBonus": REF_BONUS,
        "bot": BOT_USERNAME, "refLink": f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}",
        "packs": PACKS, "subOffer": SUB, "dailyHour": DAILY_HOUR, "dev": DEV,
    }


# ───────────────────────── бот ─────────────────────────
bot = None
dp = None
BOT_USERNAME = os.getenv("BOT_USERNAME", "PolnochArkanBot")

if BOT_TOKEN:
    from aiogram import Bot, Dispatcher, F
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from aiogram.filters import Command, CommandObject, CommandStart
    from aiogram.filters import StateFilter
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                               LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo)
    from aiogram.utils.web_app import safe_parse_webapp_init_data

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    def app_kb(text: str = "🔮 Сделать расклад") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=WEBAPP_URL))]])

    @dp.message(CommandStart())
    async def cmd_start(message: Message, command: CommandObject) -> None:
        ref = None
        if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
            ref = int(command.args[4:])
        user, is_new = get_user(message.from_user.id, message.from_user.full_name, ref)
        db.execute("UPDATE users SET blocked=0 WHERE id=?", (user["id"],))
        remember_username(user["id"], message.from_user.username)
        text = ("🌙 Добро пожаловать в «Полночный Аркан» ✦\n\n"
                f"Задай вопрос, перемешай колоду Таро и открой карты. Каждый день — {FREE_PER_DAY} бесплатных расклада.\n\n"
                f"Каждое утро в {DAILY_HOUR}:00 пришлю тебе «Карту дня» (выключить: /daily off).")
        if is_new and user["credits"]:
            text += f"\n\n🎁 Тебе в подарок раскладов: {user['credits']}"
        await message.answer(text, reply_markup=app_kb())
        if is_new and user["ref_by"]:
            try:
                await bot.send_message(user["ref_by"], f"🎉 По твоей ссылке пришёл друг! +{REF_BONUS} расклада.")
            except Exception:
                pass

    @dp.message(Command("daily"))
    async def cmd_daily(message: Message, command: CommandObject) -> None:
        get_user(message.from_user.id, message.from_user.full_name)
        on = 0 if (command.args or "").strip().lower() in ("off", "выкл", "0") else 1
        db.execute("UPDATE users SET daily=? WHERE id=?", (on, message.from_user.id))
        db.commit()
        await message.answer("🔔 «Карта дня» включена" if on else "🔕 «Карта дня» выключена. Включить: /daily on")

    @dp.message(Command("paysupport"))
    async def cmd_paysupport(message: Message) -> None:
        await message.answer(f"По вопросам оплаты и возвратов напиши {SUPPORT_CONTACT}. Укажи дату покупки — разберёмся.\n"
                             "Подписку можно отменить в любой момент: Настройки Telegram → Мои звёзды.")

    @dp.message(Command("terms"))
    async def cmd_terms(message: Message) -> None:
        await message.answer("Расклады Таро носят развлекательный характер и не являются советом в медицинских, "
                             "юридических или финансовых вопросах. Купленные расклады начисляются сразу после оплаты. "
                             "Подписка «Безлимит» продлевается каждые 30 дней, пока её не отменить.")

    @dp.pre_checkout_query()
    async def pre_checkout(q: PreCheckoutQuery) -> None:
        item = q.invoice_payload.split(":")[0]
        ok = item in PACKS or item == "sub"
        await q.answer(ok=ok, error_message=None if ok else "Товар не найден")

    @dp.message(F.successful_payment)
    async def paid(message: Message) -> None:
        sp = message.successful_payment
        item = sp.invoice_payload.split(":")[0]
        cur = db.execute("INSERT INTO payments(charge_id,user_id,item,stars,created) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(charge_id) DO NOTHING",
                         (sp.telegram_payment_charge_id, message.from_user.id, item, sp.total_amount, utcnow().isoformat()))
        db.commit()
        if cur.rowcount == 0:
            return  # повтор того же платежа
        uid = message.from_user.id
        get_user(uid, message.from_user.full_name)
        if item == "sub":
            exp = sp.subscription_expiration_date
            extend_sub(uid, dt.datetime.fromtimestamp(exp, dt.timezone.utc) if exp else None)
            text = ("✨ Подписка «Безлимит» продлена!" if sp.is_recurring and not sp.is_first_recurring
                    else "✨ Подписка «Безлимит» активна — все расклады без ограничений 30 дней!")
        else:
            db.execute("UPDATE users SET credits=credits+? WHERE id=?", (PACKS[item]["spreads"], uid))
            db.commit()
            text = f"✨ Спасибо! Начислено раскладов: {PACKS[item]['spreads']}"
        await message.answer(text, reply_markup=app_kb("🔮 К раскладам"))

    # ═════════════════════════ АДМИНКА (/admin) ═════════════════════════
    is_admin = F.from_user.id.in_(ADMIN_IDS)

    class AdForm(StatesGroup):
        content = State()
        url = State()
        button = State()

    def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in row] for row in rows])

    class GiveForm(StatesGroup):
        who = State()

    ADMIN_MENU = kb([("📊 Статистика", "adm:stats")], [("👑 Выдать безлимит", "adm:give")],
                    [("💸 Платежи и возвраты", "adm:pays:0")], [("📢 Реклама на странице расклада", "adm:ads")])
    BACK = [("⬅️ В меню", "adm:menu")]
    ITEM_NAMES = {**{k: v["title"] for k, v in PACKS.items()}, "sub": "Безлимит"}

    async def edit(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
        try:
            await cb.message.edit_text(text, reply_markup=markup)
        except Exception:  # сообщение с фото или без изменений — шлём новое
            await cb.message.answer(text, reply_markup=markup)
        try:
            await cb.answer()
        except Exception:  # уже ответили (например, всплывающим уведомлением)
            pass

    @dp.message(Command("admin"), is_admin)
    async def adm_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("🛠 Админка", reply_markup=ADMIN_MENU)

    @dp.message(Command("cancel"), is_admin)
    async def adm_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=ADMIN_MENU)

    @dp.callback_query(F.data == "adm:menu", is_admin)
    async def adm_menu(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await edit(cb, "🛠 Админка", ADMIN_MENU)

    # ── статистика ──
    @dp.callback_query(F.data == "adm:stats", is_admin)
    async def adm_stats(cb: CallbackQuery) -> None:
        q = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0  # noqa: E731
        day_ago = (utcnow() - dt.timedelta(days=1)).isoformat()
        month_ago = (utcnow() - dt.timedelta(days=30)).isoformat()
        now_iso = utcnow().isoformat()
        text = (
            "📊 Статистика\n\n"
            f"👥 Пользователей: {q('SELECT COUNT(*) FROM users')}\n"
            f"🆕 Новых за сутки: {q('SELECT COUNT(*) FROM users WHERE created>?', day_ago)}\n"
            f"🔮 Раскладов за сутки: {q('SELECT COUNT(*) FROM draws WHERE created>?', day_ago)}\n"
            f"👑 Активных подписок: {q('SELECT COUNT(*) FROM users WHERE sub_until>?', now_iso)}\n"
            f"🔔 Получают «Карту дня»: {q('SELECT COUNT(*) FROM users WHERE daily=1 AND blocked=0')}\n"
            f"🚫 Заблокировали бота: {q('SELECT COUNT(*) FROM users WHERE blocked=1')}\n\n"
            f"⭐ Выручка за 30 дней: {q('SELECT SUM(stars) FROM payments WHERE refunded IS NULL AND created>?', month_ago)}\n"
            f"⭐ Выручка всего: {q('SELECT SUM(stars) FROM payments WHERE refunded IS NULL')} "
            f"(покупок: {q('SELECT COUNT(*) FROM payments WHERE refunded IS NULL')})\n"
            f"↩️ Возвратов: {q('SELECT COUNT(*) FROM payments WHERE refunded IS NOT NULL')} "
            f"на {q('SELECT SUM(stars) FROM payments WHERE refunded IS NOT NULL')} ⭐"
        )
        await edit(cb, text, kb([("🔄 Обновить", "adm:stats")], BACK))

    # ── выдача безлимита ──
    FOREVER = dt.datetime(2099, 12, 31, tzinfo=dt.timezone.utc)

    def find_user(text: str) -> sqlite3.Row | None:
        t = (text or "").strip()
        if t.lstrip("-").isdigit():
            return user_row(int(t))
        t = t.removeprefix("https://t.me/").removeprefix("t.me/").lstrip("@").lower()
        return db.execute("SELECT * FROM users WHERE username=?", (t,)).fetchone() if t else None

    def sub_status(u: sqlite3.Row) -> str:
        if not sub_active(u):
            return "нет безлимита"
        until = dt.datetime.fromisoformat(u["sub_until"])
        return "♾ безлимит навсегда" if until.year >= 2099 else f"👑 безлимит до {until.astimezone(TZ):%d.%m.%Y %H:%M}"

    async def show_give_card(target, uid: int) -> None:
        u = user_row(uid)
        uname = f" (@{u['username']})" if u["username"] else ""
        text = (f"👤 {u['name'] or '—'}{uname}\nid: {u['id']}\nСтатус: {sub_status(u)}\n"
                f"Раскладов на балансе: {u['credits']}\n\nСколько безлимита выдать?")
        rows = [[("+7 дней", f"adm:g:{uid}:7"), ("+30 дней", f"adm:g:{uid}:30"), ("+90 дней", f"adm:g:{uid}:90")],
                [("♾ Навсегда", f"adm:g:{uid}:0")]]
        if sub_active(u):
            rows.append([("❌ Забрать безлимит", f"adm:gx:{uid}")])
        rows.append([("👤 Другой пользователь", "adm:give")])
        rows.append(BACK)
        if isinstance(target, CallbackQuery):
            await edit(target, text, kb(*rows))
        else:
            await target.answer(text, reply_markup=kb(*rows))

    @dp.callback_query(F.data == "adm:give", is_admin)
    async def adm_give(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(GiveForm.who)
        await edit(cb, "👑 Кому выдать безлимит?\n\nПришли id, @username или перешли любое сообщение этого человека.\n"
                       "Человек должен хотя бы раз запустить бота.\n\nОтмена: /cancel", kb(BACK))

    @dp.message(GiveForm.who, is_admin)
    async def adm_give_who(message: Message, state: FSMContext) -> None:
        u = None
        origin = message.forward_origin
        if origin is not None:
            sender = getattr(origin, "sender_user", None)
            if sender is None:
                await message.answer("У этого человека скрыт аккаунт при пересылке. Пришли его id или @username.")
                return
            u = user_row(sender.id)
        else:
            u = find_user(message.text or "")
        if not u:
            await message.answer("Не нашёл такого пользователя в боте. Проверь id/@username — человек должен хотя бы раз "
                                 "нажать /start. Или /cancel")
            return
        await state.clear()
        await show_give_card(message, u["id"])

    @dp.message(Command("give"), is_admin)
    async def adm_give_cmd(message: Message, command: CommandObject) -> None:
        """/give <id или @username> [дней, 0 = навсегда]"""
        parts = (command.args or "").split()
        u = find_user(parts[0]) if parts else None
        if not u:
            await message.answer("Использование: /give <id или @username> [дней]\nНапример: /give @anna 30  ·  /give 12345 0 (навсегда)")
            return
        if len(parts) > 1 and parts[1].isdigit():
            await message.answer(await grant_sub(u["id"], int(parts[1]), message.from_user.id))
        else:
            await show_give_card(message, u["id"])

    async def grant_sub(uid: int, days: int, by: int) -> str:
        u = user_row(uid)
        if days == 0:
            until = FOREVER
        else:
            base = dt.datetime.fromisoformat(u["sub_until"]) if sub_active(u) else utcnow()
            until = min(base + dt.timedelta(days=days), FOREVER)
        db.execute("UPDATE users SET sub_until=? WHERE id=?", (until.isoformat(), uid))
        db.commit()
        log.info("admin %s выдал безлимит %s на %s дн.", by, uid, days or "∞")
        gift = "навсегда" if days == 0 else f"до {until.astimezone(TZ):%d.%m.%Y}"
        try:
            await bot.send_message(uid, f"🎁 Тебе подарили «Безлимит» {gift}! Все расклады без ограничений и без рекламы ✨",
                                   reply_markup=app_kb("🔮 К раскладам"))
        except Exception:
            pass
        return f"✅ Выдано: {u['name'] or uid} — {sub_status(user_row(uid))}"

    @dp.callback_query(F.data.startswith("adm:g:"), is_admin)
    async def adm_give_do(cb: CallbackQuery) -> None:
        _, _, uid, days = cb.data.split(":")
        msg = await grant_sub(int(uid), int(days), cb.from_user.id)
        await cb.answer(msg, show_alert=True)
        await show_give_card(cb, int(uid))

    @dp.callback_query(F.data.startswith("adm:gx:"), is_admin)
    async def adm_give_revoke(cb: CallbackQuery) -> None:
        uid = int(cb.data.split(":")[2])
        db.execute("UPDATE users SET sub_until=? WHERE id=?", (utcnow().isoformat(), uid))
        db.commit()
        log.info("admin %s забрал безлимит у %s", cb.from_user.id, uid)
        await cb.answer("Безлимит забран", show_alert=True)
        await show_give_card(cb, uid)

    # ── платежи и возвраты ──
    def pay_label(p: sqlite3.Row) -> str:
        name = (p["name"] or str(p["user_id"]))[:14]
        mark = "↩️ " if p["refunded"] else ""
        return f"{mark}{p['stars']}⭐ {ITEM_NAMES.get(p['item'], p['item'])} · {name} · {p['created'][5:10]}"

    async def show_payments(target, page: int, user_id: int | None = None) -> None:
        per = 8
        where, args = ("WHERE p.user_id=?", (user_id,)) if user_id else ("", ())
        rows = db.execute(f"SELECT p.*, u.name FROM payments p LEFT JOIN users u ON u.id=p.user_id {where} "
                          f"ORDER BY p.created DESC LIMIT ? OFFSET ?", (*args, per + 1, page * per)).fetchall()
        buttons = [[(pay_label(p), f"adm:pay:{p['rid']}")] for p in rows[:per]]
        nav = []
        if page > 0 and not user_id:
            nav.append(("◀️", f"adm:pays:{page - 1}"))
        if len(rows) > per and not user_id:
            nav.append(("▶️", f"adm:pays:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append(BACK)
        title = f"💸 Платежи пользователя {user_id}" if user_id else f"💸 Платежи (стр. {page + 1})"
        text = (f"{title}\n\nНажми на платёж, чтобы вернуть звёзды.\n"
                "Найти платежи человека: /refund <id пользователя>") if rows else f"{title}\n\nПлатежей пока нет."
        markup = kb(*buttons)
        if isinstance(target, CallbackQuery):
            await edit(target, text, markup)
        else:
            await target.answer(text, reply_markup=markup)

    @dp.callback_query(F.data.startswith("adm:pays:"), is_admin)
    async def adm_pays(cb: CallbackQuery) -> None:
        await show_payments(cb, int(cb.data.split(":")[2]))

    @dp.message(Command("refund"), is_admin)
    async def adm_refund_cmd(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Использование: /refund <id пользователя>\n(id видно в списке платежей и в статистике)")
            return
        await show_payments(message, 0, int(arg))

    @dp.callback_query(F.data.startswith("adm:pay:"), is_admin)
    async def adm_pay(cb: CallbackQuery) -> None:
        p = db.execute("SELECT p.*, u.name FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE p.rid=?",
                       (int(cb.data.split(":")[2]),)).fetchone()
        if not p:
            await cb.answer("Платёж не найден", show_alert=True)
            return
        text = (f"💳 Платёж\n\nПокупатель: {p['name'] or '—'} (id {p['user_id']})\nТовар: {ITEM_NAMES.get(p['item'], p['item'])}\n"
                f"Сумма: {p['stars']} ⭐\nДата: {p['created'][:16].replace('T', ' ')} UTC\nID платежа: {p['charge_id']}\n")
        if p["refunded"]:
            text += f"\n↩️ Возвращён {p['refunded'][:16].replace('T', ' ')} UTC"
            await edit(cb, text, kb([("⬅️ К платежам", "adm:pays:0")]))
        else:
            text += "\nПри возврате звёзды вернутся покупателю, а купленные расклады/подписка будут отозваны."
            await edit(cb, text, kb([("↩️ Вернуть звёзды", f"adm:rf:{p['rid']}")], [("⬅️ К платежам", "adm:pays:0")]))

    @dp.callback_query(F.data.startswith("adm:rf:"), is_admin)
    async def adm_refund_confirm(cb: CallbackQuery) -> None:
        rid = cb.data.split(":")[2]
        await edit(cb, "Точно вернуть звёзды? Это действие нельзя отменить.",
                   kb([("✅ Да, вернуть", f"adm:rfok:{rid}"), ("Отмена", f"adm:pay:{rid}")]))

    @dp.callback_query(F.data.startswith("adm:rfok:"), is_admin)
    async def adm_refund_do(cb: CallbackQuery) -> None:
        p = db.execute("SELECT * FROM payments WHERE rid=?", (int(cb.data.split(":")[2]),)).fetchone()
        if not p or p["refunded"]:
            await cb.answer("Уже возвращён или не найден", show_alert=True)
            return
        try:
            await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=p["charge_id"])
        except Exception as e:
            await edit(cb, f"❌ Telegram не принял возврат:\n{e}", kb([("⬅️ К платежам", "adm:pays:0")]))
            return
        if p["item"] == "sub":
            try:  # отменяем автопродление
                await bot.edit_user_star_subscription(user_id=p["user_id"], telegram_payment_charge_id=p["charge_id"], is_canceled=True)
            except Exception as e:
                log.info("cancel subscription: %s", e)
            db.execute("UPDATE users SET sub_until=? WHERE id=?", (utcnow().isoformat(), p["user_id"]))
        elif p["item"] in PACKS:
            n = PACKS[p["item"]]["spreads"]
            db.execute("UPDATE users SET credits=CASE WHEN credits>? THEN credits-? ELSE 0 END WHERE id=?", (n, n, p["user_id"]))
        db.execute("UPDATE payments SET refunded=? WHERE rid=?", (utcnow().isoformat(), p["rid"]))
        db.commit()
        try:
            await bot.send_message(p["user_id"], f"↩️ Тебе вернули {p['stars']} ⭐ за «{ITEM_NAMES.get(p['item'], p['item'])}».")
        except Exception:
            pass
        await edit(cb, f"✅ Возврат выполнен: {p['stars']} ⭐ пользователю {p['user_id']}.", kb([("⬅️ К платежам", "adm:pays:0")], BACK))

    # ── реклама ──
    def ad_markup_for_user(ad: sqlite3.Row) -> InlineKeyboardMarkup | None:
        if ad["url"]:
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=ad["button"] or "Подробнее", url=ad["url"])]])
        return None

    async def send_ad_preview(chat_id: int, ad: sqlite3.Row) -> None:
        text = ad["text"] or " "
        if ad["image"]:
            await bot.send_photo(chat_id, ad["image"], caption=text[:1024], reply_markup=ad_markup_for_user(ad))
        else:
            await bot.send_message(chat_id, text, reply_markup=ad_markup_for_user(ad))

    @dp.callback_query(F.data == "adm:ads", is_admin)
    async def adm_ads(cb: CallbackQuery) -> None:
        ad = active_ad()
        rows = [[("➕ Новая реклама", "adm:adnew")]]
        if ad:
            ctr = f"{ad['clicks'] / ad['views'] * 100:.1f}%" if ad["views"] else "—"
            text = (f"📢 Сейчас крутится реклама #{ad['id']}\n\n👁 Показов: {ad['views']}\n👆 Кликов: {ad['clicks']}\n📈 CTR: {ctr}\n\n"
                    "Её видят все на странице расклада, кроме подписчиков «Безлимита».")
            rows += [[("👀 Посмотреть", f"adm:adview:{ad['id']}"), ("⏸ Выключить", f"adm:adoff:{ad['id']}")]]
        else:
            text = "📢 Реклама сейчас выключена."
        old = db.execute("SELECT * FROM ads WHERE active=0 ORDER BY id DESC LIMIT 5").fetchall()
        for a in old:
            rows.append([(f"▶️ Включить #{a['id']} ({a['views']} пок., {a['clicks']} кл.)", f"adm:adon:{a['id']}"),
                         ("🗑", f"adm:addel:{a['id']}")])
        rows.append(BACK)
        await edit(cb, text, kb(*rows))

    @dp.callback_query(F.data.startswith("adm:adview:"), is_admin)
    async def adm_adview(cb: CallbackQuery) -> None:
        ad = db.execute("SELECT * FROM ads WHERE id=?", (int(cb.data.split(":")[2]),)).fetchone()
        if ad:
            await send_ad_preview(cb.from_user.id, ad)
        await cb.answer()

    @dp.callback_query(F.data.startswith("adm:adoff:"), is_admin)
    async def adm_adoff(cb: CallbackQuery) -> None:
        db.execute("UPDATE ads SET active=0 WHERE id=?", (int(cb.data.split(":")[2]),))
        db.commit()
        await adm_ads(cb)

    @dp.callback_query(F.data.startswith("adm:adon:"), is_admin)
    async def adm_adon(cb: CallbackQuery) -> None:
        db.execute("UPDATE ads SET active=0")
        db.execute("UPDATE ads SET active=1 WHERE id=?", (int(cb.data.split(":")[2]),))
        db.commit()
        await cb.answer("Реклама запущена ✅")
        await adm_ads(cb)

    @dp.callback_query(F.data.startswith("adm:addel:"), is_admin)
    async def adm_addel(cb: CallbackQuery) -> None:
        ad = db.execute("SELECT * FROM ads WHERE id=?", (int(cb.data.split(":")[2]),)).fetchone()
        if ad:
            _ad_img_cache.pop(ad["id"], None)
            db.execute("DELETE FROM ads WHERE id=?", (ad["id"],))
            db.commit()
        await cb.answer("Удалено")
        await adm_ads(cb)

    @dp.callback_query(F.data == "adm:adnew", is_admin)
    async def adm_adnew(cb: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AdForm.content)
        await edit(cb, "Шаг 1/3. Пришли рекламу: фото с подписью или просто текст.\n\nОтмена: /cancel", kb())

    @dp.message(AdForm.content, is_admin)
    async def adm_ad_content(message: Message, state: FSMContext) -> None:
        # картинка хранится в Telegram (file_id) — диск сервера на Render временный
        image = message.photo[-1].file_id if message.photo else None
        text = message.caption if message.photo else message.text
        if not text and not image:
            await message.answer("Нужен текст или фото. Попробуй ещё раз или /cancel")
            return
        await state.update_data(text=text or "", image=image)
        await state.set_state(AdForm.url)
        await message.answer("Шаг 2/3. Пришли ссылку (https://… или https://t.me/…).\nБез ссылки — отправь «-».")

    @dp.message(AdForm.url, is_admin)
    async def adm_ad_url(message: Message, state: FSMContext) -> None:
        url = (message.text or "").strip()
        if url == "-":
            url = ""
        elif url.startswith("t.me/"):
            url = "https://" + url
        elif not url.startswith(("https://", "http://")):
            await message.answer("Ссылка должна начинаться с https:// (или «-» без ссылки)")
            return
        await state.update_data(url=url)
        if not url:
            await finish_ad(message, state, "")
            return
        await state.set_state(AdForm.button)
        await message.answer("Шаг 3/3. Текст кнопки (например «Перейти в канал»).\nОтправь «-» — будет «Подробнее».")

    @dp.message(AdForm.button, is_admin)
    async def adm_ad_button(message: Message, state: FSMContext) -> None:
        b = (message.text or "").strip()
        await finish_ad(message, state, "Подробнее" if b in ("", "-") else b[:40])

    async def finish_ad(message: Message, state: FSMContext, button: str) -> None:
        d = await state.get_data()
        await state.clear()
        new_id = db.execute("INSERT INTO ads(text,image,url,button,active,created) VALUES(?,?,?,?,0,?) RETURNING id",
                            (d["text"], d["image"], d["url"], button, utcnow().isoformat())).fetchone()["id"]
        db.commit()
        ad = db.execute("SELECT * FROM ads WHERE id=?", (new_id,)).fetchone()
        await message.answer("Так реклама будет выглядеть (в приложении — карточкой под раскладом):")
        await send_ad_preview(message.chat.id, ad)
        await message.answer(f"Запускаем рекламу #{ad['id']}? Текущая реклама при этом выключится.",
                             reply_markup=kb([("✅ Запустить", f"adm:adon:{ad['id']}"), ("🗑 Удалить", f"adm:addel:{ad['id']}")], BACK))

    # обычные сообщения (не в процессе заполнения формы)
    @dp.message(F.text, StateFilter(None))
    async def any_text(message: Message) -> None:
        await message.answer("Нажми кнопку, чтобы сделать расклад 👇", reply_markup=app_kb())


# ───────────────────────── картинки карт ─────────────────────────
_card_locks: dict[str, asyncio.Lock] = {}
_wiki_sem: asyncio.Semaphore | None = None  # Wikimedia режет частые запросы — качаем по одной


async def card_bytes(card_id: str, session: aiohttp.ClientSession) -> bytes | None:
    """Картинка карты (колода Райдера–Уэйта, общественное достояние) из кэша или с Wikimedia Commons."""
    global _wiki_sem
    path = CARD_CACHE / f"{card_id}.jpg"
    if path.exists():
        return path.read_bytes()
    if _wiki_sem is None:
        _wiki_sem = asyncio.Semaphore(1)
    lock = _card_locks.setdefault(card_id, asyncio.Lock())
    async with lock, _wiki_sem:
        if path.exists():
            return path.read_bytes()
        url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + CARDS[card_id]["file"].replace(" ", "_") + "?width=330"
        for attempt in range(4):
            try:
                async with session.get(url, headers={"User-Agent": "TarotMiniApp/1.0 (https://t.me; tarot bot)"},
                                       timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 429 or r.status >= 500:
                        await asyncio.sleep(2 ** attempt * 2)
                        continue
                    if r.status != 200:
                        log.warning("card %s: HTTP %s", card_id, r.status)
                        return None
                    data = await r.read()
                    path.write_bytes(data)
                    await asyncio.sleep(0.3)
                    return data
            except Exception as e:
                log.warning("card %s: %s", card_id, e)
                await asyncio.sleep(2)
        log.warning("card %s: не удалось скачать", card_id)
        return None


async def fetch_all_cards() -> None:
    """python server.py --dev --fetch-cards — заранее скачать все 78 карт в data/cards."""
    async with aiohttp.ClientSession() as s:
        failed = []
        for i, cid in enumerate(CARDS, 1):
            ok = await card_bytes(cid, s)
            print(f"[{i}/78] {cid} {'ok' if ok else 'ОШИБКА'}", flush=True)
            if not ok:
                failed.append(cid)
        print("Готово." if not failed else f"Не скачались: {failed}")


async def h_card(request: web.Request) -> web.StreamResponse:
    cid = request.match_info["cid"]
    if cid not in CARDS:
        raise web.HTTPNotFound()
    data = await card_bytes(cid, request.app["http"])
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=2592000"})


_ad_img_cache: dict[int, bytes] = {}


async def h_ad_image(request: web.Request) -> web.Response:
    """Картинка рекламы: хранится в Telegram, отдаём через сервер с кэшем в памяти."""
    ad_id = int(request.match_info["aid"])
    if ad_id not in _ad_img_cache:
        ad = db.execute("SELECT image FROM ads WHERE id=?", (ad_id,)).fetchone()
        if not ad or not ad["image"]:
            raise web.HTTPNotFound()
        if bot:
            buf = await bot.download(ad["image"])
            _ad_img_cache[ad_id] = buf.read()
        elif (ADS_DIR / ad["image"]).is_file():  # локальный тест без бота
            _ad_img_cache[ad_id] = (ADS_DIR / ad["image"]).read_bytes()
        else:
            raise web.HTTPNotFound()
    return web.Response(body=_ad_img_cache[ad_id], content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


async def h_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


# ───────────────────────── авторизация Mini App ─────────────────────────
def auth(body: dict) -> sqlite3.Row:
    init_data = body.get("initData") or ""
    if DEV and not init_data:
        return get_user(1, "Dev")[0]
    if not BOT_TOKEN:
        raise web.HTTPUnauthorized(text="no bot token")
    try:
        data = safe_parse_webapp_init_data(BOT_TOKEN, init_data)
    except Exception:
        raise web.HTTPUnauthorized(text="bad initData")
    u = data.user
    ref = None
    sp = data.start_param or ""
    if sp.startswith("ref_") and sp[4:].isdigit():
        ref = int(sp[4:])
    row, _ = get_user(u.id, " ".join(filter(None, [u.first_name, u.last_name])), ref)
    if (row["username"] or "") != (u.username or "").lower():
        remember_username(u.id, u.username)
    return row


# ───────────────────────── API ─────────────────────────
async def h_me(request: web.Request) -> web.Response:
    body = await request.json()
    u = auth(body)
    if "daily" in body:
        db.execute("UPDATE users SET daily=? WHERE id=?", (1 if body["daily"] else 0, u["id"]))
        db.commit()
        u = user_row(u["id"])
    return web.json_response(profile(u))


async def h_draw(request: web.Request) -> web.Response:
    """Тянет карты на сервере и списывает: подписка → бесплатный лимит (простые) → купленный расклад."""
    body = await request.json()
    u = auth(body)
    spread = body.get("spread")
    if spread not in SPREADS:
        raise web.HTTPBadRequest(text="bad spread")
    n, tier = SPREADS[spread]
    uid = u["id"]

    if sub_active(u):
        paid_by = "sub"
    elif tier == "basic" and free_left(u) > 0:
        used = (u["free_used"] if u["free_date"] == today() else 0) + 1
        db.execute("UPDATE users SET free_date=?, free_used=? WHERE id=?", (today(), used, uid))
        paid_by = "free"
    else:
        cur = db.execute("UPDATE users SET credits=credits-1 WHERE id=? AND credits>0", (uid,))
        if cur.rowcount == 0:
            db.commit()
            return web.json_response({"error": "limit", "me": profile(user_row(uid))}, status=402)
        paid_by = "credit"

    rng = random.SystemRandom()
    cards = [[cid, 1 if rng.random() < 0.3 else 0] for cid in rng.sample(list(CARDS), n)]
    db.execute("INSERT INTO draws(user_id,spread,cards,paid_by,created) VALUES(?,?,?,?,?)",
               (uid, spread, json.dumps(cards), paid_by, utcnow().isoformat()))
    ad = None if paid_by == "sub" else active_ad()  # подписчикам «Безлимита» реклама не показывается
    if ad:
        db.execute("UPDATE ads SET views=views+1 WHERE id=?", (ad["id"],))
    db.commit()
    return web.json_response({"cards": cards, "paidBy": paid_by, "me": profile(user_row(uid)),
                              "ad": ad_public(ad) if ad else None})


async def h_ad_click(request: web.Request) -> web.Response:
    body = await request.json()
    auth(body)
    db.execute("UPDATE ads SET clicks=clicks+1 WHERE id=?", (int(body.get("id") or 0),))
    db.commit()
    return web.json_response({"ok": True})


async def h_invoice(request: web.Request) -> web.Response:
    body = await request.json()
    u = auth(body)
    item = body.get("item")
    if item not in PACKS and item != "sub":
        raise web.HTTPBadRequest(text="bad item")
    if DEV and not bot:  # в DEV оплата «проходит» сразу
        if item == "sub":
            extend_sub(u["id"])
        else:
            db.execute("UPDATE users SET credits=credits+? WHERE id=?", (PACKS[item]["spreads"], u["id"]))
            db.commit()
        return web.json_response({"dev": True})
    if item == "sub":
        link = await bot.create_invoice_link(
            title="Полночный Аркан: Безлимит", description="Все расклады без ограничений. Продлевается каждые 30 дней, отмена в любой момент.",
            payload=f"sub:{u['id']}", currency="XTR",
            prices=[LabeledPrice(label=SUB["title"], amount=SUB["stars"])],
            subscription_period=2592000,  # 30 дней — единственный период, который поддерживает Telegram
        )
    else:
        p = PACKS[item]
        link = await bot.create_invoice_link(
            title=f"Полночный Аркан: {p['title']}", description="Расклады не сгорают и подходят для любых раскладов, включая «Кельтский крест».",
            payload=f"{item}:{u['id']}", currency="XTR", prices=[LabeledPrice(label=p["title"], amount=p["stars"])],
        )
    return web.json_response({"link": link})


async def h_share(request: web.Request) -> web.Response:
    """Принимает PNG расклада, сохраняет (для сторис нужна публичная ссылка) и присылает его в чат."""
    body = await request.json()
    u = auth(body)
    img = body.get("image", "")
    if not img.startswith("data:image/png;base64,"):
        raise web.HTTPBadRequest()
    data = base64.b64decode(img.split(",", 1)[1])
    if len(data) > 5_000_000:
        raise web.HTTPRequestEntityTooLarge(max_size=5_000_000, actual_size=len(data))
    name = secrets.token_urlsafe(12) + ".png"
    (SHARES / name).write_bytes(data)
    sent = False
    if bot:
        try:
            await bot.send_photo(u["id"], BufferedInputFile(data, "tarot.png"),
                                 caption=f"🔮 Мой расклад — @{BOT_USERNAME}\nПерешли друзьям или выложи в сторис ✨")
            sent = True
        except Exception as e:
            log.warning("send_photo: %s", e)
    return web.json_response({"url": f"{WEBAPP_URL}shares/{name}", "sent": sent})


# ───────────────────────── ежедневная «Карта дня» ─────────────────────────
DAILY_TEXT = [
    "Сегодня обрати внимание на знаки — ответы рядом.", "Доверься интуиции в мелочах.",
    "Хороший день, чтобы начать то, что откладывал(а).", "Будь мягче к себе и к другим.",
    "Не спеши с выводами — дай ситуации раскрыться.", "Сделай один смелый шаг.",
]


async def daily_loop(app: web.Application) -> None:
    while True:
        try:
            t = dt.datetime.now(TZ)
            last = db.execute("SELECT v FROM meta WHERE k='daily_sent'").fetchone()
            if t.hour >= DAILY_HOUR and (not last or last[0] != today()):
                db.execute("INSERT INTO meta(k,v) VALUES('daily_sent', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (today(),))
                db.commit()
                await send_daily(app)
        except Exception:
            log.exception("daily loop")
        await asyncio.sleep(60)


async def send_daily(app: web.Application) -> None:
    users = [r[0] for r in db.execute("SELECT id FROM users WHERE daily=1 AND blocked=0")]
    log.info("Карта дня: рассылка %d пользователям", len(users))
    kb = app_kb("🔮 Сделать расклад")
    for uid in users:
        cid = random.choice(list(CARDS))
        rev = random.random() < 0.3
        caption = (f"☀️ Твоя карта дня: {CARDS[cid]['name']}{' (перевёрнутая)' if rev else ''}\n\n"
                   f"{random.choice(DAILY_TEXT)}\n\nЗначение карты и бесплатные расклады — в приложении 👇")
        img = await card_bytes(cid, app["http"])
        for _ in range(2):
            try:
                if img:
                    await bot.send_photo(uid, BufferedInputFile(img, "card.jpg"), caption=caption, reply_markup=kb)
                else:
                    await bot.send_message(uid, caption, reply_markup=kb)
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
                db.commit()
                break
            except Exception as e:
                log.warning("daily %s: %s", uid, e)
                break
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — в пределах лимитов Telegram


# ───────────────────────── запуск ─────────────────────────
async def h_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})


async def on_startup(app: web.Application) -> None:
    global BOT_USERNAME
    app["http"] = aiohttp.ClientSession()
    if bot:
        me = await bot.get_me()
        BOT_USERNAME = me.username
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🔮 Расклад", web_app=WebAppInfo(url=WEBAPP_URL)))
        if BOT_MODE == "webhook":
            # Render будит уснувший сервер входящим запросом — поэтому вебхук, а не polling
            await bot.set_webhook(WEBAPP_URL + "tg/webhook", secret_token=WEBHOOK_SECRET,
                                  allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
        else:
            await bot.delete_webhook()
            app["polling"] = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        app["daily"] = asyncio.create_task(daily_loop(app))
        log.info("Бот @%s запущен (%s)", BOT_USERNAME, BOT_MODE)


async def on_cleanup(app: web.Application) -> None:
    for k in ("polling", "daily"):
        if k in app:
            app[k].cancel()
    await app["http"].close()
    if bot:
        await bot.session.close()


def make_app() -> web.Application:
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app.router.add_get("/", h_index)
    app.router.add_get("/cards/{cid}.jpg", h_card)
    app.router.add_post("/api/me", h_me)
    app.router.add_post("/api/draw", h_draw)
    app.router.add_post("/api/invoice", h_invoice)
    app.router.add_post("/api/share", h_share)
    app.router.add_post("/api/ad_click", h_ad_click)
    app.router.add_get("/ads/{aid:\\d+}.jpg", h_ad_image)
    app.router.add_get("/health", h_health)
    app.router.add_static("/shares/", SHARES)
    if bot and BOT_MODE == "webhook":
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/tg/webhook")
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if "--fetch-cards" in sys.argv:
        asyncio.run(fetch_all_cards())
        raise SystemExit
    log.info("Mini App: %s  (DEV=%s)", WEBAPP_URL, DEV)
    web.run_app(make_app(), port=PORT)
