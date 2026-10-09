import pytest
import requests
from conftest import SERVICE_KEY, FakePortal, FakeResponse

from hubspot_audit.client import HubSpotClient, Probe
from hubspot_audit.errors import ApiError, AuthError, ForbiddenError, UnsupportedError


def make_client(portal, **kw) -> HubSpotClient:
    return HubSpotClient(SERVICE_KEY, session=portal, sleep=lambda _s: None, **kw)


def test_bearer_header_and_missing_key():
    portal = FakePortal()
    make_client(portal)
    assert portal.headers["Authorization"] == f"Bearer {SERVICE_KEY}"
    with pytest.raises(AuthError):
        HubSpotClient("", session=portal)


def test_bad_key_is_auth_error():
    client = HubSpotClient("wrong", session=FakePortal(), sleep=lambda _s: None)
    with pytest.raises(AuthError):
        client.get("/crm/v3/owners")


def test_403_and_404_map_to_typed_errors():
    portal = FakePortal(deny={"/crm/v3/owners"})
    client = make_client(portal)
    with pytest.raises(ForbiddenError):
        client.get("/crm/v3/owners")
    with pytest.raises(UnsupportedError):
        client.get("/automation/v4/sequences")


def test_retries_429_then_succeeds():
    portal = FakePortal()
    portal.flaky_once.add("/crm/v3/owners")
    client = make_client(portal)
    assert client.get("/crm/v3/owners")["results"]
    assert client.call_log[-1].attempts == 2


def test_5xx_exhausts_retries():
    class Down(FakePortal):
        def route(self, *a):
            return FakeResponse(503, {})

    with pytest.raises(ApiError):
        make_client(Down()).get("/crm/v3/owners")


def test_network_error_retries_then_raises():
    class Boom(FakePortal):
        def request(self, *a, **k):
            raise requests.ConnectionError("nope")

    with pytest.raises(ApiError):
        make_client(Boom()).get("/crm/v3/owners")


def test_pagination_follows_after_cursor():
    class Paged(FakePortal):
        def route(self, method, path, params, body):
            if params.get("after") == "b":
                return FakeResponse(200, {"results": [{"id": 3}]})
            if params.get("after") == "a":
                return FakeResponse(
                    200, {"results": [{"id": 2}], "paging": {"next": {"after": "b"}}}
                )
            return FakeResponse(200, {"results": [{"id": 1}], "paging": {"next": {"after": "a"}}})

    assert [r["id"] for r in make_client(Paged()).paginate("/x")] == [1, 2, 3]


def test_paginate_respects_max_records():
    assert len(list(make_client(FakePortal()).search("contacts", max_records=4))) == 4


def test_probe_records_forbidden_and_stops_on_auth():
    portal = FakePortal(deny={"/crm/v3/owners"})
    client = make_client(portal)
    results = client.run_probes(
        [
            Probe("crm.owners", "GET", "/crm/v3/owners"),
            Probe("crm.deals", "GET", "/crm/v3/objects/deals"),
        ]
    )
    assert [r.status for r in results] == ["forbidden", "ok"]
    assert client.granted_areas() == ["crm.deals"]


def test_call_log_has_no_key_or_record_data():
    client = make_client(FakePortal())
    client.get("/crm/v3/owners")
    text = repr(client.call_log)
    assert SERVICE_KEY not in text and "Ann" not in text
