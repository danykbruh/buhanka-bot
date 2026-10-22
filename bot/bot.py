"""Бот пекарни "Буханка" (концепт): открывает мини-апп и принимает заказы из него.

Настройки только из переменных окружения (или файла .env рядом с ботом):
    BOT_TOKEN      - токен от @BotFather
    ADMIN_CHAT_ID  - чат, куда дублируются заказы (ваш user id или id группы)
    WEBAPP_URL     - https-адрес мини-аппа (например, GitHub Pages)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import ErrorEvent, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("buhanka")

MENU_BUTTON_TEXT = "🥐 Открыть меню"

# Ограничения на входящие данные. Заказ приходит от клиента, поэтому проверяем всё.
MAX_ITEMS = 50
MAX_QTY = 99
MAX_PRICE = 100_000
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------- Конфиг ----------

@dataclass(frozen=True)
class Config:
    token: str
    admin_chat_id: int | None
    webapp_url: str


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
            log.warning("ADMIN_CHAT_ID=%r не число - заказы не будут дублироваться админу", admin_raw)
    else:
        log.warning("ADMIN_CHAT_ID не задан - заказы не будут дублироваться админу")

    return Config(token=token, admin_chat_id=admin_chat_id, webapp_url=webapp_url)


# ---------- Разбор заказа ----------

class OrderError(ValueError):
    """Данные из мини-аппа не похожи на заказ."""


@dataclass(frozen=True)
class OrderItem:
    title: str
    options: str
    qty: int
    price: int

    @property
    def sum(self) -> int:
        return self.qty * self.price


@dataclass(frozen=True)
class Order:
    items: list[OrderItem]
    pickup_date: date
    pickup_time: str
    name: str
    comment: str
    client_total: int | None

    @property
    def total(self) -> int:
        return sum(i.sum for i in self.items)


def _text(value: object, field: str, limit: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise OrderError(f"{field}: ожидалась строка")
    value = " ".join(value.split())[:limit]
    if required and not value:
        raise OrderError(f"{field}: пусто")
    return value


def _int(value: object, field: str, lo: int, hi: int) -> int:
    # bool - подкласс int, отсекаем явно
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise OrderError(f"{field}: ожидалось целое от {lo} до {hi}")
    return value


def parse_order(raw: str) -> Order:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise OrderError("не JSON") from e
    if not isinstance(data, dict):
        raise OrderError("корень не объект")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise OrderError("items: пустой или слишком длинный список")

    items = []
    for n, it in enumerate(raw_items, 1):
        if not isinstance(it, dict):
            raise OrderError(f"items[{n}]: не объект")
        items.append(OrderItem(
            title=_text(it.get("title"), f"items[{n}].title", 80),
            options=_text(it.get("options"), f"items[{n}].options", 160, required=False),
            qty=_int(it.get("qty"), f"items[{n}].qty", 1, MAX_QTY),
            price=_int(it.get("price"), f"items[{n}].price", 0, MAX_PRICE),
        ))

    pickup = data.get("pickup")
    if not isinstance(pickup, dict):
        raise OrderError("pickup: не объект")
    try:
        pickup_date = date.fromisoformat(str(pickup.get("date")))
    except ValueError as e:
        raise OrderError("pickup.date: не дата") from e
    pickup_time = str(pickup.get("time"))
    if not TIME_RE.match(pickup_time):
        raise OrderError("pickup.time: не ЧЧ:ММ")

    total = data.get("total")
    return Order(
        items=items,
        pickup_date=pickup_date,
        pickup_time=pickup_time,
        name=_text(data.get("name"), "name", 40),
        comment=_text(data.get("comment"), "comment", 200, required=False),
        client_total=total if isinstance(total, int) and not isinstance(total, bool) else None,
    )


# ---------- Тексты ----------

def money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def pickup_label(order: Order, today: date) -> str:
    delta = (order.pickup_date - today).days
    day = {0: "сегодня", 1: "завтра"}.get(delta, order.pickup_date.strftime("%d.%m"))
    return f"{day}, {order.pickup_time}"


def items_block(order: Order) -> str:
    lines = []
    for i in order.items:
        line = f"• {escape(i.title)} × {i.qty} - {money(i.sum)}"
        if i.options:
            line += f"\n   <i>{escape(i.options)}</i>"
        lines.append(line)
    return "\n".join(lines)


def receipt_text(order: Order, number: int) -> str:
    parts = [
        f"🧾 <b>Заказ №{number} принят</b>",
        "",
        items_block(order),
        "",
        f"<b>Итого: {money(order.total)}</b>",
        f"🕒 Самовывоз: <b>{pickup_label(order, date.today())}</b>",
        f"👤 Имя: {escape(order.name)}",
    ]
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    parts += ["", "Оплата при получении. Покажите этот чек на кассе.",
              "<i>Концепт. Не является официальным сервисом пекарни Буханка.</i>"]
    return "\n".join(parts)


def admin_text(order: Order, number: int, message: Message) -> str:
    user = message.from_user
    who = escape(order.name)
    if user:
        link = f'<a href="tg://user?id={user.id}">{escape(user.full_name)}</a>'
        who += f" ({link}" + (f", @{escape(user.username)}" if user.username else "") + f", id {user.id})"
    parts = [
        f"🔔 <b>Новый заказ №{number}</b>",
        f"🕒 Самовывоз: <b>{pickup_label(order, date.today())}</b>",
        f"👤 {who}",
        "",
        items_block(order),
        "",
        f"<b>Итого: {money(order.total)}</b>",
    ]
    if order.client_total is not None and order.client_total != order.total:
        parts.append(f"⚠️ Мини-апп прислал сумму {money(order.client_total)}, пересчитано по позициям")
    if order.comment:
        parts.append(f"💬 {escape(order.comment)}")
    return "\n".join(parts)


# ---------- Хендлеры ----------

def build_router(config: Config) -> Router:
    router = Router()
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

    @router.message(F.web_app_data)
    async def web_app_order(message: Message, bot: Bot) -> None:
        raw = message.web_app_data.data
        try:
            order = parse_order(raw)
        except OrderError as e:
            log.warning("Битый заказ от %s: %s; data=%.300r",
                        message.from_user.id if message.from_user else "?", e, raw)
            await message.answer(
                "😕 Не получилось прочитать заказ. Откройте меню ещё раз и попробуйте снова.",
                reply_markup=keyboard,
            )
            return

        number = message.message_id
        await message.answer(receipt_text(order, number), reply_markup=keyboard)

        if config.admin_chat_id is not None:
            try:
                await bot.send_message(config.admin_chat_id, admin_text(order, number, message))
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
    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(config))

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Любая непредвиденная ошибка в хендлере логируется, бот продолжает работать
        log.exception("Ошибка при обработке апдейта: %s", event.exception, exc_info=event.exception)
        return True

    me = await bot.get_me()
    log.info("Бот @%s запущен, мини-апп: %s", me.username, config.webapp_url)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
