"""Бот пекарни "Буханка" (концепт): открывает мини-апп и принимает заказы из него.

Настройки только из переменных окружения (или файла .env рядом с ботом):
    BOT_TOKEN      - токен от @BotFather
    ADMIN_CHAT_ID  - чат, куда дублируются заказы и ошибки (ваш user id или id группы)
    WEBAPP_URL     - https-адрес мини-аппа (например, GitHub Pages)
Необязательные:
    MENU_FILE      - меню, по умолчанию ../webapp/menu.json
    BAKERIES_FILE  - пекарни, по умолчанию ../webapp/bakeries.js
    LOG_DIR        - логи, по умолчанию bot/logs
    DATA_DIR       - база заказов, по умолчанию bot/data
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import ErrorEvent, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

from order import (DEFAULT_BAKERIES_FILE, DEFAULT_MENU_FILE, Bakery, Menu, Order, OrderError,
                   fingerprint, load_bakeries, load_menu, parse_order)
from storage import Storage

BOT_DIR = Path(__file__).resolve().parent
load_dotenv(BOT_DIR / ".env")

log = logging.getLogger("buhanka")

MENU_BUTTON_TEXT = "🥐 Открыть меню"

# Антиспам
RATE_LIMIT_ORDERS = 3
RATE_LIMIT_WINDOW = 10 * 60   # секунд
DUPLICATE_WINDOW = 30         # одинаковый заказ за это время считается двойным нажатием
ERROR_NOTIFY_INTERVAL = 5 * 60


# ---------- Конфиг и логи ----------

@dataclass(frozen=True)
class Config:
    token: str
    admin_chat_id: int | None
    webapp_url: str
    menu_file: Path
    bakeries_file: Path
    log_dir: Path
    data_dir: Path


def _path_env(name: str, default: Path) -> Path:
    return Path(os.getenv(name, "").strip() or default)


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    webapp_url = os.getenv("WEBAPP_URL", "").strip()
    admin_raw = os.getenv("ADMIN_CHAT_ID", "").strip()

    errors = []
    if not token:
        errors.append("BOT_TOKEN не задан")
    if not webapp_url.startswith("https://"):
        errors.append("WEBAPP_URL должен начинаться с https:// (Telegram открывает мини-аппы только по https)")
    if errors:
        for e in errors:
            log.error(e)
        sys.exit("Заполните переменные окружения, см. bot/.env.example")

    admin_chat_id = None
    if admin_raw:
        try:
            admin_chat_id = int(admin_raw)
        except ValueError:
            log.warning("ADMIN_CHAT_ID=%r не число - заказы и ошибки не будут дублироваться админу", admin_raw)
    else:
        log.warning("ADMIN_CHAT_ID не задан - заказы и ошибки не будут дублироваться админу")

    return Config(
        token=token,
        admin_chat_id=admin_chat_id,
        webapp_url=webapp_url,
        menu_file=_path_env("MENU_FILE", DEFAULT_MENU_FILE),
        bakeries_file=_path_env("BAKERIES_FILE", DEFAULT_BAKERIES_FILE),
        log_dir=_path_env("LOG_DIR", BOT_DIR / "logs"),
        data_dir=_path_env("DATA_DIR", BOT_DIR / "data"),
    )


def setup_logging(log_dir: Path | None) -> None:
    """Логи в консоль и, если задана папка, в log_dir/bot.log: 5 файлов по 1 МБ."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file = RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000, backupCount=4, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)


# ---------- Тексты ----------

def money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def pickup_label(order: Order, now: datetime) -> str:
    delta = (order.pickup.date() - now.astimezone(order.pickup.tzinfo).date()).days
    day = {0: "сегодня", 1: "завтра"}.get(delta, order.pickup.strftime("%d.%m"))
    return f"{day}, {order.pickup:%H:%M}"


def items_block(order: Order) -> str:
    lines = []
    for line in order.lines:
        text = f"• {escape(line.title)} × {line.qty} - {money(line.sum)}"
        if line.options:
            text += f"\n   <i>{escape(line.options)}</i>"
        lines.append(text)
    return "\n".join(lines)


def receipt_text(order: Order, number: int, now: datetime) -> str:
    parts = [
        f"🧾 <b>Заказ №{number} принят</b>",
        "",
        items_block(order),
        "",
        f"<b>Итого: {money(order.total)}</b>",
        f"📍 Где забрать: <b>{escape(order.bakery.address)}</b>",
        f"🕒 Самовывоз: <b>{pickup_label(order, now)}</b>",
        f"👤 Имя: {escape(order.name)}",
    ]
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    parts += ["", "Оплата при получении. Покажите этот чек на кассе.",
              "<i>Концепт. Не является официальным сервисом пекарни Буханка.</i>"]
    return "\n".join(parts)


def admin_text(order: Order, number: int, message: Message, now: datetime) -> str:
    user = message.from_user
    who = escape(order.name)
    if user:
        link = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
        who += f" ({link}" + (f", @{escape(user.username)}" if user.username else "") + f", id {user.id})"
    parts = [
        f"🔔 <b>Новый заказ №{number}</b>",
        f"📍 {escape(order.bakery.address)} <i>({escape(order.bakery.city)}, {escape(order.bakery.id)})</i>",
        f"🕒 Самовывоз: <b>{pickup_label(order, now)}</b>",
        f"👤 {who}",
        "",
        items_block(order),
        "",
        f"<b>Итого: {money(order.total)}</b>",
    ]
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    return "\n".join(parts)


def format_uptime(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{days} д " if days else "") + f"{hours} ч {minutes} мин"


# ---------- Защита от спама и шума ----------

class DuplicateGuard:
    """Помнит, какие заказы пришли за последние DUPLICATE_WINDOW секунд (двойное нажатие)."""

    def __init__(self, window: float = DUPLICATE_WINDOW):
        self.window = window
        self.seen: dict[tuple[int, str], float] = {}

    def is_duplicate(self, user_id: int, raw: str) -> bool:
        now = time.monotonic()
        self.seen = {k: t for k, t in self.seen.items() if now - t < self.window}
        key = (user_id, fingerprint(raw))
        if key in self.seen:
            return True
        self.seen[key] = now
        return False


class ErrorNotifier:
    """Короткое сообщение админу об ошибке, не чаще раза в ERROR_NOTIFY_INTERVAL."""

    def __init__(self, admin_chat_id: int | None, interval: float = ERROR_NOTIFY_INTERVAL):
        self.admin_chat_id = admin_chat_id
        self.interval = interval
        self.last_sent = -interval
        self.suppressed = 0
        self.total = 0

    async def notify(self, bot: Bot, error: BaseException) -> None:
        self.total += 1
        if self.admin_chat_id is None:
            return
        now = time.monotonic()
        if now - self.last_sent < self.interval:
            self.suppressed += 1
            return
        text = f"⚠️ Ошибка в боте: <code>{escape(type(error).__name__)}: {escape(str(error)[:200])}</code>"
        if self.suppressed:
            text += f"\nЕщё ошибок с прошлого сообщения: {self.suppressed}"
        text += "\nПодробности в logs/bot.log"
        try:
            await bot.send_message(self.admin_chat_id, text)
            self.last_sent, self.suppressed = now, 0
        except Exception:
            log.exception("Не удалось отправить админу сообщение об ошибке")


class IsAdmin(Filter):
    def __init__(self, admin_chat_id: int | None):
        self.admin_chat_id = admin_chat_id

    async def __call__(self, message: Message) -> bool:
        if self.admin_chat_id is None:
            return False
        return message.chat.id == self.admin_chat_id or bool(message.from_user and message.from_user.id == self.admin_chat_id)


# ---------- Хендлеры ----------

def build_router(config: Config, menu: Menu, bakeries: dict[str, Bakery], storage: Storage,
                 notifier: ErrorNotifier, started_at: float) -> Router:
    router = Router()
    duplicates = DuplicateGuard()
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=config.webapp_url))]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Нажмите кнопку, чтобы открыть меню",
    )

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        name = escape(message.from_user.first_name) if message.from_user else "друг"
        await message.answer(
            f"Привет, {name}! 👋\n\n"
            "Это <b>Буханка</b> - горячая выпечка, кофе и завтраки с самовывозом ко времени.\n\n"
            f"Нажмите <b>{MENU_BUTTON_TEXT}</b> внизу, соберите заказ и выберите, когда заберёте. "
            "Чек придёт сюда, в чат.\n\n"
            "<i>Концепт. Не является официальным сервисом пекарни Буханка.</i>",
            reply_markup=keyboard,
        )

    @router.message(Command("health"), IsAdmin(config.admin_chat_id))
    async def health(message: Message) -> None:
        now = datetime.now(menu.tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_count, today_sum = storage.count_since(midnight.timestamp())
        await message.answer(
            "🩺 <b>Бот работает</b>\n"
            f"Аптайм: {format_uptime(time.monotonic() - started_at)}\n"
            f"Заказов сегодня: {today_count} на {money(today_sum)}\n"
            f"Последний номер: {storage.last_number() or 'заказов ещё не было'}\n"
            f"Меню: {len(menu.items)} позиций, версия {escape(menu.version)}\n"
            f"Пекарен: {len(bakeries)}\n"
            f"Ошибок с запуска: {notifier.total}"
        )

    @router.message(F.web_app_data)
    async def web_app_order(message: Message, bot: Bot) -> None:
        raw = message.web_app_data.data
        user_id = message.from_user.id if message.from_user else message.chat.id

        if duplicates.is_duplicate(user_id, raw):
            log.info("Повтор заказа от %s в течение %s с, пропускаю", user_id, DUPLICATE_WINDOW)
            return

        recent = storage.user_orders_since(user_id, time.time() - RATE_LIMIT_WINDOW)
        if len(recent) >= RATE_LIMIT_ORDERS:
            wait_min = max(1, round((recent[0] + RATE_LIMIT_WINDOW - time.time()) / 60))
            log.warning("Лимит заказов для %s: %d за %d мин", user_id, len(recent), RATE_LIMIT_WINDOW // 60)
            await message.answer(
                f"🙏 Вы уже оформили {len(recent)} заказа за последние 10 минут. "
                f"Новый можно будет отправить примерно через {wait_min} мин. Спасибо за понимание!",
                reply_markup=keyboard,
            )
            return

        now = datetime.now(timezone.utc)
        try:
            order = parse_order(raw, menu, bakeries, now)
        except OrderError as e:
            log.warning("Заказ от %s не принят: %s; data=%.300r", user_id, e, raw)
            await message.answer(e.user_message, reply_markup=keyboard)
            return

        number = storage.add_order(user_id, order.total, order.bakery.id, raw)
        log.info("Заказ №%s от %s на %s, %s", number, user_id, order.total, order.bakery.id)
        await message.answer(receipt_text(order, number, now), reply_markup=keyboard)

        if config.admin_chat_id is not None:
            try:
                await bot.send_message(config.admin_chat_id, admin_text(order, number, message, now))
            except Exception:
                # Клиент уже получил чек, ошибка уведомления админа не должна это ломать
                log.exception("Не удалось отправить заказ №%s админу %s", number, config.admin_chat_id)

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer(f"Чтобы сделать заказ, нажмите <b>{MENU_BUTTON_TEXT}</b> внизу экрана.",
                             reply_markup=keyboard)

    return router


async def main() -> None:
    config = load_config()
    setup_logging(config.log_dir)
    try:
        menu = load_menu(config.menu_file)
        bakeries = load_bakeries(config.bakeries_file)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.error("Не удалось загрузить меню или пекарни: %s", e)
        sys.exit(1)
    storage = Storage(config.data_dir / "orders.db")
    log.info("Меню %s: %d позиций, пекарен: %d, последний заказ: %s",
             menu.version, len(menu.items), len(bakeries), storage.last_number())

    started_at = time.monotonic()
    notifier = ErrorNotifier(config.admin_chat_id)
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(config, menu, bakeries, storage, notifier, started_at))

    @dp.errors()
    async def on_error(event: ErrorEvent, bot: Bot) -> bool:
        # Любая ошибка в хендлере логируется, бот продолжает работать, админ получает короткое сообщение
        log.error("Ошибка при обработке апдейта %s", event.update.update_id, exc_info=event.exception)
        await notifier.notify(bot, event.exception)
        message = event.update.message
        if message is not None:
            try:
                await message.answer("😕 Что-то пошло не так. Попробуйте ещё раз чуть позже.")
            except Exception:
                log.exception("Не удалось ответить гостю после ошибки")
        return True

    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            log.error("Telegram не принял BOT_TOKEN: проверьте токен в .env (его выдаёт @BotFather)")
            sys.exit(1)
        log.info("Бот @%s запущен, мини-апп: %s", me.username, config.webapp_url)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        storage.close()
        await bot.session.close()


if __name__ == "__main__":
    setup_logging(None)  # до чтения конфига ошибки пишутся только в консоль
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
