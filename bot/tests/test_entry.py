"""Вход в мини-апп: приветствие на /start, inline-кнопки, нижняя кнопка, команды и описание бота."""

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import bot as b
from aiogram.types import InlineKeyboardMarkup, MenuButtonCommands, ReplyKeyboardMarkup

from test_bonus_flow import GUEST, env  # noqa: F401 (фикстура env)


def guest_message():
    return NS(from_user=NS(id=GUEST, first_name="Гость"), chat=NS(id=GUEST), answer=AsyncMock(),
              answer_photo=AsyncMock(return_value=NS(photo=[NS(file_id="small"), NS(file_id="COVER_ID")])))


def test_cover_file_exists_and_is_1280x720():
    data = b.COVER_FILE.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(data[16:20], "big") == 1280 and int.from_bytes(data[20:24], "big") == 720


def test_start_sends_cover_inline_buttons_and_bottom_button(env):
    db, h, bot = env

    async def scenario():
        m = guest_message()
        await h["start"](m)
        photo_call = m.answer_photo.call_args
        assert photo_call.kwargs["caption"] == (
            "Горячая выпечка и кофе без очереди.\n1. Откройте меню кнопкой внизу\n"
            "2. Выберите позиции, пекарню и время\n3. Заберите заказ, он будет готов\n"
            "Концепт, не официальный сервис пекарни Буханка.")
        inline = photo_call.kwargs["reply_markup"]
        assert isinstance(inline, InlineKeyboardMarkup)
        buttons = [btn for row in inline.inline_keyboard for btn in row]
        assert [btn.text for btn in buttons] == ["Мои заказы", "Мои штампы"]
        assert all(btn.web_app is None for btn in buttons)  # мини-апп только в нижней кнопке

        reply = m.answer.call_args.kwargs["reply_markup"]
        assert isinstance(reply, ReplyKeyboardMarkup)
        button = reply.keyboard[0][0]
        assert button.text == "Открыть меню" and button.web_app.url.startswith("https://e.x/")
        assert reply.resize_keyboard and reply.is_persistent
        assert reply.input_field_placeholder == "Нажмите кнопку ниже, чтобы заказать"

        # Второй /start: обложка уже загружена, отправляется по file_id
        m2 = guest_message()
        await h["start"](m2)
        assert m2.answer_photo.call_args.args[0] == "COVER_ID"

    asyncio.run(scenario())


def test_start_without_cover_falls_back_to_text(env, monkeypatch):
    db, h, bot = env
    monkeypatch.setattr(b, "COVER_FILE", Path("нет-такого-файла.png"))

    async def scenario():
        m = guest_message()
        await h["start"](m)
        assert not m.answer_photo.called
        texts = [c.args[0] for c in m.answer.call_args_list]
        assert texts[0].startswith("Горячая выпечка и кофе без очереди.")
        assert isinstance(m.answer.call_args_list[-1].kwargs["reply_markup"], ReplyKeyboardMarkup)

    asyncio.run(scenario())


def test_inline_buttons_show_orders_and_stamps(env):
    db, h, bot = env

    async def press(data):
        cb = NS(data=data, from_user=NS(id=GUEST), message=NS(answer=AsyncMock()), answer=AsyncMock())
        await h["nav"](cb)
        assert cb.answer.called
        return cb.message.answer.call_args

    async def scenario():
        orders = await press("nav:orders")
        assert "пока нет заказов" in orders.args[0]
        assert isinstance(orders.kwargs["reply_markup"], ReplyKeyboardMarkup)
        stamps = await press("nav:bonus")
        assert "У вас 0 из 5 штампов" in stamps.args[0]

    asyncio.run(scenario())


def test_bot_profile_commands_menu_button_descriptions():
    calls = NS(set_my_commands=AsyncMock(), set_chat_menu_button=AsyncMock(side_effect=RuntimeError("сеть")),
               set_my_short_description=AsyncMock(), set_my_description=AsyncMock())
    asyncio.run(b.setup_bot_profile(calls))
    commands = calls.set_my_commands.call_args.args[0]
    assert [(c.command, c.description) for c in commands] == [
        ("start", "Открыть меню"), ("orders", "Мои заказы"), ("bonus", "Мои штампы")]
    assert isinstance(calls.set_chat_menu_button.call_args.kwargs["menu_button"], MenuButtonCommands)
    # Ошибка одного шага не мешает остальным
    short = calls.set_my_short_description.call_args.kwargs["short_description"]
    desc = calls.set_my_description.call_args.kwargs["description"]
    assert short == "Горячая выпечка и кофе к вашему времени. Концепт." and len(short) <= 120
    assert "Концепт, не официальный бот пекарни Буханка" in desc and len(desc) <= 512 and 2 <= desc.count("\n") + 1 <= 3
