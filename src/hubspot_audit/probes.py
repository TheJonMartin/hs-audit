"""Startup probes: one cheap request per scope area."""

from __future__ import annotations

from .client import Probe

PROBES = [
    Probe("crm.contacts", "GET", "/crm/v3/objects/contacts", {"limit": 1}),
    Probe("crm.companies", "GET", "/crm/v3/objects/companies", {"limit": 1}),
    Probe("crm.deals", "GET", "/crm/v3/objects/deals", {"limit": 1}),
    Probe("crm.owners", "GET", "/crm/v3/owners", {"limit": 1}),
    Probe("crm.schemas", "GET", "/crm/v3/properties/contacts"),
    Probe("crm.custom_objects", "GET", "/crm/v3/schemas"),
    Probe("crm.lists", "POST", "/crm/v3/lists/search", body={"query": "", "count": 1}),
    Probe("forms", "GET", "/marketing/v3/forms", {"limit": 1}),
    Probe("content", "GET", "/marketing/v3/emails", {"limit": 1}),
    Probe("automation", "GET", "/automation/v4/flows", {"limit": 1}),
    Probe("settings.users", "GET", "/settings/v3/users", {"limit": 1}),
    Probe("communication_preferences", "GET", "/communication-preferences/v3/definitions"),
    Probe("crm.engagements", "GET", "/crm/v3/objects/calls", {"limit": 1}),
]
ACCOUNT_INFO_PATH = "/account-info/v3/details"
