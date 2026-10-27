"""Выгружает правила промокодов для предпросмотра скидки в мини-аппе.

Мини-апп статичный и не может спросить бота, поэтому получает файл webapp/promo-codes.json:
вместо самих кодов там SHA-256 от кода в верхнем регистре. Мини-апп хеширует то, что ввёл гость,
и находит правило. Хеш прячет список кодов от случайного просмотра, но короткий код можно
подобрать перебором, так что секретом это не считается. Итоговую скидку всё равно считает бот.

Запуск после любой правки bot/promos.json:
    python bot/export_promos.py
"""

from __future__ import annotations

import json
from pathlib import Path

from order import WEBAPP_DIR, load_promos, promo_hash

PROMOS_FILE = Path(__file__).resolve().parent / "promos.json"
EXPORT_FILE = WEBAPP_DIR / "promo-codes.json"


def build_export(promos_file: Path = PROMOS_FILE) -> str:
    rules = []
    for code, promo in sorted(load_promos(promos_file).items()):
        rules.append({
            "hash": promo_hash(code),
            "title": promo.title,
            "percent": promo.percent,
            "scope": promo.scope,
            "before": promo.before,
            "validFrom": promo.valid_from.isoformat(),
            "validTo": promo.valid_to.isoformat(),
        })
    data = {
        "_about": "Сгенерировано bot/export_promos.py из bot/promos.json, руками не править. "
                  "Только для предпросмотра скидки: итоговую сумму считает бот.",
        "codes": rules,
    }
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    EXPORT_FILE.write_text(build_export(), encoding="utf-8")
    print(f"Записано {EXPORT_FILE}")
