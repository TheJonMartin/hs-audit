"""Category 2: Lead Management."""

from __future__ import annotations

import re
from datetime import timedelta

from ..context import RECENT_DAYS_LONG, RECENT_DAYS_SHORT, AuditContext
from ..errors import AuthError, HubSpotError
from ..metrics import (
    distribution,
    metric,
    parse_hs_datetime,
    pct,
    safe,
    safe_many,
)
from . import CategoryResult

NUMBER = 2
NAME = "Lead Management"
MANUAL_ITEMS = ["Which forms are live on the website"]
ROUTING_PATTERN = re.compile(
    r"(assign|rout(e|ing)|round[\s-]?robin|rotat|owner|lead[\s-]?distribution)", re.IGNORECASE
)
UNKNOWN_SOURCE_VALUES = ("DIRECT_TRAFFIC", "OFFLINE")
MAX_SUBMISSION_PAGES = 20
SUBMISSION_PAGE_SIZE = 50
MS_PER_SECOND = 1000


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Lead Management metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    result.metrics.extend(_form_metrics(ctx, result))
    result.metrics.extend(_source_metrics(ctx))
    result.metrics.extend(_ownership_metrics(ctx))
    result.metrics.append(_routing_workflows(ctx))
    return result


def _form_metrics(ctx: AuditContext, result: CategoryResult) -> list[dict]:
    source = "/marketing/v3/forms"
    keys = [("forms.total", "count"), ("forms.inventory", "list")]

    def compute() -> list[dict]:
        forms = list(ctx.client.paginate("/marketing/v3/forms", {"limit": 100}))
        rows = [_form_row(ctx, f) for f in forms]
        result.tables["form_list"] = rows
        return [
            metric("forms.total", len(forms), "count", source),
            metric(
                "forms.inventory",
                rows,
                "list",
                source,
                note="Submission counts use the legacy form-integrations endpoint; null means unavailable.",
            ),
        ]

    return safe_many(source, compute, keys)


def _form_row(ctx: AuditContext, form: dict) -> dict:
    """One form with submission counts. Submitter data is never read, only timestamps."""
    row = {
        "id": form.get("id"),
        "name": form.get("name"),
        "form_type": form.get("formType"),
        "archived": bool(form.get("archived")),
        "updated_at": form.get("updatedAt"),
        "submissions_30d": None,
        "submissions_90d": None,
        "last_submission_at": None,
    }
    try:
        stamps = _submission_times(ctx, str(form.get("id")))
    except AuthError:
        raise
    except HubSpotError:
        return row
    cutoff30 = ctx.now - timedelta(days=RECENT_DAYS_SHORT)
    cutoff90 = ctx.now - timedelta(days=RECENT_DAYS_LONG)
    row["submissions_30d"] = sum(1 for s in stamps if s >= cutoff30)
    row["submissions_90d"] = sum(1 for s in stamps if s >= cutoff90)
    row["last_submission_at"] = max(stamps).isoformat() if stamps else None
    return row


def _submission_times(ctx: AuditContext, form_id: str) -> list:
    """Submission timestamps, newest first, stopping once past the 90-day window."""
    path = f"/form-integrations/v1/submissions/forms/{form_id}"
    cutoff = ctx.now - timedelta(days=RECENT_DAYS_LONG)
    stamps = []
    params: dict = {"limit": SUBMISSION_PAGE_SIZE}
    for _ in range(MAX_SUBMISSION_PAGES):
        page = ctx.client.get(path, params)
        for item in page.get("results", []):
            moment = parse_hs_datetime(item.get("submittedAt"))
            if moment:
                stamps.append(moment)
        after = (page.get("paging") or {}).get("next", {}).get("after")
        if not after or (stamps and min(stamps) < cutoff):
            break
        params["after"] = after
    return stamps


def _source_metrics(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/objects/contacts/search"

    def total_contacts() -> int:
        return ctx.cached("contacts_total", lambda: ctx.client.count("contacts"))

    def unknown_pct() -> float | None:
        unknown = 0
        for value in UNKNOWN_SOURCE_VALUES:
            unknown += ctx.client.count(
                "contacts",
                [{"propertyName": "hs_analytics_source", "operator": "EQ", "value": value}],
            )
        unknown += ctx.client.count(
            "contacts", [{"propertyName": "hs_analytics_source", "operator": "NOT_HAS_PROPERTY"}]
        )
        return pct(unknown, total_contacts())

    return [
        safe("contacts.total", "count", source, total_contacts),
        safe(
            "contacts.source_distribution",
            "count_by_source",
            source,
            lambda: distribution(
                (r.get("properties") or {}).get("hs_analytics_source") for r in ctx.contact_sample()
            ),
            note="From the contact sample (most recently created).",
        ),
        safe(
            "contacts.unknown_or_direct_source_pct",
            "percent",
            source,
            unknown_pct,
            note="Direct traffic, offline and blank original source over all contacts.",
        ),
    ]


def _ownership_metrics(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/objects/contacts/search"
    since = ctx.days_ago_ms(RECENT_DAYS_SHORT)

    def unowned() -> dict:
        count = ctx.client.count(
            "contacts", [{"propertyName": "hubspot_owner_id", "operator": "NOT_HAS_PROPERTY"}]
        )
        total = ctx.cached("contacts_total", lambda: ctx.client.count("contacts"))
        return {"count": count, "pct_of_contacts": pct(count, total)}

    def no_activity() -> int:
        return ctx.client.count(
            "contacts",
            [
                {"propertyName": "createdate", "operator": "GTE", "value": since},
                {"propertyName": "notes_last_updated", "operator": "NOT_HAS_PROPERTY"},
            ],
        )

    return [
        safe("contacts.no_owner", "count_and_percent", source, unowned),
        safe(
            "contacts.created_30d_no_activity",
            "count",
            source,
            no_activity,
            note="Uses 'Last activity date' being blank; verify against the UI in testing.",
        ),
    ]


def _routing_workflows(ctx: AuditContext) -> dict:
    source = "/automation/v4/flows"

    def compute() -> dict:
        flows = ctx.workflows()
        matches = [
            {"name": f.get("name"), "enabled": bool(f.get("isEnabled")), "type": f.get("flowType")}
            for f in flows
            if ROUTING_PATTERN.search(str(f.get("name") or ""))
        ]
        return {"count": len(matches), "workflows": matches}

    return safe(
        "workflows.routing_related",
        "count_and_list",
        source,
        compute,
        note="Matched by workflow name only; trigger conditions are not exposed in the list endpoint.",
    )
