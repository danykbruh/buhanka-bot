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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import (BotCommand, CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, MenuButtonCommands, Message, ReplyKeyboardMarkup,
                           WebAppInfo)
from dotenv import load_dotenv

from order import (DEFAULT_BAKERIES_FILE, DEFAULT_MENU_FILE, DEFAULT_PROMOS_FILE, WEBAPP_DIR, Bakery, Menu, Order, OrderError,
                   Pricing, Promo, fingerprint, load_bakeries, load_menu, load_promos, parse_order, price_order)
from storage import BonusState, Storage, StoredOrder

BOT_DIR = Path(__file__).resolve().parent
load_dotenv(BOT_DIR / ".env")

log = logging.getLogger("buhanka")

MENU_BUTTON_TEXT = "Открыть меню"
INPUT_PLACEHOLDER = "Нажмите кнопку ниже, чтобы заказать"

# Приветствие на /start: обложка и подпись. Обложка лежит рядом с мини-аппом
COVER_FILE = WEBAPP_DIR / "img" / "cover.png"
WELCOME_CAPTION = (
    "Горячая выпечка и кофе без очереди.\n"
    "1. Откройте меню кнопкой внизу\n"
    "2. Выберите позиции, пекарню и время\n"
    "3. Заберите заказ, он будет готов\n"
    "Концепт, не официальный сервис пекарни Буханка."
)
# Нижняя кнопка ставится отдельным сообщением: у сообщения с inline-кнопками не может быть второй клавиатуры
WELCOME_KEYBOARD_TEXT = f"Кнопка «{MENU_BUTTON_TEXT}» внизу, под полем ввода."
NAV_CALLBACK = "nav"

# Профиль бота: команды, кнопка меню слева от поля ввода и описания
BOT_COMMANDS = [("start", "Открыть меню"), ("orders", "Мои заказы"), ("bonus", "Мои штампы")]
SHORT_DESCRIPTION = "Горячая выпечка и кофе к вашему времени. Концепт."
DESCRIPTION = (
    "Закажите горячую выпечку и кофе с самовывозом ко времени: меню, корзина и выбор пекарни прямо в Telegram.\n"
    "Бот пришлёт чек, сообщит, когда заказ готов, и подарит каждый 6-й кофе.\n"
    "Концепт, не официальный бот пекарни Буханка."
)

# Антиспам
RATE_LIMIT_ORDERS = 3
RATE_LIMIT_WINDOW = 10 * 60   # секунд
DUPLICATE_WINDOW = 30         # одинаковый заказ за это время считается двойным нажатием
ERROR_NOTIFY_INTERVAL = 5 * 60

# Статусы заказа: код в базе -> (значок, подпись). "new" ставится при оформлении, остальные - кнопками админа
STATUSES = {
    "new": ("🆕", "Новый"),
    "accepted": ("✅", "Принят"),
    "cooking": ("👩‍🍳", "Готовится"),
    "ready": ("🥐", "Готов к выдаче"),
    "cancelled": ("❌", "Отменён"),
}
ADMIN_STATUS_BUTTONS = ("accepted", "cooking", "ready", "cancelled")
STATUS_CALLBACK = "st"


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
    promos_file: Path = DEFAULT_PROMOS_FILE


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
        promos_file=_path_env("PROMOS_FILE", DEFAULT_PROMOS_FILE),
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


def pickup_label(when: datetime, now: datetime) -> str:
    delta = (when.date() - now.astimezone(when.tzinfo).date()).days
    day = {0: "сегодня", 1: "завтра"}.get(delta, when.strftime("%d.%m"))
    return f"{day}, {when:%H:%M}"


def items_block(order: Order) -> str:
    lines = []
    for line in order.lines:
        text = f"• {escape(line.title)} × {line.qty} - {money(line.sum)}"
        if line.options:
            text += f"\n   <i>{escape(line.options)}</i>"
        lines.append(text)
    return "\n".join(lines)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def stamps_bar(state: BonusState) -> str:
    """Полоска штампов: 🟤🟤🟤⚪⚪"""
    return "🟤" * state.stamps + "⚪" * (state.per_reward - state.stamps)


def bonus_text(state: BonusState) -> str:
    lines = [
        f"☕ <b>Каждый {state.per_reward + 1}-й кофе в подарок</b>",
        "",
        stamps_bar(state),
        f"У вас {state.stamps} из {state.per_reward} штампов",
    ]
    if state.available:
        lines += ["", f"🎁 Следующий кофе бесплатно" + (f" (подарков: {state.available})" if state.available > 1 else "")
                  + ". Бот применит подарок сам в ближайшем заказе с кофе."]
    else:
        left = state.per_reward - state.stamps
        lines += ["", f"Ещё {left} кофе, и следующий будет бесплатным."]
    lines += ["", "<i>Штамп ставится за каждый оплаченный кофе (американо, капучино, латте, раф), "
                  "когда заказ готов к выдаче. Кофе в комбо не считается.</i>"]
    return "\n".join(lines)


def money_lines(pricing: Pricing) -> list[str]:
    """Строки суммы для чека и копии админу."""
    if not pricing.promo_discount and not pricing.bonus_discount:
        return [f"<b>Итого: {money(pricing.total)}</b>"]
    lines = [f"Сумма: {money(pricing.subtotal)}"]
    if pricing.promo_discount:
        lines.append(f"🏷 Промокод {escape(pricing.promo.code)} ({escape(pricing.promo.title)}): -{money(pricing.promo_discount)}")
    if pricing.bonus_discount:
        lines.append(f"🎁 Кофе в подарок ({escape(pricing.bonus_title)}): -{money(pricing.bonus_discount)}")
    lines.append(f"<b>Итого к оплате: {money(pricing.total)}</b>")
    return lines


def receipt_text(order: Order, pricing: Pricing, number: int, now: datetime) -> str:
    parts = [
        f"🧾 <b>Заказ №{number} оформлен</b>",
        "",
        items_block(order),
        "",
        *money_lines(pricing),
        f"📍 Где забрать: <b>{escape(order.bakery.address)}</b>",
        f"🕒 Самовывоз: <b>{pickup_label(order.pickup, now)}</b>",
        f"👤 Имя: {escape(order.name)}",
    ]
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    if pricing.bonus_title:
        parts += ["", "🎁 Это ваш подарочный кофе за собранные штампы. Спасибо, что берёте кофе у нас!"]
    if pricing.paid_coffee:
        n = pricing.paid_coffee
        parts += ["", f"☕ +{n} {plural(n, 'штамп', 'штампа', 'штампов')} к бонусам, когда заказ будет готов. Проверить: /bonus"]
    parts += ["", "Когда пекарня примет заказ, пришлём сообщение сюда. Все заказы и статусы: /orders",
              "Оплата при получении. Покажите этот чек на кассе.",
              "<i>Концепт. Не является официальным сервисом пекарни Буханка.</i>"]
    return "\n".join(parts)


def admin_text(order: Order, pricing: Pricing, number: int, message: Message, now: datetime) -> str:
    user = message.from_user
    who = escape(order.name)
    if user:
        link = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
        who += f" ({link}" + (f", @{escape(user.username)}" if user.username else "") + f", id {user.id})"
    parts = [
        f"🔔 <b>Новый заказ №{number}</b>",
        f"📍 {escape(order.bakery.address)} <i>({escape(order.bakery.city)}, {escape(order.bakery.id)})</i>",
        f"🕒 Самовывоз: <b>{pickup_label(order.pickup, now)}</b>",
        f"👤 {who}",
        "",
        items_block(order),
        "",
        *money_lines(pricing),
    ]
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    return "\n".join(parts)


def status_label(status: str) -> str:
    emoji, label = STATUSES.get(status, ("•", status))
    return f"{emoji} {label}"


def status_line(stored: StoredOrder, tz: timezone) -> str:
    at = datetime.fromtimestamp(stored.status_at or stored.created_at, tz)
    return f"<b>Статус: {status_label(stored.status)}</b> · {at:%H:%M}"


# Короткие коды статусов для адреса мини-аппа
STATUS_CODES = {"new": "n", "accepted": "a", "cooking": "c", "ready": "r", "cancelled": "x"}


def webapp_url_for(base: str, state: BonusState, orders: list[StoredOrder]) -> str:
    """Адрес мини-аппа со штампами и статусами последних заказов гостя.

    Мини-апп статичный и не может спросить бота, поэтому бот передаёт ему эти данные в адресе кнопки
    "Открыть меню" и обновляет кнопку в каждом своём сообщении. Данные только для показа:
    скидки и проверки по-прежнему считает бот. o = номер.статус.время_оформления.сумма через запятую.
    """
    params = [("st", str(state.stamps)), ("gift", str(state.available))]
    if orders:
        params.append(("o", ",".join(
            f"{o.number}.{STATUS_CODES.get(o.status, 'n')}.{int(o.created_at)}.{o.total}" for o in orders)))
    split = urlsplit(base)
    query = [(k, v) for k, v in parse_qsl(split.query) if k not in ("st", "gift", "o")] + params
    return urlunsplit(split._replace(query=urlencode(query, safe=",.")))


def admin_keyboard(number: int, current: str) -> InlineKeyboardMarkup:
    """Кнопки статуса под копией заказа у админа, текущий статус отмечен точкой."""
    buttons = [
        InlineKeyboardButton(
            text=("• " if code == current else "") + STATUSES[code][1],
            callback_data=f"{STATUS_CALLBACK}:{number}:{code}",
        )
        for code in ADMIN_STATUS_BUTTONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:2], buttons[2:]])


def client_status_text(stored: StoredOrder, bakery: Bakery | None, tz: timezone) -> str:
    n = stored.number
    where = escape(bakery.address) if bakery else "в выбранной пекарне"
    when = f" {pickup_label(datetime.fromtimestamp(stored.pickup_at, tz), datetime.now(tz))}" if stored.pickup_at else ""
    return {
        "accepted": f"✅ Заказ №{n} принят. Ждём вас{when}: <b>{where}</b>",
        "cooking": f"👩‍🍳 Заказ №{n} готовится.",
        "ready": f"🥐 Заказ №{n} готов, ждём вас: <b>{where}</b>",
        "cancelled": f"❌ Заказ №{n} отменён. Если это ошибка, оформите заказ заново через меню.",
    }.get(stored.status, f"Заказ №{n}: {status_label(stored.status)}")


def orders_text(orders: list[StoredOrder], bakeries: dict[str, Bakery], tz: timezone) -> str:
    if not orders:
        return f"У вас пока нет заказов. Нажмите <b>{MENU_BUTTON_TEXT}</b> внизу, чтобы сделать первый."
    now = datetime.now(tz)
    blocks = ["📋 <b>Ваши последние заказы</b>"]
    for o in orders:
        bakery = bakeries.get(o.bakery_id)
        when = pickup_label(datetime.fromtimestamp(o.pickup_at, tz), now) if o.pickup_at else \
            datetime.fromtimestamp(o.created_at, tz).strftime("%d.%m")
        lines = [f"<b>№{o.number}</b> · {status_label(o.status)}"]
        if o.summary:
            lines.append(escape(o.summary))
        lines.append(f"{money(o.total)} · самовывоз {when}")
        if bakery:
            lines.append(f"📍 {escape(bakery.address)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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
                 notifier: ErrorNotifier, started_at: float, promos: dict[str, Promo] | None = None) -> Router:
    promos = promos or {}
    router = Router()
    duplicates = DuplicateGuard()

    def keyboard_for(user_id: int | None) -> ReplyKeyboardMarkup:
        """Кнопка мини-аппа с актуальными штампами и статусами заказов этого гостя."""
        url = config.webapp_url
        if user_id is not None:
            url = webapp_url_for(url, storage.bonus_state(user_id, menu.stamps_per_reward), storage.user_orders(user_id, 5))
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=url))]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder=INPUT_PLACEHOLDER,
        )

    def guest_id(message: Message) -> int:
        return message.from_user.id if message.from_user else message.chat.id

    # Inline-кнопки под приветствием. Кнопки мини-аппа здесь нет: заказ через sendData доходит только из нижней
    nav_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Мои заказы", callback_data=f"{NAV_CALLBACK}:orders"),
        InlineKeyboardButton(text="Мои штампы", callback_data=f"{NAV_CALLBACK}:bonus"),
    ]])
    cover = {"file_id": None}  # после первой отправки Telegram отдаёт file_id, дальше файл не загружаем заново

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        sent = None
        if cover["file_id"] or COVER_FILE.exists():
            try:
                sent = await message.answer_photo(cover["file_id"] or FSInputFile(COVER_FILE), caption=WELCOME_CAPTION,
                                                  parse_mode=None, reply_markup=nav_keyboard)
                cover["file_id"] = sent.photo[-1].file_id
            except TelegramBadRequest as e:
                log.warning("Не удалось отправить обложку: %s", e)
                cover["file_id"] = None
        if sent is None:  # обложки нет или Telegram её не принял - приветствие текстом
            await message.answer(WELCOME_CAPTION, parse_mode=None, reply_markup=nav_keyboard)
        await message.answer(WELCOME_KEYBOARD_TEXT, reply_markup=keyboard_for(guest_id(message)))

    @router.callback_query(F.data.in_({f"{NAV_CALLBACK}:orders", f"{NAV_CALLBACK}:bonus"}))
    async def nav(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id
        if callback.data.endswith("orders"):
            text = orders_text(storage.user_orders(user_id, 5), bakeries, menu.tz)
        else:
            text = bonus_text(storage.bonus_state(user_id, menu.stamps_per_reward))
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(text, reply_markup=keyboard_for(user_id))

    @router.message(Command("health"), IsAdmin(config.admin_chat_id))
    async def health(message: Message) -> None:
        now = datetime.now(menu.tz)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_count, today_sum = storage.count_since(midnight.timestamp())
        await message.answer(
            "🩺 <b>Бот работает</b>\n"
            f"Аптайм: {format_uptime(time.monotonic() - started_at)}\n"
            f"Заказов сегодня (без отменённых): {today_count} на {money(today_sum)}\n"
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
                reply_markup=keyboard_for(user_id),
            )
            return

        now = datetime.now(timezone.utc)
        try:
            order = parse_order(raw, menu, bakeries, now)
            # Скидки считает только бот: промокод по bot/promos.json, подарочный кофе по штампам из базы
            bonus = storage.bonus_state(user_id, menu.stamps_per_reward)
            pricing = price_order(order, menu, promos, now, free_coffee=bonus.available > 0)
        except OrderError as e:
            log.warning("Заказ от %s не принят: %s; data=%.300r", user_id, e, raw)
            await message.answer(e.user_message, reply_markup=keyboard_for(user_id))
            return

        summary = ", ".join(f"{line.title} × {line.qty}" for line in order.lines)
        number = storage.add_order(
            user_id, pricing.total, order.bakery.id, raw, chat_id=message.chat.id, summary=summary,
            pickup_at=order.pickup.timestamp(), subtotal=pricing.subtotal,
            promo_code=pricing.promo.code if pricing.promo else None, promo_discount=pricing.promo_discount,
            bonus_discount=pricing.bonus_discount, coffee_qty=pricing.paid_coffee, bonus_used=bool(pricing.bonus_title),
        )
        log.info("Заказ №%s от %s на %s (скидки %s + %s), %s", number, user_id, pricing.total,
                 pricing.promo_discount, pricing.bonus_discount, order.bakery.id)
        await message.answer(receipt_text(order, pricing, number, now), reply_markup=keyboard_for(user_id))

        if config.admin_chat_id is not None:
            try:
                base = admin_text(order, pricing, number, message, now)
                storage.set_admin_text(number, base)
                stored = storage.get_order(number)
                await bot.send_message(config.admin_chat_id, f"{base}\n\n{status_line(stored, menu.tz)}",
                                       reply_markup=admin_keyboard(number, stored.status))
            except Exception:
                # Клиент уже получил чек, ошибка уведомления админа не должна это ломать
                log.exception("Не удалось отправить заказ №%s админу %s", number, config.admin_chat_id)

    @router.message(Command("bonus"))
    async def my_bonus(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else message.chat.id
        await message.answer(bonus_text(storage.bonus_state(user_id, menu.stamps_per_reward)), reply_markup=keyboard_for(user_id))

    @router.message(Command("orders"))
    async def my_orders(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else message.chat.id
        await message.answer(orders_text(storage.user_orders(user_id, 5), bakeries, menu.tz), reply_markup=keyboard_for(user_id))

    @router.callback_query(F.data.startswith(f"{STATUS_CALLBACK}:"))
    async def change_status(callback: CallbackQuery, bot: Bot) -> None:
        admin = config.admin_chat_id
        chat_id = callback.message.chat.id if callback.message else None
        if admin is None or (callback.from_user.id != admin and chat_id != admin):
            log.warning("Попытка сменить статус не админом: %s", callback.from_user.id)
            await callback.answer("Менять статус может только администратор", show_alert=True)
            return
        try:
            _, number_s, status = callback.data.split(":")
            number = int(number_s)
        except ValueError:
            await callback.answer("Неизвестная кнопка")
            return
        stored = storage.get_order(number)
        if stored is None or status not in ADMIN_STATUS_BUTTONS:
            await callback.answer("Заказ или статус не найден", show_alert=True)
            return
        if stored.status == status:
            await callback.answer(f"Уже: {STATUSES[status][1]}")
            return

        stored = storage.set_status(number, status)
        log.info("Заказ №%s: статус %s (админ %s)", number, status, callback.from_user.id)
        # Перерисовываем копию у админа: исходный текст + новый статус и время
        if isinstance(callback.message, Message) and stored.admin_text:
            try:
                await callback.message.edit_text(f"{stored.admin_text}\n\n{status_line(stored, menu.tz)}",
                                                 reply_markup=admin_keyboard(number, status))
            except TelegramBadRequest as e:
                log.warning("Не удалось обновить сообщение заказа №%s: %s", number, e)

        delivered = True
        if stored.chat_id is not None:
            text = client_status_text(stored, bakeries.get(stored.bakery_id), menu.tz)
            # Штампы начисляются в момент "Готов к выдаче": сразу показываем гостю новый счёт
            if status == "ready" and stored.coffee_qty:
                state = storage.bonus_state(stored.user_id, menu.stamps_per_reward)
                n = stored.coffee_qty
                text += f"\n\n☕ +{n} {plural(n, 'штамп', 'штампа', 'штампов')}: {stamps_bar(state)} {state.stamps} из {state.per_reward}"
                if state.available:
                    text += "\n🎁 Следующий кофе бесплатно, бот применит подарок сам."
            try:
                await bot.send_message(stored.chat_id, text, reply_markup=keyboard_for(stored.user_id))
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                delivered = False
                log.warning("Гость заказа №%s не получил статус: %s", number, e)
        if delivered:
            await callback.answer(f"№{number}: {STATUSES[status][1]}")
        else:
            await callback.answer(f"Статус сохранён, но гость не получил сообщение (возможно, заблокировал бота)",
                                  show_alert=True)

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer(f"Чтобы сделать заказ, нажмите <b>{MENU_BUTTON_TEXT}</b> внизу экрана.",
                             reply_markup=keyboard_for(guest_id(message)))

    return router


async def setup_bot_profile(bot: Bot) -> None:
    """Команды, кнопка меню и описания бота. Ошибка здесь не мешает принимать заказы."""
    steps = [
        ("команды", bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])),
        # Слева от поля ввода список команд, а не второй вход в мини-апп: из него заказ через sendData не уйдёт
        ("кнопка меню", bot.set_chat_menu_button(menu_button=MenuButtonCommands())),
        ("короткое описание", bot.set_my_short_description(short_description=SHORT_DESCRIPTION)),
        ("описание", bot.set_my_description(description=DESCRIPTION)),
    ]
    for name, call in steps:
        try:
            await call
        except Exception as e:
            log.warning("Не удалось обновить %s бота: %s", name, e)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_dir)
    try:
        menu = load_menu(config.menu_file)
        bakeries = load_bakeries(config.bakeries_file)
        promos = load_promos(config.promos_file)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.error("Не удалось загрузить меню, пекарни или промокоды: %s", e)
        sys.exit(1)
    storage = Storage(config.data_dir / "orders.db")
    log.info("Меню %s: %d позиций, пекарен: %d, последний заказ: %s",
             menu.version, len(menu.items), len(bakeries), storage.last_number())

    started_at = time.monotonic()
    notifier = ErrorNotifier(config.admin_chat_id)
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(config, menu, bakeries, storage, notifier, started_at, promos))
    log.info("Промокодов: %d", len(promos))

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
        await setup_bot_profile(bot)
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
