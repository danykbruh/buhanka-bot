"""Адрес кнопки "Открыть меню": штампы и статусы последних заказов для мини-аппа."""

import asyncio
from urllib.parse import parse_qs, urlsplit

import bot as b
from storage import BonusState, StoredOrder

from test_bonus_flow import ADMIN, GUEST, env, order_json, place, set_status  # noqa: F401 (фикстура env)


def stored(number, status, created, total):
    return StoredOrder(number=number, user_id=1, chat_id=1, created_at=created, total=total, bakery_id="b005",
                       status=status, status_at=None, summary="", pickup_at=None, admin_text="")


def test_url_params_format():
    url = b.webapp_url_for("https://x.github.io/buhanka-bot/", BonusState(paid_ready=8, used=1, per_reward=5),
                           [stored(1004, "ready", 1761550000.7, 620), stored(1003, "cancelled", 1761540000, 290)])
    q = parse_qs(urlsplit(url).query)
    assert q["st"] == ["3"] and q["gift"] == ["0"]
    assert q["o"] == ["1004.r.1761550000.620,1003.x.1761540000.290"]
    assert "," in url and "%2C" not in url  # список читается без лишнего кодирования


def test_existing_query_kept_and_old_params_replaced():
    url = b.webapp_url_for("https://x.io/app/?utm=bot&st=9&o=old", BonusState(0, 0, 5), [])
    q = parse_qs(urlsplit(url).query)
    assert q == {"utm": ["bot"], "st": ["0"], "gift": ["0"]}
    assert urlsplit(url).path == "/app/"


def menu_url(markup):
    return markup.keyboard[0][0].web_app.url


def test_keyboard_updates_after_order_and_status(env):
    db, h, bot = env

    async def scenario():
        m_order = await place_with_markup(h, bot, order_json([("latte", {"size": "300"}, 280, 2)]))
        assert "o=1001.n." in m_order and "st=0" in m_order
        await set_status(h, bot, 1001, "ready")
        # Гость получил уведомление вместе с обновлённой кнопкой: статус "готов" и 2 штампа
        url = menu_url(bot.send_message.call_args.kwargs["reply_markup"])
        assert "o=1001.r." in url and "st=2" in url

    asyncio.run(scenario())


async def place_with_markup(h, bot, raw):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    m = NS(web_app_data=NS(data=raw), from_user=NS(id=GUEST, full_name="Гость", username=None),
           chat=NS(id=GUEST), answer=AsyncMock())
    await h["web_app_order"](m, bot)
    return menu_url(m.answer.call_args.kwargs["reply_markup"])
