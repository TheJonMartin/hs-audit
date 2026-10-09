"""Category 4: Marketing."""

from __future__ import annotations

from datetime import timedelta

from ..context import RECENT_DAYS_LONG, AuditContext
from ..metrics import distribution, metric, parse_hs_datetime, pct, safe, safe_many
from . import CategoryResult

NUMBER = 4
NAME = "Marketing"
MANUAL_ITEMS = [
    "Connected ad accounts and sync status",
    "Whether the preference center is published",
]
EMAIL_PAGE_SIZE = 100
MAX_EMAILS = 500


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Marketing metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    result.metrics.extend(_email_metrics(ctx, result))
    result.metrics.extend(_list_metrics(ctx, result))
    result.metrics.append(safe_subscription_types(ctx))
    result.metrics.append(_source_drilldown(ctx))
    return result


def _email_metrics(ctx: AuditContext, result: CategoryResult) -> list[dict]:
    source = "/marketing/v3/emails"
    keys = [("emails.sent_90d.count", "count"), ("emails.sent_90d.rates", "percent_by_rate")]

    def compute() -> list[dict]:
        cutoff = ctx.now - timedelta(days=RECENT_DAYS_LONG)
        emails = list(
            ctx.client.paginate(
                source, {"limit": EMAIL_PAGE_SIZE, "includeStats": "true"}, max_records=MAX_EMAILS
            )
        )
        rows = []
        for email in emails:
            published = parse_hs_datetime(email.get("publishDate") or email.get("publishedAt"))
            counters = (email.get("stats") or {}).get("counters") or {}
            sent = counters.get("sent") or 0
            if not published or published < cutoff or not sent:
                continue
            rows.append(
                {
                    "name": email.get("name"),
                    "published_at": published.isoformat(),
                    "sent": sent,
                    "open_pct": pct(counters.get("open", 0), sent),
                    "click_pct": pct(counters.get("click", 0), sent),
                    "bounce_pct": pct(counters.get("bounce", 0), sent),
                }
            )
        result.tables["marketing_emails_90d"] = rows
        total_sent = sum(r["sent"] for r in rows)

        def weighted(field: str) -> float | None:
            return pct(
                round(sum(r[field] * r["sent"] for r in rows if r[field] is not None) / 100),
                total_sent,
            )

        rates = {
            "open": weighted("open_pct"),
            "click": weighted("click_pct"),
            "bounce": weighted("bounce_pct"),
        }
        return [
            metric(
                "emails.sent_90d.count",
                len(rows),
                "count",
                source,
                note="Emails with stats published in the last 90 days. Stats shape is verified in testing.",
            ),
            metric(
                "emails.sent_90d.rates",
                rates,
                "percent_by_rate",
                source,
                note="Send-weighted across emails. Per-email rates are in marketing_emails_90d.csv.",
            ),
        ]

    return safe_many(source, compute, keys)


def _list_metrics(ctx: AuditContext, result: CategoryResult) -> list[dict]:
    source = "/crm/v3/lists/search"
    keys = [("lists.total", "count"), ("lists.by_type", "count_by_type")]

    def compute() -> list[dict]:
        lists = []
        offset = 0
        while True:
            page = ctx.client.post(
                source,
                {
                    "query": "",
                    "count": 500,
                    "offset": offset,
                    "additionalProperties": ["hs_list_size"],
                },
            )
            lists.extend(page.get("lists", []))
            if not page.get("hasMore"):
                break
            offset = page.get("offset", offset + 500)
        result.tables["list_inventory"] = [
            {
                "name": item.get("name"),
                "type": item.get("processingType"),
                "object_type_id": item.get("objectTypeId"),
                "size": (item.get("additionalProperties") or {}).get("hs_list_size"),
                "updated_at": item.get("updatedAt"),
            }
            for item in lists
        ]
        return [
            metric("lists.total", len(lists), "count", source),
            metric(
                "lists.by_type",
                distribution(i.get("processingType") for i in lists),
                "count_by_type",
                source,
            ),
        ]

    return safe_many(source, compute, keys)


def safe_subscription_types(ctx: AuditContext) -> dict:
    """Subscription type inventory, shared with Category 9."""
    source = "/communication-preferences/v3/definitions"

    def compute() -> dict:
        definitions = ctx.cached(
            "subscription_definitions",
            lambda: ctx.client.get(source).get("subscriptionDefinitions", []),
        )
        return {
            "count": len(definitions),
            "types": [
                {"name": d.get("name"), "active": bool(d.get("isActive"))} for d in definitions
            ],
        }

    return safe("subscriptions.types", "count_and_list", source, compute)


def _source_drilldown(ctx: AuditContext) -> dict:
    source = "/crm/v3/objects/contacts/search"

    def compute() -> dict | None:
        sample = ctx.contact_sample()
        if not sample:
            return None
        with_data = [
            r for r in sample if (r.get("properties") or {}).get("hs_analytics_source_data_1")
        ]
        return {
            "source_drilldown_populated_pct": pct(len(with_data), len(sample)),
            "top_drilldown_by_source": distribution(
                (r.get("properties") or {}).get("hs_analytics_source") for r in with_data
            ),
        }

    return safe(
        "contacts.source_drilldown",
        "percent_and_distribution",
        source,
        compute,
        note="Drill-down 1 populated rate, as a proxy for UTM/source capture. Sample only.",
    )
