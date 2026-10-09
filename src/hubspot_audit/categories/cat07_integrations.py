"""Category 7: Integrations. No reliable API coverage; everything comes from the checklist."""

from __future__ import annotations

from ..context import AuditContext
from . import CategoryResult

NUMBER = 7
NAME = "Integrations"
MANUAL_ITEMS = [
    "Connected apps with sync status",
    "Sync error details",
    "Private apps and service keys list",
]


def run(ctx: AuditContext) -> CategoryResult:  # noqa: ARG001 - uniform category signature
    """Return an empty metric set; all inputs are manual."""
    return CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
