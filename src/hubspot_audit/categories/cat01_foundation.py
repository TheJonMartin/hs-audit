"""Category 1: CRM Foundation."""

from __future__ import annotations

import re
from typing import Any

from ..context import CONTACT_POPULATION_PROPERTIES, AuditContext
from ..errors import AuthError, HubSpotError
from ..metrics import (
    STATUS_EMPTY,
    duplicate_groups,
    email_key,
    metric,
    name_company_key,
    pct,
    population_rates,
    safe,
    safe_many,
)
from . import CategoryResult

NUMBER = 1
NAME = "CRM Foundation"
STANDARD_OBJECTS = ("contacts", "companies", "deals")
TEST_NAME_PATTERN = re.compile(
    r"(^|[_\s-])(test|tmp|temp|old|copy|zz|deprecated|do[_\s-]?not[_\s-]?use|unused)($|[_\s\d-])",
    re.IGNORECASE,
)
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
NUMERIC_SUFFIX_PATTERN = re.compile(r"(_|\s)\d+$")
MANUAL_ITEMS = [
    "User last-login dates",
    "Whether a sandbox exists",
    "Required fields per pipeline stage (verify API coverage first)",
]


def run(ctx: AuditContext) -> CategoryResult:
    """Collect CRM Foundation metrics."""
    result = CategoryResult(NUMBER, NAME, manual_items=MANUAL_ITEMS)
    for obj in STANDARD_OBJECTS:
        result.metrics.extend(_property_metrics(ctx, obj, result))
    result.metrics.extend(_custom_object_metrics(ctx))
    result.metrics.extend(_lifecycle_metrics(ctx))
    result.metrics.extend(_pipeline_metrics(ctx))
    result.metrics.extend(_contact_quality_metrics(ctx))
    result.metrics.extend(_user_metrics(ctx))
    return result


def classify_property(prop: dict[str, Any]) -> list[str]:
    """Return naming-pattern flags for a custom property definition."""
    flags = []
    name = str(prop.get("name") or "")
    label = str(prop.get("label") or "")
    if not SNAKE_CASE_PATTERN.match(name):
        flags.append("non_snake_case_name")
    if TEST_NAME_PATTERN.search(name) or TEST_NAME_PATTERN.search(label):
        flags.append("test_or_legacy_marker")
    if NUMERIC_SUFFIX_PATTERN.search(name) or NUMERIC_SUFFIX_PATTERN.search(label):
        flags.append("numeric_suffix")
    return flags


def _property_metrics(ctx: AuditContext, obj: str, result: CategoryResult) -> list[dict]:
    source = f"/crm/v3/properties/{obj}"
    keys = [
        (f"properties.{obj}.total", "count"),
        (f"properties.{obj}.custom", "count"),
        (f"properties.{obj}.default", "count"),
        (f"properties.{obj}.custom_pct", "percent"),
        (f"properties.{obj}.naming_flags", "count_by_flag"),
        (f"properties.{obj}.duplicate_custom_labels", "count"),
    ]

    def compute() -> list[dict]:
        props = ctx.properties(obj)
        custom = [p for p in props if not p.get("hubspotDefined")]
        flag_counts: dict[str, int] = {}
        rows = []
        for prop in props:
            flags = [] if prop.get("hubspotDefined") else classify_property(prop)
            for flag in flags:
                flag_counts[flag] = flag_counts.get(flag, 0) + 1
            rows.append(
                {
                    "object": obj,
                    "name": prop.get("name"),
                    "label": prop.get("label"),
                    "type": prop.get("type"),
                    "field_type": prop.get("fieldType"),
                    "group": prop.get("groupName"),
                    "hubspot_defined": bool(prop.get("hubspotDefined")),
                    "hidden": bool(prop.get("hidden")),
                    "created_at": prop.get("createdAt"),
                    "updated_at": prop.get("updatedAt"),
                    "naming_flags": ";".join(flags),
                }
            )
        result.tables.setdefault("property_list", []).extend(rows)
        labels = [str(p.get("label") or "").strip().lower() for p in custom]
        duplicate_labels = len(labels) - len(set(labels))
        return [
            metric(f"properties.{obj}.total", len(props), "count", source),
            metric(f"properties.{obj}.custom", len(custom), "count", source),
            metric(f"properties.{obj}.default", len(props) - len(custom), "count", source),
            metric(f"properties.{obj}.custom_pct", pct(len(custom), len(props)), "percent", source),
            metric(f"properties.{obj}.naming_flags", flag_counts, "count_by_flag", source),
            metric(f"properties.{obj}.duplicate_custom_labels", duplicate_labels, "count", source),
        ]

    return safe_many(source, compute, keys)


def _custom_object_metrics(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/schemas"
    keys = [("objects.custom.count", "count"), ("objects.custom.list", "list")]

    def compute() -> list[dict]:
        schemas = ctx.schemas()
        summary = [
            {
                "name": s.get("name"),
                "label": s.get("labels", {}).get("singular"),
                "property_count": len(s.get("properties", [])),
            }
            for s in schemas
        ]
        return [
            metric("objects.custom.count", len(schemas), "count", source),
            metric("objects.custom.list", summary, "list", source),
        ]

    return safe_many(source, compute, keys)


def _lifecycle_metrics(ctx: AuditContext) -> list[dict]:
    out = []
    for key, prop_name in (
        ("lifecycle_stages", "lifecyclestage"),
        ("lead_statuses", "hs_lead_status"),
    ):
        source = "/crm/v3/properties/contacts"

        def compute(prop_name=prop_name):
            for prop in ctx.properties("contacts"):
                if prop.get("name") == prop_name:
                    return [o.get("label") for o in prop.get("options", []) if not o.get("hidden")]
            return []

        out.append(safe(f"contacts.{key}", "list", source, compute))
    return out


def _pipeline_metrics(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/pipelines/deals"
    keys = [("pipelines.deals.count", "count"), ("pipelines.deals.stages", "list")]

    def compute() -> list[dict]:
        pipelines = ctx.deal_pipelines()
        summary = [
            {
                "label": p.get("label"),
                "stage_count": len(p.get("stages", [])),
                "stages": [s.get("label") for s in p.get("stages", [])],
            }
            for p in pipelines
        ]
        return [
            metric("pipelines.deals.count", len(pipelines), "count", source),
            metric("pipelines.deals.stages", summary, "list", source),
        ]

    return safe_many(source, compute, keys)


def _contact_quality_metrics(ctx: AuditContext) -> list[dict]:
    source = "/crm/v3/objects/contacts/search"
    keys = [
        ("contacts.sample_size", "count"),
        ("contacts.population_rates", "percent_by_property"),
        ("contacts.duplicates.email", "duplicate_summary"),
        ("contacts.duplicates.name_company", "duplicate_summary"),
    ]

    def compute() -> list[dict]:
        sample = ctx.contact_sample()
        note = "Computed on the most recently created contacts, not the full database."
        if not sample:
            return [metric(k, None, u, source, STATUS_EMPTY) for k, u in keys]
        return [
            metric("contacts.sample_size", len(sample), "count", source, note=note),
            metric(
                "contacts.population_rates",
                population_rates(sample, CONTACT_POPULATION_PROPERTIES),
                "percent_by_property",
                source,
                note=note,
            ),
            metric(
                "contacts.duplicates.email",
                duplicate_groups(sample, email_key),
                "duplicate_summary",
                source,
                note="Duplicates found within the sample only; the full-database rate is higher or equal.",
            ),
            metric(
                "contacts.duplicates.name_company",
                duplicate_groups(sample, name_company_key),
                "duplicate_summary",
                source,
                note="Requires first name, last name and company; within the sample only.",
            ),
        ]

    return safe_many(source, compute, keys)


def _user_metrics(ctx: AuditContext) -> list[dict]:
    source = "/settings/v3/users"
    keys = [
        ("users.total", "count"),
        ("users.super_admin_count", "count"),
        ("users.role_distribution", "count_by_role"),
    ]

    def compute() -> list[dict]:
        users = ctx.users()
        role_names = _role_names(ctx)
        roles: dict[str, int] = {}
        for user in users:
            for role_id in user.get("roleIds") or ([user["roleId"]] if user.get("roleId") else []):
                label = role_names.get(str(role_id), f"role-{role_id}")
                roles[label] = roles.get(label, 0) + 1
        supers = sum(1 for u in users if u.get("superAdmin"))
        return [
            metric("users.total", len(users), "count", source),
            metric("users.super_admin_count", supers, "count", source),
            metric("users.role_distribution", roles, "count_by_role", source),
        ]

    return safe_many(source, compute, keys)


def _role_names(ctx: AuditContext) -> dict[str, str]:
    """Role id -> name. Role lookup is Enterprise-only, so failure falls back to ids."""
    try:
        rows = ctx.cached(
            "roles", lambda: ctx.client.get("/settings/v3/users/roles").get("results", [])
        )
    except AuthError:
        raise
    except HubSpotError:
        return {}
    return {str(r.get("id")): r.get("name") or str(r.get("id")) for r in rows}
