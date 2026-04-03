from collections import defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

WEGMANS_DEPARTMENTS: list[str] = [
    "Bakery",
    "Beverages",
    "Dairy",
    "Deli",
    "Frozen",
    "Grocery",
    "Health & Beauty",
    "Household",
    "Meat",
    "Natural/Organic",
    "Produce",
    "Seafood",
]


class GroceryItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    quantity: int = 1
    unit: str | None = None
    department: str | None = None
    checked: bool = False
    notes: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GroceryList(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    items: list[GroceryItem] = []
    owner: str = "jake"

    def _touch(self) -> None:
        self.updated_at = _now()

    def add_item(
        self,
        name: str,
        quantity: int = 1,
        unit: str | None = None,
        department: str | None = None,
        notes: str | None = None,
    ) -> GroceryItem:
        item = GroceryItem(
            name=name, quantity=quantity, unit=unit, department=department, notes=notes
        )
        self.items.append(item)
        self._touch()
        return item

    def remove_item(self, item_id: str) -> bool:
        for i, item in enumerate(self.items):
            if item.id == item_id:
                self.items.pop(i)
                self._touch()
                return True
        return False

    def check_item(self, item_id: str) -> bool:
        for item in self.items:
            if item.id == item_id:
                item.checked = True
                self._touch()
                return True
        return False

    def uncheck_item(self, item_id: str) -> bool:
        for item in self.items:
            if item.id == item_id:
                item.checked = False
                self._touch()
                return True
        return False

    def items_by_department(self) -> dict[str, list[GroceryItem]]:
        groups: dict[str, list[GroceryItem]] = defaultdict(list)
        for item in self.items:
            dept = item.department or "Uncategorized"
            groups[dept].append(item)
        for items in groups.values():
            items.sort(key=lambda i: i.name.lower())
        return dict(sorted(groups.items()))

    def summary(self) -> str:
        by_dept = self.items_by_department()
        total = len(self.items)
        checked = sum(1 for i in self.items if i.checked)
        lines = [
            f"[bold]{self.name}[/bold] — {checked}/{total} checked",
            "",
        ]
        for dept, items in by_dept.items():
            lines.append(f"  [cyan]{dept}[/cyan] ({len(items)})")
            for item in items:
                mark = "[green]x[/green]" if item.checked else " "
                qty = f" x{item.quantity}" if item.quantity > 1 else ""
                unit = f" {item.unit}" if item.unit else ""
                lines.append(f"    [{mark}] {item.name}{qty}{unit}")
        return "\n".join(lines)
