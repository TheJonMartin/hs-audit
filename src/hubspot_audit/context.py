"""Shared audit context: client, clock, settings and cached fetches reused across categories."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .client import HubSpotClient
from .metrics import to_epoch_ms

DEFAULT_SAMPLE_SIZE = 1000
MAX_OPEN_DEALS = 5000
STALE_DAYS_SHORT = 14
STALE_DAYS_LONG = 30
RECENT_DAYS_SHORT = 30
RECENT_DAYS_LONG = 90
STALE_WORKFLOW_DAYS = 183  # roughly six months
RENEWAL_WINDOW_DAYS = 60

CONTACT_SAMPLE_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "jobtitle",
    "phone",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "hs_analytics_source",
    "hs_analytics_source_data_1",
    "hs_object_source",
    "createdate",
    "hs_email_optout",
]
# Fields profiled for population rate on the contact sample. Raw values never leave memory.
CONTACT_POPULATION_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "jobtitle",
    "phone",
    "lifecyclestage",
    "hs_lead_status",
    "hubspot_owner_id",
    "hs_analytics_source",
]
DEAL_PROPERTIES = [
    "dealname",
    "dealstage",
    "pipeline",
    "amount",
    "closedate",
    "createdate",
    "hs_lastmodifieddate",
    "hubspot_owner_id",
    "notes_last_updated",
    "hs_manual_forecast_category",
]
ENGAGEMENT_OBJECTS = ("calls", "meetings", "emails")


@dataclass
class AuditContext:
    """Everything a category module needs. Expensive fetches are cached, errors included."""

    client: HubSpotClient
    now: datetime
    sample_size: int = DEFAULT_SAMPLE_SIZE
    _cache: dict[str, tuple[bool, Any]] = field(default_factory=dict)

    def days_ago_ms(self, days: int) -> str:
        """Epoch-ms string for ``days`` before now, for search filters."""
        return to_epoch_ms(self.now - timedelta(days=days))

    def cached(self, key: str, fetch: Callable[[], Any]) -> Any:
        """Return a cached value, or fetch it once. A failed fetch re-raises on every call."""
        if key not in self._cache:
            try:
                self._cache[key] = (True, fetch())
            except Exception as exc:  # noqa: BLE001 - re-raised on every access below
                self._cache[key] = (False, exc)
        ok, value = self._cache[key]
        if not ok:
            raise value
        return value

    # ---- cached fetches -------------------------------------------------------------------

    def properties(self, object_type: str) -> list[dict[str, Any]]:
        """Property definitions for an object type or custom object type id."""
        return self.cached(
            f"properties:{object_type}",
            lambda: self.client.get(f"/crm/v3/properties/{object_type}").get("results", []),
        )

    def schemas(self) -> list[dict[str, Any]]:
        """Custom object schemas."""
        return self.cached("schemas", lambda: self.client.get("/crm/v3/schemas").get("results", []))

    def owners(self) -> list[dict[str, Any]]:
        """CRM owners (people who can own records)."""
        return self.cached(
            "owners", lambda: list(self.client.paginate("/crm/v3/owners", {"limit": 500}))
        )

    def owner_names(self) -> dict[str, str]:
        """Owner id -> display name. Owner names are permitted in the bundle."""
        names = {}
        for owner in self.owners():
            full = f"{owner.get('firstName') or ''} {owner.get('lastName') or ''}".strip()
            names[str(owner.get("id"))] = full or f"owner-{owner.get('id')}"
        return names

    def users(self) -> list[dict[str, Any]]:
        """Portal users with roles."""
        return self.cached("users", lambda: list(self.client.paginate("/settings/v3/users")))

    def contact_sample(self) -> list[dict[str, Any]]:
        """Most recently created contacts, up to ``sample_size``. Held in memory only."""
        return self.cached(
            "contact_sample",
            lambda: list(
                self.client.search(
                    "contacts",
                    properties=CONTACT_SAMPLE_PROPERTIES,
                    sorts=[{"propertyName": "createdate", "direction": "DESCENDING"}],
                    max_records=self.sample_size,
                )
            ),
        )

    def open_deals(self) -> list[dict[str, Any]]:
        """Open deals, up to ``MAX_OPEN_DEALS``."""
        return self.cached(
            "open_deals",
            lambda: list(
                self.client.search(
                    "deals",
                    filters=[{"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}],
                    properties=DEAL_PROPERTIES,
                    max_records=MAX_OPEN_DEALS,
                )
            ),
        )

    def deal_pipelines(self) -> list[dict[str, Any]]:
        """Deal pipelines with stages."""
        return self.cached(
            "deal_pipelines",
            lambda: self.client.get("/crm/v3/pipelines/deals").get("results", []),
        )

    def workflows(self) -> list[dict[str, Any]]:
        """Workflow (flow) inventory from the automation v4 API."""
        return self.cached(
            "workflows",
            lambda: list(self.client.paginate("/automation/v4/flows", {"limit": 100})),
        )

    def engagement_counts(self) -> dict[str, dict[str, int]]:
        """Calls/meetings/emails in the last 30 days per owner id. Uses search totals only."""
        return self.cached("engagement_counts", self._fetch_engagement_counts)

    def _fetch_engagement_counts(self) -> dict[str, dict[str, int]]:
        since = self.days_ago_ms(RECENT_DAYS_SHORT)
        counts: dict[str, dict[str, int]] = {}
        for owner_id in self.owner_names():
            counts[owner_id] = {}
            for obj in ENGAGEMENT_OBJECTS:
                counts[owner_id][obj] = self.client.count(
                    obj,
                    [
                        {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": owner_id},
                        {"propertyName": "hs_timestamp", "operator": "GTE", "value": since},
                    ],
                )
        return counts
