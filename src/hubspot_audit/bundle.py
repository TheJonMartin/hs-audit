"""Assemble, validate and write the audit bundle."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from . import __version__
from .categories import CategoryResult
from .errors import BundleValidationError

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
REDACTED_EMAIL = "[redacted-email]"
SCHEMA_RELATIVE_PATH = Path("schema") / "audit_bundle.schema.json"


def load_schema() -> dict[str, Any]:
    """Load the bundle schema from the repo's ``schema/`` directory (editable install)."""
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / SCHEMA_RELATIVE_PATH).read_text(encoding="utf-8"))


def assemble(
    results: list[CategoryResult],
    prospect: str,
    portal_id: str | None,
    now: datetime,
    areas_granted: list[str],
    probes: list[dict[str, str]],
    sample_size: int,
) -> dict[str, Any]:
    """Build the bundle dict from category results."""
    return {
        "meta": {
            "prospect": prospect,
            "portal_id": portal_id,
            "run_at": now.isoformat(),
            "script_version": __version__,
            "scopes_granted": areas_granted,
            "scope_probes": probes,
            "categories_run": [r.number for r in results],
            "contact_sample_size": sample_size,
        },
        "categories": {
            str(r.number): {
                "name": r.name,
                "metrics": r.metrics,
                "manual_items": r.manual_items,
            }
            for r in results
        },
    }


def validate(bundle: dict[str, Any], service_key: str) -> None:
    """Validate against the schema, require unique metric keys per category, and check the key is absent.

    Raises:
        BundleValidationError: any check fails.
    """
    try:
        jsonschema.validate(bundle, load_schema())
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path)
        raise BundleValidationError(
            f"Bundle failed schema validation at '{path}': {exc.message}"
        ) from exc
    # Reused metrics (e.g. subscriptions.types in Categories 4 and 9) keep the same key in each
    # category, so uniqueness is enforced per category.
    for number, category in bundle["categories"].items():
        seen: set[str] = set()
        for item in category["metrics"]:
            if item["key"] in seen:
                raise BundleValidationError(
                    f"Duplicate metric key in category {number}: {item['key']}"
                )
            seen.add(item["key"])
    if service_key and service_key in json.dumps(bundle):
        raise BundleValidationError("Service Key found in bundle output.")


def redact_cell(value: Any) -> tuple[Any, int]:
    """Replace email-like strings in a CSV cell. Returns the cell and the redaction count."""
    if not isinstance(value, str):
        return value, 0
    cleaned, count = EMAIL_PATTERN.subn(REDACTED_EMAIL, value)
    return cleaned, count


def write_outputs(
    bundle: dict[str, Any],
    tables: dict[str, list[dict[str, Any]]],
    output_root: Path,
    prospect: str,
    now: datetime,
) -> Path:
    """Write ``audit_bundle.json`` and the row-level CSVs. Returns the run directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", prospect.lower()).strip("-") or "prospect"
    run_dir = output_root / f"{slug}_{now.strftime('%Y-%m-%d')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    redactions = 0
    for name, rows in tables.items():
        if not rows:
            continue
        with (run_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            for row in rows:
                cleaned = {}
                for key, value in row.items():
                    cleaned[key], count = redact_cell(value)
                    redactions += count
                writer.writerow(cleaned)
    bundle["meta"]["csv_files"] = sorted(f"{n}.csv" for n, r in tables.items() if r)
    bundle["meta"]["csv_email_redactions"] = redactions
    (run_dir / "audit_bundle.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir
