"""Заказы в SQLite: сквозная нумерация после перезапуска, антиспам и счётчики для /health.

Бот работает в одном процессе и одном потоке asyncio, а запросы к базе синхронные и короткие,
поэтому проверка лимита и запись заказа не перемешиваются с другими апдейтами.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

FIRST_ORDER_NUMBER = 1001


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS orders (
                number     INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                created_at REAL    NOT NULL,
                total      INTEGER NOT NULL,
                bakery_id  TEXT    NOT NULL,
                payload    TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS orders_user_time ON orders (user_id, created_at);
            """
        )
        # Нумерация с 1001, чтобы первый заказ не выглядел как "№1"
        if self.db.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'orders'").fetchone() is None:
            self.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('orders', ?)", (FIRST_ORDER_NUMBER - 1,))
        self.db.commit()

    def add_order(self, user_id: int, total: int, bakery_id: str, payload: str) -> int:
        """Сохраняет принятый заказ и возвращает его номер."""
        cur = self.db.execute(
            "INSERT INTO orders (user_id, created_at, total, bakery_id, payload) VALUES (?, ?, ?, ?, ?)",
            (user_id, time.time(), total, bakery_id, payload),
        )
        self.db.commit()
        return cur.lastrowid

    def user_orders_since(self, user_id: int, since: float) -> list[float]:
        """Время принятых заказов гостя начиная с since, по возрастанию."""
        rows = self.db.execute(
            "SELECT created_at FROM orders WHERE user_id = ? AND created_at >= ? ORDER BY created_at",
            (user_id, since),
        ).fetchall()
        return [r[0] for r in rows]

    def count_since(self, since: float) -> tuple[int, int]:
        """Число заказов и их сумма начиная с since."""
        count, total = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE created_at >= ?", (since,)
        ).fetchone()
        return count, total

    def last_number(self) -> int | None:
        row = self.db.execute("SELECT MAX(number) FROM orders").fetchone()
        return row[0]

    def close(self) -> None:
        self.db.close()
