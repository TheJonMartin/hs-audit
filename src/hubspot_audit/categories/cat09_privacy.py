"""Category 9: Privacy."""

from __future__ import annotations

from ..context import AuditContext
from ..metrics import pct, safe
from . import CategoryResult
from .cat04_marketing import safe_subscription_types

NUMBER = 9
NAME = "Privacy"
MANUAL_ITEMS = [
    "Privacy and consent settings page",
    "Consent checkboxes on active forms",
]
SOURCE = "/crm/v3/objects/contacts/search"


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Privacy metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)

    def unsubscribed() -> dict:
        count = ctx.client.count(
            "contacts", [{"propertyName": "hs_email_optout", "operator": "EQ", "value": "true"}]
        )
        total = ctx.cached("contacts_total", lambda: ctx.client.count("contacts"))
        return {"count": count, "pct_of_contacts": pct(count, total)}

    result.metrics.append(
        safe(
            "contacts.unsubscribed",
            "count_and_percent",
            SOURCE,
            unsubscribed,
            note="Contacts opted out of all email (hs_email_optout = true).",
        )
    )
    result.metrics.append(safe_subscription_types(ctx))
    return result
