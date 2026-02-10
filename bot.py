import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# =========================
#  НАСТРОЙКИ (первый релиз)
# =========================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Добавьте переменную окружения BOT_TOKEN со значением токена BotFather.")

TIMEZONE = os.getenv("BOT_TZ", "Europe/Berlin")
PING_HOUR = int(os.getenv("BOT_PING_HOUR", "19"))
PING_MINUTE = int(os.getenv("BOT_PING_MINUTE", "0"))
DB_PATH = os.getenv("BOT_DB_PATH", "eco_tracker.db")

# Доступные классы в первом релизе
CLASSES = ["6В", "6Г"]

# Эко-привычки (ежедневные)
# key должен быть коротким латинским идентификатором
HABITS = [
    ("water_teeth", "🚰 Выключаю воду при чистке зубов"),
    ("lights_off", "💡 Выключаю свет, выходя из комнаты"),
    ("no_cup", "🥤 Не пью из одноразового стаканчика"),
    ("no_bag", "🛍️ Не использую пластиковый пакет"),
    ("trash_place", "🗑️ Мусор - в отведённые места"),
    ("eco_move", "🚶 Пешком/экологичный транспорт"),
]

# =========================
#  ЛОГИРОВАНИЕ
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eco_bot")

# =========================
#  БАЗА ДАННЫХ (SQLite)
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                class_name TEXT,
                joined_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                habit_key TEXT NOT NULL,
                PRIMARY KEY (user_id, day, habit_key),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

def upsert_user(user_id: int, username: str | None, first_name: str | None):
    with db() as conn:
        conn.execute("""
            INSERT INTO users(user_id, username, first_name, class_name, joined_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user_id, username, first_name, None, datetime.now().isoformat()))

def set_user_class(user_id: int, class_name: str):
    with db() as conn:
        conn.execute("UPDATE users SET class_name=? WHERE user_id=?", (class_name, user_id))

def get_user_class(user_id: int) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT class_name FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else None

def get_all_users():
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [r[0] for r in rows]

def get_class_users(class_name: str):
    with db() as conn:
        rows = conn.execute("SELECT user_id FROM users WHERE class_name=?", (class_name,)).fetchall()
        return [r[0] for r in rows]

def set_habit(user_id: int, day: str, habit_key: str, enabled: bool):
    with db() as conn:
        if enabled:
            conn.execute("""
                INSERT OR IGNORE INTO checkins(user_id, day, habit_key)
                VALUES(?,?,?)
            """, (user_id, day, habit_key))
        else:
            conn.execute("""
                DELETE FROM checkins WHERE user_id=? AND day=? AND habit_key=?
            """, (user_id, day, habit_key))

def get_user_day_habits(user_id: int, day: str) -> set[str]:
    with db() as conn:
        rows = conn.execute("""
            SELECT habit_key FROM checkins WHERE user_id=? AND day=?
        """, (user_id, day)).fetchall()
        return {r[0] for r in rows}

def get_user_stats(user_id: int):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM checkins WHERE user_id=?", (user_id,)).fetchone()[0]
        days = conn.execute("SELECT COUNT(DISTINCT day) FROM checkins WHERE user_id=?", (user_id,)).fetchone()[0]
        by_habit = conn.execute("""
            SELECT habit_key, COUNT(*) as c FROM checkins
            WHERE user_id=?
            GROUP BY habit_key
            ORDER BY c DESC
        """, (user_id,)).fetchall()
        return total, days, by_habit

def get_group_stats(where_sql: str = "", params: tuple = ()):
    """
    Возвращает:
    - users_count: сколько участников (у кого есть class_name если нужно)
    - total_actions: всего отметок
    - by_habit: топ привычек
    - days_count: сколько уникальных дней активности
    """
    with db() as conn:
        users_count = conn.execute(f"""
            SELECT COUNT(*) FROM users
            {where_sql}
        """, params).fetchone()[0]

        total_actions = conn.execute(f"""
            SELECT COUNT(*) FROM checkins
            JOIN users ON users.user_id = checkins.user_id
            {where_sql}
        """, params).fetchone()[0]

        days_count = conn.execute(f"""
            SELECT COUNT(DISTINCT checkins.day) FROM checkins
            JOIN users ON users.user_id = checkins.user_id
            {where_sql}
        """, params).fetchone()[0]

        by_habit = conn.execute(f"""
            SELECT checkins.habit_key, COUNT(*) as c
            FROM checkins
            JOIN users ON users.user_id = checkins.user_id
            {where_sql}
            GROUP BY checkins.habit_key
            ORDER BY c DESC
        """, params).fetchall()

        return users_count, total_actions, days_count, by_habit

# =========================
#  КНОПКИ
# =========================
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Отметить сегодня")],
            [KeyboardButton(text="📊 Моя статистика"), KeyboardButton(text="🙋 Статистика класса")],
            [KeyboardButton(text="🏫 Статистика школы")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие…",
    )

def class_pick_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c, callback_data=f"class:{c}") for c in CLASSES]
    ])

def habits_kb(user_id: int, day_str: str) -> InlineKeyboardMarkup:
    selected = get_user_day_habits(user_id, day_str)
    rows = []
    for key, label in HABITS:
        mark = "✅ " if key in selected else "☐ "
        rows.append([InlineKeyboardButton(text=mark + label, callback_data=f"toggle:{day_str}:{key}")])
    rows.append([InlineKeyboardButton(text="📌 Готово", callback_data=f"done:{day_str}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# =========================
#  ВСПОМОГАТЕЛЬНОЕ
# =========================
def habit_label(key: str) -> str:
    for k, lbl in HABITS:
        if k == key:
            return lbl
    return key

def format_top_habits(by_habit, limit=3) -> str:
    if not by_habit:
        return "Пока нет данных."
    lines = []
    for key, cnt in by_habit[:limit]:
        lines.append(f"{habit_label(key)} — {cnt}")
    return "\n".join(lines)

# =========================
#  BOT / DP
# =========================
bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)

    user_class = get_user_class(m.from_user.id)
    if not user_class:
        await m.answer(
            "Привет! Я бот трекера эко-привычек 🌿\n\n"
            "Сначала выберите Ваш класс (это нужно для статистики класса и школы).",
            reply_markup=class_pick_kb(),
        )
    else:
        await m.answer(
            "Привет! Вы уже подключены 🌿\n"
            "Нажмите «✅ Отметить сегодня» или используйте меню.",
            reply_markup=main_menu_kb()
        )

@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "Команды:\n"
        "/start — подключиться\n"
        "/checkin — отметить привычки за сегодня\n"
        "/stats — моя статистика\n"
        "/setclass — поменять класс\n\n"
        "Также можно пользоваться кнопками меню.",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("setclass"))
async def cmd_setclass(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    await m.answer("Выберите класс:", reply_markup=class_pick_kb())

@dp.callback_query(F.data.startswith("class:"))
async def cb_setclass(cb: CallbackQuery):
    _, class_name = cb.data.split(":", 1)
    if class_name not in CLASSES:
        await cb.answer("Такого класса нет в первом релизе.")
        return
    upsert_user(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
    set_user_class(cb.from_user.id, class_name)
    await cb.answer("Класс сохранён!")
    await cb.message.answer(
        f"Готово ✅ Ваш класс: {class_name}\n"
        "Теперь можно отмечать привычки.",
        reply_markup=main_menu_kb()
    )

@dp.message(Command("checkin"))
async def cmd_checkin(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not get_user_class(m.from_user.id):
        await m.answer("Сначала выберите класс:", reply_markup=class_pick_kb())
        return
    day_str = date.today().isoformat()
    await m.answer(
        f"Отметьте эко-действия за сегодня ({day_str}):",
        reply_markup=habits_kb(m.from_user.id, day_str)
    )

@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    upsert_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    await send_my_stats(m)

async def send_my_stats(m: Message):
    total, days, by_habit = get_user_stats(m.from_user.id)
    text = (
        "📊 *Моя статистика*\n"
        f"Активных дней: *{days}*\n"
        f"Всего отметок: *{total}*\n\n"
        "Топ привычек:\n"
        f"{format_top_habits(by_habit, limit=3)}"
    )
    await m.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

async def send_class_stats(m: Message):
    class_name = get_user_class(m.from_user.id)
    if not class_name:
        await m.answer("Сначала выберите класс:", reply_markup=class_pick_kb())
        return
    where = "WHERE users.class_name=?"
    users_count, total_actions, days_count, by_habit = get_group_stats(where, (class_name,))
    text = (
        "🙋 *Статистика класса*\n"
        f"Класс: *{class_name}*\n"
        f"Участников: *{users_count}*\n"
        f"Активных дней (суммарно): *{days_count}*\n"
        f"Всего эко-действий: *{total_actions}*\n\n"
        "Топ привычек:\n"
        f"{format_top_habits(by_habit, limit=3)}"
    )
    await m.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

async def send_school_stats(m: Message):
    # Школа = все участники (оба класса)
    where = ""  # без фильтра
    users_count, total_actions, days_count, by_habit = get_group_stats(where, ())
    # Кто активнее за 7 дней (упрощённо: за последние 7 календарных дней по отметкам)
    today = date.today()
    from_day = (today.toordinal() - 6)
    days_list = [date.fromordinal(from_day + i).isoformat() for i in range(7)]
    with db() as conn:
        rows = conn.execute("""
            SELECT users.class_name, COUNT(*) as c
            FROM checkins
            JOIN users ON users.user_id = checkins.user_id
            WHERE checkins.day IN ({})
            GROUP BY users.class_name
            ORDER BY c DESC
        """.format(",".join("?" for _ in days_list)), tuple(days_list)).fetchall()
    top_class_line = "Нет данных за последние 7 дней."
    if rows and rows[0][0]:
        top_class_line = f"Самый активный класс за 7 дней: *{rows[0][0]}* (отметок: *{rows[0][1]}*)"

    text = (
        "🏫 *Статистика школы*\n"
        f"Участников: *{users_count}*\n"
        f"Активных дней (суммарно): *{days_count}*\n"
        f"Всего эко-действий: *{total_actions}*\n"
        f"{top_class_line}\n\n"
        "Топ привычек:\n"
        f"{format_top_habits(by_habit, limit=3)}"
    )
    await m.answer(text, parse_mode="Markdown", reply_markup=main_menu_kb())

# ======= Меню-кнопки (ReplyKeyboard) =======
@dp.message(F.text == "✅ Отметить сегодня")
async def menu_checkin(m: Message):
    await cmd_checkin(m)

@dp.message(F.text == "📊 Моя статистика")
async def menu_my_stats(m: Message):
    await send_my_stats(m)

@dp.message(F.text == "🙋 Статистика класса")
async def menu_class_stats(m: Message):
    await send_class_stats(m)

@dp.message(F.text == "🏫 Статистика школы")
async def menu_school_stats(m: Message):
    await send_school_stats(m)

# ======= Inline-галочки привычек =======
@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(cb: CallbackQuery):
    _, day_str, key = cb.data.split(":", 2)
    user_id = cb.from_user.id
    selected = get_user_day_habits(user_id, day_str)
    new_state = key not in selected
    set_habit(user_id, day_str, key, new_state)

    # обновляем клавиатуру
    await cb.message.edit_reply_markup(reply_markup=habits_kb(user_id, day_str))
    await cb.answer("Отмечено ✅" if new_state else "Снято ⛔")

@dp.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery):
    _, day_str = cb.data.split(":", 1)
    selected = get_user_day_habits(cb.from_user.id, day_str)
    await cb.answer("Сохранено!")
    await cb.message.answer(
        f"Спасибо! За {day_str} отмечено привычек: {len(selected)} ✅",
        reply_markup=main_menu_kb()
    )

# =========================
#  ЕЖЕДНЕВНЫЙ ВЕЧЕРНИЙ ПИНГ
# =========================
async def evening_ping():
    day_str = date.today().isoformat()
    users = get_all_users()
    for uid in users:
        try:
            # проверим, что пользователь выбрал класс (иначе напомним)
            cls = get_user_class(uid)
            if not cls:
                await bot.send_message(uid, "Выберите класс для участия:", reply_markup=class_pick_kb())
                continue

            await bot.send_message(
                uid,
                f"Вечерний эко-чек-ин 🌙\nОтметьте действия за сегодня ({day_str}):",
                reply_markup=habits_kb(uid, day_str)
            )
        except Exception as e:
            # например, пользователь заблокировал бота
            log.warning("Не удалось отправить сообщение пользователю %s: %s", uid, e)

from aiohttp import web
async def health_server():
    app = web.Application()

    async def ok(_):
        return web.Response(text="OK")

    app.router.add_get("/", ok)

    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    init_db()

    # Порт для Render
    await health_server()

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        evening_ping,
        CronTrigger(hour=PING_HOUR, minute=PING_MINUTE),
        id="evening_ping",
        replace_existing=True,
    )
    scheduler.start()

    log.info("Eco Habits Bot запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
