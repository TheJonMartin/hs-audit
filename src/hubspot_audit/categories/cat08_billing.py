"""Category 8: Billing."""

from __future__ import annotations

from datetime import timedelta

from ..context import RENEWAL_WINDOW_DAYS, AuditContext
from ..metrics import metric, population_rates, safe, safe_many, to_epoch_ms
from . import CategoryResult

NUMBER = 8
NAME = "Billing"
MANUAL_ITEMS = ["Billing system sync health"]
CUSTOM_SAMPLE_SIZE = 200
MAX_PROFILED_PROPERTIES = 40


def run(ctx: AuditContext) -> CategoryResult:
    """Collect Billing metrics: custom-object record counts, field population and renewals."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    result.metrics.extend(_custom_object_profiles(ctx))
    result.metrics.append(_renewals(ctx))
    return result


def _custom_object_profiles(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/schemas"
    keys = [("objects.custom.profiles", "list")]

    def compute() -> list[dict]:
        profiles = []
        for schema in ctx.schemas():
            type_id = schema.get("objectTypeId")
            custom_props = [
                p["name"] for p in schema.get("properties", []) if not p.get("hubspotDefined")
            ][:MAX_PROFILED_PROPERTIES]
            total = ctx.client.count(type_id)
            sample = list(
                ctx.client.search(type_id, properties=custom_props, max_records=CUSTOM_SAMPLE_SIZE)
            )
            profiles.append(
                {
                    "name": schema.get("name"),
                    "record_count": total,
                    "sample_size": len(sample),
                    "population_rates": population_rates(sample, custom_props),
                }
            )
        return [
            metric(
                "objects.custom.profiles",
                profiles,
                "list",
                source,
                note="Billing fields are not fixed; every custom property is profiled on a sample.",
            )
        ]

    return safe_many(source, compute, keys)


def _renewals(ctx: AuditContext) -> dict:
    source = "/crm/v3/objects/deals/search"

    def compute() -> int:
        end = ctx.now + timedelta(days=RENEWAL_WINDOW_DAYS)
        return ctx.client.count(
            "deals",
            [
                {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"},
                {"propertyName": "dealtype", "operator": "EQ", "value": "existingbusiness"},
                {"propertyName": "closedate", "operator": "LTE", "value": to_epoch_ms(end)},
                {"propertyName": "closedate", "operator": "GTE", "value": to_epoch_ms(ctx.now)},
            ],
        )

    return safe(
        "deals.renewals_next_60d",
        "count",
        source,
        compute,
        note="Proxy: open 'Existing Business' deals closing in 60 days. Portals that model renewals "
        "differently (custom object, pipeline) need a manual check.",
    )
