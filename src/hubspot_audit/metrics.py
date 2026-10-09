"""Shared metric helpers: metric records, population rates, aging, dedupe, distributions."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from .errors import AuthError, ForbiddenError, HubSpotError, UnsupportedError

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FORBIDDEN = "forbidden"
STATUS_UNSUPPORTED = "unsupported"
STATUS_ERROR = "error"
STATUSES = (STATUS_OK, STATUS_EMPTY, STATUS_FORBIDDEN, STATUS_UNSUPPORTED, STATUS_ERROR)
MS_PER_SECOND = 1000
SECONDS_PER_DAY = 86400
PERCENT = 100.0


def metric(
    key: str,
    value: Any,
    unit: str,
    source: str,
    status: str = STATUS_OK,
    note: str | None = None,
) -> dict[str, Any]:
    """Build one bundle metric. ``key`` must match ``^[a-z0-9_.]+$`` (enforced by the schema)."""
    record: dict[str, Any] = {
        "key": key,
        "value": value,
        "unit": unit,
        "source": source,
        "status": status,
    }
    if note:
        record["note"] = note
    return record


def status_for_error(exc: HubSpotError) -> str:
    """Map an API exception to a metric status."""
    if isinstance(exc, ForbiddenError):
        return STATUS_FORBIDDEN
    if isinstance(exc, UnsupportedError):
        return STATUS_UNSUPPORTED
    return STATUS_ERROR


def failed(key: str, unit: str, source: str, exc: HubSpotError) -> dict[str, Any]:
    """A gap entry for an endpoint that failed. The value is null, never zero."""
    return metric(key, None, unit, source, status_for_error(exc), note=f"HTTP {exc.status}")


def safe(
    key: str, unit: str, source: str, compute: Callable[[], Any], note: str | None = None
) -> dict[str, Any]:
    """Run ``compute``; convert per-endpoint API failures into a gap metric.

    ``AuthError`` is re-raised because a rejected key makes the whole bundle untrustworthy.
    """
    try:
        value = compute()
    except AuthError:
        raise
    except HubSpotError as exc:
        return failed(key, unit, source, exc)
    return metric(key, value, unit, source, note=note)


def safe_many(
    source: str, compute: Callable[[], list[dict[str, Any]]], fallback_keys: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Run a computation that yields several metrics from one fetch.

    On API failure, every ``(key, unit)`` in ``fallback_keys`` becomes a gap entry.
    """
    try:
        return compute()
    except AuthError:
        raise
    except HubSpotError as exc:
        return [failed(key, unit, source, exc) for key, unit in fallback_keys]


def pct(part: int, whole: int) -> float | None:
    """Percentage rounded to one decimal, or None when ``whole`` is zero."""
    if not whole:
        return None
    return round(part / whole * PERCENT, 1)


def population_rates(
    records: list[dict[str, Any]], properties: Iterable[str]
) -> dict[str, float | None]:
    """Percent of ``records`` with a non-empty value, per property."""
    total = len(records)
    rates: dict[str, float | None] = {}
    for prop in properties:
        filled = sum(1 for r in records if _has_value((r.get("properties") or {}).get(prop)))
        rates[prop] = pct(filled, total)
    return rates


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def parse_hs_datetime(value: Any) -> datetime | None:
    """Parse a HubSpot timestamp (ISO 8601 or epoch milliseconds) into an aware datetime."""
    if not _has_value(value):
        return None
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / MS_PER_SECOND, tz=timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def days_since(value: Any, now: datetime) -> float | None:
    """Days between ``value`` and ``now``; None when the value is missing or unparseable."""
    parsed = parse_hs_datetime(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / SECONDS_PER_DAY


def to_epoch_ms(moment: datetime) -> str:
    """Epoch milliseconds as a string, the format the search API filters expect."""
    return str(int(moment.timestamp() * MS_PER_SECOND))


def distribution(values: Iterable[Any], missing_label: str = "(none)") -> dict[str, int]:
    """Count occurrences, labelling empty values ``missing_label``. Sorted by count, descending."""
    counter = Counter(str(v) if _has_value(v) else missing_label for v in values)
    return dict(counter.most_common())


def average(values: list[float]) -> float | None:
    """Mean rounded to one decimal, or None for an empty list."""
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def duplicate_groups(
    records: list[dict[str, Any]], key_fn: Callable[[dict[str, Any]], str | None]
) -> dict[str, int]:
    """Count duplicate clusters without exposing the keys.

    Returns ``clusters`` (keys seen more than once), ``extra_records`` (records beyond the first
    in each cluster) and ``records_in_clusters``.
    """
    counter = Counter(k for k in (key_fn(r) for r in records) if k)
    dup_counts = [n for n in counter.values() if n > 1]
    return {
        "clusters": len(dup_counts),
        "extra_records": sum(n - 1 for n in dup_counts),
        "records_in_clusters": sum(dup_counts),
    }


def email_key(record: dict[str, Any]) -> str | None:
    """Duplicate key on normalized email."""
    email = _norm((record.get("properties") or {}).get("email"))
    return email or None


def name_company_key(record: dict[str, Any]) -> str | None:
    """Duplicate key on first + last name + company. Requires all three to be present."""
    props = record.get("properties") or {}
    parts = [_norm(props.get(p)) for p in ("firstname", "lastname", "company")]
    return "|".join(parts) if all(parts) else None
