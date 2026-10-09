"""Category 3: Sales Process."""

from __future__ import annotations

from ..context import ENGAGEMENT_OBJECTS, STALE_DAYS_LONG, STALE_DAYS_SHORT, AuditContext
from ..errors import HubSpotError
from ..metrics import (
    average,
    days_since,
    distribution,
    metric,
    parse_hs_datetime,
    safe,
    safe_many,
)
from . import CategoryResult

NUMBER = 3
NAME = "Sales Process"
MANUAL_ITEMS = ["Sequence open and reply rates (verify API coverage first)"]
OPEN_DEALS_SOURCE = "/crm/v3/objects/deals/search"


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Sales Process metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    result.metrics.extend(_deal_metrics(ctx, result))
    result.metrics.append(_engagement_metric(ctx))
    result.metrics.append(_sequence_metric(ctx))
    return result


def stage_labels(ctx: AuditContext) -> dict[str, str]:
    """Stage id -> 'Pipeline / Stage' label. Falls back to raw ids if pipelines are unavailable."""
    try:
        labels = {}
        for pipeline in ctx.deal_pipelines():
            for stage in pipeline.get("stages", []):
                labels[stage["id"]] = f"{pipeline.get('label')} / {stage.get('label')}"
        return labels
    except HubSpotError:
        return {}


def _deal_metrics(ctx: AuditContext, result: CategoryResult) -> list[dict]:
    keys = [
        ("deals.open.total", "count"),
        ("deals.open.by_stage", "count_by_stage"),
        ("deals.open.no_activity_14d", "count"),
        ("deals.open.no_activity_30d", "count"),
        ("deals.open.past_due_close_date", "count"),
        ("deals.open.avg_age_days_by_stage", "days_by_stage"),
        ("deals.open.forecast_category", "count_by_category"),
    ]

    def compute() -> list[dict]:
        deals = ctx.open_deals()
        total = ctx.client.count(
            "deals", [{"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}]
        )
        labels = stage_labels(ctx)
        owners = _owner_names_or_empty(ctx)
        truncated = total > len(deals)
        note = f"Pulled {len(deals)} of {total} open deals." if truncated else None
        by_stage: dict[str, list[float]] = {}
        no14 = no30 = past_due = 0
        rows = []
        for deal in deals:
            props = deal.get("properties") or {}
            stage = labels.get(props.get("dealstage"), props.get("dealstage") or "(none)")
            age = days_since(props.get("createdate"), ctx.now)
            if age is not None:
                by_stage.setdefault(stage, []).append(age)
            idle = days_since(props.get("notes_last_updated"), ctx.now)
            if idle is None or idle > STALE_DAYS_SHORT:
                no14 += 1
            if idle is None or idle > STALE_DAYS_LONG:
                no30 += 1
            close = parse_hs_datetime(props.get("closedate"))
            is_past_due = bool(close and close < ctx.now)
            past_due += is_past_due
            rows.append(
                {
                    "deal_name": props.get("dealname"),
                    "owner": owners.get(str(props.get("hubspot_owner_id")), "(unassigned)"),
                    "stage": stage,
                    "amount": props.get("amount"),
                    "close_date": props.get("closedate"),
                    "age_days": round(age, 1) if age is not None else None,
                    "days_since_last_activity": round(idle, 1) if idle is not None else None,
                    "past_due": is_past_due,
                }
            )
        result.tables["open_deals"] = rows
        stage_counts = distribution(r["stage"] for r in rows)
        return [
            metric("deals.open.total", total, "count", OPEN_DEALS_SOURCE, note=note),
            metric(
                "deals.open.by_stage", stage_counts, "count_by_stage", OPEN_DEALS_SOURCE, note=note
            ),
            metric(
                "deals.open.no_activity_14d",
                no14,
                "count",
                OPEN_DEALS_SOURCE,
                note="Counts deals with a blank Last activity date as inactive.",
            ),
            metric(
                "deals.open.no_activity_30d",
                no30,
                "count",
                OPEN_DEALS_SOURCE,
                note="Counts deals with a blank Last activity date as inactive.",
            ),
            metric("deals.open.past_due_close_date", past_due, "count", OPEN_DEALS_SOURCE),
            metric(
                "deals.open.avg_age_days_by_stage",
                {k: average(v) for k, v in by_stage.items()},
                "days_by_stage",
                OPEN_DEALS_SOURCE,
            ),
            metric(
                "deals.open.forecast_category",
                distribution(
                    (d.get("properties") or {}).get("hs_manual_forecast_category") for d in deals
                ),
                "count_by_category",
                OPEN_DEALS_SOURCE,
                note=note,
            ),
        ]

    return safe_many(OPEN_DEALS_SOURCE, compute, keys)


def _owner_names_or_empty(ctx: AuditContext) -> dict[str, str]:
    try:
        return ctx.owner_names()
    except HubSpotError:
        return {}


def _engagement_metric(ctx: AuditContext) -> dict:
    def compute() -> dict:
        names = ctx.owner_names()
        return {names[owner_id]: counts for owner_id, counts in ctx.engagement_counts().items()}

    return safe(
        "activity.per_owner_30d",
        "counts_by_owner",
        "/crm/v3/objects/{calls,meetings,emails}/search",
        compute,
        note=f"Keys per owner: {', '.join(ENGAGEMENT_OBJECTS)}. Logged activity only, last 30 days.",
    )


def _sequence_metric(ctx: AuditContext) -> dict:
    source = "/automation/v4/sequences"

    def compute() -> dict:
        # Beta endpoint: the list call is scoped to a user, so query as each user id we can see.
        sequences = []
        for user in ctx.users():
            page = ctx.client.get(source, {"userId": user["id"], "limit": 100})
            sequences.extend(
                {"name": s.get("name"), "updated_at": s.get("updatedAt")}
                for s in page.get("results", [])
            )
        return {"count": len(sequences), "sequences": sequences}

    return safe(
        "sequences.inventory",
        "count_and_list",
        source,
        compute,
        note="Beta endpoint (automation v4 sequences). Fall back to the screenshot checklist if unsupported.",
    )
