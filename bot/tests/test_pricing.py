"""Пересчёт скидок: промокоды из bot/promos.json и каждый 6-й кофе в подарок.

Запуск из корня репозитория:  pip install -r bot/requirements-dev.txt && pytest bot/tests
"""

import json
from datetime import datetime, timedelta

import pytest

import order as o
from export_promos import EXPORT_FILE, build_export
from storage import Storage

MENU = o.load_menu()
BAKERIES = o.load_bakeries()
PROMOS = o.load_promos()
MSK = MENU.tz

MORNING = datetime(2026, 10, 27, 8, 30, tzinfo=MSK)   # до 10:00, KOFE действует
EVENING = datetime(2026, 10, 27, 18, 0, tzinfo=MSK)   # после 10:00

LATTE_400 = ("latte", {"size": "400"}, 320)
AMERICANO = ("americano", {"size": "300"}, 220)
TEA = ("tea-black", {"size": "300"}, 180)
CROISSANT = ("croissant-classic", {}, 190)
COMBO = ("combo-1", {"drink": "latte", "main": "croissant-raspberry", "donut": "none"}, 420)


def payload(lines, now, promo=None, discount=0):
    """Заказ в том виде, как его шлёт мини-апп. lines: [(id, opts, цена, qty)]."""
    items = [{"id": i, "opts": opts, "qty": qty, "price": price} for i, opts, price, qty in lines]
    subtotal = sum(price * qty for _, _, price, qty in lines)
    slot = o.build_slots(now, MENU.hours, MENU)[0]
    return json.dumps({
        "v": 3, "items": items, "subtotal": subtotal, "promo": promo, "discount": discount,
        "total": subtotal - (discount or 0),
        "pickup": {"date": f"{slot:%Y-%m-%d}", "time": f"{slot:%H:%M}"},
        "bakery": {"id": "b005"}, "name": "Тест", "comment": "",
    })


def price(lines, now, promo=None, discount=0, free_coffee=False):
    order = o.parse_order(payload(lines, now, promo, discount), MENU, BAKERIES, now)
    return o.price_order(order, MENU, PROMOS, now, free_coffee)


def line(spec, qty=1):
    return (*spec, qty)


# ---------- Без скидок ----------

def test_no_discounts():
    p = price([line(LATTE_400, 2), line(CROISSANT)], EVENING)
    assert (p.subtotal, p.promo_discount, p.bonus_discount, p.total) == (830, 0, 0, 830)
    assert p.coffee_qty == 2 and p.paid_coffee == 2


def test_old_client_without_subtotal_still_accepted():
    data = json.loads(payload([line(CROISSANT)], EVENING))
    for key in ("subtotal", "promo", "discount"):
        data.pop(key)
    order = o.parse_order(json.dumps(data), MENU, BAKERIES, EVENING)
    assert o.price_order(order, MENU, PROMOS, EVENING, False).total == 190


# ---------- BUHANKA10: -10% на весь заказ ----------

def test_buhanka10_whole_order():
    p = price([line(LATTE_400), line(CROISSANT, 3)], EVENING, "BUHANKA10", 89)
    assert p.subtotal == 890 and p.promo_discount == 89 and p.total == 801


def odd_order(item_id, unit, qty, code):
    """Заказ с ценой, не кратной 10: в меню все цены круглые, а округление проверить надо."""
    lines = [o.OrderLine(item_id=item_id, title="Тест", options="", qty=qty, price=unit)]
    return o.Order(lines=lines, bakery=BAKERIES["b005"], pickup=EVENING, name="x", comment="", promo_code=code)


@pytest.mark.parametrize("unit, qty, code, now, expected", [
    (333, 1, "BUHANKA10", EVENING, 33),   # 33.3 -> 33
    (339, 1, "BUHANKA10", EVENING, 33),   # 33.9 -> 33, вниз даже у .9
    (333, 3, "BUHANKA10", EVENING, 99),   # 99.9 -> 99: процент от суммы, а не от каждой штуки
    (333, 1, "KOFE", MORNING, 66),        # 66.6 -> 66
])
def test_percent_rounds_down_to_ruble(unit, qty, code, now, expected):
    item = "latte" if code == "KOFE" else "croissant-classic"
    p = o.price_order(odd_order(item, unit, qty, code), MENU, PROMOS, now, False)
    assert p.promo_discount == expected and p.total == unit * qty - expected


def test_odd_price_gift_with_kofe_stays_non_negative():
    p = o.price_order(odd_order("latte", 333, 2, "KOFE"), MENU, PROMOS, MORNING, True)
    # 20% от 666 = 133; подарочная чашка: 333 - 66 = 267; вторая чашка оплачивается со скидкой
    assert (p.promo_discount, p.bonus_discount, p.total) == (133, 267, 266)


@pytest.mark.parametrize("code", ["buhanka10", "  Buhanka10 ", "BUHANKA10"])
def test_code_is_case_and_space_insensitive(code):
    assert price([line(CROISSANT)], EVENING, code, 19).promo_discount == 19


def test_combo_gets_whole_order_discount():
    assert price([line(COMBO)], EVENING, "BUHANKA10", 42).total == 378


# ---------- KOFE: -20% на кофе до 10:00 ----------

def test_kofe_only_coffee_morning():
    p = price([line(LATTE_400, 2), line(CROISSANT), line(TEA)], MORNING, "KOFE", 128)
    assert p.promo_discount == 640 * 20 // 100 == 128
    assert p.total == 640 + 190 + 180 - 128


def test_kofe_does_not_apply_to_tea_or_combo_coffee():
    with pytest.raises(o.OrderError) as e:
        price([line(TEA), line(COMBO)], MORNING, "KOFE", None)
    assert "только на кофе" in e.value.user_message


def test_kofe_after_10_rejected():
    with pytest.raises(o.OrderError) as e:
        price([line(LATTE_400)], EVENING, "KOFE", 64)
    assert "до 10:00" in e.value.user_message


def test_kofe_exactly_at_10_rejected():
    ten = datetime(2026, 10, 27, 10, 0, tzinfo=MSK)
    with pytest.raises(o.OrderError):
        price([line(LATTE_400)], ten, "KOFE", 64)


# ---------- Неверные коды ----------

def test_unknown_code():
    with pytest.raises(o.OrderError) as e:
        price([line(CROISSANT)], EVENING, "FREEFOOD", None)
    assert "«FREEFOOD» не найден" in e.value.user_message


def test_html_in_code_is_escaped():
    with pytest.raises(o.OrderError) as e:
        price([line(CROISSANT)], EVENING, "<b>x</b>", None)
    assert "<b>" not in e.value.user_message and "&lt;" in e.value.user_message.lower()


def test_expired_code():
    later = datetime(2027, 1, 1, 9, 0, tzinfo=MSK)
    with pytest.raises(o.OrderError) as e:
        o.check_promo("BUHANKA10", PROMOS, later, MENU)
    assert "закончился 31.12.2026" in e.value.user_message


def test_code_not_started_yet():
    early = datetime(2026, 9, 30, 12, 0, tzinfo=MSK)
    with pytest.raises(o.OrderError) as e:
        o.check_promo("BUHANKA10", PROMOS, early, MENU)
    assert "начнёт действовать 01.10.2026" in e.value.user_message


def test_last_valid_day_inclusive():
    assert o.check_promo("BUHANKA10", PROMOS, datetime(2026, 12, 31, 23, 50, tzinfo=MSK), MENU).percent == 10


# ---------- Скидка из мини-аппа против скидки бота ----------

# Расхождение не отклоняет заказ: сумма считается по боту, а mismatch нужен для сообщения гостю и лога

def check(raw, now, free_coffee=False):
    return o.validate_order(raw, MENU, BAKERIES, PROMOS, now, free_coffee)


def test_client_discount_mismatch_uses_bot_discount():
    r = check(payload([line(LATTE_400)], MORNING, "KOFE", 100), MORNING)
    assert r.pricing.promo_discount == 64 and r.pricing.total == 256
    assert r.mismatch.expected == 256 and r.mismatch.sent == 220


def test_client_claims_discount_without_code():
    r = check(payload([line(LATTE_400)], EVENING, None, 50), EVENING)
    assert r.pricing.total == 320 and r.mismatch.sent == 270


def test_unchecked_preview_accepts_bot_discount():
    # Мини-апп не смог проверить код (discount: null) - бот применяет свою скидку, это не расхождение
    r = check(payload([line(CROISSANT, 2)], EVENING, "BUHANKA10", None), EVENING)
    assert r.pricing.promo_discount == 38 and r.mismatch is None


def test_correct_discount_is_not_mismatch():
    assert check(payload([line(CROISSANT, 2)], EVENING, "BUHANKA10", 38), EVENING).mismatch is None


def test_discount_bigger_than_subtotal_ignored():
    r = check(payload([line(CROISSANT)], EVENING, "BUHANKA10", 500), EVENING)
    assert r.pricing.total == 171 and r.mismatch.expected == 171


# ---------- Каждый 6-й кофе в подарок ----------

def test_free_coffee_is_most_expensive_cup():
    p = price([line(AMERICANO), line(LATTE_400), line(CROISSANT)], EVENING, free_coffee=True)
    assert p.bonus_discount == 320 and "Латте" in p.bonus_title
    assert p.total == 220 + 320 + 190 - 320
    assert p.coffee_qty == 2 and p.paid_coffee == 1  # подарочная чашка не идёт в штампы


def test_no_free_coffee_without_reward():
    p = price([line(LATTE_400)], EVENING, free_coffee=False)
    assert p.bonus_discount == 0 and p.bonus_title is None


def test_reward_not_spent_without_coffee():
    p = price([line(TEA), line(COMBO)], EVENING, free_coffee=True)
    assert p.bonus_discount == 0 and p.bonus_title is None


def test_bonus_with_kofe_never_below_zero():
    # KOFE уже дал 20% на подарочную чашку: бонус обнуляет только остаток, чашка стоит ровно 0
    p = price([line(LATTE_400)], MORNING, "KOFE", 64, free_coffee=True)
    assert (p.promo_discount, p.bonus_discount, p.total) == (64, 256, 0)


def test_bonus_with_whole_order_code():
    p = price([line(LATTE_400), line(CROISSANT)], EVENING, "BUHANKA10", 51, free_coffee=True)
    assert p.promo_discount == 51 and p.bonus_discount == 320 - 32 and p.total == 510 - 51 - 288


# ---------- Штампы в базе ----------

@pytest.fixture
def db(tmp_path):
    s = Storage(tmp_path / "orders.db")
    yield s
    s.close()


def add(db, coffee, status, bonus_used=False, user=1):
    n = db.add_order(user, 100, "b005", "{}", coffee_qty=coffee, bonus_used=bonus_used)
    db.set_status(n, status)
    return n


def test_stamps_count_only_ready_orders(db):
    add(db, 2, "ready")
    add(db, 1, "accepted")
    add(db, 3, "cancelled")
    add(db, 1, "new")
    state = db.bonus_state(1, MENU.stamps_per_reward)
    assert (state.paid_ready, state.stamps, state.available) == (2, 2, 0)


def test_fifth_coffee_unlocks_gift(db):
    add(db, 3, "ready")
    add(db, 2, "ready")
    state = db.bonus_state(1, 5)
    assert state.stamps == 0 and state.available == 1


def test_gift_spent_once_even_before_ready(db):
    add(db, 5, "ready")
    add(db, 0, "new", bonus_used=True)  # подарок уже в новом заказе
    assert db.bonus_state(1, 5).available == 0


def test_cancelled_gift_order_returns_gift(db):
    add(db, 5, "ready")
    add(db, 0, "cancelled", bonus_used=True)
    assert db.bonus_state(1, 5).available == 1


def test_stamps_are_per_user(db):
    add(db, 5, "ready", user=1)
    assert db.bonus_state(2, 5).paid_ready == 0


def test_status_change_back_removes_stamps(db):
    n = add(db, 4, "ready")
    db.set_status(n, "cooking")  # админ ошибся и вернул статус
    assert db.bonus_state(1, 5).stamps == 0


# ---------- Файл для мини-аппа ----------

def test_promo_export_is_up_to_date():
    assert EXPORT_FILE.read_text(encoding="utf-8") == build_export(), \
        "webapp/promo-codes.json устарел: выполните python bot/export_promos.py"


def test_export_contains_no_plain_codes():
    text = EXPORT_FILE.read_text(encoding="utf-8")
    assert "BUHANKA10" not in text and "KOFE" not in text
    assert o.promo_hash(" kofe ") in text
