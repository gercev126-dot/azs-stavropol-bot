# Топливо 26 v3 — народная карта топлива: наличие + цены + актуальность
import asyncio
import logging
import math
import os
import sqlite3
import time
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Настройки ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = 5438874849
BOT_USERNAME = "AZS_Stavropol_bot"
# Токен платёжного провайдера. В России подключается через @BotFather -> Payments -> ЮKassa.
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "fuel.db")
STALE_SECONDS = 6 * 60 * 60  # общий статус (очередь/нет и т.д.) считается свежим 6 часов
PRICE_FRESH_SECONDS = 24 * 60 * 60   # 🟢 цена/наличие актуальны
PRICE_STALE_SECONDS = 7 * 24 * 60 * 60  # 🟡 устарело, дальше 🔴 нет данных
REPORT_COOLDOWN_SECONDS = 5 * 60  # не чаще раза в 5 минут на одну АЗС от одного человека
CITY_NAME = "Ставрополь"
SUB_PRICE_RUB = 149
SUB_DAYS = 30

STATUS_LABELS = {
    "available": "🟢 Есть",
    "limited": "🟡 Мало / лимит",
    "queue": "🟠 Очередь",
    "none": "🔴 Нет",
}
GOOD_STATUSES = {"available", "limited"}

# Виды топлива: код -> отображаемое имя
FUEL_INFO = {
    "92": "АИ-92",
    "95": "АИ-95",
    "98": "АИ-98/100",
    "dt": "Дизель",
    "gas": "Газ LPG",
}
FUEL_ORDER = ["92", "95", "98", "dt", "gas"]
FUEL_TYPES = FUEL_ORDER  # обратная совместимость с быстрым отчётом

# (name, address, region, lat, lon)
# Координаты — уровня квартала/улицы (для ранжирования "ближайшая АЗС"), не GPS-съёмка день в день.
SEED_STATIONS = [
    ("Лукойл", "ул. Доваторцев, 38 корп. 2", "Ставрополь", 45.02130, 41.93427),
    ("Лукойл", "просп. Кулакова, 7/2", "Ставрополь", 45.04800, 41.94700),
    ("Лукойл", "ул. Коломийцева, 27", "Ставрополь", 45.01800, 41.97800),
    ("Роснефть", "ул. Ленина, 431а/1", "Ставрополь", 45.01000, 41.96500),
    ("Роснефть", "ул. 8 Марта, 139", "Ставрополь", 45.02300, 41.92800),
    ("Роснефть", "ул. Бабушкина, 1В", "Ставрополь", 45.05000, 41.96500),
    ("Газпром", "ул. Пригородная, 249", "Ставрополь", 45.01000, 41.98500),
    ("Газпром", "просп. Кулакова, 6Б", "Ставрополь", 45.04700, 41.94600),
    ("Astra", "ул. Доваторцев, 52Б", "Ставрополь", 45.01900, 41.93600),
    ("Роснефть", "г. Михайловск, ул. Почтовая, 2", "Михайловск и округа", 45.13100, 42.02800),
    ("Лукойл", "г. Михайловск, ул. Привокзальная, 1", "Михайловск и округа", 45.13600, 42.04000),
    ("Роснефть", "г. Михайловск, ул. Гагарина, 1/1", "Михайловск и округа", 45.12900, 42.02500),
    ("Роснефть", "с. Надежда, ул. Мира, 139", "Михайловск и округа", 45.09500, 41.87000),
    ("Роснефть", "трасса А154 (Астрахань — Элиста — Ставрополь), 561 км", "Михайловск и округа", 45.06000, 41.91000),
]

# ---------- База данных ----------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT 'Ставрополь',
            lat REAL,
            lon REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            fuels TEXT,
            price REAL,
            note TEXT,
            ts INTEGER NOT NULL,
            user_id INTEGER,
            FOREIGN KEY(station_id) REFERENCES stations(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fuel_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL,
            fuel_type TEXT NOT NULL,
            price REAL,
            available INTEGER,
            photo_file_id TEXT,
            ts INTEGER NOT NULL,
            user_id INTEGER,
            user_name TEXT,
            FOREIGN KEY(station_id) REFERENCES stations(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            station_id INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            UNIQUE(user_id, station_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_seen INTEGER NOT NULL
        )"""
    )
    count = conn.execute("SELECT COUNT(*) AS c FROM stations").fetchone()["c"]
    if count == 0:
        conn.executemany(
            "INSERT INTO stations (name, address, region, lat, lon) VALUES (?, ?, ?, ?, ?)",
            SEED_STATIONS,
        )
        conn.commit()
    conn.close()


def get_stations():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM stations ORDER BY region, name").fetchall()
    conn.close()
    return rows


def get_station(station_id):
    conn = db_connect()
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (station_id,)).fetchone()
    conn.close()
    return row


def add_station(name, address, region="Ставрополь", lat=None, lon=None):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO stations (name, address, region, lat, lon) VALUES (?, ?, ?, ?, ?)",
        (name, address, region, lat, lon),
    )
    conn.commit()
    station_id = cur.lastrowid
    conn.close()
    return station_id


def get_latest_report(station_id):
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    conn.close()
    return row


def add_report(station_id, status, fuels, note, user_id, price=None):
    conn = db_connect()
    prev = conn.execute(
        "SELECT * FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO reports (station_id, status, fuels, price, note, ts, user_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (station_id, status, ",".join(fuels), price, note, int(time.time()), user_id),
    )
    conn.commit()
    conn.close()
    return prev


# ---------- Цены по видам топлива ----------

def add_fuel_price(station_id, fuel_type, price, available, user_id, user_name, photo_file_id=None):
    conn = db_connect()
    conn.execute(
        """INSERT INTO fuel_prices (station_id, fuel_type, price, available, photo_file_id, ts, user_id, user_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (station_id, fuel_type, price, 1 if available else 0, photo_file_id, int(time.time()), user_id, user_name),
    )
    conn.commit()
    conn.close()


def get_latest_fuel_price(station_id, fuel_type):
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM fuel_prices WHERE station_id = ? AND fuel_type = ? ORDER BY ts DESC LIMIT 1",
        (station_id, fuel_type),
    ).fetchone()
    conn.close()
    return row


def get_station_fuel_prices(station_id):
    """Возвращает {fuel_code: latest_row_or_None} по всем видам топлива станции."""
    return {code: get_latest_fuel_price(station_id, code) for code in FUEL_ORDER}


def get_price_history(station_id, fuel_type, limit_days=2):
    """Последние записи по разным дням (для отображения истории цен)."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT price, ts FROM fuel_prices WHERE station_id = ? AND fuel_type = ? AND price IS NOT NULL ORDER BY ts DESC LIMIT 50",
        (station_id, fuel_type),
    ).fetchall()
    conn.close()
    seen_days = []
    result = []
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r["ts"]))
        if day not in seen_days:
            seen_days.append(day)
            result.append({"day": day, "price": r["price"], "ts": r["ts"]})
        if len(result) >= limit_days:
            break
    return result


def get_city_fuel_averages():
    """Средняя цена по городу и самая дешёвая станция для каждого вида топлива."""
    conn = db_connect()
    now = int(time.time())
    result = {}
    for code in FUEL_ORDER:
        rows = conn.execute(
            """SELECT fp.price, fp.station_id, fp.ts, s.name, s.address
               FROM fuel_prices fp
               JOIN stations s ON s.id = fp.station_id
               WHERE fp.fuel_type = ? AND fp.available = 1 AND fp.price IS NOT NULL
               ORDER BY fp.ts DESC""",
            (code,),
        ).fetchall()
        latest_per_station = {}
        for r in rows:
            if r["station_id"] not in latest_per_station and (now - r["ts"]) < PRICE_STALE_SECONDS:
                latest_per_station[r["station_id"]] = r
        prices = [r["price"] for r in latest_per_station.values()]
        cheapest = min(latest_per_station.values(), key=lambda r: r["price"]) if latest_per_station else None
        result[code] = {
            "avg": round(sum(prices) / len(prices), 2) if prices else None,
            "cheapest": cheapest,
            "count": len(prices),
        }
    conn.close()
    return result


def get_cheapest_stations(fuel_type, limit=5):
    conn = db_connect()
    now = int(time.time())
    rows = conn.execute(
        """SELECT fp.price, fp.station_id, fp.ts, s.name, s.address, s.region
           FROM fuel_prices fp
           JOIN stations s ON s.id = fp.station_id
           WHERE fp.fuel_type = ? AND fp.available = 1 AND fp.price IS NOT NULL
           ORDER BY fp.ts DESC""",
        (fuel_type,),
    ).fetchall()
    conn.close()
    latest_per_station = {}
    for r in rows:
        if r["station_id"] not in latest_per_station and (now - r["ts"]) < PRICE_STALE_SECONDS:
            latest_per_station[r["station_id"]] = r
    result = sorted(latest_per_station.values(), key=lambda r: r["price"])
    return result[:limit]


def get_available_stations(fuel_type, limit=10):
    conn = db_connect()
    now = int(time.time())
    rows = conn.execute(
        """SELECT fp.price, fp.station_id, fp.ts, s.name, s.address, s.region
           FROM fuel_prices fp
           JOIN stations s ON s.id = fp.station_id
           WHERE fp.fuel_type = ? AND fp.available = 1
           ORDER BY fp.ts DESC""",
        (fuel_type,),
    ).fetchall()
    conn.close()
    latest_per_station = {}
    for r in rows:
        if r["station_id"] not in latest_per_station and (now - r["ts"]) < PRICE_STALE_SECONDS:
            latest_per_station[r["station_id"]] = r
    result = list(latest_per_station.values())
    result.sort(key=lambda r: (r["price"] is None, r["price"]))
    return result[:limit]


def get_last_activity(station_id):
    """Самое свежее событие по станции — либо быстрый отчёт, либо обновление цены. Для 'последнее обновление'."""
    conn = db_connect()
    report = conn.execute(
        "SELECT ts, user_id, NULL as user_name FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    price = conn.execute(
        "SELECT ts, user_id, user_name FROM fuel_prices WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    conn.close()
    candidates = [r for r in (report, price) if r]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r["ts"])


def get_recent_changes(limit=15):
    """Для админки — последние изменения по всем станциям (и статусы, и цены)."""
    conn = db_connect()
    rows = conn.execute(
        """SELECT r.ts, r.user_id, NULL as user_name, s.name, s.address, r.status as info, 'статус' as kind
           FROM reports r JOIN stations s ON s.id = r.station_id
           UNION ALL
           SELECT fp.ts, fp.user_id, fp.user_name, s.name, s.address,
                  (CASE WHEN fp.available = 1 THEN fp.fuel_type || ' = ' || COALESCE(fp.price, 0) ELSE fp.fuel_type || ' нет' END) as info,
                  'цена' as kind
           FROM fuel_prices fp JOIN stations s ON s.id = fp.station_id
           ORDER BY ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


# ---------- Вспомогательное ----------

def time_ago(ts):
    diff = int(time.time()) - ts
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин назад"
    if diff < 86400:
        return f"{diff // 3600} ч назад"
    return f"{diff // 86400} дн назад"


def freshness_label(ts):
    if ts is None:
        return "🔴 Нет данных"
    diff = int(time.time()) - ts
    if diff < PRICE_FRESH_SECONDS:
        return f"🟢 Актуально ({time_ago(ts)})"
    if diff < PRICE_STALE_SECONDS:
        return f"🟡 Устарело ({time_ago(ts)})"
    return "🔴 Нет свежих данных"


def yandex_maps_link(name, address):
    query = f"{name} {address} {CITY_NAME}"
    return f"https://yandex.ru/maps/?text={quote(query)}"


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def get_display_name(user):
    if user and user.first_name:
        return user.first_name
    return "пользователь"


def register_user(user_id):
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
        (user_id, int(time.time())),
    )
    conn.commit()
    conn.close()


def get_all_user_ids():
    conn = db_connect()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def seconds_since_last_report(user_id, station_id):
    conn = db_connect()
    row = conn.execute(
        "SELECT ts FROM reports WHERE user_id = ? AND station_id = ? ORDER BY ts DESC LIMIT 1",
        (user_id, station_id),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return int(time.time()) - row["ts"]


def get_confirmation_count(station_id):
    conn = db_connect()
    rows = conn.execute(
        "SELECT status, user_id, ts FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 15",
        (station_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return 0
    latest_status = rows[0]["status"]
    now = int(time.time())
    users = set()
    for r in rows:
        if r["status"] != latest_status or (now - r["ts"]) > STALE_SECONDS:
            break
        users.add(r["user_id"])
    return len(users)


def get_overall_stats():
    conn = db_connect()
    stations = conn.execute("SELECT id FROM stations").fetchall()
    now = int(time.time())
    fresh_total = 0
    fresh_good = 0
    for s in stations:
        row = conn.execute(
            "SELECT * FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
            (s["id"],),
        ).fetchone()
        if row and (now - row["ts"]) < STALE_SECONDS:
            fresh_total += 1
            if row["status"] in GOOD_STATUSES:
                fresh_good += 1
    reports_today = conn.execute(
        "SELECT COUNT(*) AS c FROM reports WHERE ts > ?", (now - 86400,)
    ).fetchone()["c"]
    conn.close()
    return {
        "total_stations": len(stations),
        "fresh_total": fresh_total,
        "fresh_good": fresh_good,
        "reports_today": reports_today,
    }


# ---------- Подписки ----------

def add_subscription(user_id, station_id, days=SUB_DAYS):
    conn = db_connect()
    existing = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? AND station_id = ?",
        (user_id, station_id),
    ).fetchone()
    now = int(time.time())
    if existing:
        base = max(existing["expires_at"], now)
        new_expiry = base + days * 86400
        conn.execute("UPDATE subscriptions SET expires_at = ? WHERE id = ?", (new_expiry, existing["id"]))
    else:
        new_expiry = now + days * 86400
        conn.execute(
            "INSERT INTO subscriptions (user_id, station_id, expires_at) VALUES (?, ?, ?)",
            (user_id, station_id, new_expiry),
        )
    conn.commit()
    conn.close()
    return new_expiry


def get_active_subscriptions_for_station(station_id):
    conn = db_connect()
    now = int(time.time())
    rows = conn.execute(
        "SELECT * FROM subscriptions WHERE station_id = ? AND expires_at > ?",
        (station_id, now),
    ).fetchall()
    conn.close()
    return rows


def get_user_subscriptions(user_id):
    conn = db_connect()
    now = int(time.time())
    rows = conn.execute(
        """SELECT subscriptions.*, stations.name, stations.address
           FROM subscriptions JOIN stations ON stations.id = subscriptions.station_id
           WHERE subscriptions.user_id = ? AND subscriptions.expires_at > ?
           ORDER BY subscriptions.expires_at""",
        (user_id, now),
    ).fetchall()
    conn.close()
    return rows


# ---------- FSM состояния ----------

class ReportFlow(StatesGroup):
    choosing_station = State()
    choosing_status = State()
    choosing_fuels = State()
    typing_price = State()
    typing_note = State()


class AddStationFlow(StatesGroup):
    typing_name = State()
    typing_address = State()
    choosing_region = State()


class SubscribeFlow(StatesGroup):
    choosing_station = State()


class PriceFlow(StatesGroup):
    choosing_fuel = State()
    typing_price = State()
    waiting_photo = State()


router = Router()
bot_instance: Bot | None = None


@router.message.middleware()
async def register_user_middleware(handler, event: Message, data):
    if event.from_user:
        register_user(event.from_user.id)
    return await handler(event, data)


# ---------- Клавиатуры ----------

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗺 Где есть топливо")],
            [KeyboardButton(text="💰 Цены"), KeyboardButton(text="🔍 Найти")],
            [KeyboardButton(text="📍 Рядом со мной", request_location=True)],
            [KeyboardButton(text="📍 Сообщить наличие"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="📤 Поделиться")],
            [KeyboardButton(text="ℹ️ О проекте")],
        ],
        resize_keyboard=True,
    )


def stations_keyboard(prefix="pick_station"):
    kb = []
    for s in get_stations():
        latest = get_latest_report(s["id"])
        if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS:
            icon = STATUS_LABELS[latest["status"]].split()[0]
        else:
            icon = "⚪️"
        region_tag = "" if s["region"] == "Ставрополь" else f" [{s['region']}]"
        label = f"{icon} {s['name']} — {s['address']}{region_tag}"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{s['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def status_keyboard():
    kb = [[InlineKeyboardButton(text=label, callback_data=f"status:{key}")] for key, label in STATUS_LABELS.items()]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def fuels_keyboard(selected):
    row = []
    for code in FUEL_ORDER:
        mark = "✅ " if code in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{FUEL_INFO[code]}", callback_data=f"fuel:{code}"))
    rows = [row[:3], row[3:], [InlineKeyboardButton(text="Готово ➡️", callback_data="fuel:done")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fuel_price_keyboard(station_id):
    kb = []
    prices = get_station_fuel_prices(station_id)
    for code in FUEL_ORDER:
        row = prices[code]
        if row and row["available"] and row["price"]:
            label = f"{FUEL_INFO[code]} — {row['price']:.2f}₽"
        elif row and not row["available"]:
            label = f"{FUEL_INFO[code]} — нет в наличии"
        else:
            label = f"{FUEL_INFO[code]} — нет данных"
        kb.append([InlineKeyboardButton(text=label, callback_data=f"price_fuel:{station_id}:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def build_station_card(station_id):
    station = get_station(station_id)
    prices = get_station_fuel_prices(station_id)
    lines = [f"⛽ *{station['name']}*", f"📍 {station['region']}, {station['address']}", "", "Топливо:"]
    for code in FUEL_ORDER:
        row = prices[code]
        if row and row["available"] and row["price"]:
            lines.append(f"🟢 {FUEL_INFO[code]} — {row['price']:.2f} ₽")
        elif row and row["available"]:
            lines.append(f"🟢 {FUEL_INFO[code]} — есть, цена неизвестна")
        elif row and not row["available"]:
            lines.append(f"🔴 {FUEL_INFO[code]} — нет данных")
        else:
            lines.append(f"⚪️ {FUEL_INFO[code]} — нет данных")

    activity = get_last_activity(station_id)
    lines.append("")
    if activity:
        lines.append(f"🕒 Последнее обновление: {time_ago(activity['ts'])}")
        who = activity["user_name"] if activity["user_name"] else "пользователь"
        lines.append(f"👤 Обновил: {who}")
        lines.append(freshness_label(activity["ts"]))
    else:
        lines.append("🔴 Нет данных по этой станции")

    map_link = yandex_maps_link(station["name"], station["address"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть маршрут", url=map_link)],
        [InlineKeyboardButton(text="🔄 Сообщить наличие", callback_data=f"quick_report:{station_id}")],
        [InlineKeyboardButton(text="💰 Обновить цену", callback_data=f"price_menu:{station_id}")],
        [InlineKeyboardButton(text="🔔 Подписаться", callback_data=f"card_sub:{station_id}")],
    ])
    return "\n".join(lines), kb


async def send_subscription_invoice(bot, chat_id, user_id, station_id):
    if not PAYMENT_PROVIDER_TOKEN:
        await bot.send_message(
            chat_id,
            "Оплата пока не подключена. Владельцу бота нужно привязать платёжного "
            "провайдера через @BotFather → Payments и добавить PAYMENT_PROVIDER_TOKEN.",
        )
        return
    station = get_station(station_id)
    prices = [LabeledPrice(label=f"Подписка на {SUB_DAYS} дней", amount=SUB_PRICE_RUB * 100)]
    await bot.send_invoice(
        chat_id=chat_id,
        title=f"Уведомления: {station['name']}, {station['address']}",
        description=f"Пришлю сообщение, как только на этой АЗС появится топливо. Действует {SUB_DAYS} дней.",
        payload=f"sub:{station_id}:{user_id}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
    )


# ---------- Хендлеры: базовые ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "⛽ Топливо 26\n\n"
        "Народная карта топлива Ставропольского края: наличие + цены + актуальность.\n\n"
        "Чем больше людей отправляют данные — тем точнее карта.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(F.text == "🗺 Где есть топливо")
async def menu_list(message: Message):
    await cmd_list(message)

@router.message(F.text == "📍 Сообщить наличие")
async def menu_report(message: Message, state: FSMContext):
    await cmd_report(message, state)

@router.message(F.text == "🔔 Мои уведомления")
async def menu_subs(message: Message):
    await cmd_mysubs(message)

@router.message(F.text == "📊 Статистика")
async def menu_stats(message: Message):
    await cmd_stats(message)

@router.message(F.text == "💰 Цены")
async def menu_prices(message: Message):
    await cmd_prices(message)

@router.message(F.text == "🔍 Найти")
async def menu_find(message: Message):
    await cmd_find(message)

@router.message(F.text == "📤 Поделиться")
async def menu_share(message: Message):
    bot_link = f"https://t.me/{BOT_USERNAME}"
    share_text = "Бот показывает, где в Ставрополе и области есть топливо и почём 👇"
    share_url = f"https://t.me/share/url?url={quote(bot_link)}&text={quote(share_text)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить другу или в чат", url=share_url)]])
    await message.answer("Перешли бота знакомым — чем больше людей отмечают станции, тем точнее карта для всех.", reply_markup=kb)

@router.message(F.text == "ℹ️ О проекте")
async def menu_about(message: Message):
    await message.answer(
        "⛽ Топливо 26 — народная карта топлива.\n\n"
        "Наличие, цены по видам топлива и их актуальность — от самих водителей."
    )


# ---------- /list — выбор района ----------

@router.message(Command("list"))
async def cmd_list(message: Message):
    stations = get_stations()
    if not stations:
        await message.answer("Пока нет станций в базе.")
        return
    regions_seen = []
    for s in stations:
        if s["region"] not in regions_seen:
            regions_seen.append(s["region"])
    kb_rows = [[InlineKeyboardButton(text=f"📍 {region}", callback_data=f"region_menu:{region}")] for region in regions_seen]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("Выбери район:", reply_markup=kb)


@router.callback_query(F.data.startswith("region_menu:"))
async def region_menu(callback: CallbackQuery):
    region = callback.data.split(":", 1)[1]
    stations = [s for s in get_stations() if s["region"] == region]
    kb_rows = []
    for s in stations:
        latest = get_latest_report(s["id"])
        icon = STATUS_LABELS[latest["status"]].split()[0] if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS else "⚪️"
        kb_rows.append([InlineKeyboardButton(text=f"{icon} {s['name']}, {s['address']}", callback_data=f"station_card:{s['id']}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer(f"📍 {region} — выбери станцию:", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("station_card:"))
async def open_station_card(callback: CallbackQuery):
    station_id = int(callback.data.split(":")[1])
    text, kb = build_station_card(station_id)
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ---------- Быстрое обновление статуса (как раньше) ----------

@router.callback_query(F.data.startswith("quick_report:"))
async def quick_report(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    cooldown = seconds_since_last_report(callback.from_user.id, station_id)
    if cooldown is not None and cooldown < REPORT_COOLDOWN_SECONDS:
        wait_min = (REPORT_COOLDOWN_SECONDS - cooldown) // 60 + 1
        await callback.answer(f"Ты уже отмечал эту АЗС недавно. Попробуй через {wait_min} мин.", show_alert=True)
        return

    latest = get_latest_report(station_id)
    station = get_station(station_id)
    if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS:
        label = STATUS_LABELS[latest["status"]]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё так же", callback_data=f"confirm:{station_id}")],
            [InlineKeyboardButton(text="✏️ Статус изменился", callback_data=f"change_status:{station_id}")],
        ])
        await callback.message.answer(
            f"{station['name']}, {station['address']}\nСейчас в базе: {label} ({time_ago(latest['ts'])})",
            reply_markup=kb,
        )
        await callback.answer()
        return

    await state.update_data(station_id=station_id, fuels=[])
    await state.set_state(ReportFlow.choosing_status)
    await callback.message.answer("Какой сейчас статус?", reply_markup=status_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_report(callback: CallbackQuery):
    station_id = int(callback.data.split(":")[1])
    cooldown = seconds_since_last_report(callback.from_user.id, station_id)
    if cooldown is not None and cooldown < REPORT_COOLDOWN_SECONDS:
        wait_min = (REPORT_COOLDOWN_SECONDS - cooldown) // 60 + 1
        await callback.answer(f"Ты уже отмечал эту АЗС недавно. Попробуй через {wait_min} мин.", show_alert=True)
        return
    latest = get_latest_report(station_id)
    if not latest:
        await callback.answer("Нет данных для подтверждения", show_alert=True)
        return
    fuels = latest["fuels"].split(",") if latest["fuels"] else []
    add_report(station_id, latest["status"], fuels, "Подтверждено", callback.from_user.id, price=latest["price"])
    count = get_confirmation_count(station_id)
    await callback.message.edit_text(f"Спасибо! Подтверждено ✅ (уже {count} чел. согласны)")
    await callback.answer()


@router.callback_query(F.data.startswith("change_status:"))
async def change_status(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    cooldown = seconds_since_last_report(callback.from_user.id, station_id)
    if cooldown is not None and cooldown < REPORT_COOLDOWN_SECONDS:
        wait_min = (REPORT_COOLDOWN_SECONDS - cooldown) // 60 + 1
        await callback.answer(f"Ты уже отмечал эту АЗС недавно. Попробуй через {wait_min} мин.", show_alert=True)
        return
    await state.update_data(station_id=station_id, fuels=[])
    await state.set_state(ReportFlow.choosing_status)
    await callback.message.edit_text("Какой сейчас статус?", reply_markup=status_keyboard())
    await callback.answer()


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.set_state(ReportFlow.choosing_station)
    await message.answer("Какая АЗС?", reply_markup=stations_keyboard())


@router.callback_query(ReportFlow.choosing_station, F.data.startswith("pick_station:"))
async def pick_station(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    cooldown = seconds_since_last_report(callback.from_user.id, station_id)
    if cooldown is not None and cooldown < REPORT_COOLDOWN_SECONDS:
        wait_min = (REPORT_COOLDOWN_SECONDS - cooldown) // 60 + 1
        await callback.answer(f"Ты уже отмечал эту АЗС недавно. Попробуй через {wait_min} мин.", show_alert=True)
        return
    await state.update_data(station_id=station_id, fuels=[])
    await state.set_state(ReportFlow.choosing_status)
    await callback.message.edit_text("Какой сейчас статус?", reply_markup=status_keyboard())
    await callback.answer()


@router.callback_query(ReportFlow.choosing_status, F.data.startswith("status:"))
async def pick_status(callback: CallbackQuery, state: FSMContext):
    status = callback.data.split(":")[1]
    await state.update_data(status=status)
    await state.set_state(ReportFlow.choosing_fuels)
    data = await state.get_data()
    await callback.message.edit_text(
        "Какое топливо есть/касается? (можно несколько, потом «Готово»)",
        reply_markup=fuels_keyboard(data.get("fuels", [])),
    )
    await callback.answer()


@router.callback_query(ReportFlow.choosing_fuels, F.data.startswith("fuel:"))
async def pick_fuel(callback: CallbackQuery, state: FSMContext):
    value = callback.data.split(":")[1]
    data = await state.get_data()
    fuels = data.get("fuels", [])

    if value == "done":
        await state.set_state(ReportFlow.typing_price)
        await callback.message.edit_text("Почём топливо? (например: 61.20)\nЕсли не знаешь или не важно — напиши «-»")
        await callback.answer()
        return

    if value in fuels:
        fuels.remove(value)
    else:
        fuels.append(value)
    await state.update_data(fuels=fuels)
    await callback.message.edit_reply_markup(reply_markup=fuels_keyboard(fuels))
    await callback.answer()


@router.message(ReportFlow.typing_price)
async def type_price(message: Message, state: FSMContext):
    raw = message.text.strip().replace(",", ".")
    price = None
    if raw != "-":
        try:
            price = float(raw)
            if price <= 0 or price > 500:
                raise ValueError
        except ValueError:
            await message.answer("Не понял цену. Напиши число, например 61.20, или «-», если пропускаем.")
            return
    await state.update_data(price=price)
    await state.set_state(ReportFlow.typing_note)
    await message.answer("Комментарий? (например: лимит 30 л, подвезли утром)\nЕсли нечего добавить, напиши «-»")


@router.message(ReportFlow.typing_note)
async def type_note(message: Message, state: FSMContext):
    note = "" if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    station_id = data["station_id"]
    new_status = data["status"]
    fuels = data.get("fuels", [])
    price = data.get("price")

    prev = add_report(station_id=station_id, status=new_status, fuels=fuels, note=note, user_id=message.from_user.id, price=price)
    await state.clear()
    await message.answer("Спасибо! Отчёт сохранён и виден всем в /list ✅")

    became_good = new_status in GOOD_STATUSES and (prev is None or prev["status"] not in GOOD_STATUSES)
    if became_good:
        await notify_subscribers(station_id, new_status, fuels)


# ---------- Цены по видам топлива ----------

@router.callback_query(F.data.startswith("price_menu:"))
async def price_menu(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    await state.update_data(price_station_id=station_id)
    await state.set_state(PriceFlow.choosing_fuel)
    await callback.message.answer("Какое топливо обновляем?", reply_markup=fuel_price_keyboard(station_id))
    await callback.answer()


@router.callback_query(PriceFlow.choosing_fuel, F.data.startswith("price_fuel:"))
async def price_pick_fuel(callback: CallbackQuery, state: FSMContext):
    _, station_id_str, fuel_code = callback.data.split(":")
    station_id = int(station_id_str)
    await state.update_data(price_station_id=station_id, price_fuel=fuel_code)
    await state.set_state(PriceFlow.typing_price)
    await callback.message.answer(
        f"{FUEL_INFO[fuel_code]} — какая цена? Напиши число (например 61.20).\n"
        f"Если этого топлива нет на станции — напиши «нет»."
    )
    await callback.answer()


@router.message(PriceFlow.typing_price)
async def price_type_price(message: Message, state: FSMContext):
    raw = message.text.strip().replace(",", ".").lower()
    if raw in ("нет", "нету", "-"):
        await state.update_data(price_value=None, price_available=False)
    else:
        try:
            value = float(raw)
            if value <= 0 or value > 500:
                raise ValueError
        except ValueError:
            await message.answer("Не понял. Напиши число (например 61.20) или «нет», если топлива нет.")
            return
        await state.update_data(price_value=value, price_available=True)

    await state.set_state(PriceFlow.waiting_photo)
    await message.answer("Можешь прислать фото таблo цен (по желанию). Если не хочешь — напиши «-».")


@router.message(PriceFlow.waiting_photo, F.photo)
async def price_with_photo(message: Message, state: FSMContext):
    photo_file_id = message.photo[-1].file_id
    await save_price_update(message, state, photo_file_id)


@router.message(PriceFlow.waiting_photo, F.text)
async def price_without_photo(message: Message, state: FSMContext):
    await save_price_update(message, state, None)


async def save_price_update(message: Message, state: FSMContext, photo_file_id):
    data = await state.get_data()
    station_id = data["price_station_id"]
    fuel_code = data["price_fuel"]
    price = data.get("price_value")
    available = data.get("price_available", False)
    user_name = get_display_name(message.from_user)

    add_fuel_price(station_id, fuel_code, price, available, message.from_user.id, user_name, photo_file_id)
    await state.clear()

    station = get_station(station_id)
    if available and price:
        result_text = f"Спасибо! {FUEL_INFO[fuel_code]} на «{station['name']}» — {price:.2f}₽ ✅"
    elif available:
        result_text = f"Спасибо! Отметил, что {FUEL_INFO[fuel_code]} есть на «{station['name']}» ✅"
    else:
        result_text = f"Спасибо! Отметил, что {FUEL_INFO[fuel_code]} нет на «{station['name']}» ✅"
    await message.answer(result_text)


# ---------- Подписка через карточку станции ----------

@router.callback_query(F.data.startswith("card_sub:"))
async def card_subscribe(callback: CallbackQuery):
    station_id = int(callback.data.split(":")[1])
    await send_subscription_invoice(callback.bot, callback.message.chat.id, callback.from_user.id, station_id)
    await callback.answer()


# ---------- Раздел "Цены" ----------

@router.message(Command("prices"))
async def cmd_prices(message: Message):
    stats = get_city_fuel_averages()
    lines = [f"💰 Цены на топливо по {CITY_NAME}\n"]
    lines.append("Средняя цена по городу:\n")
    for code in FUEL_ORDER:
        s = stats[code]
        if s["avg"]:
            lines.append(f"{FUEL_INFO[code]}: {s['avg']:.2f} ₽ ({s['count']} АЗС)")
        else:
            lines.append(f"{FUEL_INFO[code]}: нет данных")
    lines.append("\nСамая дешёвая АЗС:\n")
    for code in FUEL_ORDER:
        s = stats[code]
        if s["cheapest"]:
            c = s["cheapest"]
            lines.append(f"{FUEL_INFO[code]}: {c['name']} — {c['price']:.2f} ₽ ({c['address']})")
    await message.answer("\n".join(lines))


# ---------- Умный поиск ----------

@router.message(Command("find"))
async def cmd_find(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Самый дешёвый бензин", callback_data="find_cheapest_menu")],
        [InlineKeyboardButton(text="⛽ Где есть газ", callback_data="find_avail:gas")],
        [InlineKeyboardButton(text="🚛 Где есть дизель", callback_data="find_avail:dt")],
    ])
    await message.answer(
        "🔍 Что ищем?\n\n(Для поиска ближайшей АЗС нажми «📍 Рядом со мной» в меню внизу — бот попросит поделиться геолокацией)",
        reply_markup=kb,
    )


@router.callback_query(F.data == "find_cheapest_menu")
async def find_cheapest_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=FUEL_INFO[code], callback_data=f"find_cheapest:{code}")] for code in FUEL_ORDER
    ])
    await callback.message.answer("Какое топливо?", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("find_cheapest:"))
async def find_cheapest(callback: CallbackQuery):
    fuel_code = callback.data.split(":")[1]
    results = get_cheapest_stations(fuel_code, limit=5)
    if not results:
        await callback.message.answer(f"Пока нет свежих данных о ценах на {FUEL_INFO[fuel_code]}.")
        await callback.answer()
        return
    lines = [f"💰 Самый дешёвый {FUEL_INFO[fuel_code]}:\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['price']:.2f}₽ — {r['name']}, {r['address']} ({time_ago(r['ts'])})")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data.startswith("find_avail:"))
async def find_avail(callback: CallbackQuery):
    fuel_code = callback.data.split(":")[1]
    results = get_available_stations(fuel_code, limit=10)
    if not results:
        await callback.message.answer(f"Пока нет свежих отметок о наличии {FUEL_INFO[fuel_code]}.")
        await callback.answer()
        return
    lines = [f"⛽ Где есть {FUEL_INFO[fuel_code]}:\n"]
    for r in results:
        price_text = f" — {r['price']:.2f}₽" if r["price"] else ""
        lines.append(f"🟢 {r['name']}, {r['address']}{price_text} ({time_ago(r['ts'])})")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ---------- Геолокация ----------

@router.message(F.location)
async def handle_location(message: Message):
    user_lat = message.location.latitude
    user_lon = message.location.longitude
    stations = [s for s in get_stations() if s["lat"] and s["lon"]]
    if not stations:
        await message.answer("Координаты станций ещё не заполнены.")
        return

    ranked = sorted(stations, key=lambda s: haversine_km(user_lat, user_lon, s["lat"], s["lon"]))
    lines = ["📍 Ближайшие АЗС:\n"]
    kb_rows = []
    for s in ranked[:5]:
        dist_km = haversine_km(user_lat, user_lon, s["lat"], s["lon"])
        dist_text = f"{int(dist_km * 1000)} м" if dist_km < 1 else f"{dist_km:.1f} км"
        prices = get_station_fuel_prices(s["id"])
        avail_bits = []
        for code in FUEL_ORDER:
            row = prices[code]
            if row and row["available"]:
                price_text = f" {row['price']:.2f}₽" if row["price"] else ""
                avail_bits.append(f"{FUEL_INFO[code]}{price_text}")
        avail_text = ", ".join(avail_bits) if avail_bits else "нет свежих данных"
        lines.append(f"{dist_text}: {s['name']}, {s['address']}\n{avail_text}\n")
        kb_rows.append([InlineKeyboardButton(text=f"{s['name']} ({dist_text})", callback_data=f"station_card:{s['id']}")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("\n".join(lines), reply_markup=kb)


# ---------- Добавление станций (админ) ----------

@router.message(Command("addstation"))
async def cmd_add_station(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    await state.set_state(AddStationFlow.typing_name)
    await message.answer("Название сети АЗС (например: Лукойл):")


@router.message(AddStationFlow.typing_name)
async def add_station_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddStationFlow.typing_address)
    await message.answer("Адрес / ориентир:")


@router.message(AddStationFlow.typing_address)
async def add_station_address(message: Message, state: FSMContext):
    await state.update_data(address=message.text.strip())
    await state.set_state(AddStationFlow.choosing_region)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ставрополь", callback_data="region:Ставрополь")],
        [InlineKeyboardButton(text="Михайловск и округа", callback_data="region:Михайловск и округа")],
    ])
    await message.answer("В каком районе станция?", reply_markup=kb)


@router.callback_query(AddStationFlow.choosing_region, F.data.startswith("region:"))
async def add_station_region(callback: CallbackQuery, state: FSMContext):
    region = callback.data.split(":", 1)[1]
    data = await state.get_data()
    add_station(data["name"], data["address"], region)
    await state.clear()
    await callback.message.edit_text(
        f"АЗС добавлена ✅ ({region})\nКоординаты пока не заданы — «рядом со мной» её не увидит, "
        f"пока админ не добавит lat/lon вручную в базу."
    )
    await callback.answer()


# ---------- Подписки и оплата ----------

@router.message(Command("mysubs"))
async def cmd_mysubs(message: Message):
    subs = get_user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("У тебя нет активных подписок. Оформить можно через карточку станции (кнопка «🔔 Подписаться»).")
        return
    lines = []
    for s in subs:
        days_left = (s["expires_at"] - int(time.time())) // 86400
        lines.append(f"🔔 {s['name']}, {s['address']} — ещё {days_left} дн.")
    await message.answer("\n".join(lines))


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, state: FSMContext):
    if not PAYMENT_PROVIDER_TOKEN:
        await message.answer(
            "Оплата пока не подключена. Владельцу бота нужно привязать платёжного "
            "провайдера через @BotFather → Payments и добавить PAYMENT_PROVIDER_TOKEN."
        )
        return
    await state.set_state(SubscribeFlow.choosing_station)
    await message.answer(
        f"Выбери АЗС, за которой следить.\nПодписка: {SUB_PRICE_RUB} ₽ / {SUB_DAYS} дней.",
        reply_markup=stations_keyboard(prefix="sub_station"),
    )


@router.callback_query(SubscribeFlow.choosing_station, F.data.startswith("sub_station:"))
async def choose_sub_station(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    await state.clear()
    await callback.answer()
    await send_subscription_invoice(callback.bot, callback.message.chat.id, callback.from_user.id, station_id)


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    _, station_id_str, user_id_str = payload.split(":")
    station_id = int(station_id_str)
    user_id = int(user_id_str)
    add_subscription(user_id, station_id, days=SUB_DAYS)
    station = get_station(station_id)
    await message.answer(
        f"Оплата прошла ✅ Подписка на «{station['name']}, {station['address']}» активна {SUB_DAYS} дней."
    )


async def notify_subscribers(station_id, status, fuels):
    if bot_instance is None:
        return
    subs = get_active_subscriptions_for_station(station_id)
    if not subs:
        return
    station = get_station(station_id)
    fuels_text = f" ({', '.join(fuels)})" if fuels else ""
    text = f"⛽ На «{station['name']}, {station['address']}» появилось топливо{fuels_text}!\nСтатус: {STATUS_LABELS[status]}"
    for sub in subs:
        try:
            await bot_instance.send_message(sub["user_id"], text)
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление {sub['user_id']}: {e}")


# ---------- Статистика ----------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    st = get_overall_stats()
    if st["fresh_total"] == 0:
        await message.answer(f"Пока мало свежих отчётов по городу.\nВсего станций в базе: {st['total_stations']}.\nБудь первым — /report")
        return
    pct = round(st["fresh_good"] / st["fresh_total"] * 100)
    bar_len = 10
    filled = round(pct / 100 * bar_len)
    bar = "🟩" * filled + "⬜️" * (bar_len - filled)
    await message.answer(
        f"📊 Обстановка по {CITY_NAME}\n\n{bar} {pct}%\n\n"
        f"Станций с топливом (по свежим отчётам): {st['fresh_good']} из {st['fresh_total']}\n"
        f"Отчётов за сутки: {st['reports_today']}\n"
        f"Всего станций в базе: {st['total_stations']}"
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await message.answer("Использование: /broadcast текст сообщения")
        return
    user_ids = get_all_user_ids()
    sent = 0
    for uid in user_ids:
        try:
            await message.bot.send_message(uid, f"📢 {text}")
            sent += 1
        except Exception as e:
            logger.warning(f"Не удалось отправить {uid}: {e}")
    await message.answer(f"Разослано {sent} из {len(user_ids)} пользователям")


# ---------- Админ-панель ----------

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🕓 Последние изменения", callback_data="admin_recent")],
        [InlineKeyboardButton(text="📍 Список АЗС (ID)", callback_data="admin_stations")],
    ])
    await message.answer("⚙️ Админ-панель", reply_markup=kb)


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    await cmd_stats(callback.message)
    await callback.answer()


@router.callback_query(F.data == "admin_recent")
async def admin_recent(callback: CallbackQuery):
    rows = get_recent_changes(15)
    if not rows:
        await callback.message.answer("Пока нет изменений.")
        await callback.answer()
        return
    lines = ["🕓 Последние изменения:\n"]
    for r in rows:
        who = r["user_name"] if r["user_name"] else (r["user_id"] or "?")
        lines.append(f"[{r['kind']}] {r['name']}, {r['address']} — {r['info']} · {who} · {time_ago(r['ts'])}")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin_stations")
async def admin_stations(callback: CallbackQuery):
    stations = get_stations()
    lines = [f"#{s['id']}: {s['name']}, {s['address']} ({s['region']})" for s in stations]
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа")
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Использование: /setprice <station_id> <92|95|98|dt|gas> <цена или 'нет'>")
        return
    _, station_id_str, fuel_code, price_str = parts
    try:
        station_id = int(station_id_str)
    except ValueError:
        await message.answer("station_id должен быть числом")
        return
    if fuel_code not in FUEL_INFO:
        await message.answer(f"Неизвестный вид топлива. Варианты: {', '.join(FUEL_INFO.keys())}")
        return
    if price_str.lower() in ("нет", "-"):
        add_fuel_price(station_id, fuel_code, None, False, message.from_user.id, "админ")
        await message.answer(f"Готово: {FUEL_INFO[fuel_code]} на #{station_id} отмечено как отсутствующее.")
        return
    try:
        price = float(price_str.replace(",", "."))
    except ValueError:
        await message.answer("Цена должна быть числом.")
        return
    add_fuel_price(station_id, fuel_code, price, True, message.from_user.id, "админ")
    await message.answer(f"Готово: {FUEL_INFO[fuel_code]} на #{station_id} = {price:.2f}₽")


# ---------- Запуск ----------

async def main():
    global bot_instance
    init_db()
    bot_instance = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот запущен")
    await dp.start_polling(bot_instance)


if __name__ == "__main__":
    asyncio.run(main())
