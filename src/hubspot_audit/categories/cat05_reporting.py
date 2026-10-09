"""Category 5: Reporting. No reliable API coverage; everything comes from the checklist."""

from __future__ import annotations

from ..context import AuditContext
from . import CategoryResult

NUMBER = 5
NAME = "Reporting"
MANUAL_ITEMS = [
    "Dashboard list with owner and last viewed",
    "Report list",
    "Main dashboard screenshots",
    "External reporting tools in use (question)",
]


def run(ctx: AuditContext) -> CategoryResult:  # noqa: ARG001 - uniform category signature
    """Return an empty metric set; all inputs are manual."""
    return CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
