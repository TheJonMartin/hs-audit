"""Read-only scopes requested by the audit app. Names are unverified against HubSpot's picker."""

from __future__ import annotations

REQUIRED_SCOPES = [
    "crm.objects.contacts.read",
    "crm.objects.companies.read",
    "crm.objects.deals.read",
    "crm.objects.owners.read",
    "crm.schemas.contacts.read",
    "crm.schemas.companies.read",
    "crm.schemas.deals.read",
    "crm.lists.read",
    "settings.users.read",
]
# Optional scopes let the install succeed on tiers or portals that lack the matching feature.
OPTIONAL_SCOPES = [
    "crm.objects.custom.read",
    "crm.schemas.custom.read",
    "forms",
    "content",
    "automation",
    "communication_preferences.read",
]
