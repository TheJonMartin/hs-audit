"""A fake HubSpot portal served through an injectable requests-like session."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SERVICE_KEY = "pat-na1-FAKE-KEY-0000"
SECRET_EMAIL = "jane.doe@example.com"


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.content = json.dumps(self._body).encode()

    def json(self):
        return self._body


def contact(i: int, **extra) -> dict:
    props = {
        "email": SECRET_EMAIL if i < 2 else f"person{i}@example.com",
        "firstname": "Jane" if i < 2 else f"First{i}",
        "lastname": "Doe" if i < 2 else f"Last{i}",
        "company": "Acme" if i < 2 else f"Co{i}",
        "jobtitle": "VP" if i % 4 == 0 else "",
        "hs_analytics_source": "ORGANIC_SEARCH" if i % 2 else "",
        "hs_object_source_label": "FORM" if i % 2 else "CRM_UI",
        "createdate": iso(i),
    }
    props.update(extra)
    return {"id": str(i), "properties": props}


class FakePortal:
    """Routes requests to canned data. ``deny`` is a set of path prefixes returning 403."""

    def __init__(self, deny: set[str] | None = None) -> None:
        self.deny = deny or set()
        self.headers: dict = {}
        self.requests: list[tuple[str, str]] = []
        self.contacts = [contact(i) for i in range(10)]
        self.flaky_once: set[str] = set()

    def request(self, method, url, params=None, json=None, timeout=None):
        path = url.replace("https://api.hubapi.com", "")
        self.requests.append((method, path))
        if self.headers.get("Authorization") != f"Bearer {SERVICE_KEY}":
            return FakeResponse(401, {"category": "INVALID_AUTHENTICATION"})
        if any(path.startswith(p) for p in self.deny):
            return FakeResponse(403, {"category": "MISSING_SCOPES"})
        if path in self.flaky_once:
            self.flaky_once.discard(path)
            return FakeResponse(429, {}, {"Retry-After": "0"})
        return self.route(method, path, params or {}, json or {})

    # ------------------------------------------------------------------ routes
    def route(self, method, path, params, body) -> FakeResponse:
        if path == "/account-info/v3/details":
            return FakeResponse(200, {"portalId": 12345})
        if path.startswith("/crm/v3/properties/"):
            return FakeResponse(200, {"results": self._properties(path.rsplit("/", 1)[1])})
        if path == "/crm/v3/schemas":
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "name": "invoices",
                            "objectTypeId": "2-1",
                            "labels": {"singular": "Invoice"},
                            "properties": [{"name": "invoice_total", "hubspotDefined": False}],
                        }
                    ]
                },
            )
        if path == "/crm/v3/pipelines/deals":
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "label": "Sales",
                            "stages": [
                                {"id": "s1", "label": "Qualified"},
                                {"id": "s2", "label": "Proposal"},
                            ],
                        }
                    ]
                },
            )
        if path == "/crm/v3/owners":
            return FakeResponse(
                200,
                {
                    "results": [
                        {"id": "1", "firstName": "Ann", "lastName": "Owner"},
                        {"id": "2", "firstName": "Bo", "lastName": "Idle"},
                    ]
                },
            )
        if path == "/settings/v3/users":
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": "10",
                            "email": "admin@example.com",
                            "roleIds": ["r1"],
                            "superAdmin": True,
                        },
                        {
                            "id": "11",
                            "email": "rep@example.com",
                            "roleIds": ["r2"],
                            "superAdmin": False,
                        },
                    ]
                },
            )
        if path == "/settings/v3/users/roles":
            return FakeResponse(
                200, {"results": [{"id": "r1", "name": "Admin"}, {"id": "r2", "name": "Sales"}]}
            )
        if path == "/automation/v4/flows":
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": "1",
                            "name": "Lead Routing - Round Robin",
                            "isEnabled": True,
                            "flowType": "WORKFLOW",
                            "updatedAt": iso(400),
                        },
                        {
                            "id": "2",
                            "name": "Nurture",
                            "isEnabled": False,
                            "flowType": "WORKFLOW",
                            "updatedAt": iso(10),
                        },
                    ]
                },
            )
        if path == "/automation/v4/sequences":
            return FakeResponse(404, {"category": "NOT_FOUND"})
        if path == "/marketing/v3/forms":
            return FakeResponse(
                200, {"results": [{"id": "f1", "name": "Contact us", "formType": "hubspot"}]}
            )
        if path.startswith("/form-integrations/v1/submissions/forms/"):
            return FakeResponse(
                200,
                {"results": [{"submittedAt": int((NOW - timedelta(days=5)).timestamp() * 1000)}]},
            )
        if path == "/marketing/v3/emails":
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "name": "Newsletter",
                            "publishDate": iso(20),
                            "stats": {
                                "counters": {"sent": 200, "open": 80, "click": 20, "bounce": 4}
                            },
                        }
                    ]
                },
            )
        if path == "/crm/v3/lists/search":
            return FakeResponse(
                200,
                {
                    "lists": [
                        {
                            "name": "All",
                            "processingType": "DYNAMIC",
                            "objectTypeId": "0-1",
                            "additionalProperties": {"hs_list_size": "10"},
                        }
                    ],
                    "hasMore": False,
                },
            )
        if path == "/communication-preferences/v3/definitions":
            return FakeResponse(
                200, {"subscriptionDefinitions": [{"name": "Marketing", "isActive": True}]}
            )
        if path.endswith("/search"):
            return self._search(path.split("/")[4], body)
        if path.startswith("/crm/v3/objects/"):
            return FakeResponse(200, {"results": []})
        return FakeResponse(404, {"category": "NOT_FOUND"})

    @staticmethod
    def _properties(obj: str) -> list[dict]:
        base = [
            {"name": "email", "label": "Email", "hubspotDefined": True, "type": "string"},
            {
                "name": "lifecyclestage",
                "label": "Lifecycle Stage",
                "hubspotDefined": True,
                "options": [{"label": "Lead"}, {"label": "Customer"}],
            },
            {
                "name": "hs_lead_status",
                "label": "Lead Status",
                "hubspotDefined": True,
                "options": [{"label": "New"}],
            },
        ]
        if obj == "contacts":
            base += [
                {"name": "fav_color", "label": "Fav Color", "type": "string"},
                {"name": "FavColor2", "label": "Fav Colour 2", "type": "string"},
                {"name": "test_field", "label": "Fav Color", "type": "string"},
            ]
        return base

    def _search(self, obj: str, body: dict) -> FakeResponse:
        filters = [f for g in body.get("filterGroups", []) for f in g["filters"]]
        if obj == "contacts":
            if body.get("limit") == 1:
                total = 1000
                if any(f["propertyName"] == "hubspot_owner_id" for f in filters):
                    total = 250
                if any(f["propertyName"] == "hs_email_optout" for f in filters):
                    total = 50
                if any(f["propertyName"] == "hs_analytics_source" for f in filters):
                    total = 100
                return FakeResponse(200, {"total": total, "results": []})
            return FakeResponse(200, {"total": len(self.contacts), "results": self.contacts})
        if obj == "deals":
            if body.get("limit") == 1:
                return FakeResponse(200, {"total": 2, "results": []})
            return FakeResponse(
                200,
                {
                    "total": 2,
                    "results": [
                        {
                            "id": "d1",
                            "properties": {
                                "dealname": "Big Deal",
                                "dealstage": "s1",
                                "hubspot_owner_id": "1",
                                "createdate": iso(40),
                                "closedate": iso(3),
                                "notes_last_updated": iso(2),
                                "hs_lastmodifieddate": iso(2),
                                "hs_manual_forecast_category": "COMMIT",
                            },
                        },
                        {
                            "id": "d2",
                            "properties": {
                                "dealname": f"Deal for {SECRET_EMAIL}",
                                "dealstage": "s2",
                                "hubspot_owner_id": "1",
                                "createdate": iso(100),
                                "closedate": iso(-30),
                                "hs_lastmodifieddate": iso(50),
                            },
                        },
                    ],
                },
            )
        if obj in ("calls", "meetings", "emails"):
            owner = next(
                (f["value"] for f in filters if f["propertyName"] == "hubspot_owner_id"), None
            )
            return FakeResponse(200, {"total": 7 if owner == "1" else 0, "results": []})
        return FakeResponse(
            200, {"total": 3, "results": [{"id": "1", "properties": {"invoice_total": "5"}}]}
        )


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
