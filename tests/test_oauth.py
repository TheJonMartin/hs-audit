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
    """Fake token endpoints. Each grant issues a numbered token valid for ``ttl`` seconds."""

    def __init__(self, ttl=1800, fail=False, hub_in_token=False):
        self.ttl, self.fail, self.hub_in_token = ttl, fail, hub_in_token
        self.posts, self.urls, self.n = [], [], 0

    def post(self, url, data=None, timeout=None):
        self.posts.append(data)
        self.urls.append(url)
        if self.fail:
            return FakeResponse(400, {"message": "bad code SECRETCODE"})
        if url.endswith("/token/introspect"):
            return FakeResponse(
                200, {"active": True, "hub_id": 99, "scopes": ["b", "a"], "user": "x@example.com"}
            )
        if url.endswith("/token/revoke"):
            return FakeResponse(200, {})
        self.n += 1
        body = {"access_token": f"at-{self.n}", "refresh_token": "rt-1", "expires_in": self.ttl}
        if self.hub_in_token:
            body.update({"hub_id": 77, "scopes": ["z"]})
        return FakeResponse(200, body)


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


def test_endpoints_use_dated_oauth_version():
    session = TokenSession()
    make(session).token_info()
    assert all("/oauth/2026-03/" in u for u in session.urls)
    assert not any("/oauth/v1/" in u for u in session.urls)


def test_token_info_falls_back_to_introspect_and_keeps_only_hub_and_scopes():
    session = TokenSession()
    assert make(session).token_info() == {"hub_id": 99, "scopes": ["a", "b"]}
    assert session.urls[-1].endswith("/token/introspect")
    assert (
        session.posts[-1]["token"] == "at-1"
        and session.posts[-1]["token_type_hint"] == "access_token"
    )


def test_token_info_prefers_values_returned_with_the_token():
    session = TokenSession(hub_in_token=True)
    assert make(session).token_info() == {"hub_id": 77, "scopes": ["z"]}
    assert not any(u.endswith("/introspect") for u in session.urls)


def test_revoke_posts_refresh_token_and_failure_is_an_auth_error():
    session = TokenSession()
    make(session).revoke_refresh_token()
    assert session.urls[-1].endswith("/token/revoke") and session.posts[-1]["token"] == "rt-1"
    with pytest.raises(AuthError):
        make(TokenSession(fail=True)).revoke_refresh_token()
    with pytest.raises(AuthError):
        make(refresh=None).revoke_refresh_token()


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


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/cb",
        "http://127.0.0.1:8765/cb",
        "https://localhost:8765/cb",
        "http://localhost/cb",
    ],
)
def test_loopback_requires_http_localhost_with_port(uri):
    with pytest.raises(AuthError):
        run_loopback_flow(make(), ["a"], [], uri, announce=lambda _t: None)
    assert auth_mod.LOOPBACK_HOST == "127.0.0.1"


def test_rotated_refresh_token_triggers_writeback_and_failure_is_fatal():
    class Rotating(TokenSession):
        def post(self, url, data=None, timeout=None):
            self.posts.append(data)
            return FakeResponse(
                200, {"access_token": "at", "refresh_token": "rt-NEW", "expires_in": 1800}
            )

    seen = []
    oauth = make(Rotating(), refresh="rt-1")
    oauth.set_refresh_token("rt-1", on_change=seen.append)
    oauth.authorization_header()
    assert seen == ["rt-NEW"]

    def boom(_token):
        raise AuthError("writeback failed")

    oauth2 = make(Rotating(), refresh="rt-1")
    oauth2.set_refresh_token("rt-1", on_change=boom)
    with pytest.raises(AuthError):
        oauth2.authorization_header()


def test_unchanged_refresh_token_does_not_write_back():
    seen = []
    oauth = make(TokenSession(), refresh="rt-1")  # session returns refresh_token "rt-1"
    oauth.set_refresh_token("rt-1", on_change=seen.append)
    oauth.authorization_header()
    assert seen == []
