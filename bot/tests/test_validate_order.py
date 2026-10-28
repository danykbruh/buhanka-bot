"""validate_order: бот считает заказ только по своему меню и отклоняет неверные данные.

Запуск из корня репозитория:  pip install -r bot/requirements-dev.txt && pytest bot/tests
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

import order as o

MENU = o.load_menu()
BAKERIES = o.load_bakeries()
PROMOS = o.load_promos()
MSK = MENU.tz
NOW = datetime(2026, 10, 27, 18, 0, tzinfo=MSK)
TOKEN = "123456:TEST-token"


def make(items, now=NOW, **extra):
    """Заказ как из мини-аппа. items: [(id, opts, цена за штуку, qty)], суммы честные."""
    subtotal = sum(price * qty for _, _, price, qty in items if isinstance(qty, (int, float)))
    slot = o.build_slots(now, MENU.hours, MENU)[0]
    data = {
        "v": 2, "items": [{"id": i, "opts": opts, "qty": qty, "price": price} for i, opts, price, qty in items],
        "subtotal": subtotal, "promo": None, "discount": 0, "total": subtotal,
        "pickup": {"date": f"{slot:%Y-%m-%d}", "time": f"{slot:%H:%M}"},
        "bakery": {"id": "b005"}, "name": "Тест", "comment": "",
    }
    data.update(extra)
    return data


def check(data, now=NOW, free_coffee=False, **kw):
    raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return o.validate_order(raw, MENU, BAKERIES, PROMOS, now, free_coffee, **kw)


def rejected(data, now=NOW, **kw) -> str:
    """Текст отказа для гостя. В нём не должно быть технических подробностей."""
    with pytest.raises(o.OrderError) as e:
        check(data, now, **kw)
    msg = e.value.user_message
    for leak in ("items[", "qty", "opts", "payload", "initData", "Traceback", str(e.value)):
        assert leak not in msg
    assert "—" not in msg and "–" not in msg
    return msg


LATTE = ("latte", {"size": "400"}, 320, 1)
CROISSANT = ("croissant-classic", {}, 190, 2)
COMBO = ("combo-1", {"drink": "raf", "main": "croissant-raspberry", "donut": "none"}, 460, 1)


# ---------- Нормальный заказ ----------

def test_normal_order():
    r = check(make([LATTE, CROISSANT]))
    assert r.pricing.total == 700 and r.mismatch is None
    assert [(line.item_id, line.qty, line.price) for line in r.order.lines] == [("latte", 1, 320), ("croissant-classic", 2, 190)]


def test_combo_options_priced_by_menu():
    # Раф в комбо +40 к цене набора
    r = check(make([COMBO]))
    assert r.pricing.total == 460 and r.mismatch is None


def test_only_needed_fields_matter():
    # Лишние поля клиента ничего не меняют
    r = check(make([LATTE], discountPercent=99, bonus=True, items_total=1))
    assert r.pricing.total == 320 and r.mismatch is None


# ---------- Подмена цен и суммы ----------

def test_price_swap_accepted_by_menu_price():
    data = make([LATTE])
    data["items"][0]["price"] = 1
    data["subtotal"] = data["total"] = 1
    r = check(data)
    assert r.pricing.total == 320
    assert (r.mismatch.expected, r.mismatch.sent) == (320, 1)
    assert any("latte" in d for d in r.mismatch.details)


def test_sum_swap_accepted_by_menu_sum():
    data = make([LATTE, CROISSANT], total=10)
    r = check(data)
    assert r.pricing.total == 700 and (r.mismatch.expected, r.mismatch.sent) == (700, 10)


def test_garbage_prices_ignored():
    data = make([LATTE], subtotal="бесплатно", total=None, discount=-5)
    data["items"][0]["price"] = {"x": 1}
    r = check(data)
    assert r.pricing.total == 320 and r.mismatch.sent is None


# ---------- Позиции и количество ----------

def test_unknown_id_rejected():
    msg = rejected(make([("pizza-xxl", {}, 1, 1)]))
    assert msg == o.MSG_MENU_CHANGED


@pytest.mark.parametrize("qty", [0, -1, -100, 21, 10**9, 10**100, 1.5, "2", True, None])
def test_bad_qty_rejected(qty):
    assert "от 1 до 20" in rejected(make([("croissant-classic", {}, 190, qty)]))


def test_too_many_lines_rejected():
    assert "не больше 30" in rejected(make([("croissant-classic", {}, 190, 1)] * 31))


# ---------- Опции ----------

def test_foreign_option_group_rejected():
    # У круассана нет размера, это опция напитка
    assert rejected(make([("croissant-classic", {"size": "400"}, 190, 1)])) == o.MSG_MENU_CHANGED


def test_foreign_choice_in_combo_rejected():
    # Круассан вместо напитка в комбо
    opts = {"drink": "croissant-classic", "main": "croissant-raspberry", "donut": "none"}
    assert rejected(make([("combo-1", opts, 420, 1)])) == o.MSG_MENU_CHANGED


def test_missing_required_combo_choice_rejected():
    assert rejected(make([("combo-1", {"main": "croissant-raspberry"}, 420, 1)])) == o.MSG_MENU_CHANGED


def test_option_value_of_wrong_type_rejected():
    assert rejected(make([("latte", {"size": ["400"]}, 320, 1)])) == o.MSG_MENU_CHANGED


# ---------- Промокод и подарочный кофе ----------

def test_expired_promo_rejected():
    later = datetime(2027, 1, 5, 12, 0, tzinfo=MSK)
    msg = rejected(make([LATTE], later, promo="BUHANKA10", discount=32, total=288), later)
    assert "закончился 31.12.2026" in msg


def test_unknown_promo_rejected():
    assert "не найден" in rejected(make([LATTE], promo="FREE100"))


def test_promo_conditions_not_met_rejected():
    # KOFE только на кофе и только до 10:00
    morning = NOW.replace(hour=8, minute=30)
    assert "только на кофе" in rejected(make([CROISSANT], morning, promo="KOFE", discount=0), morning)
    assert "до 10:00" in rejected(make([LATTE], promo="KOFE", discount=32))


def test_free_coffee_only_from_bot_data():
    # Клиент не может включить подарок сам: решает free_coffee из базы бота
    assert check(make([LATTE], bonus=True, free=True)).pricing.bonus_discount == 0
    assert check(make([LATTE]), free_coffee=True).pricing.total == 0


# ---------- Время самовывоза ----------

def test_time_in_past_rejected():
    past = NOW - timedelta(hours=1)
    assert rejected(make([LATTE], pickup={"date": f"{past:%Y-%m-%d}", "time": f"{past:%H:%M}"})) == o.MSG_SLOT


def test_time_before_prep_rejected():
    soon = NOW + timedelta(minutes=5)
    assert rejected(make([LATTE], pickup={"date": f"{soon:%Y-%m-%d}", "time": f"{soon:%H:%M}"})) == o.MSG_SLOT


def test_time_outside_hours_rejected():
    assert rejected(make([LATTE], pickup={"date": "2026-10-28", "time": "23:30"})) == o.MSG_SLOT


def test_bad_time_format_rejected():
    assert rejected(make([LATTE], pickup={"date": "2026-10-28", "time": "9 утра"})) == o.MSG_INVALID


# ---------- Размер и формат ----------

def test_oversized_payload_rejected():
    raw = json.dumps(make([LATTE], comment="я" * 3000), ensure_ascii=False)
    assert len(raw.encode()) > o.MAX_PAYLOAD_BYTES
    assert rejected(raw) == o.MSG_TOO_BIG


@pytest.mark.parametrize("raw", ["", "не json", "[]", "null", '{"items": []}', '{"items": "латте"}'])
def test_broken_payload_rejected(raw):
    assert rejected(raw) == o.MSG_INVALID


# ---------- Подпись initData ----------

def signed_init_data(user_id=42, auth=NOW, token=TOKEN):
    fields = {"auth_date": str(int(auth.timestamp())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False)}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_init_data_accepted():
    r = check(make([LATTE], initData=signed_init_data()), bot_token=TOKEN, user_id=42)
    assert r.init_user_id == 42 and r.pricing.total == 320


def test_init_data_with_wrong_token_rejected():
    assert rejected(make([LATTE], initData=signed_init_data(token="999:other")), bot_token=TOKEN) == o.MSG_INVALID


def test_tampered_init_data_rejected():
    tampered = signed_init_data().replace("%22id%22%3A+42", "%22id%22%3A+43")
    assert tampered != signed_init_data()
    assert rejected(make([LATTE], initData=tampered), bot_token=TOKEN) == o.MSG_INVALID


def test_init_data_of_other_user_rejected():
    assert rejected(make([LATTE], initData=signed_init_data(user_id=7)), bot_token=TOKEN, user_id=42) == o.MSG_INVALID


def test_old_init_data_rejected():
    old = signed_init_data(auth=NOW - timedelta(days=2))
    assert rejected(make([LATTE], initData=old), bot_token=TOKEN) == o.MSG_INVALID


def test_without_init_data_still_accepted():
    # Мини-апп с кнопки клавиатуры (sendData) initData не получает
    assert check(make([LATTE]), bot_token=TOKEN, user_id=42).init_user_id is None


def test_init_data_fields_parsed():
    data = signed_init_data()
    fields = o.validate_init_data(data, TOKEN, NOW.astimezone(timezone.utc))
    assert o.init_data_user_id(fields) == 42
