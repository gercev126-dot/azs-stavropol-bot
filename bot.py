# Топливо 26 v2
import asyncio
import logging
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
# Токен платёжного провайдера. В России подключается через @BotFather -> Payments -> ЮKassa (или другой
# провайдер, поддерживающий Telegram Payments в РФ). Без него /subscribe работать не будет.
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "fuel.db")
STALE_SECONDS = 6 * 60 * 60  # отчёт считается свежим 6 часов
REPORT_COOLDOWN_SECONDS = 5 * 60  # не чаще раза в 5 минут на одну АЗС от одного человека
CITY_NAME = "Ставрополь"
SUB_PRICE_RUB = 149  # цена подписки
SUB_DAYS = 30

STATUS_LABELS = {
    "available": "🟢 Есть",
    "limited": "🟡 Мало / лимит",
    "queue": "🟠 Очередь",
    "none": "🔴 Нет",
}
GOOD_STATUSES = {"available", "limited"}  # статусы, при которых стоит уведомлять
FUEL_TYPES = ["92", "95", "ДТ"]

SEED_STATIONS = [
    # (name, address, region)
    ("Лукойл", "ул. Доваторцев, 38 корп. 2", "Ставрополь"),
    ("Лукойл", "просп. Кулакова, 7/2", "Ставрополь"),
    ("Лукойл", "ул. Коломийцева, 27", "Ставрополь"),
    ("Роснефть", "ул. Ленина, 431а/1", "Ставрополь"),
    ("Роснефть", "ул. 8 Марта, 139", "Ставрополь"),
    ("Роснефть", "ул. Бабушкина, 1В", "Ставрополь"),
    ("Газпром", "ул. Пригородная, 249", "Ставрополь"),
    ("Газпром", "просп. Кулакова, 6Б", "Ставрополь"),
    ("Astra", "ул. Доваторцев, 52Б", "Ставрополь"),
    # Михайловск и округа (Шпаковский округ)
    ("Роснефть", "г. Михайловск, ул. Почтовая, 2", "Михайловск и округа"),
    ("Лукойл", "г. Михайловск, ул. Привокзальная, 1", "Михайловск и округа"),
    ("Роснефть", "г. Михайловск, ул. Гагарина, 1/1", "Михайловск и округа"),
    ("Роснефть", "с. Надежда, ул. Мира, 139", "Михайловск и округа"),
    ("Роснефть", "трасса А154 (Астрахань — Элиста — Ставрополь), 561 км", "Михайловск и округа"),
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
            region TEXT NOT NULL DEFAULT 'Ставрополь'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            fuels TEXT,
            note TEXT,
            ts INTEGER NOT NULL,
            user_id INTEGER,
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
            "INSERT INTO stations (name, address, region) VALUES (?, ?, ?)", SEED_STATIONS
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


def get_latest_report(station_id):
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    conn.close()
    return row


def add_station(name, address, region="Ставрополь"):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO stations (name, address, region) VALUES (?, ?, ?)", (name, address, region)
    )
    conn.commit()
    station_id = cur.lastrowid
    conn.close()
    return station_id


def add_report(station_id, status, fuels, note, user_id):
    conn = db_connect()
    # смотрим предыдущий статус ДО вставки нового отчёта — чтобы понять, это "новое появление бензина" или нет
    prev = conn.execute(
        "SELECT * FROM reports WHERE station_id = ? ORDER BY ts DESC LIMIT 1",
        (station_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO reports (station_id, status, fuels, note, ts, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        (station_id, status, ",".join(fuels), note, int(time.time()), user_id),
    )
    conn.commit()
    conn.close()
    return prev


def time_ago(ts):
    diff = int(time.time()) - ts
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин назад"
    if diff < 86400:
        return f"{diff // 3600} ч назад"
    return f"{diff // 86400} дн назад"


def yandex_maps_link(name, address):
    query = f"{name} {address} {CITY_NAME}"
    return f"https://yandex.ru/maps/?text={quote(query)}"


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
    """Сколько РАЗНЫХ людей за последнее свежее время подтвердили один и тот же статус подряд."""
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
        conn.execute(
            "UPDATE subscriptions SET expires_at = ? WHERE id = ?",
            (new_expiry, existing["id"]),
        )
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
    typing_note = State()


class AddStationFlow(StatesGroup):
    typing_name = State()
    typing_address = State()
    choosing_region = State()


class SubscribeFlow(StatesGroup):
    choosing_station = State()


router = Router()
bot_instance: Bot | None = None  # заполняется в main(), нужен для отправки уведомлений


@router.message.middleware()
async def register_user_middleware(handler, event: Message, data):
    if event.from_user:
        register_user(event.from_user.id)
    return await handler(event, data)

# ---------- Клавиатуры ----------

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
    kb = [
        [InlineKeyboardButton(text=label, callback_data=f"status:{key}")]
        for key, label in STATUS_LABELS.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def fuels_keyboard(selected):
    row = []
    for f in FUEL_TYPES:
        mark = "✅ " if f in selected else ""
        row.append(InlineKeyboardButton(text=f"{mark}{f}", callback_data=f"fuel:{f}"))
    kb = [row, [InlineKeyboardButton(text="Готово ➡️", callback_data="fuel:done")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


# ---------- Хендлеры: базовые ----------

@router.message(CommandStart())
async def cmd_start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗺 Где есть топливо")],
            [KeyboardButton(text="📍 Сообщить наличие"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="📤 Поделиться")],
            [KeyboardButton(text="ℹ️ О проекте")],
        ],
        resize_keyboard=True
    )

    await message.answer(
        "⛽ Топливо 26\n\n"
        "Народная карта топлива Ставропольского края.\n"
        "Помогаем водителям находить бензин, газ и дизель.\n\n"
        "Чем больше людей отправляют данные — тем точнее карта.\n\n"
        "📊 «Статистика» покажет общую картину по городу за секунду.",
        reply_markup=keyboard
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

@router.message(F.text == "📤 Поделиться")
async def menu_share(message: Message):
    bot_link = f"https://t.me/{BOT_USERNAME}"
    share_text = "Бот показывает, где в Ставрополе и области есть бензин прямо сейчас 👇"
    share_url = f"https://t.me/share/url?url={quote(bot_link)}&text={quote(share_text)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить другу или в чат", url=share_url)]
    ])
    await message.answer(
        "Перешли бота знакомым — чем больше людей отмечают станции, тем точнее карта для всех.",
        reply_markup=kb,
    )

@router.message(F.text == "ℹ️ О проекте")
async def menu_about(message: Message):
    await message.answer(
        "⛽ Топливо 26 — народная карта топлива.\n\n"
        "Пользователи помогают друг другу узнавать, где есть бензин, газ и дизель."
    )

@router.message(Command("list"))
async def cmd_list(message: Message):
    stations = get_stations()
    if not stations:
        await message.answer("Пока нет станций в базе.")
        return
    lines = []
    kb_rows = []
    current_region = None
    for s in stations:
        if s["region"] != current_region:
            current_region = s["region"]
            lines.append(f"\n📍 *{current_region}*")
        latest = get_latest_report(s["id"])
        map_link = yandex_maps_link(s["name"], s["address"])
        if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS:
            label = STATUS_LABELS[latest["status"]]
            fuels = f" [{latest['fuels']}]" if latest["fuels"] else ""
            confirm_count = get_confirmation_count(s["id"])
            confirm_text = f" · подтверждено {confirm_count} чел." if confirm_count > 1 else ""
            lines.append(
                f"{label}{fuels} — {s['name']}, [{s['address']}]({map_link}) · {time_ago(latest['ts'])}{confirm_text}"
            )
            icon = label.split()[0]
        else:
            lines.append(f"⚪️ Нет свежих данных — {s['name']}, [{s['address']}]({map_link})")
            icon = "⚪️"
        kb_rows.append([InlineKeyboardButton(
            text=f"{icon} Обновить: {s['name']} ({s['region']})", callback_data=f"quick_report:{s['id']}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer(
        "\n".join(lines).strip() + "\n\nЖми на станцию ниже, чтобы быстро обновить статус:",
        reply_markup=kb,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


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

    # свежих данных нет — сразу к выбору статуса
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
    add_report(station_id, latest["status"], fuels, "Подтверждено", callback.from_user.id)
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


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    st = get_overall_stats()
    if st["fresh_total"] == 0:
        await message.answer(
            f"Пока мало свежих отчётов по городу.\n"
            f"Всего станций в базе: {st['total_stations']}.\n"
            f"Будь первым — /report"
        )
        return
    pct = round(st["fresh_good"] / st["fresh_total"] * 100)
    bar_len = 10
    filled = round(pct / 100 * bar_len)
    bar = "🟩" * filled + "⬜️" * (bar_len - filled)
    await message.answer(
        f"📊 Обстановка по {CITY_NAME}\n\n"
        f"{bar} {pct}%\n\n"
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
        await state.set_state(ReportFlow.typing_note)
        await callback.message.edit_text(
            "Комментарий? (например: лимит 30 л, подвезли утром)\n"
            "Если нечего добавить, напиши «-»"
        )
        await callback.answer()
        return

    if value in fuels:
        fuels.remove(value)
    else:
        fuels.append(value)
    await state.update_data(fuels=fuels)
    await callback.message.edit_reply_markup(reply_markup=fuels_keyboard(fuels))
    await callback.answer()


@router.message(ReportFlow.typing_note)
async def type_note(message: Message, state: FSMContext):
    note = "" if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    station_id = data["station_id"]
    new_status = data["status"]
    fuels = data.get("fuels", [])

    prev = add_report(
        station_id=station_id,
        status=new_status,
        fuels=fuels,
        note=note,
        user_id=message.from_user.id,
    )
    await state.clear()
    await message.answer("Спасибо! Отчёт сохранён и виден всем в /list ✅")

    # Уведомляем подписчиков, если бензин ПОЯВИЛСЯ (раньше не было / было "нет"/"очередь", стало "есть"/"мало")
    became_good = new_status in GOOD_STATUSES and (prev is None or prev["status"] not in GOOD_STATUSES)
    if became_good:
        await notify_subscribers(station_id, new_status, fuels)


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
    await callback.message.edit_text(f"АЗС добавлена ✅ ({region}) Теперь она доступна в /report и /list")
    await callback.answer()


# ---------- Хендлеры: подписка и оплата ----------

@router.message(Command("mysubs"))
async def cmd_mysubs(message: Message):
    subs = get_user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("У тебя нет активных подписок. Оформить: /subscribe")
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
            "провайдера через @BotFather → Payments (например, ЮKassa), "
            "и добавить PAYMENT_PROVIDER_TOKEN в переменные окружения."
        )
        return
    await state.set_state(SubscribeFlow.choosing_station)
    await message.answer(
        f"Выбери АЗС, за которой следить.\n"
        f"Подписка: {SUB_PRICE_RUB} ₽ / {SUB_DAYS} дней — уведомлю, как только там появится бензин.",
        reply_markup=stations_keyboard(prefix="sub_station"),
    )


@router.callback_query(SubscribeFlow.choosing_station, F.data.startswith("sub_station:"))
async def choose_sub_station(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
    station = get_station(station_id)
    await state.clear()
    await callback.answer()

    prices = [LabeledPrice(label=f"Подписка на {SUB_DAYS} дней", amount=SUB_PRICE_RUB * 100)]
    await callback.message.answer_invoice(
        title=f"Уведомления: {station['name']}, {station['address']}",
        description=f"Пришлю сообщение, как только на этой АЗС появится бензин. Действует {SUB_DAYS} дней.",
        payload=f"sub:{station_id}:{callback.from_user.id}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
    )


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    _, station_id_str, user_id_str = payload.split(":")
    station_id = int(station_id_str)
    user_id = int(user_id_str)
    expiry = add_subscription(user_id, station_id, days=SUB_DAYS)
    station = get_station(station_id)
    await message.answer(
        f"Оплата прошла ✅ Подписка на «{station['name']}, {station['address']}» "
        f"активна {SUB_DAYS} дней. Как только там появится бензин — пришлю сообщение."
    )


async def notify_subscribers(station_id, status, fuels):
    if bot_instance is None:
        return
    subs = get_active_subscriptions_for_station(station_id)
    if not subs:
        return
    station = get_station(station_id)
    fuels_text = f" ({', '.join(fuels)})" if fuels else ""
    text = (
        f"⛽ На «{station['name']}, {station['address']}» появился бензин{fuels_text}!\n"
        f"Статус: {STATUS_LABELS[status]}"
    )
    for sub in subs:
        try:
            await bot_instance.send_message(sub["user_id"], text)
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление {sub['user_id']}: {e}")


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
