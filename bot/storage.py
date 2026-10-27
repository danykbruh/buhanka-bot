"""Заказы в SQLite: сквозная нумерация после перезапуска, статусы, антиспам и счётчики для /health.

Бот работает в одном процессе и одном потоке asyncio, а запросы к базе синхронные и короткие,
поэтому проверка лимита и запись заказа не перемешиваются с другими апдейтами.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

FIRST_ORDER_NUMBER = 1001

# Колонки, которых не было в первой версии базы: добавляются при запуске, данные не теряются
_MIGRATIONS = {
    "chat_id": "INTEGER",
    "status": "TEXT NOT NULL DEFAULT 'new'",
    "status_at": "REAL",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "pickup_at": "REAL",
    "admin_text": "TEXT NOT NULL DEFAULT ''",
}


@dataclass(frozen=True)
class StoredOrder:
    number: int
    user_id: int
    chat_id: int | None
    created_at: float
    total: int
    bakery_id: str
    status: str
    status_at: float | None
    summary: str
    pickup_at: float | None
    admin_text: str


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
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(orders)")}
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {column} {ddl}")
        # Нумерация с 1001, чтобы первый заказ не выглядел как "№1"
        if self.db.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'orders'").fetchone() is None:
            self.db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('orders', ?)", (FIRST_ORDER_NUMBER - 1,))
        self.db.commit()

    def add_order(self, user_id: int, total: int, bakery_id: str, payload: str, *, chat_id: int | None = None,
                  summary: str = "", pickup_at: float | None = None) -> int:
        """Сохраняет принятый заказ со статусом "new" и возвращает его номер."""
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO orders (user_id, created_at, total, bakery_id, payload, chat_id, status, status_at, summary, pickup_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
            (user_id, now, total, bakery_id, payload, chat_id, now, summary, pickup_at),
        )
        self.db.commit()
        return cur.lastrowid

    def set_admin_text(self, number: int, text: str) -> None:
        """Текст копии заказа у админа без строки статуса: по нему сообщение перерисовывается при смене статуса."""
        self.db.execute("UPDATE orders SET admin_text = ? WHERE number = ?", (text, number))
        self.db.commit()

    def set_status(self, number: int, status: str) -> StoredOrder | None:
        self.db.execute("UPDATE orders SET status = ?, status_at = ? WHERE number = ?", (status, time.time(), number))
        self.db.commit()
        return self.get_order(number)

    def get_order(self, number: int) -> StoredOrder | None:
        row = self.db.execute(f"SELECT {_COLUMNS} FROM orders WHERE number = ?", (number,)).fetchone()
        return StoredOrder(*row) if row else None

    def user_orders(self, user_id: int, limit: int = 5) -> list[StoredOrder]:
        rows = self.db.execute(
            f"SELECT {_COLUMNS} FROM orders WHERE user_id = ? ORDER BY number DESC LIMIT ?", (user_id, limit)
        ).fetchall()
        return [StoredOrder(*r) for r in rows]

    def user_orders_since(self, user_id: int, since: float) -> list[float]:
        """Время принятых заказов гостя начиная с since, по возрастанию."""
        rows = self.db.execute(
            "SELECT created_at FROM orders WHERE user_id = ? AND created_at >= ? ORDER BY created_at",
            (user_id, since),
        ).fetchall()
        return [r[0] for r in rows]

    def count_since(self, since: float) -> tuple[int, int]:
        """Число заказов и их сумма начиная с since, без отменённых."""
        count, total = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE created_at >= ? AND status != 'cancelled'",
            (since,),
        ).fetchone()
        return count, total

    def last_number(self) -> int | None:
        row = self.db.execute("SELECT MAX(number) FROM orders").fetchone()
        return row[0]

    def close(self) -> None:
        self.db.close()


_COLUMNS = ("number, user_id, chat_id, created_at, total, bakery_id, status, status_at, summary, pickup_at, admin_text")
