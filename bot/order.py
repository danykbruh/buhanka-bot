"""Проверка заказа из мини-аппа.

Бот не доверяет ценам и сумме из мини-аппа: пересчитывает каждую позицию по webapp/menu.json
(тот же файл читает мини-апп), проверяет лимиты, пекарню и слот самовывоза.
Здесь нет ничего про Telegram, поэтому функции легко проверять отдельно.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
DEFAULT_MENU_FILE = WEBAPP_DIR / "menu.json"
DEFAULT_BAKERIES_FILE = WEBAPP_DIR / "bakeries.js"
DEFAULT_PROMOS_FILE = Path(__file__).resolve().parent / "promos.json"
PROMO_CODE_RE = re.compile(r"^[A-ZА-ЯЁ0-9_-]{1,24}$")

BAKERIES_ARRAY_RE = re.compile(r"window\.BAKERIES\s*=\s*(\[.*\])\s*;", re.DOTALL)
HOURS_RE = re.compile(r"^(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Гость мог открыть оформление и отправить заказ не сразу: слоты, которые он видел,
# строились по более раннему времени. Насколько назад бот допускает такое отставание.
SLOT_GRACE = timedelta(minutes=20)

# Тексты для гостя по типу ошибки
MSG_INVALID = "😕 Не получилось прочитать заказ. Откройте меню ещё раз и попробуйте снова."
MSG_MENU_CHANGED = "🔄 Меню обновилось, откройте его заново и соберите заказ ещё раз."
MSG_SLOT = "🕒 Это время самовывоза уже недоступно. Откройте меню и выберите другое."
MSG_BAKERY = "📍 Не нашли выбранную пекарню. Откройте меню и выберите пекарню заново."
MSG_PROMO_CHANGED = "🏷 Скидка по промокоду изменилась. Откройте корзину заново, мы пересчитаем сумму."
_PROMO_FIX = " Уберите или замените код в корзине и оформите заказ снова."


class OrderError(ValueError):
    """Заказ не принят. str(e) - причина для лога, user_message - текст для гостя."""

    def __init__(self, reason: str, user_message: str = MSG_INVALID):
        super().__init__(reason)
        self.user_message = user_message


# ---------- Данные ----------

@dataclass(frozen=True)
class Bakery:
    id: str
    city: str
    address: str
    hours: tuple[str, str] | None = None


@dataclass(frozen=True)
class Promo:
    code: str
    title: str
    percent: int
    scope: str            # "all" - весь заказ, "coffee" - только позиции с coffee: true
    valid_from: date
    valid_to: date        # включительно
    before: str | None = None  # "10:00" - код действует только при заказе до этого времени


@dataclass(frozen=True)
class Menu:
    version: str
    items: dict[str, dict[str, Any]]
    coffee_ids: frozenset[str]
    stamps_per_reward: int  # сколько оплаченных кофе дают один бесплатный (каждый 6-й в подарок -> 5)
    max_qty: int
    max_lines: int
    name_max: int
    comment_max: int
    hours: tuple[str, str]
    prep: timedelta
    slot_step: timedelta
    slot_count: int
    tz: timezone


def load_menu(path: Path = DEFAULT_MENU_FILE) -> Menu:
    data = json.loads(path.read_text(encoding="utf-8"))
    items = {it["id"]: it for it in data["items"]}
    if not items:
        raise ValueError(f"в {path} пустое меню")
    limits, cfg = data["limits"], data["bakery"]
    return Menu(
        version=str(data.get("version", "")),
        items=items,
        coffee_ids=frozenset(i for i, it in items.items() if it.get("coffee")),
        stamps_per_reward=int(data.get("bonus", {}).get("coffeeEvery", 6)) - 1,
        max_qty=int(limits["maxQty"]),
        max_lines=int(limits["maxLines"]),
        name_max=int(limits["nameMax"]),
        comment_max=int(limits["commentMax"]),
        hours=(cfg["hours"]["open"], cfg["hours"]["close"]),
        prep=timedelta(minutes=cfg["prepMinutes"]),
        slot_step=timedelta(minutes=cfg["slotStep"]),
        slot_count=int(cfg["slotCount"]),
        tz=timezone(timedelta(minutes=data["timezone"]["utcOffsetMinutes"])),
    )


def load_bakeries(path: Path = DEFAULT_BAKERIES_FILE) -> dict[str, Bakery]:
    """Читает тот же список, что и мини-апп: JSON-массив внутри webapp/bakeries.js."""
    match = BAKERIES_ARRAY_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"в {path} не найден массив window.BAKERIES")
    bakeries = {}
    for row in json.loads(match.group(1)):
        hours = HOURS_RE.match(row.get("hours") or "")
        bakeries[row["id"]] = Bakery(
            id=row["id"], city=row["city"], address=row["address"],
            hours=(hours.group(1), hours.group(2)) if hours else None,
        )
    if not bakeries:
        raise ValueError(f"в {path} пустой список пекарен")
    return bakeries


def normalize_code(code: str) -> str:
    return code.strip().upper()


def promo_hash(code: str) -> str:
    """Тот же хеш считает мини-апп (crypto.subtle) для предпросмотра скидки."""
    return hashlib.sha256(normalize_code(code).encode("utf-8")).hexdigest()


def load_promos(path: Path = DEFAULT_PROMOS_FILE) -> dict[str, Promo]:
    data = json.loads(path.read_text(encoding="utf-8"))
    promos = {}
    for raw_code, p in data["codes"].items():
        code = normalize_code(raw_code)
        if not PROMO_CODE_RE.match(code):
            raise ValueError(f"промокод {raw_code!r}: допустимы буквы, цифры, _ и -, до 24 символов")
        if p["scope"] not in ("all", "coffee") or not 0 < int(p["percent"]) <= 100:
            raise ValueError(f"промокод {code}: scope all/coffee и percent от 1 до 100")
        if p.get("before"):
            time.fromisoformat(p["before"])  # проверка формата ЧЧ:ММ
        promos[code] = Promo(
            code=code, title=p["title"], percent=int(p["percent"]), scope=p["scope"],
            valid_from=date.fromisoformat(p["valid_from"]), valid_to=date.fromisoformat(p["valid_to"]),
            before=p.get("before") or None,
        )
    return promos


# ---------- Заказ ----------

@dataclass(frozen=True)
class OrderLine:
    item_id: str
    title: str
    options: str
    qty: int
    price: int  # цена за штуку по меню бота

    @property
    def sum(self) -> int:
        return self.qty * self.price


@dataclass(frozen=True)
class Order:
    lines: list[OrderLine]
    bakery: Bakery
    pickup: datetime  # во времени пекарни
    name: str
    comment: str
    promo_code: str | None = None
    client_discount: int | None = None  # скидка, которую показал мини-апп; None - не проверял

    @property
    def total(self) -> int:
        """Сумма по ценам меню, до скидок."""
        return sum(line.sum for line in self.lines)


@dataclass(frozen=True)
class Pricing:
    subtotal: int
    promo: Promo | None
    promo_discount: int
    bonus_title: str | None  # какой кофе отдали в подарок
    bonus_discount: int
    coffee_qty: int           # всего чашек кофе в заказе

    @property
    def total(self) -> int:
        return self.subtotal - self.promo_discount - self.bonus_discount

    @property
    def paid_coffee(self) -> int:
        """Чашки, которые пойдут в штампы: подарочная не считается."""
        return self.coffee_qty - (1 if self.bonus_title else 0)


def fingerprint(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _int(value: object) -> int | None:
    # bool - подкласс int, отсекаем явно
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object, field: str, limit: int, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise OrderError(f"{field}: ожидалась строка")
    value = " ".join(value.split())
    if required and not value:
        raise OrderError(f"{field}: пусто")
    if len(value) > limit:
        raise OrderError(
            f"{field}: {len(value)} символов при лимите {limit}",
            f"✂️ Слишком длинный текст: {'имя' if field == 'name' else 'комментарий'} не длиннее {limit} символов. "
            "Откройте меню и сократите его.",
        )
    return value


def price_line(item: dict[str, Any], opts: object) -> tuple[int, str]:
    """Цена за штуку и подпись опций по меню. Неизвестная или пропущенная опция - заказ по старому меню."""
    if not isinstance(opts, dict):
        raise OrderError(f"{item['id']}: opts не объект", MSG_MENU_CHANGED)
    groups = item.get("options") or []
    unknown = set(opts) - {g["id"] for g in groups}
    if unknown:
        raise OrderError(f"{item['id']}: неизвестные группы опций {sorted(unknown)}", MSG_MENU_CHANGED)
    price, labels = item["price"], []
    for group in groups:
        choice_id = opts.get(group["id"])
        if choice_id is None:
            if group.get("required"):
                raise OrderError(f"{item['id']}: не выбрано {group['id']}", MSG_MENU_CHANGED)
            continue
        choice = next((c for c in group["choices"] if c["id"] == choice_id), None)
        if choice is None:
            raise OrderError(f"{item['id']}: неизвестный вариант {group['id']}={choice_id!r:.40}", MSG_MENU_CHANGED)
        price += choice["delta"]
        if choice["id"] != "none":
            labels.append(choice["title"])
    return price, ", ".join(labels)


def build_slots(now: datetime, hours: tuple[str, str], menu: Menu, count: int | None = None) -> list[datetime]:
    """Слоты самовывоза. Повторяет buildSlots из webapp/app.js, менять их нужно вместе."""
    step, tz = menu.slot_step, menu.tz
    count = menu.slot_count if count is None else count
    local = now.astimezone(tz)
    result: list[datetime] = []
    for day_offset in (0, 1):
        if result:
            break
        day = local.date() + timedelta(days=day_offset)
        opening = datetime.combine(day, time.fromisoformat(hours[0]), tz)
        last = datetime.combine(day, time.fromisoformat(hours[1]), tz) - step
        if day_offset == 0:
            # Округление вверх до шага от полуночи UTC, как в JS (сдвиг пояса кратен шагу)
            secs = (local + menu.prep).timestamp() + tz.utcoffset(None).total_seconds()
            step_s = step.total_seconds()
            t = datetime.fromtimestamp(math.ceil(secs / step_s) * step_s - tz.utcoffset(None).total_seconds(), tz)
        else:
            t = opening
        t = max(t, opening)
        while t <= last and len(result) < count:
            result.append(t)
            t += step
    return result


def check_pickup(pickup: object, bakery: Bakery, menu: Menu, now: datetime) -> datetime:
    if not isinstance(pickup, dict):
        raise OrderError("pickup: не объект")
    date_s, time_s = pickup.get("date"), pickup.get("time")
    if not isinstance(time_s, str) or not TIME_RE.match(time_s):
        raise OrderError("pickup.time: не ЧЧ:ММ")
    try:
        day = date.fromisoformat(str(date_s))
    except ValueError as e:
        raise OrderError("pickup.date: не дата") from e
    when = datetime.combine(day, time.fromisoformat(time_s), menu.tz)
    if when <= now:
        raise OrderError(f"pickup: {when:%d.%m %H:%M} уже прошло", MSG_SLOT)
    # Слоты, которые гость мог видеть: построенные от "сейчас" и от "сейчас минус запас".
    # Нужны оба списка: вечером ранний список ещё сегодняшний, а текущий уже на завтра.
    hours = bakery.hours or menu.hours
    extra = math.ceil(SLOT_GRACE / menu.slot_step)
    allowed = set(build_slots(now, hours, menu)) | set(build_slots(now - SLOT_GRACE, hours, menu, menu.slot_count + extra))
    if when not in allowed:
        raise OrderError(f"pickup: {when:%d.%m %H:%M} не из допустимых слотов", MSG_SLOT)
    return when


def parse_order(raw: str, menu: Menu, bakeries: dict[str, Bakery], now: datetime) -> Order:
    """Разбирает и проверяет заказ. Любое несоответствие - OrderError с текстом для гостя."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise OrderError("не JSON") from e
    if not isinstance(data, dict):
        raise OrderError("корень не объект")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise OrderError("items: пустой или не список")
    if len(raw_items) > menu.max_lines:
        raise OrderError(
            f"items: {len(raw_items)} позиций",
            f"🧺 В одном заказе не больше {menu.max_lines} позиций. Откройте меню и уберите лишнее.",
        )

    lines = []
    for n, it in enumerate(raw_items, 1):
        if not isinstance(it, dict):
            raise OrderError(f"items[{n}]: не объект")
        item = menu.items.get(it.get("id")) if isinstance(it.get("id"), str) else None
        if item is None:
            raise OrderError(f"items[{n}]: неизвестный id {it.get('id')!r:.40}", MSG_MENU_CHANGED)
        qty = _int(it.get("qty"))
        if qty is None or not 1 <= qty <= menu.max_qty:
            raise OrderError(
                f"items[{n}].qty: {it.get('qty')!r:.20}",
                f"🧺 Одной позиции можно заказать от 1 до {menu.max_qty} штук. Откройте меню и поправьте количество.",
            )
        price, label = price_line(item, it.get("opts", {}))
        if _int(it.get("price")) != price:
            raise OrderError(f"items[{n}] {item['id']}: цена {it.get('price')!r:.20}, по меню {price}", MSG_MENU_CHANGED)
        lines.append(OrderLine(item_id=item["id"], title=item["title"], options=label, qty=qty, price=price))

    # subtotal - сумма по ценам до скидок. Старые версии мини-аппа присылали только total
    subtotal = sum(line.sum for line in lines)
    client_subtotal = data.get("subtotal", data.get("total"))
    if _int(client_subtotal) != subtotal:
        raise OrderError(f"subtotal: {client_subtotal!r:.20}, по меню {subtotal}", MSG_MENU_CHANGED)

    promo_raw = data.get("promo")
    if promo_raw is not None and not isinstance(promo_raw, str):
        raise OrderError("promo: ожидалась строка")
    promo_code = normalize_code(promo_raw)[:40] if promo_raw and promo_raw.strip() else None
    client_discount = data.get("discount")
    if client_discount is not None and (_int(client_discount) is None or not 0 <= client_discount <= subtotal):
        raise OrderError(f"discount: {client_discount!r:.20}")
    if "subtotal" in data and _int(data.get("total")) != subtotal - (client_discount or 0):
        raise OrderError(f"total: {data.get('total')!r:.20} при subtotal {subtotal} и скидке {client_discount}")

    # Адрес берём из своего списка по id, а не из присланных данных
    raw_bakery = data.get("bakery")
    bakery = bakeries.get(raw_bakery.get("id")) if isinstance(raw_bakery, dict) and isinstance(raw_bakery.get("id"), str) else None
    if bakery is None:
        bad_id = raw_bakery.get("id") if isinstance(raw_bakery, dict) else raw_bakery
        raise OrderError(f"bakery: неизвестная пекарня {bad_id!r:.40}", MSG_BAKERY)

    return Order(
        lines=lines,
        bakery=bakery,
        pickup=check_pickup(data.get("pickup"), bakery, menu, now),
        name=_text(data.get("name"), "name", menu.name_max, required=True),
        comment=_text(data.get("comment"), "comment", menu.comment_max, required=False),
        promo_code=promo_code,
        client_discount=client_discount,
    )


# ---------- Скидки ----------
# Правила повторены в webapp/app.js (promoPreview), менять их нужно вместе:
# скидка в процентах округляется вниз до рубля и считается от цен меню, без учёта бонуса.

def check_promo(code: str, promos: dict[str, Promo], now: datetime, menu: Menu) -> Promo:
    """Действует ли промокод сейчас. Иначе OrderError с понятным текстом для гостя."""
    shown = escape(code[:24])
    promo = promos.get(code) if PROMO_CODE_RE.match(code) else None
    if promo is None:
        raise OrderError(f"promo: неизвестный код {code!r:.30}",
                         f"🏷 Промокод «{shown}» не найден. Проверьте написание." + _PROMO_FIX)
    local = now.astimezone(menu.tz)
    if local.date() < promo.valid_from:
        raise OrderError(f"promo {code}: начнёт действовать {promo.valid_from}",
                         f"🏷 Промокод «{shown}» начнёт действовать {promo.valid_from:%d.%m.%Y}." + _PROMO_FIX)
    if local.date() > promo.valid_to:
        raise OrderError(f"promo {code}: истёк {promo.valid_to}",
                         f"🏷 Срок действия промокода «{shown}» закончился {promo.valid_to:%d.%m.%Y}." + _PROMO_FIX)
    if promo.before and local.time() >= time.fromisoformat(promo.before):
        raise OrderError(f"promo {code}: только до {promo.before}, сейчас {local:%H:%M}",
                         f"☕ Промокод «{shown}» действует при заказе до {promo.before}." + _PROMO_FIX)
    return promo


def promo_applies(promo: Promo, item_id: str, menu: Menu) -> bool:
    return promo.scope == "all" or item_id in menu.coffee_ids


def price_order(order: Order, menu: Menu, promos: dict[str, Promo], now: datetime, free_coffee: bool) -> Pricing:
    """Итоговая сумма заказа: промокод и подарочный кофе. free_coffee - есть ли у гостя заработанный подарок."""
    subtotal = order.total
    coffee_lines = [line for line in order.lines if line.item_id in menu.coffee_ids]
    coffee_qty = sum(line.qty for line in coffee_lines)

    promo, promo_discount = None, 0
    if order.promo_code:
        promo = check_promo(order.promo_code, promos, now, menu)
        eligible = sum(line.sum for line in order.lines if promo_applies(promo, line.item_id, menu))
        if eligible == 0:
            raise OrderError(f"promo {promo.code}: нет подходящих позиций",
                             f"☕ Промокод «{escape(promo.code)}» действует только на кофе, а в заказе его нет." + _PROMO_FIX)
        promo_discount = eligible * promo.percent // 100

    # Мини-апп показывал свою скидку: если она разошлась с нашей, гость должен увидеть настоящую сумму
    if order.client_discount is not None and order.client_discount != promo_discount:
        raise OrderError(f"discount: мини-апп {order.client_discount}, бот {promo_discount}", MSG_PROMO_CHANGED)

    # Подарок - самый дорогой кофе в заказе. Скидка по промокоду на эту чашку уже дана,
    # поэтому бонус обнуляет только остаток её цены
    bonus_title, bonus_discount = None, 0
    if free_coffee and coffee_lines:
        gift = max(coffee_lines, key=lambda line: line.price)
        promo_part = gift.price * promo.percent // 100 if promo and promo_applies(promo, gift.item_id, menu) else 0
        bonus_title = gift.title + (f", {gift.options}" if gift.options else "")
        bonus_discount = gift.price - promo_part

    return Pricing(subtotal=subtotal, promo=promo, promo_discount=promo_discount,
                   bonus_title=bonus_title, bonus_discount=bonus_discount, coffee_qty=coffee_qty)
