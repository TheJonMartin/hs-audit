"""Category 6: Workflows."""

from __future__ import annotations

from datetime import timedelta

from ..context import STALE_WORKFLOW_DAYS, AuditContext
from ..metrics import distribution, metric, parse_hs_datetime, safe_many
from . import CategoryResult

NUMBER = 6
NAME = "Workflows"
MANUAL_ITEMS = [
    "Workflow error details (check the API first; the list endpoint does not expose errors)",
    "Enrollment counts (verify API coverage)",
]
SOURCE = "/automation/v4/flows"


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Workflow inventory metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    keys = [
        ("workflows.total", "count"),
        ("workflows.by_status", "count_by_status"),
        ("workflows.by_type", "count_by_type"),
        ("workflows.stale_but_active", "count"),
    ]

    def compute() -> list[dict]:
        flows = ctx.workflows()
        cutoff = ctx.now - timedelta(days=STALE_WORKFLOW_DAYS)
        rows = []
        stale_active = 0
        for flow in flows:
            updated = parse_hs_datetime(flow.get("updatedAt"))
            enabled = bool(flow.get("isEnabled"))
            is_stale = bool(enabled and updated and updated < cutoff)
            stale_active += is_stale
            rows.append(
                {
                    "id": flow.get("id"),
                    "name": flow.get("name"),
                    "enabled": enabled,
                    "flow_type": flow.get("flowType"),
                    "object_type_id": flow.get("objectTypeId"),
                    "created_at": flow.get("createdAt"),
                    "updated_at": flow.get("updatedAt"),
                    "stale_but_active": is_stale,
                }
            )
        result.tables["workflow_list"] = rows
        return [
            metric("workflows.total", len(flows), "count", SOURCE),
            metric(
                "workflows.by_status",
                distribution("active" if r["enabled"] else "inactive" for r in rows),
                "count_by_status",
                SOURCE,
            ),
            metric(
                "workflows.by_type",
                distribution(r["flow_type"] for r in rows),
                "count_by_type",
                SOURCE,
            ),
            metric(
                "workflows.stale_but_active",
                stale_active,
                "count",
                SOURCE,
                note="Active and not updated in about six months.",
            ),
        ]

    result.metrics.extend(safe_many(SOURCE, compute, keys))
    return result
