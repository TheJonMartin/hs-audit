"""Authentication providers: Service Key (static) and OAuth 2.0 for a public app."""

from __future__ import annotations

import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .errors import AuthError

AUTHORIZE_URL = "https://app.hubspot.com/oauth/authorize"
OAUTH_EXCHANGE_URL = "https://api.hubapi.com/oauth/v1/token"
OAUTH_INTROSPECT_URL = "https://api.hubapi.com/oauth/v1/access-tokens/"
TOKEN_TIMEOUT_SECONDS = 30
# Refresh this long before expiry so a request never starts with a token about to lapse.
REFRESH_MARGIN_SECONDS = 120
LOOPBACK_TIMEOUT_SECONDS = 300
LOOPBACK_HOST = "127.0.0.1"
CALLBACK_PAGE = (
    b"<html><body><h3>Authorization received. You can close this tab.</h3></body></html>"
)


class AuthProvider(Protocol):
    """Supplies the Authorization header and can refresh it after a 401."""

    def authorization_header(self) -> str:
        """Return the current ``Bearer ...`` header value, refreshing first if needed."""

    def refresh(self) -> bool:
        """Force a refresh. Return True if a new token was obtained."""

    def secrets(self) -> list[str]:
        """Every secret value that must never appear in output."""


class StaticKeyAuth:
    """Service Key authentication."""

    def __init__(self, key: str) -> None:
        if not key:
            raise AuthError("HUBSPOT_SERVICE_KEY is not set.")
        self._key = key

    def authorization_header(self) -> str:
        return f"Bearer {self._key}"

    def refresh(self) -> bool:
        return False

    def secrets(self) -> list[str]:
        return [self._key]


class OAuthAuth:
    """OAuth 2.0 for a HubSpot public app. Tokens live in memory only and are never written."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        refresh_token: str | None = None,
        session: requests.Session | None = None,
        clock=time.time,
    ) -> None:
        if not client_id or not client_secret:
            raise AuthError("HUBSPOT_CLIENT_ID and HUBSPOT_CLIENT_SECRET are required for OAuth.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._session = session or requests.Session()
        self._clock = clock

    # ---- install flow ---------------------------------------------------------------------

    def authorization_url(self, scopes: list[str], optional_scopes: list[str], state: str) -> str:
        """Build the URL the prospect's admin opens to install the app."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
        }
        if optional_scopes:
            params["optional_scope"] = " ".join(optional_scopes)
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def exchange_code(self, code: str) -> None:
        """Trade an authorization code for tokens."""
        self._token_request(
            {"grant_type": "authorization_code", "redirect_uri": self._redirect_uri, "code": code}
        )

    def token_info(self) -> dict[str, Any]:
        """Portal id and the scopes actually granted for the current token (no user data kept)."""
        token = self._valid_access_token()
        try:
            response = self._session.get(
                OAUTH_INTROSPECT_URL + token, timeout=TOKEN_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise AuthError(f"Token info request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise AuthError(f"Token info request failed: HTTP {response.status_code}")
        body = response.json()
        return {"hub_id": body.get("hub_id"), "scopes": sorted(body.get("scopes") or [])}

    # ---- AuthProvider ---------------------------------------------------------------------

    def authorization_header(self) -> str:
        return f"Bearer {self._valid_access_token()}"

    def refresh(self) -> bool:
        if not self._refresh_token:
            return False
        self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
        return True

    def secrets(self) -> list[str]:
        return [s for s in (self._client_secret, self._refresh_token, self._access_token) if s]

    # ---- internals ------------------------------------------------------------------------

    def _valid_access_token(self) -> str:
        if not self._access_token or self._clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
            if not self.refresh():
                raise AuthError(
                    "No valid OAuth token. Run the install flow or set HUBSPOT_REFRESH_TOKEN."
                )
        return self._access_token  # type: ignore[return-value]

    def _token_request(self, grant: dict[str, str]) -> None:
        data = {"client_id": self._client_id, "client_secret": self._client_secret, **grant}
        try:
            response = self._session.post(
                OAUTH_EXCHANGE_URL, data=data, timeout=TOKEN_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            raise AuthError(f"Token request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            # Never echo the body: it can contain the submitted code or token.
            raise AuthError(f"Token request failed: HTTP {response.status_code}")
        body = response.json()
        self._access_token = body["access_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._expires_at = self._clock() + int(body.get("expires_in", 0))


def new_state() -> str:
    """Random CSRF state for the install flow."""
    return secrets.token_urlsafe(24)


def run_loopback_flow(
    oauth: OAuthAuth, scopes: list[str], optional: list[str], redirect_uri: str, announce=print
) -> None:
    """Install via a one-shot listener on localhost. Use when the approver is on a call with us.

    Raises:
        AuthError: timeout, state mismatch, or no code returned.
    """
    parsed = urlparse(redirect_uri)
    if parsed.hostname not in ("localhost", "127.0.0.1") or not parsed.port:
        raise AuthError("Loopback flow needs a localhost redirect URI with a port.")
    state = new_state()
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = parse_qs(urlparse(self.path).query)
            result["code"] = (query.get("code") or [""])[0]
            result["state"] = (query.get("state") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(CALLBACK_PAGE)

        def log_message(self, *args: Any) -> None:  # silence request logging (codes are secrets)
            return

    server = HTTPServer((LOOPBACK_HOST, parsed.port), Handler)
    server.timeout = LOOPBACK_TIMEOUT_SECONDS
    announce("Open this URL as a HubSpot admin of the portal to audit:")
    announce(oauth.authorization_url(scopes, optional, state))
    try:
        deadline = time.time() + LOOPBACK_TIMEOUT_SECONDS
        while "code" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result.get("code"):
        raise AuthError("No authorization code received before the timeout.")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise AuthError("OAuth state mismatch; install aborted.")
    oauth.exchange_code(result["code"])


__all__ = ["AuthProvider", "StaticKeyAuth", "OAuthAuth", "new_state", "run_loopback_flow"]
