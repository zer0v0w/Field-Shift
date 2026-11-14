"""Persistent on-farm storage inventory: seeds and plants.

The farmer records what they physically have on hand so recommendations can be
grounded in reality ("you already have chickpea seed") instead of assuming they
will buy everything.

Design notes
------------
* File-backed JSON store, atomic writes, process-wide lock.  The app is
  single-user, so this is enough; swap ``StorageStore`` for a database later
  without changing the API surface.
* Every write re-reads under the lock, so concurrent HTTP threads cannot clobber
  each other.
* Unknown fields are preserved under ``details`` rather than silently dropped.

Nothing here is farming advice; it is an inventory record.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = Path(
    os.environ.get("AGRI_STORAGE_PATH", PROJECT_ROOT / "data" / "storage" / "inventory.json")
)

ITEM_TYPES = ("seed", "plant")
SEED_UNITS = ("kg", "g", "seeds", "packets", "bags")
PLANT_UNITS = ("units", "plants", "trays", "bunches", "seedlings")
DEFAULT_UNITS = {"seed": "kg", "plant": "units"}

# Flat fields stored on every item, plus type-specific ones.
_COMMON_FIELDS = ("type", "crop", "variety", "quantity", "unit", "notes")
_SEED_FIELDS = (
    "germination_percent",
    "purchase_date",
    "expiry_date",
    "supplier",
    "storage_location",
    "lot_number",
)
_PLANT_FIELDS = (
    "growth_stage",
    "planted_date",
    "field",
    "health",
    "source",
)

_NUMERIC_FIELDS = ("quantity", "germination_percent")


class StorageError(ValueError):
    """Invalid inventory payload."""


class ItemNotFound(KeyError):
    """No item with that id."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _norm_crop(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()


def _as_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise StorageError(f"'{field}' must be a number") from None
    if number < 0:
        raise StorageError(f"'{field}' cannot be negative")
    return number


def _clean_text(value: Any, field: str, *, required: bool = False, max_len: int = 200) -> str:
    if value is None:
        if required:
            raise StorageError(f"'{field}' is required")
        return ""
    text = str(value).strip()
    if required and not text:
        raise StorageError(f"'{field}' is required")
    if len(text) > max_len:
        raise StorageError(f"'{field}' is too long (max {max_len} characters)")
    return text


def _clean_date(value: Any, field: str) -> str:
    text = _clean_text(value, field, max_len=40)
    if not text:
        return ""
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise StorageError(f"'{field}' must be an ISO date (YYYY-MM-DD)") from None
    return text


def _clean_item(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    """Validate/normalise a create or update payload."""
    if not isinstance(payload, dict):
        raise StorageError("Item payload must be a JSON object")

    item_type = _clean_text(payload.get("type"), "type").lower() or None
    if item_type is not None and item_type not in ITEM_TYPES:
        raise StorageError(f"'type' must be one of: {', '.join(ITEM_TYPES)}")
    if item_type is None and not partial:
        raise StorageError("'type' is required (seed or plant)")

    cleaned: dict[str, Any] = {}
    if item_type:
        cleaned["type"] = item_type

    if "crop" in payload:
        cleaned["crop"] = _clean_text(payload.get("crop"), "crop", required=True, max_len=120)
    elif not partial:
        raise StorageError("'crop' is required")

    if "variety" in payload:
        cleaned["variety"] = _clean_text(payload.get("variety"), "variety", max_len=120)

    if "quantity" in payload:
        cleaned["quantity"] = _as_number(payload.get("quantity"), "quantity")
    elif not partial:
        cleaned["quantity"] = 0.0

    if "unit" in payload:
        cleaned["unit"] = _clean_text(payload.get("unit"), "unit", max_len=20) or DEFAULT_UNITS.get(
            item_type or "seed", "units"
        )

    if "notes" in payload:
        cleaned["notes"] = _clean_text(payload.get("notes"), "notes", max_len=2000)

    if "germination_percent" in payload and payload.get("germination_percent") is not None:
        pct = _as_number(payload.get("germination_percent"), "germination_percent")
        if pct > 100:
            raise StorageError("'germination_percent' cannot exceed 100")
        cleaned["germination_percent"] = pct

    for field in ("purchase_date", "expiry_date", "planted_date"):
        if field in payload and payload.get(field) is not None:
            cleaned[field] = _clean_date(payload.get(field), field)

    for field in ("supplier", "storage_location", "lot_number", "growth_stage", "field", "health", "source"):
        if field in payload and payload.get(field) is not None:
            cleaned[field] = _clean_text(payload.get(field), field, max_len=200)

    # Preserve anything else the client sends, without letting it overwrite core keys.
    known = set(_COMMON_FIELDS) | set(_SEED_FIELDS) | set(_PLANT_FIELDS) | {
        "id",
        "created_at",
        "updated_at",
        "details",
    }
    extras = {k: v for k, v in payload.items() if k not in known}
    if "details" in payload and isinstance(payload["details"], dict):
        extras.update(payload["details"])
    if extras:
        cleaned["details"] = extras

    return cleaned


class StorageStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.RLock()
        self._ensure()

    # ---------------------------------------------------------------- plumbing
    def _ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"schema_version": "1.0", "items": []})

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"schema_version": "1.0", "items": []}
        except Exception:
            data = {"schema_version": "1.0", "items": []}
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            data = {"schema_version": "1.0", "items": []}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _items(self) -> list[dict[str, Any]]:
        return self._read()["items"]

    # ------------------------------------------------------------------- reads
    def list(
        self,
        *,
        item_type: str | None = None,
        crop: str | None = None,
        query: str | None = None,
        include_empty: bool = True,
    ) -> list[dict[str, Any]]:
        items = list(self._items())
        if item_type:
            items = [i for i in items if i.get("type") == item_type]
        if crop:
            needle = _norm_crop(crop)
            items = [i for i in items if _norm_crop(i.get("crop", "")) == needle]
        if not include_empty:
            items = [i for i in items if float(i.get("quantity") or 0) > 0]
        if query:
            needle = str(query).strip().lower()
            items = [
                i
                for i in items
                if needle in " ".join(
                    str(i.get(f, "")) for f in ("crop", "variety", "notes", "supplier", "storage_location")
                ).lower()
            ]
        return sorted(items, key=lambda i: i.get("updated_at", ""), reverse=True)

    def get(self, item_id: str) -> dict[str, Any]:
        for item in self._items():
            if item.get("id") == item_id:
                return item
        raise ItemNotFound(item_id)

    def summary(self) -> dict[str, Any]:
        items = self._items()
        by_type = {t: {"items": 0, "quantity": 0.0, "crops": set()} for t in ITEM_TYPES}
        by_crop: dict[str, dict[str, Any]] = {}
        expiring: list[dict[str, Any]] = []
        today = datetime.now(timezone.utc).date()

        for item in items:
            item_type = item.get("type") or "seed"
            bucket = by_type.setdefault(item_type, {"items": 0, "quantity": 0.0, "crops": set()})
            qty = float(item.get("quantity") or 0)
            bucket["items"] += 1
            bucket["quantity"] += qty
            bucket["crops"].add(item.get("crop", ""))

            crop_key = _norm_crop(item.get("crop", ""))
            crop_entry = by_crop.setdefault(
                crop_key, {"crop": item.get("crop", ""), "items": 0, "quantity": 0.0, "types": set()}
            )
            crop_entry["items"] += 1
            crop_entry["quantity"] += qty
            crop_entry["types"].add(item_type)

            expiry = item.get("expiry_date")
            if expiry:
                try:
                    days = (datetime.fromisoformat(expiry.replace("Z", "+00:00")).date() - today).days
                    if days <= 60:
                        expiring.append(
                            {
                                "id": item.get("id"),
                                "crop": item.get("crop"),
                                "variety": item.get("variety"),
                                "expiry_date": expiry,
                                "days_until_expiry": days,
                            }
                        )
                except ValueError:
                    pass

        def _clean(bucket: dict[str, Any]) -> dict[str, Any]:
            out = dict(bucket)
            if isinstance(out.get("crops"), set):
                out["crops"] = sorted(c for c in out["crops"] if c)
            if isinstance(out.get("types"), set):
                out["types"] = sorted(out["types"])
            out["quantity"] = round(out.get("quantity", 0.0), 3)
            return out

        return {
            "total_items": len(items),
            "by_type": {k: _clean(v) for k, v in by_type.items()},
            "by_crop": sorted((_clean(v) for v in by_crop.values()), key=lambda c: c["items"], reverse=True),
            "expiring_within_60_days": sorted(expiring, key=lambda e: e["days_until_expiry"]),
            "generated_at": _now(),
        }

    def crops(self) -> list[str]:
        """Distinct crops the farmer has on hand (for Groq crop-list defaults)."""
        seen: dict[str, str] = {}
        for item in self._items():
            name = str(item.get("crop") or "").strip()
            if name:
                seen.setdefault(_norm_crop(name), name)
        return sorted(seen.values())

    # ------------------------------------------------------------------ writes
    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = _clean_item(payload)
        now = _now()
        item = {
            "id": f"itm_{uuid.uuid4().hex[:12]}",
            **cleaned,
            "created_at": now,
            "updated_at": now,
        }
        item.setdefault("unit", DEFAULT_UNITS.get(item["type"], "units"))
        with self._lock:
            data = self._read()
            data["items"].append(item)
            self._write(data)
        return item

    def update(self, item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            raise StorageError("Update payload must be a non-empty JSON object")
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item.get("id") != item_id:
                    continue
                cleaned = _clean_item(patch, partial=True)
                merged = {**item, **cleaned}
                merged["id"] = item_id
                merged["created_at"] = item.get("created_at", _now())
                merged["updated_at"] = _now()
                data["items"][index] = merged
                self._write(data)
                return merged
        raise ItemNotFound(item_id)

    def delete(self, item_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item.get("id") == item_id:
                    removed = data["items"].pop(index)
                    self._write(data)
                    return removed
        raise ItemNotFound(item_id)

    def adjust(self, item_id: str, delta: float) -> dict[str, Any]:
        """Add/remove stock (e.g. -1.5 kg after sowing). Never goes below zero."""
        try:
            change = float(delta)
        except (TypeError, ValueError):
            raise StorageError("'delta' must be a number") from None
        with self._lock:
            data = self._read()
            for index, item in enumerate(data["items"]):
                if item.get("id") != item_id:
                    continue
                current = float(item.get("quantity") or 0)
                item["quantity"] = max(0.0, round(current + change, 3))
                item["updated_at"] = _now()
                data["items"][index] = item
                self._write(data)
                return item
        raise ItemNotFound(item_id)

    def replace_all(self, items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bulk import (used by tests / restore)."""
        created = [self.create(payload) for payload in items]
        return created


# --------------------------------------------------------------------------- #
# module singleton
# --------------------------------------------------------------------------- #

_STORE: StorageStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> StorageStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = StorageStore()
        return _STORE
