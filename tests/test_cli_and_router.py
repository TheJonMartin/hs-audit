import pytest
import requests
from conftest import SERVICE_KEY, FakePortal

from hubspot_audit import cli, error_router
from hubspot_audit.errors import AuthError

CONFIG = {
    "url": "http://router.test/hook",
    "platform": "LOCAL_SCRIPT",
    "flow_id": "f1",
    "flow_name": "Audit",
}


class Calls:
    def __init__(self, *statuses):
        self.statuses, self.seen = list(statuses), []

    def post(self, url, json=None, timeout=None):
        self.seen.append((url, json, timeout))
        status = self.statuses.pop(0)
        if status == "net":
            raise requests.ConnectionError()
        return type("R", (), {"status_code": status})()


def fake_request(portal):
    def request(self, method, url, params=None, json=None, timeout=None):
        portal.headers = dict(self.headers)
        return portal.request(method, url, params=params, json=json)

    return request


def test_router_retries_5xx_once(monkeypatch):
    monkeypatch.setattr(error_router.time, "sleep", lambda _s: None)
    calls = Calls(503, 200)
    monkeypatch.setattr(error_router.requests, "post", calls.post)
    error_router.post_to_error_router(CONFIG, {"a": 1})
    assert len(calls.seen) == 2 and calls.seen[0][2] == 5


def test_router_never_retries_4xx(monkeypatch):
    calls = Calls(400)
    monkeypatch.setattr(error_router.requests, "post", calls.post)
    error_router.post_to_error_router(CONFIG, {"a": 1})
    assert len(calls.seen) == 1


def test_router_gives_up_after_two_network_errors(monkeypatch, capsys):
    monkeypatch.setattr(error_router.time, "sleep", lambda _s: None)
    calls = Calls("net", "net")
    monkeypatch.setattr(error_router.requests, "post", calls.post)
    error_router.post_to_error_router(CONFIG, {"a": 1})
    assert len(calls.seen) == 2 and "CRITICAL" in capsys.readouterr().err


def test_payload_shape(monkeypatch):
    calls = Calls(200)
    monkeypatch.setattr(error_router.requests, "post", calls.post)
    error_router.handle_failure(
        CONFIG,
        ValueError("boom"),
        {"record_id": "123", "process_name": "Cat 1", "correlation_id": "c"},
    )
    payload = calls.seen[0][1]
    assert payload == {
        "record_id": "123",
        "flow_id": "f1",
        "flow_name": "Audit",
        "process_name": "Cat 1",
        "error_details": "ValueError: boom",
        "source": "LOCAL_SCRIPT",
        "correlation_id": "c",
    }


def test_config_requires_url_and_canonical_platform(monkeypatch):
    monkeypatch.delenv("ERROR_ROUTER_URL", raising=False)
    with pytest.raises(RuntimeError):
        error_router.load_router_config()
    monkeypatch.setenv("ERROR_ROUTER_URL", "http://x")
    monkeypatch.setenv("PLATFORM_NAME", "aws-lambda")
    with pytest.raises(RuntimeError):
        error_router.load_router_config()


def test_parse_categories():
    assert cli.parse_categories("3,1", set(range(1, 11))) == [1, 3]
    assert cli.parse_categories(None, {1, 2}) == [1, 2]
    with pytest.raises(ValueError):
        cli.parse_categories("11", set(range(1, 11)))


def test_main_posts_to_router_and_reraises_on_bad_key(monkeypatch, tmp_path):
    posted = []
    monkeypatch.setenv("ERROR_ROUTER_URL", "http://router.test")
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", "bad-key")
    monkeypatch.setenv("PROSPECT_NAME", "Acme")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "handle_failure", lambda cfg, exc, ctx: posted.append((exc, dict(ctx)))
    )
    monkeypatch.setattr("requests.Session.request", fake_request(FakePortal()))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(AuthError):
        cli.main(["run", "--output-dir", str(tmp_path)])
    assert isinstance(posted[0][0], AuthError) and posted[0][1]["process_name"] == "Probing scopes"


def test_main_happy_path_writes_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("ERROR_ROUTER_URL", "http://router.test")
    monkeypatch.setenv("HUBSPOT_SERVICE_KEY", SERVICE_KEY)
    monkeypatch.setenv("PROSPECT_NAME", "Acme")
    monkeypatch.chdir(tmp_path)
    portal = FakePortal()
    monkeypatch.setattr("requests.Session.request", fake_request(portal))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    assert cli.main(["run", "--categories", "1,3", "--output-dir", str(tmp_path / "out")]) == 0
    assert list((tmp_path / "out").glob("*/audit_bundle.json"))
