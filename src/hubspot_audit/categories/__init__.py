"""One module per audit category. Each exposes ``run(ctx) -> CategoryResult``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CategoryResult:
    """A category's slice of the bundle."""

    number: int
    name: str
    metrics: list[dict[str, Any]] = field(default_factory=list)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    manual_items: list[str] = field(default_factory=list)


def registry() -> dict[int, tuple[str, object]]:
    """Map category number -> (name, module). Imported lazily to avoid import cycles."""
    from . import (
        cat01_foundation,
        cat02_lead_management,
        cat03_sales_process,
        cat04_marketing,
        cat05_reporting,
        cat06_workflows,
        cat07_integrations,
        cat08_billing,
        cat09_privacy,
        cat10_adoption,
    )

    modules = [
        cat01_foundation,
        cat02_lead_management,
        cat03_sales_process,
        cat04_marketing,
        cat05_reporting,
        cat06_workflows,
        cat07_integrations,
        cat08_billing,
        cat09_privacy,
        cat10_adoption,
    ]
    return {m.NUMBER: (m.NAME, m) for m in modules}
