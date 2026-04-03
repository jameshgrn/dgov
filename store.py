import sqlite3
from datetime import datetime
from pathlib import Path

from wegmans.config import DB_PATH
from wegmans.models import GroceryItem, GroceryList

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS lists (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit TEXT,
    department TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
"""


class ListStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DB_PATH
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def save_list(self, grocery_list: GroceryList) -> None:
        c = self._conn
        c.execute(
            "INSERT OR REPLACE INTO lists (id, name, created_at, updated_at, owner) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                grocery_list.id,
                grocery_list.name,
                grocery_list.created_at.isoformat(),
                grocery_list.updated_at.isoformat(),
                grocery_list.owner,
            ),
        )
        c.execute("DELETE FROM items WHERE list_id = ?", (grocery_list.id,))
        for item in grocery_list.items:
            c.execute(
                "INSERT INTO items (id, list_id, name, quantity, unit, department, checked, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    grocery_list.id,
                    item.name,
                    item.quantity,
                    item.unit,
                    item.department,
                    int(item.checked),
                    item.notes,
                ),
            )
        c.commit()

    def _row_to_list(self, row: tuple, items_rows: list[tuple]) -> GroceryList:
        items = [
            GroceryItem(
                id=r[0],
                name=r[2],
                quantity=r[3],
                unit=r[4],
                department=r[5],
                checked=bool(r[6]),
                notes=r[7],
            )
            for r in items_rows
        ]
        return GroceryList(
            id=row[0],
            name=row[1],
            created_at=datetime.fromisoformat(row[2]),
            updated_at=datetime.fromisoformat(row[3]),
            owner=row[4],
            items=items,
        )

    def load_list(self, list_id: str) -> GroceryList | None:
        row = self._conn.execute(
            "SELECT * FROM lists WHERE id = ?", (list_id,)
        ).fetchone()
        if row is None:
            return None
        items_rows = self._conn.execute(
            "SELECT * FROM items WHERE list_id = ?", (list_id,)
        ).fetchall()
        return self._row_to_list(row, items_rows)

    def load_list_by_name(self, name: str) -> GroceryList | None:
        row = self._conn.execute(
            "SELECT * FROM lists WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            return None
        items_rows = self._conn.execute(
            "SELECT * FROM items WHERE list_id = ?", (row[0],)
        ).fetchall()
        return self._row_to_list(row, items_rows)

    def all_lists(self) -> list[GroceryList]:
        rows = self._conn.execute(
            "SELECT * FROM lists ORDER BY updated_at DESC"
        ).fetchall()
        result = []
        for row in rows:
            items_rows = self._conn.execute(
                "SELECT * FROM items WHERE list_id = ?", (row[0],)
            ).fetchall()
            result.append(self._row_to_list(row, items_rows))
        return result

    def delete_list(self, list_id: str) -> bool:
        self._conn.execute("DELETE FROM items WHERE list_id = ?", (list_id,))
        cursor = self._conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def search_items(self, query: str) -> list[tuple[GroceryList, GroceryItem]]:
        pattern = f"%{query}%"
        rows = self._conn.execute(
            "SELECT i.*, l.* FROM items i "
            "JOIN lists l ON i.list_id = l.id "
            "WHERE i.name LIKE ? OR i.notes LIKE ?",
            (pattern, pattern),
        ).fetchall()
        results: list[tuple[GroceryList, GroceryItem]] = []
        for r in rows:
            item = GroceryItem(
                id=r[0],
                name=r[2],
                quantity=r[3],
                unit=r[4],
                department=r[5],
                checked=bool(r[6]),
                notes=r[7],
            )
            gl = GroceryList(
                id=r[8],
                name=r[9],
                created_at=datetime.fromisoformat(r[10]),
                updated_at=datetime.fromisoformat(r[11]),
                owner=r[12],
                items=[],
            )
            results.append((gl, item))
        return results
