"""Category 10: Adoption. Reuses Category 3 activity data and Category 1 user data."""

from __future__ import annotations

from ..context import STALE_DAYS_SHORT, AuditContext
from ..metrics import days_since, distribution, pct, safe
from . import CategoryResult

NUMBER = 10
NAME = "Adoption"
MANUAL_ITEMS = ["User last-login dates (same checklist item as Category 1)"]
SOURCE = "/crm/v3/objects/{calls,meetings,emails,deals,contacts}/search"
# Values of hs_object_source_label (Record source) that mean a person created or loaded the record.
MANUAL_CREATION_SOURCES = ("CRM_UI", "CRM_UI_BULK_ACTION", "IMPORT", "BATCH_UPDATE")


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Adoption metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)

    def per_owner() -> dict:
        names = ctx.owner_names()
        return {names[o]: c for o, c in ctx.engagement_counts().items()}

    def zero_activity_owners() -> list[str]:
        names = ctx.owner_names()
        return sorted(
            names[o] for o, counts in ctx.engagement_counts().items() if not sum(counts.values())
        )

    def unmodified_deals() -> int:
        return sum(
            1
            for d in ctx.open_deals()
            if (days_since((d.get("properties") or {}).get("hs_lastmodifieddate"), ctx.now) or 0)
            > STALE_DAYS_SHORT
        )

    def creation_sources() -> dict:
        sample = ctx.contact_sample()
        sources = [(r.get("properties") or {}).get("hs_object_source_label") for r in sample]
        manual = sum(1 for v in sources if v in MANUAL_CREATION_SOURCES)
        return {
            "distribution": distribution(sources),
            "manual_or_offline_pct": pct(manual, len(sample)),
        }

    result.metrics.append(
        safe(
            "activity.per_owner_30d",
            "counts_by_owner",
            SOURCE,
            per_owner,
            note="Reuses Category 3. Logged calls, meetings and emails, last 30 days.",
        )
    )
    result.metrics.append(
        safe("activity.owners_with_zero_activity_30d", "owner_names", SOURCE, zero_activity_owners)
    )
    result.metrics.append(
        safe(
            "deals.open.unmodified_14d",
            "count",
            SOURCE,
            unmodified_deals,
            note="Open deals not modified in 14 days. Sample capped at 5,000 open deals.",
        )
    )
    result.metrics.append(
        safe(
            "contacts.creation_source",
            "count_by_source",
            SOURCE,
            creation_sources,
            note=f"Record source on the contact sample. Manual/offline sources: {', '.join(MANUAL_CREATION_SOURCES)}.",
        )
    )
    return result
