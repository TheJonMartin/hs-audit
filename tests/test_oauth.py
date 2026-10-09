import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import FakePortal, FakeResponse

from hubspot_audit import auth as auth_mod
from hubspot_audit.auth import OAuthAuth, StaticKeyAuth, run_loopback_flow
from hubspot_audit.client import HubSpotClient
from hubspot_audit.errors import AuthError

REDIRECT = "http://localhost:8765/callback"


class TokenSession:
    """Fake token endpoint. Each grant issues a numbered token valid for ``ttl`` seconds."""

    def __init__(self, ttl=1800, fail=False):
        self.ttl, self.fail, self.posts, self.n = ttl, fail, [], 0

    def post(self, url, data=None, timeout=None):
        self.posts.append(data)
        if self.fail:
            return FakeResponse(400, {"message": "bad code SECRETCODE"})
        self.n += 1
        return FakeResponse(
            200, {"access_token": f"at-{self.n}", "refresh_token": "rt-1", "expires_in": self.ttl}
        )

    def get(self, url, timeout=None):
        return FakeResponse(200, {"hub_id": 99, "scopes": ["b", "a"], "user": "x@example.com"})


def make(session=None, clock=None, refresh="rt-1"):
    return OAuthAuth(
        "cid",
        "csecret",
        REDIRECT,
        refresh_token=refresh,
        session=session or TokenSession(),
        clock=clock or time.time,
    )


def test_authorization_url_has_scopes_state_and_optional():
    url = make().authorization_url(["a.read", "b.read"], ["c.read"], "st")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["a.read b.read"] and q["optional_scope"] == ["c.read"]
    assert q["state"] == ["st"] and q["client_id"] == ["cid"] and q["redirect_uri"] == [REDIRECT]


def test_refreshes_when_expiring_and_reuses_when_fresh():
    now = [1000.0]
    session = TokenSession(ttl=1800)
    oauth = make(session, clock=lambda: now[0])
    assert oauth.authorization_header() == "Bearer at-1"
    assert oauth.authorization_header() == "Bearer at-1" and len(session.posts) == 1
    now[0] += 1800 - 60  # inside the refresh margin
    assert oauth.authorization_header() == "Bearer at-2"
    assert session.posts[1]["grant_type"] == "refresh_token"


def test_failure_message_never_echoes_body_or_secrets():
    oauth = make(TokenSession(fail=True))
    with pytest.raises(AuthError) as exc:
        oauth.exchange_code("SECRETCODE")
    assert "SECRETCODE" not in str(exc.value) and "csecret" not in str(exc.value)


def test_no_refresh_token_raises():
    with pytest.raises(AuthError):
        make(refresh=None).authorization_header()


def test_token_info_keeps_only_hub_and_scopes():
    assert make().token_info() == {"hub_id": 99, "scopes": ["a", "b"]}


def test_secrets_list_covers_all_credentials():
    oauth = make()
    oauth.authorization_header()
    assert set(oauth.secrets()) == {"csecret", "rt-1", "at-1"}
    assert StaticKeyAuth("k").secrets() == ["k"]


def test_client_refreshes_once_on_401_and_retries():
    class Portal(FakePortal):
        def request(self, method, url, params=None, json=None, timeout=None):
            self.requests.append((method, url))
            if self.headers.get("Authorization") == "Bearer at-1":
                return FakeResponse(401, {})
            return FakeResponse(200, {"results": [{"id": 1}]})

    session = TokenSession()
    oauth = make(session)
    client = HubSpotClient(auth=oauth, session=Portal(), sleep=lambda _s: None)
    assert client.get("/crm/v3/owners")["results"] == [{"id": 1}]
    assert len(session.posts) == 2  # initial token + one refresh


def test_client_gives_up_on_second_401():
    class Always401(FakePortal):
        def request(self, *a, **k):
            return FakeResponse(401, {})

    client = HubSpotClient(auth=make(), session=Always401(), sleep=lambda _s: None)
    with pytest.raises(AuthError):
        client.get("/crm/v3/owners")


def _hit(url, delay=0.3):
    time.sleep(delay)
    try:
        urllib.request.urlopen(url, timeout=5).read()  # noqa: S310
    except Exception:  # noqa: BLE001 - server closes after one request
        pass


def test_loopback_flow_exchanges_code_with_matching_state():
    session = TokenSession()
    oauth = make(session, refresh=None)
    seen = {}

    def announce(text):
        if text.startswith("http"):
            seen["state"] = parse_qs(urlparse(text).query)["state"][0]
            threading.Thread(
                target=_hit, args=(f"{REDIRECT}?code=thecode&state={seen['state']}",), daemon=True
            ).start()

    run_loopback_flow(oauth, ["a"], [], REDIRECT, announce=announce)
    assert (
        session.posts[0]["code"] == "thecode"
        and session.posts[0]["grant_type"] == "authorization_code"
    )


def test_loopback_flow_rejects_state_mismatch():
    session = TokenSession()

    def announce(text):
        if text.startswith("http"):
            threading.Thread(
                target=_hit, args=(f"{REDIRECT}?code=c&state=forged",), daemon=True
            ).start()

    with pytest.raises(AuthError):
        run_loopback_flow(make(session, refresh=None), ["a"], [], REDIRECT, announce=announce)
    assert session.posts == []


def test_loopback_requires_localhost_redirect():
    with pytest.raises(AuthError):
        run_loopback_flow(make(), ["a"], [], "https://example.com/cb", announce=lambda _t: None)
    assert auth_mod.LOOPBACK_HOST == "127.0.0.1"
