import asyncio
import logging
import os
import sqlite3
import time

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
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Настройки ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = 5438874849
# Токен платёжного провайдера. В России подключается через @BotFather -> Payments -> ЮKassa (или другой
# провайдер, поддерживающий Telegram Payments в РФ). Без него /subscribe работать не будет.
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "fuel.db")
STALE_SECONDS = 6 * 60 * 60  # отчёт считается свежим 6 часов
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
    ("Лукойл", "ул. Доваторцева"),
    ("Роснефть", "ул. Кулакова"),
    ("Газпромнефть", "ул. Пирогова"),
    ("Astra", "ул. Ленина"),
    ("Teboil", "ул. Голенева"),
    ("Лукойл", "пр-т Юности"),
    ("Роснефть", "Южный обход"),
    ("Газпромнефть", "ул. Мира"),
    ("ТЗК", "ул. Октябрьская"),
    ("Astra", "ул. 50 лет ВЛКСМ"),
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
            address TEXT NOT NULL
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
    count = conn.execute("SELECT COUNT(*) AS c FROM stations").fetchone()["c"]
    if count == 0:
        conn.executemany(
            "INSERT INTO stations (name, address) VALUES (?, ?)", SEED_STATIONS
        )
        conn.commit()
    conn.close()


def get_stations():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM stations ORDER BY name").fetchall()
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


def add_station(name, address):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO stations (name, address) VALUES (?, ?)", (name, address)
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


class SubscribeFlow(StatesGroup):
    choosing_station = State()


router = Router()
bot_instance: Bot | None = None  # заполняется в main(), нужен для отправки уведомлений

# ---------- Клавиатуры ----------

def stations_keyboard(prefix="pick_station"):
    kb = []
    for s in get_stations():
        latest = get_latest_report(s["id"])
        if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS:
            icon = STATUS_LABELS[latest["status"]].split()[0]
        else:
            icon = "⚪️"
        label = f"{icon} {s['name']} — {s['address']}"
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
    await message.answer(
        "Привет! Это народная карта бензина по Ставрополю.\n\n"
        "/report — сообщить, что на АЗС\n"
        "/list — посмотреть текущую обстановку\n"
        "/addstation — добавить новую АЗС в список\n"
        "/subscribe — платное уведомление, когда на выбранной АЗС появится бензин\n"
        "/mysubs — мои активные подписки"
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    stations = get_stations()
    if not stations:
        await message.answer("Пока нет станций в базе.")
        return
    lines = []
    for s in stations:
        latest = get_latest_report(s["id"])
        if latest and (int(time.time()) - latest["ts"]) < STALE_SECONDS:
            label = STATUS_LABELS[latest["status"]]
            fuels = f" [{latest['fuels']}]" if latest["fuels"] else ""
            lines.append(f"{label}{fuels} — {s['name']}, {s['address']} · {time_ago(latest['ts'])}")
        else:
            lines.append(f"⚪️ Нет свежих данных — {s['name']}, {s['address']}")
    await message.answer("\n".join(lines))


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.set_state(ReportFlow.choosing_station)
    await message.answer("Какая АЗС?", reply_markup=stations_keyboard())


@router.callback_query(ReportFlow.choosing_station, F.data.startswith("pick_station:"))
async def pick_station(callback: CallbackQuery, state: FSMContext):
    station_id = int(callback.data.split(":")[1])
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
    data = await state.get_data()
    add_station(data["name"], message.text.strip())
    await state.clear()
    await message.answer("АЗС добавлена ✅ Теперь она доступна в /report и /list")


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
