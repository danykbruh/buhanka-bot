"""Сквозной сценарий через хендлеры бота: заказы, статусы, штампы, подарочный кофе, /bonus."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import bot as b
import order as o
from storage import Storage

MENU, BAKERIES, PROMOS = o.load_menu(), o.load_bakeries(), o.load_promos()
ADMIN, GUEST = 999, 42


def order_json(lines, promo=None, discount=0):
    now = datetime.now(timezone.utc)
    slot = o.build_slots(now, MENU.hours, MENU)[0]
    items = [{"id": i, "opts": opts, "qty": qty, "price": price} for i, opts, price, qty in lines]
    subtotal = sum(price * qty for _, _, price, qty in lines)
    return json.dumps({"v": 3, "items": items, "subtotal": subtotal, "promo": promo, "discount": discount,
                       "total": subtotal - (discount or 0),
                       "pickup": {"date": f"{slot:%Y-%m-%d}", "time": f"{slot:%H:%M}"},
                       "bakery": {"id": "b005"}, "name": "Гость", "comment": ""})


@pytest.fixture
def env(tmp_path):
    db = Storage(tmp_path / "orders.db")
    cfg = b.Config(token="1:x", admin_chat_id=ADMIN, webapp_url="https://e.x/", menu_file=Path(),
                   bakeries_file=Path(), log_dir=tmp_path, data_dir=tmp_path)
    router = b.build_router(cfg, MENU, BAKERIES, db, b.ErrorNotifier(ADMIN), 0, PROMOS)
    handlers = {h.callback.__name__: h.callback for h in router.message.handlers}
    handlers.update({h.callback.__name__: h.callback for h in router.callback_query.handlers})
    bot = NS(send_message=AsyncMock())
    yield db, handlers, bot
    db.close()


def guest_message(**kw):
    return NS(from_user=NS(id=GUEST, full_name="Гость", username=None), chat=NS(id=GUEST), answer=AsyncMock(), **kw)


async def place(handlers, bot, raw):
    m = guest_message(web_app_data=NS(data=raw))
    await handlers["web_app_order"](m, bot)
    return m.answer.call_args.args[0]


async def set_status(handlers, bot, number, status):
    cb = NS(data=f"st:{number}:{status}", from_user=NS(id=ADMIN), message=None, answer=AsyncMock())
    await handlers["change_status"](cb, bot)


async def bonus_reply(handlers):
    m = guest_message()
    await handlers["my_bonus"](m)
    return m.answer.call_args.args[0]


def test_sixth_coffee_free_end_to_end(env):
    db, h, bot = env

    async def scenario():
        # 3 + 2 кофе в двух заказах. Штампы появляются только после "Готов к выдаче"
        r1 = await place(h, bot, order_json([("latte", {"size": "300"}, 280, 3)]))
        assert "+3 штампа" in r1 and "Кофе в подарок" not in r1
        assert "У вас 0 из 5 штампов" in await bonus_reply(h)
        await set_status(h, bot, 1001, "ready")
        notify = bot.send_message.call_args.args[1]
        assert "готов" in notify and "+3 штампа" in notify and "3 из 5" in notify
        assert "🟤🟤🟤⚪⚪" in await bonus_reply(h)

        # Чай и кофе в комбо штампов не дают
        await place(h, bot, order_json([("tea-black", {"size": "300"}, 180, 1),
                                        ("combo-1", {"drink": "latte", "main": "croissant-raspberry", "donut": "none"}, 420, 1)]))
        await set_status(h, bot, 1002, "ready")
        assert "3 из 5" in await bonus_reply(h)

        # Заказ в работе (не готов) штампы не добавляет
        await place(h, bot, order_json([("americano", {"size": "300"}, 220, 2)]))
        assert "3 из 5" in await bonus_reply(h)
        await set_status(h, bot, 1003, "ready")
        text = await bonus_reply(h)
        assert "Следующий кофе бесплатно" in text

        # Ждём 10 минут антиспама: подменяем время прошлых заказов
        db.db.execute("UPDATE orders SET created_at = created_at - 3600")
        db.db.commit()

        # Шестой кофе: бот сам отдаёт самый дорогой кофе и пишет об этом в чеке
        receipt = await place(h, bot, order_json([("americano", {"size": "300"}, 220, 1),
                                                  ("raf", {"size": "400"}, 360, 1)]))
        assert "Кофе в подарок (Раф, 400 мл): -360" in receipt
        assert "Итого к оплате: 220" in receipt
        assert "+1 штамп " in receipt  # подарочная чашка штамп не даёт
        stored = db.get_order(1004)
        assert (stored.total, stored.coffee_qty, stored.bonus_used) == (220, 1, 1)
        # Подарок потрачен сразу, ещё до готовности заказа: второй раз не выдаётся
        assert "Следующий кофе бесплатно" not in await bonus_reply(h)

        # Отмена заказа с подарком возвращает подарок
        await set_status(h, bot, 1004, "cancelled")
        assert "Следующий кофе бесплатно" in await bonus_reply(h)

    asyncio.run(scenario())


def test_promo_and_gift_in_receipt_and_admin_copy(env):
    db, h, bot = env

    async def scenario():
        for _ in range(5):
            n = db.add_order(GUEST, 1, "b005", "{}", coffee_qty=1)
            db.set_status(n, "ready")
        db.db.execute("UPDATE orders SET created_at = created_at - 3600")
        db.db.commit()
        receipt = await place(h, bot, order_json([("latte", {"size": "400"}, 320, 1), ("croissant-classic", {}, 190, 1)],
                                                 promo="buhanka10", discount=51))
        assert "Сумма: 510" in receipt
        assert "Промокод BUHANKA10 (-10% на весь заказ): -51" in receipt
        assert "Кофе в подарок (Латте, 400 мл): -288" in receipt
        assert "Итого к оплате: 171" in receipt
        admin_copy = bot.send_message.call_args.args[1]
        assert "Промокод BUHANKA10" in admin_copy and "Итого к оплате: 171" in admin_copy

    asyncio.run(scenario())


def test_invalid_promo_rejects_order_with_clear_message(env):
    db, h, bot = env

    async def scenario():
        reply = await place(h, bot, order_json([("croissant-classic", {}, 190, 1)], promo="NOPE", discount=None))
        assert "Промокод «NOPE» не найден" in reply
        assert db.last_number() is None  # заказ не сохранён

    asyncio.run(scenario())


def test_price_swap_accepted_at_bot_sum_with_note_and_log(env, caplog):
    db, h, bot = env

    async def scenario():
        raw = json.loads(order_json([("latte", {"size": "400"}, 320, 2)]))
        raw["items"][0]["price"], raw["subtotal"], raw["total"] = 1, 2, 2
        receipt = await place(h, bot, json.dumps(raw))
        assert "Итого: 640" in receipt
        assert f"пересчитали заказ (в корзине было {b.money(2)}). К оплате: <b>{b.money(640)}</b>" in receipt
        assert "—" not in receipt and "–" not in receipt
        assert db.get_order(1001).total == 640
        assert f"Сумма не совпала: user_id={GUEST} expected=640 sent=2" in caplog.text

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())


def test_broken_payload_rejected_politely(env):
    db, h, bot = env

    async def scenario():
        reply = await place(h, bot, '{"items": [{"id": "latte", "qty": -5}]}')
        assert reply == o.MSG_MENU_CHANGED or reply.startswith("🧺")
        assert "qty" not in reply and db.last_number() is None

    asyncio.run(scenario())
